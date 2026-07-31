---
phase: 04
status: closed
---
# Phase 04 · Pacing layer — validation log

**Date:** 2026-07-25
**Verdict:** **council cleared — round 3 passed 5/5.** Post-council fixes applied (see
below) for defects the panel raised as non-blocking but which were real, including one
regression introduced during round 3. **Phase 05 remains blocked on a real API key.**

**Council rule:** all 5 critics must pass. Changed from 4-of-5 to unanimous on 2026-07-25,
mid-build — see the rationale in `prompts/00-README.md`. Round 2 finished 4 PASS / 1
BLOCK, which the old rule would have advanced with a defect that silently recreated a
round-1 blocker. The change paid for itself immediately.

| Round | Result |
| :-- | :-- |
| 1 | **5 BLOCK / 0 PASS** — nine defects, all independently reproduced |
| 2 | 4 PASS / **1 BLOCK** — the no-bypass regression test was order-dependent |
| 3 | **5 PASS** — 24 of 39 mutations killed; every rubric-cited behaviour pinned |

---

## Blocked (needs the maintainer)

Two hard stops were hit during the phase-04 preflight. Both halt the whole 04→13 run.

| # | Blocker | Evidence | Status |
| :-- | :-- | :-- | :-- |
| 1 | **Chrome was not logged into Closebot.** A request to `https://app.closebot.com/` redirected to `/login?redirect_url=...`. | `screens/04-blocked-login.jpg` (the sign-in form); the URL comes from the navigation tool's reported address, not the image — page captures have no address bar | **CLEARED** — the maintainer logged in mid-session; `screens/04-dashboard-authenticated.jpg` |
| 2 | **`.env` contains the placeholder API key**, not a real one — the literal `.env.example` value (32 chars, ends `...here`). Every live call 401s. | `closewire ping` → HTTP 401 on `GET /agency/current` | **OPEN.** Put a real key in `.env` (`CLOSEBOT_API_KEY=`). Claude will not read the key out of the UI — that is the maintainer's to paste in. |

A sign-in via SSO was observed part-way through the session (`/finish-sso-callback`,
"Warming up the conversation engines…") but it did not complete — the tab returned to
`/login`. Three browser attempts were made, then stopped per the run's hard-stop rule.

Blocker 2 also means phases 05–13 cannot start at all: every one is defined against a
live account.

> Note on phase 03: its "live `ping`/whoami" is recorded as complete, but with the current
> `.env` it cannot be reproduced. Either the key was rotated out after phase 03, or that
> phase's live check was never run. Worth confirming at sign-off, since phase 03 is the
> only prior evidence that `X-CB-KEY` is the correct auth style.

---

## Review council — round 1 (5 critics, all BLOCK)

Five independent Opus 5 critics reviewed the first draft against this phase's
deliverables. **Verdict: 0 of 5 passed** (the advance rule needs 4 of 5). Every objection
below was reproduced by its critic before being filed; all have been fixed.

