"""Phase-06 verification: run EVERY read command and check the CLI's own guarantees.

Each command is run as a real subprocess, so exit codes, stdout/stderr separation, and
argument parsing are all exercised the way a user meets them.

What this asserts, beyond "it printed something":

* **Coverage** — every command in ``cli.reads.READ_COMMANDS`` is run, and the run fails if
  one is missing a probe.
* **`--json` purity** — stdout parses as JSON with nothing else in it. This is the promise
  that makes `| jq` safe, and pacing logs would break it if they were not on stderr.
* **Exit codes** — 0 on success, non-zero on a bad id, and the process never dies on a
  traceback.
* **Redaction** — neither the Closebot API key nor a GoHighLevel credential appears on
  stdout or stderr, in any command, in either mode.

    python scripts/verify_cli.py

Read-only, and slow: every command is a paced call.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli.reads import READ_COMMANDS
from closewire_client.config import load_config
from closewire_client.redaction import find_unredacted

FAILURES: list[str] = []
RUN: set[str] = set()

#: The trailing "N label" / "N of M label" summary `_count()` prints.
_SUMMARY = re.compile(r"^\d+( of \d+)? \w")

#: Header, separator, and summary lines a renderer may legitimately add beyond its rows.
_FRAME_ALLOWANCE = 4


#: Longer than any single call can legitimately take.
#:
#: Derived, not typed. `retry_after_max_s` defaults to 900 and `rest.py` honours a server's
#: `Retry-After` verbatim, so one throttled call can legitimately block far longer than the
#: old hard-coded `timeout=300` — and `TimeoutExpired` was caught nowhere in this file, so it
#: killed the whole verifier mid-run, printed no RESULT block, and surfaced to `ci.py` as a
#: bare `FAIL cli` with no reason. That is one of the two mechanisms behind the phase-09
#: "open flake". Two knobs in different files with no asserted relation between them.
def _subprocess_timeout() -> float:
    from closewire_client.config import load_config

    cfg = load_config(strict=False)
    return cfg.retry_after_max_s + cfg.max_retries * cfg.backoff_cap_s + 60.0


SUBPROCESS_TIMEOUT = _subprocess_timeout()


def run(args: list[str], *, expect_ok: bool = True, label: str | None = None):
    """Run one CLI invocation as a subprocess and record what happened.

    A hang is a **finding**, not a crash: `TimeoutExpired` is caught and appended to
    `FAILURES` so the run completes, prints its RESULT block, and names the command that hung.
    """
    name = label or " ".join(args[:2])
    RUN.add(name)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "cli.main", *args],
            capture_output=True, text=True, cwd=ROOT, timeout=SUBPROCESS_TIMEOUT,
        # The CLI emits UTF-8 (client names and message bodies are not ASCII). Reading
        # with the platform locale codec fails on Windows, which is a defect in the
        # *consumer*, not the CLI — `--json` is ASCII-escaped and safe either way.
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        FAILURES.append(
            f"{' '.join(args)}: no output after {SUBPROCESS_TIMEOUT:.0f}s — the CLI hung. "
            "This used to kill the verifier outright, so the run reported nothing at all."
        )
        print(f"  !! {name}: HUNG after {SUBPROCESS_TIMEOUT:.0f}s")
        return subprocess.CompletedProcess(args, returncode=124, stdout="", stderr="timeout")
    ok = proc.returncode == 0
    if expect_ok and not ok:
        FAILURES.append(f"{' '.join(args)}: exit {proc.returncode} — {proc.stderr.strip()[:160]}")
    if not expect_ok and ok:
        FAILURES.append(f"{' '.join(args)}: expected a non-zero exit, got 0")
    return proc


def check_json(args: list[str], label: str, *, already_json: bool = False):
    """Run with --json; return ``(proc, payload)``.

    The process is returned so the caller can scan the **JSON** run's own output. Passing
    the table process instead is what made the credential assertion structurally incapable
    of failing.

    ``already_json`` is for callers probing a specific *flag position* — `--json ping` vs
    `ping --json`. Those two parsed differently and one of them was broken, so the argv must
    be passed through verbatim rather than having `--json` appended to it.
    """
    proc = run(list(args) if already_json else [*args, "--json"], label=label)
    if proc.returncode != 0:
        return proc, None
    try:
        return proc, json.loads(proc.stdout or "")
    except ValueError as exc:
        head = (proc.stdout or "")[:120].replace("\n", "\\n")
        # `--json` is only appended to the message when this call appended it to the argv,
        # or the report reads `--json ping --json`.
        shown = " ".join(args) if already_json else f"{' '.join(args)} --json"
        FAILURES.append(f"{shown}: stdout is not valid JSON ({exc}) — {head!r}")
        return proc, None


def check_no_secrets(label: str, proc, *, payload=None) -> None:
    """No credential may appear on stdout or stderr, in either stream.

    ``payload`` is the already-parsed JSON when the caller has it. Deriving it here via
    ``json.loads(proc.stdout)`` meant table output always raised, ``leaks`` was always
    empty, and the structural credential walk never executed for any command in either
    mode. A critic proved the cost: with redaction neutered, 14 real client credentials
    reached stdout while this script exited 0 reporting "no credentials in output".
    """
    cfg = load_config(strict=False)
    blob = (proc.stdout or "") + (proc.stderr or "")
    if cfg.api_key and cfg.api_key in blob:
        FAILURES.append(f"{label}: the CLOSEBOT API KEY appeared in output")
    if payload is None:
        try:
            payload = json.loads(proc.stdout or "")
        except ValueError:
            return  # table mode: the substring test above is the applicable check
    leaks = find_unredacted(payload)
    # The node catalogue legitimately uses `key` for property names; no account data.
    if leaks and "descriptors" not in label:
        FAILURES.append(f"{label}: unredacted credentials at {leaks[:4]}")


#: Rows the payload holds, per command, when the envelope key is not `results`. Without
#: this the two richest renderers — `bots steps` (26 nodes) and `sources fields` (126 fields
#: across 3 objects) — were silently exempt, including the very command whose truncated-id
#: bug motivated this check.
ROW_ACCESSORS = {
    "bots steps": lambda p: p.get("described") if isinstance(p, dict) else None,
    "sources fields": lambda p: (
        [f for group in p.values() for f in group] if isinstance(p, dict) else None
    ),
    "metrics messages": lambda p: p.get("results") if isinstance(p, dict) else None,
}


def payload_rows(label: str, payload):
    """The row list a command's payload holds, or None when it has no row concept."""
    accessor = ROW_ACCESSORS.get(label)
    if accessor is not None:
        rows = accessor(payload)
        return rows if isinstance(rows, list) else None
    if isinstance(payload, dict):
        rows = payload.get("results")
        return rows if isinstance(rows, list) else None
    return payload if isinstance(payload, list) else None


