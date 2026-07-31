"""The one command that validates this repo. CI runs it; so does every council critic.

Eleven review rounds in phase 09 established that ad-hoc verification does not hold. Critics
ran different subsets, each in their own order, and things slipped between them: `ruff` sat
declared-but-never-run for eight phases, a new gate file shipped untracked because nothing
listed it, and a suite total in the log disagreed with the suite for a full round. None of
those were hard to catch. They were missed because there was no single thing to run.

So there is one now, and it is the same one everywhere:

    python scripts/ci.py            # offline — no credentials, no network, no spend
    python scripts/ci.py --live     # adds the read-only live checks (GETs only)

**Offline is the default and is what GitHub Actions runs**, because CI has no API key and
because a check that needs one is a check contributors cannot run. Everything in the offline
tier is hermetic: `verify_writes` and `verify_tier2` drive an ``httpx.MockTransport`` that
raises on any request at all, so a single escaping byte fails the run.

`--live` adds `verify_reads` and `verify_cli`. Both are **read-only GETs**. Neither sends a
message, creates, publishes or deletes anything, and neither can spend a credit. They are
excluded from the default tier because they are slow, need a key, and consume paced budget —
not because they are dangerous.

Nothing here ever sends a live message. The runtime probe (`scripts/probe_runtime_auth.py`)
is deliberately *not* wired in: it posts to the credit-spending endpoint, and a validation
command you run on every commit must never be able to do that.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (label, argv, why). Order matters: cheapest and most-diagnostic first, so a syntax error
#: reports as a syntax error rather than as 40 test failures.
OFFLINE: list[tuple[str, list[str], str]] = [
    ("lint", [sys.executable, "-m", "ruff", "check", "."],
     "pyflakes + syntax errors; the rule set lives in pyproject.toml"),
    ("tests", [sys.executable, "-m", "pytest", "tests/", "-q"],
     "the suite, including the gates on the suite and on the validation logs"),
    ("runners", [sys.executable, "scripts/verify_runners.py"],
     "every test file also runs as `python tests/test_x.py` — the second path it promises, "
     "which fifteen docstrings claimed and nothing ever executed"),
    ("writes", [sys.executable, "scripts/verify_writes.py"],
     "phase 07: every mutation dry-runs without a byte reaching the wire"),
    ("tier2", [sys.executable, "scripts/verify_tier2.py"],
     "phase 08: publish/destroy/spend refuse without confirmation"),
    ("tiers", [sys.executable, "-m", "closewire_client.tiers"],
     "the Tier-2 import block, re-derived from schema/"),
]

#: Read-only and live. GETs only — see the module docstring.
LIVE: list[tuple[str, list[str], str]] = [
    ("reads", [sys.executable, "scripts/verify_reads.py"],
     "phase 05: every Tier-0 read against the live account, no credentials in output"),
    ("cli", [sys.executable, "scripts/verify_cli.py"],
     "phase 06: every read command routes, and --json stays pure"),
]


#: Where each check's output is kept. Gitignored (`.closewire/` already is).
LOG_DIR = ROOT / ".closewire" / "ci"


def run(label: str, argv: list[str], why: str) -> tuple[bool, float]:
    """Run one check, streaming its output to the console **and** to a file.

    The tee is the fix for a real incident. `ci.py --live` reported `FAIL cli` once in three
    runs; `verify_cli.py` prints its whole `FAILURES` list before exiting, and that text —
    the only thing that could have named the cause — went to a terminal scrollback and was
    lost, because this function used to call `subprocess.run(argv, cwd=ROOT)` with no capture.
    The incident was written up as an "open flake", which is what you are left with when the
    evidence is discarded by the harness.

    `PYTHONUNBUFFERED` matters as much as the file: with stdout piped, a child buffers by
    block, so a long check emits nothing for minutes and then everything at exit — and a
    killed or interrupted child emits nothing at all.
    """
    print(f"\n{'=' * 72}\n  {label}  —  {why}\n{'=' * 72}", flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{label}.log"
    started = time.monotonic()
    with subprocess.Popen(
        argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
    ) as process, log_path.open("w", encoding="utf-8", errors="replace") as sink:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            sink.write(line)
    elapsed = time.monotonic() - started
    ok = process.returncode == 0
    print(f"  -> {'PASS' if ok else 'FAIL'} ({elapsed:.1f}s)  [{log_path.relative_to(ROOT)}]",
          flush=True)
    return ok, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live", action="store_true",
        help="also run the read-only live checks (needs CLOSEBOT_API_KEY; GETs only)",
    )
    args = parser.parse_args()

    checks = list(OFFLINE) + (list(LIVE) if args.live else [])
    results: list[tuple[str, bool, float]] = []
    for label, argv, why in checks:
        ok, elapsed = run(label, argv, why)
        results.append((label, ok, elapsed))

    print(f"\n{'=' * 72}\n  SUMMARY\n{'=' * 72}")
    for label, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL':4s}  {label:8s} {elapsed:6.1f}s")
    failed = [label for label, ok, _ in results if not ok]
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        # Echo the tail of each failing check's log. A failure that does not reproduce on the
        # next run must still leave something to read — that is exactly what the phase-09
        # "open flake" needed and did not have.
        for label in failed:
            log_path = LOG_DIR / f"{label}.log"
            if not log_path.exists():
                continue
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            print(f"\n--- last 40 lines of {log_path.relative_to(ROOT)} ---")
            for line in tail:
                print(f"  {line}")
        return 1
    tier = "offline + live read-only" if args.live else "offline"
    print(f"\nALL CHECKS PASSED ({tier}, {len(results)} checks)")
    if not args.live:
        print("Live read-only checks were NOT run. Use --live to include them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