| # | Blocking defect | Fix |
| :-- | :-- | :-- |
| 1 | **Budget ceiling breachable (TOCTOU).** The window check and the record were separate critical sections with a 1–4s think-time between them, so every concurrent reader passed the same free slot. Reproduced end-to-end: **6 requests on the wire against a ceiling of 4, zero budget waits.** | `_reserve()` now claims the slot *inside the same critical section that finds room*. Regression test: `test_op_ceiling_holds_under_concurrent_readers` (12 threads, ceiling 4 → exactly 4 granted). |
| 2 | **`Session` was an unpaced, authenticated network primitive.** Its `pacer` argument was stored and never read. Reproduced: 20 authenticated POSTs in 0.0078s with `ops_last_hour=0` — and the same Session reached the *live-message* host by absolute URL. | `Pacer` now tracks slot ownership per thread; `Session.request` raises `PacingBypassError` unless the caller holds one. `LiveMessageClient` takes a `Pacer` so phase 09 has a paced anchor. Regression: `test_session_refuses_to_send_outside_a_pacing_slot`. |
| 3 | **Breaker did not stop calls already inside the pacer.** It was checked before the blocking waits but not after, so anything parked in the budget or think-time wait was still released. Reproduced: **3 requests reached the transport 1.7s after `breaker_state == "open"`** — a window that stretches to a full hour behind a budget wait. | `acquire()` re-checks the breaker immediately before yielding. Regression: `test_breaker_stops_a_call_already_inside_the_pacer`. |
| 4 | **`Retry-After` silently truncated** to the 60s cap — a server asking for 3600s was retried after 60. A test *named* `..._honors_retry_after` asserted the truncation as correct. | An explicit `Retry-After` is now obeyed as given. One beyond `CLOSEWIRE_RETRY_AFTER_MAX_S` (default 900s) stops the retry loop and surfaces, rather than coming back early. HTTP-date form is now parsed too. Regressions: `test_retry_after_is_honored_not_truncated`, `..._beyond_the_limit_surfaces...`, `test_rest_parses_http_date_retry_after`. |
| 5 | **`POST /lead/search` was dry-run-suppressed.** A Tier-0 read phase 05 must ship was classified as a write by verb, charged to the write budget, and under dry-run returned a fabricated `{"dry_run": true}` instead of lead data. | `request()` takes an explicit `write=` override; `_is_write` now treats only GET/HEAD/OPTIONS as reads so unknown verbs fail toward the stricter lane. Regressions: `test_search_style_post_can_be_marked_a_read`, `test_unknown_verbs_default_to_the_write_lane`. |
| 6 | **Clock/sleeper mismatch spun forever.** `Pacer(cfg, sleeper=lambda _: None)` on the real clock made any budget wait an unbounded CPU spin emitting ~1.9 MB of log in 3s — and phase 04 had shipped exactly that construction as the recommended test helper. | The wait detects a clock that does not advance and raises with an actionable message; a round backstop bounds the loop. `tests/test_transport.py` now uses a coupled fake clock. Regression: `test_mismatched_clock_and_sleeper_raises_instead_of_spinning`. |
| 7 | **`closewire pacing-reset` did not exist**, yet the halt message told the operator to run it; `PacerStats` reached neither CLI nor MCP despite deliverable 7. | Added `closewire pacing-status` (with `--json`) and `closewire pacing-reset`. A tripped breaker is now persisted to `CLOSEWIRE_STATE_DIR`, so the halt survives a restart and there is something for the reset to clear. Previously a re-run resumed hammering a revoked key immediately. |
| 8 | **Dry-run's "logs it" was untested and invisible.** It was `log.info` with no handler configured anywhere, so a real dry-run POST printed nothing; deleting the log line left all tests green. | Raised to `log.warning` (visible via logging's lastResort floor) and asserted, including its level. |
| 9 | **This log cited evidence it did not have.** The screenshot was filed as `.png` when the file was `.jpg`, and the image was the SSO loading splash — no login form, no address bar — yet the log leaned on it for "redirects to /login" and "host confirmed". | Re-captured the actual sign-in page; corrected the filename; the log and `config.py` were narrowed to say the URL comes from the navigation tool. (The authenticated dashboard was subsequently reached — see *UI validation* below — and both were updated again.) |

Also fixed from the non-blocking pile: `Config.api_key` had no `repr=False` and printed
in full in any traceback; `CLOSEWIRE_DRY_RUN=y` silently meant *false* (a safety flag
failing open on a typo — now an error); `budget_waits` double-counted; `current_backoff`
was never cleared; only 2 of ~13 knobs were validated (`write_delay_mult=0.1` was accepted
and made writes 100x *faster* than reads); a large `max_retries` overflowed; and the
scaffold-era half-pacing API (`wait`/`note_op`/…) was dead code inviting exactly the
partial pacing this phase forbids — deleted.

