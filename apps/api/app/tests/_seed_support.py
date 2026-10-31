"""Test support for suites that start from the four-market demo seed."""

from __future__ import annotations

from sqlalchemy import delete

from app.jobs.models import Job, JobEvent


def clear_seeded_runs(factory) -> None:
    """Delete the demo seed's own scout runs: its durable jobs and their events.

    Since P6-UI-009 the seed records one real run per scout request. A suite that
    asserts exactly which jobs the code under test created starts from the seeded
    configuration with an empty jobs table instead. Events go first: these throwaway
    engines do not enforce the ``ON DELETE CASCADE`` foreign key.
    """
    with factory() as s:
        s.execute(delete(JobEvent))
        s.execute(delete(Job))
        s.commit()


def forget_findings(db, scout_request_id: str) -> None:
    """Remove a Scout's earlier cards and their sightings, committed, so the next run creates
    its own cards.

    W16 customer repairs (PROPOSED): a re-run of the same evidence re-uses the finding's card
    (one card per finding) and creates none. A test that asserts on what a re-run *creates*
    (its markers, its screening) clears the earlier cards first; its assertions are unchanged.
    """
    from app.opportunities.models import Opportunity, OpportunityObservation

    db.execute(
        delete(OpportunityObservation).where(
            OpportunityObservation.scout_request_id == scout_request_id
        )
    )
    db.execute(delete(Opportunity).where(Opportunity.scout_request_id == scout_request_id))
    db.commit()
