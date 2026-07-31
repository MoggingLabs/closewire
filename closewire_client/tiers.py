"""Route tiers, enforced **over** the generated Tier-0 package.

Why this module exists
----------------------
``scripts/codegen.py`` emits one wrapper per REST operation, mechanically, from the
vendored spec. It has no notion of tier — it cannot have one, because the tier system was
designed three phases later. The result was a complete, *ungated* shadow copy of the
dangerous surface sitting inside ``endpoints/``: ``endpoints.billing.post_agency_billing_refill``
spent real money in one line, with no confirmation token, while
``closewire_client.tier2.billing.refill`` demanded one. The gate had been layered *beside*
the generated code instead of over it.

This module layers it over. It owns:

* the **policy** — which ``(verb, path)`` routes are Tier-2, derived from three rules and
  pinned as data, plus an explicit disposition for every *other* mutating route in the
  vendored spec, so a new one cannot appear unclassified;
* the **enforcement** — an import hook that rebinds every generated function bound to a
  Tier-2 route to a shim that refuses loudly and names the ``tier2`` replacement;
* the **proof** — :func:`audit`, runnable as ``python -m closewire_client.tiers``, which
  re-derives the policy from ``schema/`` and asserts the live package matches it.

Three properties are deliberate:

**Identity is the route, never the function name.** A guard keyed on
``post_agency_billing_refill`` would be silently undone the moment codegen renamed it.
:func:`route_of` recovers ``(method, path)`` from the function's own bytecode constants
(the literals it passes to :meth:`RestClient.request`), falling back to its docstring — so
the binding is to the HTTP route the function actually calls.

**Enforcement lives one level above the blast radius.** ``endpoints/*.py`` and
``endpoints/__init__.py`` are rewritten wholesale by every codegen run, so nothing put
there can be trusted to survive one. The hook is installed from ``closewire_client/__init__.py``,
which codegen never touches, and Python guarantees a parent package's ``__init__`` runs
before any submodule of it can be imported. Regenerating ``endpoints/`` therefore cannot
reopen the hole — the new modules are guarded as they load, including any *new* Tier-2
route a spec refresh introduces.

**Refusal is an exception, never a silent no-op**, and it always names the gated
replacement. A wrapper that quietly returned ``None`` would be worse than the hole: the
caller would think the refill happened.

This module does **not** import :mod:`closewire_client.tier2`; it names it in a string.
``endpoints/`` must stay incapable of reaching a Tier-2 *function* (asserted by
``scripts/verify_tier2.py`` group 8) — knowing the name of one is not the same as being
able to call it.
"""

from __future__ import annotations

import functools
import importlib
import json
import pkgutil
import re
import sys
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any, Callable, NamedTuple

from closewire_client.errors import ClosewireError

__all__ = [
    "Tier2RouteBlocked",
    "Tier2FieldBlocked",
    "Tier2Route",
    "TIER2_ROUTES",
    "NOT_GATED_MUTATIONS",
    "TRASH_ROUTES",
    "TRASH_FIELDS",
    "route_of",
    "tier2_rule",
    "guard_module",
    "install",
    "audit",
    "main",
]

Route = tuple[str, str]

_ENDPOINTS_PACKAGE = "closewire_client.endpoints"
_HTTP_VERBS = frozenset({"GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"})


# ── Policy ────────────────────────────────────────────────────────────────────
#
# Tier-2 is "publish, destroy, and spend" (see ``closewire_client/tier2/__init__.py``).
# Three rules say which routes those are. They are stated as predicates over
# ``(verb, path)`` so they keep matching after a rename, a re-tag, or a codegen rerun.

#: RULE D — collections whose records the account cannot reconstruct. Deleting one of
#: these loses a bot's flow, a persona, a CRM connection *and its OAuth credential*, a real
#: person's conversation history, or an API credential. Deleting a folder, an FAQ, a
#: notification or a library file loses content that can be recreated, so those stay
#: Tier-1 — see :data:`NOT_GATED_MUTATIONS`, where every one of them is named.
UNRECONSTRUCTABLE_ROOTS = ("/bot", "/persona", "/agency/source", "/lead", "/account/apiKey")