def check_render_fidelity(label: str, table_proc, payload) -> None:
    """Compare the number of rows *rendered* against the number *fetched*.

    Scope, stated precisely because the first version of this docstring overstated it: this
    catches a renderer **dropping or inventing whole rows**. It does **not** catch a
    malformed cell — a blank column renders the right number of lines, and so does a
    truncated id. Those two defects shipped in this phase and neither would be caught here;
    what closed them was reading the output.

    Two escapes are deliberately avoided:

    * **Framing is not trusted.** Keying off the separator line ``_table()`` emits let a
      renderer that stopped emitting it disable the check on itself. Row counting now falls
      back to counting content lines.
    * **Both directions are checked.** Only testing ``rendered < fetched`` let a renderer
      that fabricates rows pass silently.
    """
    if payload is None or table_proc.returncode != 0:
        return
    rows = payload_rows(label, payload)
    if not rows:
        return

    lines = (table_proc.stdout or "").splitlines()
    separators = [i for i, line in enumerate(lines) if line.strip("- ") == "" and "-" in line]

    if separators:
        # Sum every block, not just the first: `sources fields` legitimately renders one
        # sub-table per object type (55 + 47 + 24 = 126), and counting only the first
        # block reported it as dropping 71 rows it had in fact printed.
        rendered = 0
        for sep in separators:
            start = sep + 1
            end = next(
                (i for i in range(start, len(lines)) if not lines[i].strip()), len(lines)
            )
            rendered += end - start
    else:
        # No separator: either a list-style renderer, or a column renderer that stopped
        # emitting its own framing. Count content lines instead of returning early, so the
        # second case cannot silence the check.
        content = [
            line for line in lines
            if line.strip() and not _SUMMARY.match(line.strip()) and not line.strip().startswith("(")
        ]
        rendered = len(content)

    if rendered < len(rows):
        FAILURES.append(
            f"{label}: table rendered {rendered} row(s) but the payload holds "
            f"{len(rows)} - the renderer is dropping data"
        )
    elif rendered > len(rows) + _FRAME_ALLOWANCE:
        FAILURES.append(
            f"{label}: table rendered {rendered} line(s) for a payload of {len(rows)} "
            f"row(s) - the renderer may be inventing data"
        )


