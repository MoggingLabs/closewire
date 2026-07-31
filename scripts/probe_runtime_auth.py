"""Probe every credential shape against the live runtime endpoint, at zero credit cost.

Phase 09 could never get a 200 out of `POST https://api.closebot.ai/message`. Every attempt
across request shapes, bot state and credential placement returned the same `410` — one per
row of the 410 evidence table in `docs/validation/09-runtime.md`, which is the count of
record. (This sentence said "Fourteen" for two rounds after the table reached nineteen; the
number is deliberately not repeated here now, since a figure transcribed into two places is
the drift this project keeps blocking on.) Rounds 9 and 10 established that the credential
*form* had never been varied either, because `live.py` hardcoded its header instead of using
`auth.py`.

This script exists for two reasons a council raised:

1. **The probes were prose.** Their results lived only in `docs/validation/09-runtime.md`,
   with no script, no capture, and nothing a reader could re-run. A critic pointed out that
   rounds 2 and 4 had both blocked on evidence presented without a capture, and that the
   round-9 probes were the same shape. Now the evidence is executable.
2. **One shape was declined as "beyond the brief".** `RESEARCH.md` documents a legacy proxy
   pairing `Authorization: Bearer` with a **`bot_id`** body field against `api.closebot.ai` —
   this exact host. Declining a free, safe probe of the only documented deployment pattern
   for the host was evasive, and a critic said so. It runs here.

**Cost: zero.** Every probe omits `message`, and `message` is the only input a reply can be
generated from — the runtime answers `440 No Message Body` when it is absent. `followup` and
`is_update`, the two flags that could produce output without it, are never set. The script
reads `usedResponses` before and after and prints both, so the claim is checked rather than
asserted.

**Pacing.** The four style probes go through `LiveMessageClient`, so they are slot-gated like
any other send. The `bot_id` shape carries a field the vendored spec does not declare, so it
cannot go through `send_message`'s whitelist; it acquires a write slot explicitly and calls
`assert_in_slot` before posting, which is exactly what `live.py` does. There is deliberately
no unpaced route to this endpoint anywhere in the project, including here.

Run: `python scripts/probe_runtime_auth.py`
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from closewire_client.auth import AUTH_STYLES, DEFAULT_AUTH_STYLE, ApiKeyAuth
from closewire_client.config import load_config
from closewire_client.endpoints import agency
from closewire_client.errors import ClosewireError
from closewire_client.live import (
    LiveMessageClient,
    message_endpoint,
    scrub_body,
)
from closewire_client.rest import RestClient

#: A synthetic contact. Never a real client contact — that is a hard stop for this project.
CONTACT = "zz-closewire-probe-contact"


def used_responses(client) -> float:
    """Read the meter through the caller's client, so it shares the one Pacer."""
    return float(agency.get_agency_usage(client)["usedResponses"])


def probe_style(config, style: str, pacer) -> str:
    """One paced send with `message` omitted, under `style`, on the shared Pacer."""
    client = LiveMessageClient(config, pacer=pacer, auth_style=style)
    try:
        reply = client.send_message(id=CONTACT)
        return f"HTTP {reply.status_code} {str(reply.raw)[:110]}"
    except ClosewireError as error:
        return f"{type(error).__name__}: {str(error)[:110]}"