Two council claims were checked and **not** adopted: budget state being per-process is
inherent to deliverable 7's "in-memory" wording (now surfaced explicitly in
`pacing_status` output rather than changed), and `live.py` being unimplemented is phase-09
work, not a phase-04 defect.

---

## Review council — round 2 (4 PASS, 1 BLOCK)

The same five critics were resumed with their context intact, so each verified whether its
*own* round-1 objection was genuinely fixed rather than meeting the code fresh. All
round-1 blocking defects were confirmed closed by the critic that raised them — the
budget ceiling held under a 40-thread hammer (was 6-through-a-ceiling-of-4), the
20-unpaced-POST attack yielded zero requests and 20 `PacingBypassError`s, the
breaker-escape probe put zero requests on the wire (was 3, at 1.7s after OPEN), and
`Retry-After` measured exact to the second at 30/60/120/300/900s.

**Critic 4 blocked**, and was right. Its finding and the fixes:

| # | Defect | Fix |
| :-- | :-- | :-- |
| 1 | **BLOCKING — the no-bypass regression test was order-dependent.** It attempted the unpaced send *before* any slot existed, so it could only detect a mark that was never **set**, never one that was never **cleared**. Mutation N12 (neuter `_exit()`) left all 43 tests green while putting **20 unpaced authenticated POSTs on the wire against `ops_last_hour=1`** — silently recreating round-1 defect #2. | The test now asserts both directions. Beyond the one-assertion fix, a slot is now a **one-shot token**: `assert_in_slot` consumes it, so reusing one slot for a second send raises. Verified: mutation N12 is now KILLED. |
| 2 | `_exit()` ran on paths where `_enter()` never did, so a failed inner acquire decremented the **outer** thread's mark and killed a legitimately paced call. Found independently by critics 1 and 4. | The `finally` only unwinds what it set. Nested acquires now raise `NestedSlotError` rather than deadlocking on the non-reentrant write lane (found by critics 2 and 5). |
| 3 | **403 backoff was entirely untested** — removing it from `RETRYABLE_STATUSES` survived, despite deliverable 4 naming it explicitly. | Added `test_403_gets_backoff_and_retry_like_429` and `test_rest_retries_403_then_succeeds`. Mutation KILLED. |
| 4 | The `_recent_429` success-reset was unpinned; scattered 429s that each recovered would still trip the breaker and halt everything. | Asserted across 10 recover-then-succeed cycles. Mutation KILLED. |
| 5 | `budget_waits` once-per-episode was untested — the coupled `FakeClock` always frees the window in exactly one round, so the guard never ran. | Added a clock that under-advances (0.6x), forcing multiple rounds. Mutation KILLED. |
| 6 | The RestClient/Session shared-Pacer guard had no test. | Added. |
| 7 | Every `_config()` leaked a temp dir — 44 per run, 824 found on disk. | `atexit` cleanup; verified a full run now leaves zero. |

**A regression I introduced in round 1**, caught by critic 1: the `_MAX_BUDGET_ROUNDS=16`
backstop starved under lock contention once `max_read_concurrency` rose above the default
— pool 8 → 7 dropped calls, pool 64 → 58 — and the error message blamed a nonexistent
clock bug. Measured clean at the shipped default of 3, and no ceiling was ever breached.
Now bounded by an elapsed-time deadline *and* a far larger round cap (256), with an
accurate message. `max_read_concurrency` is capped at 8 in `_validate`, since deliverable
2 scopes it to 2–3.

Fixing that surfaced a hazard my first fix missed: a purely time-based deadline never
fires against a sleeper whose clock converges on the target asymptotically. Both backstops
are needed — the deadline catches a ceiling below the offered load, the round cap catches
the asymptotic sleeper.

