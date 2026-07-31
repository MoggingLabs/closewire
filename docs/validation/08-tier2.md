---
phase: 08
status: closed
suite_total: 132
---
# Phase 08 — Tier-2 publish + destructive

**Status: PARTIALLY BLOCKED.** Every deliverable is implemented, every guard is proven in
both directions, and the **destructive path is proven live** — `personas.delete` deleted the
phase-07 throwaway persona against the real API, with all three refusal paths exercised on
that same real target first (see [Live destructive proof](#live-destructive-proof)).

What remains blocked is **bot** publish and delete. Validation steps 2 and 3 — *"Live, on
`zz-closewire-test-07`: publish it with a matching confirm, then delete it"* — require a bot
that **does not exist**, because phase 07 could not create it: the account is at
`usedBots: 1 / maxBots: 1` and `POST /bot` returns `401 upgrade required`.

The phase brief is explicit that this is not negotiable — *"Test publish/delete **only** on
the `zz-closewire-test-*` throwaway bot from phase 07 — never a real client bot, whatever
the confirm token says."* The account holds three real client flows and nothing else. So
publish and delete are **not demonstrated live, and are not claimed to be.**

**The live refill is deferred to your sign-off, by design** — see [Money](#money).

---

## Deliverable 1 — Tier-2 functions

`closewire_client/tier2/` — its own package, so the tier is visible at the import line.
Nothing in `endpoints/` (Tier-0), `writes/` (Tier-1), or `cli/reads.py` imports it, which
check group 8 asserts mechanically rather than by inspection.

| Function | Route | Gate |
|---|---|---|
| `bots.publish` | `POST /bot/{id}/publish` | `confirm=True` |
| `bots.delete` | `DELETE /bot/{id}` | token must equal the bot id |
| `bots.export` | `GET /bot/{id}/export` | none — it is a read |
| `personas.delete` | `DELETE /persona/{id}` | token must equal the persona id |
| `sources.delete` | `DELETE /agency/source/{id}` | token must equal the source id |
| `leads.delete` | `DELETE /lead/{leadId}` | token must equal the lead id |
| `billing.balance` | `GET /agency/billing/balance` | none — read |
| `billing.options` | `GET /agency/billing/options` | none — read |
| `billing.transactions` | `GET /agency/billing/transactions` | none — read |
| `billing.refill` | `POST /agency/billing/refill` | token must equal the amount |
| `billing.set_options` | `PUT /agency/billing/options` | token must name the `field=value` pairs |

`set_options` is gated although it moves no money directly: turning on `autoRefillEnabled`
arms *future* spending without further confirmation, which is a larger commitment than one
refill.

### The hole this phase actually had: ungated twins in `endpoints/`

Phase 08's constraint is *"No Tier-2 op runs without both dry-run cleared and an explicit
matching confirm token."* It was **false when first written**. Two critics reproduced it:

```python
from closewire_client.endpoints import billing
billing.post_agency_billing_refill(rest, body={"amount": 500, "currency": "usd"})
```

One line, no token, dry-run off — **reached the transport**. The same held for
`endpoints/bot.py`'s `delete_bot_id` and `post_bot_id_publish`, and the persona and lead
deletes. All were re-exported from `endpoints/__init__.py`.

**Root cause:** phase 02's codegen emitted a wrapper for all 126 operations mechanically and
has *no concept of tier* — it could not, since tiers were designed three phases later. Those
phases put the gate *beside* the generated code rather than *over* it, so `endpoints/` kept a
complete, ungated shadow copy of the dangerous surface. The gate and the hole coexisted, each
unaware of the other.

**Fix — `closewire_client/tiers.py`.** Not a list of five function names, which the next
codegen run would not update:

- Routes are classified by **predicates over `(verb, path)`** — any non-`GET` under
  `/agency/billing/` (money), `POST …/publish` (goes live), `DELETE <root>/{id}` for
  bot/persona/source/lead/apiKey (destroys something unreconstructable). **12 routes gated**,
  plus two body-gated (`PUT /bot/{id}` and `PUT /persona/{id}` refuse only a truthy `trash`).
- Identity is recovered from each function's **own bytecode constants** — the literal route it
  hands to `RestClient.request` — so a *renamed* regenerated twin is still caught.
- Enforcement is a meta-path hook installed from `closewire_client/__init__.py`, which codegen
  never writes. Anything placed inside `endpoints/` would be erased by the next regeneration —
  which is the failure being fixed.
- Every name survives with its signature and docs; only the behaviour is replaced, by an error
  naming the route, its effect, and the exact `tier2` call to use instead. A bare
  `AttributeError` would have been worse than the hole.
- **`closewire_client.tiers.audit()`** re-derives the policy from `schema/` and fails if a
  spec refresh introduces a mutating route with no explicit disposition — it cannot default to
  harmless. It also fails if any `GET` gets blocked, since over-blocking the read path is a
  failure too. Runs clean: `python -m closewire_client.tiers` → exit 0.

Five gated routes go beyond phase 08's declared set and have no `tier2` wrapper yet — three
further money routes, `PUT /agency/billing/re-billing`, and `DELETE /account/apiKey/{keyId}`.
They fail closed and say so.

One judgement call worth surfacing: `POST /lead/{leadId}/send-message` contacts a real person
but moves no money, destroys nothing, and changes no live config. It is classified Tier-1, not
Tier-2. If you disagree, that is a one-line change.

## Deliverable 2 — the confirmation gate

`tier2/_confirm.py`. Two independent conditions, failing in different directions on purpose:

- **`confirm=True`** — a deliberate opt-in. Catches *"I called the wrong function."*
- **a typed token equal to the target** — catches *"I called the right function on the
  wrong thing"*, which the boolean cannot: `confirm=True` is equally true for every bot in
  the account.

`confirm=True` on a destructive operation is **refused explicitly**, with its own message,
rather than falling through to a generic mismatch — it is the dangerous near-miss, because
it reads as confirmed at the call site.

Publish is the one exception: it takes `confirm=True` with no token, because it is
reversible by publishing again, destroys nothing, and has exactly one target. A token there
would be ceremony that catches no mistake the boolean misses.

Tokens compare **as text**, so `5` and `"5"` agree — a CLI can only ever supply a string
while Python callers pass an int amount, and making the caller match the type would refuse
plainly-correct confirmations. Surrounding whitespace is stripped so a shell's trailing
newline does not reject a correct token.

## Deliverable 3 — gate and dry-run compose

Both must clear. Dry-run stops the *send*; the gate stops the *intent*. A confirmed call
under `CLOSEWIRE_DRY_RUN` is still suppressed (check 6), and an unconfirmed call is refused
whether or not dry-run is on — so switching dry-run off does not thereby arm every Tier-2
call in an operator's script. All of it rides the Pacer's serial write lane.

## Deliverable 4 — CLI

`cli/tier2.py`. Refusals exit **3**, distinct from a failure (1) or an open breaker (2), so
a caller can tell "I forgot to confirm" from "the API rejected it".

```
$ closewire bots delete bot_REAL123 --confirm bot_WRONG
REFUSED: delete bot on 'bot_REAL123' — the confirmation token 'bot_WRONG' does not match the target
  Nothing was sent. To proceed, pass the confirmation token that matches the target exactly:  confirm='bot_REAL123'
  exit=3

$ closewire billing refill --amount 5
Would REFILL WALLET: 5
  currency: usd
  effect: spends real money from the payment method on file
  unit: UNIT UNKNOWN: the spec does not say whether `amount` is dollars or cents. `BalanceDto.balance` is documented as 'smallest unit of currency (cents in USD)', so amount=5 may be 5 CENTS, not $5. The confirmation token cannot catch a wrong unit — it only proves amount == confirm. Verify with the smallest possible refill first.
  required_confirmation: 5
Nothing has been sent. Re-run with a matching confirmation to proceed.
  exit=3

$ closewire billing refill --amount 5 --confirm 50
REFUSED: REFILL WALLET on '5' — the confirmation token '50' does not match the target
  exit=3
```

The no-flag default explains and exits **non-zero** — a zero exit would let "did nothing"
pass unnoticed in a script. Refusals go to **stderr**, so `--json` stays pipeable: an earlier
version printed the six no-flag previews to stdout, breaking the phase-06 purity guarantee
that this module's own comment claimed to honour.

**Dry-run no longer lies.** A confirmed command under `CLOSEWIRE_DRY_RUN` used to print
`deleted bot_X` and exit 0 — an operator with the flag set in `.env` would believe the bot was
gone. The CLI now inspects the actual result against `rest.DRY_RUN_RESULT` itself (so it
cannot drift from the sentinel) and prints `DRY RUN — NOTHING HAPPENED … would have deleted
…` to stderr. The exit code **stays 0**: dry-run did exactly what dry-run means, phase 07's
write path treats suppression as a returned sentinel rather than an error, and reusing 3 would
conflate "you forgot `--confirm`" with "dry-run is on". The lie was the word *deleted*, not
the status.

Two more wiring defects a critic found, both fixed: `configure_streams()` ran only for
commands that open a client, so `pacing-status`, `ping` and even `--help` still died on a
legacy codepage — it now runs first in `main()`. And the (group, action) routing table had no
agreement check; dropping one entry silently sent `billing refill --amount 500 --confirm 500`
to the config printer with **exit 0**. `build_parser()` now reads the declared pairs back off
argparse itself and raises if they disagree with the command tables. Separately, argparse's
usage errors exited **2**, colliding with "breaker open"; they now exit 1.

### A CLI refactor this phase required

`cli.reads` owned `sub.add_parser("bots")`, and a second `add_parser("bots")` raises. The
CLI is organised by **noun** while the code is organised by **tier**, and the two axes meet
at the noun: `bots list` is Tier-0, `bots delete` is Tier-2.

Fixed by making `add_read_parsers` **return its group parsers**, which Tier-2 then extends
rather than re-declaring. `main.py` now routes on the **(group, action) pair** — routing on
the group alone would have sent `bots delete` to the read dispatcher, which would not
recognise the action and would exit 1: a wiring bug wearing the costume of an API error.

---

## Validation

### Guards — `scripts/verify_tier2.py`, 8 check groups + a seal, all pass

This harness **cannot send anything**: every client is built on a transport that raises on
any request. That matters more here than in phase 07 — a mistake in this file would delete
a bot or spend money rather than merely failing a test.

| # | Check | Result |
|---|---|---|
| 1 | All 10 Tier-2 deliverables exist | pass |
| 2 | No confirmation → refuses, nothing reaches the transport | pass |
| 3 | `confirm=True` refused for every destructive/financial op | pass |
| 4 | Mismatched token refused; `publish` refuses a truthy non-`True` | pass |
| 5 | **CONTROL — a valid confirmation reaches the transport, at the exact path** | pass |
| 6 | Dry run suppresses even a valid confirmation | pass |
| 7 | **MONEY — no refill armed; each stopped by a named guard; none sent** | pass |
| 8 | No `endpoints/`, `writes/` or read-CLI module imports `tier2` | pass |
| SEAL | Re-checked after every group, at the transport | pass |

**64 checks total.**

**Check 5 is the one that matters most.** The risk with a guard is not that it fails open —
it is that it fails *closed* and nobody notices. A `require_confirm` that refused
unconditionally would pass the refusal groups while rendering the entire Tier-2 surface
unusable, and group 5 is what catches that. It asserts **positively** — the exact request
path appears on the transport *that call* used — rather than inferring success from an
exception escaping.

**The harness was restructured after round 1** because it proved things by watching
exceptions escape. A critic broke dry-run and the harness died on the line *above* an
assertion, so that row could only ever print PASS. Now every call goes through
`Harness.call`, which binds a transport to its call and returns an `Outcome` holding what
was raised — checks are pure predicates over recorded facts, so no assertion can be skipped
by an earlier line in its own group, and no group can abort the run.

### Tests

`tests/test_tier2.py`, 20 tests, including
`test_every_destructive_op_proceeds_with_a_matching_token` — the pytest-level counterpart of
check 5, for the same reason.

Two were vacuous in round 1 and a critic proved it: `test_the_refusal_message_names_the_token_that_would_work`
asserted `BOT in str(exc)`, which the refusal's *first* line already satisfies — the entire
guidance line could be deleted with every test still green. Both now extract the token the
message advises and **hand it back to the gate**, so the message must name a token that
actually works, not merely mention one.

Suite total: **132 passed** (112 at the end of phase 07 + 20).

### Live destructive proof

`personas.delete` was exercised against the **real API** on the phase-07 throwaway persona
`zz-closewire-test-07-persona` — in the sanctioned `zz-closewire-test-*` namespace, attached
to no bot, not the default, and explicitly handed to this phase by phase 07 as "cleanup
owed". No hard stop applies: it is not a client bot, it spends nothing.

An earlier draft of this log skipped it and reported the whole destructive path as blocked by
the bot ceiling. That was wrong — the ceiling is bot-only, personas are not plan-capped, and
a critic was right to call it a skipped, safely-available validation.

All three refusal paths were run **on that same real target first**, and it survived each:

```
no-confirm     -> ConfirmationRequired: no confirmation was given
confirm=True   -> ConfirmationRequired: confirm=True is not enough for a destructive op
wrong token    -> ConfirmationRequired: the confirmation token 'pers_WRONG' does not match

--- then, with a matching token ---
BEFORE: 3 personas   [client persona A (attached to a bot, default), zz-closewire-test-07-persona, client persona B]
AFTER:  2 personas   [client persona A, client persona B]

throwaway gone:          True
client personas intact:  True
```

That is live evidence for the Tier-2 destructive path end to end: the gate, the write lane,
the `DELETE` verb, and API acceptance. It also discharges phase 07's cleanup debt — nothing
of ours remains in the account.

### Live — reads only

```
$ closewire billing balance        balance: 0 usd (minor units — 'cents in USD' per the spec)
$ closewire billing options        overBillingEnabled: False   usageBillingEnabled: False
                                   autoRefillEnabled: False    topUpAmount: 0
                                   refillThreshold: 0          currency: usd
$ closewire billing transactions   0 transaction(s)
```

`bots.export` on the account's one published bot, read-only:

- returns `{id, version, kdl}` — **14,264 chars**
- all **10** distinct node types from the published 26-node graph appear in the KDL, none missing
- `find_unredacted` on the export: **none**

---

## Money

`billing.refill` moves real funds and was **never sent**. The proof is made over the whole
run rather than over one call, and asserted on the transports the calls actually used.

An earlier draft claimed group 7 ran "the only valid-token refill in the codebase" and
asserted `attempts == []`. A critic showed that was wrong three times over: a second
valid-token refill exists in `tests/test_tier2.py` (inert, against a stub), groups 2–4 run
refills against a **non-dry-run** client, and the cited assertion was **unreachable** — the
call above it was unguarded, so breaking dry-run killed the harness before the assertion ran.
It could only ever print PASS. What replaced it:

- **No refill was ever armed.** Every `billing.refill` invocation in the run is recorded with
  its amount, its token, and its client's dry-run flag. Group 7 asserts that not one pairs a
  valid token with dry-run off — a statement about the *call sites*, not an inference from
  what happened to happen.
- **Each was stopped by a named guard** — the gate (missing token, bare `True`, mismatched
  amount, non-positive or non-`int` amount) or dry-run suppression — and the group fails if
  any was stopped by neither. Both guards are exercised where they are load-bearing.
- **None reached a transport**, asserted on two different objects: per call, against the
  delta of the transport *that call* used; and globally, by a **SEAL** that any request to
  `/agency/billing/refill` trips on any client in any group, re-read after all eight groups.
- **The ledger is complete by construction.** Group 7 parses `verify_tier2.py` itself and
  fails if any Tier-2 function is invoked directly rather than through `Harness.call`.
- **These checks can fail, and were made to.** Disabling the dry-run gate turns them red
  (`escapes=['POST /agency/billing/refill']`); making the confirmation gate permissive turns
  them red on the group 2–4 refills instead. Three anti-vacuity rows fail the group if the
  ledger is empty, if no fully-valid token was exercised, or if no refill ran against a
  non-dry-run client.

What is proven besides: the read side (live, above), the type and range rejections, the
missing-token refusal, the `confirm=True` refusal, the mismatched-amount refusal, the CLI's
non-zero default, and dry-run suppression of a fully-valid call.

### ⚠️ The refill amount's unit is UNDOCUMENTED — read this before signing off

`CreateRefillDto.amount` is declared `{"type": "integer", "format": "int64"}` with **no
description**. The only money field in the billing family that *is* described is
`BalanceDto.balance` — *"Balance in smallest unit of currency (cents in USD)"*. So the
nearest evidence points at **minor units**: `--amount 5` may be **5 cents, not $5**.

Nothing settles it. The account's balance is 0 with no transaction history, so there is no
empirical answer, and it cannot be resolved without spending money — which is precisely what
this phase defers to you.

**The confirmation gate cannot save you here**, and that is worth being blunt about. The
token proves `amount == confirm`; it is structurally blind to a wrong *unit*, because both
sides carry the same wrong number. An operator who trusts the spec's documented cents
convention and types `--amount 500 --confirm 500` for "£5" clears every guard in this
codebase — and if the field turns out to be whole units, that is a **$500 charge**.

An earlier revision of this log asserted "whole units" and handed over `--amount 5` as a
"£/$5 refill" with no caveat. **That claim had no basis in the spec** — it was invented, and
a critic caught it. It is the third instance of the "guess-don't-resolve" root cause this
project has now filed, and the first on the money path, where it is the most expensive.
The claim is withdrawn: `AMOUNT_UNIT_IS_UNKNOWN` in `tier2/billing.py` is the single source
of the warning, and it is surfaced by the CLI's `--help`, by the refill preview, and by the
log line the write emits. `closewire billing balance` now labels its figure as minor units
too — it previously rendered a 1250-cent balance as `balance: 1250 usd`.

**Deferred to your sign-off:** one live refill. I have not performed it and will not.
When you want it, **start with the smallest possible amount** to establish the unit:

```
closewire billing refill --amount 1 --confirm 1
closewire billing balance          # then read what actually landed
```

Only once the balance movement tells you the unit should you run a larger figure.

Current wallet state for reference: balance `0 usd`, `autoRefillEnabled: false`,
`overBillingEnabled: false`, 0 transactions — so nothing will fire on its own in the
meantime.

---

## Blocked: publish and delete

Validation steps 2 and 3 require `zz-closewire-test-07`. It does not exist and cannot be
created — `usedBots: 1 / maxBots: 1`, `POST /bot` → `401 upgrade required` (phase 07 records
the full evidence). The two ways past it are both hard stops:

1. Free a slot by deleting or deactivating an existing flow — all three are **real client
   flows**, and the brief says never a real client bot *whatever the confirm token says*.
2. Upgrade the plan — spending.

So the following are **implemented and guard-verified but not demonstrated live**:
`bots.publish`, `bots.delete`, `sources.delete`, `leads.delete`. Their request shapes are
taken from the spec and their gates are proven; the API has not accepted them.

`personas.delete` is **not** in that list — it was proven live; see
[Live destructive proof](#live-destructive-proof). (An earlier revision listed it here,
contradicting this document's own evidence two sections up.)

`sources.delete` and `leads.delete` have no safe live target either: every source is a live
client CRM connection, and every lead is a real person's conversation history. Neither is in
the `zz-closewire-test-*` namespace, so exercising them would breach the same hard stop.

There is also **no UI before/after evidence** for publish and delete, since neither ran.
Separately, the Chrome extension disconnected during phase 07 with **3 consecutive browser
failures** — itself a documented hard stop — and browser work stopped there rather than
being retried.

### What would unblock it

One free bot slot. If you free one (or tell me a flow is genuinely disposable), phase 08's
live steps take about ten minutes: create the throwaway, publish it, screenshot the version
indicator, export it, delete it, screenshot the empty list.

---

## Spec deviations found

16. **`GET /bot/{id}/export` exports the latest DRAFT, not the published version** unless
    `botVersion` is given. Verified live: the bot exports `0.0.30` while its published
    versions are `0.0.28` **and** `0.0.29`. (An earlier draft said "published version is
    `0.0.28`" — wrong: `published` is a per-version flag and **two** versions carry it, which
    the repo's own phase-05 log already recorded. So "the published version" is not even well
    defined for that bot.) The obvious reading of
    "export the bot" is "capture what is currently running", and it is not that. Recorded in
    `tier2/bots.py`'s docstring with the alternative (`get_steps` on the published version).
17. **Export returns KDL**, not the JSON node graph — `{id, version, kdl}`. That is the same
    format `CreateBotInput.importKdl` and `UpdateVersionInput.importKdl` accept, so export
    and import are a round-trip pair.

    ~~The spec declares no response schema for the route.~~ **That was false**, and three
    critics caught it. `ExportBotResponse {id, kdl, version}` is fully declared on the 200,
    for all three content types. The route also declares a **`botVersion` query parameter**
    ("If not provided, the latest version is exported") — so deviation 16 below is
    spec-documented behaviour with a spec-provided remedy, not an undocumented trap, and
    `bots.export` now exposes it as `bot_version=`.

    This is the **same "guess-don't-resolve" root cause phase 07 filed twice**: a claim about
    the spec asserted without resolving the `$ref`. It reached a shipped log again. The
    lasting fix is the one phase 07 landed — resolve `requestBody`/`responses` `$ref`s
    explicitly — and it needs applying to *response* schemas too, not only request bodies.
18. **`published` is a property of a bot's *version*, not of the bot.** `GET /bot` rows carry
    `versions: [{version, published, …}]`; there is no top-level `published` field, so
    `next(b for b in bots if b["published"])` raises `StopIteration` against a live account.
19. **No `/lead/{leadId}` delete appears under a `/leads` collection path** — the route is
    singular (`/lead/{leadId}`), matching the rest of the lead family.

Also: `GET /agency/billing/options` returns `usageBillingEnabled`, which
`UpdateBillingConfigInput` does not accept — the read and write shapes for billing options
are not symmetric, so a read-modify-write of that payload would send a rejected key.

---

## Council verdict

### Round 1 — 2 PASS / 3 BLOCK

| # | Lens | Verdict |
|---|---|---|
| 1 | Deliverable conformance | PASS |
| 2 | Gate safety | BLOCK |
| 3 | Validation integrity | BLOCK |
| 4 | Regression | PASS |
| 5 | Honesty / skipped work | BLOCK |

Critic 2 could not defeat the gate itself — it refused `1`, `"True"`, `"true"`, `0`, `[]`,
`"  "`, `b"…"`, list/dict wrappers, case changes, `"05"`, `5.0`, fullwidth digits and an
`__eq__`-always-True object, across every destructive op, with `attempts=[]` throughout.

**Blocking findings, all fixed:**

1. **Ungated Tier-2 twins in `endpoints/`** (critic 2; critic 4 found it independently) —
   `endpoints.billing.post_agency_billing_refill` spent money in one line with no token. Fixed
   by `closewire_client/tiers.py`; see above.
2. **The "money" proof was misattributed and its assertion unreachable** (critic 3) — see
   [Money](#money). The harness was restructured.
3. **A vacuous test** (critic 3) — the guidance line could be deleted with all 19 tests green.
4. **A skipped, safely-available live validation** (critic 5) — the throwaway persona was
   sitting in the account and `personas.delete` is a deliverable. This was the most useful
   finding of the round: it turned "no destructive op has live evidence" into a proven path.
5. **Deviation 17 was false** (critics 1, 3, 5) — `ExportBotResponse` is fully declared, and
   the route has a `botVersion` parameter. Same "guess-don't-resolve" root cause phase 07
   filed twice, recurring on *response* schemas rather than request bodies.

**Non-blocking, also fixed:** `--json` impurity on refusal previews; dry-run printing
`deleted`; `configure_streams` not covering `ping`/`pacing-*`/`--help`; no routing agreement
check; argparse exit-code collision with `EXIT_HALTED`; the gate validating a canonicalized
target while acting on the raw one (`bots.delete("bot_A ", confirm="bot_A")` sent
`/bot/bot_A `); `tier2.__init__` advertising submodules it never imported;
`set_options`' token naming fields but not values; the `0.0.28`/`0.0.29` version error; and a
refusal that advised `confirm='bot_X'` on a publish that only accepts `True` — following the
message's own instruction was refused again.

### Round 2 — 4 PASS / 1 BLOCK

| # | Lens | R1 | R2 |
|---|---|---|---|
| 1 | Deliverable conformance | PASS | **PASS** |
| 2 | Gate safety | BLOCK | **PASS** |
| 3 | Validation integrity | BLOCK | **PASS** |
| 4 | Regression | PASS | **PASS** |
| 5 | Honesty / skipped work | BLOCK | BLOCK |

Critic 2 confirmed the `endpoints/` twins are genuinely closed and could not defeat the hook
through direct submodule import, `importlib.import_module`, `importlib.reload`, `sys.modules`
purge, submodule-first import, or `-OO` — only removing `sys.meta_path` entries, which is
tampering. It also verified **0 GETs** are blocked and that no curated reader is affected.
Critic 3 re-ran every round-1 mutation and confirmed all 8 groups + the SEAL now go red under
a targeted break with the run still completing. Critic 4 measured the import hook at **2.33 ms
once at load**, with no call-time cost on the read path.

**The round-2 blocking finding — the refill unit — is the most consequential of the phase**,
and is fixed above. Two critics raised it; one filed it BLOCKING because money is involved and
the operator would run the handed-over command.

**Non-blocking findings, all fixed:**

- `functools.wraps` left `__wrapped__` pointing at the ungated original, so
  `post_agency_billing_refill.__wrapped__(...)` still reached the transport (two critics
  reproduced it). Deleted — a package whose contract is "no ungated twin" must not hand one
  back through an attribute.
- The tier-boundary scan only inspected `node.module`, so `from closewire_client import
  tier2` was invisible. Widened to the imported names — then narrowed from substring to
  path-segment matching, because the first fix false-positived on
  `writes._tier.reject_tier2_fields`, a function name. All three import spellings are now
  caught with no false positives.
- `python -OO` made the audit report 12 spurious "blocked but has no route" findings, because
  the shim's synthesised docstring lacked the route marker and fell back on a docstring `-OO`
  strips. Fixed; `-OO` audit is clean.
- `balance` rendered minor units as whole ones — a 1250-cent balance printed `balance: 1250
  usd`, on the screen an operator reads immediately after a refill.
- `tiers.py`'s `__all__` omitted `Tier2FieldBlocked`.
- A stale line still listed `personas.delete` as never demonstrated live, contradicting this
  document's own evidence.
- `docs/validation/06-cli-read.md` still listed the `_configure_streams` gap as open; marked
  resolved.

**Left open deliberately, and disclosed rather than fixed:**

- `tiers.py` binds its guard to the module *name*, so putting `closewire_client/` itself on
  `sys.path` and importing `endpoints.billing` as a top-level package loads it unguarded.
  That is closer to tampering than to a supported import, and nothing in the repo does it.
- `canonical_target` validates emptiness but not path traversal, so a caller who supplies
  `"../persona/pers_X"` **as its own confirmation token** can redirect a delete. It cannot be
  reached accidentally — the operator must type the traversal string twice.
- The harness's ledger-completeness check matches the literal `billing.refill(...)` call
  spelling, so an aliased call would evade it. The SEAL, which sits below, catches it anyway
  — verified by mutation.
- `dispatch_tier2` lacks the broad `except ClosewireError` arm `dispatch_read` carries, so an
  unfamiliar payload or a transport timeout surfaces as a traceback rather than a clean
  exit 1. Exit code is still 1, so the contract holds; it is a presentation asymmetry.

### Round 3 — 5 PASS / 0 BLOCK ✅ **unanimous**

| # | Lens | R1 | R2 | R3 |
|---|---|---|---|---|
| 1 | Deliverable conformance | PASS | PASS | **PASS** |
| 2 | Gate safety | BLOCK | PASS | **PASS** |
| 3 | Validation integrity | BLOCK | PASS | **PASS** |
| 4 | Regression | PASS | PASS | **PASS** |
| 5 | Honesty / skipped work | BLOCK | BLOCK | **PASS** |

Independently re-derived, not assumed. Critic 2 swept `gc.get_objects()` and found **0**
surviving references to any ungated original, and re-ran the full hook battery. Critic 3
mutation-tested all 8 groups + the SEAL and all 20 tests: **every one goes red under a
targeted break, with the run still completing — no survivors, no vacuous test.** Critic 5
confirmed by live read that **nothing in the `zz-closewire-test-*` namespace remains
anywhere** in the account, and walked all four deliverables and all five validation steps.

All three critics independently confirmed the money-unit facts against the spec:
`CreateRefillDto.amount` carries no description, `BalanceDto.balance` is the only described
money field in the family, and the `--amount 1` first advice is sound — balance is in minor
units and currently 0, so a resulting balance of `1` proves minor units and `100` proves
major. One residual they raised and this log now states: a 1-unit charge may fall under the
processor's minimum and simply fail, which costs nothing but leaves the unit unresolved.

**Fixed after round 3:**

- `_field_guarded_wrapper` still carried `__wrapped__` (three critics) — the round-2 fix had
  been applied to the 12 blocked shims but not the 2 field-guarded ones, so
  `put_bot_id.__wrapped__(rest, id, body={"trash": True})` still sent the soft delete the
  wrapper exists to refuse. Same fix, now applied to both.
- A dead duplicate `__doc__` assignment left by the `-OO` fix, whose surviving copy lacked
  the route markers — a later de-duplication that kept the wrong one would have silently
  reinstated the bug.
- Stale CLI transcripts that predated the unit fix, in the very section claiming the unit is
  surfaced.
- An unverifiable "47 curated readers" figure, and a resolution note spliced mid-sentence
  into the phase-06 log so the bullet asserted both states at once.

**Left open and disclosed** (all non-blocking, all reproduced by critics): the guard binds to
a module name, so putting `closewire_client/` itself on `sys.path` loads `endpoints` unguarded
(tampering, nothing does it); `canonical_target` permits path traversal in a token the caller
must type twice; the ledger-completeness check matches one call spelling, with the SEAL
catching aliases; `dispatch_tier2` lacks `dispatch_read`'s broad exception arms, so an
unfamiliar payload gives a traceback rather than a clean exit 1 — same exit code either way;
`install()` can build a second finder if `sys.modules` is purged wholesale, which fails closed
with a `RecursionError`; and `OPERATIONS.md` still lists the 12 blocked wrappers without a
tier marker.
