"""Bot Testing API — throwaway QA sessions against a bot, without real traffic.

A test session is a synthetic conversation: you create one on a bot, send it messages, read
the transcript, force a step, roll back a turn, and delete it. It is how a flow gets QA'd
without touching a real contact.

**Why this is not in ``endpoints/``, which the phase brief names.** The brief calls for
``endpoints/testing.py``. Six of these eight operations *mutate* — create, send, update,
force, rollback, delete — and ``send`` drives the bot, which spends credits. ``endpoints/``
is the package phases 05–08 built a guarantee around: its curated modules do not mutate,
which is what makes the read client's read-only property checkable and what phase 11 intends
to gate MCP tools on. Putting six mutations there would falsify that guarantee on the day it
is most load-bearing, in exchange for matching a filename.

So the tier wins over the path, exactly as it did in phase 07 when ``writes/`` was split out
in the first place. The brief's operation list is ``create_session``/``list``/``get``/``send``/``listen``/
``force``/``rollback``/``delete``; four are spelled out here as ``list_sessions``,
``get_messages``, ``force_step`` and ``delete_session``, because a module-level ``list`` and
``get`` would shadow builtins at every call site and say nothing about what they list or
get. ``listen`` is kept as an alias. So: the brief's *operations*, at the path its tier
dictates — not, as an earlier revision of this docstring claimed, the brief's exact names. Recorded as a deviation in
``docs/validation/09-runtime.md``.

**Credits and lanes.** ``send`` is the only one that spends: it makes the bot generate a
reply. The others are session bookkeeping. An earlier revision of this docstring claimed
"all of them ride the paced write lane" — **false**, and two critics measured it: the six
mutations take the write lane, while ``list_sessions`` and ``get_messages`` are ``GET``s and
take the *read* lane, which also means ``CLOSEWIRE_DRY_RUN`` does not suppress them. That is
correct behaviour for reads; it was only the description that was wrong.
"""

from __future__ import annotations

from typing import Any

from ..rest import RestClient

__all__ = [
    "create_session",
    "list_sessions",
    "sessions_of",
    "get_messages",
    "send",
    "listen",
    "force_step",
    "rollback",
    "update_session",
    "delete_session",
    "MESSAGE_FIELDS",
    "UPDATE_FIELDS",
    "ROLLBACK_FIELDS",
]

#: ``TestSessionMessageInput`` — ``additionalProperties: false``.
MESSAGE_FIELDS: frozenset[str] = frozenset({"leadId", "message"})
#: ``UpdateSessionInput`` — ``additionalProperties: false``.
UPDATE_FIELDS: frozenset[str] = frozenset({"mimicSourceId"})
#: ``BotTestingRollbackInput`` — ``additionalProperties: false``.
ROLLBACK_FIELDS: frozenset[str] = frozenset({"messageId"})


def create_session(client: RestClient, bot_id: str) -> Any:
    """Open a test session on a bot. ``POST /bot/{botId}/testSession``

    Declares no request body. Returns the session, whose lead id is the handle every other
    call in this module takes.
    """
    return client.request("POST", f"/bot/{bot_id}/testSession")


def list_sessions(client: RestClient, bot_id: str) -> Any:
    """Every test session on a bot. ``GET /bot/{botId}/testSession``

    Returns whatever the API returned, unchanged. **The shape is not stable** — see
    :func:`sessions_of`, which is what a caller should use to get rows.
    """
    return client.request("GET", f"/bot/{bot_id}/testSession")


def sessions_of(payload: Any) -> list[Any]:
    """The session rows from a :func:`list_sessions` payload, whatever shape it came in.

    **This endpoint returns two different shapes on the same account**, observed live across
    three bots on one call each:

    * a bare JSON array — ``[]`` — on one bot;
    * ``{"leads": [...], "total": N}`` on the other two, including one holding 4 sessions.

    The spec declares a single response type, so neither shape is "the" documented one and a
    caller that assumes either gets an ``AttributeError`` or a silently empty list on the
    other. Which shape you get appears to track whether the bot has ever had a session, but
    two data points do not establish that, so this normalises rather than predicts.

    Costs nothing to call — it is a pure function over an already-fetched payload.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("leads", "items", "sessions", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


def get_messages(client: RestClient, bot_id: str, lead_id: str) -> Any:
    """The transcript for one session. ``GET /bot/{botId}/testSession/messages/{leadId}``"""
    return client.request("GET", f"/bot/{bot_id}/testSession/messages/{lead_id}")


#: The brief calls the transcript read ``listen``; :func:`get_messages` says what it does.
listen = get_messages


def send(client: RestClient, bot_id: str, lead_id: str, message: str) -> Any:
    """Send a message into a test session. ``POST /bot/{botId}/testSession/message``

    **Spends a credit** — this is what makes the bot generate a reply, so it is the one
    operation here that costs money. Paced on the write lane and suppressed by
    ``CLOSEWIRE_DRY_RUN`` like any other write.

    Note the route: the lead id travels in the **body**, not the path, unlike every other
    per-session call in this module.
    """
    return client.request(
        "POST",
        f"/bot/{bot_id}/testSession/message",
        json={"leadId": lead_id, "message": message},
    )


def force_step(client: RestClient, bot_id: str, lead_id: str) -> Any:
    """Force the session to advance a step. ``POST /bot/{botId}/testSession/{leadId}/force-step``

    Declares no request body. Useful when a flow is waiting on something a test session
    cannot supply.
    """
    return client.request("POST", f"/bot/{bot_id}/testSession/{lead_id}/force-step")


def rollback(client: RestClient, bot_id: str, lead_id: str, message_id: str) -> Any:
    """Undo back to a message. ``POST /bot/{botId}/testSession/{leadId}/rollback``

    Body is ``BotTestingRollbackInput {messageId}``.
    """
    return client.request(
        "POST",
        f"/bot/{bot_id}/testSession/{lead_id}/rollback",
        json={"messageId": message_id},
    )


def update_session(client: RestClient, bot_id: str, lead_id: str, **fields: Any) -> Any:
    """Update a session. ``PUT /bot/{botId}/testSession/{leadId}``

    ``UpdateSessionInput`` accepts only ``mimicSourceId`` — which makes the session behave
    as though it arrived from that connected source. Unknown keys raise rather than being
    forwarded into an ``additionalProperties: false`` body.
    """
    if not fields:
        raise ValueError("update_session() needs at least one field to change")
    unknown = sorted(set(fields) - UPDATE_FIELDS)
    if unknown:
        raise ValueError(
            f"update_session(): unknown field(s) {unknown} — UpdateSessionInput accepts "
            f"{sorted(UPDATE_FIELDS)}"
        )
    return client.request("PUT", f"/bot/{bot_id}/testSession/{lead_id}", json=dict(fields))


def delete_session(client: RestClient, bot_id: str, lead_id: str) -> Any:
    """Delete a test session. ``DELETE /bot/{botId}/testSession/{leadId}``

    Tier-1, not Tier-2, and the distinction is real: this destroys a synthetic QA
    conversation that the caller created moments ago, not a record the account cannot
    reconstruct. ``closewire_client/tiers.py``'s destroy rule is scoped to
    ``/bot|/persona|/agency/source|/lead|/account/apiKey`` root collections for exactly that
    reason — a test session is none of them.
    """
    return client.request("DELETE", f"/bot/{bot_id}/testSession/{lead_id}")