def tier2_rule(method: str, path: str) -> str | None:
    """Which Tier-2 rule this route trips, or ``None``.

    The single source of truth for :data:`TIER2_ROUTES`; :func:`audit` re-runs it over the
    whole vendored spec and fails if the pinned table has drifted from it.
    """
    if method != "GET" and path.startswith("/agency/billing/"):
        return "money"  # RULE M — anything that moves or arms money. Reads stay Tier-0.
    if method == "POST" and path.endswith("/publish"):
        return "publish"  # RULE P — makes a bot start talking to real leads.
    if method == "DELETE":
        for root in UNRECONSTRUCTABLE_ROOTS:
            if re.fullmatch(re.escape(root) + r"/\{[^/}]+\}", path):
                return "destroy"  # RULE D — see UNRECONSTRUCTABLE_ROOTS.
    return None


class Tier2Route(NamedTuple):
    """Policy for one Tier-2 route.

    Attributes:
        rule: Which rule caught it — ``money``, ``publish`` or ``destroy``.
        effect: What it does, in the refusal message's own words.
        replacement: The gated call to use instead, or ``None`` when no Tier-2 wrapper
            exists yet. ``None`` still refuses — fail closed. A route that moves money with
            no gate is the defect this module exists to fix, not an exception to it.
    """

    rule: str
    effect: str
    replacement: str | None


#: Every Tier-2 route in the vendored spec. Keyed by ``(METHOD, path-template)`` — the
#: durable identity. Produced by :func:`tier2_rule`, not by hand; :func:`audit` proves it.
TIER2_ROUTES: dict[Route, Tier2Route] = {
    # RULE M — money.
    ("POST", "/agency/billing/refill"): Tier2Route(
        "money",
        "spends real money from the payment method on file",
        "closewire_client.tier2.billing.refill(client, amount, confirm=amount)",
    ),
    ("POST", "/agency/billing/refill/source/{sourceId}"): Tier2Route(
        "money", "spends real money to top up a sub-account wallet", None
    ),
    ("POST", "/agency/billing/transactions/source/{sourceId}"): Tier2Route(
        "money", "writes a money movement into a sub-account's ledger", None
    ),
    ("DELETE", "/agency/billing/transactions/source/{sourceId}/{id}"): Tier2Route(
        "money", "destroys a financial record", None
    ),
    ("PUT", "/agency/billing/options"): Tier2Route(
        "money",
        "changes billing configuration — enabling autoRefill arms future spending",
        "closewire_client.tier2.billing.set_options(client, confirm='field=value,...', **fields)",
    ),
    ("PUT", "/agency/billing/re-billing"): Tier2Route(
        "money", "changes how sub-accounts are billed, for every future charge", None
    ),
    # RULE P — go live.
    ("POST", "/bot/{id}/publish"): Tier2Route(
        "publish",
        "makes the current draft live — the bot starts talking to real leads",
        "closewire_client.tier2.bots.publish(client, bot_id, confirm=True)",
    ),
    # RULE D — destroys a record the account cannot reconstruct.
    ("DELETE", "/bot/{id}"): Tier2Route(
        "destroy",
        "permanently deletes a bot and its flow",
        "closewire_client.tier2.bots.delete(client, bot_id, confirm=bot_id)",
    ),
    ("DELETE", "/persona/{id}"): Tier2Route(
        "destroy",
        "permanently deletes a persona",
        "closewire_client.tier2.personas.delete(client, persona_id, confirm=persona_id)",
    ),
    ("DELETE", "/agency/source/{id}"): Tier2Route(
        "destroy",
        "deletes a CRM connection, its OAuth credential, and every bot attachment",
        "closewire_client.tier2.sources.delete(client, source_id, confirm=source_id)",
    ),
    ("DELETE", "/lead/{leadId}"): Tier2Route(
        "destroy",
        "deletes a real person's contact record and conversation history",
        "closewire_client.tier2.leads.delete(client, lead_id, confirm=lead_id)",
    ),
    ("DELETE", "/account/apiKey/{keyId}"): Tier2Route(
        "destroy",
        "destroys an API credential — irrecoverable, and can lock this client out",
        None,
    ),
}


