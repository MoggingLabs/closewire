"""Tier-0 reads for bots — the curated surface over generated :mod:`.bot`.

Hand-written; ``scripts/codegen.py`` will not overwrite it.

**Read-only.** No POST/PUT/PATCH/DELETE appears here; publish, save, and delete are
Tier-1/2 and land in phases 07-08. Every call routes through
:class:`~closewire_client.rest.RestClient` and therefore the Pacer's read lane.

Empirical notes (see ``docs/validation/05-read-client.md``):

* ``GET /bot`` is **not** paginated — it returns a bare list.
* ``GET /bot/{id}/steps`` **requires** ``botVersion`` despite the spec marking it
  optional; omitting it is a 400. :func:`get_steps` therefore takes it positionally.
* ``GET /bot/nodeDescriptors`` returns a JSON *string*; :func:`node_descriptors` decodes it.

A bot row embeds ``sources: [{id, name, key, …}]`` where ``key`` is **the same
GoHighLevel OAuth credential** a source read returns — confirmed byte-identical by
fingerprint for three client sub-accounts. Nothing in this module handles that: redaction
is enforced for every response at the transport boundary (see
:mod:`closewire_client.redaction`), precisely so a module cannot leak by omission.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from closewire_client.endpoints import bot as _gen
from closewire_client.endpoints._reads import as_list, decode_maybe_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.rest import RestClient

__all__ = [
    "list_bots",
    "get",
    "get_steps",
    "node_descriptors",
    "templates",
    "versions_of",
    "latest_version",
    "published_version",
    "published_versions",
    "descriptors_by_class",
    "describe_flow",
]


def list_bots(client: "RestClient") -> list[dict[str, Any]]:
    """Every bot on the account.

    Returns a list of bot dicts; each carries ``id``, ``name``, ``versions``,
    ``sources``, ``personaIds``, ``category``, ``locked``, ``folderId``. Credentials
    inside the embedded ``sources`` are masked by the transport.
    """
    return as_list(_gen.get_bot(client))


def get(client: "RestClient", bot_id: str) -> dict[str, Any]:
    """One bot by id. Same shape as a row of :func:`list_bots`."""
    return _gen.get_bot_id(client, bot_id)


def versions_of(bot: dict[str, Any]) -> list[dict[str, Any]]:
    """Version rows from a bot dict — ``{version, name, published, modifiedAt, modifiedBy}``."""
    versions = bot.get("versions")
    return versions if isinstance(versions, list) else []


def published_versions(bot: dict[str, Any]) -> list[str]:
    """Version strings the account has actually published, in list order."""
    return [
        str(v.get("version"))
        for v in versions_of(bot)
        if v.get("published") and v.get("version") is not None
    ]


def published_version(bot: dict[str, Any]) -> str | None:
    """The last **published** version, or ``None`` if the bot has never been published.

    Prefer this over :func:`latest_version` when comparing against the UI or against
    runtime behaviour: the UI shows the published flow, while the newest version is
    frequently an unpublished draft.
    """
    published = published_versions(bot)
    return published[-1] if published else None


def latest_version(bot: dict[str, Any]) -> str | None:
    """The last version string on a bot, or ``None`` when it has none.

    .. note::
       "Latest" is **not** "live". This returns the newest version in API list order,
       which is frequently an **unpublished draft** — on a live account the newest version
       of a published bot was a draft carrying a top-level key the published versions did
       not have. Use :func:`published_version` when you mean what the UI shows. Ordering is
       the API's list order; no version sort is applied and none is documented.
    """
    versions = versions_of(bot)
    if not versions:
        return None
    return str(versions[-1].get("version")) if versions[-1].get("version") is not None else None


def get_steps(client: "RestClient", bot_id: str, bot_version: str) -> Any:
    """The Job-Flow graph for one bot version.

    Args:
        bot_id: The bot's id.
        bot_version: **Required.** The API rejects the call with HTTP 400
            ("The botVersion field is required") when it is absent, even though the
            OpenAPI spec marks it optional. Use :func:`latest_version` to pick one.

    Returns:
        The flow graph — ``{"nodes": [...], ...}`` where each node carries ``id``,
        ``type``, ``position``, and a ``data`` blob interpreted via
        :func:`node_descriptors`.
    """
    return decode_maybe_json(_gen.get_bot_id_steps(client, bot_id, botVersion=str(bot_version)))


def node_descriptors(client: "RestClient") -> dict[str, Any]:
    """The Job-Flow node catalogue used to interpret a bot's ``steps``.

    Arrives as a JSON string and is decoded here. Keys observed live:
    ``dataTypes``, ``atomicNodes``, ``groups``, ``tools``, ``learnResources``.
    """
    # Fetched with redaction off, explicitly and at exactly one call site. The catalogue
    # uses `key` for node property names (`EnableGhlBooking`, `UseAI`, …), so scrubbing it
    # would corrupt what phase 07 validates job flows against. It is a static schema and
    # contains no account data — verified live: 17 `key` fields, all property names, none
    # token-shaped. This replaces a path-matching exemption that was bypassable.
    raw = client.request("GET", "/bot/nodeDescriptors", static_schema=True)
    decoded = decode_maybe_json(raw)
    return decoded if isinstance(decoded, dict) else {}


def templates(client: "RestClient") -> list[Any]:
    """Builder template names available when creating a bot.

    Returns a bare list of **strings**, not dicts — verified live. Annotated ``Any`` so a
    caller is not misled into ``row.get(...)``, which raises on a string.
    """
    return as_list(_gen.get_bot_bbb_templates(client))


def descriptors_by_class(catalogue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index :func:`node_descriptors` by node class name.

    The catalogue identifies node types by **``className``** — not ``type``, ``name``, or
    ``id``. A flow node's ``type`` matches that ``className``, so this is the join that
    lets you interpret a ``steps`` graph:

    >>> catalogue = descriptors_by_class(node_descriptors(client))
    >>> catalogue[node["type"]]["displayName"]
    """
    index: dict[str, dict[str, Any]] = {}
    # Both sections are className-keyed. Indexing only `atomicNodes` missed the 5 `tools`
    # entries (SmartFAQ, Email, SummarizeConversation, …), so a bot that enables a global
    # tool produced a node type the index could not resolve.
    for section in ("atomicNodes", "tools"):
        entries = catalogue.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("className"):
                index.setdefault(entry["className"], entry)
    return index


