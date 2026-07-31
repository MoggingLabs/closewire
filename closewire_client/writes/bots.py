"""Tier-1 bot mutations: create, update, duplicate, save a flow, attach/detach sources.

Every function here is a normal paced write — serial lane, write budget, suppressed by
``CLOSEWIRE_DRY_RUN``. None of them publishes a bot, deletes one, or spends money; those
are Tier-2 and do not live in this package.

**How the request bodies below were resolved, and why that matters.** Each one was read out
of ``schema/openapi.json`` by following
``paths[<path>][<method>].requestBody.content["application/json"].schema`` to the
``$ref`` it names and then reading that component. An earlier pass instead *guessed* schema
names (``SaveBotToolsInput``, ``AddBotSourceInput``), found no such component, and recorded
"the spec declares no schema for this body" for three bodies that are in fact fully
declared. A name missing from ``components.schemas`` means the guess was wrong, not that the
body is free-form. Resolve the ``$ref``. What that yields:

===================================  ===================  ==================================
Route                                Request body         Fields
===================================  ===================  ==================================
``POST /bot``                        ``CreateBotInput``   name, templateId, importKdl,
                                                          folderId, category
``POST /bot/ai``                     ``AiCreateBotInput`` name, description, category,
                                                          folderId
``PUT /bot/{id}``                    ``UpdateBotInput``   see :data:`UPDATE_FIELDS`
``POST /bot/{id}/duplicate``         *(none declared)*    —
``POST /bot/{id}/save``              ``SaveBotInput``     botSteps
``POST /bot/{id}/saveTools``         ``ToolInputDto[]``   **an array**; see
                                                          :data:`TOOL_FIELDS`
``POST /bot/{id}/source/{sourceId}`` ``AttachSourceInput`` tags, channels,
                                                          personaNameOverride, enabled
``DELETE /bot/{id}/source/…``        *(none declared)*    —
===================================  ===================  ==================================

Every one of those schemas sets ``additionalProperties: false``, so an extra key is a
protocol error, not a harmless hint. That is why the functions below spell the fields out
instead of forwarding ``**kwargs``.

**On ``validate``.** The phase brief calls for a ``bots.validate`` that "returns valid", and
RESEARCH.md lists "validate flow" as an endpoint. There is no *standalone* validate route:
no path in the spec contains "valid", and the ``/bot`` family has no such operation. But
server-side flow validation does exist — it is **folded into save**. ``POST /bot/{id}/save``
answers with ``SaveBotResponse = {version, invalidPaths[], message}`` on **both** 200 and
400, and ``invalidPaths`` is the server's verdict on the graph. (An earlier revision of this
docstring claimed the spec had been searched for ``valid`` with "zero matches" across all
126 operations. That field is the match it missed — and the only one that is a declared
name; the word's only other appearances in the file are prose in ``/botMetric`` parameter
descriptions, "Valid options: …".) So:

* :func:`validate` is a **local pre-flight** — the phase-07 graph checker, no round-trip and
  no budget. It catches unknown node types, dangling edges, duplicate ids and a missing
  ``Source`` before a write is spent. It cannot vouch for server-side rules it cannot see.
* :func:`save` carries the **server's** verdict, and no longer discards it: see
  :func:`invalid_paths`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from .. import jobflow
from ..rest import RestClient
from ._tier import reject_tier2_fields

log = logging.getLogger("closewire.writes")

__all__ = [
    "create",
    "create_with_ai",
    "update",
    "duplicate",
    "save",
    "set_steps",
    "validate",
    "ValidationResult",
    "invalid_paths",
    "tool",
    "save_tools",
    "attach_source",
    "detach_source",
    "UPDATE_FIELDS",
    "TOOL_FIELDS",
]

#: Fields ``PUT /bot/{id}`` accepts, per ``UpdateBotInput`` (``additionalProperties: false``).
UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        "favorite",
        "trash",
        "locked",
        "rescheduling",
        "name",
        "folderId",
        "category",
        "followUpActive",
        "followUpSequences",
        "smartFollowUp",
        "followUpRepeat",
        "followUpVarianceMinutes",
        "followUpExtraPrompt",
    }
)

#: Keys one entry of the ``POST /bot/{id}/saveTools`` array may carry, per ``ToolInputDto``
#: (``additionalProperties: false``). Build entries with :func:`tool`.
TOOL_FIELDS: frozenset[str] = frozenset({"type", "enabled", "options"})


def create(
    client: RestClient,
    name: str,
    *,
    template_id: str | None = None,
    folder_id: str | None = None,
    category: str | None = None,
    import_kdl: str | None = None,
) -> Any:
    """Create a bot. ``POST /bot`` → ``CreateBotInput``

    ``CreateBotInput`` marks **no** field required and every field nullable, so which ones
    the server actually insists on is not knowable from the spec — and this call has not been
    exercised live (the validation account was at its plan ceiling, so no bot could be
    created). ``name`` is required *here* because a bot with no name is not something a
    caller means to make.

    Keys whose argument is ``None`` are omitted rather than sent as null, because a null
    ``templateId`` and an absent one are not reliably the same thing to this API.
    """
    body: dict[str, Any] = {"name": name}
    for key, value in (
        ("templateId", template_id),
        ("folderId", folder_id),
        ("category", category),
        ("importKdl", import_kdl),
    ):
        if value is not None:
            body[key] = value
    return client.request("POST", "/bot", json=body)


def create_with_ai(
    client: RestClient,
    description: str,
    name: str,
    *,
    category: str | None = None,
    folder_id: str | None = None,
) -> Any:
    """Create a bot from a natural-language description. ``POST /bot/ai`` → ``AiCreateBotInput``

    ``description`` **is the prompt** — the spec documents that field as "The prompt to use
    to create the new bot". There is no ``prompt`` key: ``AiCreateBotInput`` declares exactly
    ``{name, description, category, folderId}`` with ``additionalProperties: false``, so a
    body of ``{"prompt": …}`` would send a forbidden key *and* leave the prompt-carrying
    field empty. The parameters are named rather than ``**kwargs`` so that mismatch cannot
    recur silently.

    ``name`` is **required**, and positional so it cannot be forgotten — see
    :data:`REQUIRED_IN_PRACTICE`. The spec marks it ``nullable: true`` and declares no
    ``required`` array at all, so this cannot be derived from the schema; it was found by a
    live probe answering ``400 … missing required properties including: 'name'``.

    ``category`` here is the *source* category (the spec's example: "GHL, WebHook, etc."),
    which is not the same sense the word carries on :func:`create`.
    """
    body: dict[str, Any] = {"description": description, "name": name}
    for key, value in (("category", category), ("folderId", folder_id)):
        if value is not None:
            body[key] = value
    return client.request("POST", "/bot/ai", json=body)


def update(client: RestClient, bot_id: str, **fields: Any) -> Any:
    """Update a bot's settings. ``PUT /bot/{id}`` → ``UpdateBotInput``

    Only the fields passed are sent, so this is a partial update by construction — a caller
    cannot accidentally blank a setting it never mentioned. ``trash=True`` is refused — see
    :mod:`closewire_client.writes._tier`.

    Unknown keys raise rather than being forwarded: ``UpdateBotInput`` sets
    ``additionalProperties: false``, so a typo'd ``follow_up_active`` is a rejected body, and
    a rejection costs a paced write to discover. Accepted keys are :data:`UPDATE_FIELDS`.
    """
    if not fields:
        raise ValueError("update() needs at least one field to change")
    unknown = sorted(set(fields) - UPDATE_FIELDS)
    if unknown:
        raise ValueError(
            f"update(): unknown field(s) {unknown} — UpdateBotInput accepts "
            f"{sorted(UPDATE_FIELDS)}"
        )
    reject_tier2_fields(fields, operation="bots.update")
    return client.request("PUT", f"/bot/{bot_id}", json=dict(fields))


def duplicate(client: RestClient, bot_id: str) -> Any:
    """Copy a bot. ``POST /bot/{id}/duplicate``

    The spec declares no ``requestBody`` for this operation, so none is sent. The copy is a
    new bot, so expect it to count against the plan's bot allowance.
    """
    return client.request("POST", f"/bot/{bot_id}/duplicate")


def save(
    client: RestClient,
    bot_id: str,
    graph: dict[str, Any],
    *,
    validate_first: bool = True,
    require_valid: bool = False,
) -> Any:
    """Write a flow graph to a bot. ``POST /bot/{id}/save`` → ``SaveBotInput {botSteps}``

    This saves a **draft**; it does not publish. The live flow that conversations run keeps
    serving until someone publishes, which is Tier-2 and not in this package.

    Returns the decoded ``SaveBotResponse`` — ``{version, invalidPaths[], message}`` — or the
    dry-run sentinel when the write was suppressed.

    **Two validators, at different times.** ``validate_first`` runs :func:`validate` *before*
    the write and refuses to send a graph with errors, because a malformed graph otherwise
    costs a paced write to learn about; pass ``False`` to send anyway (warnings never block).
    ``invalidPaths`` in the response is the *server's* verdict, which arrives after the write
    and can see rules the local checker cannot. A non-empty ``invalidPaths`` is logged at
    WARNING and readable with :func:`invalid_paths`; ``require_valid=True`` turns it into a
    ``ValueError`` for callers that want save-or-fail.

    Raises:
        ValueError: ``validate_first`` found errors, or ``require_valid`` and the server
            returned invalid paths.
        ClosebotAPIError: Non-2xx. A 400 carries the same ``SaveBotResponse`` on
            ``.body``, so ``invalid_paths(exc.body)`` reads the verdict there too.
    """
    if validate_first:
        errors = validate(graph).errors
        if errors:
            detail = "; ".join(str(p) for p in errors[:5])
            raise ValueError(
                f"refusing to save an invalid graph ({len(errors)} error(s)): {detail}"
                + ("…" if len(errors) > 5 else "")
                + " — pass validate_first=False to override"
            )
    response = client.request("POST", f"/bot/{bot_id}/save", json={"botSteps": graph})

    if _is_dry_run(response):
        # Nothing was sent, so there is no server verdict to report and nothing to raise
        # about — the dry-run contract is `{"dry_run": True, "sent": False, …}` with no
        # `invalidPaths`, and it is returned untouched. The test cannot be fooled by a real
        # response either way: SaveBotResponse sets additionalProperties: false over
        # {version, invalidPaths, message}, so it can never carry a `dry_run` key.
        return response

    bad = invalid_paths(response)
    if bad:
        # Report, don't raise, by default — the write has *already happened*. The server
        # assigned a version (SaveBotResponse.version, in this same body) and stored the
        # draft, so `invalidPaths` is a verdict on a graph that was accepted, not a refusal.
        # Raising would discard that version and read at the call site as "the save did not
        # happen", inviting a retry that spends a second paced write on a draft already
        # there. The genuine refusal is a 400, and rest.py already raises on that.
        # `require_valid` is opt-in for save-or-fail callers, rather than the default, so
        # that the default keeps matching what actually occurred on the wire.
        log.warning(
            "POST /bot/%s/save returned %d invalid path(s): %s%s",
            bot_id,
            len(bad),
            ", ".join(bad[:5]),
            "…" if len(bad) > 5 else "",
        )
        if require_valid:
            raise ValueError(
                f"the server saved the draft but reported {len(bad)} invalid path(s): "
                + ", ".join(bad[:5])
                + ("…" if len(bad) > 5 else "")
                + " — the draft exists; pass require_valid=False to accept it"
            )
    return response


def set_steps(
    client: RestClient,
    bot_id: str,
    graph: dict[str, Any],
    *,
    validate_first: bool = True,
    require_valid: bool = False,
) -> Any:
    """Set a bot's flow steps — an alias for :func:`save`.

    The brief names ``bots.save`` and ``bots.set_steps`` as separate deliverables and gives
    ``set_steps`` a ``version`` argument. In the real API they are **one endpoint**: steps
    are read from ``GET /bot/{id}/steps`` (which takes a ``botVersion`` *query* parameter)
    and there is no ``PUT`` counterpart — the only versioned PUT, ``PUT
    /bot/{id}/version/{version}``, takes ``UpdateVersionInput {name, importKdl}`` and renames
    a version rather than setting its steps. ``POST /bot/{id}/save`` declares exactly
    ``SaveBotInput {botSteps}`` and has no parameter but the bot id, so there is nowhere to
    put a version: you do not send one, you receive one back in ``SaveBotResponse.version``.
    Rather than invent a second route or accept an argument that would have to be ignored,
    this is an honest alias, kept so callers who reach for the read's name find something.
    """
    return save(
        client, bot_id, graph, validate_first=validate_first, require_valid=require_valid
    )


class ValidationResult(Sequence[jobflow.Problem]):
    """The findings from :func:`validate`: a sequence of problems, split by severity.

    Iterating, ``len()`` and indexing all behave as the plain list this replaced, so existing
    ``for p in result`` / ``p.is_error`` code is unaffected.

    **``bool()`` deliberately raises.** The list this replaced mixed errors and warnings, so
    ``if validate(graph):`` was truthy for a graph with nothing wrong with it — the live
    published 26-node flow yields 0 errors and 2 warnings, and read as "invalid". There is no
    truth value that is right for both questions being asked, so neither is guessed at: ask
    :attr:`ok` ("may this be saved?") or :attr:`problems` ("was anything reported?").
    """

    __slots__ = ("_problems",)

    def __init__(self, problems: "Sequence[jobflow.Problem] | Any" = ()) -> None:
        self._problems: tuple[jobflow.Problem, ...] = tuple(problems)

    @property
    def problems(self) -> tuple[jobflow.Problem, ...]:
        """Every finding, errors and warnings alike, in the order reported."""
        return self._problems

    @property
    def errors(self) -> list[jobflow.Problem]:
        """Findings that block a save."""
        return [p for p in self._problems if p.is_error]

    @property
    def warnings(self) -> list[jobflow.Problem]:
        """Findings worth a look that never block a save."""
        return [p for p in self._problems if not p.is_error]

    @property
    def ok(self) -> bool:
        """True when the graph has **no errors**. Warnings do not make it False."""
        return not any(p.is_error for p in self._problems)

    def __len__(self) -> int:
        return len(self._problems)

    def __getitem__(self, index: Any) -> Any:
        return self._problems[index]

    def __bool__(self) -> bool:
        raise TypeError(
            "the truth value of a ValidationResult is ambiguous — it holds warnings as well "
            "as errors, and a graph with only warnings is fine to save. Use `.ok` for "
            "'valid?', `.errors` for what blocks a save, or `.problems` for everything."
        )

    def __repr__(self) -> str:
        return (
            f"ValidationResult(ok={self.ok}, errors={len(self.errors)}, "
            f"warnings={len(self.warnings)})"
        )


def validate(graph: dict[str, Any]) -> ValidationResult:
    """Check a flow graph **offline**. ``result.ok`` is the answer to "is this valid?".

    Takes no client and spends no budget: this is the local pre-flight, run by :func:`save`
    before it sends. The *server's* verdict on a saved graph is a different thing and comes
    back from :func:`save` — see the module docstring and :func:`invalid_paths`.
    """
    return ValidationResult(jobflow.validate_graph(graph))


def invalid_paths(response: Any) -> list[str]:
    """The server's flow-validation verdict, pulled out of a ``SaveBotResponse``.

    ``POST /bot/{id}/save`` answers with ``{version, invalidPaths[], message}`` on both 200
    and 400 — this is the only server-side validation signal the spec declares anywhere.
    Pass what :func:`save` returned, or ``ClosebotAPIError.body`` from a 400.

    Returns an empty list for anything without the key, which includes the dry-run sentinel:
    a suppressed write got no verdict, and "no verdict" must not read as "valid".
    """
    if not isinstance(response, Mapping):
        return []
    return [str(p) for p in (response.get("invalidPaths") or [])]


def tool(tool_type: str, *, enabled: bool = True, options: Any = None) -> dict[str, Any]:
    """Build one ``ToolInputDto`` entry for :func:`save_tools`.

    ``enabled`` is declared a plain ``boolean`` carrying no ``nullable`` marker — unlike
    ``type``, which is nullable — so it is always emitted rather than left to whatever an
    absent boolean means server-side. ``options`` is declared with no type at all — free-form
    JSON, shaped per tool — and is omitted when ``None``. The spec marks nothing required,
    but a tool entry with no ``type`` cannot mean anything, so it is positional here;
    :func:`save_tools` does not invent that rule for hand-built dicts.
    """
    entry: dict[str, Any] = {"type": tool_type, "enabled": enabled}
    if options is not None:
        entry["options"] = options
    return entry


def save_tools(client: RestClient, bot_id: str, tools: Sequence[Mapping[str, Any]]) -> Any:
    """Save a bot's tool configuration. ``POST /bot/{id}/saveTools``

    **The body is a JSON array**, not an object: the spec declares it inline as
    ``{"type": "array", "items": {"$ref": "ToolInputDto"}}`` — which is why no
    ``…Input`` component name exists for it — and answers with an array of ``BotToolDto``.
    So ``tools`` is a *list* of entries, each ``{type, enabled, options}``
    (``additionalProperties: false``); :func:`tool` builds one. Passing ``[]`` saves an empty
    tool set.

    A mapping is rejected outright rather than iterated: ``{"tools": [...]}`` would otherwise
    serialise to a list of its *keys* and quietly send ``["tools"]``.

    Raises:
        TypeError: ``tools`` is not a list/tuple of mappings.
        ValueError: An entry carries a key ``ToolInputDto`` does not declare.
    """
    if isinstance(tools, (str, bytes, Mapping)) or not isinstance(tools, (list, tuple)):
        raise TypeError(
            "save_tools(): the saveTools body is a JSON array of ToolInputDto, so `tools` "
            f"must be a list of {{type, enabled, options}} entries, not "
            f"{type(tools).__name__}. Empty is fine: save_tools(client, bot_id, []). "
            "Build entries with bots.tool('SomeToolClass', enabled=True)."
        )
    body: list[dict[str, Any]] = []
    for position, entry in enumerate(tools):
        if not isinstance(entry, Mapping):
            raise TypeError(
                f"save_tools(): tools[{position}] is {type(entry).__name__}, expected a "
                "ToolInputDto mapping"
            )
        unknown = sorted(set(entry) - TOOL_FIELDS)
        if unknown:
            raise ValueError(
                f"save_tools(): tools[{position}] has unknown key(s) {unknown} — "
                f"ToolInputDto accepts {sorted(TOOL_FIELDS)}"
            )
        body.append(dict(entry))
    return client.request("POST", f"/bot/{bot_id}/saveTools", json=body)


def attach_source(
    client: RestClient,
    bot_id: str,
    source_id: str,
    channels: list[str],
    *,
    tags: list[dict[str, Any]] | None = None,
    persona_name_override: str | None = None,
    enabled: bool | None = None,
) -> Any:
    """Attach a knowledge source to a bot. ``POST /bot/{id}/source/{sourceId}``

    The body is ``AttachSourceInput {tags, channels, personaNameOverride, enabled}`` with
    ``additionalProperties: false``. ``tags`` holds ``ContactTag`` objects —
    ``{name, approveDeny, id}``.

    ``channels`` is **required and positional**, and an empty list is valid
    (``channels=[]`` attaches the source with no channel restriction — verified live). The
    spec marks every field ``nullable: true`` and declares no ``required`` array, so this is
    not derivable from the schema; it was found by a live probe. See
    :data:`closewire_client.writes._required.REQUIRED_IN_PRACTICE`.

    **A body is always sent, even when it would be empty.** The previous version sent
    ``json=None`` when no optional field was given, which emits no ``Content-Type`` and gets
    ``415 Unsupported Media Type`` — a status that reads like a client/server format
    disagreement rather than a missing field, and cost a paced write to discover. Sending
    ``{}`` instead gets the far more useful ``400 … missing required properties including:
    'channels'``.
    """
    body: dict[str, Any] = {"channels": list(channels)}
    for key, value in (
        ("tags", tags),
        ("personaNameOverride", persona_name_override),
        ("enabled", enabled),
    ):
        if value is not None:
            body[key] = value
    return client.request("POST", f"/bot/{bot_id}/source/{source_id}", json=body)


def detach_source(client: RestClient, bot_id: str, source_id: str) -> Any:
    """Detach a knowledge source from a bot. ``DELETE /bot/{id}/source/{sourceId}``

    Treated as Tier-1 because of what the route addresses: it is the bot↔source *link*, the
    same path :func:`attach_source` creates, and deleting a source itself is a different
    operation on a different route (``DELETE /agency/source/{id}``). The spec declares no
    body and no response content for it.
    """
    return client.request("DELETE", f"/bot/{bot_id}/source/{source_id}")


def _is_dry_run(response: Any) -> bool:
    """Whether ``response`` is the sentinel a suppressed write returns."""
    return (
        isinstance(response, Mapping)
        and response.get("dry_run") is True
        and response.get("sent") is False
    )
