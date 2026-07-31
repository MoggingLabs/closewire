"""Tier-2 bot operations: publish, delete, export.

``export`` is a plain ``GET`` and destroys nothing, so it carries no confirmation gate. It
lives here because phase 08 names it a Tier-2 deliverable and because it is the natural
thing to run immediately *before* a delete — see :func:`export`.
"""

from __future__ import annotations

import logging
from typing import Any

from ..rest import RestClient
from ._confirm import canonical_target, confirm_target, describe_intent

log = logging.getLogger("closewire.tier2")

__all__ = ["publish", "delete", "export", "preview_publish", "preview_delete"]


def publish(client: RestClient, bot_id: str, *, confirm: Any = None) -> Any:
    """Make a bot's current draft the live version. ``POST /bot/{id}/publish``

    Requires ``confirm=True``. A typed token is deliberately **not** demanded here: publish
    is reversible by publishing again, it destroys nothing, and it has exactly one target —
    so a token would add ceremony without catching a mistake the boolean misses. Delete and
    refill are the opposite on all three counts, and do demand one.

    This is the operation that makes a bot start talking to real leads, so it is Tier-2
    despite being non-destructive.
    """
    bot_id = confirm_target("publish bot", bot_id, confirm, token_required=False)
    log.warning("tier2: publishing bot %s — this makes it live", bot_id)
    return client.request("POST", f"/bot/{bot_id}/publish")


def delete(client: RestClient, bot_id: str, *, confirm: Any = None) -> Any:
    """Delete a bot. ``DELETE /bot/{id}``

    Requires ``confirm`` to equal ``bot_id`` exactly. ``confirm=True`` is refused: it would
    be equally true for every bot in the account, which is precisely the mistake worth
    catching here.

    Export first if the flow matters — see :func:`export`. There is no undo.
    """
    bot_id = confirm_target("delete bot", bot_id, confirm)
    log.warning("tier2: DELETING bot %s — this cannot be undone", bot_id)
    return client.request("DELETE", f"/bot/{bot_id}")


def export(client: RestClient, bot_id: str, *, bot_version: str | None = None) -> Any:
    """Export a bot's definition. ``GET /bot/{id}/export``

    Args:
        bot_id: The bot's id — the route's required ``id`` path parameter.
        bot_version: The ``botVersion`` query parameter, ``"x.y.z"``. The spec documents it
            as *"The bot version (x.y.z). If not provided, the latest version is
            exported."* — so passing it is how you export something other than the latest,
            and omitting it is what produces the surprise below. **Spec-documented, not yet
            exercised live** from this client; the sibling ``botVersion`` on
            ``GET /bot/{id}/steps`` is also declared optional and is live-*required*, so
            treat this route's optionality as the spec's claim until an export proves it.

    Returns:
        ``ExportBotResponse`` — ``{id, kdl, version}``, all three declared ``string`` and
        ``nullable``, ``additionalProperties: false``. The route **does** declare a response
        schema (under ``application/json``, ``text/json`` and ``text/plain``); an earlier
        note in this repo claiming it declares none was wrong.

    The flow comes out as **KDL**, the same format ``CreateBotInput.importKdl`` ("The KDL
    template to import into the new bot") and ``UpdateVersionInput.importKdl`` ("The KDL
    import for the bot version") accept — so export and import are a round-trip pair.

    **With no ``bot_version``, this exports the latest version, which on a live bot is
    usually an unpublished draft rather than what is running.** Observed on the live
    account: the ``Money Flow`` bot carried **two** published versions, ``0.0.28`` *and*
    ``0.0.29`` (``docs/validation/05-read-client.md``), its latest version overall was the
    draft ``0.0.30`` (``docs/validation/06-cli-read.md``), and an unqualified export
    returned ``0.0.30``. That is the documented behaviour ("the latest version is
    exported"), not a deviation; the trap is only that the obvious reading of "export the
    bot" is "capture what is currently running".

    Note that **"the published version" is not well defined** for such a bot: ``published``
    is a per-version flag and more than one version can carry it, so ``Money Flow`` has two
    and there is no "the". ``endpoints.bots.published_versions()`` returns all of them;
    ``published_version()`` returns the last in API list order, which is the closest thing
    to "what the UI shows" — and is ``0.0.29`` here, not ``0.0.28``.

    To capture what is running, pass that version here — ``export(client, bot_id,
    bot_version=published_version(bot))`` — rather than routing around the route. Reading
    the graph via ``endpoints.bots.get_steps(client, bot_id, version)`` remains useful when
    you want the node/edge JSON instead of KDL, but it is no longer the workaround for a
    missing parameter, because the parameter is not missing.

    No confirmation: this is a read. Still worth running before :func:`delete`, since the
    export is the only thing that survives it.
    """
    bot_id = canonical_target("export bot", bot_id)
    params = {"botVersion": str(bot_version)} if bot_version is not None else None
    return client.request("GET", f"/bot/{bot_id}/export", params=params)


def preview_publish(bot_id: str) -> str:
    """What :func:`publish` would do, without doing it."""
    return describe_intent(
        "publish bot", bot_id, effect="the current draft becomes the live version"
    )


def preview_delete(bot_id: str) -> str:
    """What :func:`delete` would do, without doing it.

    The advertised token is the canonical target, so what the preview tells the operator to
    type is exactly what :func:`delete` will act on.
    """
    bot_id = canonical_target("DELETE bot", bot_id)
    return describe_intent(
        "DELETE bot",
        bot_id,
        effect="permanent; export first if the flow matters",
        required_confirmation=bot_id,
    )
