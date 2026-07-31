"""Tier-2 lead deletion.

A lead is a real person's conversation history. Deleting one is the operation in this
package most likely to be irreversible in a way that matters to somebody outside the
account, so it is gated identically to the rest and given no bulk form on purpose.
"""

from __future__ import annotations

from typing import Any

from ..rest import RestClient
from ._confirm import canonical_target, confirm_target, describe_intent

__all__ = ["delete", "preview_delete"]


def delete(client: RestClient, lead_id: str, *, confirm: Any = None) -> Any:
    """Delete a lead. ``DELETE /lead/{leadId}``

    ``confirm`` must equal ``lead_id``. **There is deliberately no ``delete_many``**: a
    bulk form would have one confirmation token standing for many targets, which is exactly
    the property this gate exists to prevent. Callers that genuinely need to remove several
    leads must name each one.
    """
    lead_id = confirm_target("delete lead", lead_id, confirm)
    return client.request("DELETE", f"/lead/{lead_id}")


def preview_delete(lead_id: str) -> str:
    lead_id = canonical_target("DELETE lead", lead_id)
    return describe_intent(
        "DELETE lead",
        lead_id,
        effect="removes the contact and its conversation history",
        required_confirmation=lead_id,
    )
