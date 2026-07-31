"""Gate: there is exactly one list of checks, and CI runs it.

The defect this exists to stop is not hypothetical — it is the one that made phase 09 take
eleven review rounds. `ruff` was a declared dev dependency from phase 01 that nobody ever
ran; a gate file shipped untracked because no list mentioned it; every critic verified a
different subset in a different order. All of it followed from there being no single
authoritative command.

`scripts/ci.py` is that command now. The way it stops being authoritative is drift: someone
adds a check to the workflow but not the script, or the workflow stops calling the script and
starts hand-rolling `pytest` and `ruff` steps, and six weeks later the thing developers run
locally and the thing that gates a merge are different. So:

* the workflow must invoke `scripts/ci.py`;
* the workflow must NOT run any validation command directly, because the moment it does, it
  is a second list;
* every check the script names must actually be runnable — a label pointing at a deleted
  script is a check that silently stops happening.

The last one matters most and is the easiest to get wrong. A `ci.py` listing a verifier that
no longer exists would fail loudly *when run*; this asserts it before that, and it also
catches the reverse — a verifier added under `scripts/` that nothing wires in.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CI_SCRIPT = ROOT / "scripts" / "ci.py"

#: Commands that constitute validation. A workflow naming any of these directly has started
#: keeping its own list.
_VALIDATION_COMMANDS = ("pytest", "ruff", "verify_writes", "verify_tier2", "closewire_client.tiers")

#: Scripts that are simply **not validation** — each with its reason.
#:
#: Kept separate from `_MUST_NEVER_RUN` below, because conflating them produced a real
#: hazard: `test_no_verifier_script_is_left_unwired`'s remedy message says "Wire them in",
#: and it was saying that about a script that posts to a metered endpoint.
_NOT_A_CHECK = {
    #: regenerate vendored artefacts; not checks.
    "codegen.py", "fetch_spec.py",
    #: a narrated demonstration of the pacer, not a check.
    "pacing_demo.py",
    #: the entry point itself.
    "ci.py",
    #: measures how well a gate holds. The gate is enforced on every run by
    #: `tests/test_validation_logs.py`; this sweeps the real corpus and takes minutes, so it
    #: is a tool you reach for when you change the matcher.
    "sweep_retraction_gate.py",
}

#: Header names that carry a credential, and the modules that can put one on a live wire.
#: Used by `_can_reach_a_metered_endpoint` — see it for why the predicate needs both halves.
_SPEND_IMPORTS = ("closewire_client.live", "closewire_client.tier2", "closewire_client.writes")
_SPEND_NAMES = frozenset({"send_message", "message_endpoint", "live_base", "LiveMessageClient"})
_CLIENT_NAMES = frozenset({"Client", "AsyncClient", "Session", "RestClient"})


def _can_reach_a_metered_endpoint(path: Path) -> list[str]:
    """Why this script could spend money, or `[]` if it structurally cannot.

    **The predicate, not a filename.** The previous rule named `probe_runtime_auth.py` as a
    string literal, so it said nothing about the next dangerous script — and there already
    was one: `probe_goal_state.py` imports `closewire_client.writes.testing` and builds a
    real `RestClient`, so it can open a metered test session. One of the two dangerous
    scripts was protected.

    Both halves are required, and neither alone works:

    * **network-capable** — constructs a client with no `transport=` override, or spawns
      `cli.main`. `verify_writes.py` and `verify_tier2.py` name spend surfaces *and* force
      `dry_run=False`, yet are safe, because they inject a `MockTransport` that raises on
      any request at all. "Sets `dry_run=False`" would have banned them wrongly.
    * **names a spend surface** — imports `live`/`tier2`/`writes`, or references
      `send_message`/`message_endpoint`/`live_base`/`LiveMessageClient`. `verify_reads.py`
      and `verify_cli.py` are network-capable but read-only, so this half clears them.

    Read from the AST rather than the raw text: prose in `codegen.py` and this very
    docstring mention these names, and a substring rule would flag them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))

    spend: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module.startswith(prefix) for prefix in _SPEND_IMPORTS):
                spend.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(prefix) for prefix in _SPEND_IMPORTS):
                    spend.append(alias.name)
        elif isinstance(node, ast.Name) and node.id in _SPEND_NAMES:
            spend.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _SPEND_NAMES:
            spend.append(node.attr)
    if not spend:
        return []

    network: list[str] = []
    source = path.read_text(encoding="utf-8")
    if "cli.main" in source and "subprocess" in source:
        network.append("spawns cli.main")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _CLIENT_NAMES:
            continue
        if any(kw.arg == "transport" for kw in node.keywords):
            continue  # a hermetic client: the transport is injected and can be a tripwire
        if name == "RestClient" and len(node.args) >= 2:
            continue  # handed a Session it did not build
        network.append(f"{name}() at line {node.lineno}")
    if not network:
        return []
    return sorted(set(spend)) + network


