---
phase: 06
status: closed
---
# Phase 06 · Read CLI — validation log

**Date:** 2026-07-25
**Verdict: CLEARED.** Round 1 = **2 BLOCK / 3 PASS**; round 2 re-convened both blockers on
a frozen tree and both returned **PASS**. All five critics have passed. The freeze held —
every file matched its pre-review md5.

`scripts/verify_cli.py` runs **every** command as a real subprocess — so exit codes,
stream separation, and argument parsing are exercised the way a user meets them — and
asserts coverage against `cli.reads.READ_COMMANDS`. It exits **0**.

---

## Delivered

| # | Deliverable | Status |
| :-- | :-- | :-- |
| 1 | Subcommand tree over the Tier-0 read client | done — 23 commands in 5 groups |
| 2 | Global `--json`; `--source`/`--bot`-style filters | done — `--json` works on either side of the subcommand |
| 3 | Reuses phase-03 config/auth + the Pacer; `pacing-status` | done |
| 4 | Non-zero exit codes, readable errors, key redacted | done — 0 ok · 1 failure · 2 breaker open |

**Files:** `cli/reads.py` (new) · `cli/main.py` · `scripts/verify_cli.py` (new).

### The command list

```
bots      list · get <id> · steps <id> [--version V] [--published] · descriptors · templates
personas  list · get <id>
sources   list [--all] [--query] [--category] · get <id> · calendars <id> · fields <id>
          · tags <id> · channels <id>
leads     list [--source] [--page] [--page-size] [--all] · get <id> · history <id>
          · ai-toggle <id> · search [--query] [--source ...] [--count] [--offset]
metrics   booking [--start] [--end] [--resolution] [--source] · summary
          · messages [--limit] · actions · logs
(plus phase 03/04: ping · whoami · pacing-status · pacing-reset)
```

### Read-only

`grep` for `"POST"|"PUT"|"PATCH"|"DELETE"` across `cli/` returns nothing, and no generated
write function is referenced. The one POST in the stack is `leads.search`, which the read
client routes with `write=False`.

### `--json` purity

Stdout carries JSON and nothing else, so `| jq` is always safe. Two things make that true
rather than incidental:

- **Logging is pinned to stderr.** Pacing emits think-time and budget lines, and redaction
  warns when a credential is unmasked; on stdout any of it would break the pipe. Verified
  by running with `CLOSEWIRE_LOG_LEVEL=DEBUG`, which forces **49 bytes of real log** onto
  stderr while stdout still parses as JSON. (The first version of this probe turned the
  delay knobs down instead and produced **zero** log bytes — think-time logs at DEBUG while
  the CLI pins the level to WARNING — so it asserted nothing.)
- **`--json` uses a shared parent parser with `default=SUPPRESS`**, so it works before *or*
  after the subcommand. A plain child default of `False` would have silently overwritten a
  global `--json` that was already set.

---

## Three defects found by the verification, and fixed at the root

| Defect | Root cause | Fix |
| :-- | :-- | :-- |
| **`bots descriptors` and `leads search` crashed** with `UnicodeEncodeError` on Windows | The CLI writes arbitrary API text — client names, message bodies, emoji — to a console using a legacy codepage (cp1252) | Streams reconfigured to UTF-8 with `errors="backslashreplace"`. Replacing my one `…` would have fixed the symptom; the next crash would have come from real data |
| **A bad id produced an `AttributeError` traceback** | Closebot answers some bad ids with **200 and a null body** rather than 404, so every renderer's `payload.get(...)` raised | One central `_require()` guard raising `NotFound`, plus a catch-all so a CLI never dies on an unfamiliar shape. Guarding 23 renderers individually would have been the patch |
| **The booking table's PERIOD column was blank** | I guessed the field names (`date`/`label`/`timestamp`) instead of checking | Verified the real shapes and composed the label from them — see below |

### A ninth spec deviation: `bookingGraph` returns a different shape per resolution

Not documented anywhere, and the reason the column was empty:

| Resolution | Point shape |
| :-- | :-- |
| `monthly` | `{year, month, count}` |
| `daily` | `{date, count}` |
| `hourly` | `{date, hour, count}` |

There is no single date field to read. `cli/reads.py::_booking_period` handles all three,
verified against live data at each resolution rather than inferred from one.

### Divergence from the phase prompt, deliberate

