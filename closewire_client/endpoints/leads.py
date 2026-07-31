"""Tier-0 reads for leads and their conversations.

Hand-written; ``scripts/codegen.py`` will not overwrite it. **Read-only** — the
generated module also exposes send-message, AI-toggle writes, and delete; none of them
appear here.

.. important::
   :func:`search` is a **POST that only reads**. It is passed ``write=False`` so it
   takes the Pacer's read lane and — critically — is **not** suppressed by
   ``CLOSEWIRE_DRY_RUN``. Calling the generated ``lead.post_lead_search`` directly
   instead would return a fabricated ``{"dry_run": true}`` under dry-run rather than
   lead data. Do not wrap the generated function; use this one.

Empirical notes (see ``docs/validation/05-read-client.md``): the list is paginated as
``{total, results, page, pageSize}`` with a 0-indexed ``page``; a lead row carries
``id``, ``name``, ``contactId``, ``lastMessage*``, and a nested ``source: {id, name}``
whose key really is ``id`` (unlike a top-level source row, which uses ``sourceId``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from closewire_client.endpoints import lead as _gen
from closewire_client.endpoints._reads import (
    PAGE_SIZE,
    as_list,
    page_of,
    paginate,
    total_or_len,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.rest import RestClient

__all__ = ["list_leads", "iter_leads", "search", "get", "history", "get_ai_toggle"]


def list_leads(
    client: "RestClient",
    *,
    page: int = 0,
    page_size: int = PAGE_SIZE,
    source_id: str | None = None,
) -> dict[str, Any]:
    """One page of leads, newest activity first.

    Args:
        page: 0-indexed page number.
        page_size: Rows per page.
        source_id: Optional filter to one connected sub-account.

    Returns:
        ``{"total": int, "results": [...], "page": int, "pageSize": int}``.
        :func:`search` returns the same ``total``/``results`` pair but pages by
        ``offset``/``count`` rather than ``page``/``pageSize``, because that is what the
        endpoint itself takes — so the row data is uniform, the cursor keys are not.
    """
    payload = _gen.get_lead(client, page=page, pageSize=page_size, sourceId=source_id)
    rows = page_of(payload)
    return {
        "total": total_or_len(payload, rows),
        "results": rows,
        "page": page,
        "pageSize": page_size,
    }


def iter_leads(
    client: "RestClient", *, source_id: str | None = None, max_pages: int = 100
) -> list[dict[str, Any]]:
    """Every lead across pages. Each page is a separate paced call — this is slow by design."""

    def fetch(*, page: int, pageSize: int) -> Any:
        return _gen.get_lead(client, page=page, pageSize=pageSize, sourceId=source_id)

    return list(paginate(fetch, max_pages=max_pages))


#: The API rejects a search body that omits these, even though the OpenAPI schema marks
#: every property nullable and lists no `required` array. Empty lists mean "no filter".
_SEARCH_REQUIRED_ARRAYS = ("sourceIds", "channels", "botIds", "personaIds")


def search(
    client: "RestClient",
    body: dict[str, Any] | None = None,
    *,
    search: str | None = None,
    offset: int = 0,
    count: int = PAGE_SIZE,
    source_ids: list[str] | None = None,
    channels: list[str] | None = None,
    bot_ids: list[str] | None = None,
    persona_ids: list[str] | None = None,
    total_only: bool = False,
    **extra: Any,
) -> Any:
    """Search leads. A POST, but semantically a **read**.

    Routed with ``write=False`` so it takes the read lane, is charged to the op budget
    rather than the write budget, and returns real data under ``CLOSEWIRE_DRY_RUN``.

    The four filter arrays (``sourceIds``, ``channels``, ``botIds``, ``personaIds``) are
    **mandatory in practice** — omitting any of them is an HTTP 400 complaining about
    "missing required properties", even though the OpenAPI schema marks every field
    nullable and declares no ``required`` list. They are defaulted to ``[]`` (no filter)
    so a plain ``search(client)`` works.

    Args:
        body: A complete request body. When given, only the mandatory arrays are
            back-filled; every other keyword is ignored.
        search: Free-text query.
        offset / count: Paging window (this endpoint uses offset/count, not page/pageSize).
        source_ids / channels / bot_ids / persona_ids: Filters; empty means unfiltered.
        total_only: Ask for just the count.
        extra: Any further documented field (``minimumResponses``,
            ``lastMessageDirection``, ``followUpScheduled``).
    """
    if body is None:
        body = {
            "totalOnly": total_only,
            "offset": offset,
            "count": count,
            "search": search,
            "sourceIds": source_ids or [],
            "channels": channels or [],
            "botIds": bot_ids or [],
            "personaIds": persona_ids or [],
            **extra,
        }
    else:
        body = dict(body)
    for field in _SEARCH_REQUIRED_ARRAYS:
        body.setdefault(field, [])
    payload = client.request("POST", "/lead/search", json=body, write=False)
    rows = page_of(payload)
    # Normalized to the same envelope `list_leads` returns, so a caller (and phase 06's
    # `--json`, and phase 11's tool schema) does not have to branch on which of the two it
    # called. The endpoint's own envelope key is `leads`, and it pages by offset/count.
    return {
        "total": total_or_len(payload, rows),
        "results": rows,
        "offset": body.get("offset", 0),
        "count": body.get("count", PAGE_SIZE),
    }


def get(client: "RestClient", lead_id: str) -> dict[str, Any]:
    """One lead by id, including its nested ``source`` and ``tags``."""
    return _gen.get_lead_leadid(client, lead_id)


def history(client: "RestClient", lead_id: str) -> list[dict[str, Any]]:
    """Page-visit history for a lead. Empty for leads with no tracked browsing."""
    return as_list(_gen.get_lead_leadid_page_history(client, lead_id))


def get_ai_toggle(client: "RestClient", lead_id: str) -> dict[str, Any]:
    """Whether the AI is currently allowed to reply to this lead.

    Returns ``{"enabled": bool, "applicable": bool, "reason": str | None}``.
    """
    return _gen.get_lead_leadid_ai_toggle(client, lead_id)
