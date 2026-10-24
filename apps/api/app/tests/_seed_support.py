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