The prompt specifies `metrics booking … [--resolution day]`. **`day` is rejected by the API
with HTTP 400** — established in phase 05 and re-confirmed here. The CLI accepts
`hourly|daily|monthly`, defaults to `daily`, and rejects `day` *locally* with a message
naming the valid values, so the documented-but-wrong value costs no paced call. Following
the prompt literally would have shipped a command that always fails.

---

## API ↔ UI cross-check

| Command | CLI | UI screen | Match |
| :-- | :-- | :-- | :-- |
| `bots list` | 3 bots; `Money Flow` published `0.0.28,0.0.29`, latest `0.0.30`; SRC counts 0/2/1 | Agents → Job Flows: 3 rows, Money Flow **Published**, Sources Attached none/2/1 | ✅ |
| `bots get` | `modifiedAt 2026-02-11`, category, persona ids | bot row's Date Modified "February 11, 2026" | ✅ |
| `bots steps` | published `0.0.29`: 26 nodes, 26 edges, every type resolved | Job Flow builder | ⚠️ **API-verified only** — canvas node count is not reliably readable from a screenshot |
| `personas list` | 2 — Sarah, Sam | Agents → Personas: Sarah, Sam | ✅ |
| `sources list` | total 7, all connected, category GHL | Sources: "Total 7 / Connected 7 / Disconnected 0", Source Types "LeadConnector 7" | ✅ |
| `sources channels` | 8: WhatsApp, GMB, Live_Chat, SMS, Email, FB, IG, Custom | source → Channels nav: the same 8 | ✅ |
| `sources calendars` / `fields` / `tags` | 3 / 126 across 3 objects / 71 | — | **no UI surface** — these appear only inside a Booking or ModifyTags node editor, not on the source page. Phase 07 can corroborate them there |
| `leads list` | total 343 | Chats → All **343** | ✅ |
| `leads get` / `ai-toggle` | name, source, inbound body; `{enabled: False, applicable: True}` | conversation header, source chip, message; AI toggle **off** + "AI Off tag set" banner | ✅ |
| **`metrics booking`** | **8 points: `2025-08`→8, `09`→9, `10`→7, `11`→7, `12`→6, `2026-01`→4, `02`→1, `03`→2 (total 44)** | dashboard → Metrics → **Meetings Booked / 12 months**: x-axis 08/2025…03/2026 (**8 points**), curve rises to a peak at 09/2025, plateaus, declines, troughs at 02/2026 | ⚠️ **periods and shape verified; per-value NOT** — see below |
| `metrics summary` | messages 5, users 2, storage 27158 | dashboard usage panel: Responses 4/500, Users 2/2, 0.026MB | ✅ (message-vs-response denominators differ — see phase 05) |
| `metrics messages` | rows with channel/direction/body | Chats conversation view | ✅ — but see PII note |

Screenshots: `screens/06-ui-booking-12mo.jpg`, `screens/06-ui-booking-daily-empty.jpg`,
plus `screens/05-ui-agents.jpg`, `05-ui-sources.jpg`, `05-ui-chats.jpg` for the screens
shared with phase 05.

> `docs/validation/screens/` is gitignored. The captures show a live account — real
> consumer names, phone numbers, message bodies, and the agency's client roster. They are
> local evidence only, and this document describes what matched rather than quoting it.

### How far the booking row actually goes — corrected

I first wrote "shape and **every value** agree". A critic measured the artifact against its
own gridlines and got ≈ 8.0, **8.7**, 7.0, 7.1, **6.6**, 4.3, 1.0, 2.0 against my claimed
8, **9**, 7, 7, **6**, 4, 1, 2. The chart is a smoothed spline with no markers and no data
labels, so **per-point integers cannot be read off it** — a spline overshoots and
undershoots between knots. My own UI column hedged ("peaks ~9") and the Match cell then
asserted exactness.

What **is** corroborated, and strongly:

- the **8 periods**, 08/2025–03/2026, read from text axis labels
- the **curve's shape** — rise, plateau, decline, trough at 02/2026, uptick
- a y-scale whose ceiling of 12 is consistent with a peak of 9
- the **total of 44**, established independently from the API in phase 05

A spot-check hover did surface a tooltip reading **9** at the peak, which contradicts the
8.7 spline measurement and supports the CLI — but one hover is not eight values, and the
renderer stopped responding before more could be taken. Recorded as a data point, not proof.

**This still closes phase 05's `booking_graph` row in the way that mattered**: the API
returns a non-empty series the UI independently renders over the same 8 periods. It does
not establish per-point equality, and no longer claims to.

The companion capture explains the round-1 artifact rather than merely correcting it: with
the range on **Daily**, the chart reads "No data available" — which is why a 30-day window
returned zero on an account with 44 bookings.

