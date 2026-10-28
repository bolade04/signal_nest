"""D-LLM-1 option B, cost basis: ``worst_case_cost`` is a bound, not an estimate.

Offline. Pins, for the pure function (``app.llm.bound``) and for the bytes the transport
sends:

* the bound is computed from exactly the bytes an adapter writes (an ``httpx`` spy);
* fail-closed: a model not in the price schedule, an unsupported family, a payload that
  is not one JSON object, a feature the adapters never send (tools, cache controls,
  content blocks, unknown parameters), or a missing output limit is ``Unbounded`` and the
  meter refuses the attempt with the distinct reason ``cost_unbounded`` - never a 0
  reservation;
* rounding is upward at the ledger's 1e-6 and never produces 0 for a request;
* property tests with seeded random Unicode and random byte strings: monotone in every
  input, never below the previous 3-characters-per-token heuristic;
* adversarial inputs (CJK, emoji, zero-width joiners, digit and punctuation runs, a
  prompt built without the system prompt or tools) are all covered by the same function.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from fractions import Fraction

import httpx
import pytest

from app.core.config import Settings
from app.llm.base import LLMError
from app.llm.bound import (
    MESSAGE_OVERHEAD_TOKENS,
    MICRO,
    REQUEST_OVERHEAD_TOKENS,
    Unbounded,
    encode_body,
    worst_case_cost,
)
from app.llm.metering import LLMBudgetExceededError, LLMMeter
from app.llm.providers_real import AnthropicProvider, OpenAIProvider
from app.llm.service import LLMService
from app.tests._llm_offline import offline_llm, offline_settings, wire_payload

MODEL = "claude-sonnet-4-6"
PRICES = Settings().llm_prices_usd_per_mtok
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


def _anthropic_body(**over):
    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": "You are a bound test.",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def _exact(tokens_in: int, tokens_out: int, price_in: str, price_out: str) -> Fraction:
    return (
        Fraction(tokens_in) * Fraction(price_in) + Fraction(tokens_out) * Fraction(price_out)
    ) / (1_000_000)


# --------------------------------------------------------------------------- #
# The derivation
# --------------------------------------------------------------------------- #
def test_the_bound_is_bytes_plus_overheads_plus_the_output_limit_rounded_up():
    body = _anthropic_body()
    raw = encode_body(body)
    cost = worst_case_cost(MODEL, body, PRICES)
    tokens_in = len(raw) + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * 2  # system + user
    exact = _exact(tokens_in, 1024, "3.0", "15.0")
    assert isinstance(cost, Decimal)
    assert Fraction(cost) >= exact
    assert Fraction(cost) - exact < Fraction(1, 1_000_000)  # rounded up by less than 1e-6
    assert cost == cost.quantize(MICRO)
    # bytes, a mapping and a string of the same body are the same request
    assert worst_case_cost(MODEL, raw, PRICES) == cost
    assert worst_case_cost(MODEL, raw.decode(), PRICES) == cost


def test_the_bound_counts_the_whole_body_not_just_the_message():
    base = worst_case_cost(MODEL, _anthropic_body(), PRICES)
    without_system = worst_case_cost(MODEL, _anthropic_body(system=None), PRICES)
    with_metadata = worst_case_cost(
        MODEL, _anthropic_body(metadata={"user_id": "t_" + "a" * 32}), PRICES
    )
    assert without_system < base < with_metadata


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #
UNBOUNDED_CASES = {
    "model_not_priced": (lambda: ("claude-unpriced-9", _anthropic_body(model="claude-unpriced-9"))),
    "model_family_unsupported": (lambda: ("mistral-large", _anthropic_body(model="mistral-large"))),
    "model_mismatch": (lambda: (MODEL, _anthropic_body(model="claude-opus-5"))),
    "payload_not_json": (lambda: (MODEL, b"not json")),
    "payload_not_json_array": (lambda: (MODEL, b"[1,2]")),
    "payload_not_json_nan": (lambda: (MODEL, b'{"max_tokens": NaN}')),
    "payload_not_utf8": (lambda: (MODEL, b'{"model":"\xff\xfe"}')),
    "payload_wrong_type": (lambda: (MODEL, 42)),
    "output_limit_missing": (lambda: (MODEL, _anthropic_body(max_tokens=None))),
    "output_limit_both": (lambda: (MODEL, _anthropic_body(max_completion_tokens=5))),
    "output_limit_zero": (lambda: (MODEL, _anthropic_body(max_tokens=0))),
    "output_limit_bool": (lambda: (MODEL, _anthropic_body(max_tokens=True))),
    "output_limit_float": (lambda: (MODEL, _anthropic_body(max_tokens=10.5))),
    "output_limit_huge": (lambda: (MODEL, _anthropic_body(max_tokens=10**9))),
    "tools": (lambda: (MODEL, _anthropic_body(tools=[{"name": "t"}]))),
    "tool_choice": (lambda: (MODEL, _anthropic_body(tool_choice={"type": "any"}))),
    "top_level_cache_control": (lambda: (MODEL, _anthropic_body(cache_control={"type": "x"}))),
    "thinking": (lambda: (MODEL, _anthropic_body(thinking={"type": "enabled"}))),
    "stream": (lambda: (MODEL, _anthropic_body(stream=True))),
    "content_blocks": (
        lambda: (MODEL, _anthropic_body(messages=[{"role": "user", "content": [{"type": "text"}]}]))
    ),
    "cache_control_in_a_block": (
        lambda: (
            MODEL,
            _anthropic_body(
                system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
            ),
        )
    ),
    "message_extra_key": (
        lambda: (
            MODEL,
            _anthropic_body(messages=[{"role": "user", "content": "x", "name": "n"}]),
        )
    ),
    "message_bad_role": (
        lambda: (MODEL, _anthropic_body(messages=[{"role": "tool", "content": "x"}]))
    ),
    "no_messages": (lambda: (MODEL, _anthropic_body(messages=[]))),
    "metadata_extra": (lambda: (MODEL, _anthropic_body(metadata={"user_id": "t", "x": 1}))),
}


@pytest.mark.parametrize("name", sorted(UNBOUNDED_CASES))
def test_a_request_that_cannot_be_bounded_is_unbounded_never_zero(name):
    model, payload = UNBOUNDED_CASES[name]()
    result = worst_case_cost(model, payload, {**PRICES, "mistral-large": [1.0, 1.0]})
    assert isinstance(result, Unbounded), (name, result)
    assert result.reason


@pytest.mark.parametrize(
    "schedule",
    [
        {MODEL: [3.0]},
        {MODEL: [3.0, 15.0, 1.0]},
        {MODEL: [-1.0, 15.0]},
        {MODEL: [3.0, float("nan")]},
        {MODEL: [float("inf"), 1.0]},
        {MODEL: [True, 15.0]},
        {MODEL: ["3", "15"]},
        {MODEL: None},
        {},
    ],
)
def test_an_invalid_or_missing_price_is_unbounded(schedule):
    assert isinstance(worst_case_cost(MODEL, _anthropic_body(), schedule), Unbounded)


def test_the_openai_shape_is_bounded_and_its_extra_parameters_are_not():
    schedule = {"gpt-4o-mini": [0.15, 0.6]}
    ok = {
        "model": "gpt-4o-mini",
        "temperature": 0.0,
        "max_completion_tokens": 200,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        "user": "t_x",
    }
    assert isinstance(worst_case_cost("gpt-4o-mini", ok, schedule), Decimal)
    for extra in ({"n": 4}, {"tools": []}, {"logprobs": True}, {"max_tokens": 5}):
        assert isinstance(worst_case_cost("gpt-4o-mini", {**ok, **extra}, schedule), Unbounded)
    assert isinstance(
        worst_case_cost("gpt-4o-mini", {**ok, "response_format": {"type": "text"}}, schedule),
        Unbounded,
    )


# --------------------------------------------------------------------------- #
# Rounding at 1e-6
# --------------------------------------------------------------------------- #
def test_tiny_costs_round_up_and_are_never_zero():
    body = _anthropic_body(max_tokens=1, messages=[{"role": "user", "content": ""}], system="")
    tiny = {MODEL: [0.000000001, 0.000000001]}  # a thousandth of a micro-dollar per token
    assert worst_case_cost(MODEL, body, tiny) == MICRO
    zero = {MODEL: [0.0, 0.0]}  # a free price still reserves the minimum unit
    assert worst_case_cost(MODEL, body, zero) == MICRO
    cheapest = {MODEL: [0.0001, 0.0001]}
    result = worst_case_cost(MODEL, body, cheapest)
    assert result == result.quantize(MICRO) and result >= MICRO


def test_rounding_is_always_upward_on_random_prices():
    rng = random.Random(20261027)
    for _ in range(500):
        p_in, p_out = round(rng.uniform(0, 80), 4), round(rng.uniform(0, 400), 4)
        n = rng.randint(1, 3000)
        body = _anthropic_body(max_tokens=rng.randint(1, 4000), system="s" * rng.randint(0, 50))
        body["messages"][0]["content"] = "m" * n
        cost = worst_case_cost(MODEL, body, {MODEL: [p_in, p_out]})
        tokens_in = len(encode_body(body)) + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * 2
        exact = _exact(tokens_in, body["max_tokens"], str(p_in), str(p_out))
        assert Fraction(cost) >= exact and cost >= MICRO
        assert Fraction(cost) - exact < Fraction(1, 1_000_000) or cost == MICRO


# --------------------------------------------------------------------------- #
# Properties: seeded random Unicode and random byte strings
# --------------------------------------------------------------------------- #
def _random_text(rng: random.Random) -> str:
    chars = []
    for _ in range(rng.randint(0, 200)):
        cp = rng.choice(
            [
                rng.randint(0, 0x7F),
                rng.randint(0x80, 0x7FF),
                rng.randint(0x800, 0xFFFF),
                rng.randint(0x10000, 0x10FFFF),
            ]
        )
        if 0xD800 <= cp <= 0xDFFF:
            continue  # a lone surrogate is not UTF-8
        chars.append(chr(cp))
    return "".join(chars)


def _heuristic(prompt_chars: int, max_tokens: int, schedule=PRICES) -> Fraction:
    """The retired 3-characters-per-token estimate, at the same price."""
    p_in, p_out = (Fraction(str(x)) for x in schedule[MODEL])
    return (Fraction(-(-prompt_chars // 3)) * p_in + Fraction(max_tokens) * p_out) / 1_000_000


def test_the_bound_never_falls_below_the_retired_heuristic_on_random_unicode():
    rng = random.Random(1)
    for _ in range(400):
        text, system = _random_text(rng), _random_text(rng)
        max_tokens = rng.randint(1, 4000)
        body = _anthropic_body(
            system=system, max_tokens=max_tokens, messages=[{"role": "user", "content": text}]
        )
        cost = worst_case_cost(MODEL, body, PRICES)
        assert isinstance(cost, Decimal)
        assert Fraction(cost) >= _heuristic(len(system) + len(text), max_tokens)
        # ... and never below one token per UTF-8 byte of every text component
        per_byte = len(system.encode()) + len(text.encode())
        assert Fraction(cost) >= _exact(per_byte, max_tokens, "3.0", "15.0")


def test_the_bound_is_monotone_in_content_output_limit_and_prices():
    rng = random.Random(2)
    for _ in range(200):
        text = _random_text(rng)
        extra = _random_text(rng)
        base = _anthropic_body(messages=[{"role": "user", "content": text}])
        longer = _anthropic_body(messages=[{"role": "user", "content": text + extra}])
        assert worst_case_cost(MODEL, longer, PRICES) >= worst_case_cost(MODEL, base, PRICES)
        more_out = {**base, "max_tokens": base["max_tokens"] + rng.randint(0, 500)}
        assert worst_case_cost(MODEL, more_out, PRICES) >= worst_case_cost(MODEL, base, PRICES)
        pricier = {MODEL: [3.0 + rng.random(), 15.0 + rng.random()]}
        assert worst_case_cost(MODEL, base, pricier) >= worst_case_cost(MODEL, base, PRICES)
        more_messages = {
            **base,
            "messages": base["messages"] + [{"role": "assistant", "content": ""}],
        }
        assert worst_case_cost(MODEL, more_messages, PRICES) >= worst_case_cost(MODEL, base, PRICES)


def test_random_byte_strings_are_bounded_or_refused_never_an_error_or_zero():
    rng = random.Random(3)
    for _ in range(500):
        blob = bytes(rng.randrange(256) for _ in range(rng.randint(0, 300)))
        result = worst_case_cost(MODEL, blob, PRICES)
        assert isinstance(result, Unbounded) or result >= MICRO
    # valid JSON built from random bytes (latin-1 view): still bounded by the true byte count
    for _ in range(200):
        blob = bytes(rng.randrange(256) for _ in range(rng.randint(0, 300)))
        text = blob.decode("latin-1")
        body = _anthropic_body(messages=[{"role": "user", "content": text}])
        raw = encode_body(body)
        cost = worst_case_cost(MODEL, raw, PRICES)
        assert Fraction(cost) >= _exact(len(raw), 1024, "3.0", "15.0")


ADVERSARIAL = {
    "cjk": "漢字" * 800,
    "emoji": "😀👩‍👩‍👧‍👦" * 400,
    "zero_width_joiners": "‍" * 3000,
    "combining_marks": "a" + "́" * 3000,
    "digits": "1234567890" * 400,
    "punctuation": "!@#$%^&*()_+{}|:<>?" * 200,
    "single_char_run": "a" * 6000,
    "null_and_control": "\x00\x01\x02\x1b[0m" * 500,
    "special_token_text": "<|endoftext|><|im_start|>" * 300,
    "rtl_and_private_use": "‮" * 800,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_token_dense_adversarial_content_is_bounded_at_one_token_per_byte(name):
    text = ADVERSARIAL[name]
    body = _anthropic_body(messages=[{"role": "user", "content": text}])
    cost = worst_case_cost(MODEL, body, PRICES)
    assert isinstance(cost, Decimal)
    # The worst conceivable tokenisation of this text: one token per byte, 1024 out.
    assert Fraction(cost) >= _exact(len(text.encode("utf-8")), 1024, "3.0", "15.0")
    # ... and it exceeds what the 3-chars-per-token heuristic would have reserved
    assert Fraction(cost) > _heuristic(len(text) + len(body["system"]), 1024)


def test_cjk_and_emoji_are_where_the_heuristic_was_not_a_bound():
    """One CJK character is 3 bytes and can be 1 token: the heuristic charged a third of
    that. The byte bound is at least three times higher on such text (minus overheads)."""
    text = "漢" * 3000
    body = _anthropic_body(messages=[{"role": "user", "content": text}], max_tokens=1)
    heuristic = _heuristic(len(text), 1)
    assert Fraction(worst_case_cost(MODEL, body, PRICES)) >= 3 * heuristic * Fraction(95, 100)


def test_a_body_built_without_system_or_tools_is_bounded_by_the_same_function():
    minimal = {"model": MODEL, "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}
    cost = worst_case_cost(MODEL, minimal, PRICES)
    assert isinstance(cost, Decimal) and cost >= MICRO
    assert worst_case_cost(MODEL, {**minimal, "tools": []}, PRICES) == Unbounded(
        "payload_feature_not_priced", "tools"
    )


# --------------------------------------------------------------------------- #
# The bound sees the exact bytes the transport sends (httpx spy)
# --------------------------------------------------------------------------- #
class _Spy:
    def __init__(self, reply):
        self.reply = reply
        self.sent: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.read())
        return self.reply


def _anthropic_reply():
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": json.dumps(CLASSIFIED)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def _openai_reply():
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(CLASSIFIED)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


class _BoundSpy(LLMMeter):
    """Records the bytes the service hands the meter, before they are bounded."""

    seen: list[bytes | None]

    def before_attempt(self, provider, model, payload):
        self.seen.append(payload)
        return super().before_attempt(provider, model, payload)


@pytest.mark.parametrize("content", ["plain", "漢字😀", "‍" * 50, ""])
@pytest.mark.parametrize("which", ["anthropic", "openai"])
def test_the_bytes_that_are_bounded_are_the_bytes_the_transport_sends(which, content):
    model = MODEL if which == "anthropic" else "gpt-4o-mini"
    prices = {**PRICES, "gpt-4o-mini": [0.15, 0.6]}
    settings = offline_settings(
        llm_provider=which, llm_api_key="k", llm_model=model, llm_prices_usd_per_mtok=prices
    )
    spy = _Spy(_anthropic_reply() if which == "anthropic" else _openai_reply())
    client = httpx.Client(transport=httpx.MockTransport(spy))
    cls = AnthropicProvider if which == "anthropic" else OpenAIProvider
    service = LLMService(cls("k", model, 5, client=client), settings=settings)
    meter = _BoundSpy(settings)
    meter.seen = []
    meter._tenant_ref = "t_" + "9" * 32  # bound by hand: no database needed for the spy
    meter._workspace_id = meter._organization_id = "x"
    meter._bound = True
    service.run("classify_signal", {"content": content}, meter=meter)
    assert len(spy.sent) == 1 and meter.seen == spy.sent  # byte for byte
    sent = spy.sent[0]
    assert sent == encode_body(json.loads(sent))  # canonical: the bytes ARE the encoding
    assert "t_" + "9" * 32 in sent.decode()  # the tenant reference is in the bounded bytes
    expected = worst_case_cost(model, sent, prices)
    assert isinstance(expected, Decimal)
    assert meter.worst_case(which, model, spy.sent[0]) == expected


def test_a_system_prompt_dropped_from_the_body_would_lower_the_bound():
    """The system prompt is in the bytes the bound sees (it is in the request body)."""
    sent = wire_payload(MODEL, content="x")
    body = json.loads(sent)
    assert body["system"]
    without = worst_case_cost(MODEL, {k: v for k, v in body.items() if k != "system"}, PRICES)
    assert worst_case_cost(MODEL, sent, PRICES) > without


# --------------------------------------------------------------------------- #
# The meter refuses what it cannot bound
# --------------------------------------------------------------------------- #
class _NoWireProvider:
    name = "anthropic"
    model = MODEL

    def __init__(self):
        self.calls = 0

    def generate(self, request, prompt_name, prompt_version):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("a provider call was made for an unbounded request")


def _meter(settings) -> LLMMeter:
    meter = LLMMeter(settings)
    meter._tenant_ref, meter._workspace_id, meter._organization_id = "t", "w", "o"
    meter._bound = True
    return meter


def test_the_meter_refuses_a_provider_that_cannot_show_its_bytes():
    settings = offline_settings(llm_provider="anthropic", llm_api_key="k", llm_model=MODEL)
    provider = _NoWireProvider()
    meter = _meter(settings)
    with pytest.raises(LLMBudgetExceededError) as exc:
        LLMService(provider, settings=settings).run(
            "classify_signal", {"content": "x"}, meter=meter
        )
    assert (exc.value.reason, exc.value.detail) == ("cost_unbounded", "no_wire_payload")
    assert provider.calls == 0 and meter.refusals == {"cost_unbounded": 1} and meter.attempts == 0


def test_the_meter_refuses_a_model_missing_from_the_price_table_before_any_request():
    model = "claude-unpriced-9"
    settings = offline_settings(llm_provider="anthropic", llm_api_key="k", llm_model=model)
    spy = _Spy(_anthropic_reply())
    provider = AnthropicProvider(
        "k", model, 5, client=httpx.Client(transport=httpx.MockTransport(spy))
    )
    meter = _meter(settings)
    with pytest.raises(LLMBudgetExceededError) as exc:
        LLMService(provider, settings=settings).run(
            "classify_signal", {"content": "x"}, meter=meter
        )
    assert (exc.value.reason, exc.value.detail) == ("cost_unbounded", "model_not_priced")
    assert spy.sent == [] and meter.spent == 0.0


def test_the_unbounded_refusal_never_reserves_zero_on_the_shared_ledger(tmp_path):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation
    from app.organizations.models import Organization, Workspace

    engine = create_engine(f"sqlite:///{tmp_path / 'b.db'}", future=True)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with make() as db:
        db.add(Organization(id="org-b", name="B", slug="b"))
        db.add(Workspace(id="ws-b", organization_id="org-b", name="B", slug="b"))
        db.commit()
    settings = offline_settings(
        llm_provider="anthropic", llm_api_key="k", llm_model="claude-unpriced-9"
    )
    meter = LLMMeter(settings, session_factory=make, shared_ledger=True)
    with make() as db:
        meter.bind(db, organization_id="org-b", workspace_id="ws-b", scout_request_id=None)
    provider = AnthropicProvider(
        "k", "claude-unpriced-9", 5, client=httpx.Client(transport=httpx.MockTransport(_Spy(None)))
    )
    with pytest.raises(LLMBudgetExceededError):
        LLMService(provider, settings=settings).run(
            "classify_signal", {"content": "x"}, meter=meter
        )
    with make() as db:
        assert db.scalar(select(func.count()).select_from(LLMBudgetReservation)) == 0
        assert db.scalar(select(func.count()).select_from(LLMBudgetPeriod)) == 0
    engine.dispose()


#: Outcome classifier (default-deny): only an error positively known to precede dispatch
#: (ConnectError) and a 4xx / 429 reply (the provider stated the request was not processed)
#: are free; every other outcome is charged the call's bound and still has its usage row.
FREE = "free"
BOUND = "bound"


def _gzip_garbage(request):
    return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"not gzip at all")


CLASSIFIER_CASES = {
    "ConnectError": (httpx.ConnectError("refused"), FREE),
    "HTTP 400": (httpx.Response(400, json={"type": "error", "error": {}}), FREE),
    "HTTP 401": (httpx.Response(401, json={"type": "error", "error": {}}), FREE),
    "HTTP 404": (httpx.Response(404, json={"type": "error", "error": {}}), FREE),
    "HTTP 408": (httpx.Response(408, json={"type": "error", "error": {}}), FREE),
    "HTTP 409": (httpx.Response(409, json={"type": "error", "error": {}}), FREE),
    "HTTP 413": (httpx.Response(413, json={"type": "error", "error": {}}), FREE),
    "HTTP 429": (httpx.Response(429, json={"type": "error", "error": {}}), FREE),
    "HTTP 500": (httpx.Response(500, json={"type": "error", "error": {}}), BOUND),
    "HTTP 502": (httpx.Response(502, text="bad gateway"), BOUND),
    "HTTP 503": (httpx.Response(503, text="unavailable"), BOUND),
    "HTTP 504": (httpx.Response(504, text="gateway timeout"), BOUND),
    "HTTP 529": (httpx.Response(529, json={"type": "error", "error": {}}), BOUND),
    "HTTP 302": (httpx.Response(302, headers={"location": "https://x.invalid/"}), BOUND),
    "ConnectTimeout": (httpx.ConnectTimeout("t"), BOUND),
    "PoolTimeout": (httpx.PoolTimeout("t"), BOUND),
    "ReadTimeout": (httpx.ReadTimeout("t"), BOUND),
    "WriteTimeout": (httpx.WriteTimeout("t"), BOUND),
    "ReadError": (httpx.ReadError("r"), BOUND),
    "WriteError": (httpx.WriteError("w"), BOUND),
    "CloseError": (httpx.CloseError("c"), BOUND),
    "RemoteProtocolError": (httpx.RemoteProtocolError("p"), BOUND),
    "LocalProtocolError": (httpx.LocalProtocolError("p"), BOUND),
    "ProxyError": (httpx.ProxyError("p"), BOUND),
    "UnsupportedProtocol": (httpx.UnsupportedProtocol("u"), BOUND),
    "NetworkError": (httpx.NetworkError("n"), BOUND),
    "DecodingError": (_gzip_garbage, BOUND),
    "TooManyRedirects": (httpx.TooManyRedirects("m"), BOUND),
}


@pytest.mark.parametrize("name", sorted(CLASSIFIER_CASES))
def test_the_outcome_classifier_is_default_deny(name):
    step, expected = CLASSIFIER_CASES[name]
    settings = offline_settings(
        llm_provider="anthropic", llm_api_key="k", llm_model=MODEL, llm_max_retries=0
    )

    def handler(request):
        if callable(step):
            return step(request)
        if isinstance(step, Exception):
            raise step
        return step

    provider = AnthropicProvider(
        "k", MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    meter = _meter(settings)
    with pytest.raises(LLMError):
        LLMService(provider, settings=settings).run(
            "classify_signal", {"content": "x"}, meter=meter
        )
    worst = float(meter.worst_case("anthropic", MODEL, provider.prepare(_request("x")).content))
    assert meter.spent == (0.0 if expected == FREE else pytest.approx(worst)), name
    assert len(meter._records) == 1  # the attempt has its usage record either way
    assert meter._records[0]["cost_usd"] == meter.spent


def _request(content: str):
    from app.llm.base import LLMRequest

    return LLMRequest(
        task="classify_signal",
        inputs={"content": content},
        max_tokens=offline_settings().llm_max_output_tokens,
        tenant_ref="t",
    )


# --------------------------------------------------------------------------- #
# F4: one input price tier - long-context requests fail closed unless the tier is named
# --------------------------------------------------------------------------- #
def test_a_request_above_the_single_tier_threshold_is_unbounded_unless_the_tier_is_named():
    from app.llm.bound import SINGLE_TIER_INPUT_TOKEN_THRESHOLD

    big = "x" * (SINGLE_TIER_INPUT_TOKEN_THRESHOLD + 10)
    body = _anthropic_body(messages=[{"role": "user", "content": big}])
    assert worst_case_cost(MODEL, body, PRICES) == Unbounded(
        "long_context_tier_unpriced", "input bound above the single tier"
    )
    # just below the threshold is priced at the single tier
    edge = "x" * (SINGLE_TIER_INPUT_TOKEN_THRESHOLD - 2000)
    ok = _anthropic_body(messages=[{"role": "user", "content": edge}])
    assert isinstance(worst_case_cost(MODEL, ok, PRICES), Decimal)
    # a schedule that names the tier prices the whole request at it
    tiered = {MODEL: [3.0, 15.0, 6.0, 22.5]}
    cost = worst_case_cost(MODEL, body, tiered)
    raw = encode_body(body)
    tokens_in = len(raw) + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * 2
    assert Fraction(cost) >= _exact(tokens_in, 1024, "6.0", "22.5")
    assert Fraction(cost) - _exact(tokens_in, 1024, "6.0", "22.5") < Fraction(1, 1_000_000)
    # below the threshold a tiered schedule still prices at the first tier
    assert worst_case_cost(MODEL, ok, tiered) == worst_case_cost(MODEL, ok, PRICES)
    # a malformed tier (three elements, a negative tier price) is invalid
    assert isinstance(worst_case_cost(MODEL, ok, {MODEL: [3.0, 15.0, 6.0]}), Unbounded)
    assert isinstance(worst_case_cost(MODEL, body, {MODEL: [3.0, 15.0, -1.0, 2.0]}), Unbounded)


def test_the_meter_refuses_a_long_context_body_with_the_distinct_reason():
    settings = offline_settings(
        llm_provider="anthropic", llm_api_key="k", llm_model=MODEL, llm_max_untrusted_chars=50000
    )
    spy = _Spy(_anthropic_reply())
    provider = AnthropicProvider(
        "k", MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(spy))
    )
    meter = _meter(settings)
    # a prompt this large cannot be built from the capped inputs; bound the bytes directly
    huge = json.dumps(_anthropic_body(messages=[{"role": "user", "content": "y" * 300_000}]))
    with pytest.raises(LLMBudgetExceededError) as exc:
        meter.before_attempt("anthropic", MODEL, huge.encode())
    assert (exc.value.reason, exc.value.detail) == ("cost_unbounded", "long_context_tier_unpriced")
    assert spy.sent == [] and provider is not None


# --------------------------------------------------------------------------- #
# r4-MINOR-1: whatever the decode path raises is an undecodable reply -> the bound
# --------------------------------------------------------------------------- #
_NESTING = 200_000  # deep enough to raise RecursionError in the json decoder


def _deep(open_: str = "[", close: str = "]") -> str:
    return open_ * _NESTING + close * _NESTING


def _anthropic_text_reply(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def _openai_text_reply(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


DECODE_CASES = {
    # the whole 200 body is a deeply nested document: resp.json() raises RecursionError
    "nested_body": lambda reply: httpx.Response(200, content=_deep().encode()),
    # the envelope is fine, the model's text is deeply nested: json.loads raises it
    "nested_text": lambda reply: reply(_deep()),
    "nested_text_in_object": lambda reply: reply(_deep('{"a":', "}")),
    "invalid_utf8_body": lambda reply: httpx.Response(200, content=b"\xff\xfe\x00 not utf-8"),
}


@pytest.mark.parametrize("case", sorted(DECODE_CASES))
@pytest.mark.parametrize("which", ["anthropic", "openai"])
def test_an_undecodable_reply_is_malformed_and_charged_the_bound(which, case):
    from app.llm.base import LLMMalformedError

    model = MODEL if which == "anthropic" else "gpt-4o-mini"
    prices = {**PRICES, "gpt-4o-mini": [0.15, 0.6]}
    settings = offline_settings(
        llm_provider=which,
        llm_api_key="k",
        llm_model=model,
        llm_max_retries=0,
        llm_prices_usd_per_mtok=prices,
    )
    reply = _anthropic_text_reply if which == "anthropic" else _openai_text_reply
    response = DECODE_CASES[case](reply)
    cls = AnthropicProvider if which == "anthropic" else OpenAIProvider
    provider = cls(
        "k", model, 5, client=httpx.Client(transport=httpx.MockTransport(lambda r: response))
    )
    meter = _meter(settings)
    with pytest.raises(LLMMalformedError):  # never a RecursionError / ValueError escape
        LLMService(provider, settings=settings).run(
            "classify_signal", {"content": "x"}, meter=meter
        )
    prepared = provider.prepare(_request("x")).content
    worst = float(meter.worst_case(which, model, prepared))
    if case.endswith("_body"):  # no usable envelope, no usage block: the bound
        assert meter.spent == pytest.approx(worst) and worst > 0
    else:  # the envelope carried a usage block (10 in, 5 out): charged what it reported
        assert 0 < meter.spent < worst
        assert meter._records[0]["input_tokens"] == 10
    assert [r["outcome"] for r in meter._records] == ["LLMMalformedError"]
