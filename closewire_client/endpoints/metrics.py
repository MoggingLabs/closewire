"""Tier-0 reads for bot metrics — booking graph, messages, actions, summary.

Hand-written; ``scripts/codegen.py`` will not overwrite it. **Read-only.**

Empirical notes (see ``docs/validation/05-read-client.md``):

* ``bookingGraph``'s ``resolution`` accepts **``hourly`` | ``daily`` | ``monthly``**.
  The value ``"day"`` — used by RESEARCH.md and the phase-05 prompt — is rejected with
  HTTP 400. :func:`booking_graph` validates locally so the mistake fails fast and
  readably instead of costing a paced round-trip.
* ``/botMetric/logs`` returned **HTTP 504** and ``/botMetric/actionCount`` **HTTP 500**
  on a live account, at both the 30s default and a 120s timeout. Treated as
  server-side; :func:`logs` and :func:`action_count` are implemented and left in place,
  but callers should expect them to fail and are told so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from closewire_client.endpoints import bot_metric as _gen
from closewire_client.endpoints._reads import as_list

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.rest import RestClient

__all__ = [
    "RESOLUTIONS",
    "AGENCY_METRICS",
    "booking_graph",
    "messages",
    "message_count",
    "actions",
    "action_count",
    "logs",
    "summary",
    "agency_metric",
]

#: Values ``bookingGraph`` actually accepts. NOT ``"day"`` — that is a 400.
RESOLUTIONS = ("hourly", "daily", "monthly")

#: Values ``agencyMetric`` accepts for ``metric``. Undocumented in the OpenAPI spec —
#: recovered from the API's own 400 response ("Unsupported metric. Valid options: …").
AGENCY_METRICS = (
    "responses",
    "bookings",
    "activeSources",
    "contacts",
    "totalStorage",
    "revenue",
)


def booking_graph(
    client: "RestClient",
    *,
    start: str,
    end: str,
    resolution: str = "daily",
    source_id: str | None = None,
) -> list[dict[str, Any]]:
    """Bookings over time.

    Args:
        start: ISO date, e.g. ``"2026-06-25"``.
        end: ISO date.
        resolution: One of :data:`RESOLUTIONS`. Defaults to ``"daily"``.
        source_id: Optional filter to one connected sub-account.

    Raises:
        ValueError: ``resolution`` is not one Closebot accepts — caught locally so a
            typo costs nothing rather than a paced call and an opaque 400.
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {list(RESOLUTIONS)}, got {resolution!r}. "
            "(Note: 'day' is documented in the vendor toolkit but rejected by the API.)"
        )
    if start > end:
        # An inverted range returns [] rather than an error, which looks exactly like
        # "no bookings" — the failure mode that made a whole validation run vacuous.
        raise ValueError(
            f"start ({start!r}) is after end ({end!r}); the API would return an empty "
            "series that is indistinguishable from 'no bookings in range'"
        )
    return as_list(
        _gen.get_botmetric_bookinggraph(
            client, start=start, end=end, resolution=resolution, sourceId=source_id
        )
    )


def messages(client: "RestClient", **filters: Any) -> list[dict[str, Any]]:
    """Recent messages across the account.

    Rows carry ``messageId``, ``sourceId``, ``leadId``, ``botId``, ``channel``,
    ``direction`` (``to_bot`` / ``from_bot``), ``message``, ``timestamp``, ``activities``.

    .. warning::
       This is the largest PII surface in the read client: rows contain **raw message
       bodies** written by real consumers, plus responder names. There is no redaction
       helper for it and none is appropriate — the content *is* the data. Treat the return
       value as confidential: do not log it, and gate it carefully in phase 11 before it
       reaches a model context.

    Returns a bare list with **no envelope and no ``total``**, capped at 100 rows on the
    accounts seen so far — a caller cannot distinguish "100 messages exist" from "the
    server truncated". No pagination parameters are offered by the endpoint.
    """
    return as_list(_gen.get_botmetric_messages(client, **filters))


def message_count(client: "RestClient", **filters: Any) -> Any:
    """Aggregate message counts."""
    return _gen.get_botmetric_messagecount(client, **filters)


def actions(client: "RestClient", **filters: Any) -> list[dict[str, Any]]:
    """Actions the bots have taken (bookings, tags applied, handoffs).

    Known to **time out** on at least one live account — reproduced on every attempt
    across several runs, at both the 30s default and a 120s timeout. Unfiltered calls are
    the ones observed failing; narrow the window with filters if the endpoint supports it.
    """
    return as_list(_gen.get_botmetric_actions(client, **filters))


def action_count(client: "RestClient", **filters: Any) -> Any:
    """Aggregate action counts.

    Known to return **HTTP 500** on at least one live account — see the module docstring.
    """
    return _gen.get_botmetric_actioncount(client, **filters)


def logs(client: "RestClient", **filters: Any) -> list[dict[str, Any]]:
    """Bot execution logs. **At least one filter is required.**

    Args:
        **filters: ``botId``, ``messageId``, ``sourceId``, ``leadId`` or ``actionId``.

    Called with none, the API answers ``HTTP 400 {"error": "Must specify at least one filter
    (botId, messageId, sourceId, leadId, actionId)"}``. The vendored spec declares them all
    optional, so this is the *spec understates what is required* class again — the same one
    `writes/_required.py` exists for, and found the same way, by a live 400.

    **It fails either way, and the filter does not rescue it.** Unfiltered it now returns that
    400 promptly; supplying `botId` — precisely what the 400 asks for — goes back to hanging
    until the read timeout, which is the 504/timeout phase 05 recorded. Both were probed in
    phase 09 round 13. So the endpoint is unusable in both shapes, and the unfiltered call is
    preferred only because a fast, self-describing rejection beats a two-minute hang.

    The behaviour changed under us between phase 05 and now: it used to hang on the unfiltered
    call too. An earlier version of this docstring said "narrow the window with filters **if
    supported**" — they are supported, and mandatory, and insufficient.
    """
    return as_list(_gen.get_botmetric_logs(client, **filters))


def summary(client: "RestClient") -> dict[str, Any]:
    """Agency-wide summary — the numbers behind the dashboard's usage panel.

    Keys include ``currentMonthMessageCount``, ``lastMonthMessageCount``,
    ``totalStorage``, ``currentMonthSuccessfulBookings``, ``currentMonthActiveSources``,
    ``currentUsers``, ``currentMonthContacts``, ``currentMonthRespondedContacts``.
    """
    payload = _gen.get_botmetric_agencysummary(client)
    return payload if isinstance(payload, dict) else {}


def agency_metric(
    client: "RestClient", *, metric: str, resolution: str = "daily", **filters: Any
) -> Any:
    """Per-agency metric series.

    ``metric`` and ``resolution`` are **required** — calling without them is an HTTP 400
    (``"The metric field is required."`` / ``"The resolution field is required."``), even
    though the generated signature marks every parameter optional. ``metric`` must be one
    of :data:`AGENCY_METRICS`, a set the spec does not document at all. Both are validated
    locally so a wrong value costs no paced round-trip.
    """
    if metric not in AGENCY_METRICS:
        raise ValueError(f"metric must be one of {list(AGENCY_METRICS)}, got {metric!r}")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {list(RESOLUTIONS)}, got {resolution!r}")
    return _gen.get_botmetric_agencymetric(
        client, metric=metric, resolution=resolution, **filters
    )