`bots steps` remains API-verified-only: the builder is a pan/zoom canvas, and counting 26
nodes from a screenshot would not be evidence.

---

## Failure paths

| Case | Result |
| :-- | :-- |
| `bots get bot_DOES_NOT_EXIST` | exit **1**, `no bot found with id …` plus an id-format hint. No traceback |
| `metrics booking --resolution day` | exit **1**, rejected locally, names the valid values, **no API call made** |
| `metrics actions` / `metrics logs` | usually exit 1 — the documented server-side failures. **Intermittent**: `actions` returned exit 0 in one critic's run, and `verify_cli.py` already treats a success as "intermittent, not permanently broken". Phase 05 corrected the identical claim for the same endpoint; the correction had not propagated here |
| Any command | neither the Closebot API key nor a GoHighLevel credential appears on stdout or stderr, in either output mode |

---

## Local verification

```
python scripts/verify_cli.py     ->  23/23 commands, both modes, EXIT 0
python scripts/verify_reads.py   ->  31 calls, full coverage, EXIT 0
python tests/test_redaction.py   ->  17 passed
python tests/test_pacing.py      ->  52 passed
python tests/test_transport.py   ->   6 passed
```

---

## Round 1 findings and what changed

**2 BLOCK / 3 PASS.** Both blockers were defects my own verification could not see.

| Critic | Blocking finding | Fix |
| :-- | :-- | :-- |
| 1 | **`bots steps` printed silently-corrupted node ids.** The NEXT cell was pre-sliced with a bare `[:28]` — exactly the column width — so `_trunc` saw `len == width`, added no ellipsis, and 36-char UUIDs were cut to 28 with no marker, on 22 of 26 nodes. A NEXT value could never be matched to a NODE value | Slice deleted; `_trunc` marks every truncation. Verified live: **0 of 22 NEXT targets fail to resolve** to a node id |
| 4 | **The redaction assertion could not fail.** Both `check_no_secrets` calls passed the *table* process, so `--json` output was never inspected and `find_unredacted` never executed at all. Proven: with redaction neutered, **14 real client credentials** reached stdout while the script exited 0 reporting "no credentials in output" | `check_json` returns its process; the JSON run is scanned with its parsed payload; the four discovery commands are scanned too. Verified the fixed check catches exactly that leak |

### The root cause behind both rendering defects

Neither the blank PERIOD column nor the cut UUIDs could have been caught by what I built.
`verify_cli.py` checked exit codes, JSON parseability, coverage, and secrets — **nothing
compared rendered output against the payload it came from.** `check_render_fidelity` now
does, failing the run when a table shows fewer rows than the payload holds.

It produced a false positive on its first run: `bots templates` renders a plain indented
list, not a column table, and the row heuristic scored it zero. Now keyed off the separator
line `_table()` emits, so list-style renderers are skipped rather than mis-scored.

Also fixed from the non-blocking pile: the `--json`-purity-under-logging probe forced
**zero** log bytes and asserted nothing (think-time logs at DEBUG while the CLI pinned the
level to WARNING) — the CLI now honours `CLOSEWIRE_LOG_LEVEL`, a genuine operator feature,
and the probe forces 49 bytes of real log while stdout stays pure; `metrics messages
--json` returned a bare truncated array and now emits `{total, returned, results}`;
`bots steps --published` on a never-published bot reported "has no versions", which was
false; and `.get(k, "")[:n]` raised on a present-but-null value, now handled by `_short`.

## Round 2 — both blockers cleared, and what the round still found

Both critics re-verified on a tree frozen and md5-fingerprinted before they started; the
freeze held. Critic 1 re-ran the NEXT fix across **all three bots — 99 nodes, 0 unresolved
targets**. Critic 4 reproduced its exact round-1 leak and confirmed `verify_cli.py` now
exits 1 and names it on the JSON runs.

Neither blocked, but both punctured the fix I had written *because of* the overstatement
problem — `check_render_fidelity`'s docstring claimed to close the class containing both
shipped defects, and it caught **neither**: a blank column renders the right number of
lines, and `bots steps` was skipped outright because its payload has no `results` key.
Real coverage was 10 of 23 commands, only in the drop direction, and a renderer that
suppressed its own separator line disabled the check on itself.

Fixed rather than re-worded:

- **`ROW_ACCESSORS`** brings in the two richest renderers — `bots steps` (26 nodes) and
  `sources fields` (126 fields across 3 objects) — which were exempt, including the command
  whose bug motivated the check.