#: Fields that make an ordinary update destructive. Mirrors
#: ``closewire_client.writes._tier.TIER2_FIELDS``; :func:`audit` asserts they agree. Kept
#: duplicated rather than imported so ``import closewire_client`` does not drag in the
#: Tier-1 write package (which pulls ``jobflow``) on every startup.
TRASH_FIELDS = frozenset({"trash"})

#: Routes whose request body declares ``trash`` — a **soft delete** wearing an update's
#: clothes. ``UpdateBotInput`` and ``UpdatePersonaInput`` both declare it; derived from
#: ``schema/openapi.json`` and re-derived by :func:`audit`. These routes are Tier-1 and stay
#: callable; only a body with a truthy :data:`TRASH_FIELDS` key is refused, because that is
#: the exact body ``writes/_tier.py`` already refuses one lane over.
TRASH_ROUTES: frozenset[Route] = frozenset(
    {("PUT", "/bot/{id}"), ("PUT", "/persona/{id}")}
)


#: Every *other* mutating route in the vendored spec, with the reason it is not Tier-2.
#: This table is what makes the policy checkable rather than merely asserted: :func:`audit`
#: fails if a spec refresh introduces a mutating route that appears in neither table, so a
#: new ``DELETE /agency/subaccount/{id}`` cannot slip in silently classified as harmless.
NOT_GATED_MUTATIONS: dict[Route, str] = {
    # Relationship edits. Both records survive; `writes.bots.detach_source` is Tier-1 for
    # exactly this reason — unlinking a source is not deleting one.
    ("POST", "/bot/{id}/source/{sourceId}"): "attach/detach",
    ("DELETE", "/bot/{id}/source/{sourceId}"): "attach/detach",
    ("POST", "/library/files/{fileId}/source/{sourceId}"): "attach/detach",
    ("DELETE", "/library/files/{fileId}/source/{sourceId}"): "attach/detach",
    # The bot test harness. Nothing here touches a real lead or a live bot.
    ("POST", "/bot/{botId}/testSession"): "bot-testing",
    ("POST", "/bot/{botId}/testSession/message"): "bot-testing",
    ("PUT", "/bot/{botId}/testSession/{leadId}"): "bot-testing",
    ("DELETE", "/bot/{botId}/testSession/{leadId}"): "bot-testing",
    ("POST", "/bot/{botId}/testSession/{leadId}/force-step"): "bot-testing",
    ("POST", "/bot/{botId}/testSession/{leadId}/rollback"): "bot-testing",
    # Live-demo links: a sandbox surface, not account data.
    ("POST", "/bot-live-demo/{botId}"): "live-demo",
    ("PUT", "/bot-live-demo/{botId}/{key}"): "live-demo",
    ("DELETE", "/bot-live-demo/{botId}/{key}"): "live-demo",
    ("POST", "/bot-live-demo/live/{key}/session"): "live-demo",
    ("PUT", "/bot-live-demo/live/{key}/session/{sessionLeadId}"): "live-demo",
    ("POST", "/bot-live-demo/live/{key}/session/{sessionLeadId}/message"): "live-demo",
    # Creates. Nothing is destroyed and nothing goes live: a created bot is a draft until
    # `POST /bot/{id}/publish`, which is Tier-2.
    ("POST", "/account/apiKey"): "create",
    ("POST", "/agency/invite"): "create",
    ("POST", "/agency/source"): "create",
    ("POST", "/bot"): "create",
    ("POST", "/bot/ai"): "create",
    ("POST", "/bot/{id}/duplicate"): "create",
    ("POST", "/hierarchy"): "create",
    ("POST", "/library/files"): "create",
    ("POST", "/library/webscrape"): "create",
    ("POST", "/persona"): "create",
    ("POST", "/smart-faq"): "create",
    # Draft edits. `save` writes the draft graph; publishing it is the Tier-2 step.
    ("POST", "/bot/{id}/save"): "update-draft",
    ("POST", "/bot/{id}/saveTools"): "update-draft",
    ("PUT", "/bot/{id}/version/{version}"): "update-draft",
    # Field updates. `PUT /bot/{id}` and `PUT /persona/{id}` are additionally body-gated —
    # see TRASH_ROUTES.
    ("PUT", "/agency/source/{sourceId}"): "update",
    ("PUT", "/bot/{id}"): "update",
    ("PUT", "/hierarchy/{id}"): "update",
    ("PUT", "/lead/{leadId}"): "update",
    ("PUT", "/lead/{leadId}/ai-toggle"): "update",
    ("PUT", "/lead/{leadId}/instance/{botId}"): "update",
    ("PUT", "/library/files/{fileId}"): "update",
    ("PUT", "/library/files/{fileId}/scrape-pages"): "update",
    ("PUT", "/notifications/forwarding"): "update",
    ("PUT", "/notifications/{id}"): "update",
    ("PUT", "/persona/{id}"): "update",
    ("POST", "/botVariables/{botId}/{sourceId}"): "update",
    ("POST", "/botMetric/messageFeedback"): "update",
    # Deletes of content the account can recreate from what it already has.
    ("DELETE", "/agency/invite"): "delete-recreatable",
    ("DELETE", "/hierarchy/{id}"): "delete-recreatable",
    ("DELETE", "/library/files/{fileId}"): "delete-recreatable",
    ("DELETE", "/notifications/{id}"): "delete-recreatable",
    ("DELETE", "/smart-faq/{id}"): "delete-recreatable",
    # Runtime messaging — the product doing its job. The live surface has its own client
    # (`closewire_client.live`) and its own pacing lane.
    ("POST", "/lead/{leadId}/send-message"): "runtime-messaging",
    ("POST", "/lead/{leadId}/typing"): "runtime-messaging",
    ("POST", "/webhook/event/{sourceId}"): "runtime-messaging",
    ("POST", "/smart-faq/answer"): "runtime-messaging",
    ("POST", "/smart-faq/answered-followup"): "runtime-messaging",
    # A POST that is really a read; `endpoints.leads.search` runs it in the read lane.
    ("POST", "/lead/search"): "read-shaped-post",
}


# ── Refusal ───────────────────────────────────────────────────────────────────
class Tier2RouteBlocked(ClosewireError, RuntimeError):
    """A Tier-0 generated wrapper for a Tier-2 route was called.

    Subclasses :class:`~closewire_client.errors.ClosewireError` so the CLI's handler catches
    it, and ``RuntimeError`` so ordinary Python handling still applies.

    Attributes:
        route: The ``(METHOD, path)`` that was refused.
        qualname: The generated function that was called.
        replacement: The gated ``tier2`` call to use instead, when one exists.
    """

    def __init__(self, route: Route, qualname: str, policy: Tier2Route) -> None:
        self.route = route
        self.qualname = qualname
        self.replacement = policy.replacement
        method, path = route
        if policy.replacement:
            fix = (
                "Use the gated equivalent, which requires a confirmation token that "
                "matches the target:\n"
                f"    {policy.replacement}\n"
            )
        else:
            fix = (
                "No gated wrapper exists for this route yet. Add one under "
                "closewire_client/tier2/\n"
                "    (see closewire_client/tier2/_confirm.py) rather than calling this "
                "one.\n"
            )
        super().__init__(
            f"REFUSED: {qualname} is an ungated Tier-0 wrapper for a Tier-2 route.\n"
            f"  {method} {path} — {policy.effect}.\n"
            f"Nothing was sent. closewire_client.endpoints mirrors the whole API for "
            f"reads; it cannot\n"
            f"carry a confirmation, so it refuses Tier-2 routes instead of offering an "
            f"unconfirmed twin.\n"
            f"{fix}"
            f"Tier-2 calls are gated by closewire_client.tier2 and are still suppressed by "
            f"CLOSEWIRE_DRY_RUN."
        )


class Tier2FieldBlocked(ClosewireError, ValueError):
    """A Tier-1 generated wrapper was called with a destructive field in its body."""

    def __init__(self, route: Route, qualname: str, fields: list[str]) -> None:
        self.route = route
        self.qualname = qualname
        self.fields = fields
        method, path = route
        named = ", ".join(fields)
        super().__init__(
            f"REFUSED: {qualname} was called with {named}={{truthy}}.\n"
            f"  {method} {path} with {named} set is a soft delete — destructive, and "
            f"Tier-2 by policy\n"
            f"  even though the route itself is an ordinary Tier-1 update.\n"
            f"Nothing was sent. closewire_client.writes refuses the same body "
            f"(writes/_tier.py); this\n"
            f"generated wrapper refuses it too, so the Tier-1 guard cannot be walked "
            f"around by calling\n"
            f"the generated twin. To delete for real, use the gated Tier-2 call — e.g.\n"
            f"    closewire_client.tier2.bots.delete(client, bot_id, confirm=bot_id)\n"
            f"Setting {named}=False (restoring from the trash) is allowed."
        )


# ── Route extraction ──────────────────────────────────────────────────────────
_DOC_ROUTE = re.compile(r"``([A-Z]+) (/[^`\s]*)``")


def _string_consts(code: CodeType) -> set[str]:
    """Every string constant in ``code``, including nested code objects.

    Comprehensions are separate code objects before Python 3.12 and inlined after, so a
    generated function's path literal can live at either depth. Both are searched.
    """
    found: set[str] = set()
    stack = [code]
    while stack:
        current = stack.pop()
        for const in current.co_consts:
            if isinstance(const, str):
                found.add(const)
            elif isinstance(const, CodeType):
                stack.append(const)
    return found


def route_of(func: Any) -> Route | None:
    """The ``(METHOD, path-template)`` a generated wrapper calls, or ``None``.

    Read from the function's **bytecode constants** — the literals it hands to
    :meth:`RestClient.request` — so the binding is to the route itself and survives a
    rename, a re-tag, and ``python -OO`` (which strips docstrings). Requires exactly one
    verb literal and one path literal; a hand-written module that issues several requests
    is deliberately not classified here.

    Falls back to the ``METHOD /path`` marker every generated docstring carries, which is
    what keeps :func:`audit` able to classify an already-guarded shim.
    """
    code = getattr(func, "__code__", None)
    if isinstance(code, CodeType):
        consts = _string_consts(code)
        verbs = consts & _HTTP_VERBS
        paths = {c for c in consts if c.startswith("/")}
        if len(verbs) == 1 and len(paths) == 1:
            return (next(iter(verbs)), next(iter(paths)))
    doc = getattr(func, "__doc__", None)
    if isinstance(doc, str):
        match = _DOC_ROUTE.search(doc)
        if match:
            return (match.group(1), match.group(2))
    return None


# ── Guarding ──────────────────────────────────────────────────────────────────
def _blocked_wrapper(
    func: Callable[..., Any], route: Route, policy: Tier2Route
) -> Callable[..., Any]:
    """A same-signature stand-in that raises instead of calling ``func``."""
    qualname = f"{getattr(func, '__module__', '?')}.{getattr(func, '__name__', '?')}"

    @functools.wraps(func)
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise Tier2RouteBlocked(route, qualname, policy)

    refuse.__doc__ = (
        f"**BLOCKED — Tier-2 route.** {route[0]} {route[1]} {policy.effect}.\n\n"
        f"Calling this raises :class:`~closewire_client.tiers.Tier2RouteBlocked`. "
        + (f"Use ``{policy.replacement}``.\n\n" if policy.replacement else "")
        + f"Original wrapper's documentation follows.\n\n{func.__doc__ or ''}"
    )
    # `functools.wraps` sets `__wrapped__` to the original, so the ungated function was
    # still one attribute read away — two reviewers reproduced a live refill through
    # `post_agency_billing_refill.__wrapped__(...)`, and `inspect.unwrap` follows it
    # automatically. Dropped: this module's contract is that the generated package offers no
    # ungated twin, and an attribute that hands one back is exactly such a twin.
    #
    # `rest.request("POST", "/agency/billing/refill", ...)` remains reachable and always
    # was — that is the deliberate low-level escape hatch, not an accident of decoration.
    del refuse.__wrapped__
    # The synthesised docstring must carry the route in the marker format `route_of` reads,
    # or a re-scan of an already-blocked shim cannot find it. That mattered under `-OO`,
    # which strips the original docstring this used to fall back on: the audit then reported
    # 12 spurious "blocked but has no route" findings.
    refuse.__doc__ = (
        f"**BLOCKED — Tier-2 route.** ``{route[0]} {route[1]}`` {policy.effect}.\n\n"
        f"Calling this raises :class:`~closewire_client.tiers.Tier2RouteBlocked`. "
        + (f"Use ``{policy.replacement}``.\n\n" if policy.replacement else "")
        + f"Original wrapper's documentation follows.\n\n{func.__doc__ or ''}"
    )
    refuse._closewire_tier2_blocked = True  # type: ignore[attr-defined]
    refuse._closewire_route = route  # type: ignore[attr-defined]
    return refuse


def _field_guarded_wrapper(func: Callable[..., Any], route: Route) -> Callable[..., Any]:
    """Pass through, unless the body carries a truthy :data:`TRASH_FIELDS` key."""
    qualname = f"{getattr(func, '__module__', '?')}.{getattr(func, '__name__', '?')}"

    @functools.wraps(func)
    def checked(*args: Any, **kwargs: Any) -> Any:
        body = kwargs.get("body")
        if isinstance(body, dict):
            offenders = sorted(k for k in body if k in TRASH_FIELDS and body[k])
            if offenders:
                raise Tier2FieldBlocked(route, qualname, offenders)
        return func(*args, **kwargs)

    checked._closewire_field_guarded = True  # type: ignore[attr-defined]
    checked._closewire_route = route  # type: ignore[attr-defined]
    # Same reason the blocked shims drop it: `functools.wraps` sets `__wrapped__` to the
    # original, and three reviewers reached the ungated body through it —
    # `put_bot_id.__wrapped__(rest, id, body={"trash": True})` sent the soft delete this
    # wrapper exists to refuse. "No ungated twin" has to hold for the field guard too.
    del checked.__wrapped__

    return checked


def guard_module(module: ModuleType) -> list[str]:
    """Rebind every Tier-2 route in ``module`` to a refusing shim. Returns the names.

    Idempotent, and safe on the hand-written read modules: a function is touched only when
    its route is *definitively* Tier-2, so a curated read whose route cannot be extracted
    is left exactly as it was.
    """
    guarded: list[str] = []
    for name, obj in list(vars(module).items()):
        if name.startswith("__") or not callable(obj) or not hasattr(obj, "__code__"):
            continue
        if getattr(obj, "_closewire_tier2_blocked", False) or getattr(
            obj, "_closewire_field_guarded", False
        ):
            continue
        route = route_of(obj)
        if route is None:
            continue
        policy = TIER2_ROUTES.get(route)
        if policy is not None:
            setattr(module, name, _blocked_wrapper(obj, route, policy))
            guarded.append(name)
        elif route in TRASH_ROUTES:
            setattr(module, name, _field_guarded_wrapper(obj, route))
            guarded.append(name)
    return guarded


class _GuardingLoader:
    """Delegates to the real loader, then guards the module it produced."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def create_module(self, spec: Any) -> Any:
        create = getattr(self.inner, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: ModuleType) -> None:
        self.inner.exec_module(module)
        guard_module(module)

    def __getattr__(self, name: str) -> Any:  # get_source / is_package / get_filename …
        return getattr(self.inner, name)

    def __repr__(self) -> str:
        return f"<closewire tier guard over {self.inner!r}>"


class _TierGuardFinder:
    """Intercepts imports of ``closewire_client.endpoints`` and its submodules.

    A meta-path finder rather than a line in ``endpoints/__init__.py`` because that file is
    rewritten by every codegen run — anything load-bearing placed there is one
    ``python scripts/codegen.py`` away from being gone. This finder is installed from
    ``closewire_client/__init__.py``, which codegen never writes, and Python runs a parent
    package's ``__init__`` before any of its submodules can be imported. There is therefore
    no import path to a generated module that skips it.
    """

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != _ENDPOINTS_PACKAGE and not fullname.startswith(_ENDPOINTS_PACKAGE + "."):
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is not None and not isinstance(spec.loader, _GuardingLoader):
                spec.loader = _GuardingLoader(spec.loader)
            return spec
        return None


def install() -> None:
    """Install the guard. Idempotent; called from ``closewire_client/__init__.py``."""
    if not any(isinstance(f, _TierGuardFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _TierGuardFinder())
    # Anything already imported (only possible on a reload) is guarded in place.
    for name, module in list(sys.modules.items()):
        if name == _ENDPOINTS_PACKAGE or name.startswith(_ENDPOINTS_PACKAGE + "."):
            if isinstance(module, ModuleType):
                guard_module(module)


# ── Audit — the mechanical check ──────────────────────────────────────────────
def _schema_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "schema"


def _spec_routes() -> tuple[list[dict[str, Any]], dict[Route, str | None]]:
    """REST operations from the vendored index, plus each one's request-body schema."""
    index = json.loads((_schema_dir() / "endpoints.index.json").read_text(encoding="utf-8"))
    entries = [e for e in index if "base_url" not in e]  # drop the live-message op
    bodies = {(e["method"], e["path"]): e.get("request_body") for e in entries}
    return entries, bodies


def _trash_routes_from_spec(bodies: dict[Route, str | None]) -> set[Route]:
    """Routes whose request body declares a :data:`TRASH_FIELDS` property."""
    spec = json.loads((_schema_dir() / "openapi.json").read_text(encoding="utf-8"))
    schemas = spec.get("components", {}).get("schemas", {})
    trashy = {
        name
        for name, schema in schemas.items()
        if isinstance(schema, dict) and TRASH_FIELDS & set(schema.get("properties") or {})
    }
    return {route for route, body in bodies.items() if body in trashy}


def _package_functions() -> dict[str, dict[str, Any]]:
    """``{module_name: {func_name: func}}`` for every module in the endpoints package."""
    package = importlib.import_module(_ENDPOINTS_PACKAGE)
    out: dict[str, dict[str, Any]] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{_ENDPOINTS_PACKAGE}.{info.name}")
        out[info.name] = {
            name: obj
            for name, obj in vars(module).items()
            if callable(obj) and hasattr(obj, "__code__") and not name.startswith("__")
        }
    return out