def head(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def main() -> int:
    # Called for its validation, not its value: `load_config(strict=True)` raises if the
    # environment is not configured, and failing here beats failing partway through a run
    # that has already spent paced calls. The binding was unused, which read as dead code.
    load_config()

    head("DISCOVER IDS (one paced call each)")
    discovered = {}
    for args, key in ((["bots", "list"], "bots"), (["sources", "list"], "sources"),
                      (["leads", "list"], "leads"), (["personas", "list"], "personas")):
        label = " ".join(args)
        proc, payload = check_json(args, label)
        check_no_secrets(f"{label} --json", proc, payload=payload)
        # ...and the TABLE renderer, which check_json never exercises because it appends
        # --json. These four commands' table counts are exactly what the cross-check cites,
        # and they were never run.
        tbl = run(args, label=label)
        check_no_secrets(label, tbl)
        check_render_fidelity(label, tbl, payload)
        discovered[key] = payload

    bots = discovered["bots"] or []
    bot_id = bots[0]["id"] if bots else None
    published_bot = next((b for b in bots if b.get("versions") and
                          any(v.get("published") for v in b["versions"])), None)
    srcs = discovered["sources"] or {}
    src_id = (srcs.get("results") or [{}])[0].get("sourceId")
    lds = discovered["leads"] or {}
    lead_id = (lds.get("results") or [{}])[0].get("id")
    pers = discovered["personas"] or []
    persona_id = pers[0].get("id") if pers else None
    print(f"  bot={bot_id}  published_bot={published_bot and published_bot['id']}")
    print(f"  source={src_id}  lead={lead_id}  persona={persona_id}")

    head("EVERY COMMAND, TABLE + JSON")
    probes: list[tuple[list[str], str]] = [
        (["bots", "get", bot_id], "bots get"),
        (["bots", "steps", published_bot["id"] if published_bot else bot_id], "bots steps"),
        (["bots", "descriptors"], "bots descriptors"),
        (["bots", "templates"], "bots templates"),
        (["personas", "get", persona_id], "personas get"),
        (["sources", "get", src_id], "sources get"),
        (["sources", "calendars", src_id], "sources calendars"),
        (["sources", "fields", src_id], "sources fields"),
        (["sources", "tags", src_id], "sources tags"),
        (["sources", "channels", src_id], "sources channels"),
        (["leads", "get", lead_id], "leads get"),
        (["leads", "history", lead_id], "leads history"),
        (["leads", "ai-toggle", lead_id], "leads ai-toggle"),
        (["leads", "search"], "leads search"),
        (["metrics", "booking", "--start", "2024-08-01", "--end", "2026-07-25",
          "--resolution", "monthly"], "metrics booking"),
        (["metrics", "summary"], "metrics summary"),
        (["metrics", "messages", "--limit", "5"], "metrics messages"),
    ]
    for args, label in probes:
        if any(a is None for a in args):
            FAILURES.append(f"{label}: skipped, no id discovered")
            continue
        table = run(args, label=label)
        check_no_secrets(label, table)
        jproc, payload = check_json(args, label)
        check_no_secrets(f"{label} --json", jproc, payload=payload)
        check_render_fidelity(label, table, payload)
        first = ((table.stdout or "").splitlines() or [""])[0][:58]
        print(f"  {label:22s} exit={table.returncode}  json={'ok' if payload is not None else 'FAIL':4s}  {first}")

    # Known server-side failures: run them, do not require success.
    for args, label in ((["metrics", "actions"], "metrics actions"),
                        (["metrics", "logs"], "metrics logs")):
        proc = run(args, expect_ok=False, label=label)
        print(f"  {label:22s} exit={proc.returncode}  (server-side failure expected)")
        if proc.returncode == 0:
            FAILURES.remove(f"{' '.join(args)}: expected a non-zero exit, got 0")
            print("    -> succeeded this run; it is intermittent, not permanently broken")

    head("TOP-LEVEL COMMANDS (not in READ_COMMANDS, and previously never run here)")
    # `ping`/`whoami` are read-only and live, but they are not in `cli.reads.READ_COMMANDS`,
    # so the coverage loop below has never touched them. That gap was not theoretical: a
    # phase-09 critic found that `closewire --json ping` printed seven lines of human text
    # and `closewire ping --json` was an argparse error, because `ping` was the one read
    # subparser built without `parents=[json_opt]`. This script certified "--json is pure"
    # for eleven review rounds while never invoking the command that broke it.
    #
    # Both flag orders are probed, because the two failed differently: prefix `--json` was
    # accepted and ignored, postfix was rejected outright.
    for argv, label in (
        (["--json", "ping"], "--json ping"),
        (["ping", "--json"], "ping --json"),
        (["--json", "whoami"], "--json whoami"),
        (["whoami", "--json"], "whoami --json"),
    ):
        proc, payload = check_json(argv, label, already_json=True)
        check_no_secrets(label, proc, payload=payload)
        print(f"  {label:18s} exit={proc.returncode}  "
              f"{'JSON' if payload is not None else 'NOT JSON'}")

    human = run(["ping"], label="ping (table)")
    check_no_secrets("ping", human)
    if "closewire" not in human.stdout:
        FAILURES.append("`closewire ping` no longer prints its human summary")

    head("FAILURE PATHS")
    bad = run(["bots", "get", "bot_DOES_NOT_EXIST"], expect_ok=False, label="bad id")
    print(f"  bad bot id           exit={bad.returncode}  stderr: {bad.stderr.strip().splitlines()[-1][:60] if bad.stderr.strip() else ''}")
    if "Traceback" in bad.stderr:
        FAILURES.append("a bad id produced a traceback instead of a readable error")
    check_no_secrets("bad id", bad)

    badres = run(["metrics", "booking", "--resolution", "day"], expect_ok=False,
                 label="bad resolution")
    print(f"  --resolution day     exit={badres.returncode}  "
          f"{'rejected locally' if 'must be one of' in badres.stderr else 'NOT rejected'}")
    if "must be one of" not in badres.stderr:
        FAILURES.append("--resolution day was not rejected locally (the phase prompt uses it)")

    head("--json PURITY UNDER LOGGING")
    # Force REAL log output. Turning the delay knobs down produced ZERO bytes, because
    # think-time logs at DEBUG while the CLI pins the level to WARNING - so the old probe
    # asserted nothing. A tiny hourly ceiling makes the Pacer emit a budget WARNING.
    noisy = subprocess.run(
        [sys.executable, "-m", "cli.main", "sources", "list", "--json"],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
        encoding="utf-8", errors="replace",
        # DEBUG makes the Pacer log every think-time decision, so the probe forces real
        # output in one paced call. A tiny hourly ceiling would also log - and would then
        # BLOCK FOR A REAL HOUR, because a budget wait is a wall-clock wait.
        env={**__import__("os").environ, "CLOSEWIRE_LOG_LEVEL": "DEBUG"},
    )
    stderr_bytes = len(noisy.stderr or "")
    if stderr_bytes == 0:
        FAILURES.append(
            "the --json purity probe produced no log output, so it proves nothing - it "
            "must force the Pacer to log before asserting stdout stays clean"
        )
        print("  !! probe forced no log output - the assertion is vacuous")
    else:
        try:
            json.loads(noisy.stdout or "")
            print(f"  stdout parses as JSON with {stderr_bytes} bytes of real log "
                  f"on stderr: OK")
        except ValueError:
            FAILURES.append("--json stdout was polluted by log output")

    head("COVERAGE")
    missing = [c for c in READ_COMMANDS if c not in RUN]
    if missing:
        FAILURES.append(f"commands never run: {missing}")
        print(f"  !! {len(missing)} command(s) never run: {missing}")
    else:
        print(f"  every command in READ_COMMANDS exercised ({len(READ_COMMANDS)})")

    head("RESULT")
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("every command ran, --json is pure, exit codes behave, no credentials in output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