- **Framing is no longer trusted.** With no separator, content lines are counted instead of
  returning early, so a renderer cannot silence the check by changing its own output.
- **Both directions.** Fabricated rows now fail too.
- **The docstring states real scope**: it catches whole rows dropped or invented; it does
  **not** catch a malformed cell, which is what both shipped defects actually were.

That last correction is the seventh time a claim in this project outran its evidence — this
time inside the mechanism added to stop that. Recorded rather than smoothed over.

Also corrected: the **seventh superseded claim** — the failure-paths row asserting
`metrics actions` exits 1, when it is intermittent and returned 0 in a critic's run. Phase
05 had already corrected the identical claim for the same endpoint.

Scoping the check wider immediately produced a false positive on `sources fields` (55 rows
counted, 126 in the payload). Checked before changing the renderer: it prints all 126 across
**three sub-tables**, and the counter only summed the first block. The counter was wrong,
not the CLI.

### A process failure of mine, recorded because it was dangerous

Testing whether the fixed redaction check catches a leak, I mutated `redact_secrets` inside
a `try` wrapping a **20-minute** verification run. The command was backgrounded on timeout;
I stopped it; the `finally` never ran — leaving redaction **disabled in the working tree**,
with `sources get --json` returning a live client OAuth credential until I noticed and
restored it.

The mutation pattern is sound; putting a long-running command inside the `try` is not. A
mutation test must apply, assert **quickly**, and restore. The re-verification was redone
against `check_no_secrets` directly with synthetic payloads — instant, and incapable of
leaving a security control off.

A critic demonstrated the better technique in round 2 and it is worth adopting: inject the
mutation through a **`PYTHONPATH` shim** rather than editing the file. Nothing is written to
the repo, so there is nothing to restore and no window in which a security control is off.
Its whole round left every frozen file byte-identical.

---

## Open / carried forward

- `bots steps` needs builder-level corroboration in phase 07.
- Source calendars/fields/tags need node-editor corroboration in phase 07.
- **`metrics messages` prints real consumer message bodies.** The CLI caps display at
  `--limit` (default 20) and warns, but phase 11's MCP tool needs a **row limit**, not just
  field redaction — a critic measured 100 conversations returned unfiltered.
- **`write=False` now bypasses five protections** (read/write lane, write budget, dry-run,
  the redaction-sentinel guard, and the params check). Both critics who examined it agree
  it should be renamed or allowlisted **before phase 07 ships the first real write**.
- Table output is UTF-8; a consumer reading it must decode UTF-8. `--json` is unaffected —
  `json.dumps` escapes to ASCII, so piped JSON is safe for any reader.
- **Phase 07 must refactor the CLI wiring before adding writes.** `add_read_parsers`
  returns `None` and holds each group parser in a local, so a second `add_parser("bots")`
  raises `ValueError`; and `main.py` routes on the *group* alone, so a registered write
  action would fall into `cmd_read` and exit 1 with nothing printed.
- **Replace the read-only grep with a runtime invariant.** `dispatch_read` holds `rest`, so
  a Tier-0 command can assert `rest.pacer.stats().writes_last_hour == 0` — built on phase
  04's own accounting, and it survives whatever phase 07 adds to the same file. The grep
  dies the day writes land.
- `_require()` (the null-body guard) is client-layer policy sitting in the presentation
  layer; phase 11 needs the same behaviour.
- `--all` reports the fetched count as `total`, understating it in `--json` on a truncated
  sweep; it also ignores `--page`/`--page-size` and cannot raise `max_pages`.
- `_trunc`/`_table` size columns by code points, so CJK and emoji misalign.
- The PII warning lives only on `metrics messages` in table mode; `leads list/get/search`
  print the same class of data with none, and it vanishes under `--json`.
- `leads list [--bot <id>]` is in the prompt but unimplementable — `GET /lead` accepts only
  `page`, `pageSize`, `sourceId`, `leadId`. It **is** implementable on `leads search`.
- `_configure_streams` runs only in `cmd_read`, so `ping` — which uses `ensure_ascii=False`
  — can still hit the encoding crash on a legacy codepage. It belongs in `main()`.

  **RESOLVED in phase 08:** stream configuration now runs first in `main()` and the
  implementation moved to `closewire_client/console.py`, so every entry point — including
  `ping`, `pacing-status` and `--help` — is covered.
- A shell loop over CLI invocations is throttled only by think-time, not the hourly ceiling
  (each process starts a fresh in-memory budget). The persisted breaker halt does survive.