def describe_flow(catalogue: dict[str, Any], graph: Any) -> list[dict[str, Any]]:
    """Human-readable summary of a ``steps`` graph, one row per node.

    Returns ``[{id, type, displayName, group, known, next}]``. ``known`` is False for a
    node type absent from the catalogue — worth surfacing rather than silently rendering
    the raw class name. ``next`` is a list of ``{target, sourceHandle, targetHandle}``
    taken from the graph's ``edges`` — not a bare id list: a branch node's two targets are
    distinguishable only by ``sourceHandle``, and handle order differs between nodes, so
    flattening loses exactly the information that makes flow order answerable. Without it
    the rows carry only *list* order, while the phase-05 UI check asks for "same node
    types, **same order**".
    """
    index = descriptors_by_class(catalogue)
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    edges = graph.get("edges") if isinstance(graph, dict) else None

    outgoing: dict[str, list[str]] = {}
    for edge in edges or []:
        if isinstance(edge, dict) and edge.get("source") is not None:
            # Keep the handles: a branch node's two targets are only distinguishable by
            # `sourceHandle`, so flattening to a bare id list makes flow order ambiguous
            # at exactly the nodes where order matters most.
            outgoing.setdefault(str(edge["source"]), []).append(
                {
                    "target": str(edge.get("target")),
                    "sourceHandle": edge.get("sourceHandle"),
                    "targetHandle": edge.get("targetHandle"),
                }
            )

    rows = []
    for node in nodes or []:
        kind = node.get("type")
        entry = index.get(kind, {})
        rows.append(
            {
                "id": node.get("id"),
                "type": kind,
                "displayName": entry.get("displayName"),
                "group": entry.get("group"),
                "known": kind in index,
                "next": outgoing.get(str(node.get("id")), []),
            }
        )
    return rows
