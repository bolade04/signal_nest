"""LLM cost accounting, per-run cap, per-workspace budget and tenant reference (P6-LLM-4).

* **Price table** - ``Settings.llm_prices_usd_per_mtok`` maps a model id to
  ``[input, output]`` USD per million tokens, with its source and date in
  ``Settings.llm_price_source``. A model missing from the table is charged at the
  table's most expensive rates (fail-closed: an unknown model can never look free).
  The mock provider costs nothing.
* **Per-run cap** - at most ``llm_max_calls_per_run`` provider *attempts*
  (provider retries included) per scout run. On the durable worker path a run is one
  ``scout_request.execute`` job, and the cap spans all of that job's worker attempts.
  Every usage row carries its job's id, and each attempt's meter starts from the
  number of rows recorded for that same job. Another run of the request (a manual
  run while this job waits to retry) has its own job, so it never uses this run's
  cap. An attempt whose rows were never recorded (see "Failed attempts") is not
  seen.
* **Per-workspace budget** - the workspace's model spend in a calendar month (UTC)
  must stay within ``llm_workspace_monthly_budget_usd``. Before each provider call
  the call's worst case is reserved against the month: :func:`app.llm.bound.worst_case_cost`
  of the exact request bytes (UTF-8 bytes bound the input tokens, plus stated overhead
  constants, plus the request's output limit, at the model's price, rounded up at
  1e-6). A call whose cost cannot be bounded is not made.
* **Refusal state** - a run is refused at the API with HTTP 429
  ``llm_budget_exhausted`` once the month's budget is spent
  (:func:`ensure_run_allowed`); inside a run, an attempt that would cross the cap or
  the budget is not made (:class:`LLMBudgetExceededError`) and the pipeline marks
  the affected output ``budget_limited``.
* **Usage records** - one ``llm_usage_events`` row per attempt: task, provider,
  model, tokens, cost and outcome. No prompt, content or model text.
* **Tenant reference** - :func:`tenant_reference` derives an opaque identifier
  from the workspace id with an HMAC under the application secret; it is sent to
  the provider (Anthropic ``metadata.user_id``, OpenAI ``user``) so provider-side
  abuse signals are per tenant, and it reveals no id, name or PII. Rotating
  ``SECRET_KEY`` changes every reference.

Budget guarantee (D-LLM-1 option B; ledger in :mod:`app.llm.budget`)
=====================================================================

**The guarantee.** On PostgreSQL, the sum of the worst-case cost bounds of all
provider calls started by a workspace in a UTC calendar month never exceeds the
workspace budget as configured at reservation time, across any number of processes. A
call's bound is `worst_case_cost` of the exact request bytes under the configured
price schedule (UTF-8 bytes bound the tokens, plus stated overhead constants, plus the
request's output limit, in exact decimal arithmetic rounded up at 1e-6), and a call
whose cost cannot be bounded is not made. This is the application's defined budget
guarantee; it is not a statement about the provider's invoice. Not covered: provider-
side billing differences from the price schedule, including the tokenizer premise and
overhead constants the bound rests on (stated in `app.llm.bound`); calls made outside
the metered service (E6 probe); budget changes mid-month lowering below already-
reserved amounts; SQLite, where the guarantee degrades to per-session (documented,
tests pin the degraded behaviour).

**How.** With a session factory (the durable worker gives the meter one) on a
PostgreSQL bind, the meter runs the *shared-ledger* path: before every provider
call it reserves the call's worst case in a short transaction of its own, on a
second session, with one conditional ``UPDATE`` of the workspace's
``llm_budget_periods`` row (``reserved_usd + w <= budget``). The reservation is
committed before the call is made, so every other process sees it at once; a
refused reservation is :class:`LLMBudgetExceededError` (``workspace_budget``) and
no call is made. After the call the reservation is settled in another short
transaction, keyed by the attempt (job id, worker attempt, call ordinal), and the
usage row is written in that same transaction - at settlement, not at run end. A
second settlement of the same key is a no-op.

**Cost basis (the application's, not the provider's).** The reserved amount ``w`` is
``worst_case_cost`` of the request bytes the transport sends (:mod:`app.llm.bound`
states the derivation, its premises with their sources, the pricing components it
covers - input and output tokens; cache, tool and thinking charges are excluded because
the request cannot carry them - and every fail-closed case). A request that cannot be
bounded is refused with the distinct reason ``cost_unbounded`` and reserves nothing.
``w`` is what the call consumes of the budget: settlement records the charge but never
changes the consumed amount, so the sum the guarantee speaks of is exactly the ledger's
``reserved_usd``; the run endpoint's 429 reports that amount. **Invariant: actual <=
reserved.** If a provider ever reports usage above ``w`` (the bound's premise failed),
the charge stays ``w`` - an after-the-fact refusal cannot repair an exceeded budget, so
``reserved_usd`` must never be what exceeds it - and the event is logged at ERROR,
counted (``llm_cost_bound_violations_total``) and recorded on the period
(``bound_violations``); the usage row keeps the reported figures. The outcome
classifier is **default-deny**: an attempt is free only if the provider is known not to
have processed the request - an ``httpx.ConnectError`` (raised while connecting, before any
request byte is written; the only exception class known to precede dispatch) and an HTTP 4xx
or 429 reply (the provider stated the request was not processed). Every other outcome may
have reached the provider and settles at ``w`` and still writes its usage row: a timeout of
any phase (``ConnectTimeout`` and ``PoolTimeout`` are not proven byte-free), a read, write
or protocol error, ``DecodingError``, ``TooManyRedirects``, any other ``httpx`` error, an
HTTP 5xx reply (a gateway's 502/503/504 does not say the origin did not process it), an
undecodable reply and a reply carrying no usage block (the last two are a conservative basis
pending D-LLM-3, not an accepted rule). A rejected or rate-limited reply (4xx, 429) is charged
nothing at settlement but has consumed its ``w`` of the month at reservation, and the
service's retries multiply that: a 429 storm consumes (``llm_max_retries`` + 1) x ``w`` per
logical call with no spend (a 5xx storm consumes that and is charged it). The ledger bounds what the
application *starts* under its own price schedule; the provider's invoice remains the only
statement of what was billed.

**Unknown outcomes.** If the process dies between reserving and settling, the
reservation stays at ``w`` and is the record of that call. Recovery (the worker's
sweep of reservations past ``expires_at``, a job reaching a terminal state, or the
job's next attempt starting) releases it at ``w``: the worst case is kept as spend
for the month. **An unknown outcome is charged its worst case.** A leaked
reservation therefore under-spends the budget, never over-spends it, and nothing
that was reserved is unrecorded: a reserved-but-unsettled call has its reservation
row, a settled call has both rows. If the settlement write itself fails, the
reservation likewise stays at ``w`` until recovery. If the reservation cannot be
written (database unavailable), the attempt raises and no call is made. When
recovery releases a reservation before its own process settles it (a call that
outlived the expiry window on a job whose lease is gone, or a lost attempt still
running), the worst case stands as the charge and the late settlement still writes
the call's usage row - exactly once per attempt, however many processes or repeats
settle late (a conditional claim) - logged at WARNING (``llm.settlement_after_release``)
and counted (``llm_settlements_after_release_total``); the sweep never expires a
reservation whose job still holds a live lease. A settlement, release or sweep that finds
its workspace or organization deleted writes nothing: it logs and counts
(``llm_settlements_orphaned_total``), and never recreates a period, reservation or usage
row.

**SQLite (and any meter without a session factory): the same-session path.** A
second connection cannot reserve while the run's own session holds the database's
single write lock, so there the meter keeps today's behaviour: it reads committed
month-to-date spend once, when it starts, checks ``month_to_date + spent + w <=
budget`` in memory before each call, and writes its rows at run end in the run's
transaction (usage rows, plus already-settled reservation rows and the period row
so the ledger has the same shape on both dialects). Runs in flight together see
each other's spend only once committed: with N such runs of one workspace the
month can be exceeded by up to (N - 1) x the remainder each read at its start, plus
one attempt's estimate error each. That is the per-session guarantee the SQLite
tests pin; it is not the PostgreSQL guarantee.

Failed attempts (same-session path): a run's rows are written in its own
transaction. On the durable worker path they are also re-written after an attempt's
rollback (see :class:`LLMMeter`), so a failed or retried attempt is counted and the
next attempt's budget check and call cap see it. The writing fails closed: if that
write itself fails, the worker fails the job for good instead of retrying it into
spend that no budget check can see (``app.jobs.worker``). On the shared-ledger path
every call is committed at settlement, so an attempt's rollback loses none of them.
Gaps that remain on the same-session path:

* a worker process that dies during an attempt records nothing for that attempt,
  because the meter is held in memory;
* an after-rollback write that fails after a lease loss cannot fail the job, because
  another worker owns it;
* after a terminal failure with unrecorded spend, a later run of the request, manual
  or scheduled, starts from the recorded month-to-date only;
* a correlated fault: if the after-rollback write fails AND recording the job's
  terminal failure also fails (a database outage), the job is left ``running``.
  Lease recovery then requeues it, and it spends again unrecorded until a failure
  can be recorded. This is bounded by ``job_default_max_attempts`` x the run's call
  cap. Nothing durable marks the lost write, so recovery cannot tell this job apart.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import SignalNestError
from app.core.logging import get_logger
from app.llm import budget as ledger
from app.llm.base import LLMError, LLMMalformedError, LLMResponse, LLMTimeoutError
from app.llm.bound import Unbounded, worst_case_cost
from app.llm.models import LLMUsageEvent

logger = get_logger("signalnest.llm")

SessionFactory = Callable[[], Session]

#: A ledger transaction that loses a deadlock / serialization conflict is run again, at
#: most this many times in all (the conflict is transient: the other side has won).
_CONFLICT_ATTEMPTS = 3


class LLMBudgetExceededError(LLMError):
    """An attempt was not made because it would cross the run cap or the budget."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        #: "run_call_cap" | "workspace_budget" | "cost_unbounded"
        self.reason = reason
        #: For "cost_unbounded", why the cost could not be bounded
        #: (:class:`app.llm.bound.Unbounded` ``reason``); otherwise ``None``.
        self.detail: str | None = None


