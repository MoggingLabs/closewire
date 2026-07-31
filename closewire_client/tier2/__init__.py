"""Tier-2 — publish, destroy, and spend.

The third and last tier, kept in its own package for the same reason ``writes/`` is separate
from ``endpoints/``: the tier is visible at the import line, so a reviewer never has to
recall which functions are dangerous, and phase 11 can gate MCP tools on provenance rather
than on a name that might be misleading.

Everything here is guarded by :func:`closewire_client.tier2.confirm_target` — see
:mod:`closewire_client.tier2._confirm` for why a boolean alone is not enough. Nothing in
``endpoints/`` (Tier-0 reads) or ``writes/`` (Tier-1 mutations) imports this package, so
neither can reach a Tier-2 operation however its arguments are shaped.

The five operation submodules are **imported here**, not merely named in ``__all__``. That
is what makes ``import closewire_client.tier2 as t2; t2.billing.balance(client)`` work at
all: listing a submodule in ``__all__`` only affects ``from … import *``: it does not bind
the attribute, so plain attribute access raised ``AttributeError`` until the import existed.
Provenance is the point of this package — phase 11 gates MCP tools on which package a
function came from — and a gate that cannot reach the package's members through the package
is not a gate.

**Money.** ``billing.refill`` moves real funds. Its read side and every refusal path are
exercised; the one live refill is deliberately left for the operator's sign-off and is not
performed here. See ``docs/validation/08-tier2.md``.
"""

from __future__ import annotations

from . import billing, bots, leads, personas, sources
from ._confirm import (
    ConfirmationRequired,
    canonical_target,
    confirm_target,
    describe_intent,
    require_confirm,
)

__all__ = [
    "bots",
    "personas",
    "sources",
    "leads",
    "billing",
    "canonical_target",
    "confirm_target",
    "require_confirm",
    "describe_intent",
    "ConfirmationRequired",
]
