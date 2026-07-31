"""Every test file must also run as `python tests/test_x.py` — the second path it promises.

Every test file carries an `if __name__ == "__main__":` block that sweeps `globals()` for
`test_*` and runs them. `tests/test_suite_integrity.py` exists to protect that block and
protects it *statically*: no test defined below the runner, and the runner sweeps `globals()`
rather than a hand-kept list. Neither property implies the runner **works**.

Nothing ever ran it. `scripts/ci.py` runs `pytest tests/ -q` and stops there, so the second
execution path was a promise made in every one of their docstrings and kept by nobody. (An earlier version of
this paragraph said "fifteen" and was stale within the round that wrote it — three test files
landed alongside it. The script globs, so the count was never load-bearing; it is simply not
stated now, for the same reason no other count in this repo is transcribed twice.) A single test
that takes a pytest fixture is invisible to every existing gate and fatal to the runner:

    TypeError: test_needs_a_fixture() missing 1 required positional argument: 'tmp_path'

pytest collects it, the static gate is green, and `python tests/test_jobflow.py` dies.

So this runs them. Two assertions per file: it exits 0, and the number of `[PASS]` lines it
prints equals the number of tests pytest collects from that file — because a runner that
silently runs a subset is the original defect (`test_suite_integrity.py`'s whole history) in
a form the static check cannot see either.

    python scripts/verify_runners.py

Offline, no credentials, no network — the same tier as `pytest`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: `[PASS] name` — what every runner in this repo prints per test.
_PASS = re.compile(r"^\s*\[PASS\]\s+(\S+)", re.MULTILINE)


def _collected(path: Path) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(path), "-q", "--collect-only"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0:
        return -1
    return sum(1 for line in result.stdout.splitlines() if "::" in line)


def main() -> int:
    files = sorted(TESTS.glob("test_*.py"))
    if not files:
        print("no test files found — this check has nothing to assert")
        return 1

    problems: list[str] = []
    total = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        if "__main__" not in source:
            # No runner declared, so nothing is promised. `test_suite_integrity` already
            # allows this; it only constrains files that *do* declare one.
            print(f"  {path.name:34s} (no runner — nothing promised)")
            continue

        run = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                             cwd=ROOT)
        ran = len(_PASS.findall(run.stdout))
        collected = _collected(path)
        total += ran

        if run.returncode != 0:
            tail = (run.stderr or run.stdout).strip().splitlines()[-3:]
            problems.append(f"{path.name}: `python tests/{path.name}` exited "
                            f"{run.returncode}\n      " + "\n      ".join(tail))
            print(f"  {path.name:34s} FAILED (exit {run.returncode})")
            continue
        if collected < 0:
            problems.append(f"{path.name}: pytest could not collect it")
            continue
        if ran != collected:
            problems.append(f"{path.name}: the runner reported {ran} tests, pytest collects "
                            f"{collected} — the runner is running a subset")
            print(f"  {path.name:34s} MISMATCH ({ran} vs {collected})")
            continue
        print(f"  {path.name:34s} {ran:3d} tests, matches pytest")

    print()
    if problems:
        print("FAILURES:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"every runner works and reports the full set ({total} tests across "
          f"{len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
