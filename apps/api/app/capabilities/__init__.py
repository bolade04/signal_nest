"""Governed per-workspace capability foundation (Phase 4A-C).

Phase 4A-C.1 establishes only the *type-safety and persistence* foundation for
governed workspace capability overrides:

* :mod:`app.capabilities.registry` — the closed, immutable registry of the only
  capabilities that may ever be governed, each bound to its global feature flag
  and carrying frozen governance policy metadata.
* :mod:`app.capabilities.models` — the additive ``workspace_capability_overrides``
  persistence model recording an operator's *intent* to enable/disable a single
  capability for a single workspace.

This batch ships fully **dark**: it defines no resolver, no precedence execution,
no service, no route, and consumes nothing. The three global capability flags
remain ``False`` and no live feature gate is wired to any of this. The resolver,
override service, and operator API are later, separately approved 4A-C batches.
"""

from __future__ import annotations
