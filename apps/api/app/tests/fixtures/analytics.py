"""The Phase 5E analytics seeded fixture (owned by lane 5E-W1; empty at W21 CF-3).

Project Phase 5E plan §5: the six-family, five-milestone seeded fixture behind exit criterion 4
("analytics endpoints return correct aggregates on seeded fixtures") is authored by lane 5E-W1.
Its registration in the freeze-owned root ``conftest.py`` is CF-3 payload (Project Phase 5
plan §6.3), so CF-3 creates this module empty for that import line to resolve. The root
``conftest.py`` star-imports it: a fixture is registered by listing its name in ``__all__``.

At CF-3 the ``app/tests/fixtures/`` directory has no ``__init__.py`` (it is imported as a
namespace package); lane 5A-W4 creates the package's ``__init__.py`` and its own modules.
"""

from __future__ import annotations

#: The fixtures this module registers through the root ``conftest.py`` (none at CF-3).
__all__: list[str] = []