#: Scripts that must **never** be reachable from CI, because they can spend money.
#:
#: Derived by `_can_reach_a_metered_endpoint`; listed here so the set is also visible to a
#: reader, and cross-checked against the predicate by
#: `test_the_predicate_still_separates_the_dangerous_from_the_hermetic`.
_MUST_NEVER_RUN = {"probe_runtime_auth.py", "probe_goal_state.py"}


def _workflow_body() -> str:
    """The workflow with comments stripped.

    Comments legitimately name the excluded scripts to explain *why* they are excluded, and
    a gate that forbade discussing itself would be absurd.
    """
    return "\n".join(
        line for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _ci_checks() -> list[tuple[str, list[str]]]:
    """The (label, argv) pairs `scripts/ci.py` declares, read from its AST.

    Read rather than imported so this test cannot be defeated by import-time side effects,
    and so a syntax error in `ci.py` surfaces here as a syntax error.
    """
    tree = ast.parse(CI_SCRIPT.read_text(encoding="utf-8"), str(CI_SCRIPT))
    found: list[tuple[str, list[str]]] = []
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in {"OFFLINE", "LIVE"} or node.value is None:
            continue
        for element in getattr(node.value, "elts", []):
            parts = getattr(element, "elts", [])
            if len(parts) < 2:
                continue
            label = parts[0].value if isinstance(parts[0], ast.Constant) else "?"
            argv = [
                item.value for item in getattr(parts[1], "elts", [])
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            found.append((label, argv))
    return found


def test_the_workflow_delegates_to_the_ci_script() -> None:
    """CI must run the same command a developer runs, not a parallel copy of it."""
    assert WORKFLOW.exists(), f"{WORKFLOW.relative_to(ROOT)} is missing: nothing gates a merge"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/ci.py" in text, (
        "the CI workflow does not invoke scripts/ci.py, so what CI validates and what a "
        "developer validates locally are no longer the same thing"
    )


def test_the_workflow_does_not_keep_its_own_list_of_checks() -> None:
    """A workflow that runs `pytest` itself has become a second, drifting source of truth."""
    text = WORKFLOW.read_text(encoding="utf-8")
    # Only the executable part. Comments legitimately mention these by name to explain why
    # they are not listed here, and a gate that forbade discussing itself would be absurd.
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    offenders = [command for command in _VALIDATION_COMMANDS if command in body]
    assert not offenders, (
        f"the CI workflow runs {offenders} directly instead of going through scripts/ci.py. "
        "Add the check to scripts/ci.py so local runs and CI cannot diverge."
    )


def test_every_check_ci_declares_actually_exists() -> None:
    """A label pointing at a deleted script is a check that silently stopped running."""
    problems: list[str] = []
    for label, argv in _ci_checks():
        targets = [part for part in argv if part.endswith(".py")]
        for target in targets:
            if not (ROOT / target).exists():
                problems.append(f"{label}: {target} does not exist")
    assert not problems, "\n".join(problems)


def test_no_script_that_can_spend_is_reachable_from_ci() -> None:
    """The class gate. Derived from what a script *does*, never from its filename.

    The previous version named `probe_runtime_auth.py` as a string literal — so it said
    nothing about the next dangerous script, and there already was one: `probe_goal_state.py`
    opens metered test sessions and was completely unguarded. A second spending script added
    tomorrow is caught by this without anyone remembering to name it.
    """
    dangerous = {path.name: _can_reach_a_metered_endpoint(path)
                 for path in sorted((ROOT / "scripts").glob("*.py"))}
    dangerous = {name: why for name, why in dangerous.items() if why}

    wired = {Path(part).name for _label, argv in _ci_checks()
             for part in argv if part.endswith(".py")}
    overlap = sorted(set(dangerous) & wired)
    assert not overlap, (
        f"these scripts can reach a metered endpoint and are wired into scripts/ci.py: "
        f"{ {name: dangerous[name] for name in overlap} }. CI runs on every commit; it must "
        "never be able to spend."
    )

    body = _workflow_body()
    in_workflow = sorted(name for name in dangerous if name in body)
    assert not in_workflow, (
        f"the CI workflow invokes {in_workflow}, which can spend credits."
    )

    undeclared = sorted(set(dangerous) - _MUST_NEVER_RUN)
    assert not undeclared, (
        f"{undeclared} can reach a metered endpoint but is not in _MUST_NEVER_RUN. Add it "
        "with its reason, so the exclusion is a decision rather than an oversight."
    )


def test_the_predicate_still_separates_the_dangerous_from_the_hermetic() -> None:
    """Self-check. A predicate narrowed to nothing makes the gate above vacuous.

    The hermetic half matters as much as the dangerous half: `verify_writes.py` and
    `verify_tier2.py` both name spend surfaces *and* force `dry_run=False`, and are safe only
    because they inject a transport that raises. A rule that banned them would be abandoned
    within a week, which is how gates die.
    """
    scripts = ROOT / "scripts"
    for name in sorted(_MUST_NEVER_RUN):
        assert _can_reach_a_metered_endpoint(scripts / name), (
            f"{name} is declared dangerous but the predicate no longer sees why — the gate "
            "above has gone blind"
        )
    for name in ("verify_writes.py", "verify_tier2.py", "verify_reads.py", "verify_cli.py",
                 "verify_runners.py", "sweep_retraction_gate.py"):
        assert not _can_reach_a_metered_endpoint(scripts / name), (
            f"{name} is hermetic or read-only but the predicate calls it dangerous; a rule "
            "that over-fires gets deleted"
        )


def test_ci_declares_some_checks_at_all() -> None:
    """The self-check. An empty list would make all three tests above vacuously green."""
    checks = _ci_checks()
    assert len(checks) >= 5, f"scripts/ci.py declares only {len(checks)} checks: {checks}"
    labels = {label for label, _ in checks}
    for expected in ("lint", "tests"):
        assert expected in labels, f"scripts/ci.py no longer runs {expected!r}"


def test_no_verifier_script_is_left_unwired() -> None:
    """A verifier nothing runs is a verifier that rots.

    `scripts/verify_cli.py` was written in phase 06 and, before `ci.py`, was run only when a
    critic happened to think of it. Anything new under `scripts/` must either be wired into
    `ci.py` or be named in `_NOT_A_CHECK` with a reason — an explicit decision either way.
    """
    wired = {part for _label, argv in _ci_checks() for part in argv if part.endswith(".py")}
    wired_names = {Path(part).name for part in wired}
    unwired = sorted(
        path.name for path in (ROOT / "scripts").glob("*.py")
        if path.name not in wired_names
        and path.name not in _NOT_A_CHECK
        and path.name not in _MUST_NEVER_RUN
    )
    assert not unwired, (
        f"these scripts are neither run by scripts/ci.py nor listed as deliberately excluded: "
        f"{unwired}. Wire them in, or add them to _NOT_A_CHECK with the reason. If the script "
        "can reach a metered endpoint, it belongs in _MUST_NEVER_RUN instead — do NOT wire it."
    )


def test_ci_captures_every_check_it_runs() -> None:
    """A failing check must leave evidence, or a flake can never be diagnosed.

    `ci.py --live` reported `FAIL cli` once in three runs and the reason was unrecoverable:
    `verify_cli.py` prints its whole FAILURES list before exiting, and `ci.py` ran it with no
    capture, so the only text that could have named the cause went to a scrollback. It was
    written up as an "open flake" — which is what you are left with when the harness discards
    the evidence.
    """
    source = CI_SCRIPT.read_text(encoding="utf-8")
    assert "PYTHONUNBUFFERED" in source, (
        "scripts/ci.py does not force unbuffered child output; with stdout piped a long check "
        "emits nothing for minutes and a killed child emits nothing at all"
    )
    assert "LOG_DIR" in source and "log_path.open" in source, (
        "scripts/ci.py does not persist each check's output. A failing check that does not "
        "reproduce on the next run must still leave something to read."
    )


def test_no_single_call_can_outlive_the_verifier_subprocess_timeout() -> None:
    """Two knobs in different files, with the relation between them asserted.

    `rest.py` honours a server `Retry-After` verbatim up to `retry_after_max_s` (default 900),
    while `verify_cli.py` used to kill each subprocess at a hard-coded 300 — and caught
    `TimeoutExpired` nowhere, so one throttled call killed the verifier mid-run and surfaced
    as a bare `FAIL cli`. The timeout is now derived; this stops it drifting back apart.
    """
    import importlib.util

    from closewire_client.config import load_config

    spec = importlib.util.spec_from_file_location("_vc", ROOT / "scripts" / "verify_cli.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    config = load_config(strict=False)
    worst = config.retry_after_max_s + config.max_retries * config.backoff_cap_s
    assert module.SUBPROCESS_TIMEOUT > worst, (
        f"one honoured Retry-After can block {worst:.0f}s, but verify_cli kills the "
        f"subprocess at {module.SUBPROCESS_TIMEOUT:.0f}s — a legitimate wait would be "
        "reported as a hang"
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} CI wiring tests passed.")
