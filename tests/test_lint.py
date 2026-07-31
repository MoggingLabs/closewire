"""Gate: the linter that was always declared actually runs.

`ruff>=0.5` has been in `[project.optional-dependencies].dev` since phase 01. It was never
installed and never run. The cost of that showed up in phase 09: nine council rounds hunted
dead imports by reading diffs, three were removed by hand in round 9, a critic found a fourth
in round 10, and this log argued — wrongly — that no gate could ship for the class because a
hand-rolled AST scan would false-positive.

That argument was backwards. The hand-rolled scan produced roughly a hundred false positives
for one true finding. `ruff --select F` produced one finding and no false positives, which is
the whole reason linters exist. The operator's standing rule is that every fix ships a gate
that bites; the correct gate here was never bespoke code, it was running the tool already in
the dependency list.

Scoped to `F` (pyflakes: unused imports and locals, undefined names, f-string mistakes) and
`E9` (syntax errors) via `[tool.ruff.lint]` in `pyproject.toml`. Style rules are excluded
deliberately — see the comment there.

A missing `ruff` is a **failure**, not a skip. A gate that silently disappears when its tool
is absent is the "check that cannot fail" shape this suite has now been blocked on twice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_is_installed() -> None:
    """`ruff` is a declared dev dependency. If it is missing, the gate below is vacuous."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, (
        "ruff is not installed, so the lint gate cannot run and would pass silently. It is "
        "declared in pyproject.toml's dev extra: `pip install -e .[dev]`.\n" + result.stderr
    )


def test_the_repo_is_clean_under_ruff() -> None:
    """The gate. Rule selection lives in `pyproject.toml`, not here, so there is one source.

    Reported with ruff's own output, because "the repo does not lint" is not actionable and
    ruff's message already names the file, line and fix.
    """
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "."],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, (
        "ruff found problems. Run `python -m ruff check --fix .` and re-read what it "
        "could not fix:\n" + (result.stdout or result.stderr)
    )


def test_the_selected_rules_include_the_class_this_gate_exists_for() -> None:
    """Self-check: a rule set narrowed to nothing would leave the gate above always green.

    Phase 09 shipped two gates whose sentinels matched nothing and a third whose heuristic
    fired on 41% of its input. The lesson is asserted rather than remembered: prove `F401`
    (unused import) is actually enabled, by linting a file that contains one.
    """
    probe = "import os\nimport sys\n\nprint(sys.version)\n"
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "-"],
        input=probe, capture_output=True, text=True, cwd=ROOT,
    )
    assert "F401" in (result.stdout + result.stderr), (
        "the configured rule set no longer flags an unused import, so the lint gate can no "
        "longer catch the class it was added for. Check `[tool.ruff.lint].select` in "
        f"pyproject.toml.\nruff said: {result.stdout or result.stderr!r}"
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} lint gate tests passed.")
