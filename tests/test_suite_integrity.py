"""Gates on the test suite itself.

Every file here runs two ways — under pytest, and directly (`python tests/test_x.py`) via an
`if __name__ == "__main__":` block that sweeps `globals()` for `test_*`. That sweep executes
*at the point the block appears in the file*, so **a test defined below it is invisible to
the direct runner** while pytest still collects it.

That has now bitten three times, each time on the newest test — the one guarding whatever was
just fixed:

* phase 09 round 3: two `decline_backoff` regressions, added below the runner;
* round 5: the same file, again;
* round 6: `test_show_finds_a_row_keyed_only_by_leadid`, the guard for a `leadId` fallback
  two critics had mutated away.

Each time the direct runner printed a count that quietly excluded the new guard. Fixing the
three instances does not stop a fourth, so this asserts the property instead: **whatever the
runner block claims to run, it must be everything pytest collects.**
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: Excluded from its own scan. Not because it lacks a runner — it has one, and it obeys both
#: rules — but because a gate that reports on itself invites the next reader to "fix" the gate
#: rather than the file that broke. (An earlier comment here claimed the file had no runner,
#: which was simply false; a critic caught it.)
_SELF = Path(__file__).name


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS.glob("test_*.py") if p.name != _SELF)


def _runner_line(tree: ast.Module) -> int | None:
    """Line of the `if __name__ == "__main__":` block, or None."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            return node.lineno
    return None


def test_no_test_is_defined_below_its_files_main_runner() -> None:
    """The gate. A test below the runner is collected by pytest and skipped by the runner.

    Reported per file with the offending names, because "some file is wrong" is not a useful
    failure message at 2am.
    """
    problems: list[str] = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        runner = _runner_line(tree)
        if runner is None:
            continue
        stragglers = [
            node.name
            for node in tree.body
            # Async and class-based tests count too: pytest collects them, the `globals()`
            # sweep does not, and a gate that only sees `def` would miss the next drift in a
            # shape it was written to stop. Flagged by critics in rounds 7 and 8.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and (node.name.startswith("test_") or node.name.startswith("Test"))
            and node.lineno > runner
        ]
        if stragglers:
            problems.append(
                f"{path.name}: {stragglers} defined below the __main__ runner at "
                f"line {runner}, so `python {path.name}` will not run them"
            )
    assert not problems, "\n".join(problems)


def test_every_test_file_that_can_run_directly_says_so_honestly() -> None:
    """A runner that reports a count must count what it actually ran.

    Weaker than the gate above and deliberately so: this only checks the runner sweeps
    `globals()` rather than a hand-maintained list, which is the other way the printed number
    drifts from reality.
    """
    problems: list[str] = []
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path))
        if _runner_line(tree) is None:
            continue
        # Locate the runner by LINE, from the AST, rather than by searching for a
        # double-quoted literal: `if __name__ == '__main__':` is equally valid and made an
        # earlier version raise ValueError instead of asserting.
        tail = "\n".join(source.splitlines()[_runner_line(tree) - 1:])
        if "globals()" not in tail:
            problems.append(
                f"{path.name}: its runner does not sweep globals(), so its count is "
                "hand-maintained and will drift"
            )
    assert not problems, "\n".join(problems)


def test_every_source_file_is_known_to_git() -> None:
    """A file git has never heard of ships as nothing at all.

    Phase 09 round 10 added `tests/test_auth_provenance.py` — the gate for that round's one
    BLOCKING finding — and left it untracked. The log asserted the fix was gated; a commit
    would have shipped the fix with no gate and, worse, turned the suite-total gate red on a
    fresh clone for a reason that named the wrong file.

    Checked with `git ls-files --others`, which already excludes anything in the index — so
    a path registered with `git add -N` counts as known, which is what this repo needs. An
    earlier version also ran `--cached --others` and never read its output; the docstring
    credited that dead subprocess with the behaviour `--others` provides on its own.
    """
    import subprocess

    root = TESTS.parent
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, cwd=root,
    )
    assert untracked.returncode == 0, f"git ls-files failed: {untracked.stderr}"
    # `.py` plus everything under `docs/validation/`. The first version checked only `.py`
    # and immediately let an untracked evidence capture through — the very file a critic had
    # asked for so the probe results would stop being unverifiable prose. A deliverable that
    # is not in the commit is not a deliverable, whatever its extension.
    watched = [
        line.strip() for line in untracked.stdout.split("\n")
        if line.strip().endswith(".py") or line.strip().startswith("docs/validation/")
    ]
    assert not watched, (
        "these files are not known to git, so a commit would not contain them — the fix or "
        "the evidence ships without them:\n  " + "\n  ".join(watched) + "\n"
        "Run `git add -N <path>` to register them."
    )


#: Paths a `.gitignore` rule hides on purpose, each with the reason.
#:
#: `screens/` holds raw UI captures straight from the browser, and some carry client names
#: and phone numbers. The rule is right; what was wrong is that nothing *knew* about it, so
#: thirteen pieces of cited evidence were invisible to the gate above while
#: `docs/validation/09-runtime.md` cited them as proof. Committed, redacted counterparts live
#: under `docs/validation/evidence/`.
_DELIBERATELY_IGNORED = ("docs/validation/screens/",)


def test_no_source_or_evidence_file_is_swallowed_by_a_gitignore_rule() -> None:
    """The other half of "known to git", and the half that was structurally invisible.

    `git ls-files --others --exclude-standard` applies `.gitignore`, so the check above
    cannot see a file an ignore rule hides — which is the one case where a file silently
    ships as nothing at all. In this repo that blind spot is already occupied: thirteen
    files under `docs/validation/screens/` are ignored, and the phase-09 log cites them as
    its step-3 UI evidence.

    So an ignored path that matches the watched set must be an explicit decision in
    `_DELIBERATELY_IGNORED`, not an accident of a pattern someone widened.
    """
    import subprocess

    root = TESTS.parent
    ignored = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
        capture_output=True, text=True, cwd=root,
    )
    assert ignored.returncode == 0, f"git ls-files failed: {ignored.stderr}"

    hidden = [
        line.strip() for line in ignored.stdout.split("\n")
        if (line.strip().endswith(".py") or line.strip().startswith("docs/validation/"))
        and not line.strip().startswith(_DELIBERATELY_IGNORED)
    ]
    assert not hidden, (
        "a .gitignore rule is hiding these source/evidence files, so they ship as nothing "
        "and no other check can see them:\n  " + "\n  ".join(hidden) + "\n"
        "Either narrow the ignore rule, or add the path to _DELIBERATELY_IGNORED with the "
        "reason it must stay out of the repo."
    )


def test_the_ignored_allowlist_still_describes_something_real() -> None:
    """A stale exemption hides the next real one.

    If `screens/` ever stops being ignored — or stops existing — this exemption is no longer
    a decision, it is a leftover, and leftovers are how an allowlist quietly grows to cover
    a defect nobody chose.
    """
    import subprocess

    root = TESTS.parent
    ignored = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
        capture_output=True, text=True, cwd=root,
    ).stdout
    for prefix in _DELIBERATELY_IGNORED:
        assert prefix in ignored, (
            f"_DELIBERATELY_IGNORED names {prefix!r}, but nothing ignored matches it. "
            "Remove the entry — a stale exemption is a hole nobody is watching."
        )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} suite-integrity tests passed.")
