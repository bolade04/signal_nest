"""Test support for the P6 platform-service rules (P6-PLAT-1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

from app.core import config


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