Also hardened from the round-2 non-blocking pile, all raised by more than one critic:
`CLOSEWIRE_STATE_DIR` is now anchored to the directory holding `.env` rather than the cwd
(a halt was escapable by `cd`, and `pacing-reset` from the wrong directory reported
"nothing to reset" while the real latch survived); a corrupt `breaker.json` now **halts
and logs at ERROR** instead of silently starting closed — an unreadable safety latch is
treated as engaged, recoverable via `pacing-reset`; `_persist_breaker` writes to a temp
file and atomically replaces, so a crash mid-write cannot manufacture the truncated state;
`PacingBypassError` and `NestedSlotError` are exported from the package root; and
`LiveMessageClient` gained the same pacer/session mismatch guard `RestClient` has. Its
docstring had claimed the Pacer was "shared with the REST client" when nothing arranged
that — corrected to tell phase 09 to thread `RestClient.pacer` through explicitly.

> **Superseded by phase 09.** `LiveMessageClient` now **refuses** `session=` outright with a
> `TypeError` naming `transport=` and `pacer=session.pacer`, so the mismatch guard described
> above no longer has anything to compare. The reason: a `Session` is bound to the REST host
> and merges `ApiKeyAuth`'s headers into every request, so routing the runtime surface
> through it would change what goes on the wire and break this module's auth guarantee — and
> the parameter was in fact being **silently ignored**, handing a caller who mocked at the
> Session layer a real, credit-spending POST. The property is strictly stronger, not weaker.
> See `docs/validation/09-runtime.md`.

**Deferred to phase 05, deliberately:** the generated `endpoints/lead.py:66` still calls
`POST /lead/search` without `write=False`, so the original defect reproduces through that
specific path. The override exists and works; `prompts/05-read-client.md` has phase 05
authoring a curated `leads.py` rather than wrapping the generated function, and
`scripts/codegen.py` cannot emit the override. Phase 05 must not wrap the generated
function directly — and note that any later codegen run deletes every `*.py` in
`endpoints/`, which would take hand-written phase-05 modules with it.

---

## Review council — round 3 (5 PASS) and the fixes applied after it

All five critics passed. Critic 4's round-2 blocker is confirmed closed: mutation N12
(neuter `_exit()`) is killed, along with N12c and T7, and the one-shot slot token is pinned
four independent ways. Across the round the panel ran 39 mutations and killed 24, including
every rubric-cited behaviour and every round-1 and round-2 claimed fix they could reach.

Independent re-verification of the earlier blockers: the budget ceiling held at exactly 4
against `max_ops_per_hour=4` and a 40-thread hammer landed on exactly 12 ops / 6 writes;
`Retry-After` measured exact at 1/30/60/120/300/899/900s and surfaced without retrying at
901 and 3600s; the 500-sends-in-one-slot attack now puts **1** request on the wire with
499 refusals; and the starvation false-positive is gone across pools 1–8 (0 errors, versus
3/4/7 at pools 4/6/8 in round 2).

A passing verdict is not the same as nothing left to fix. Five findings were labelled
non-blocking — correctly, since they sit above the rubric's bar — but were real, so they
were fixed rather than shipped:

