# Handoff — resuming Closewire in a new session / on another machine

Read this first, then `docs/validation/09-runtime.md`.

## Where the work stands

Phases **04–09 are built**; phases **10–13 are not started**. Phase 09 is **not signed off**.

| | |
|---|---|
| Suite | 306 tests, `python scripts/ci.py` green (6 offline checks) |
| Live tier | `python scripts/ci.py --live` green (8 checks, read-only GETs) |
| Council | 15 rounds. Best result **3 PASS / 2 BLOCK** (round 15). The standing rule is **unanimous**, so phase 09 has never passed |
| Round 15 blocks | Fixed, **not yet reviewed**. Round 16 has not been convened |

## Setup on a new machine

```bash
git clone git@github.com:MoggingLabs/closewire.git && cd closewire
python -m pip install -e ".[dev]"
cp .env.example .env      # then paste CLOSEBOT_API_KEY into .env
python scripts/ci.py      # offline, no credentials needed — should be all green
python scripts/ci.py --live   # needs the key; read-only, ~9 min
```

`.env` is gitignored and must never be committed. The key is not in this repo.

## The one thing blocking phase 09

**`send_message` has never returned 200.** Nineteen live attempts, all `410`, at zero credit
cost. Varied and ruled out: three credential header forms, the `api_key` body form, no
credential at all, the legacy `bot_id` proxy shape, a real bot, the bot unlocked and active,
`bot=` set and omitted, and — most recently — the whole account being live rather than
deactivated.

**The one variable never tested is the contact `id`.** Every probe used
`zz-closewire-probe-contact`, which exists nowhere. The 410 text leads with *"Account not yet
connected to a bot"*, and `prompts/RESEARCH.md` records that the runtime resolves contacts
inside an integrated GoHighLevel source. If the endpoint requires a contact that actually
exists in a connected CRM, every probe so far was always going to 410.

Testing that needs a **real client contact**, which the runner prompt lists as a hard stop.
Two ways forward, both needing the operator:

1. Ask Closebot support why `POST api.closebot.ai/message` 410s for this account.
2. Observe the Closebot web app's own network traffic during a real conversation to see the
   payload shape and what a working `id` looks like. **Diagnostic only** — internal endpoints
   are undocumented and session tokens are short-lived, so nothing should be *built* on them.

Reproduce the current state: `python scripts/probe_runtime_auth.py --live` (zero credits —
every request omits `message`). Latest capture: `docs/validation/evidence/`.

## Open items needing the operator

- **A hard stop was tripped and must not recur.** `probe_runtime_auth.py` picks "the first bot
  on the account" for its `bot_id` probe. With the throwaway deleted, that is now a **real
  client bot** (`bot_4D0HRW9R9UC7UTY9`). It was a message-less POST that spent nothing, but the
  script should be pinned to an explicit id before it runs again.
- Whether to convene **round 16** or accept phase 09 and move to phase 10.
- Whether the **MCP server** (phase 11) is still in scope — `README.md` leads with it and
  `mcp_server/` is scaffold only.

## Standing rules for whoever picks this up

These are the operator's, and they are not optional:

1. **Every fix ships a gate that bites** — a mechanical check for the *class*, proven to fail
   when the defect is reintroduced. Not a check for the instance.
2. **Root cause, never the symptom.** Round 13's council ruled INSTANCE ONLY on six of nine
   fixes; round 14 re-did them properly with five root-cause agents.
3. **All five critics must raise no blocking objection** before advancing. Unlimited rounds.
4. **Validate through `scripts/ci.py`** — you and every critic. Never a hand-picked subset.
5. **Dry-run first, spend the fewest sends possible.** The phase overran its ~20-send budget
   at 26.

## Traps this repo has, that will cost you time

- **`git checkout <file>` EMPTIES files here.** Everything was staged `git add -N` against a
  single scaffold commit, so index blobs were empty. Use `cp` backups when mutation-testing.
  (Less dangerous after the first real commit, but check before trusting it.)
- **Heredocs mangle `\n`.** `python - <<'PY'` with `\n` inside a replacement string writes a
  real newline into the file and breaks it. Use the Edit tool for anything containing `\n`.
  This cost four separate repairs.
- `docs/validation/screens/` is gitignored (client PII). Committed, redacted UI evidence lives
  in `docs/validation/evidence/ui/` with sidecars.

## Known-broken vendor endpoints

Documented as deviations in `docs/validation/09-runtime.md`: `metrics.logs` (400 unfiltered,
timeout filtered), `GET …/testSession/messages/{leadId}` (never returns), `DELETE /bot/{id}`
(500), `GET /bot/{id}/export` (500 on new bots), `metrics.actions` (intermittent), and
`GET /bot/{id}/steps` requiring `botVersion` the spec calls optional.
