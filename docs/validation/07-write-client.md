---
phase: 07
status: closed
suite_total: 112
---
# Phase 07 — Tier-1 write client

**Status: BLOCKED on a hard stop.** Every deliverable is implemented and the write path is
proven end-to-end against the live API, but validation step 2 — creating the throwaway bot
`zz-closewire-test-07` — **cannot be completed**, because the account is at its plan
ceiling and the only ways past it are both explicit hard stops. Details in
[Blocked](#blocked-plan-ceiling). The rest of the phase is complete and validated.

---

## Deliverable 1 — write functions

`closewire_client/writes/` — a package separate from `endpoints/`, so the tier is visible
in the import path rather than asserted in a docstring.

What the read-only guarantee covers, stated precisely: `endpoints/` also holds **generated**
wrappers for the whole API surface, and those do contain `POST`/`DELETE` calls, so it is not
wholly free of mutating verbs. What holds — and is re-checked mechanically each round — is
narrower: the six **curated** read modules contain exactly one mutating verb (`leads.search`,
a POST whose semantics are a pure read, hence the sole entry in `READ_ONLY_POSTS`), and
nothing in `endpoints/` or `cli/` imports `writes/`, so no read path can reach a Tier-1
write.

Request bodies below were resolved by following
`paths[<path>][<method>].requestBody.content["application/json"].schema` to its `$ref`.
Every one of these schemas sets `additionalProperties: false`, so an extra key is a protocol
error rather than a harmless hint — which is why the functions spell fields out instead of
forwarding `**kwargs`.

| Function | Route | Request body | Notes |
|---|---|---|---|
| `bots.create` | `POST /bot` | `CreateBotInput` | `None` fields omitted, not sent as null |
| `bots.create_with_ai` | `POST /bot/ai` | `AiCreateBotInput` | `description` **is** the prompt — deviation 13 |
| `bots.update` | `PUT /bot/{id}` | `UpdateBotInput` | allowlisted; partial; `trash=True` refused |
| `bots.duplicate` | `POST /bot/{id}/duplicate` | *(none)* | no body; **presumed** to consume a plan slot — untested |
| `bots.save` | `POST /bot/{id}/save` | `SaveBotInput` | validates locally first; surfaces `invalidPaths` |
| `bots.set_steps` | `POST /bot/{id}/save` | `SaveBotInput` | alias of `save` — deviation 11 |
| `bots.validate` | *(local)* | — | no *standalone* route; server verdict rides in `save` — deviation 10 |
| `bots.save_tools` | `POST /bot/{id}/saveTools` | `ToolInputDto[]` — an **array** | deviation 14 |
| `bots.attach_source` | `POST /bot/{id}/source/{sourceId}` | `AttachSourceInput` | deviation 15 |
| `bots.detach_source` | `DELETE /bot/{id}/source/{sourceId}` | *(none)* | unlinks only; source survives |
| `personas.create` | `POST /persona` | `CreatePersonaInput` | `aiProviderPreferences` required — deviation 12 |
| `personas.update` | `PUT /persona/{id}` | `UpdatePersonaInput` | allowlisted; partial; `trash=True` refused |

An earlier revision of this table said "body undeclared in spec; passed through" for
`create_with_ai` and `save_tools`. Both were **false** — they are the very claims deviations
13 and 14 record — and two round-2 critics caught them still standing here after the
deviations section had been corrected. That is this repo's recurring failure mode exactly:
the detailed section gets fixed, the summary a reader hits first does not. The cross-
references above are now to the real deviation numbers (10–15); the earlier "deviation 1/2/3"
pointers were left over from a renumber and resolved to nothing.

**Prerequisite landed first.** Three phase-05/06 critics required a `write=False` guard
before any mutation shipped. `rest.py` now restricts that override to
`READ_ONLY_POSTS = {"/lead/search"}`; `/bot`, `/bot/abc/publish` and
`/agency/billing/refill` are all refused with `write=False`, while `write=True` is always
allowed.

## Deliverable 2 — offline graph validation

`closewire_client/jobflow.py` checks a flow graph against the vendored node catalogue
before it costs a paced write.

- Live published 26-node graph → **0 errors**, 2 warnings, both legitimate (an undeclared
  `globalAgentTools` key, and 7 genuinely orphaned nodes in that flow).
- All five deliberate defects caught: unknown node type, duplicate id, dangling edge,
  no `Source` node, unreachable node.
- **No required-field check**, deliberately: the catalogue carries no `required` flag, so
  inventing one would reject valid graphs.

---

## Two defects found and root-caused

### 1. The dry run did not log payloads

Phase 07 requires the dry run to log "the exact payloads". It did not — it emitted
`DRY RUN — suppressed write POST /bot`, method and path only.

**Root cause:** the suppression log lives in `pacing.py`, which is handed only a
description string. It never sees a body, so it *cannot* report one. The body exists only
in `RestClient.request`.

**Fix:** log the payload in `rest.py:333`, the one place that holds it — redacted through
the same `redact_secrets` + `Config.scrub` path the response boundary uses, because a
request body can carry a credential and a dry run must not become the one code path that
prints it. Proven by check 9 below.

### 2. A plan refusal would have tripped the auth circuit breaker

Closebot answers "your plan is maxed" with **HTTP 401 `{"error": "upgrade required"}`** —
the same status as a revoked key. `pacing.py` counted every 401 toward
`breaker_auth_threshold` (default 3), so **three** attempted creates on a maxed plan would
have opened the breaker and **persisted a halt to disk**, requiring a manual reset, over a
perfectly valid API key. The same flaw made an entitlement `403` retry with backoff, since
403 is in `RETRYABLE_STATUSES`.

**Root cause:** one cause, two symptoms — `note_response` was given only a status code, and
this API overloads 401 to mean two unrelated things, so the status alone cannot classify
the failure.

**Fix:** `note_response` now accepts `body_text` and consults
`is_entitlement_refusal()` before applying either rule. Marker matching is deliberately
narrow (`"upgrade required"`, `"upgrade your plan"`, `"plan limit"`, `"quota exceeded"`)
and bounded to the first 400 characters — the dangerous direction is a false positive,
which would stop the breaker tripping on a genuinely bad key. A 401 with **no** body is
still treated as an auth failure.

Live proof, against the real API:

```
breaker_auth_threshold = 3
attempt 1: ClosebotAPIError | breaker=closed recent_auth=0
attempt 2: ClosebotAPIError | breaker=closed recent_auth=0
attempt 3: ClosebotAPIError | breaker=closed recent_auth=0
attempt 4: ClosebotAPIError | breaker=closed recent_auth=0
```

Before the fix, attempt 3 opens the breaker and writes a halt file.

---

## Tier-1 / Tier-2 boundary

`UpdateBotInput` and `UpdatePersonaInput` both declare a `trash` boolean. A `**fields`
passthrough would have carried `trash=True` — a soft delete — down the Tier-1 lane. The
guard lives once, in `writes/_tier.py`, because the hole is the passthrough pattern and it
exists in the same shape in two places. `trash=False` (restoring) is allowed.

---

## Validation

### Dry run — `scripts/verify_writes.py`, 10 check groups, all pass

"Sends nothing" is proved **below** the layer under test: an `httpx` transport that raises
on any request at all. Trusting `DRY_RUN_RESULT` would be circular — it is the flag being
tested.

| # | Check | Result |
|---|---|---|
| 1 | All 12 deliverable functions exist | pass |
| 2 | Every write returns the dry-run sentinel | 11/11 |
| 3 | **Zero requests reached the transport** | `attempts=[]` |
| 4 | **Every payload logged individually**, one log line per write | 11/11, `logged=11` |
| 5 | All 11 charged to the **write** lane, none to the read lane | `ops=11, writes=11` |
| 6 | **CONTROL — with dry run off, the tripwire fires** | `attempts=['POST /bot']` |
| 7 | `trash=True` refused on both; `trash=False` allowed | pass |
| 8 | `save()` refuses a graph with errors | pass |
| 9 | Dry-run payload log masks a nested `apiKey` and the config key | pass |
| 10 | `write=False` refused off-allowlist; non-bool flags rejected | pass |

Check 6 exists because phase 06 shipped a redaction check that could not fail. Without it,
checks 2–5 would pass just as happily against a transport that was never wired up.

**Check 4 was itself vacuous in round 1** and a critic proved it: the bot name it searched
for was a strict substring of the persona name, so one persona payload satisfied it, and the
path it searched for is emitted by `pacing.py` whether or not a body is logged at all.
Logging *only* persona bodies passed all three fragments. It now gives each of the 11 writes
a unique, mutually non-substring marker — carried in that call's own payload for the nine
writes that have one, and in the path for the two that send no body at all (`duplicate`,
`detach_source`), which the harness labels as such. It captures only
the `closewire.rest` logger, and asserts one payload log per write. Re-running the critic's
mutation (log only persona bodies) turns **7 of 11 markers red**, and logging no body at
all turns **9** red — either way it fails, where all three old fragments passed. The "markers don't overlap" property is
itself asserted, so the check cannot quietly go vacuous again.

**Check 10 exists** because a critic replaced the `write=False` guard with `if False:` and
every other check stayed green — the prerequisite this phase landed before shipping any
mutation had no regression guard anywhere.

### Test suite

`112 passed`, from 75 at the start of the phase:

| Added | Why |
|---|---|
| 11 pacing tests | the entitlement/breaker work, including `test_real_401_still_trips_the_breaker` — the control that stops the fix degenerating into "never trip" |
| 17 `tests/test_jobflow.py` | `jobflow.py` had **no tests at all** in round 1; its guarantees rested on one ad-hoc run against a live graph that is not committed |
| 9 redaction tests | the pair/string redaction rules had **no regression guard**: a critic stubbed the whole round-1 fix out and the suite still went green |

`scripts/verify_reads.py` and `scripts/verify_cli.py` both still exit 0 against the live
API — the redaction change touches every response, not just dry-run logs, so phases 05/06
were re-verified rather than assumed.

### Live API

| Operation | Result |
|---|---|
| `bots.create('zz-closewire-test-07')` | **HTTP 401 "upgrade required"** — plan ceiling |
| `personas.create(...)` | created `zz-closewire-test-07-persona` (id withheld — see Cleanup) |
| `personas.update(...)` | applied; `GET` read-back confirms `description` and `typoPercent=7` |

### UI cross-check — `app.closebot.com/bots`

Personas and Job Flows are both panels on the **Agents** page; `/personas` is a 404.

- **The live persona is present**: `zz-closewire-test-07-persona`, 0 Jobs.
- **The live update is visible**: hovering a card shows the description
  "UPDATED by Closewire phase 07 live chec…". → `screens/07-persona-updated.jpg`

  ⚠️ **This screenshot does not by itself prove *which* card the tooltip belongs to.** A
  round-2 critic measured it: the tooltip box spans x 48–282, which is 3 px from one *real
  client* persona's info icon and at the right edge of the throwaway's, and the frame has no
  caret or hover highlight to settle it. Since this document already withdrew one
  non-discriminating UI claim, the same standard applies here.

  **The claim is instead carried by a Tier-0 read**, which is authoritative and unambiguous:

  ```
  personas in account: 3
    'Sarah'                        typoPercent=8  desc='Booking Agent'
    'zz-closewire-test-07-persona' typoPercent=7  desc='UPDATED by Closewire phase 07 live check.'
    'Sam'                          typoPercent=5  desc='This persona was automatically generated…'
  ```

  Both fields `personas.update` set are present on the throwaway persona and on nothing else,
  and the two real client personas carry different values — so the update landed where
  intended and touched nothing that was not ours.
- **The bot ceiling is real**: "🔒 1 of 1 flows used", **Add Flow greyed out**, and the same
  three flows phases 05 and 06 recorded, only one of them Published.
  → `screens/07-agents-ceiling.jpg` (renamed from `07-agents-unchanged.jpg`: "unchanged" was
  the reading withdrawn below, and leaving it in the filename kept the retracted claim alive)

#### What the UI does **not** prove, and why

An earlier draft of this document claimed the screenshot showed "nothing changed by the dry
run". **That was a non-sequitur and has been withdrawn.** A critic pointed out that the
observation has no discriminating power:

- `POST /bot` was refused server-side by the plan ceiling regardless, so the flow list would
  read exactly three whether the dry run suppressed the send or shipped it live;
- there is no before-image to compare against, only one post-hoc screenshot;
- the one artifact a leaked dry-run write *could* have created in this account is a persona
  (personas are not plan-capped), and the frame cited as "nothing changed" contains the
  persona — which the live write legitimately created. The UI cannot separate the two.

The dry-run negative is therefore carried **entirely by check 3**: a transport that raises
on any request, with check 6 proving that transport does fire when a send is real. That is
strictly stronger evidence than a screenshot, but it is offline evidence, and this section
no longer claims otherwise.

One further limitation, stated plainly: the dry-run harness runs against a throwaway key and
a tripwire transport, so it never aimed at the live account at all. "`CLOSEWIRE_DRY_RUN=1`
against the live config changes nothing in this account" is **not** established by anything
here. What is established is that no write reaches a transport under dry run.

---

## Blocked: plan ceiling

`GET /agency/usage` reports `usedBots: 1 / maxBots: 1`; the UI corroborates with "1 of 1
flows used" and a disabled **Add Flow**. `POST /bot` is refused server-side.

Before attempting the write I confirmed no spend was possible:
`overBillingEnabled: false`, `usageBillingEnabled: false`, `autoRefillEnabled: false`,
`balance: 0`. Exceeding the plan cannot bill this account — so the attempt was safe, and it
neither created nor charged anything.

The only two routes past the ceiling are **both explicit hard stops**:

1. Free a slot by deleting or deactivating an existing flow — all three are real client
   flows; scope is `zz-closewire-test-*` only.
2. Upgrade the plan — spending.

So validation step 2 and everything gated behind it (live `bots.save` of a flow graph,
`attach_source` on a throwaway bot) are **not done, and cannot be done without a
decision that is yours to make**. They are not reported as passing.

What that leaves unproven, stated precisely:

- The bot-side writes are proven for **lane, suppression, budget accounting and redaction**
  (checks 2–10), and their **payload shapes now match the spec schemas**, re-resolved field
  by field. They have **not** been accepted by the live API. An earlier draft said "verified
  for shape" — true only in the weak sense of matching a declared schema, and written while
  two of those shapes were in fact wrong. Restated rather than left standing.
- The persona-side writes carry the end-to-end live proof that transport, pacing, the write
  lane and error handling work against the real service.
- `bots.duplicate` is asserted to consume a plan slot. That is inference from the UI's
  disabled **Add Flow**, not a tested fact — and it was not attempted, because the only
  duplicable flows are real client flows.

### Cleanup owed

`zz-closewire-test-07-persona` remains in the account. Deleting it is Tier-2, which lands in
phase 08 — and this phase's own guard refuses `trash=True` by design. It is in the
sanctioned throwaway namespace, and findable by that name.

(An earlier draft printed the persona's live object id here. Removed: `prompts/07` says
"never commit real ids/keys", and no other phase log records one.)

---

## Spec deviations found (10–15 overall; six from this phase)

10. **No *standalone* validate route — but server-side validation does exist.** The phase
    brief lists `bots.validate` and RESEARCH.md lists "validate flow". No path in the spec
    contains `valid`, and the `/bot` family has no validate operation. **However**, an
    earlier revision of this document claimed the spec had been searched for `valid` with
    "zero matches across all 126 operations", and concluded no server-side validation
    exists at all. **That was false**, and two critics caught it independently:
    `POST /bot/{id}/save` answers with `SaveBotResponse = {version, invalidPaths[], message}`
    on **both** 200 and 400, and `invalidPaths` *is* the server's verdict on the graph.
    `bots.save` was discarding it. It now surfaces it (`bots.invalid_paths`, and an opt-in
    `require_valid=True`), and `bots.validate` remains the local pre-flight it should be.

    Root cause of the false claim: the search was run over path *names* only, never over
    component schemas — the same guess-don't-resolve habit behind deviations 13 and 14.
11. **`set_steps` has no version parameter.** The brief signature is
    `set_steps(id, version, graph)`. `GET /bot/{id}/steps` has no `PUT` counterpart, and
    `POST /bot/{id}/save` takes exactly `{botSteps}` — no version. Kept as an honest alias
    of `save` rather than inventing a route or accepting an argument that would be ignored.
12. **`aiProviderPreferences` is required on `POST /persona`** despite the spec marking no
    field on `CreatePersonaInput` as required. Omitting it returns a 400 naming it. It is
    now a named parameter with a default, so the requirement is visible at the call site
    instead of discovered from a deserialization error.

13. **`POST /bot/ai` takes `AiCreateBotInput {name, description, category, folderId}`**, with
    `additionalProperties: false`, where `description` **is the prompt** ("The prompt to use
    to create the new bot"). The first implementation sent `{"prompt": ...}` — a forbidden
    key, with the prompt-carrying field left empty — on the false premise that the spec
    declared no schema for this body.
14. **`POST /bot/{id}/saveTools` takes a JSON *array* of `ToolInputDto {type, enabled,
    options}`**, not an object. The first implementation accepted an arbitrary object and
    the harness exercised it as `{"tools": []}`, which would have serialised to `["tools"]`.
    It was never caught because dry-run suppressed the send.
15. **`POST /bot/{id}/source/{sourceId}` takes `AttachSourceInput {tags, channels,
    personaNameOverride, enabled}`**, also `additionalProperties: false`. `attach_source`
    was forwarding `**body` unchecked. Found by a deep-dive, not by a critic.

Deviations 13–15 share deviation 10's root cause: request bodies were looked up by
*guessing* a component name (`SaveBotToolsInput`, `AddBotSourceInput`) and, when the guess
missed, recorded as "the spec declares no schema" — instead of resolving
`paths[…].requestBody.content[…].schema.$ref`. All eight `/bot` write bodies have now been
re-resolved the correct way and are tabulated in `writes/bots.py`'s module docstring.

Also observed: the plan's `usedBots` counts **published/active** flows, not total bots —
the account holds three flows but reports `usedBots: 1`.

---

## Council verdict

### Round 1 — 4 BLOCK / 1 PASS

Five adversarial Opus 5 critics, each with a distinct lens, judging only against phase 07's
deliverables. Every finding below was independently reproduced by the critic who filed it.

| # | Lens | Verdict |
|---|---|---|
| 1 | Deliverable conformance | BLOCK |
| 2 | Write-path safety | BLOCK |
| 3 | Validation integrity | BLOCK |
| 4 | Regression / tier boundary | PASS |
| 5 | Blocked-work honesty | BLOCK |

**Blocking findings.** Seven were fixed; the eighth is unfixable without a hard stop:

1. **`SaveBotResponse.invalidPaths` exists** (critics 1 and 5, independently) — the "no
   server-side validation" claim was false and `save` discarded the server's verdict.
2. **`create_with_ai` sent a forbidden key** and never populated the prompt field (critic 5).
3. **`save_tools` sent an object where the spec requires an array** (critic 5).
4. **`write=0` bypassed the write-lane guard** (critic 2) — the guard tested identity while
   every consumer tested truthiness, so a falsy non-`False` value routed a `POST /bot` onto
   the concurrent read lane, unbudgeted and unsuppressed. Demonstrated reaching the
   transport under dry run.
5. **The dry-run log leaked credentials in name/value pairs** (critic 2) — a catalogue-legal
   `WebHook` header `{"Header": "Authorization", "Value": "Bearer …"}` printed verbatim at
   WARNING, because redaction matched key names only.
6. **The UI "nothing changed" claim was a non-sequitur** (critic 3) — withdrawn; see above.
7. **Check 4 could not fail** (critic 3) — fixed and re-proven by mutation.
8. **10 of 12 functions have no live/UI evidence** (critic 1) — genuine, unfixable without
   a hard stop; see [Blocked](#blocked-plan-ceiling). Not reported as passing anywhere.

**Root causes addressed, not symptoms.** Four deep-dive agents with disjoint file ownership
traced each cluster to its cause and found more than the critics had:

- *Guess-don't-resolve* (findings 1–3) produced **eight** false docstring claims, not three:
  `attach_source` and `update` were also forwarding unchecked keys into
  `additionalProperties: false` bodies.
- *Unnormalised bool flags* (finding 4) also affected **`allow_secrets`** — the dangerous
  one, read for truth, so `allow_secrets="false"` would have silently **granted** the
  unmasked-credential capability. Flags are now rejected rather than coerced, and an
  import-time assertion fails the build if a future bool flag is added unvalidated.
- *Name-only redaction* (finding 5) was a structural blind spot, not a WebHook quirk; it
  also missed `authToken`/`authorizationCode`, real credential fields in the vendored spec.
- The entitlement fix from round 1 had itself introduced a **stale-backoff regression** and
  an **echo-injection weakness** (caller text in an echoed error body could permanently stop
  the auth breaker tripping on a revoked key). Matching now keys off the server's own error
  fields. `jobflow.py` had four id-keying defects, including duplicate ids going undetected
  for integer ids.

### Round 2 — 3 PASS / 2 BLOCK

Same five lenses, re-run against a frozen tree, each critic asked to verify its own round-1
findings were *genuinely* fixed and to hunt for defects the fixes introduced.

| # | Lens | R1 | R2 |
|---|---|---|---|
| 1 | Deliverable conformance | BLOCK | **PASS** |
| 2 | Write-path safety | BLOCK | BLOCK |
| 3 | Validation integrity | BLOCK | **PASS** |
| 4 | Regression / tier boundary | PASS | **PASS** |
| 5 | Blocked-work honesty | BLOCK | BLOCK |

Critic 3 re-ran its own round-1 mutation and confirmed check 4 now turns 9 of 11 markers
red. Critic 4 confirmed all four of its round-1 findings fixed by reproduction, found no
integration regression between the four parallel rewrites, and confirmed the breaker still
opens on a genuinely revoked key. Critic 2 could not defeat the flag normalisation across
ten non-bool candidates.

**Round-2 blocking findings, all fixed:**

1. **Redaction inverted on `Key`/`Value` pairs** (critic 2 BLOCKING; critic 4 found it
   independently). `"key"` is in `SECRET_FIELDS` because it is a real credential field
   elsewhere — but it is also the literal name of the *label* slot of the catalogue's
   `BotAtomicNodeWebHookParameter`. So `{"Key": "page", "Value": "3"}` masked the label and
   left the value. Two failures: read-modify-write of any flow carrying a WebHook parameter
   became impossible (`contains_redacted` refused the save — and editing flow steps is this
   phase's stated goal), and `{"Key": "auth", "Value": "sk_live_…"}` masked `auth` while
   printing the credential.
2. **`WebHook.JsonBody` leaked** (critic 2 BLOCKING). A credential inside a JSON-bearing
   *string field* survived redaction and printed verbatim at WARNING in the dry-run log —
   though the identical bytes arriving as a whole text body were correctly masked.
   `find_unredacted` agreed it was clean, so the read/CLI harnesses structurally could not
   have caught it.
3. **The deliverable table still carried the two false spec claims** (critic 5 BLOCKING;
   critic 4 independently). Deviations 13 and 14 had been written up correctly while the
   summary table 220 lines earlier still said "body undeclared in spec" — this repo's
   recurring failure mode, caught again.
4. **The persona-update UI evidence did not discriminate** (critic 5 BLOCKING). Pixel
   measurement put the tooltip 3 px from a real client persona's icon. Replaced with an
   authoritative Tier-0 read-back; see the UI section.

Root cause of 1 and 2, addressed rather than special-cased: the field-name rule and the pair
rule were two independent passes with overlapping vocabularies and no notion of *which slot*
of a structure they were looking at. A name that is a credential in one position is a
harmless label in another. Slot roles are now assigned **once per mapping**, before any rule
runs, and both `redact_secrets` and `find_unredacted` ask the same single question — so the
detector cannot drift from the redactor.

Checked in both directions, because over-redaction breaks writes and under-redaction leaks:

- **No credential lost.** Across both spec files, the node catalogue and eight
  credential-bearing payloads, no path holding a real credential is unmasked by the new
  rule (0 lost in `openapi.json` 14→14 and `openapi.live.json` 20→20). Stated precisely
  because an earlier draft said "no path masked by the old rule is unmasked" — literally
  false, and contradicted by the next bullet: 17 catalogue paths *are* now unmasked, and
  that is the point, since all 17 are harmless labels (`UseAI`, `BodyType`, …). A source row's `key`
  is still masked — the demotion applies only in a label slot with no competing label.
- **20 paths no longer over-redacted**, all of them pair labels (17 catalogue conditions,
  3 WebHook `Key`s) — which is what unblocked read-modify-write.
- **`find_unredacted` now reports the JsonBody path** it previously called clean.

**A side effect worth recording.** The `static_schema` bypass — which disables scrubbing
outright — existed because the catalogue uses `key` for property names and scrubbing
corrupted them. With slot-awareness that is no longer true: the vendored catalogue now
scrubs to a **byte-identical** document (17 values changed before, 0 after, verified
directly). The bypass is therefore a no-op today. It is kept for now because deleting a
safety-relevant branch late in a phase is not a change to make without review, but a
scrubbing bypass that provably does nothing is a latent hazard rather than a neutral one —
**phase 08 should delete it.**

### Round 3 — 5 PASS / 0 BLOCK ✅ **unanimous**

| # | Lens | R1 | R2 | R3 |
|---|---|---|---|---|
| 1 | Deliverable conformance | BLOCK | PASS | **PASS** |
| 2 | Write-path safety | BLOCK | BLOCK | **PASS** |
| 3 | Validation integrity | BLOCK | PASS | **PASS** |
| 4 | Regression / tier boundary | PASS | PASS | **PASS** |
| 5 | Blocked-work honesty | BLOCK | BLOCK | **PASS** |

Every critic re-derived its verdict independently rather than assuming its round-2 result.
Notably, critic 4 diffed a reconstructed pre-rewrite redactor against the current one over
**every live payload fetched raw** — bot list, sources, personas, all bot details and all
version graphs — and found **0 paths masked before and not now, 0 newly masked**, i.e. the
redaction rewrite is a provable no-op on the real account. Critic 2 confirmed all three live
graphs (34/26/39 nodes) now scrub to themselves, so read-modify-write of every real bot is
viable. Critic 4 also tripped and persisted the breaker with a bogus key against the live
API, confirming the entitlement work did not weaken it.

**Open non-blocking items, carried forward rather than silently dropped:**

- Redaction vocabulary misses `X-Api-Token`, `Cookie`, `X-Auth`, `PRIVATE-TOKEN` — the
  *structural* defect is fixed, the name list is tuning.
- A `JsonBody` written as a Closebot template (`{{lead.phone}}`) does not parse as JSON, so
  an embedded credential still prints in the dry-run log. `redact_text` behaves identically
  for an unparseable body, so the two paths at least agree now.
- Sole-label demotion means `{"key": "<token>", "value": …}` would go unmasked. Three
  critics independently searched both spec files, the catalogue and all live responses and
  found **zero** such shapes, but the residual is real.
- `rest.py`'s import-time flag assertion iterates a hard-coded tuple, so a brand-new public
  method is unchecked.
- Nested objects inside `attach_source(tags=…)` and `update(followUpSequences=…)` are not
  validated per-entry the way `save_tools` validates its array.
- Nothing mechanically enforces "`endpoints/`/`cli/` must not import `writes/`" — it is
  checked each round by hand and by critics, not by a test.

**Also recorded honestly:** during round 2 the Chrome extension disconnected, giving **3
consecutive browser failures** — a documented hard stop. Browser work stopped there rather
than being retried. It cost nothing: the only outstanding UI question was which card a
tooltip belonged to, and a Tier-0 `GET /persona` answered it better than any screenshot
could. No claim in this document depends on a frame that was never captured.