| # | Finding | Fix |
| :-- | :-- | :-- |
| 1 | **Regression introduced in round 3.** The atomic-persist rewrite used a single shared temp filename, so concurrent trips lost the persisted halt entirely — measured **16/30 at 2 threads, 25/30 at 8**, versus 0/30 for round 2's non-atomic version. The error handler's `tmp.unlink()` could also delete another caller's in-flight temp. It fires on the *expected* trigger: a revoked key trips every in-flight call at once. | Temp name is now per-writer (`pid` + thread id), so a writer only ever unlinks its own. Regression test spawns 8 concurrent trips and asserts the latch survives with no stray `.tmp`. |
| 2 | **The nested-slot guard had a hole under dry-run** (found independently by critics 1 and 5). Making dry-run skip the thread mark — my round-3 fix — left the write lane held with `in_slot` False, so a nested acquire **hung forever**: precisely the deadlock `NestedSlotError` exists to prevent. `prompts/07-write-client.md` mandates exercising the write path under dry-run first, so this was the mode new composite code would meet first. | The dry-run path now takes the thread mark with **zero** send tokens — nesting raises, and the suppressed write is still structurally unable to reach the transport. Both properties tested. |
| 3 | **Invalid UTF-8 in `breaker.json` escaped `Pacer.__init__`** (`UnicodeDecodeError` is a `ValueError`, not an `OSError`). It failed closed, but crashed `ping` with a raw traceback and defeated the documented `pacing-reset` recovery — the one corruption shape of thirteen that was unrecoverable. | Caught and latched like any other unreadable state; recovery verified. |
| 4 | **`pacing-reset` could report success without succeeding** — printing "traffic may resume" and exiting 0 while the latch survived and the next run came back halted. | `reset_breaker()` returns a bool; the CLI exits 1 with an actionable message. Tested on both failure routes (unusable state dir, unremovable latch). |
| 5 | A `CLOSEWIRE_STATE_DIR` pointing at a regular file read as "no halt" on Windows, silently disabling persistence while `_persist_breaker` warned on every trip — the two halves disagreeing about whether that config is a fault. Also: `opened_at` was unbounded and could dump ~20KB at the operator. | Non-directory state dir now latches; `opened_at` truncated to 64 chars. |

Test coverage gaps critic 4 identified were closed too: `_resolve_state_dir` had **zero**
coverage (a revert to the `cd`-escapable behaviour would have shipped green), and the
`max_read_concurrency <= 8` cap was absent from the knob-rejection list.

### A process failure worth recording

Critic 2 found a stale `/tmp/pacing.bak` containing `if False:` substituted for both
budget backstops — left by another critic mutating concurrently. The working tree was
clean, but separately, a critic that snapshotted `config.py` before an edit of mine and
restored it afterwards **silently reverted a comment correction**, which was only caught by
re-reading the file. Lesson for later phases: do not edit files while a mutation-testing
council is running. Verify the tree after every round.

### Post-council mutation battery (source restored and byte-verified after each)

| Mutation | Result |
| :-- | :-- |
| shared temp filename (the lost-latch race) | KILLED |
| dry-run skips the thread mark | KILLED |
| UTF-8 latch not caught | KILLED |
| reset reports success unconditionally | KILLED |
| `state_dir` anchoring reverted to cwd-relative | KILLED |
| read-pool cap removed | KILLED |

### Knowingly left open

- `endpoints/lead.py` — the generated `post_lead_search` omits `write=False`, so the
  original defect reproduces through that path. Phase 05 must author a curated `leads.py`
  rather than wrap the generated function; codegen cannot emit the override, and
  `clean_endpoints_dir()` deletes every `*.py` in that directory on each run.
- A leaked slot (`__enter__` with no `__exit__`) still leaks its lane permit, so enough
  leaks wedge the client. Fail-closed — it hangs rather than sends — but with no diagnostic.
- The persisted latch carries no account identity, so a reset clears it for any account
  sharing that `.env` directory. Over-halting is the fail-safe direction.
- Two of four breaker gates in `acquire()` remain individually removable without a test
  failing. Defense-in-depth only: with either gone the call is still refused at the final
  gate, just after a wasted wait.
- `scripts/fetch_spec.py` uses `urlopen` unpaced. Build-time, unauthenticated, public
  swagger; critic 2 re-inventoried every network call site in the repo and endorsed
  deferring it.

---

## Delivered

| # | Deliverable | Status |
| :-- | :-- | :-- |
| 1 | `Pacer` on every request; randomized delay in `[min, max]` + jitter; writes stricter | done |
| 2 | Writes serial (mutex), reads bounded pool (semaphore, default 3) | done |
| 3 | Sliding-hour budgets for ops + writes; BLOCK on hit, logged `pacing: waiting Ns for budget` | done (atomically, after fix 1) |
| 4 | Exponential backoff w/ jitter on 429/403, capped, honors `Retry-After`; re-raise after N | done (after fix 4) |
| 5 | Circuit breaker on recent 401/403 or repeated 429 → `PacingHalt`, manual reset | done (after fixes 3, 7) |
| 6 | `CLOSEWIRE_DRY_RUN` — writes logged + counted, never sent | done (after fixes 5, 8) |
| 7 | `PacerStats` snapshot for CLI/MCP; no secrets | done — `closewire pacing-status [--json]`; MCP tool lands in phase 11 |
| 8 | UI loop bootstrap — `CLOSEBOT_UI_BASE`, `docs/validation/` created | partial — login host confirmed, loop unusable (blocker 1) |