class LLMMeterBindingError(RuntimeError):
    """A meter was bound a second time, or after it had recorded an attempt.

    A programming error, not an LLM outcome: it is deliberately not an ``LLMError``,
    so no LLM failure handling (fallback, ``budget_limited`` marking) absorbs it.
    """


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
    """The month's consumed budget: the larger of the usage rows' sum and the period's
    ``reserved_usd`` (worst-case basis: calls in flight and unknown outcomes included).
    On the same-session path the two agree; on the shared-ledger path the period leads."""
    since = _month_start(now or datetime.now(UTC))
    total = db.scalar(
        select(func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0)).where(
            LLMUsageEvent.workspace_id == workspace_id, LLMUsageEvent.created_at >= since
        )
    )
    reserved = ledger.reserved_month_to_date_usd(db, workspace_id, now)
    return max(float(total or 0.0), float(reserved))


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

    Bound to its workspace once the run has loaded its scout request. Two paths:

    * **Shared ledger** (``session_factory`` given and the bind is PostgreSQL, or
      ``shared_ledger=True``): each call is reserved before it is made and settled
      after it, each in a short transaction of its own on a session from the factory;
      the usage row is written at settlement. :meth:`flush` then has nothing to write.
    * **Same session** (otherwise): attempts are kept in memory and written by
      :meth:`flush` in the run's own transaction. On the durable worker path
      (``app.jobs.handlers.execute_scout_request``), :meth:`flush` is also registered
      as an after-rollback write. When an attempt fails, or loses its lease at
      completion, the worker rolls the attempt back and commits the same rows in a
      fresh transaction. Money spent by a failed attempt is therefore still counted,
      and the next attempt's budget check sees it.

    Ownership is fixed: :meth:`bind` succeeds at most once, and only on a meter that
    has recorded nothing; any further :meth:`bind` raises
    :class:`LLMMeterBindingError` before changing anything. ``job_id`` and ``attempt``
    (set at construction) and the owner fields set by :meth:`bind`
    (``organization_id``, ``workspace_id``, ``scout_request_id``, ``tenant_ref``) are
    read-only. A meter's records, counters and every row it writes therefore belong
    to one run of one tenant. Build a new meter for each run (the durable handler
    builds one per attempt).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        job_id: str | None = None,
        attempt: int = 0,
        session_factory: SessionFactory | None = None,
        shared_ledger: bool | None = None,
    ):
        self.settings = settings
        #: The durable job this run is. Its earlier attempts' usage rows count
        #: towards the call cap, and every row this meter writes carries it.
        self._job_id = job_id
        #: The worker attempt ordinal of this run (0 off the worker path); part of
        #: every attempt key, so a retried job's reservations never collide.
        self._attempt = attempt
        self._session_factory = session_factory
        #: ``None``: decide at bind from the dialect. ``True``/``False``: forced
        #: (tests pin both paths on SQLite).
        self._shared_requested = shared_ledger
        self._shared = False
        #: Off the worker path there is no job id: a per-meter token keys the attempts.
        self._run_key = job_id or f"run-{uuid.uuid4().hex}"
        #: The reservation awaiting settlement: (attempt_key, worst case) or None.
        self._pending: tuple[str, Decimal] | None = None
        #: The bound of the attempt in flight (set by :meth:`before_attempt`).
        self._worst: Decimal | None = None
        self._bound = False
        self.prior_attempts = 0
        self._organization_id: str | None = None
        self._workspace_id: str | None = None
        self._scout_request_id: str | None = None
        self._tenant_ref: str | None = None
        self.month_to_date = 0.0
        self.attempts = 0
        self.spent = 0.0
        self.refusals: dict[str, int] = {}
        self._records: list[dict[str, Any]] = []
        #: The bound of each record, in step (same-session path).
        self._bounds: list[Decimal] = []

    def bind(
        self,
        db: Session,
        *,
        organization_id: str,
        workspace_id: str,
        scout_request_id: str | None,
    ) -> LLMMeter:
        """Bind this meter, once, to the run's tenant and request.

        Raises :class:`LLMMeterBindingError`, changing nothing, if the meter is
        already bound or has already recorded an attempt. On the shared-ledger path
        the reservations an earlier attempt of this job left in flight (its lease was
        lost, or it died) are released first, at their worst case.
        """
        if self._bound:
            raise LLMMeterBindingError(
                "this LLM meter is already bound to a run; build a new meter per run"
            )
        if self._records or self.attempts or self.spent or self.refusals or self._pending:
            raise LLMMeterBindingError(
                "this LLM meter already holds attempts from before it was bound; "
                "build a new meter per run"
            )
        shared = self._shared_requested
        if shared is None:
            shared = (
                self._session_factory is not None and db.get_bind().dialect.name == "postgresql"
            )
        if shared and self._session_factory is None:
            raise LLMMeterBindingError("a shared-ledger meter needs a session factory")
        # Read everything first, so a failing query leaves the meter unbound.
        tenant_ref = tenant_reference(self.settings, workspace_id)
        month_to_date = month_to_date_usd(db, workspace_id)
        prior_attempts = 0
        if self._job_id is not None:
            prior_attempts = int(
                db.scalar(
                    select(func.count())
                    .select_from(LLMUsageEvent)
                    .where(LLMUsageEvent.job_id == self._job_id)
                )
                or 0
            )
        if shared and self._job_id is not None:
            self._in_own_transaction(
                lambda s: ledger.release_for_job(
                    s, job_id=self._job_id, now=datetime.now(UTC), outcome="rebind"
                )
            )
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._scout_request_id = scout_request_id
        self._tenant_ref = tenant_ref
        self.month_to_date = month_to_date
        self.prior_attempts = prior_attempts
        self.attempts = prior_attempts
        self._shared = bool(shared)
        self._bound = True
        return self

    @property
    def job_id(self) -> str | None:
        return self._job_id

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def shared_ledger(self) -> bool:
        """Whether this meter reserves and settles through the shared ledger."""
        return self._shared

    @property
    def organization_id(self) -> str | None:
        return self._organization_id

    @property
    def workspace_id(self) -> str | None:
        return self._workspace_id

    @property
    def scout_request_id(self) -> str | None:
        return self._scout_request_id

    @property
    def tenant_ref(self) -> str | None:
        return self._tenant_ref

    def worst_case(
        self, provider: str, model: str, payload: bytes | None
    ) -> Decimal | Unbounded:
        """The attempt's worst-case cost bound (:func:`app.llm.bound.worst_case_cost`).

        ``payload`` is the request body exactly as the transport sends it; ``None``
        (a provider that cannot show its bytes) is unbounded. The mock costs nothing.
        """
        if provider == "mock":
            return Decimal(0)
        if payload is None:
            return Unbounded("no_wire_payload")
        return worst_case_cost(model, payload, self.settings.llm_prices_usd_per_mtok)

    def attempt_key(self, call: int) -> str:
        """The key of this run's ``call``-th call: job (or run token), attempt, call."""
        return f"{self._run_key}:{self._attempt}:{call}"

    def _in_own_transaction(self, work: Callable[[Session], Any]) -> Any:
        """Run ``work`` on a session of its own and commit it; roll back on any error.

        A deadlock or serialization failure (SQLSTATE 40P01 / 40001) is retried, on a
        fresh session, at most :data:`_CONFLICT_ATTEMPTS` times in all. A rolled-back
        attempt leaves nothing behind (the exactly-once usage claim included), so a retry
        can neither lose nor duplicate a usage row. Any other error is raised at once.
        """
        assert self._session_factory is not None
        for attempt in range(1, _CONFLICT_ATTEMPTS + 1):
            session = self._session_factory()
            try:
                result = work(session)
                session.commit()
                return result
            except BaseException as exc:
                session.rollback()
                if attempt == _CONFLICT_ATTEMPTS or not ledger.is_retryable_conflict(exc):
                    raise
                logger.warning(
                    "llm.ledger_transaction_retry",
                    extra={"extra_fields": {"attempt": attempt}},
                )
                time.sleep(0.05 * attempt)
            finally:
                session.close()
        raise AssertionError("unreachable")  # pragma: no cover

    def before_attempt(self, provider: str, model: str, payload: bytes | None):
        """Raise :class:`LLMBudgetExceededError` instead of making a disallowed attempt.

        ``payload`` is the request body exactly as it will be sent. A request whose cost
        cannot be bounded is refused with reason ``cost_unbounded`` (never reserved as
        zero). On the shared-ledger path the budget check is the reservation itself: the
        call's worst case is reserved, and committed, before this returns.
        """
        self._worst = None
        if provider != "mock":
            if self.attempts >= self.settings.llm_max_calls_per_run:
                self._refuse("run_call_cap")
            worst = self.worst_case(provider, model, payload)
            if isinstance(worst, Unbounded):
                self._refuse("cost_unbounded", detail=worst.reason)
            budget = self.settings.llm_workspace_monthly_budget_usd
            if self._shared:
                self._reserve(worst, ledger.usd(budget))
            elif self.month_to_date + self.spent + float(worst) > budget:
                self._refuse("workspace_budget")
            self._worst = worst
        self.attempts += 1

    def _reserve(self, worst: Decimal, budget: Decimal) -> None:
        assert self._workspace_id is not None and self._organization_id is not None
        key = self.attempt_key(self.attempts - self.prior_attempts)
        now = datetime.now(UTC)
        reserved = self._in_own_transaction(
            lambda s: ledger.reserve(
                s,
                workspace_id=self._workspace_id,
                organization_id=self._organization_id,
                job_id=self._job_id,
                attempt_key=key,
                worst_usd=worst,
                budget_usd=budget,
                expires_at=ledger.reservation_expiry(self.settings, now),
                now=now,
            )
        )
        if not reserved:
            self._refuse("workspace_budget")
        self._pending = (key, worst)

    def _refuse(self, reason: str, *, detail: str | None = None) -> None:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        error = LLMBudgetExceededError(
            f"LLM call not made: {reason}" + (f" ({detail})" if detail else ""), reason
        )
        error.detail = detail
        raise error

    def _charge(
        self,
        provider: str,
        model: str,
        worst: Decimal,
        usage,
        response: LLMResponse | None,
        error: Exception | None,
    ) -> float:
        """The conservative cost of one attempt (see the module docstring)."""
        if provider == "mock":
            return 0.0
        if usage is not None and (usage.input_tokens or usage.output_tokens):
            return cost_usd(self.settings, provider, model, usage.input_tokens, usage.output_tokens)
        if response is not None or usage is not None:
            return float(worst)  # the provider replied but reported no usage: the worst case
        if isinstance(error, LLMTimeoutError):
            return float(worst)  # the provider may have processed it
        if isinstance(error, LLMMalformedError):
            return float(worst)  # an undecodable reply: conservative basis pending D-LLM-3
        if getattr(error, "outcome_unknown", False):
            return float(worst)  # request sent, no response: unknown, not known-free
        return 0.0  # rejected, rate limited, unavailable or never connected

    def after_attempt(
        self,
        *,
        task: str,
        provider: str,
        model: str,
        response: LLMResponse | None = None,
        error: Exception | None = None,
    ) -> float:
        if self._worst is None and provider != "mock":
            raise LLMMeterBindingError("after_attempt without a before_attempt that bounded it")
        worst = self._worst if self._worst is not None else Decimal(0)
        self._worst = None
        usage = response.usage if response is not None else getattr(error, "usage", None)
        tokens_in = usage.input_tokens if usage else 0
        tokens_out = usage.output_tokens if usage else 0
        cost = self._charge(provider, model, worst, usage, response, error)
        self.spent = round(self.spent + cost, 6)
        record = {
            "task": task,
            "provider": provider,
            "model": model,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": cost,
            "outcome": "ok" if error is None else type(error).__name__,
        }
        if self._shared and self._pending is not None:
            key, _worst = self._pending
            self._pending = None
            self._in_own_transaction(
                lambda s: ledger.settle(
                    s,
                    attempt_key=key,
                    settled_usd=ledger.usd(cost),
                    now=datetime.now(UTC),
                    usage={"scout_request_id": self._scout_request_id, **record},
                )
            )
        else:
            self._records.append(record)
            self._bounds.append(worst)
        return cost

    def summary(self) -> dict[str, Any]:
        return {
            "llm_attempts": self.attempts,
            "llm_prior_attempts": self.prior_attempts,
            "llm_cost_usd": self.spent,
            "llm_refusals": dict(self.refusals),
        }

    def flush(self, db: Session) -> None:
        """Add this run's unsettled usage to ``db``'s current transaction.

        Same-session path: one usage row per recorded attempt, the matching settled
        reservation rows and the period row, idempotently per attempt key. On the
        shared-ledger path every call was written at settlement, so this adds nothing.
        """
        if self.workspace_id is None or self.organization_id is None:
            return
        now = datetime.now(UTC)
        for call, record in enumerate(self._records):
            ledger.record_settled(
                db,
                workspace_id=self.workspace_id,
                organization_id=self.organization_id,
                job_id=self.job_id,
                attempt_key=self.attempt_key(call),
                settled_usd=ledger.usd(record["cost_usd"]),
                now=now,
                usage={"scout_request_id": self.scout_request_id, **record},
                worst_usd=self._bounds[call] if record["provider"] != "mock" else None,
            )
