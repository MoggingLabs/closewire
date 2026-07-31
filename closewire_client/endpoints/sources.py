"""Tier-0 reads for sources (CRM sub-account connections).

Hand-written; ``scripts/codegen.py`` will not overwrite it. **Read-only.**

A *source* is a connected CRM sub-account — for GoHighLevel clients, one source per
sub-account. Its calendars, custom fields, tags, and channels are what a bot books
against, so these reads are the groundwork for phase 10's wiring.

.. warning::
   ``GET /agency/source`` returns each source's **``accessToken`` and ``key``** — the
   GoHighLevel OAuth credentials for a client's sub-account. They are not the Closebot
   API key, so :meth:`Config.scrub` does not mask them. Masking is **not** done in this
   module: it is enforced for every response at the transport boundary (see
   :mod:`closewire_client.redaction`), because doing it per-module is what let a bot read
   leak the identical credential. ``include_secrets=True`` threads through to that
   boundary for the one caller that genuinely needs a raw token (phase 10), and logs a
   warning every time.

Empirical notes (see ``docs/validation/05-read-client.md``):

* The identifier field is **``sourceId``**, not ``id``.
* The list is paginated: ``{total, results, page, pageSize}``, ``page`` 0-indexed.
* ``fields`` returns a **dict keyed by object type** (``contact``, …), not a list.
* Calendar rows can carry the sentinel ``id: "not_in_db"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from closewire_client.endpoints import source as _gen
from closewire_client.endpoints._reads import (
    PAGE_SIZE,
    as_list,
    page_of,
    paginate,
    total_or_len,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.rest import RestClient

__all__ = [
    "list_sources",
    "iter_sources",
    "get",
    "source_id_of",
    "list_calendars",
    "list_fields",
    "list_tags",
    "list_channels",
    "list_hubspot_owners",
]


def source_id_of(source: dict[str, Any]) -> str | None:
    """The id of a source row, tolerating both spellings (`sourceId` in practice)."""
    value = source.get("sourceId") or source.get("id")
    return str(value) if value else None


def list_sources(
    client: "RestClient",
    *,
    page: int = 0,
    page_size: int = PAGE_SIZE,
    query: str | None = None,
    category: str | None = None,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """One page of sources.

    Args:
        page: 0-indexed page number.
        page_size: Rows per page.
        query: Optional free-text filter.
        category: Optional CRM category filter (e.g. ``"GHL"``).
        include_secrets: When False (default) every ``accessToken``/``key`` is masked.

    Returns:
        ``{"total": int, "results": [...], "page": int, "pageSize": int}``.
    """
    payload = client.request(
        "GET",
        "/agency/source",
        params={
            k: v
            for k, v in {
                "page": page,
                "pageSize": page_size,
                "query": query,
                "category": category,
            }.items()
            if v is not None
        },
        include_secrets=include_secrets,
    )
    rows = page_of(payload)
    return {
        "total": total_or_len(payload, rows),
        "results": rows,
        "page": page,
        "pageSize": page_size,
    }


def iter_sources(
    client: "RestClient",
    *,
    include_secrets: bool = False,
    max_pages: int = 100,
    **filters: Any,
) -> list[dict[str, Any]]:
    """Every source across all pages (each page is a separate paced call).

    ``max_pages`` mirrors :func:`leads.iter_leads`; exceeding it logs a warning rather
    than truncating silently.

    .. warning::
       ``forceTokenRefresh=True`` is a valid filter that the API accepts on this GET, and
       it has a **server-side side effect**: it refreshes the client's OAuth token. It is
       reachable through ``**filters`` but no read in this module passes it.
    """

    if filters.get("forceTokenRefresh"):
        raise ValueError(
            "forceTokenRefresh has a server-side side effect (it refreshes the client's "
            "OAuth token) and must not be reached through a read helper. Phase 10 should "
            "expose it as a named function so the effect is visible at the call site."
        )

    def fetch(*, page: int, pageSize: int) -> Any:
        # Caller filters first, paging last: a `page=`/`pageSize=` slipped in through
        # **filters must not override the paginator's own cursor.
        params = {k: v for k, v in filters.items() if v is not None}
        params.update({"page": page, "pageSize": pageSize})
        return client.request(
            "GET", "/agency/source", params=params, include_secrets=include_secrets
        )

    return list(paginate(fetch, max_pages=max_pages))


def get(client: "RestClient", source_id: str, *, include_secrets: bool = False) -> dict[str, Any]:
    """One source by id.

    ``include_secrets=True`` returns the raw GoHighLevel OAuth credentials — the escape
    hatch phase 10 needs. It requires a client built with ``allow_secrets=True``; on an
    ordinary client it raises ``PermissionError`` rather than quietly masking, so the
    caller learns the capability is missing instead of silently getting a useless value.
    """
    return client.request(
        "GET", f"/agency/source/{source_id}", include_secrets=include_secrets
    )


def list_calendars(client: "RestClient", source_id: str) -> list[dict[str, Any]]:
    """Booking calendars on the connected sub-account.

    Rows are ``{name, id}``. An ``id`` of ``"not_in_db"`` means the calendar exists in
    the CRM but has not been imported into Closebot.
    """
    return as_list(_gen.get_agency_source_id_calendars(client, source_id))


def list_fields(client: "RestClient", source_id: str) -> dict[str, list[dict[str, Any]]]:
    """Custom fields, **grouped by object type** (``contact``, …).

    Each field is ``{name, fieldKey, dataType, id}``. Returns a dict, not a list —
    the one read in this module whose envelope differs from its siblings.
    """
    payload = _gen.get_agency_source_id_fields(client, source_id)
    return payload if isinstance(payload, dict) else {}


def list_tags(client: "RestClient", source_id: str) -> list[dict[str, Any]]:
    """Contact tags on the connected sub-account (``{name, id}``)."""
    return as_list(_gen.get_agency_source_id_tags(client, source_id))


def list_channels(client: "RestClient", source_id: str) -> list[dict[str, Any]]:
    """Messaging channels — ``{name, id, source}``, e.g. SMS, WhatsApp, Live_Chat, GMB."""
    return as_list(_gen.get_agency_source_id_channels(client, source_id))


def list_hubspot_owners(client: "RestClient", source_id: str) -> list[dict[str, Any]]:
    """HubSpot owners for a HubSpot-category source. Empty for GHL sources."""
    return as_list(_gen.get_agency_source_id_owners(client, source_id))
