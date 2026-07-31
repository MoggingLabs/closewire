"""Tier-2 persona deletion.

Note the difference from ``writes.personas.update(trash=True)``, which the Tier-1 guard
refuses: that is a *soft* delete and reversible, this is not. Both had to exist somewhere,
and the tier boundary is where they differ.
"""

from __future__ import annotations

from typing import Any

from ..rest import RestClient
from ._confirm import canonical_target, confirm_target, describe_intent

__all__ = ["delete", "preview_delete"]


def delete(client: RestClient, persona_id: str, *, confirm: Any = None) -> Any:
    """Delete a persona. ``DELETE /persona/{id}``

    ``confirm`` must equal ``persona_id``. A persona may be attached to bots; deleting it
    is not undone by re-creating one with the same name.
    """
    persona_id = confirm_target("delete persona", persona_id, confirm)
    return client.request("DELETE", f"/persona/{persona_id}")


def preview_delete(persona_id: str) -> str:
    persona_id = canonical_target("DELETE persona", persona_id)
    return describe_intent(
        "DELETE persona", persona_id, effect="permanent", required_confirmation=persona_id
    )
