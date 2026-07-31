"""Gate: `--json` works on every command, derived from the parser rather than a list.

`cli/main.py`'s module docstring promises "A global `--json` switches **any** command to JSON
on stdout with nothing else". Twice now that promise was false, and both times the coverage
that should have caught it was a hand-maintained list:

* round 12 — `ping`/`whoami`: `closewire --json ping` printed seven lines of human text,
  `closewire ping --json` was an argparse error. They are not in `cli.reads.READ_COMMANDS`,
  which is what `scripts/verify_cli.py` iterates, so the verifier certifying `--json` purity
  had never invoked the commands that broke it.
* round 13 — `pacing-reset`: **the identical defect, eight lines away in the same file**, found
  by a critic immediately after the round-12 fix shipped. The round-12 fix had added two
  hand-written probes for `ping`/`whoami` and left the mechanism alone, so the class was
  untouched: of 41 declared command pairs, 16 had no `--json` coverage at all.

That is the lesson this file encodes. The defect is not "ping was broken" or "pacing-reset was
broken" — it is **"coverage is a list someone maintains, and the parser is the truth"**. So
coverage is derived from `cli.main.build_parser()` here, and a 42nd command cannot be added
without either accepting `--json` or being named, with a reason, in `_NO_JSON`.

Two properties, because the two failures were different:

* **parse** — `--json` must be accepted in *both* positions. Prefix was accepted-and-ignored;
  postfix was a hard usage error. A check on one position would have caught only one bug.
* **forward** — the handler must actually pass it on. `ping` parsed `--json` fine once the
  subparser had it; the handler was `lambda args: cmd_ping()`, dropping it on the floor.

Offline and fast. `scripts/verify_cli.py` proves purity live against the real API; this proves
the wiring exists at all, on every commit, in under a second.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from cli.main import _HANDLERS, _declared_pairs, build_parser

ROOT = Path(__file__).resolve().parents[1]

#: Commands that legitimately produce no JSON, each with the reason. Anything here is an
#: explicit decision; anything *not* here must honour `--json`.
_NO_JSON: dict[str, str] = {
    # Bare `closewire` prints a config summary and is not a command.
    "": "no subcommand — the config summary, not a result",
}


def _commands() -> list[tuple[str, str]]:
    """Every (group, leaf) pair the parser declares — the same source `_ROUTES` is checked against."""
    return sorted(_declared_pairs(build_parser()))


def _subparsers(parser) -> dict[str, object]:
    """Every subparser the parser declares, keyed by the name it is invoked under.

    Includes aliases: `whoami` is an alias of `ping`, and it was equally broken.
    """
    import argparse

    found: dict[str, object] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            found.update(action.choices)
    return found


def _has_json_option(parser) -> bool:
    """Does this parser declare a `--json` option?

    Introspected rather than probed with `parse_args`, because commands like `test start`
    have required options (`--bot`) and `billing refill` has required positionals — a probe
    would fail on the missing argument and report a `--json` defect that is not there. The
    property being asserted is "this subparser declares the flag", and that is exactly what
    reading its option strings tests.
    """
    return any("--json" in (action.option_strings or []) for action in parser._actions)


def test_every_command_accepts_json_in_both_positions() -> None:
    """`--json` before *and* after the subcommand.

    Both orders are asserted because the two real defects failed differently: prefix `--json`
    was silently accepted and ignored (the root parser has the flag, so parsing succeeded and
    the handler discarded it), postfix was a hard `unrecognized arguments: --json`. A check on
    one position would have caught one bug and missed the other.
    """
    root = build_parser()
    assert _has_json_option(root), (
        "the root parser no longer declares --json, so `closewire --json <cmd>` is a usage "
        "error for every command"
    )

    problems: list[str] = []
    subparsers = _subparsers(root)
    for name, subparser in sorted(subparsers.items()):
        if name in _NO_JSON:
            continue
        nested = _subparsers(subparser)
        if nested:
            # A group like `bots` or `test`: the leaves carry the flag, not the group.
            for leaf_name, leaf in sorted(nested.items()):
                full = f"{name} {leaf_name}"
                if full in _NO_JSON:
                    continue
                if not _has_json_option(leaf):
                    problems.append(
                        f"{full}: subparser does not declare --json, so "
                        f"`closewire {full} --json` is `unrecognized arguments`. Give it "
                        "`parents=[json_opt]`, or add it to _NO_JSON with a reason."
                    )
        elif not _has_json_option(subparser):
            problems.append(
                f"{name}: subparser does not declare --json, so `closewire {name} --json` "
                "is `unrecognized arguments`. Give it `parents=[json_opt]`, or add it to "
                "_NO_JSON with a reason."
            )
    assert not problems, "\n".join(problems)


def test_every_handler_forwards_json_to_its_command() -> None:
    """A subparser that accepts `--json` and a handler that drops it is the `ping` defect.

    Checked by reading the handler's source: it must reference `json`. `cmd_ping` parsed the
    flag correctly the moment its subparser gained `parents=[json_opt]` — and still printed
    human text, because `_HANDLERS["ping"]` was `lambda args: cmd_ping()`.
    """
    problems: list[str] = []
    for name, handler in _HANDLERS.items():
        if name in _NO_JSON:
            continue
        try:
            source = inspect.getsource(handler)
        except (OSError, TypeError):  # pragma: no cover - defensive
            continue
        # Two legitimate shapes. Either the handler pulls the flag off `args` and passes it
        # explicitly (`cmd_ping(as_json=getattr(args, "json", False))`), or it hands the whole
        # namespace on and the command reads it itself (`cmd_read(args)`) — the group
        # dispatchers do the latter, and `args` carries `json` intact. What must not happen is
        # the `ping` defect: `lambda args: cmd_ping()`, where `args` is accepted and dropped.
        forwards_flag = "json" in source
        forwards_namespace = "(args" in source.replace(" ", "")
        if not (forwards_flag or forwards_namespace):
            problems.append(
                f"_HANDLERS[{name!r}] neither reads `json` off args nor forwards args, so a "
                f"parsed --json is dropped before it reaches the command: "
                f"{source.strip()[:90]}"
            )
    assert not problems, "\n".join(problems)


def test_the_parser_declares_more_commands_than_the_read_list() -> None:
    """The self-check, and the whole reason this file derives instead of listing.

    If this ever stops holding — if `READ_COMMANDS` grows to cover the parser — the two are
    the same set and a list-driven check would be adequate. Until then, anything iterating
    `READ_COMMANDS` is looking at a strict subset, and asserting that keeps the next reader
    from "simplifying" this file back into the defect it exists to stop.
    """
    from cli.reads import READ_COMMANDS

    declared = _commands()
    assert len(declared) > len(READ_COMMANDS), (
        f"the parser declares {len(declared)} commands and READ_COMMANDS holds "
        f"{len(READ_COMMANDS)}. If they are now equal, this gate's premise has changed — "
        "re-read its docstring before deleting it."
    )


def test_no_json_exemptions_are_justified() -> None:
    """An exemption with an empty reason is an exemption nobody has to defend."""
    for name, reason in _NO_JSON.items():
        assert reason and len(reason) > 10, f"_NO_JSON[{name!r}] needs a real reason"


def test_the_json_promise_is_still_documented() -> None:
    """The gate exists to enforce a promise. If the promise is withdrawn, so is the gate.

    Pins the claim in `cli/main.py`'s docstring, so that weakening the guarantee is a
    deliberate edit to both places rather than a silent drift in one.
    """
    source = (ROOT / "cli" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    doc = ast.get_docstring(tree) or ""
    assert "--json" in doc, (
        "cli/main.py's module docstring no longer documents the --json contract this gate "
        "enforces. If the promise changed, update _NO_JSON and this test together."
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} json-contract tests passed.")
