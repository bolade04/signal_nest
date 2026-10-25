"""LLM cost accounting, per-run cap, per-workspace budget and tenant reference (P6-LLM-4).

* **Price table** - ``Settings.llm_prices_usd_per_mtok`` maps a model id to
  ``[input, output]`` USD per million tokens, with its source and date in
  ``Settings.llm_price_source``. A model missing from the table is charged at the
  table's most expensive rates (fail-closed: an unknown model can never look free).
  The mock provider costs nothing.
* **Per-run cap** - at most ``llm_max_calls_per_run`` provider *attempts*
  (provider retries included) per scout run. On the durable worker path a run is one
  ``scout_request.execute`` job, and the cap spans all of that job's worker attempts.
  Each attempt's meter starts from the usage rows this scout request has recorded
  since the job was enqueued (``run_started_at``). An attempt whose rows were never
  recorded (see "Failed attempts") is not seen.
* **Per-workspace budget** - month-to-date spend (calendar month, UTC) plus this
  run's spend plus the worst case of the next attempt must stay within
  ``llm_workspace_monthly_budget_usd``. The worst case assumes ~3 characters per
  input token and a reply of ``max_tokens``. That over-estimates for most English
  text, but it is an estimate, not a bound: text that tokenises at fewer than 3
  characters per token (CJK, emoji, some symbols) is under-estimated. The same
  estimate is the charge for a timed-out attempt.
* **Refusal state** - a run is refused at the API with HTTP 429
  ``llm_budget_exhausted`` once the month's budget is spent
  (:func:`ensure_run_allowed`); inside a run, an attempt that would cross the cap or
  the budget is not made (:class:`LLMBudgetExceededError`) and the pipeline marks
  the affected output ``budget_limited``.
* **Usage records** - one ``llm_usage_events`` row per attempt: task, provider,
  model, tokens, cost and outcome. No prompt, content or model text. A successful
  reply is charged its reported usage; a reply that arrived but was unusable
  (truncated, refused, non-JSON) is charged its reported usage when the provider
  sent one; a timeout is charged the worst case (the provider may have processed
  it); a 4xx/429/5xx/connection failure is charged nothing.
* **Tenant reference** - :func:`tenant_reference` derives an opaque identifier
  from the workspace id with an HMAC under the application secret; it is sent to
  the provider (Anthropic ``metadata.user_id``, OpenAI ``user``) so provider-side
  abuse signals are per tenant, and it reveals no id, name or PII. Rotating
  ``SECRET_KEY`` changes every reference.

Concurrency: a run reads month-to-date spend once, when it starts, and sees
other runs' spend only once they have recorded it. With N runs of one workspace in
flight together (``worker_concurrency`` above 1, several workers, or several
requests), each can spend up to the budget remaining at its own start. Together they
can therefore exceed the budget by up to (N - 1) x that remainder, plus one
attempt's worst case each. Each run's total is still bounded by its call cap. There
is no reservation in the database, and this bound is stated, not hidden.

Failed attempts: a run's rows are written in its own transaction. On the durable
worker path they are also re-written after an attempt's rollback (see
:class:`LLMMeter`), so a failed or retried attempt is counted and the next
attempt's budget check and call cap see it. The writing fails closed: if that write
itself fails, the worker fails the job for good instead of retrying it into spend
that no budget check can see (``app.jobs.worker``). Gaps that remain:

* a worker process that dies during an attempt records nothing for that attempt,
  because the meter is held in memory;
* an after-rollback write that fails after a lease loss cannot fail the job, because
  another worker owns it;
* after a terminal failure with unrecorded spend, a later run of the request, manual
  or scheduled, starts from the recorded month-to-date only.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import SignalNestError
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMResponse, LLMTimeoutError
from app.llm.models import LLMUsageEvent

logger = get_logger("signalnest.llm")

_CHARS_PER_TOKEN = 3  # low for English (over-estimates); CJK/emoji can be under it


class LLMBudgetExceededError(LLMError):
    """An attempt was not made because it would cross the run cap or the budget."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason  # "run_call_cap" | "workspace_budget"


class LLMBudgetExhaustedError(SignalNestError):
    status_code = 429
    code = "llm_budget_exhausted"


def tenant_reference(settings: Settings, workspace_id: str) -> str:
    digest = hmac.new(
        settings.secret_key.encode(), f"llm-tenant:{workspace_id}".encode(), hashlib.sha256
    )
    return "t_" + digest.hexdigest()[:32]


def prices(settings: Settings, provider: str, model: str) -> tuple[float, float]:
    if provider == "mock":
        return 0.0, 0.0
    table = settings.llm_prices_usd_per_mtok
    if model in table:
        return float(table[model][0]), float(table[model][1])
    logger.warning("llm.price_unknown", extra={"extra_fields": {"model": model}})
    return (
        max((float(p[0]) for p in table.values()), default=0.0),
        max((float(p[1]) for p in table.values()), default=0.0),
    )


def cost_usd(
    settings: Settings, provider: str, model: str, tokens_in: int, tokens_out: int
) -> float:
    price_in, price_out = prices(settings, provider, model)
    return round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)