**Files:** `closewire_client/pacing.py` (new) · `rest.py` · `session.py` · `live.py` ·
`config.py` · `__init__.py` · `cli/main.py` · `.env.example` · `.gitignore` ·
`tests/test_pacing.py` (new, 52 tests) · `tests/test_transport.py` ·
`scripts/pacing_demo.py` (new).

### No-bypass guarantee

Now structural rather than conventional. `Pacer.acquire` marks the calling thread;
`Session.request` refuses to send without that mark. Reaching for `Session` directly
raises `PacingBypassError` instead of quietly putting an authenticated request on the
wire. `RestClient` and its `Session` must share one `Pacer` — a mismatch raises at
construction rather than making every request look like a bypass.

---

## Knobs

| Env var | Default | Effect |
| :-- | :-- | :-- |
| `CLOSEWIRE_MIN_DELAY_S` / `CLOSEWIRE_MAX_DELAY_S` | 1.0 / 4.0 | Think-time band; a uniform random draw per call |
| `CLOSEWIRE_JITTER_S` | 0.35 | Extra jitter so gaps are never a clean interval |
| `CLOSEWIRE_WRITE_DELAY_MULT` | 2.0 | Writes this much slower than reads (must be >= 1.0) |
| `CLOSEWIRE_MAX_READ_CONCURRENCY` | 3 | Reads in flight at once (writes are always 1) |
| `CLOSEWIRE_MAX_OPS_PER_HOUR` | 300 | Sliding-hour ceiling, all calls |
| `CLOSEWIRE_MAX_WRITES_PER_HOUR` | 60 | Sliding-hour ceiling, writes |
| `CLOSEWIRE_MAX_RETRIES` | 4 | Retries before a 429/403 is raised (max 32) |
| `CLOSEWIRE_BACKOFF_BASE_S` / `_CAP_S` / `_JITTER_S` | 2.0 / 60.0 / 1.0 | `base * 2^attempt` + jitter, capped |
| `CLOSEWIRE_RETRY_AFTER_MAX_S` | 900 | Longest server-requested wait we will sit through |
| `CLOSEWIRE_BREAKER_AUTH_THRESHOLD` | 3 | Recent 401/403 before halt |
| `CLOSEWIRE_BREAKER_429_THRESHOLD` | 5 | Recent 429 before halt |
| `CLOSEWIRE_STATE_DIR` | `.closewire` | Where a tripped breaker is persisted (gitignored) |
| `CLOSEWIRE_DRY_RUN` | 0 | Writes previewed, never sent |
| `CLOSEBOT_UI_BASE` | `https://app.closebot.com` | Build-time UI validation only |

Values that would disable or invert pacing are now rejected at construction.

---

## Local verification (passing)

```
python tests/test_pacing.py       ->  52 passed
python tests/test_transport.py    ->   6 passed  (no phase-03 regression)
python scripts/pacing_demo.py     ->   5 scenes, all as specified
closewire pacing-status [--json]  ->  budgets + breaker, exit 0, 16-key JSON
closewire pacing-reset            ->  "breaker was already closed"
```

Self-run mutation battery against every round-2 finding (source restored after each,
verified byte-identical):

