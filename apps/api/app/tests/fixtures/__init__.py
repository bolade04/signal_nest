"""Shared test fixtures for the Phase 5 adversarial batteries, one module per subject area.

Created by the 5A-W4 lane (``docs/project-phase-5a-recommendation-foundation.md`` §5, W4
row; W21 lane ADV5AB). Later milestones add their own modules here (5E-W1 adds
``analytics.py``). This package is ``apps/api/app/tests/fixtures/``; it is not the
repository-root ``tests/fixtures/`` that the freeze-owned governance glob matches.

* :mod:`app.tests.fixtures.llm` -- seeded mock LLM providers that record what they are sent.
* :mod:`app.tests.fixtures.briefs` -- brief-world helpers: validation evidence rows and a
  hold point that keeps a brief capture's transaction (and its opportunity lock) open.
"""
