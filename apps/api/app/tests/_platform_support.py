"""Test support for the P6 platform-service rules (P6-PLAT-1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

from app.core import config


def production_vector_kwargs() -> dict[str, object]:
    """The production-valid vector configuration of this build (P6-PLAT-1, strategy B).

    Production accepts the in-process per-run similarity only as the explicitly
    acknowledged bounded backend (operator decision 2026-10-27): the acknowledgement
    plus a bound within ``config.VECTOR_INPROCESS_HARD_CEILING``. Tests of *other*
    production rules build their production Settings with these values - a real
    configuration validated by the real validator, with no patched build fact.
    """
    return {
        "vector_backend": "bruteforce",
        "vector_inprocess_production_approved": True,
        "vector_inprocess_max_signals_per_run": config.VECTOR_INPROCESS_HARD_CEILING,
    }


@contextmanager
def pgvector_build() -> Iterator[None]:
    """Validate Settings as a build that implements pgvector would (P6-PLAT-1).

    This build does not (``config.PGVECTOR_IMPLEMENTED`` is False), so ``pgvector`` is
    refused in every environment. Only the test that pins the build fact as the sole
    gate on production pgvector uses this; a valid production configuration never
    needs it (see :func:`production_vector_kwargs`).
    """
    with mock.patch.object(config, "PGVECTOR_IMPLEMENTED", True):
        yield
