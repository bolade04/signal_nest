"""Scout request pipeline orchestration.

Implements the Phase 2 job chain. A scout run is a durable ``scout_request.execute``
job: the API enqueues it in the database job store and a worker runs :func:`_run`
through the handler in :mod:`app.jobs.handlers`.

Chain: ingest -> normalize -> classify -> dedupe/cluster -> noise filter ->
relevance -> geo-relevance -> validate -> opportunity/confidence score -> decide ->
explain (claim-safe) -> persist opportunities.

Every step is scoped to a single scout request, so results never leak across
locations/markets.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.claims.engine import check_claim_safety, translate_competitor_weakness
from app.connectors.fetch_limits import fetch_stats, partial_fetch_facts
from app.connectors.takedown import active_takedowns, without_taken_down
from app.core.config import get_settings
from app.core.enums import (
    DecisionAction,
    ExplanationSource,
    OpportunityStatus,
    RiskLevel,
    ScoutRequestStatus,
)
from app.core.logging import get_logger
from app.geography.engine import CoverageRule, resolve_geo
from app.geography.markets import market_from_parts
from app.infra.vector import (
    SIGNAL_VOLUME_ABOVE_BOUND,
    SignalVolumeAboveBoundError,
    build_index,
    check_inprocess_bound,
    embed_text,
)
from app.intelligence.analyze import analyze_signal
from app.intelligence.enrichment import get_enricher
from app.intelligence.models import AnalysisInput, OpportunityCandidate
from app.intelligence.persistence import attach_opportunity, persist_intelligence
from app.jobs.admission import (
    REQUEST_GONE,
    SKIPPED_OWNER_ACTIVE,
    STATUS_CHANGED_DURING_RUN,
    admit_operation,
    current_admission,
)
from app.jobs.context import ExecutionContext, JobContextError, scope_matches
from app.llm.base import LLMError
from app.llm.metering import LLMBudgetExceededError, LLMMeter
from app.llm.screening import screen_classification, screen_explanation
from app.llm.service import llm_service
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.opportunities.context import build_business_context
from app.opportunities.models import (
    Opportunity,
    OpportunityScore,
    ValidationEvidence,
)
from app.scoring.decision import DecisionInput, decide
from app.scoring.noise import evaluate_noise
from app.scoring.opportunity import (
    classify_opportunity,
    confidence_level,
    score_confidence,
    score_opportunity,
)
from app.scoring.relevance import RELEVANCE_ACTION_FLOOR, score_relevance
from app.scoring.types import SignalInput
from app.scoring.validation import score_validation
from app.scouting_requests.connectors import _rss_enabled_for, fetch_signals
from app.scouting_requests.models import ScoutRequest
from app.signals.models import (
    NormalizedSignal,
    RawSignal,
    SignalCluster,
    SignalLocationEvidence,
)

logger = get_logger("signalnest.pipeline")

DEDUPE_THRESHOLD = 0.92

# Run outcomes reported in the ``scout_request.execute`` job's ``result_summary`` (D-25).
RUN_COMPLETED = "completed"
# The request was paused when the job reached it: no fetch, no model call, status kept.
SKIPPED_REQUEST_PAUSED = "skipped_request_paused"
# Legacy path only (``_run`` called without ``admitted_run``: the replay demo seed and
# direct test calls): the request was paused while this run was in flight; findings and
# stats are kept, the pause wins over the run's terminal status write. The durable worker
# path stops such a run instead (``app.jobs.admission``: ``stopped_request_paused``).
PAUSED_DURING_RUN = "paused_during_run"
# STATUS_CHANGED_DURING_RUN (imported from ``app.jobs.admission``): the request left
# ``running`` for a status other than ``paused`` with this run's fence unchanged.
# On the durable path the cooperative-pause outcomes ``stopped_request_paused`` and
# ``request_gone`` are defined in ``app.jobs.admission`` (D-25, PROPOSED).


def _signal_is_simulated(signal) -> bool:
    """Fixture signals carry no flag; anything not explicitly live is simulated."""
    return bool(getattr(signal, "is_simulated", True))


def _card_is_simulated(members) -> bool:
    """An opportunity is simulated if any member is: one fixture signal makes it so."""
    return any(m.is_simulated for m in members)


def refused_run_stats(*, scanned: int, bound: int, fetch_limits: list[dict] | None = None) -> dict:
    """The ``ScoutRequest.stats`` of a run refused at the in-process bound (P6-PLAT-1 B).

    The four ordinary counts are present (nothing was analysed, so they are honest
    zeros apart from ``scanned``), plus the stable reason code and the bound the run
    exceeded. When the per-source fetch-limit facts are known (``fetch_limits``), they are
    carried too, with the ``partial_fetch`` marker: the fetch limit applies first (per
    source), the bound second (per run, over the total after the takedown re-check). Every
    consulted source appears in ``fetch_limits``, clipped, failed or whole. There is no
    ``coverage`` object: it describes a completed run (P6-DATA-2), and a refused run did
    not complete. Safe aggregates only - never content.
    """
    stats = {
        "scanned": scanned,
        "noise_filtered": 0,
        "signals_analyzed": 0,
        "opportunities": 0,
        "refused": True,
        "refusal_reason": SIGNAL_VOLUME_ABOVE_BOUND,
        "bound": bound,
    }
    if fetch_limits is not None:
        stats["fetch_limits"] = fetch_limits
        stats.update(partial_fetch_facts(fetch_limits))
    return stats


def _coverage_rule_for(db: Session, request: ScoutRequest) -> tuple[CoverageRule, str | None]:
    market = request.resolved_market
    rule = None
    home = None
    if request.location_id:
        location = db.get(BusinessLocation, request.location_id)
        if location:
            if not market:
                market = f"{location.city}, {location.state_province or location.country}"
            # The location's own market, from its structured fields (P6-DATA-6).
            home = market_from_parts(
                city=location.city,
                region=location.state_province,
                country=location.country,
            )
        rule = db.scalar(
            select(GeoCoverageRule).where(GeoCoverageRule.location_id == request.location_id)
        )
    if rule:
        cov = CoverageRule(
            coverage_type=rule.coverage_type,
            center_latitude=rule.center_latitude,
            center_longitude=rule.center_longitude,
            radius_miles=rule.radius_miles,
            country=rule.country,
            state=rule.state,
            included_markets=tuple(rule.included_markets or ()),
            excluded_markets=tuple(rule.excluded_markets or ()),
            online_global=rule.online_global,
            home_market=home,
        )
    elif request.location_id or market:
        cov = CoverageRule(
            coverage_type="radius",
            included_markets=(market,) if market else (),
            home_market=home,
        )
    else:
        # No location and no market: an explicitly unscoped (global/online) request,
        # which the fixture connector serves across every market.
        cov = CoverageRule(coverage_type="online", online_global=True)
    return cov, market


#: The meter of the run the durable handler is executing (P6-LLM-4), supplied with
#: :func:`metered_run`. A context variable rather than an argument keeps the handler's
#: call ``_run(db, scout_request_id, context)`` - a seam job tests stub - unchanged.
_run_meter: ContextVar[LLMMeter | None] = ContextVar("llm_run_meter", default=None)


@contextmanager
def metered_run(meter: LLMMeter) -> Iterator[LLMMeter]:
    """Make ``meter`` the meter of the :func:`_run` called inside this block."""
    token = _run_meter.set(meter)
    try:
        yield meter
    finally:
        _run_meter.reset(token)


def _skip_paused(request: ScoutRequest) -> dict:
    """The skip result for a paused request: nothing fetched, nothing called, status kept."""
    logger.info(
        "scout.skipped",
        extra={"extra_fields": {"request": request.id, "outcome": SKIPPED_REQUEST_PAUSED}},
    )
    return {"scout_request_id": request.id, "outcome": SKIPPED_REQUEST_PAUSED}


def _run(
    db: Session,
    scout_request_id: str,
    context: ExecutionContext | None = None,
    meter: LLMMeter | None = None,
) -> dict:
    request = db.get(ScoutRequest, scout_request_id)
    if not request:
        raise ValueError(f"Scout request {scout_request_id} not found")

    # Isolation guard: when the caller declared a tenant context, the loaded request
    # must belong to exactly that org + workspace (and location, if scoped). This makes
    # cross-market blending impossible even if a wrong id is ever enqueued.
    if context is not None:
        if not scope_matches(
            context,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        ):
            raise JobContextError(
                "Job execution context does not match the scout request's tenant scope."
            )
        if context.location_id is not None and context.location_id != request.location_id:
            raise JobContextError(
                "Job execution context location does not match the scout request."
            )

    # D-25: a paused request does no new work. A run queued before the pause (or a
    # scheduled run whose tick raced it) is skipped here -- before any connector fetch
    # or model call -- with the request's status left exactly as the customer set it.
    # The skip is the job's result: recorded on the job record, nothing to retry.
    if request.status == ScoutRequestStatus.PAUSED.value:
        return _skip_paused(request)

    # D-25 cooperative pause (PROPOSED): on the durable worker path the claim is committed
    # in a transaction of its own, guarded by the job's run fence, so this run's long
    # transaction never holds the request row's update lock across a fetch or a model
    # call, and a pause (which advances the fence) is never blocked by it.
    admission = current_admission()
    if admission is not None:
        skipped = admission.claim(now=datetime.now(UTC))
        if skipped in (REQUEST_GONE, SKIPPED_OWNER_ACTIVE):
            # Gone, or another attempt validly owns this Scout (O2, PROPOSED): nothing written.
            return {"scout_request_id": request.id, "outcome": skipped}
        if skipped is not None:
            return _skip_paused(request)
        db.refresh(request, attribute_names=["status"])
    else:
        # Legacy path (no admission: the replay demo seed, direct test calls). Claim the
        # run with a compare-and-set, the same idiom as the manual-run route: ``running``
        # is written only if the row is still not paused, so a pause committed between
        # the load above and this write is not overwritten -- it wins, and the run is
        # skipped exactly as if the pause had been seen on load.
        claimed = db.execute(
            update(ScoutRequest)
            .where(
                ScoutRequest.id == request.id,
                ScoutRequest.status != ScoutRequestStatus.PAUSED.value,
            )
            .values(status=ScoutRequestStatus.RUNNING.value)
            .execution_options(synchronize_session=False)
        )
        db.refresh(request, attribute_names=["status"])
        if claimed.rowcount != 1:
            return _skip_paused(request)

    settings = get_settings()
    # P6-LLM-4: one meter per run - call cap, workspace budget, usage records and the
    # opaque tenant reference sent to the provider. The production caller is the durable
    # handler (``app.jobs.handlers.execute_scout_request``), which supplies a shared-ledger
    # meter through :func:`metered_run`; the bare ``LLMMeter(get_settings())`` fallback has
    # no session factory, so it is the same-session path even on PostgreSQL. Outside tests
    # it is reached only by the replay demo seed (``app.connectors.replay.seed_replay``),
    # which refuses anything but a local development/test SQLite database before it calls
    # this function (pinned by ``test_llm_budget_reservation`` and
    # ``test_replay_seed_guard``). A new production caller must pass a meter built with a
    # session factory.
    meter = (meter or _run_meter.get() or LLMMeter(get_settings())).bind(
        db,
        organization_id=request.organization_id,
        workspace_id=request.workspace_id,
        scout_request_id=request.id,
    )

    ctx = build_business_context(db, request.workspace_id)
    coverage, market = _coverage_rule_for(db, request)

    # Source selection records why the run holds what it holds (P6-DATA-2): live
    # feeds, the simulated sandbox/demo library, or an explicit "not covered".
    # P6-CAP-1: the request's own tenant scope decides RSS through the resolver.
    # #268: the fetch limit applies first, per source - each source reads at most
    # ``max_items`` entries (looking one past it) and reports what it delivered, clipped
    # or failed, in ``batch.reports``.
    batch = fetch_signals(
        market=market,
        keywords=request.keywords or ctx.keywords,
        source_types=request.source_types or [],
        rss_enabled=_rss_enabled_for(
            db,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        ),
        takedowns=active_takedowns(db),
    )
    # D-25: the post-fetch boundary. A pause that committed while the sources were read
    # stops the run here, before anything is written or refused (a no-op off the durable
    # path).
    admit_operation("post_fetch")
    # A takedown may have committed while the sources were read: re-check under each
    # source's lock before anything is written (P6-PRIV-4; see app.connectors.takedown).
    fixtures = without_taken_down(db, batch.signals)
    fetch = fetch_stats(batch.reports)

    scanned = len(fixtures)
    # P6-PLAT-1 (strategy B): the per-run in-process index is bounded. A run that
    # would index more signals than the bound is refused here, before any signal is
    # stored, embedded, compared or sent to a model - no partial dedupe, no chunking, no
    # truncation. The bound counts what would actually be indexed: the total over every
    # source, after the fetch limits and after the takedown re-check above.
    # The handler records the refusal as the run's outcome (status failed, stats
    # carrying the reason code; a request paused meanwhile stays paused, D-25); it is
    # per run, per workspace, so it never affects another workspace's run.
    try:
        check_inprocess_bound(scanned, settings)
    except SignalVolumeAboveBoundError as exc:
        logger.warning(
            "scout.refused",
            extra={
                "extra_fields": {
                    "request": request.id,
                    "reason": exc.reason,
                    "scanned": exc.scanned,
                    "bound": exc.bound,
                    "partial_fetch": fetch["partial_fetch"],
                }
            },
        )
        exc.fetch_limits = fetch["fetch_limits"]
        raise
    if admission is not None:
        # D-25: the run's writes begin here; a cooperative stop discards exactly them.
        admission.begin_writes(db)
    normalized: list[NormalizedSignal] = []
    # Kept (non-noise, non-duplicate) signals of this run, for the dedupe step. The
    # index is the configured vector backend (P6-PLAT-1); there is no fallback.
    kept_index = build_index()
    noise_filtered = 0

    for fx in fixtures:
        simulated = _signal_is_simulated(fx)
        provenance = dict(getattr(fx, "attribution", None) or {})
        source_key = provenance.get("source_key")
        raw = RawSignal(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            source_type=fx.source_type,
            source_url=fx.source_url,
            author=fx.author,
            published_at=datetime.now(UTC),
            language=fx.language,
            content=fx.content,
            raw_metadata={
                "topics": fx.topics,
                "market": fx.market,
                **({"provenance": provenance} if provenance else {}),
            },
            is_simulated=simulated,
            source_key=source_key,
        )
        db.add(raw)
        db.flush()

        signal_input = SignalInput(
            content=fx.content,
            source_type=fx.source_type,
            engagement=fx.engagement,
            age_days=fx.published_days_ago,
            author=fx.author,
            language=fx.language,
            duplicate_count=fx.duplicate_count,
            distinct_source_types=fx.distinct_source_types,
            has_buying_intent=fx.has_buying_intent,
            has_active_ads=fx.has_active_ads,
            news_coverage=fx.news_coverage,
            search_trend_up=fx.search_trend_up,
        )

        pre_score, is_noise, noise_reasons = evaluate_noise(signal_input)
        content_hash = hashlib.sha256(fx.content.strip().lower().encode()).hexdigest()[:64]
        embedding = embed_text(fx.content)

        # Dedupe against already-kept signals in this request.
        nearest = kept_index.search(embedding, top_k=1)
        is_duplicate = bool(nearest) and nearest[0][1] >= DEDUPE_THRESHOLD

        signal_type = None
        pain_dna = None
        sentiment = None
        audience_label = None
        # P6-LLM-5: whether this signal's classification is model output or a
        # provider failure left it unclassified (None = no model call was made).
        classification_source: str | None = None
        if not is_noise and not is_duplicate:
            signal_input.signal_type = None
            try:
                classified, _ = llm_service.run(
                    "classify_signal",
                    {"content": fx.content, "audiences": ctx.audiences or None},
                    meter=meter,
                )
                # P6-LLM-3: labels constrained to the closed vocabularies before use.
                labels = screen_classification(classified, ctx.industry)
                signal_type = labels["signal_type"]
                pain_dna = labels["pain_point_dna"]
                sentiment = labels["sentiment"]
                audience_label = labels["audience_fit_label"]
                signal_input.signal_type = signal_type
                classification_source = ExplanationSource.MODEL.value
            except LLMBudgetExceededError:
                classification_source = ExplanationSource.BUDGET_LIMITED.value
            except LLMError as exc:
                classification_source = ExplanationSource.DEGRADED.value
                logger.warning(
                    "classify_failed", extra={"extra_fields": {"error_class": type(exc).__name__}}
                )

        # Additive Phase 3B Batch 3 annotation: a deterministic, offline intelligence
        # read of this signal (facts vs inference, versioned score, structured
        # accept/reject). Computed once here so the advisory annotation below and the
        # Batch 4A persisted record are derived from the *same* candidate and can
        # never diverge. Purely advisory metadata — it does not alter the existing
        # normalization, scoring, clustering or decision outputs below.
        candidate = _analyze_candidate(fx, ctx)
        intelligence_annotation = (
            candidate.as_dict() if candidate is not None else {"error": "annotation_unavailable"}
        )

        norm = NormalizedSignal(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            raw_signal_id=raw.id,
            source_type=fx.source_type,
            source_url=fx.source_url,
            author=fx.author,
            published_at=raw.published_at,
            language=fx.language,
            excerpt=fx.content[:400],
            content_hash=content_hash,
            embedding=embedding,
            signal_type=signal_type,
            pain_point_dna=pain_dna,
            sentiment=sentiment,
            pre_analysis_score=pre_score,
            is_noise=is_noise,
            noise_reasons=noise_reasons,
            is_duplicate=is_duplicate,
            is_simulated=simulated,
            source_key=source_key,
            ingest_metadata={
                "audience_label": audience_label,
                "classification_source": classification_source,
                "market": fx.market,
                "engagement": fx.engagement,
                "intelligence": intelligence_annotation,
                **({"provenance": provenance} if provenance else {}),
            },
        )
        db.add(norm)
        db.flush()

        # Batch 4A: persist the same candidate as a first-class, scoped, immutable
        # record now that ``norm.id`` exists. Fail-open (savepoint-isolated) so a
        # persistence fault can never corrupt ingestion or the advisory annotation.
        if candidate is not None:
            _persist_intelligence_record(db, request, norm, candidate)

        # Geo evidence.
        geo = resolve_geo(coverage, fx.geo_evidence, target_market=fx.market)
        db.add(
            SignalLocationEvidence(
                workspace_id=request.workspace_id,
                normalized_signal_id=norm.id,
                resolved_market=geo.resolved_market,
                confidence=geo.confidence,
                evidence=geo.evidence,
                inside_scout_area=geo.inside_scout_area,
            )
        )

        if is_noise:
            noise_filtered += 1
            continue
        if is_duplicate:
            continue

        kept_index.add(norm.id, embedding)
        normalized.append(norm)

    # Cluster non-noise signals by pain-point DNA (simple, explainable clustering).
    opportunities = _build_opportunities(db, request, ctx, coverage, market, normalized, meter)
    meter.flush(db)

    stats = {
        "scanned": scanned,
        "noise_filtered": noise_filtered,
        "signals_analyzed": len(normalized),
        "opportunities": len(opportunities),
        "coverage": batch.coverage.as_stats(),
        **fetch,
    }
    if admission is not None:
        # D-25 commit boundary: ``completed``, the stats and ``last_run_at`` in one
        # guarded statement, only while the request is still ``running`` with this run's
        # fence. A pause that committed first makes it match nothing: ``RunStopped``, and
        # the handler discards this run's writes with its savepoint.
        admission.finish(db, stats=stats, now=datetime.now(UTC))
        db.refresh(request, attribute_names=["status", "stats", "last_run_at"])
        outcome = RUN_COMPLETED
    else:
        request.last_run_at = datetime.now(UTC)
        request.stats = stats
        db.flush()
        # Legacy path: settle the request as ``completed`` only if it is still in the
        # ``running`` state this run set. The status is re-read from the row, so a pause
        # that landed while the run was in flight wins: what the run produced (findings,
        # stats, ``last_run_at``) is kept, the request stays paused.
        db.refresh(request, attribute_names=["status"])
        if request.status == ScoutRequestStatus.RUNNING.value:
            request.status = ScoutRequestStatus.COMPLETED.value
            outcome = RUN_COMPLETED
        elif request.status == ScoutRequestStatus.PAUSED.value:
            outcome = PAUSED_DURING_RUN
        else:
            outcome = STATUS_CHANGED_DURING_RUN
        db.flush()
    logger.info(
        "scout.completed",
        extra={
            "extra_fields": {
                "request": request.id,
                "outcome": outcome,
                **stats,
                **meter.summary(),
            }
        },
    )
    return {"scout_request_id": request.id, "outcome": outcome, **stats}


def _build_opportunities(
    db, request, ctx, coverage, market, normalized, meter: LLMMeter
) -> list[Opportunity]:
    groups: dict[str, list[NormalizedSignal]] = {}
    for n in normalized:
        key = n.pain_point_dna or n.signal_type or "general"
        groups.setdefault(key, []).append(n)

    opportunities: list[Opportunity] = []
    for key, members in groups.items():
        primary = members[0]

        # Persist the (explainable) cluster and link its member signals.
        cluster = SignalCluster(
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            label=key.replace("_", " ").title(),
            centroid=_mean_embedding(members),
            size=len(members),
        )
        db.add(cluster)
        db.flush()
        for m in members:
            m.cluster_id = cluster.id

        signal_input = SignalInput(
            content=" ".join(m.excerpt for m in members)[:2000],
            source_type=primary.source_type,
            signal_type=primary.signal_type,
            pain_point_dna=primary.pain_point_dna,
            engagement=sum(int(m.ingest_metadata.get("engagement", 0) or 0) for m in members),
            age_days=0.0,
            distinct_source_types=len({m.source_type for m in members}),
            duplicate_count=len(members),
            has_buying_intent=any(m.signal_type == "buying_intent" for m in members),
        )

        relevance, rel_expl = score_relevance(signal_input, ctx)
        validation, validation_ev = score_validation(signal_input)
        opp_breakdown = score_opportunity(signal_input, relevance, validation)
        conf_breakdown = score_confidence(
            signal_input, len(validation_ev), opp_breakdown.total, relevance, validation
        )
        classification = classify_opportunity(opp_breakdown.total)
        conf_level = confidence_level(conf_breakdown.total)

        # Geo: take the strongest evidence among members.
        geo = resolve_geo(coverage, _member_geo_evidence(db, members), target_market=market)

        # Claim safety on the raw discussion text (must not fabricate product claims).
        safety = check_claim_safety(signal_input.content, ctx.industry)
        risk = RiskLevel(safety.risk_level.value)
        safe_positioning = translate_competitor_weakness(primary.pain_point_dna, ctx.industry)

        audience_label = primary.ingest_metadata.get("audience_label") or (
            ctx.audiences[0] if ctx.audiences else "your target customers"
        )
        audience_fit = bool(ctx.audiences) or audience_label is not None

        decision, rationale = decide(
            DecisionInput(
                relevance_score=relevance,
                opportunity_score=opp_breakdown.total,
                confidence_score=conf_breakdown.total,
                classification=classification,
                risk_level=risk,
                inside_scout_area=geo.inside_scout_area,
                is_noise=False,
                audience_fit=audience_fit,
            )
        )

        # Explanation — from the model only when action is plausible; the source
        # marker is persisted so degraded or rule-based copy is never mistaken for
        # model reasoning (P6-LLM-5). A member whose classification failed degrades
        # the whole opportunity: its scores, title and grouping lack that input. A
        # member left unclassified because a usage limit was reached (P6-LLM-4) lacks
        # it just the same, and is marked ``budget_limited``.
        sources = {m.ingest_metadata.get("classification_source") for m in members}
        if ExplanationSource.DEGRADED.value in sources:
            unclassified: ExplanationSource | None = ExplanationSource.DEGRADED
        elif ExplanationSource.BUDGET_LIMITED.value in sources:
            unclassified = ExplanationSource.BUDGET_LIMITED
        else:
            unclassified = None
        explanation, explanation_source, explanation_warnings = _explain(
            request,
            primary,
            geo,
            audience_label,
            safe_positioning,
            relevance,
            unclassified,
            ctx.industry,
            meter,
        )

        priority = int(round(opp_breakdown.total * (conf_breakdown.total / 100.0)))
        opp = Opportunity(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            brand_id=request.brand_id,
            scout_request_id=request.id,
            location_id=request.location_id,
            campaign_id=request.campaign_id,
            cluster_id=cluster.id,
            title=_title(primary, geo.resolved_market or market),
            classification=classification.value,
            decision=decision.value,
            opportunity_score=opp_breakdown.total,
            confidence_score=conf_breakdown.total,
            confidence_level=conf_level.value,
            priority_score=priority,
            relevance_score=relevance,
            risk_level=risk.value,
            resolved_market=geo.resolved_market or market,
            inside_scout_area=geo.inside_scout_area,
            why_it_matters=explanation.get("why_it_matters"),
            who_cares=explanation.get("who_cares"),
            observed_evidence=[_evidence(m) for m in members],
            ai_inference=explanation.get("ai_inference"),
            recommended_action=(
                explanation.get("recommended_action")
                if decision in (DecisionAction.ACT_NOW, DecisionAction.ACT_SOON)
                else rationale
            ),
            suggested_angles=explanation.get("suggested_angles", []),
            risk_note="; ".join(safety.warnings) or explanation.get("risk_note", ""),
            claims_warnings=[*safety.warnings, *explanation_warnings],
            audience_fit=audience_label,
            urgency=_urgency(decision),
            commercial_value="high" if signal_input.has_buying_intent else "medium",
            source_summary=sorted({m.source_type for m in members}),
            explanation_source=explanation_source.value,
            status=OpportunityStatus.NEW.value,
            is_simulated=_card_is_simulated(members),
        )
        db.add(opp)
        db.flush()

        # Batch 4A: link this cluster's persisted intelligence rows to the
        # opportunity, scoped to the workspace so linkage can never cross tenants.
        try:
            attach_opportunity(
                db,
                workspace_id=request.workspace_id,
                opportunity_id=opp.id,
                normalized_signal_ids=[m.id for m in members],
            )
        except Exception as exc:  # pragma: no cover - link failure must not break run
            # Class name only: a driver StatementError's str() carries the full
            # SQL and bound parameters, which redaction does not strip.
            logger.warning(
                "intelligence_record_link_failed",
                extra={
                    "extra_fields": {
                        "error_class": type(exc).__name__,
                        "opportunity": opp.id,
                    }
                },
            )

        db.add(
            OpportunityScore(
                workspace_id=request.workspace_id,
                opportunity_id=opp.id,
                kind="opportunity",
                breakdown=opp_breakdown.factors,
                total=opp_breakdown.total,
            )
        )
        db.add(
            OpportunityScore(
                workspace_id=request.workspace_id,
                opportunity_id=opp.id,
                kind="confidence",
                breakdown=conf_breakdown.factors,
                total=conf_breakdown.total,
            )
        )
        for ev in validation_ev:
            db.add(
                ValidationEvidence(
                    workspace_id=request.workspace_id,
                    opportunity_id=opp.id,
                    source_type=ev["source_type"],
                    detail=ev["detail"],
                    weight=ev["weight"],
                )
            )
        opp.__dict__["_relevance_expl"] = rel_expl  # for debugging/tests
        opportunities.append(opp)

    return opportunities


def _analyze_candidate(fx, ctx) -> OpportunityCandidate | None:
    """Deterministic, offline intelligence read of a connector signal.

    Returns the candidate so a single analysis feeds both the advisory
    ``ingest_metadata["intelligence"]`` annotation and the Batch 4A persisted
    record. Any failure is swallowed to ``None`` — an annotation (and its
    persistence) must never break ingestion.
    """
    try:
        return analyze_signal(
            AnalysisInput(
                content=fx.content,
                source_type=fx.source_type,
                market=fx.market,
                author=fx.author,
                language=fx.language,
                published_days_ago=float(fx.published_days_ago),
                engagement=int(fx.engagement),
                distinct_source_types=int(fx.distinct_source_types),
                duplicate_count=int(fx.duplicate_count),
                has_active_ads=fx.has_active_ads,
                news_coverage=fx.news_coverage,
                search_trend_up=fx.search_trend_up,
                # A live source's record must not be labelled simulated (the SPA shows
                # the intelligence record's flag as a badge); fixtures carry no flag.
                is_simulated=_signal_is_simulated(fx),
            ),
            ctx,
        )
    except Exception as exc:  # pragma: no cover - annotation must never break ingest
        logger.warning("intelligence_annotation_failed", extra={"extra_fields": {"err": str(exc)}})
        return None


def _persist_intelligence_record(db, request, norm, candidate: OpportunityCandidate) -> None:
    """Persist one candidate as a scoped, immutable record (fail-open).

    The concurrency-safe idempotent insert is savepoint-isolated inside
    ``persist_intelligence``; this wrapper additionally swallows any unexpected
    fault to a log line so persistence can never corrupt ingestion or opportunity
    creation.
    """
    try:
        persist_intelligence(
            db,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            normalized_signal_id=norm.id,
            candidate=candidate,
            location_id=request.location_id,
            enricher_name=get_enricher().name,
        )
    except Exception as exc:  # pragma: no cover - persistence must never break ingest
        # Class name only: a driver StatementError's str() carries the full SQL
        # and bound parameters, which redaction does not strip.
        logger.warning(
            "intelligence_record_persist_failed",
            extra={
                "extra_fields": {
                    "error_class": type(exc).__name__,
                    "signal": norm.id,
                }
            },
        )


def _evidence(member: NormalizedSignal) -> dict:
    """One observed-evidence entry; live entries name their source (P6-PRIV-4)."""
    entry = {
        "source_type": member.source_type,
        "source_url": member.source_url,
        "excerpt": member.excerpt,
    }
    provenance = (member.ingest_metadata or {}).get("provenance") or {}
    if member.source_key:
        entry["source_key"] = member.source_key
        entry["publisher"] = provenance.get("publisher")
        entry["delivery"] = provenance.get("delivery")
    return entry


def _mean_embedding(members) -> list[float]:
    vectors = [m.embedding for m in members if m.embedding]
    if not vectors:
        return []
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]


def _member_geo_evidence(db, members) -> list[tuple[str, str]]:
    evidence: list[tuple[str, str]] = []
    for m in members:
        rows = db.execute(
            select(SignalLocationEvidence).where(
                SignalLocationEvidence.normalized_signal_id == m.id
            )
        ).scalars()
        for row in rows:
            for note in row.evidence or []:
                if ": " in note:
                    kind, market = note.split(": ", 1)
                    evidence.append((kind, market))
    return evidence


def _placeholder(why_it_matters: str, recommended_action: str) -> dict:
    """Copy stored instead of a model explanation: it says no AI explanation is shown
    rather than imitating one, and ``ai_inference`` stays empty (P6-LLM-3/4/5)."""
    return {
        "why_it_matters": why_it_matters,
        "ai_inference": None,
        "recommended_action": recommended_action,
        "risk_note": "",
        "suggested_angles": [],
    }


#: Placeholder stored when the provider fails (P6-LLM-5). It states that no AI
#: explanation exists rather than imitating one; ``ai_inference`` stays empty. It
#: makes no claim that the scores are unaffected: an outage fails classification
#: too, and the scores, title and grouping are computed from the classification.
DEGRADED_WHY = (
    "AI explanation unavailable: the language-model provider did not return a "
    "usable response for this run."
)
#: The placeholder when a member signal could not be classified.
UNCLASSIFIED_WHY = (
    f"{DEGRADED_WHY} AI classification failed for at least one of these signals, so the "
    "scores, title and grouping shown were computed without it."
)
DEGRADED_EXPLANATION = _placeholder(
    DEGRADED_WHY,
    "Review the observed evidence before acting; no AI recommendation was generated.",
)
#: The model replied but its text failed output screening (P6-LLM-3). Reached only
#: when every member signal was classified by the model.
WITHHELD_EXPLANATION = _placeholder(
    "AI explanation withheld: the generated text did not pass safety screening, so it "
    "is not shown.",
    "Review the observed evidence before acting; no AI recommendation is shown.",
)
#: No call was made: run call cap or workspace monthly budget reached (P6-LLM-4).
BUDGET_LIMITED_WHY = (
    "AI explanation not generated: this workspace reached its AI usage limit for this "
    "run or this month."
)
#: The limit was reached before every member signal was classified.
BUDGET_UNCLASSIFIED_WHY = (
    f"{BUDGET_LIMITED_WHY} AI classification was not run for at least one of these "
    "signals, so the scores, title and grouping shown were computed without it."
)
BUDGET_LIMITED_EXPLANATION = _placeholder(
    BUDGET_LIMITED_WHY,
    "Review the observed evidence before acting; no AI recommendation was generated.",
)


def _explain(
    request,
    primary,
    geo,
    audience_label,
    safe_positioning,
    relevance,
    unclassified: ExplanationSource | None,
    industry,
    meter,
) -> tuple[dict, ExplanationSource, list[str]]:
    if unclassified is ExplanationSource.DEGRADED:
        # Never explained: the prompt would carry an empty pain point, and a
        # below-floor verdict may only reflect the missing classification.
        return (
            {
                **DEGRADED_EXPLANATION,
                "why_it_matters": UNCLASSIFIED_WHY,
                "who_cares": audience_label,
            },
            ExplanationSource.DEGRADED,
            [],
        )
    if unclassified is ExplanationSource.BUDGET_LIMITED:
        # Same reasoning; and no further call is attempted for this opportunity.
        return (
            {
                **BUDGET_LIMITED_EXPLANATION,
                "why_it_matters": BUDGET_UNCLASSIFIED_WHY,
                "who_cares": audience_label,
            },
            ExplanationSource.BUDGET_LIMITED,
            [],
        )
    if relevance < RELEVANCE_ACTION_FLOOR:
        return {
            "why_it_matters": "Below the relevance action floor; monitoring only.",
            "who_cares": audience_label,
            "ai_inference": "Not enough topical relevance to this business to act.",
            "recommended_action": "No action recommended.",
            "risk_note": "",
            "suggested_angles": [],
        }, ExplanationSource.RULE_BASED, []
    try:
        out, _ = llm_service.run(
            "explain_opportunity",
            {
                "resolved_market": geo.resolved_market,
                "audience_fit": audience_label,
                "pain_point_dna": primary.pain_point_dna,
                "safe_positioning": safe_positioning,
            },
            meter=meter,
        )
    except LLMBudgetExceededError:
        return (
            {**BUDGET_LIMITED_EXPLANATION, "who_cares": audience_label},
            ExplanationSource.BUDGET_LIMITED,
            [],
        )
    except LLMError as exc:
        logger.warning(
            "explain_degraded",
            extra={"extra_fields": {"request": request.id, "error_class": type(exc).__name__}},
        )
        return (
            {**DEGRADED_EXPLANATION, "who_cares": audience_label},
            ExplanationSource.DEGRADED,
            [],
        )
    # P6-LLM-3: model text is screened before it can be persisted or shown.
    screened = screen_explanation(out.model_dump(), industry)
    if screened.output is None:
        logger.warning(
            "explain_withheld",
            extra={"extra_fields": {"request": request.id, "reason": screened.withheld_reason}},
        )
        return (
            {**WITHHELD_EXPLANATION, "who_cares": audience_label},
            ExplanationSource.WITHHELD,
            screened.warnings,
        )
    return screened.output, ExplanationSource.MODEL, screened.warnings


def _title(primary, market) -> str:
    topic = (primary.pain_point_dna or primary.signal_type or "opportunity").replace("_", " ")
    where = f" in {market}" if market else ""
    return f"{topic.title()}{where}"


def _urgency(decision: DecisionAction) -> str:
    return {
        DecisionAction.ACT_NOW: "high",
        DecisionAction.ACT_SOON: "medium",
    }.get(decision, "low")