def probe_legacy_bot_id(config, bot_id: str, pacer) -> str:
    """`Authorization: Bearer` + a `bot_id` body field — the `RESEARCH.md` proxy shape.

    `bot_id` is not a `MessagePayload` property, so `send_message` would reject it. The
    request is built here instead, but still inside a pacing slot with `assert_in_slot`, so
    it is not an unpaced route to the runtime endpoint.
    """
    import httpx

    endpoint = message_endpoint(config.live_base)
    payload = {"id": CONTACT, "bot_id": bot_id}  # no `message` -> nothing to answer
    headers = ApiKeyAuth(config.api_key, "authorization-bearer").headers()
    with httpx.Client(timeout=60.0) as client:
        with pacer.acquire(write=True, description="POST /message (bot_id probe)") as slot:
            if slot.dry_run_blocked:
                return "DRY RUN - not sent"
            pacer.assert_in_slot("POST", endpoint)
            response = client.post(endpoint, json=payload, headers=headers)
    # `scrub_body`, not `config.scrub`: the value-based rule alone finds only OUR key.
    # This path re-implemented half the pipeline, and its stdout is committed as
    # evidence — a third party's credential echoed in a 410 would have shipped unmasked.
    return f"HTTP {response.status_code} {str(scrub_body(config, response.text))[:110]}"


def main(argv: list[str] | None = None) -> int:
    print("# produced by: scripts/probe_runtime_auth.py --live")
    config = load_config()

    # Refuse, never correct. This line used to read `dataclasses.replace(load_config(),
    # dry_run=False)`, which silently cleared the operator's safety belt: someone who ran the
    # documented command with `CLOSEWIRE_DRY_RUN=1` set got real, chargeable POSTs to the
    # runtime endpoint. It is the same shape `live.py` refuses for `session=` and `rest.py`
    # for `_require_flag` — this codebase's house rule is structural refusal over silent
    # correction, and this script was the one place that broke it.
    if config.dry_run and "--live" not in (argv if argv is not None else sys.argv[1:]):
        print(
            "probe_runtime_auth.py: CLOSEWIRE_DRY_RUN is set, so every probe would be "
            "suppressed and the run would prove nothing.\n"
            "  This script used to clear the flag for you. It no longer does: that turned a "
            "dry-run safety belt into five real, chargeable POSTs.\n"
            "  Re-run with --live to waive the flag for this invocation only, or unset "
            "CLOSEWIRE_DRY_RUN.",
            file=sys.stderr,
        )
        return 2
    if config.dry_run:
        config = dataclasses.replace(config, dry_run=False)

    # ONE Pacer for the whole invocation. Each probe used to build its own — the client, the
    # `bot_id` path, and every `RestClient(config)` — and hourly budget counters are
    # per-instance, so a single run granted itself six fresh hourly write budgets. An
    # operator who set a 1/hour ceiling as a credit guard got six writes. `live.py`'s own
    # constructor docstring says omitting `pacer=` is "almost never what you want".
    client = RestClient(config)
    pacer = client.pacer

    before = used_responses(client)
    print(f"usedResponses BEFORE: {before}")
    print(f"endpoint: {message_endpoint(config.live_base)}")
    print(f"contact:  {CONTACT} (synthetic)\n")

    print("-- credential form, `message` omitted --")
    for style in AUTH_STYLES:
        print(f"  {style:24s} -> {probe_style(config, style, pacer)}")

    empty = dataclasses.replace(config, api_key="")
    # `DEFAULT_AUTH_STYLE`, not the literal: style names and header names are the same string
    # up to case, so `tests/test_auth_provenance.py` cannot tell a typed style name from a
    # typed header name and flags both. Naming the constant is the right call regardless —
    # `config.py` had its own copy of this list until round 10, and they could drift.
    print(f"  {'no credential at all':24s} -> {probe_style(empty, DEFAULT_AUTH_STYLE, pacer)}")

    print("\n-- RESEARCH.md legacy proxy shape: Bearer + `bot_id` body --")
    from closewire_client.endpoints import bot as bot_ep

    listed = bot_ep.get_bot(client)
    ids = [b.get("_id") or b.get("id") for b in (listed if isinstance(listed, list) else [])]
    target = next((i for i in ids if i), None)
    if target is None:
        print("  skipped: no bot on the account to name in `bot_id`")
    else:
        print(f"  bot_id={target} -> {probe_legacy_bot_id(config, target, pacer)}")

    after = used_responses(client)
    print(f"\nusedResponses AFTER:  {after}")
    print(f"CREDITS SPENT: {after - before}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
