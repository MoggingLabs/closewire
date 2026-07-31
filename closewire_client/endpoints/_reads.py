"""Shared helpers for the curated Tier-0 read modules.

Hand-written — **not** generated. ``scripts/codegen.py`` leaves files without the
``@generated`` marker alone, so this survives a spec refresh.

Three things every read module needs, all learned empirically against a live account
rather than from the spec (see ``docs/validation/05-read-client.md``):

* **Pagination.** Closebot's paginated envelope is ``{total, results, page, pageSize}``
  with a **0-indexed** ``page`` and a default ``pageSize`` of 20. Some endpoints
  (``/bot``, ``/persona``) are not paginated at all and return a bare list.
* **Secret redaction is NOT here.** It is enforced for every response at the transport
  boundary — see :mod:`closewire_client.redaction`. It was tried per-module first and the
  leak simply moved to whichever module forgot.
* **JSON-in-a-string.** ``GET /bot/nodeDescriptors`` replies with a 45 KB JSON *string*
  and a non-JSON content type, so the transport hands back text.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

log = logging.getLogger("closewire.reads")

__all__ = [
    "PAGE_SIZE",
    "as_list",
    "decode_maybe_json",
    "page_of",
    "paginate",
    "total_of",
    "total_or_len",
]

#: Closebot's own default page size, confirmed against a live account.
PAGE_SIZE = 20


#: Envelope keys seen in the wild. ``results`` is the common one; ``POST /lead/search``
#: uses ``leads``. Ordered most-common first — the first list-valued key wins.
_PAGE_KEYS = ("results", "leads", "items", "data")


def page_of(payload: Any) -> list[dict[str, Any]]:
    """Return the rows from a paginated envelope, or the list itself if unpaginated.

    Prefers the first **non-empty** list-valued envelope key, falling back to the first
    list-valued one. Preferring merely the first list-valued key would return ``[]`` for
    ``{"results": [], "data": [...]}`` — an empty page that silently hides real rows.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    first_list: list[Any] | None = None
    for key in _PAGE_KEYS:
        rows = payload.get(key)
        if isinstance(rows, list):
            if rows:
                return rows
            if first_list is None:
                first_list = rows
    return first_list if first_list is not None else []


def total_of(payload: Any, rows: list[Any] | None = None) -> int | None:
    """Total row count across all pages, or **None** when the envelope omits it.

    Returning ``None`` rather than the page length matters: a caller that treats "one
    page's worth" as the grand total stops after page 0 and silently drops the rest.
    ``bool`` is rejected explicitly — ``isinstance(True, int)`` is True in Python.
    """
    if isinstance(payload, dict):
        total = payload.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            return total
    return None


def total_or_len(payload: Any, rows: list[Any] | None = None) -> int:
    """:func:`total_of` with the page length as an explicit, opt-in fallback."""
    total = total_of(payload, rows)
    if total is not None:
        return total
    return len(rows if rows is not None else page_of(payload))


def as_list(payload: Any) -> list[Any]:
    """Coerce any read response to a list (empty for ``None``)."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    return page_of(payload)


def paginate(fetch, *, page_size: int = PAGE_SIZE, max_pages: int = 100) -> Iterator[dict[str, Any]]:
    """Yield every row across pages, calling ``fetch(page=, pageSize=)`` per page.

    Pages are **0-indexed**. Stops when a page comes back short, when ``total`` is
    reached, or at ``max_pages``. Hitting the cap logs a **warning** naming how many rows
    were returned and how many the server said exist — truncation is never silent.

    An envelope without ``total`` no longer ends the sweep after page 0; the loop keeps
    going until a short page or the cap.

    Every underlying call goes through the Pacer, so a wide sweep is slow by design.
    """
    seen = 0
    total: int | None = None
    for page in range(max_pages):
        payload = fetch(page=page, pageSize=page_size)
        rows = page_of(payload)
        if not rows:
            return
        yield from rows
        seen += len(rows)
        if total is None:
            total = total_of(payload, rows)
        if len(rows) < page_size or (total is not None and seen >= total):
            return

    log.warning(
        "pagination truncated at max_pages=%s (%s rows returned%s) — raise max_pages "
        "or narrow the query; the result is INCOMPLETE",
        max_pages,
        seen,
        f" of {total} reported" if total is not None else "",
    )




def decode_maybe_json(payload: Any) -> Any:
    """Parse a JSON payload that arrived as a string.

    ``GET /bot/nodeDescriptors`` returns JSON text without a JSON content type, so the
    transport correctly hands back a ``str``. Returns the value unchanged when it is
    already decoded or is not valid JSON.
    """
    if not isinstance(payload, str):
        return payload
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload
