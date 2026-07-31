"""``closewire`` console entry point.

Subcommands:

* (no subcommand)  -> print version + a secret-free config summary.
* ``ping`` / ``whoami`` -> prove live connectivity: call ``GET /agency/current`` (+
  ``GET /agency/usage``) and ``GET /bot``, then print the agency, usage, and a bot count
  with the first few bot ids/names. The API key is redacted in every code path.
* ``pacing-status`` -> print the pacer's budgets, breaker state, and dry-run posture.
* ``pacing-reset`` -> close a tripped circuit breaker after investigating.
* ``bots`` / ``personas`` / ``sources`` / ``leads`` / ``metrics`` -> Tier-0 reads
  (see :mod:`cli.reads`). Read-only; every call is paced.
* ``bots publish|delete|export`` / ``personas|sources|leads delete`` / ``billing`` ->
  Tier-2 (see :mod:`cli.tier2`). Every one is guarded by an echoed ``--confirm``.
* ``test start|say|show|end`` -> a QA loop on a throwaway test session
  (see :mod:`cli.testing`). ``test say`` spends a credit.

A global ``--json`` switches any command to JSON on stdout with nothing else, so output is
safe to pipe. Logging is pinned to stderr for the same reason.

Exit codes: ``0`` ok · ``1`` failure (including a usage error) · ``2`` the circuit breaker
is OPEN · ``3`` refused — a Tier-2 guard, or a ``test`` command's local validation. See
:class:`_Parser` for why argparse's own ``2`` is remapped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Callable, NoReturn

from closewire_client import __version__, load_config
from closewire_client.config import Config, ConfigError, MissingConfigError
from closewire_client.console import configure_streams
from cli.reads import READ_COMMANDS, add_read_parsers, dispatch_read
from cli.testing import TEST_COMMANDS, add_test_parsers, dispatch_test
from cli.tier2 import TIER2_COMMANDS, add_tier2_parsers, dispatch_tier2

_MAX_JSON = 1500

#: The process exit-code contract, in one place. Every ``cli`` dispatcher module declares
#: its own copy of these numbers; :func:`_assert_exit_codes_agree` checks they all agree.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_HALTED = 2
#: A guard refused, or a command's own validation did. Declared here although this module
#: never returns it: the contract an operator scripts against is all four numbers, and a
#: code that exists only in the modules using it has nothing to be checked against.
EXIT_REFUSED = 3

#: The status ``argparse.ArgumentParser.error()`` exits with. See :class:`_Parser`.
_ARGPARSE_USAGE_STATUS = 2


class _Parser(argparse.ArgumentParser):
    """``argparse`` with its usage-error status moved off this CLI's ``EXIT_HALTED``.

    ``ArgumentParser.error()`` exits **2**, and 2 is this project's "the circuit breaker is
    OPEN" — the one code that means *stop, traffic is halted, investigate the account*. So
    a mistyped flag reported the same thing as a tripped breaker, and a wrapper that reacts
    to 2 would send an operator to ``closewire pacing-reset`` over a typo. A usage error is
    an ordinary failure of the invocation, so it reports :data:`EXIT_FAILURE`; ``--help``
    and ``--version`` exit 0 and are untouched.

    A subclass rather than a ``try/except SystemExit`` in :func:`main`, because argparse
    errors from *sub*parsers too ("the following arguments are required: action").
    ``add_subparsers`` propagates ``type(self)`` to every parser it creates, so declaring
    the root as this class covers all three levels — including the group parsers
    :mod:`cli.reads` builds and the actions :mod:`cli.tier2` attaches to them.
    """

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        super().exit(
            EXIT_FAILURE if status == _ARGPARSE_USAGE_STATUS else status, message
        )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="closewire",
        description="Configure and read Closebot.ai from the command line.",
    )
    parser.add_argument("--version", action="version", version=f"closewire {__version__}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout and nothing else, so the output is safe to pipe to jq. "
        "Progress, pacing, and warnings always go to stderr.",
    )
    # Shared so `--json` works before OR after the subcommand. SUPPRESS is load-bearing:
    # a child default of False would overwrite a global `--json` that was already set.
    json_opt = argparse.ArgumentParser(add_help=False)
    json_opt.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                          help="Emit JSON to stdout and nothing else.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "ping",
        aliases=["whoami"],
        parents=[json_opt],
        help="Prove connectivity: show the agency and this account's bots.",
    )
    sub.add_parser(
        "pacing-status",
        parents=[json_opt],
        help="Show pacing budgets, breaker state, and dry-run posture.",
    )
    sub.add_parser(
        "pacing-reset",
        parents=[json_opt],
        help="Close a tripped circuit breaker (do this only after investigating).",
    )
    groups = add_read_parsers(sub, json_opt)
    add_tier2_parsers(sub, groups, json_opt)
    add_test_parsers(sub, json_opt)
    _assert_routes_agree(parser)
    _assert_exit_codes_agree()
    return parser


# ── Routing ───────────────────────────────────────────────────────────────────
def _pair(command: str) -> tuple[str, str]:
    """``"bots delete"`` → ``("bots", "delete")``; ``"ping"`` → ``("ping", "")``."""
    group, _, action = command.partition(" ")
    return group, action


#: ``(group, action)`` → the handler that must run.
#:
#: Routing is on the **pair**, not the group. Two tiers share the nouns ``bots``,
#: ``personas``, ``sources`` and ``leads``, so a group name no longer identifies a handler:
#: routing on the group alone would have sent ``bots delete`` to the read dispatcher, which
#: would not recognise the action and would exit 1 — a silent failure wearing the costume of
#: an API error.
#:
#: **Derived, not retyped.** The read third is built from :data:`cli.reads.READ_COMMANDS`, the
#: Tier-2 third from :data:`cli.tier2.TIER2_COMMANDS`, and the QA third from
#: :data:`cli.testing.TEST_COMMANDS` — every group has exactly one tuple that owns it, and
#: :func:`_assert_routes_agree` checks the result against what argparse actually declares.
#: Before that, correctness rested
#: on four independently hand-kept artifacts (this table, both command tuples, and a separate
#: ``_READ_GROUPS``) with nothing asserting they agreed — and the failure mode was silent in
#: the worst possible direction: a pair missing from the table fell through to
#: :func:`cmd_config`, so ``closewire billing refill --amount 500 --confirm 500`` printed a
#: config summary and **exited 0**. A money command reporting success for doing nothing.
#: ``_READ_GROUPS`` is gone: one fewer artifact to keep in step.
_ROUTES: dict[tuple[str, str], str] = {
    ("ping", ""): "ping",
    ("whoami", ""): "ping",
    ("pacing-status", ""): "pacing-status",
    ("pacing-reset", ""): "pacing-reset",
    **{_pair(command): "read" for command in READ_COMMANDS},
    **{_pair(command): "tier2" for command in TIER2_COMMANDS},
    **{_pair(command): "test" for command in TEST_COMMANDS},
}

#: Handler name → the function :func:`main` calls. Thin lambdas rather than direct function
#: references so the name is resolved from module globals at call time: the table can sit
#: next to :data:`_ROUTES` (where it is read) instead of after the definitions, and a test
#: that patches ``cli.main.cmd_tier2`` still sees its patch take effect.
_HANDLERS: dict[str, Callable[[Any], int]] = {
    "ping": lambda args: cmd_ping(as_json=getattr(args, "json", False)),
    "pacing-status": lambda args: cmd_pacing_status(as_json=getattr(args, "json", False)),
    "pacing-reset": lambda args: cmd_pacing_reset(as_json=getattr(args, "json", False)),
    "read": lambda args: cmd_read(args),
    "tier2": lambda args: cmd_tier2(args),
    "test": lambda args: cmd_test(args),
}


def _subparsers_of(parser: argparse.ArgumentParser) -> "argparse._SubParsersAction | None":
    # `_actions` / `_SubParsersAction` are argparse internals, and there is no public way to
    # ask a parser what subcommands it declares. Reading them is what makes the check an
    # *independent* witness: anything that re-derived the list from READ_COMMANDS and
    # TIER2_COMMANDS would be comparing those two tuples with themselves.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _declared_pairs(parser: argparse.ArgumentParser) -> frozenset[tuple[str, str]]:
    """Every ``(group, action)`` argparse declares, read back off the built parser.

    Read from the parser rather than from any list, so this is an independent witness: it
    sees exactly what a user can type, including aliases (``whoami``) and whatever a tier
    module attached to a group it did not create.
    """
    top = _subparsers_of(parser)
    if top is None:  # pragma: no cover - the CLI always has subcommands
        return frozenset()
    pairs: set[tuple[str, str]] = set()
    for name, subparser in top.choices.items():
        nested = _subparsers_of(subparser)
        if nested is None:
            pairs.add((name, ""))
        else:
            pairs.update((name, action) for action in nested.choices)
    return frozenset(pairs)


def _assert_routes_agree(parser: argparse.ArgumentParser) -> None:
    """Fail loudly if the parser, the route table, and the handlers disagree.

    Called from :func:`build_parser`, so it runs on **every** invocation and in every test
    that builds the CLI — a mismatch cannot be shipped, let alone reached at runtime. The
    check is one set comparison over the whole declared surface: 4 top-level commands
    (counting the ``whoami`` alias) + 23 Tier-0 reads + 10 Tier-2 + 4 ``test`` = 41 pairs
    today, and it is the tuples that are authoritative, not that count.

    It exists because every failure mode here is silent. A declared pair with no route
    falls through to :func:`cmd_config` and exits **0** printing a config summary; a routed
    pair argparse never declares is dead code that reads as coverage; a route naming a
    handler that does not exist raises ``KeyError`` mid-command. None of those announce
    themselves, and one of them turns ``billing refill`` into a silent success.
    """
    declared = _declared_pairs(parser)
    routed = frozenset(_ROUTES)
    problems = []
    if unroutable := sorted(declared - routed):
        problems.append(
            f"declared by argparse but not routed (each would fall through to cmd_config() "
            f"and exit 0 printing a config summary): {unroutable}"
        )
    if orphans := sorted(routed - declared):
        problems.append(
            f"routed but never declared, so unreachable: {orphans}"
        )
    if missing := sorted(set(_ROUTES.values()) - set(_HANDLERS)):
        problems.append(f"routed to handlers that do not exist: {missing}")
    if problems:
        raise RuntimeError(
            "closewire CLI routing table is out of step with the parser:\n  - "
            + "\n  - ".join(problems)
            + "\nThe table is built from cli.reads.READ_COMMANDS, cli.tier2.TIER2_COMMANDS "
            "and cli.testing.TEST_COMMANDS; add or remove the command in the tuple its "
            "group belongs to and in that module's argparse declaration together."
        )


#: The codes every dispatcher module must declare. ``EXIT_REFUSED`` is deliberately absent:
#: a module with no refusal path (``cli.reads``) has no reason to name it, and requiring it
#: would push a dead constant into it.
_UNIVERSAL_EXIT_CODES = ("EXIT_OK", "EXIT_FAILURE", "EXIT_HALTED")


def _exit_code_modules() -> list[tuple[str, dict[str, Any]]]:
    """Every loaded ``cli`` submodule's ``EXIT_*`` constants, **discovered not listed**.

    A hand-kept list is exactly what failed. The previous one named ``cli.reads`` and
    ``cli.tier2`` and was never extended when :mod:`cli.testing` arrived with a fourth
    independent copy of the constants — so ``cli.testing.EXIT_HALTED = 4`` (a critic's
    probe: "breaker open" now colliding with nothing at all, and the CLI reporting a halt
    the operator's wrapper cannot recognise) raised nothing at all.

    Reading ``sys.modules`` makes the set self-maintaining. Anything this CLI can route to
    has necessarily been imported by the time :func:`build_parser` runs — routing to a
    module means importing its dispatcher at the top of this file — so what is loaded *is*
    the exit-code surface, and a fifth tier module is covered on the day it is written
    rather than on the day someone remembers this function. ``cli.main`` itself appears
    here when it was imported as a module (the console-script path) and compares against
    its own numbers, which is a no-op, not a false negative.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("cli."):
            continue
        # Read `__dict__` defensively rather than calling vars(): sys.modules is a public
        # dict anyone may put anything in (a lazy-import shim, a test double, None for a
        # failed import), and this runs inside build_parser() — on *every* invocation of
        # every command. A TypeError here would take the whole CLI down over an entry that
        # was never part of the exit-code surface to begin with.
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        codes = {
            const: value
            for const, value in namespace.items()
            if const.startswith("EXIT_")
        }
        if codes:
            found.append((name, codes))
    # Keyed on the name: the payloads are dicts, which have no ordering at all.
    return sorted(found, key=lambda pair: pair[0])


def _assert_exit_codes_agree() -> None:
    """Fail loudly if the CLI modules stop meaning the same thing by 0/1/2/3.

    Each dispatcher module declares its own copy, and an operator's wrapper scripts against
    the numbers, not the constants — so a divergence would be invisible in review and
    visible only as a wrapper reacting to the wrong event: sending someone to
    ``pacing-reset`` over a refusal, or ignoring a halt. It is the same reason
    :class:`_Parser` exists.

    Called from :func:`build_parser`, so it runs on every invocation and in every test that
    builds the CLI. Three ways to disagree are checked, not one: a module that gives a
    shared name a different number, a module that *omits* one of
    :data:`_UNIVERSAL_EXIT_CODES` (and has therefore stopped declaring the contract it
    returns), and a module that invents an ``EXIT_*`` this file has never heard of — the
    last because a fifth code is a change to the contract itself, and it should be made
    here, deliberately, rather than appear in one module and be discovered by a wrapper.
    """
    ours = {
        "EXIT_OK": EXIT_OK,
        "EXIT_FAILURE": EXIT_FAILURE,
        "EXIT_HALTED": EXIT_HALTED,
        "EXIT_REFUSED": EXIT_REFUSED,
    }
    problems: list[str] = []
    for name, declared in _exit_code_modules():
        problems += [
            f"{name} does not declare {const}"
            for const in _UNIVERSAL_EXIT_CODES
            if const not in declared
        ]
        for const, value in sorted(declared.items()):
            if const not in ours:
                problems.append(
                    f"{name} declares {const}={value!r}, which cli.main does not"
                )
            elif value != ours[const]:
                problems.append(f"{name}.{const} is {value!r}, not {ours[const]!r}")
    if problems:
        raise RuntimeError(
            "closewire CLI exit codes disagree with cli.main "
            f"({', '.join(f'{k}={v}' for k, v in ours.items())}):\n  - "
            + "\n  - ".join(problems)
            + "\n0 ok / 1 failure / 2 breaker OPEN / 3 refused is a contract operators "
            "script against; every cli module must declare the same numbers."
        )


def main(argv: list[str] | None = None) -> int:
    # FIRST, and process-level. Every line below can print — including argparse's own help
    # and usage text, which carries '…' and '—'. Doing this inside `_with_client` (a
    # *request*-level function) left `ping`/`whoami`, `pacing-status`, `pacing-reset`,
    # `--help` and bare `closewire` running on the platform's codepage, where a single
    # em-dash killed the process with an unhandled UnicodeEncodeError and a traceback.
    # `closewire_client.console` says as much: being able to print non-ASCII is a property
    # of the process, so it belongs at the process entry point.
    configure_streams()
    _configure_logging()
    args = build_parser().parse_args(argv)
    handler = _ROUTES.get((args.command or "", getattr(args, "action", None) or ""))
    if handler is None:
        # Only bare `closewire` reaches here: argparse rejects an unknown subcommand, and
        # _assert_routes_agree has already proved every declared one has a route.
        return cmd_config()
    return _HANDLERS[handler](args)


# ── Tier-0 read commands ──────────────────────────────────────────────────────
def cmd_read(args: Any) -> int:
    """Run a read command against a paced client. 0 ok · 1 failure · 2 breaker open."""
    return _with_client(args, dispatch_read)


def cmd_test(args: Any) -> int:
    """Run a `test` command. 0 ok · 1 failure · 2 breaker open · 3 refused."""
    return _with_client(args, dispatch_test)


def cmd_tier2(args: Any) -> int:
    """Run a Tier-2 command. 0 ok · 1 failure · 2 breaker open · 3 a guard refused."""
    return _with_client(args, dispatch_tier2)


def _with_client(args: Any, dispatch) -> int:
    """Load config, open a paced client, and hand it to a dispatcher.

    Shared by both tiers so a Tier-2 command cannot accidentally acquire a differently
    configured client — in particular one whose pacing or dry-run posture differs from the
    read path's.

    Stream and logging setup used to happen here. Both are properties of the *process*, so
    doing them per request meant every entry point that never opens a client — ``ping``,
    ``pacing-status``, ``pacing-reset``, ``--help``, bare ``closewire`` — ran without them.
    They live in :func:`main` now, and are deliberately **not** repeated here: a redundant
    call would keep the request-level habit alive.
    """
    try:
        config = load_config()
    except MissingConfigError as exc:
        print(f"cannot run: missing {', '.join(exc.missing)} -- set it in `.env` "
              "(copy `.env.example`).", file=sys.stderr)
        return EXIT_FAILURE
    except ConfigError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    from closewire_client.rest import RestClient

    with RestClient(config) as rest:
        return dispatch(args, rest, as_json=getattr(args, "json", False))


def _configure_logging() -> None:
    """Send every log record to **stderr**.

    `--json` promises that stdout carries JSON and nothing else. Pacing emits think-time
    and budget messages, and redaction warns when a credential is unmasked; all of it must
    stay off stdout or piping to `jq` breaks.
    """
    root = logging.getLogger("closewire")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    # WARNING by default; CLOSEWIRE_LOG_LEVEL=DEBUG surfaces think-time and budget
    # decisions. Without this there was no way to watch the pacer at all, and no cheap way
    # to prove stdout stays clean while it logs.
    level = os.environ.get("CLOSEWIRE_LOG_LEVEL", "WARNING").upper()
    root.setLevel(getattr(logging, level, logging.WARNING))


# ── `closewire pacing-status` / `pacing-reset` ────────────────────────────────
def _load_pacer() -> "tuple[Any, int]":
    """Load config + a Pacer. Returns ``(pacer, 0)`` or ``(None, exit_code)``."""
    from closewire_client.pacing import Pacer

    try:
        config = load_config(strict=False)
    except ConfigError as exc:
        print(f"cannot load config: {exc}", file=sys.stderr)
        return None, 1
    try:
        return Pacer(config), 0
    except ValueError as exc:
        print(f"pacing configuration is invalid:\n{exc}", file=sys.stderr)
        return None, 1


def cmd_pacing_status(*, as_json: bool = False) -> int:
    """Print the pacer's stats. 0 when the breaker is closed, 2 when it is OPEN."""
    pacer, code = _load_pacer()
    if pacer is None:
        return code
    stats = pacer.stats()
    if as_json:
        print(json.dumps(stats.as_dict(), indent=2))
    else:
        print(f"closewire {__version__} -- pacing status\n")
        print(stats.render())
        if stats.breaker_state != "closed":
            print(
                "\n  Traffic is HALTED. Investigate the account/key, then run:\n"
                "    closewire pacing-reset",
                file=sys.stderr,
            )
    return EXIT_HALTED if stats.breaker_state != "closed" else EXIT_OK


def cmd_pacing_reset(*, as_json: bool = False) -> int:
    """Close a tripped breaker (clearing the persisted halt).

    0 when the halt is fully cleared, 1 when the persisted latch survived — reporting
    success while the next run comes back halted would be worse than an error.

    ``as_json`` is here for the same reason `cmd_ping` has it, and it is the *second*
    instance of that defect: `pacing-reset` was the last subparser built without
    ``parents=[json_opt]``, so `closewire pacing-reset --json` was an argparse error while
    `closewire --json pacing-reset` printed prose to stdout. Round 12 fixed `ping` and a
    critic found this one eight lines away in the same file — which is why the fix now
    ships with `tests/test_json_contract.py`, deriving coverage from the parser instead of
    from a hand-kept list.
    """
    pacer, code = _load_pacer()
    if pacer is None:
        return code
    was = pacer.stats()
    cleared = pacer.reset_breaker()
    if not cleared:
        # stderr in both modes: --json promises stdout is JSON, and an error is not a result.
        print(
            "breaker could NOT be fully reset -- the persisted halt is still on disk, so "
            "the next run will start halted.\n"
            "  Check CLOSEWIRE_STATE_DIR is a writable directory, then retry.",
            file=sys.stderr,
        )
        return 1
    if as_json:
        print(json.dumps({
            "reset": True,
            "was_state": was.breaker_state,
            "was_reason": was.breaker_reason,
        }, indent=2))
        return 0
    if was.breaker_state == "closed":
        print("breaker was already closed -- nothing to reset.")
    else:
        print(f"breaker was OPEN ({was.breaker_reason})\nbreaker reset -- traffic may resume.")
    return 0


# ── `closewire` (no subcommand): config summary ───────────────────────────────
def cmd_config() -> int:
    """Print version + redacted config. 0 on clean load, 1 when incomplete/invalid."""
    print(f"closewire {__version__}", flush=True)
    try:
        config = load_config()
    except MissingConfigError as exc:
        print("\nconfiguration: INCOMPLETE", file=sys.stderr)
        print(f"  missing required env var(s): {', '.join(exc.missing)}", file=sys.stderr)
        print(
            "  -> copy `.env.example` to `.env` and fill in your values "
            "(never commit real secrets).",
            file=sys.stderr,
        )
        print("\nloaded so far (defaults + any present values):", file=sys.stderr)
        print(exc.partial.redacted_summary(), file=sys.stderr)
        return 1
    except ConfigError as exc:
        print("\nconfiguration: INVALID", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 1

    print("\nconfiguration: OK")
    print(config.redacted_summary())
    return 0


# ── `closewire ping` / `whoami`: live connectivity ────────────────────────────
def cmd_ping(*, as_json: bool = False) -> int:
    """Call the read-only whoami endpoints and print the result. 0 on success.

    ``as_json`` exists because `--json` promised "stdout and nothing else" and this
    command did not honour it: `closewire --json ping` wrote seven lines of human text
    and `closewire ping --json` was a parse error, because `ping` was the one read
    subparser built without ``parents=[json_opt]``. A critic found it in phase 09 round
    12; `scripts/verify_cli.py` could not, because it never invoked `ping` or `whoami`.
    """
    if not as_json:
        print(f"closewire {__version__}", flush=True)
    try:
        config = load_config()
    except MissingConfigError as exc:
        print("\ncannot ping: missing required config", file=sys.stderr)
        print(f"  {', '.join(exc.missing)} -- set it in `.env` (copy `.env.example`).",
              file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"\ncannot ping: {exc}", file=sys.stderr)
        return 1

    # Import the transport lazily so plain `closewire` (config summary) needs no httpx.
    from closewire_client.endpoints import agency, bot
    from closewire_client.errors import ClosebotAPIError, ClosewireError
    from closewire_client.rest import RestClient

    if not as_json:
        print(f"auth style: {config.auth_style}  |  base: {config.api_base}", flush=True)

    try:
        with RestClient(config) as rest:
            if not as_json:
                print("-> GET /agency/current ...", flush=True)
            agency_obj = agency.get_agency_current(rest)
            if not as_json:
                print("-> GET /agency/usage ...", flush=True)
            try:
                usage_obj: Any = agency.get_agency_usage(rest)
            except ClosebotAPIError as exc:
                usage_obj = {"_error": f"HTTP {exc.status_code}"}
            if not as_json:
                print("-> GET /bot ...", flush=True)
            bots_obj = bot.get_bot(rest)
    except ClosebotAPIError as exc:
        print(f"\nHTTP {exc.status_code}: {exc.method} {exc.path}", file=sys.stderr)
        print(f"  {config.scrub(str(exc.body))[:800]}", file=sys.stderr)
        if exc.status_code in (401, 403):
            print(
                f"  auth rejected for style {config.auth_style!r} -- try another "
                "CLOSEWIRE_AUTH_STYLE (x-cb-key | authorization-key | authorization-bearer).",
                file=sys.stderr,
            )
        return 1
    except ClosewireError as exc:
        print(f"\nrequest failed: {config.scrub(str(exc))}", file=sys.stderr)
        return 1

    if as_json:
        # Scrubbed like every other JSON path: the payload is serialised, run through
        # `Config.scrub`, and printed as the only thing on stdout.
        payload = {
            "version": __version__,
            "auth_style": config.auth_style,
            "api_base": config.api_base,
            "agency": agency_obj,
            "usage": usage_obj,
            "bots": _as_list(bots_obj),
        }
        print(config.scrub(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        ))
        return 0
    _print_whoami(config, agency_obj, usage_obj, bots_obj)
    return 0


def _print_whoami(config: Config, agency_obj: Any, usage_obj: Any, bots_obj: Any) -> None:
    name = _pick(agency_obj, "name", "agencyName", "companyName", "title") or "(unknown)"
    print("\nOK (HTTP 2xx)")
    print(f"\nagency: {name}")
    print(_dump(config, agency_obj))

    print("\nusage:")
    print(_dump(config, usage_obj))

    bots = _as_list(bots_obj)
    print(f"\nbots: {len(bots)}")
    preview = [
        {
            "id": _pick(b, "id", "_id", "botId"),
            "name": _pick(b, "name", "botName", "title"),
        }
        for b in bots[:5]
    ]
    print(config.scrub(json.dumps(preview, indent=2, ensure_ascii=False, default=str)))


def _pick(obj: Any, *keys: str) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            if obj.get(key):
                return obj[key]
    return None


def _as_list(obj: Any) -> list[Any]:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("data", "bots", "items", "results"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return []


def _dump(config: Config, obj: Any) -> str:
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    text = config.scrub(text)
    return text if len(text) <= _MAX_JSON else text[:_MAX_JSON] + "\n… (truncated)"


if __name__ == "__main__":
    raise SystemExit(main())
