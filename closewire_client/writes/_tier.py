"""The Tier-1/Tier-2 boundary, enforced on update bodies.

``update()`` on both bots and personas takes ``**fields`` and forwards them, which is right
— it makes partial updates the only kind possible, so a caller cannot blank a setting it
never mentioned. But a passthrough forwards *anything*, and both ``UpdateBotInput`` and
``UpdatePersonaInput`` declare a ``trash`` boolean. Left alone, ``update(client, id,
trash=True)`` would soft-delete a bot through the Tier-1 lane — a destructive action taken
by the package whose contract is that it holds none.

The fix belongs here rather than in each module: the hole is the passthrough pattern, and
it exists in exactly the same shape in two places, so it gets exactly one guard.

Setting ``trash`` **false** is restoring from the trash and is allowed — only the truthy
direction is refused.
"""

from __future__ import annotations

from typing import Any

__all__ = ["TIER2_FIELDS", "reject_tier2_fields"]

#: Update-body fields that are destructive regardless of which resource carries them.
TIER2_FIELDS: frozenset[str] = frozenset({"trash"})


def reject_tier2_fields(fields: dict[str, Any], *, operation: str) -> None:
    """Raise if a Tier-1 update body carries a destructive field set truthy.

    Args:
        fields: The update body about to be sent.
        operation: Caller name, for the error message.

    Raises:
        ValueError: If any :data:`TIER2_FIELDS` key is present and truthy.
    """
    offenders = sorted(k for k in fields if k in TIER2_FIELDS and fields[k])
    if offenders:
        raise ValueError(
            f"{operation}: {', '.join(offenders)} is a destructive field and this is the "
            "Tier-1 write client — soft-deleting belongs in Tier-2. "
            f"(Setting {offenders[0]}=False to restore is allowed.)"
        )
