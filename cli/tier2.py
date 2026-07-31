"""Tier-2 CLI commands: publish, delete, and wallet refill.

Every command here is dangerous, so they share one shape:

* **No flags is never an action.** ``closewire bots delete <id>`` with no ``--confirm``
  prints what *would* happen and exits non-zero. Phase 08 requires the default to be a safe
  no-op that explains itself, and a zero exit would let it pass unnoticed in a script.
* **The confirmation must echo the target.** ``--confirm`` takes a value, not a bare flag,
  and that value has to equal the bot id, persona id, or amount. A boolean flag would be
  equally satisfied for any target, which is the mistake worth catching.
* **Refusals are exit code 3**, distinct from a genuine failure (1) or an open breaker (2),
  so a caller can tell "I forgot to confirm" from "the API rejected it".

**Stdout carries results; nothing else.** Every message about a call that did *not* happen
— a refusal, or a send suppressed by ``CLOSEWIRE_DRY_RUN`` — goes to stderr, in both output
modes. Two defects came from breaking that rule in opposite directions: the safe no-op
preview was printed to stdout, so ``closewire bots delete <id> --json | jq`` was handed
prose; and a dry-run call printed the completed-action summary ``deleted <id>`` and exited
0, which is a lie the operator has no way to catch. Both are reported through
:func:`_refuse` and :func:`_report` now, so there is one place where "intent" and "outcome"
are told apart rather than six.

These parsers attach to the **same** group parsers ``cli.reads`` created — see
:func:`cli.reads.add_read_parsers` for why the group cannot be declared twice.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from typing import Any

from closewire_client.errors import ClosebotAPIError
from closewire_client.pacing import PacingHalt
from closewire_client.rest import DRY_RUN_RESULT
from closewire_client.tier2 import ConfirmationRequired
from closewire_client.tier2 import billing as t2_billing
from closewire_client.tier2 import bots as t2_bots
from closewire_client.tier2 import leads as t2_leads
from closewire_client.tier2 import personas as t2_personas
from closewire_client.tier2 import sources as t2_sources

__all__ = ["add_tier2_parsers", "dispatch_tier2", "TIER2_COMMANDS", "EXIT_REFUSED"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_HALTED = 2
#: A guard refused. Distinct from a failure: nothing was sent and nothing is wrong.
EXIT_REFUSED = 3

#: Every Tier-2 command, for `--help` and the validation log.
TIER2_COMMANDS = (
    "bots publish", "bots delete", "bots export",
    "personas delete", "sources delete", "leads delete",
    "billing balance", "billing options", "billing transactions", "billing refill",
)

#: Groups this module extends rather than creates.
_DELETE_TARGETS = {
    "personas": ("persona", "PERSONA_ID"),
    "sources": ("source", "SOURCE_ID"),
    "leads": ("lead", "LEAD_ID"),
}


def add_tier2_parsers(
    sub: "argparse._SubParsersAction",
    groups: dict[str, "argparse._SubParsersAction"],
    json_opt: argparse.ArgumentParser,
) -> None:
    """Attach Tier-2 actions to the existing groups, and create the ``billing`` group.

    Args:
        sub: The top-level subparser, for groups that do not exist yet.
        groups: Group-name → action-subparser, as returned by
            :func:`cli.reads.add_read_parsers`.
        json_opt: The shared ``--json`` parent parser.
    """
    # bots publish / delete / export — added to the group `cli.reads` already owns.
    bots_group = groups["bots"]
    pub = bots_group.add_parser(
        "publish", parents=[json_opt],
        help="Make a bot's draft live. DANGEROUS: it starts talking to real leads.",
    )
    pub.add_argument("id")
    pub.add_argument(
        "--confirm", action="store_true",
        help="Required. Publish is reversible and has one target, so a flag is enough.",
    )
    dele = bots_group.add_parser(
        "delete", parents=[json_opt],
        help="Delete a bot permanently. Requires --confirm <the same bot id>.",
    )
    dele.add_argument("id")
    dele.add_argument(
        "--confirm", metavar="BOT_ID",
        help="Must equal the bot id exactly, or the command aborts.",
    )
    exp = bots_group.add_parser(
        "export", parents=[json_opt], help="Export a bot's definition (read-only).",
    )
    exp.add_argument("id")

    # personas / sources / leads delete
    for group_name, (noun, metavar) in _DELETE_TARGETS.items():
        d = groups[group_name].add_parser(
            "delete", parents=[json_opt],
            help=f"Delete a {noun} permanently. Requires --confirm <the same id>.",
        )
        d.add_argument("id")
        d.add_argument(
            "--confirm", metavar=metavar,
            help=f"Must equal the {noun} id exactly, or the command aborts.",
        )

    # billing — a new group; no Tier-0 module owns it.
    bill = sub.add_parser("billing", help="Wallet balance, transactions, and refill.")
    bs = bill.add_subparsers(dest="action", required=True)
    bs.add_parser("balance", parents=[json_opt], help="Current wallet balance.")
    bs.add_parser("options", parents=[json_opt], help="Auto-refill and over-billing config.")
    bs.add_parser("transactions", parents=[json_opt], help="Wallet transaction history.")
    ref = bs.add_parser(
        "refill", parents=[json_opt],
        help="Top up the wallet. SPENDS REAL MONEY. Requires --confirm <the same amount>.",
    )
    ref.add_argument(
        "--amount", type=int, required=True,
        help="Amount to add. UNIT UNDOCUMENTED — the spec gives `amount` no description, "
        "while the balance field it pairs with is 'cents in USD', so this may be minor "
        "units. Verify with the smallest possible refill before relying on a larger one.",
    )
    ref.add_argument(
        "--currency", default=t2_billing.DEFAULT_CURRENCY,
        help=f"Default {t2_billing.DEFAULT_CURRENCY}.",
    )
    ref.add_argument(
        "--confirm", metavar="AMOUNT",
        help="Must equal --amount exactly, or the command aborts.",
    )


def dispatch_tier2(args: argparse.Namespace, rest, as_json: bool) -> int:
    """Run one Tier-2 command. Raises nothing: failures and refusals become exit codes."""
    try:
        return _run(args, rest, as_json)
    except ConfirmationRequired as exc:
        # Not an error — the guard did its job. Same event, and now the same reporting
        # path, as the no-flag preview below: both mean "nothing was sent, here is why".
        return _refuse(str(exc))
    except PacingHalt as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_HALTED
    except ClosebotAPIError as exc:
        print(f"\nHTTP {exc.status_code}: {exc.method} {exc.path}", file=sys.stderr)
        print(f"  {str(exc.body)[:600]}", file=sys.stderr)
        return EXIT_FAILURE
    except (ValueError, TypeError) as exc:
        return _refuse(str(exc))


# ── Reporting ─────────────────────────────────────────────────────────────────
def _refuse(explanation: str) -> int:
    """Report that a guard refused: nothing was sent, and this is why. Exit 3.

    **Always stderr, in both output modes.** A refusal is not a result. The global
    ``--json`` contract is that stdout carries JSON and *nothing else*, so printing the
    safe no-op preview there made ``closewire bots delete <id> --json | jq`` fail to parse
    — 186 bytes of prose on stdout, 0 bytes on stderr, exit 3. Under ``--json`` stdout is
    therefore left empty (there is no result to serialise) and the explanation still
    reaches a human on stderr, which is exactly what the ``ConfirmationRequired`` path
    already did correctly. One function, so the two cannot diverge again.
    """
    print(f"\n{explanation}", file=sys.stderr)
    return EXIT_REFUSED


def _was_suppressed(result: Any) -> bool:
    """Did :class:`~closewire_client.rest.RestClient` suppress this call under dry-run?

    Matched against :data:`~closewire_client.rest.DRY_RUN_RESULT` itself rather than
    against key names copied out of it: the client returns
    ``dict(DRY_RUN_RESULT, method=…, path=…)``, so every key the sentinel declares must be
    present and equal. If the sentinel ever grows a field, this follows it instead of
    quietly matching less.
    """
    return isinstance(result, dict) and all(
        result.get(key) == value for key, value in DRY_RUN_RESULT.items()
    )


def _dry_run_notice(result: dict[str, Any], *, did: str, target: Any) -> str:
    route = " ".join(str(result[k]) for k in ("method", "path") if result.get(k))
    return (
        "DRY RUN — NOTHING HAPPENED.\n"
        f"  CLOSEWIRE_DRY_RUN is set, so {route or 'the request'} was never sent.\n"
        f"  Had it been sent, it would have {did} {target}.\n"
        "  Unset CLOSEWIRE_DRY_RUN (or set it to 0) to perform this for real."
    )


def _report(result: Any, *, as_json: bool, did: str, target: Any) -> int:
    """Report what a Tier-2 mutation **did**, never what it was going to do. Exit 0.

    Under ``CLOSEWIRE_DRY_RUN`` the client sends nothing and returns
    :data:`~closewire_client.rest.DRY_RUN_RESULT`. Printing the past-tense summary for that
    — ``deleted bot_X``, exit 0 — is a lie the operator cannot catch, because ``--json``
    was honest while the human path was not, and both exited 0. So the summary is now
    reachable *only* when something was really sent, and a suppressed call says so on
    stderr instead.

    **The exit code stays 0, deliberately.** A dry run that suppresses a send has done
    exactly what it was asked to do; this is the same outcome phase 07's write path
    reports, where suppression is a returned sentinel rather than a raised error and
    ``scripts/verify_writes.py`` treats a full dry run as success. Reusing 3 would conflate
    "you forgot to confirm" (fix: add ``--confirm``) with "dry-run is on" (fix: unset an
    env var) under one code, which is the exact aliasing the 1/2/3 split exists to avoid,
    and a non-zero code would make the *safe* posture look like a failure and abort every
    ``set -e`` script that adopts it. The honesty belongs in the message, not the status.
    """
    if as_json:
        # The sentinel IS the machine-readable result of the call, and it already says
        # `"sent": false`. Nothing else may join it on stdout.
        print(_json.dumps(result, indent=2, default=str))
    if _was_suppressed(result):
        print(_dry_run_notice(result, did=did, target=target), file=sys.stderr)
    elif not as_json:
        print(f"{did} {target}")
    return EXIT_OK


def _emit(payload: Any, *, as_json: bool, summary: str) -> int:
    """Render a Tier-2 **read** (export, balance, options, transactions).

    Separate from :func:`_report` because a read cannot be dry-run suppressed — the gate
    only applies to writes — so there is no outcome/intent question to get wrong here.
    """
    if as_json:
        print(_json.dumps(payload, indent=2, default=str))
    else:
        print(summary)
    return EXIT_OK


def _run(args: argparse.Namespace, rest, as_json: bool) -> int:
    group, action = args.command, args.action

    if group == "bots":
        if action == "publish":
            if not getattr(args, "confirm", False):
                return _refuse(t2_bots.preview_publish(args.id))
            result = t2_bots.publish(rest, args.id, confirm=True)
            return _report(result, as_json=as_json, did="published", target=args.id)
        if action == "delete":
            if getattr(args, "confirm", None) is None:
                return _refuse(t2_bots.preview_delete(args.id))
            result = t2_bots.delete(rest, args.id, confirm=args.confirm)
            return _report(result, as_json=as_json, did="deleted", target=args.id)
        if action == "export":
            return _emit(
                t2_bots.export(rest, args.id), as_json=as_json,
                summary=f"exported {args.id} (use --json for the payload)",
            )

    if group in _DELETE_TARGETS and action == "delete":
        module = {"personas": t2_personas, "sources": t2_sources, "leads": t2_leads}[group]
        if getattr(args, "confirm", None) is None:
            return _refuse(module.preview_delete(args.id))
        result = module.delete(rest, args.id, confirm=args.confirm)
        return _report(result, as_json=as_json, did="deleted", target=args.id)

    if group == "billing":
        if action == "balance":
            payload = t2_billing.balance(rest)
            return _emit(
                payload, as_json=as_json,
                # `BalanceDto.balance` is documented as "smallest unit of currency (cents
                # in USD)", so this is NOT a dollar figure. Printing it bare rendered 1250
                # as "balance: 1250 usd" — off by 100x on the screen an operator reads
                # right after a refill. Not converted, because "minor units per major unit"
                # is not 100 for every currency and the API states no exponent; labelled
                # instead, which is honest without inventing arithmetic.
                summary=(
                    f"balance: {payload.get('balance')} "
                    f"{payload.get('currency', '')} (minor units — 'cents in USD' per the spec)"
                ),
            )
        if action == "options":
            payload = t2_billing.options(rest)
            return _emit(
                payload, as_json=as_json,
                summary="\n".join(f"{k}: {v}" for k, v in payload.items()),
            )
        if action == "transactions":
            payload = t2_billing.transactions(rest)
            rows = payload if isinstance(payload, list) else payload.get("items", [])
            return _emit(payload, as_json=as_json, summary=f"{len(rows)} transaction(s)")
        if action == "refill":
            if getattr(args, "confirm", None) is None:
                return _refuse(t2_billing.preview_refill(args.amount, args.currency))
            result = t2_billing.refill(
                rest, args.amount, currency=args.currency, confirm=args.confirm
            )
            return _report(result, as_json=as_json, did="refilled", target=args.amount)

    print(f"unknown tier-2 command: {group} {action}", file=sys.stderr)
    return EXIT_FAILURE
