# Validation evidence

One file per build phase, written by Claude as each phase completes. Phase 13 assembles
these into `docs/VALIDATION.md` — the single packet the maintainer signs off from.

## The loop

Each phase follows the same protocol (see `prompts/00-README.md`):

1. **Act through Closewire** — the client, the CLI, or the MCP server. Never through the UI.
2. **Observe in the Closebot UI** (`CLOSEBOT_UI_BASE`, default `https://app.closebot.com`)
   using the `claude-in-chrome` tools against the maintainer's logged-in session.
3. **Screenshot** the screen that corroborates the change, into `docs/validation/screens/`
   — then commit a redacted crop plus a sidecar to `docs/validation/evidence/ui/`.

   Two tiers, because the two artefacts have different jobs and different risks:

   * `screens/` holds the **raw** capture. It is gitignored, and rightly so: some captures
     carry client names and phone numbers. Never commit one.
   * `evidence/ui/` holds a **cropped, PII-free** counterpart and a `.md` sidecar naming the
     raw file, what the image shows, and what was removed.

   This step used to end at `screens/`, which meant the protocol instructed every phase to
   file its UI evidence in the one directory phase 13's packet cannot reach — thirteen cited
   images across phases 04-09, all local-only. A clause that rests solely on a gitignored
   artefact is a clause phase 13 cannot support.

   The sidecar's `redacted:` line is an **attestation**: no test can read pixels for PII.
   What is mechanical is that the attestation exists and names its source.
4. **Log** what ran, what the UI showed, and every discrepancy — including
   "this has no UI surface".
5. **Pass = UI state matches API state.** A 2xx alone is never a pass.

## Rules

- **The UI is read-only.** Observation happens there; mutation happens only through
  Closewire. Otherwise the check proves nothing about the code.
- **Throwaway targets only.** Bots created for validation are named `zz-closewire-test-*`
  so they are unmistakable, and are cleaned up by the phase that created them.
- **No secrets in artifacts.** Screenshots are build artifacts like any other: never
  capture the API-keys screen, a live key, or client PII.
- **Unverified is written as unverified.** An item that could not be checked is logged as
  blocked or no-UI-surface, never quietly upgraded to a pass.

## Status

| Phase | File | Status |
| :-- | :-- | :-- |
| 04 · Pacing layer | `04-pacing.md` | complete |
| 05 · Tier-0 read client | `05-read-client.md` | complete |
| 06 · Read CLI | `06-cli-read.md` | complete |
| 07 · Tier-1 write client | `07-write-client.md` | complete |
| 08 · Tier-2 publish/destroy | `08-tier2.md` | complete |
| 09 · Runtime + test sessions | `09-runtime.md` | **in progress** — see its frontmatter |
| 10–13 | — | not started |

This table was five phases stale — it read "05–13 not started (blocked)" while 05 through 09
were complete with logs. Phase 13 assembles the sign-off packet from this index, so a stale
index is the *summary not updated with the detail* class in the file that defines the
protocol. Each log now also declares `phase:` and `status:` in its own frontmatter, which is
what `tests/test_validation_logs.py` reads; this table is for humans and the frontmatter is
the machine-readable source.