def audit() -> list[str]:
    """Re-derive the policy from ``schema/`` and check the live package against it.

    Returns a list of findings; empty means the property holds. Run it as
    ``python -m closewire_client.tiers``. This is what a future codegen run has to survive:
    regenerating ``endpoints/`` with a new or renamed Tier-2 operation either stays guarded
    (the hook binds by route) or turns up here as a finding — it cannot pass quietly.
    """
    findings: list[str] = []
    entries, bodies = _spec_routes()
    spec_routes = {(e["method"], e["path"]) for e in entries}

    # 1. The pinned table is exactly what the rules produce over the vendored spec.
    derived = {r for r in spec_routes if tier2_rule(*r)}
    for route in sorted(derived - set(TIER2_ROUTES)):
        findings.append(f"rule says Tier-2 but TIER2_ROUTES omits it: {route[0]} {route[1]}")
    for route in sorted(set(TIER2_ROUTES) - derived):
        findings.append(f"TIER2_ROUTES has a route no rule produces: {route[0]} {route[1]}")

    # 2. Every mutating route in the spec has a disposition. A new one fails here rather
    #    than defaulting to "harmless".
    for route in sorted(spec_routes):
        if route[0] == "GET":
            continue
        if route not in TIER2_ROUTES and route not in NOT_GATED_MUTATIONS:
            findings.append(
                f"unclassified mutating route (decide its tier): {route[0]} {route[1]}"
            )
    for route in sorted(set(NOT_GATED_MUTATIONS) - spec_routes):
        findings.append(f"NOT_GATED_MUTATIONS names a route the spec no longer has: {route}")

    # 3. The body-level guard's route set is still what the spec says.
    from_spec = _trash_routes_from_spec(bodies)
    if from_spec != set(TRASH_ROUTES):
        findings.append(
            f"TRASH_ROUTES {sorted(TRASH_ROUTES)} != routes whose body declares "
            f"{sorted(TRASH_FIELDS)} in the spec: {sorted(from_spec)}"
        )

    # 4. Tier-1's field guard and this one agree on what is destructive.
    try:
        from closewire_client.writes._tier import TIER2_FIELDS as _WRITES_FIELDS
    except Exception as exc:
        findings.append(f"could not cross-check writes/_tier.py: {exc!r}")
    else:
        if set(_WRITES_FIELDS) != set(TRASH_FIELDS):
            findings.append(
                f"TRASH_FIELDS {sorted(TRASH_FIELDS)} != writes._tier.TIER2_FIELDS "
                f"{sorted(_WRITES_FIELDS)}"
            )

    # 5. The live package: every binding on a Tier-2 route is blocked, no read is.
    covered: set[Route] = set()
    for module_name, functions in _package_functions().items():
        for func_name, func in functions.items():
            route = route_of(func)
            blocked = bool(getattr(func, "_closewire_tier2_blocked", False))
            if route is None:
                if blocked:
                    findings.append(f"{module_name}.{func_name} is blocked but has no route")
                continue
            if route in TIER2_ROUTES:
                covered.add(route)
                if not blocked:
                    findings.append(
                        f"UNGATED Tier-2 route reachable: {module_name}.{func_name} -> "
                        f"{route[0]} {route[1]}"
                    )
            elif blocked:
                findings.append(
                    f"{module_name}.{func_name} is blocked but {route[0]} {route[1]} is "
                    f"not Tier-2 — the read path must not be over-blocked"
                )
            elif route in TRASH_ROUTES and not getattr(func, "_closewire_field_guarded", False):
                findings.append(
                    f"{module_name}.{func_name} accepts a {sorted(TRASH_FIELDS)} body "
                    f"ungated -> {route[0]} {route[1]}"
                )
    for route in sorted(set(TIER2_ROUTES) - covered):
        # Not a failure: the spec has the route, this codegen run did not emit a wrapper
        # for it (or emitted it under a shape route_of cannot read). Worth saying out loud.
        findings.append(
            f"NOTE: no generated wrapper found for Tier-2 route {route[0]} {route[1]} "
            f"— nothing to guard, but confirm codegen still emits it"
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    """``python -m closewire_client.tiers`` — print the audit, exit non-zero on findings."""
    findings = audit()
    gated = len(TIER2_ROUTES)
    print(f"closewire tier audit: {gated} Tier-2 routes, {len(NOT_GATED_MUTATIONS)} mutating "
          f"routes explicitly not gated")
    for rule in ("money", "publish", "destroy"):
        routes = sorted(r for r, p in TIER2_ROUTES.items() if p.rule == rule)
        print(f"  {rule:8} {len(routes):2}  " + ", ".join(f"{m} {p}" for m, p in routes))
    if not findings:
        print("ALL CHECKS PASSED")
        return 0
    print(f"\n{len(findings)} finding(s):")
    for finding in findings:
        print(f"  {finding}")
    return 0 if all(f.startswith("NOTE:") for f in findings) else 1


if __name__ == "__main__":
    # `python -m closewire_client.tiers` executes this file a second time, because
    # `closewire_client/__init__.py` already imported it to install the guard. Delegate to
    # the copy that is actually holding the guard so the audit can never report on tables
    # that differ from the ones in force. (`audit()` is also importable directly — that is
    # the entry point for scripts and CI, and it avoids the double execution entirely.)
    _canonical = sys.modules.get(__spec__.name if __spec__ else "closewire_client.tiers")
    raise SystemExit((getattr(_canonical, "main", None) or main)())
