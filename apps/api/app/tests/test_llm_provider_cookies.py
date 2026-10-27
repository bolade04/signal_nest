"""P6-LLM-4 hardening: provider cookies never carry from one tenant's call to another's.

``app.llm.service.llm_service`` holds one provider for the whole process, and the
provider holds one ``httpx.Client``. An httpx client keeps a cookie jar by default, so
a ``Set-Cookie`` on the reply to tenant A's call would be sent back with tenant B's
next request. The adapter therefore gives its client a cookie jar whose policy refuses
every cookie (``providers_real._NoCookiePolicy``): the jar stays empty, so there is no
shared cookie state to leak, to clear or to race on.

These tests drive the real adapter and the real ``httpx.Client`` the adapter builds
for itself. Only the network I/O is replaced: ``httpx.HTTPTransport.handle_request``
answers offline, and every reply sets a cookie naming the tenant it answered. Real
HTTP stays refused (``app.tests._llm_offline``).
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from app.llm.base import LLMRequest
from app.llm.providers_real import AnthropicProvider, OpenAIProvider
from app.tests._llm_offline import offline_llm

KEY = "fixture-platform-key"
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
TENANTS = ("t_tenant_A", "t_tenant_B", "t_tenant_C", "t_tenant_D")


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


def _tenant_of(provider_name: str, body: dict) -> str:
    if provider_name == "anthropic":
        return body["metadata"]["user_id"]
    return body["user"]


def _reply_body(provider_name: str) -> dict:
    if provider_name == "anthropic":
        return {
            "content": [{"type": "text", "text": json.dumps(CLASSIFIED)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    return {
        "choices": [{"message": {"content": json.dumps(CLASSIFIED)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


class CookieSettingProvider:
    """Answers every request offline with a ``Set-Cookie`` naming the request's tenant.

    Installed as ``httpx.HTTPTransport.handle_request``, so the adapter's own default
    client (``httpx.Client(timeout=...)``, real ``HTTPTransport``) makes the request.
    Records, per request, the tenant and the ``Cookie`` header it carried.
    """

    def __init__(self, provider_name: str, *, rendezvous: int = 0):
        self.provider_name = provider_name
        self.lock = threading.Lock()
        self.seen: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.barrier = threading.Barrier(rendezvous) if rendezvous > 1 else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        tenant = _tenant_of(self.provider_name, json.loads(request.content))
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.seen.append(
                {
                    "tenant": tenant,
                    "cookie": request.headers.get("cookie"),
                    "thread": threading.current_thread().name,
                }
            )
        try:
            if self.barrier is not None:
                # Every tenant's request is in flight before any reply is returned.
                self.barrier.wait(timeout=10)
        finally:
            with self.lock:
                self.in_flight -= 1
        return httpx.Response(
            200,
            json=_reply_body(self.provider_name),
            headers=[
                ("set-cookie", f"edge_session={tenant}; Path=/"),
                ("set-cookie", f"lb_affinity={tenant}; Domain=.{request.url.host}; Path=/"),
            ],
        )


def _provider(name: str):
    cls = AnthropicProvider if name == "anthropic" else OpenAIProvider
    # No client injected: the adapter builds its own, exactly as ``llm_service`` does.
    return cls(KEY, "", 5)


def _call(provider, tenant: str) -> None:
    response = provider.generate(
        LLMRequest(task="classify_signal", inputs={"content": "x"}, tenant_ref=tenant),
        "classify_signal",
        "v1",
    )
    assert response.output == CLASSIFIED


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_sequential_tenants_never_send_a_cookie_from_an_earlier_reply(name, monkeypatch):
    fake = CookieSettingProvider(name)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _self, req: fake.handle(req))
    provider = _provider(name)

    for tenant in ("t_tenant_A", "t_tenant_B", "t_tenant_A"):
        _call(provider, tenant)

    assert [s["tenant"] for s in fake.seen] == ["t_tenant_A", "t_tenant_B", "t_tenant_A"]
    # B's request (and A's second one) carry no cookie set on any earlier reply.
    assert [s["cookie"] for s in fake.seen] == [None, None, None]
    assert len(provider._client.cookies) == 0


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_overlapping_tenants_never_send_any_cookie(name, monkeypatch):
    """Four tenants in flight at once, twice over, on ONE provider and ONE client.

    Round 1: all four requests are held at a barrier until all four are in flight, so
    the calls really overlap; then four replies each set that tenant's cookies.
    Round 2 starts only after every round-1 reply has been processed by the client
    (a second barrier), so a stored cookie from any tenant would be on every round-2
    request.
    """
    n = len(TENANTS)
    fake = CookieSettingProvider(name, rendezvous=n)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _self, req: fake.handle(req))
    provider = _provider(name)
    between_rounds = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(tenant: str) -> None:
        try:
            _call(provider, tenant)
            between_rounds.wait(timeout=10)
            _call(provider, tenant)
        except BaseException as exc:  # surfaced below, with the thread's tenant
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(tenant,), name=f"tenant-{i}")
        for i, tenant in enumerate(TENANTS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert not any(t.is_alive() for t in threads)
    # The overlap really happened: every request of a round was in flight together,
    # and neither barrier was broken (a timeout would have raised in a worker).
    assert fake.max_in_flight == n
    assert not fake.barrier.broken and not between_rounds.broken
    assert len(fake.seen) == 2 * n
    assert sorted(s["tenant"] for s in fake.seen) == sorted(TENANTS * 2)
    assert len({s["thread"] for s in fake.seen}) == n
    # No request, in either round, carried any cookie.
    assert [s["cookie"] for s in fake.seen] == [None] * (2 * n)
    assert len(provider._client.cookies) == 0


def test_an_injected_client_cannot_bring_cookies_either():
    """``client=`` (the E6 live probe and the replay tests use it) gets the same jar."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            json=_reply_body("anthropic"),
            headers={"set-cookie": "edge_session=from-a-reply; Path=/"},
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), cookies={"preloaded": "from-the-caller"}
    )
    provider = AnthropicProvider(KEY, "", 5, client=client)
    _call(provider, "t_tenant_A")
    _call(provider, "t_tenant_B")

    # The cookie the caller built the client with is gone, and no reply's cookie stuck.
    assert seen == [None, None]
    assert len(client.cookies) == 0


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_a_cookie_cannot_slip_between_one_tenants_reply_and_anothers_request(name, monkeypatch):
    """A forced interleaving that defeats "clear a shared jar around each request".

    Clearing a shared jar before and/or after each ``_post`` leaves windows where tenant
    A's reply cookie is in the jar while tenant B's request is being built. This test
    forces exactly that, on the adapter's default client:

    1. A's request reaches the (offline) transport and is held there;
    2. B enters ``_post`` (past any "clear before" step) and is held at the start of
       ``Client.build_request``, before its cookies are merged;
    3. A's reply is released; httpx extracts its cookie into the client's jar, then A
       is held in a response hook, i.e. before any "clear after" step in ``_post``;
    4. B's request is built and sent while A is still held; then both finish.

    With a jar that can hold A's cookie, B's request carries it. With the adapter's
    jar, nothing is ever stored, so B's request carries no cookie.
    """
    b_building, a_extracted, b_sent = threading.Event(), threading.Event(), threading.Event()
    seen: dict[str, str | None] = {}
    original_build = httpx.Client.build_request

    def handle(_self, request: httpx.Request) -> httpx.Response:
        tenant = _tenant_of(name, json.loads(request.content))
        seen[tenant] = request.headers.get("cookie")
        if tenant == "t_tenant_A":
            assert b_building.wait(timeout=5), "B never reached build_request"
        else:
            b_sent.set()
        return httpx.Response(
            200,
            json=_reply_body(name),
            headers={"set-cookie": f"edge_session={tenant}; Path=/"},
        )

    def build_request(self, method, url, **kwargs):
        body = kwargs.get("json")
        if isinstance(body, dict) and _tenant_of(name, body) == "t_tenant_B":
            b_building.set()
            assert a_extracted.wait(timeout=5), "A's reply was never processed"
        return original_build(self, method, url, **kwargs)

    def hold_a_after_extraction(response: httpx.Response) -> None:
        if _tenant_of(name, json.loads(response.request.content)) == "t_tenant_A":
            a_extracted.set()
            assert b_sent.wait(timeout=5), "B's request was never sent"

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.Client, "build_request", build_request)
    provider = _provider(name)  # default construction, as ``llm_service`` builds it
    provider._client.event_hooks = {"response": [hold_a_after_extraction]}
    errors: list[BaseException] = []

    def call(tenant: str) -> None:
        try:
            _call(provider, tenant)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    a = threading.Thread(target=call, args=("t_tenant_A",), name="tenant-A")
    b = threading.Thread(target=call, args=("t_tenant_B",), name="tenant-B")
    a.start()
    b.start()
    a.join(timeout=15)
    b.join(timeout=15)

    assert errors == [] and not a.is_alive() and not b.is_alive()
    # The interleaving really happened, in the order above.
    assert b_building.is_set() and a_extracted.is_set() and b_sent.is_set()
    assert seen == {"t_tenant_A": None, "t_tenant_B": None}
    assert len(provider._client.cookies) == 0


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_the_adapters_client_jar_refuses_every_cookie(name):
    from app.llm.providers_real import _NoCookiePolicy

    jar = _provider(name)._client.cookies.jar
    assert isinstance(jar._policy, _NoCookiePolicy)
    assert len(jar) == 0