| Mutation | Result |
| :-- | :-- |
| N12 — `_exit()` neutered (the round-2 blocker) | KILLED |
| N25 — `budget_waits` counted every round | KILLED |
| P1 — 403 dropped from `RETRYABLE_STATUSES` | KILLED |
| P6 — `_recent_429` success-reset removed | KILLED |
| one-shot slot token disabled | KILLED |
| nested-slot guard removed | KILLED |
| corrupt `breaker.json` silently ignored | KILLED |

Against the phase-04 "Done when", item by item:

| Required | Result |
| :-- | :-- |
| 10 fake calls, randomized gaps in band | 10/10 distinct values within `[1.00 .. 4.35]s`; a 200-sample test also pins that draws span the band and centre on its mean |
| Serial writes | 6 concurrent writes → peak in-flight **1**; 6 concurrent reads → peak **3** |
| Budget wait at a low ceiling | ceiling 3, 5 calls → call 4 blocked **3600s**, logged `pacing: waiting 3600.0s for budget`; ceiling holds under 12 concurrent readers |
| Repeated 429s trip the breaker | backoff 2→4→8→16s, then `PacingHalt` on the 5th; queued calls stopped too; halt survives a restart; `pacing-reset` resumes |
| `CLOSEWIRE_DRY_RUN=1` blocks a write but logs + counts it | write suppressed (`sent == []`), `total_writes=1`, `total_ops=1`, `dry_run_blocked=1`, logged at WARNING; reads unaffected |
| `closewire ping` works through the Pacer | **partial** — routes through the Pacer correctly, but returns HTTP 401 (blocker 2). The 401 was counted by the breaker, so feedback wiring is proven by test; a successful ping is not. |

### Two corrections found by my own tooling

The demo and then the test suite each caught a wrong assertion of mine about the sliding
window — I expected two budget waits where one was correct (all three ops age out
together when the window slides). In both cases the assertion was wrong and the code was
right; the assertions were fixed. The new `max_retries <= 32` validation also caught three
of my own tests using `max_retries=99`.

---

## UI validation — PARTIALLY PERFORMED

The maintainer logged Chrome into Closebot mid-session, clearing blocker 1.

| Required check | Status |
| :-- | :-- |
| Confirm `CLOSEBOT_UI_BASE` from a real login | **done** — authenticated dashboard served at `app.closebot.com`, `screens/04-dashboard-authenticated.jpg` |
| Screenshot Usage + dashboard home | **done** — the dashboard *is* the usage screen |
| Agency Usage screen shows paced calls registering, unbursted | **NO UI SURFACE — see below** |

### Finding: Closebot's UI has no API-request counter

This resolves the question left open above, and the answer is that the check phase 04 asks
for cannot be performed — not that it passed.

Everything the dashboard counts is **bot-message level, not HTTP-request level**:

- *Billing Period Usage*: Responses `4 / 500`, Used Storage `0.026MB / 1MB`, Users `2 / 2`
- *Lead Conversion Funnel*: Contacts (5) → AI Responded (5) → No Booking (5)
- *Metrics* dropdown, the only selectable series: **Responses Sent · Contacts Engaged ·
  Meetings Booked**

A paced `GET /bot` moves none of these. There is no screen on which "300 ops/hour" or
"calls arrived spaced rather than bursted" is observable, so **pacing has no UI surface**
and is attested by the local tests and the mutation battery alone. Phase 05's read
cross-checks are the first place UI corroboration becomes meaningful, and phase 09's
message sends are the first thing that will move the Responses counter.

### UI route map (for phases 05–12)

Worth recording now, since the UI labels do not match the API nouns:

| UI label | Route | API tag |
| :-- | :-- | :-- |
| Agents | `/bots` | Bot |
| Sources | `/settings/sources` | Source |
| Uploads | `/settings/knowledge` | Library |
| Chats | `/conversations` | Lead |

The account currently holds 5 contacts and 4 responses used of 500 — small enough that
phase 05's `bots.list()` / funnel cross-checks will be easy to verify by eye, and a
reminder that the `zz-closewire-test-*` convention matters here because this is a real
account, not an empty sandbox.
