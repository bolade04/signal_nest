"""Test support for the P6 platform-service rules (P6-PLAT-1 / P6-PLAT-3 / P6-PLAT-11)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

from app.core import config

#: An inert signing key that satisfies the staging/production floor (>= 32 bytes,
#: >= 5 distinct characters). Test-only; it signs nothing outside a test process.
TEST_SIGNING_KEY = "unit-test-signing-key-0123456789-not-a-secret"


@contextmanager
def pgvector_build() -> Iterator[None]:
    """Validate Settings as a build that implements pgvector would (P6-PLAT-1).

    This build does not (``config.PGVECTOR_IMPLEMENTED`` is False), so production
    accepts no ``vector_backend`` value at all. Tests of *other* production rules need
    an otherwise-valid production configuration and ask for one explicitly here; the
    patch covers Settings construction inside the ``with`` block only.
    """
    with mock.patch.object(config, "PGVECTOR_IMPLEMENTED", True):
        yield
