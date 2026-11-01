"""Project Phase 5A exit criterion 5 (provider side): the mock, the prompt registry and
fail-closed rendering.

* determinism of the seeded :class:`~app.llm.mock.MockLLMProvider` (same seed + inputs =>
  identical output);
* every failure branch of the mock's ``_simulate`` inputs marker;
* a golden test over ``PROMPTS`` (and the system prompt): a template that changes without a
  version bump fails here;
* fail-closed rendering: a template/input mismatch raises and nothing is sent, reserved or
  charged, and the dev fallback is not taken.

Offline throughout: the real adapter runs over ``httpx.MockTransport`` only.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from app.llm import prompts as prompts_module
from app.llm.base import (
    LLMMalformedError,
    LLMPromptRenderError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequest,
    LLMTimeoutError,
)
from app.llm.mock import MockLLMProvider
from app.llm.prompts import PROMPTS, SYSTEM_PROMPT, Prompt, render_prompt, template_fields
from app.llm.providers_real import AnthropicProvider, OpenAIProvider
from app.llm.schemas import TASK_SCHEMAS
from app.llm.service import LLMService
from app.tests._llm_offline import TEST_TENANT, offline_llm, offline_settings

CLASSIFY = {"content": "Where can I buy a quieter blender? Mine is so slow.", "audiences": None}


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


def _request(task: str = "classify_signal", inputs: dict | None = None) -> LLMRequest:
    return LLMRequest(task=task, inputs=CLASSIFY if inputs is None else inputs)


# --------------------------------------------------------------------------- #
# The seeded mock: determinism
# --------------------------------------------------------------------------- #
class TestMockDeterminism:
    @pytest.mark.parametrize("task", sorted(TASK_SCHEMAS))
    def test_same_seed_and_inputs_give_identical_output(self, mock_llm_provider, task):
        inputs = {
            f: [f"value for {f}"] if f == "evidence" else f"value for {f}"
            for f in template_fields(PROMPTS[task].template)
        }
        twin = MockLLMProvider(model=mock_llm_provider.model, seed=mock_llm_provider.seed)
        a = mock_llm_provider.generate(_request(task, inputs), task, "v")
        b = twin.generate(_request(task, inputs), task, "v")
        assert a.output == b.output and a.input_hash == b.input_hash
        assert a.is_simulated and a.status == "ok"
        # Every task's mock output is valid against its schema.
        TASK_SCHEMAS[task].model_validate(a.output)

    def test_the_seed_and_the_inputs_both_enter_the_mock_hash(self, mock_llm_provider):
        base = mock_llm_provider.generate(_request(), "p", "v").input_hash
        other_seed = MockLLMProvider(seed="another-seed").generate(_request(), "p", "v")
        other_inputs = mock_llm_provider.generate(
            _request(inputs={**CLASSIFY, "content": "different"}), "p", "v"
        )
        assert len({base, other_seed.input_hash, other_inputs.input_hash}) == 3

    def test_the_seeded_service_is_deterministic_end_to_end(self, mock_llm_service):
        first = mock_llm_service.run("classify_signal", CLASSIFY, **TEST_TENANT)
        second = mock_llm_service.run("classify_signal", CLASSIFY, **TEST_TENANT)
        assert first[0] == second[0] and first[1].input_hash == second[1].input_hash


# --------------------------------------------------------------------------- #
# The mock's ``_simulate`` marker: every failure branch
# --------------------------------------------------------------------------- #
class TestMockSimulatedFailures:
    @pytest.mark.parametrize(
        "marker, error",
        [
            ("timeout", LLMTimeoutError),
            ("rate_limit", LLMRateLimitError),
            ("refusal", LLMRefusalError),
            ("provider_error", LLMProviderError),
            ("malformed", LLMMalformedError),
        ],
    )
    def test_each_failure_marker_raises_its_normalized_error(self, mock_llm_provider, marker,
                                                             error):
        with pytest.raises(error):
            mock_llm_provider.generate(_request(inputs={**CLASSIFY, "_simulate": marker}),
                                       "p", "v")

    def test_low_confidence_returns_valid_output_marked_low_confidence(self, mock_llm_provider):
        response = mock_llm_provider.generate(
            _request(inputs={**CLASSIFY, "_simulate": "low_confidence"}), "p", "v"
        )
        assert response.status == "low_confidence"
        TASK_SCHEMAS["classify_signal"].model_validate(response.output)

    @pytest.mark.parametrize(
        "marker, error, calls",
        [
            ("timeout", LLMTimeoutError, 3),  # transient: retried, bounded
            ("rate_limit", LLMRateLimitError, 3),
            ("refusal", LLMRefusalError, 1),  # permanent: never retried
            ("provider_error", LLMProviderError, 1),
            ("malformed", LLMMalformedError, 1),
        ],
    )
    def test_through_the_service_transient_markers_retry_and_permanent_ones_do_not(
        self, mock_llm_service, mock_llm_sleeps, marker, error, calls
    ):
        with pytest.raises(error):
            mock_llm_service.run(
                "classify_signal", {**CLASSIFY, "_simulate": marker}, **TEST_TENANT
            )
        # llm_max_retries = 2: three attempts, a wait between attempts, none after the last.
        assert len(mock_llm_sleeps) == calls - 1


# --------------------------------------------------------------------------- #
# Golden prompts: a template change without a version bump fails here
# --------------------------------------------------------------------------- #
#: task -> (prompt name, version, SHA-256 of the template). When you change a template,
#: bump its version in app/llm/prompts.py AND update its row here, in the same commit.
GOLDEN_PROMPTS = {
    "classify_signal": (
        "classify_signal", "1.2.0",
        "812ac0617d6b3c9ea99c6d3b4610741623a59297e9643147459a8a0fa0bf7942",
    ),
    "explain_opportunity": (
        "explain_opportunity", "1.2.0",
        "10bf75dace4e3eb5c1dde597def1d2b819692595213c9536a8f9b9eba3552d89",
    ),
    "check_claim_safety": (
        "check_claim_safety", "1.0.0",
        "23ad60f2c36a742359c9f428c22e1e6f632592b8ef1775d53f1d7cc6d6f96608",
    ),
    "summarize_website": (
        "summarize_website", "1.0.0",
        "f16a14a055459da81090e70c7665c133a60ed3ca648e0e8a85b273155b27f56d",
    ),
    "geo_reasoning": (
        "geo_reasoning", "1.0.0",
        "07178ac5aa69dbe8dd40cbe731531f080a7ad6a5d0ec8d3ec3149b62e4fa3083",
    ),
}
#: The system prompt is sent with every real call and has no version of its own: changing
#: it changes what every prompt version means, so it is pinned too.
GOLDEN_SYSTEM_PROMPT = "41b35114ec07cb475cbb8fae9bf8efac49e03d2268850441c2424b4df5397e0e"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestGoldenPrompts:
    def test_the_registry_holds_exactly_the_pinned_tasks(self):
        assert set(PROMPTS) == set(GOLDEN_PROMPTS) == set(TASK_SCHEMAS)

    @pytest.mark.parametrize("task", sorted(GOLDEN_PROMPTS))
    def test_a_template_never_changes_under_the_same_version(self, task):
        name, version, digest = GOLDEN_PROMPTS[task]
        prompt = PROMPTS[task]
        assert prompt.name == name
        if prompt.version == version:
            assert _digest(prompt.template) == digest, (
                f"the {task} template changed but its version is still {version}: bump the "
                "version in app/llm/prompts.py and update GOLDEN_PROMPTS"
            )
        else:
            pytest.fail(
                f"{task} is now version {prompt.version} (pinned {version}): record the new "
                "version and template digest in GOLDEN_PROMPTS"
            )

    def test_the_system_prompt_is_pinned(self):
        assert _digest(SYSTEM_PROMPT) == GOLDEN_SYSTEM_PROMPT, (
            "SYSTEM_PROMPT changed: bump every prompt version and update the golden digests"
        )

    def test_the_golden_check_catches_an_unbumped_edit(self, monkeypatch):
        # Negative control: an edited template under its old version is caught.
        old = PROMPTS["classify_signal"]
        monkeypatch.setitem(
            PROMPTS, "classify_signal", Prompt(old.name, old.version, old.template + " ")
        )
        with pytest.raises(AssertionError, match="bump the version"):
            self.test_a_template_never_changes_under_the_same_version("classify_signal")


# --------------------------------------------------------------------------- #
# Fail-closed rendering: a mismatch raises and nothing is sent
# --------------------------------------------------------------------------- #
class _Transport:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(500)


def _adapter(cls, transport: _Transport):
    return cls("fixture-key", "claude-sonnet-4-6" if cls is AnthropicProvider else "gpt-4o-mini",
               5, client=httpx.Client(transport=httpx.MockTransport(transport)))


class TestFailClosedRender:
    @pytest.mark.parametrize("cls", [AnthropicProvider, OpenAIProvider])
    def test_a_missing_input_raises_and_nothing_is_sent(self, cls):
        sent = _Transport()
        with pytest.raises(LLMPromptRenderError, match="no input for audiences"):
            _adapter(cls, sent).generate(_request(inputs={"content": "x"}), "p", "v")
        assert sent.requests == []

    def test_none_is_an_explicit_empty_value_not_a_mismatch(self):
        prompt = render_prompt("classify_signal", {"content": "x", "audiences": None}, 100)
        assert '<untrusted_data name="audiences">\n\n</untrusted_data>' in prompt

    @pytest.mark.parametrize(
        "template", ["Positional {0}\n", "Attribute {content.upper}\n", "Index {content[0]}\n",
                     "Unbalanced {content\n", "Bare {}\n", "Spec {content:>{width}}\n"]
    )
    def test_a_template_the_renderer_cannot_fill_raises(self, monkeypatch, template):
        monkeypatch.setitem(PROMPTS, "classify_signal", Prompt("classify_signal", "9", template))
        sent = _Transport()
        inputs = {"content": "x", "audiences": None, "0": "x", "width": "3"}
        with pytest.raises(LLMPromptRenderError):
            _adapter(AnthropicProvider, sent).generate(_request(inputs=inputs), "p", "v")
        assert sent.requests == []

    def test_the_old_unrendered_template_fallback_is_gone(self):
        # The pre-#244 adapter sent the raw template when formatting failed. A render
        # failure now raises; the error names fields only, never input values.
        with pytest.raises(LLMPromptRenderError) as exc:
            render_prompt("explain_opportunity", {"resolved_market": "SECRET-VALUE"}, 100)
        assert "SECRET-VALUE" not in str(exc.value)
        assert "{" not in str(exc.value)

    def test_through_the_service_nothing_is_reserved_charged_retried_or_fallen_back(
        self, monkeypatch
    ):
        from sqlalchemy import create_engine, func, select
        from sqlalchemy.orm import sessionmaker

        from app.db.models import Base
        from app.llm.metering import LLMMeter
        from app.llm.models import LLMUsageEvent
        from app.organizations.models import Organization, Workspace

        engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(engine)
        make = sessionmaker(bind=engine, future=True)
        settings = offline_settings(
            llm_provider="anthropic", llm_api_key="fixture-key", llm_model="claude-sonnet-4-6",
            llm_allow_dev_fallback=True,
        )
        with make() as db:
            db.add(Organization(id="org-r", name="R", slug="org-r"))
            db.flush()
            db.add(Workspace(id="ws-r", organization_id="org-r", name="R", slug="ws-r"))
            db.commit()
            meter = LLMMeter(settings).bind(
                db, organization_id="org-r", workspace_id="ws-r", scout_request_id=None
            )
            sent = _Transport()
            sleeps: list[float] = []
            service = LLMService(_adapter(AnthropicProvider, sent), sleep=sleeps.append,
                                 settings=settings)
            with pytest.raises(LLMPromptRenderError):
                service.run("classify_signal", {"content": "x"}, organization_id="org-r",
                            workspace_id="ws-r", meter=meter)
            meter.flush(db)
            db.commit()
            assert sent.requests == [] and sleeps == []
            assert (meter.attempts, meter.spent, meter.refusals) == (0, 0.0, {})
            assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) == 0
        engine.dispose()

    def test_the_render_check_runs_before_the_first_attempt_without_a_meter(self):
        sent = _Transport()
        sleeps: list[float] = []
        settings = offline_settings(llm_allow_dev_fallback=True)
        service = LLMService(_adapter(AnthropicProvider, sent), sleep=sleeps.append,
                             settings=settings)
        with pytest.raises(LLMPromptRenderError):
            service.run("classify_signal", {"content": "x"}, **TEST_TENANT)
        # Not sent, not retried, and the dev fallback (enabled) was not taken: a mock
        # answer would have been returned instead of the error.
        assert sent.requests == [] and sleeps == []


def test_prompts_module_documents_the_fail_closed_rule():
    assert "Fail-closed rendering" in (prompts_module.__doc__ or "")