def _month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_to_date_usd(db: Session, workspace_id: str, now: datetime | None = None) -> float:
    since = _month_start(now or datetime.now(UTC))
    total = db.scalar(
        select(func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0)).where(
            LLMUsageEvent.workspace_id == workspace_id, LLMUsageEvent.created_at >= since
        )
    )
    return float(total or 0.0)


def ensure_run_allowed(db: Session, settings: Settings, workspace_id: str) -> None:
    """Refuse to start a run once the workspace's monthly LLM budget is spent."""
    if settings.llm_provider == "mock":
        return
    spent = month_to_date_usd(db, workspace_id)
    budget = settings.llm_workspace_monthly_budget_usd
    if spent >= budget:
        raise LLMBudgetExhaustedError(
            f"This workspace has used its AI budget for this month (USD {spent:.2f} of "
            f"{budget:.2f}). Runs can start again next month, or after an operator raises "
            "the budget."
        )


class LLMMeter:
    """Accounting and limits for the model calls of ONE scout run.

    Bound to its workspace once the run has loaded its scout request. Attempts are
    kept in memory and written by :meth:`flush` in the run's own transaction. On the
    durable worker path (``app.jobs.handlers.execute_scout_request``), :meth:`flush` is
    also registered as an after-rollback write. When an attempt fails, or loses its
    lease at completion, the worker rolls the attempt back and commits the same rows
    in a fresh transaction. Money spent by a failed attempt is therefore still
    counted, and the next attempt's budget check sees it.
    """

    def __init__(self, settings: Settings, *, run_started_at: datetime | None = None):
        self.settings = settings
        #: When the run began (its job's enqueue time). Usage rows this scout request
        #: recorded since then are earlier attempts of the same run, and count
        #: towards the call cap.
        self.run_started_at = run_started_at
        self.prior_attempts = 0
        self.organization_id: str | None = None
        self.workspace_id: str | None = None
        self.scout_request_id: str | None = None
        self.tenant_ref: str | None = None
        self.month_to_date = 0.0
        self.attempts = 0
        self.spent = 0.0
        self.refusals: dict[str, int] = {}
        self._records: list[dict[str, Any]] = []

    def bind(
        self,
        db: Session,
        *,
        organization_id: str,
        workspace_id: str,
        scout_request_id: str | None,
    ) -> LLMMeter:
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self.scout_request_id = scout_request_id
        self.tenant_ref = tenant_reference(self.settings, workspace_id)
        self.month_to_date = month_to_date_usd(db, workspace_id)
        if self.run_started_at is not None and scout_request_id is not None:
            self.prior_attempts = int(
                db.scalar(
                    select(func.count()).select_from(LLMUsageEvent).where(
                        LLMUsageEvent.scout_request_id == scout_request_id,
                        LLMUsageEvent.created_at >= self.run_started_at,
                    )
                )
                or 0
            )
            self.attempts = self.prior_attempts
        return self

    def worst_case_usd(self, provider: str, model: str, prompt_chars: int, max_tokens: int):
        tokens_in = -(-prompt_chars // _CHARS_PER_TOKEN)
        return cost_usd(self.settings, provider, model, tokens_in, max_tokens)

    def before_attempt(self, provider: str, model: str, prompt_chars: int, max_tokens: int):
        """Raise :class:`LLMBudgetExceededError` instead of making a disallowed attempt."""
        if provider != "mock":
            if self.attempts >= self.settings.llm_max_calls_per_run:
                self._refuse("run_call_cap")
            worst = self.worst_case_usd(provider, model, prompt_chars, max_tokens)
            budget = self.settings.llm_workspace_monthly_budget_usd
            if self.month_to_date + self.spent + worst > budget:
                self._refuse("workspace_budget")
        self.attempts += 1

    def _refuse(self, reason: str) -> None:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        raise LLMBudgetExceededError(f"LLM call not made: {reason}", reason)

    def after_attempt(
        self,
        *,
        task: str,
        provider: str,
        model: str,
        prompt_chars: int,
        max_tokens: int,
        response: LLMResponse | None = None,
        error: Exception | None = None,
    ) -> float:
        usage = response.usage if response is not None else getattr(error, "usage", None)
        tokens_in = usage.input_tokens if usage else 0
        tokens_out = usage.output_tokens if usage else 0
        if usage is not None:
            cost = cost_usd(self.settings, provider, model, tokens_in, tokens_out)
        elif isinstance(error, LLMTimeoutError):
            cost = self.worst_case_usd(provider, model, prompt_chars, max_tokens)
        else:
            cost = 0.0
        self.spent = round(self.spent + cost, 6)
        self._records.append(
            {
                "task": task,
                "provider": provider,
                "model": model,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cost_usd": cost,
                "outcome": "ok" if error is None else type(error).__name__,
            }
        )
        return cost

    def summary(self) -> dict[str, Any]:
        return {
            "llm_attempts": self.attempts,
            "llm_prior_attempts": self.prior_attempts,
            "llm_cost_usd": self.spent,
            "llm_refusals": dict(self.refusals),
        }

    def flush(self, db: Session) -> None:
        """Add one usage row per recorded attempt to ``db``'s current transaction."""
        if self.workspace_id is None:
            return
        db.add_all(
            LLMUsageEvent(
                organization_id=self.organization_id,
                workspace_id=self.workspace_id,
                scout_request_id=self.scout_request_id,
                **record,
            )
            for record in self._records
        )
