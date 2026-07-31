"""Tier-2 source deletion.

The most consequential delete in the product: a source is the CRM connection, and removing
it takes its OAuth credential and every bot attachment with it. ``writes.bots.detach_source``
unlinks one bot from a source and is Tier-1 precisely because it leaves the source intact.
"""

from __future__ import annotations

from typing import Any

from ..rest import RestClient
from ._confirm import canonical_target, confirm_target, describe_intent

__all__ = ["delete", "preview_delete"]


def delete(client: RestClient, source_id: str, *, confirm: Any = None) -> Any:
    """Delete a source. ``DELETE /agency/source/{id}``

    ``confirm`` must equal ``source_id``. Re-creating the source afterwards means
    re-authorising the CRM connection — this is not a reversible operation in practice,
    whatever the API returns.
    """
    source_id = confirm_target("delete source", source_id, confirm)
    return client.request("DELETE", f"/agency/source/{source_id}")


def preview_delete(source_id: str) -> str:
    source_id = canonical_target("DELETE source", source_id)
    return describe_intent(
        "DELETE source",
        source_id,
        effect="removes the CRM connection, its credential, and every bot attachment",
        required_confirmation=source_id,
    )
