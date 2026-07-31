"""``closewire test …`` — a QA loop against a bot, without real traffic.

Four commands, deliberately shaped like a conversation rather than an API:

* ``test start --bot <id>`` opens a session and prints its lead id.
* ``test say <session> "<msg>" --bot <id>`` sends a turn and prints the bot's reply, plus
  any goals that flipped to finished.
* ``test show <session> --bot <id>`` prints the session's **latest turn** — not the whole
  transcript. The transcript route does not return (it times out server-side, deviation 25),
  so ``show`` reads ``lastMessage``/``lastMessageDirection``/``lastMessageTime`` off the
  session row and says so in its own output. An earlier revision of this line, and of
  ``show``'s ``--help``, both promised "the transcript so far"; ``--help`` is what the
  operator actually reads, so a promise the command cannot keep belonged in neither.
* ``test end <session> --bot <id>`` deletes the session.

**``say`` is treated as spending a credit** — it makes the bot generate a reply, so it is
paced on the write lane and suppressed by ``CLOSEWIRE_DRY_RUN``. Whether a *test-session*
send is actually metered is not established: see :data:`_SPEND_NOTE`. The safe posture does
not depend on the answer.

**Stdout carries results; nothing else** — :mod:`cli.tier2`'s rule, for the same reason and
now through the same mechanism. A dry run is announced on stderr, but under ``--json`` the
sentinel is still written to stdout, because it *is* the machine-readable result of the
call and it already says ``sent: false``. Returning early instead handed
``closewire test say … --json | jq`` **zero bytes** and exit 0, on every ``test`` command.

**``--bot`` is required everywhere, including on the two commands the phase-09 brief writes
without it** (``test say <session> "<msg>"``, ``test show <session>``). Every Bot Testing
route is ``/bot/{botId}/testSession…`` and the API exposes no lookup from a lead id alone,
so the CLI has nothing to resolve it from; the alternatives are sweeping every bot's session
list before each command, or trusting a local cache that would silently address the wrong
bot once stale. Reported as a deviation from the brief's literal command forms rather than
worked around — ``test start`` prints the exact ``say`` line to use next, ``--bot`` included.

Exit codes match the rest of the CLI: ``0`` ok · ``1`` failure · ``2`` breaker open ·
``3`` refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

from closewire_client.errors import ClosebotAPIError, ClosewireError
from closewire_client.live import LiveMessageError
from closewire_client.pacing import PacingBypassError, PacingHalt
from closewire_client.rest import DRY_RUN_RESULT
from closewire_client.writes import testing as t

__all__ = ["add_test_parsers", "dispatch_test", "TEST_COMMANDS"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_HALTED = 2
EXIT_REFUSED = 3

#: Every test command, for `--help` and the validation log.
TEST_COMMANDS = ("test start", "test say", "test show", "test end")

#: Why ``--bot`` is on a command whose brief form does not mention it. In ``--help``, not
#: only in the module docstring, because the person who meets this is typing the brief's
#: literal ``test say <session> "<msg>"`` and reading the usage error it produces.
_BOT_HELP = (
    "Required — the bot the session belongs to. The Bot Testing API is keyed by bot "
    "(/bot/{botId}/testSession/…) and offers no lookup from a lead id alone, so this "
    "cannot be inferred from the session. `test start` prints the full command to use next."
)

#: What ``say`` prints about the credit it may just have spent.
#:
#: **It does not assert the spend as fact, because the phase's only measurement contradicts
#: that.** ``docs/validation/09-runtime.md`` read ``usedResponses`` before and after three
#: real bot replies through this command: ``4.0 -> 4.0``. Two explanations fit — the meter
#: lags, or test-session traffic is unmetered — and this repo cannot tell them apart from
#: one account's readings. An earlier revision of this note opened "This send spent credit."
#: That is the *same* unsupported-claim class the paragraphs below already retract once (for
#: "1 credit"), reintroduced two lines later, and it is worse than the number it replaced:
#: a wrong number invites checking, a flat assertion does not.
#:
#: Note the deliberate asymmetry with ``say``'s ``--help``, which still says SPENDS A CREDIT.
#: That is a *prospective warning* governing whether to run the command at all, and it drives
#: the write lane and the dry-run gate; understating an unmeasured cost there would be the
#: expensive error. This is a *retrospective report* of what already happened, where the
#: expensive error is claiming knowledge the account did not confirm. Conservative before,
#: honest after.
#:
#: It used to print ``[writes this hour: N]`` from ``rest.pacer.stats().writes_last_hour``,
#: offered as "the running count of sends it has made" against phase 09's ~20-message
#: budget. It printed ``1`` every time, and two independent things were wrong with it:
#:
#: * :class:`~closewire_client.pacing.PacerStats` is in-memory and per-process — it says so
#:   itself, in ``render()`` and in ``as_dict()["scope"]``. Each ``closewire`` invocation is
#:   a fresh process, so a number an operator was invited to read as cumulative restarted at
#:   zero on every line of their shell history. Three consecutive real sends each read ``1``.
#: * It counts **budget claims, not credits**. ``Pacer.acquire`` reserves the window slot
#:   *before* the dry-run gate, so a suppressed send increments it too — the "spend" would
#:   have climbed under ``CLOSEWIRE_DRY_RUN``, where nothing is spent at all.
#:
#: Nothing inside one invocation can honestly total the spend, so this stops claiming to.
#: It states what this send cost and points at the only cross-invocation source of truth:
#: the account's own usage, which is what ``docs/validation/09-runtime.md`` measured the
#: phase against (``usedResponses`` 4.0 → 4.0). A CLI-local ledger was the alternative and
#: is worse — it would count only sends made through this one command, so a credit spent by
#: ``conversation.send_message``, by the Closebot UI, or from another machine would be
#: missing from a number presented as the budget. That is the same false assurance in a
#: costlier disguise.
_SPEND_NOTE = (
    "  [This send may have spent credit — it is treated as a paid write, but whether test-\n"
    "   session sends are metered is NOT established: `usedResponses` read 4.0 before and\n"
    "   after three real replies through this command (the meter may lag, or test traffic\n"
    "   may be free — unknown). The only source of truth is the account itself: `closewire\n"
    "   ping` -> usage `usedResponses`. The pacer's counters are per-process and cannot\n"
    "   tell you.]"
)


def add_test_parsers(
    sub: "argparse._SubParsersAction", json_opt: argparse.ArgumentParser
) -> None:
    """Register the ``test`` group. It shares no noun with another tier, so it owns its own."""
    p = sub.add_parser("test", help="QA a bot through a throwaway test session.")
    s = p.add_subparsers(dest="action", required=True)

    start = s.add_parser("start", parents=[json_opt], help="Open a test session on a bot.")
    start.add_argument("--bot", required=True, help="Bot id to QA.")

    say = s.add_parser("say", parents=[json_opt],
                       help="Send a turn and print the reply. SPENDS A CREDIT.")
    say.add_argument("session", help="The session's lead id, from `test start`.")
    say.add_argument("message", help="What the contact says.")
    say.add_argument("--bot", required=True, help=_BOT_HELP)

    # NOT "the transcript so far" — that is what it used to say, and the command has never
    # been able to do it: the transcript route times out server-side (deviation 25), so this
    # prints the session row's latest turn only. `--help` is the text an operator actually
    # reads, so it is the last place a promise the command cannot keep should survive.
    show = s.add_parser("show", parents=[json_opt],
                        help="Print the session's latest turn (not the full transcript — "
                             "the transcript route does not return).")
    show.add_argument("session", help="The session's lead id, from `test start`.")
    show.add_argument("--bot", required=True, help=_BOT_HELP)

    end = s.add_parser("end", parents=[json_opt], help="Delete a test session.")
    end.add_argument("session", help="The session's lead id, from `test start`.")
    end.add_argument("--bot", required=True, help=_BOT_HELP)


def explain(exc: BaseException) -> tuple[list[str], int]:
    """Turn a failure into ``(stderr lines, exit code)`` — a **value**, not an unwind.

    Extracted from :func:`dispatch_test`'s except arms, in their original order, because
    "report a failure" and "stop producing output" were the same act and that cost an
    operator the record of a send they had already paid for. A `PacingHalt` raised by the
    reply poll unwound past the send result and `test say` exited 2 with an empty stdout —
    the credit was spent, the session id was never printed, and nothing said so.

    Reporting a failure has to be something a caller can *do* while still printing what it
    earned. That is only possible if the report is a value. See :func:`_poll_for_reply`,
    which catches instead of raising, and :func:`_report`, which prints the record first.
    """
    if isinstance(exc, PacingHalt):
        return [f"\n{exc}"], EXIT_HALTED
    if isinstance(exc, LiveMessageError):
        # The runtime's codes are semantic — "out of credits" must not read as a generic
        # failure, because the remedy is completely different.
        return [f"\n{exc}"], EXIT_FAILURE
    if isinstance(exc, ClosebotAPIError):
        return ([f"\nHTTP {exc.status_code}: {exc.method} {exc.path}",
                 f"  {str(exc.body)[:600]}"], EXIT_FAILURE)
    if isinstance(exc, (ValueError, TypeError)):
        return [f"\n{exc}"], EXIT_REFUSED
    if isinstance(exc, ClosewireError):
        return [f"\nrequest failed: {exc}"], EXIT_FAILURE
    if isinstance(exc, (AttributeError, KeyError)):
        # `_run`'s `show` branch probes rows straight off `t.sessions_of(...)`, which is
        # documented to return "whatever list the payload had" — it validates that the
        # *container* is a list, never that the *elements* are dicts. That endpoint already
        # returns two different shapes on one account, so a payload of `["lead_1", "lead_2"]`
        # is well within what it may send, and `r.get("id")` on a string is an AttributeError
        # nothing above catches. `cli.reads` covers this family for the same reason: a CLI
        # must not die on an unfamiliar payload. TypeError deliberately stays on the refusal
        # arm above. (An earlier version of this rationale cited `_transcript_lines`, which is
        # now gone and had no callers even then — a live comment arguing for an unreachable
        # path, which is how the next reader deletes the wrong thing.)
        return [f"\nunexpected response shape ({type(exc).__name__}: {exc})"], EXIT_FAILURE
    raise exc


def dispatch_test(args: argparse.Namespace, rest, as_json: bool) -> int:
    """Run one test command. Raises nothing: failures become exit codes.

    **One exception table, in `explain`.** This function used to carry its own copy of the
    six arms, and round 14 added `explain` beside it rather than replacing it — so there were
    two tables mapping the same exceptions to the same codes, with nothing keeping them in
    sync. A critic pointed out that a seventh arm added to one would never reach the other.
    The rationale for each arm lives on `explain`, where the mapping is.
    """
    try:
        return _run(args, rest, as_json)
    except (
        PacingHalt, LiveMessageError, ClosebotAPIError, ValueError, TypeError,
        ClosewireError, AttributeError, KeyError,
    ) as exc:
        lines, code = explain(exc)
        for line in lines:
            print(line, file=sys.stderr)
        return code


def _was_suppressed(result: Any) -> bool:
    """Did :class:`~closewire_client.rest.RestClient` suppress this call under dry-run?

    Matched against :data:`~closewire_client.rest.DRY_RUN_RESULT` **itself** — the client
    returns ``dict(DRY_RUN_RESULT, method=…, path=…)``, so every key the sentinel declares
    must be present and equal. Ask the sentinel, and the question cannot drift from the
    answer; ask about a key name, and it already has.

    This module used to read ``result.get("sent") is False`` — one key, copied by hand out
    of that sentinel, which is a claim about a *key name*, not about the sentinel. Any 200
    body is free to carry ``"sent": false`` for its own reasons, and one did: a genuine,
    non-dry-run reply ``{"message": "Hi Ada!", "sent": false, …}`` was announced as
    ``DRY RUN — NOTHING SENT … No credit spent.``, exit 0, with the bot's answer thrown
    away *after* the credit had been spent — the CLI reporting a saving on money it had
    just spent, in the one command that spends any.

    :func:`cli.tier2._was_suppressed` is the same three lines, deliberately. The predicate
    belongs beside :data:`DRY_RUN_RESULT` in :mod:`closewire_client.rest`, which is one
    import both tiers already have; it lives in two CLI modules only because
    ``scripts/verify_tier2.py``'s tier boundary forbids a Tier-1 module from importing
    ``cli.tier2`` at all — and that boundary is worth more than this de-duplication, since
    it is what keeps a test-session command structurally incapable of reaching ``delete``
    or ``refill``. Both copies derive from the same sentinel, so neither can drift from the
    thing being asked about, which is the property that failed here.
    """
    return isinstance(result, dict) and all(
        result.get(key) == value for key, value in DRY_RUN_RESULT.items()
    )


def _emit(payload: Any, *, as_json: bool, lines: list[str]) -> int:
    """Render a test **read** (``show``).

    Separate from :func:`_report` for the reason :mod:`cli.tier2` separates the same pair:
    the dry-run gate applies to writes only, so a read has no outcome/intent question to
    get wrong and needs no suppression branch.
    """
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in lines:
            print(line)
    return EXIT_OK


def _dry_run_notice(result: dict[str, Any], *, would_have: str) -> str:
    route = " ".join(str(result[k]) for k in ("method", "path") if result.get(k))
    return (
        "DRY RUN — NOTHING HAPPENED.\n"
        f"  CLOSEWIRE_DRY_RUN is set, so {route or 'the request'} was never sent.\n"
        f"  Had it been sent, it would have {would_have}.\n"
        "  Unset CLOSEWIRE_DRY_RUN (or set it to 0) to perform this for real."
    )


def _report(
    result: Any, *, as_json: bool, lines: Callable[[], list[str]] | list[str],
    would_have: str, failure: BaseException | None = None,
) -> int:
    """Report what a test **write** did, in both output modes. Exit 0.

    Two rules, and the old code broke each of them in a different direction:

    * **A suppressed call still has a result on stdout under ``--json``.** The sentinel is
      the machine-readable answer — it carries ``dry_run``, ``sent: false``, and the method
      and path that were not sent — so ``--json`` prints it whether or not the send
      happened. Returning ``EXIT_OK`` before printing gave a piped consumer an empty stdout
      and a success code, which is unparseable *and* indistinguishable from a command that
      produced nothing; it broke the phase-06 contract that stdout carries JSON and nothing
      else, on every ``test`` command at once.
    * **The human summary is reachable only when something was really sent.** ``lines`` is
      a callable, not a list, so the reply text, the lead id, and the credit note are never
      even computed for a call that did not happen — the past-tense summary cannot be
      printed for a dry run by accident, and a renderer cannot raise reading fields the
      sentinel does not have.

    The exit code stays 0 for a dry run, as in :func:`cli.tier2._report`: suppression is the
    safe posture doing its job, not a failure, and ``3`` already means "a guard refused",
    whose remedy (add ``--confirm``) is a different action from "unset an env var".
    """
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    if _was_suppressed(result):
        print(_dry_run_notice(result, would_have=would_have), file=sys.stderr)
    elif not as_json:
        for line in (lines() if callable(lines) else lines):
            print(line)
    # The record is on stdout before this point, unconditionally. Only then is a failure
    # reported — `failure` is a *value* precisely so that a command which already spent a
    # credit can print what it bought and still exit non-zero. See `explain`.
    if failure is not None:
        stderr_lines, code = explain(failure)
        for line in stderr_lines:
            print(line, file=sys.stderr)
        return code
    return EXIT_OK


# ``_transcript_lines`` stood here and ``_reply_text`` stood below ``_reply_from_session``.
# Both are **deleted**, and the deletion is the point rather than tidiness:
#
# * ``_transcript_lines`` rendered ``get_messages``' payload. ``show`` stopped calling
#   ``get_messages`` when that route was found not to return, so the renderer lost its only
#   caller — and the ``except (AttributeError, KeyError)`` arm in :func:`dispatch_test` went
#   on citing it as the reason it existed. An unreachable function named as live evidence is
#   strictly worse than no comment: it invites the next reader to "restore coverage" for a
#   path that hangs, or to delete the arm along with the function it names. The arm is
#   independently necessary (``show`` probes unvalidated rows) and now says so on its own.
# * ``_reply_text`` probed the *send response* for the reply. That is the exact bug this
#   phase fixed: the send response carries no reply, and reading it printed "(no reply text
#   in the response)" over answers the bot had really given. Keeping the helper keeps the
#   wrong lookup one call away from a future ``said()``.
#
# The capability is not lost: :func:`closewire_client.writes.testing.get_messages` is still
# exported for a Python caller who wants to retry the route. What is lost is CLI-local dead
# code whose docstrings asserted things the CLI no longer does — and ``tests/test_cli_testing.py``
# now pins the behaviour that replaced them, which is the coverage that was actually missing.


def _run(args: argparse.Namespace, rest, as_json: bool) -> int:
    action = args.action

    if action == "start":
        session = t.create_session(rest, args.bot)

        def started() -> list[str]:
            lead = _lead_id(session)
            return [
                f"session started on bot {args.bot}",
                f"  lead id: {lead}",
                f"  next:    closewire test say {lead} \"hello\" --bot {args.bot}",
            ]

        return _report(session, as_json=as_json, lines=started,
                       would_have=f"opened a test session on bot {args.bot}")

    if action == "say":
        # THE CREDIT IS SPENT ON THIS LINE. Everything below may only add to the record of
        # it — nothing below may prevent the record being printed, and nothing below may be
        # conditional on the output mode.
        result = t.send(rest, args.bot, args.session, args.message)
        facts, failure = _poll_for_reply(rest, args, result)
        return _report(
            _with_facts(result, facts), as_json=as_json,
            lines=_said_lines(args.message, facts),
            would_have=(f"said {args.message!r} to session {args.session}, "
                        "and spent a credit"),
            failure=failure,
        )

    if action == "show":
        # `get_messages` is the documented transcript route and it **does not return** —
        # ReadTimeout at 33 s on a one-session bot and at 150 s on a four-session one, so
        # it is not a data-volume problem (deviation 25). The session row carries the last
        # message, direction and time, so that is what is shown, with the limitation
        # stated rather than hidden behind a hang.
        rows = t.sessions_of(t.list_sessions(rest, args.bot))
        row = next((r for r in rows if str(r.get("id") or r.get("leadId")) == args.session), None)
        if row is None:
            print(f"no session {args.session} on bot {args.bot}", file=sys.stderr)
            return EXIT_FAILURE
        lines = [
            f"session {args.session}:",
            f"  last message : {row.get('lastMessage') or '(none yet)'}",
            f"  direction    : {row.get('lastMessageDirection') or '-'}",
            f"  at           : {row.get('lastMessageTime') or '-'}",
            "  NOTE: only the latest turn is shown. The full-transcript route",
            "        GET /bot/{botId}/testSession/messages/{leadId} does not return —",
            "        it times out server-side. See docs/validation/09-runtime.md.",
        ]
        return _emit(row, as_json=as_json, lines=lines)

    if action == "end":
        result = t.delete_session(rest, args.bot, args.session)
        return _report(result, as_json=as_json,
                       lines=lambda: [f"session {args.session} deleted"],
                       would_have=f"deleted test session {args.session}")

    print(f"unknown test command: {action}", file=sys.stderr)
    return EXIT_FAILURE


#: Where the facts this command computed are attached inside the JSON payload.
#:
#: Namespaced, not merged at the top level: `POST …/testSession/message` declares **no
#: response schema** (deviation 24), so its keys are whatever the server sends and a
#: top-level `reply` could silently collide with one of them.
FACTS_KEY = "_closewire"


def _poll_for_reply(rest, args: argparse.Namespace, result: Any) -> tuple[dict[str, Any], Any]:
    """Poll for the bot's reply. Returns ``(facts, failure)`` and **never raises**.

    Two defects, one cause, both carried six review rounds:

    * `say --json` never polled at all, because the poll lived inside the *human renderer*.
      A JSON consumer spent a credit and got back a send envelope that by this module's own
      comment carries no reply. The output flag decided what the command **did**, not how it
      spoke.
    * A `PacingHalt` from the poll unwound past the already-paid send, so `dispatch_test`
      exited 2 having printed nothing at all — no reply, no session id, no note that a
      credit had gone. Worse in JSON mode after the first fix: it would have exited **0**
      while the breaker was open and already latched to disk.

    So the poll is here, on the one path both modes take, and a pacing failure becomes a
    returned value. The poll is GET-only (`writes/testing.py`), so nothing here can re-send.
    """
    facts: dict[str, Any] = {
        "said": args.message, "session": args.session, "bot": args.bot,
        "reply": None, "reply_direction": None, "answered": False,
        "poll": "answered", "goals_finished": _goals(result),
    }
    if _was_suppressed(result):
        # Nothing was sent, so there is nothing to wait for. This is the one condition that
        # may skip the poll, and it is a fact about *what happened*, not about how the
        # caller wants it rendered — which is the distinction both defects above got wrong.
        # `tests/test_cli_testing.py::test_a_dry_run_say_sends_nothing_and_does_not_even_poll`
        # pins it, and caught this exact regression while the fix was being written.
        facts["poll"] = "not-sent"
        return facts, None
    try:
        # The send response does NOT carry the reply — verified live: it returned no
        # recognisable text while the bot had in fact answered. The reply lands on the
        # *session row*, so it is fetched from there. Reading it off `result` printed
        # "(no reply text in the response)" over a real answer, which is the worst possible
        # output for a QA tool: it reports the bot as mute when it spoke.
        reply, direction = _await_reply(rest, args.bot, args.session, args.message)
    except (PacingHalt, PacingBypassError) as exc:
        facts["poll"] = "aborted"
        return facts, exc
    facts["reply"] = reply
    facts["reply_direction"] = direction
    # The same predicate `_await_reply` used, not a weaker restatement of it. The restatement
    # was `reply and direction == "out"`, which lost the echo clause and printed the
    # contact's own message back as the bot's answer.
    facts["answered"] = _is_bot_reply(reply, direction, args.message)
    if not facts["answered"]:
        facts["poll"] = "timed-out"
    return facts, None


def _with_facts(result: Any, facts: dict[str, Any]) -> Any:
    """Attach `facts` to a dict payload under :data:`FACTS_KEY`, leaving anything else alone."""
    if isinstance(result, dict):
        return {**result, FACTS_KEY: facts}
    return result


def _said_lines(message: str, facts: dict[str, Any]) -> list[str]:
    """The human rendering of a `say`. Pure — it computes nothing and cannot fail."""
    out = [f"you: {message}"]
    if facts["answered"]:
        out.append(f"bot: {facts['reply']}")
    elif facts["poll"] == "aborted":
        out.append(
            f"bot: (unknown — the reply poll was aborted; the send above DID happen. "
            f"Re-run `closewire test show {facts['session']} --bot {facts['bot']}` once "
            "the halt is cleared.)"
        )
    else:
        out.append(
            "bot: (no reply within the wait — the bot may still be composing. "
            "Re-run `test show` in a moment.)"
        )
    if facts["goals_finished"]:
        out.append(f"goals finished: {', '.join(facts['goals_finished'])}")
    out.append(_SPEND_NOTE)
    return out


def _lead_id(session: Any) -> str:
    """The session handle, under whichever key this API used."""
    if isinstance(session, dict):
        for key in ("leadId", "lead_id", "id", "sessionId"):
            value = session.get(key)
            if value:
                return str(value)
    return "(could not find a lead id in the response — use --json to see it)"




#: How many times ``say`` re-reads the session waiting for the bot's reply, and how long it
#: sleeps between reads.
#:
#: The reply is **asynchronous**: ``POST …/testSession/message`` returns before the bot has
#: composed anything, so a single read straight after the send returns *your own message*
#: with ``lastMessageDirection: "in"``. Verified live — the first read showed the inbound
#: turn, a read moments later showed "Got it. What's the address of the property?".
#:
#: A QA loop that reports "no reply" for a bot that answered two seconds later is not usable,
#: so ``say`` waits. Bounded rather than open-ended, and each attempt is a paced *read* (the
#: cheap lane, no credit), so the cost of waiting is think-time and nothing else.
REPLY_ATTEMPTS = 6
REPLY_WAIT_S = 3.0


#: Outbound markers the runtime writes onto a session row that are **not** the bot speaking.
#:
#: Observed live: opening a session and sending the first turn leaves ``lastMessage`` as
#: ``*started`` with ``lastMessageDirection: "out"``. It is outbound, non-empty, and differs
#: from what was sent, so it satisfied every clause of the reply predicate and was rendered
#: as the bot's answer. Kept as an explicit set rather than a "starts with ``*``" heuristic:
#: a real reply may legitimately begin with an asterisk (markdown emphasis), and this list is
#: short, observed, and cheap to extend when the next sentinel is seen.
SESSION_SENTINELS = frozenset({"*started", "*restarted", "*ended"})


def _is_bot_reply(reply: Any, direction: Any, sent: Any) -> bool:
    """Is this session row's latest turn *the bot answering what we just said*?

    **One predicate, called from both places that ask.** It used to be an inline condition
    inside :func:`_await_reply`'s loop, which made it a loop-*exit* test and nothing more:
    on give-up the function returned ``(reply, direction)`` regardless, and the caller
    re-tested only ``reply and direction == "out"`` — dropping the "differs from what was
    sent" half. A session row of ``{"lastMessage": "hello", "lastMessageDirection": "out"}``
    after ``test say … "hello"`` therefore printed ``bot: hello``, falsifying this module's
    own claim that "a bot that echoed the contact could not be mistaken for one that
    replied". Two copies of a three-clause guard is one copy too many; there is now one, and
    :func:`_await_reply` cannot hand back a pair that fails it.

    Non-string ``lastMessage`` values are stringified rather than rejected: this endpoint's
    response shape is undeclared, and dropping a real answer because it arrived as a number
    would be the same "reports a bot that spoke as mute" failure in a new costume.

    **Fourth clause: session sentinels are not speech.** The three clauses above passed
    ``*started`` — an outbound, non-empty, non-echo marker the runtime writes onto a fresh
    session row — so `test say` printed ``bot: *started`` and the phase-09 log then recorded
    "the bot replied substantively each time" for a turn where it had not spoken at all.
    That is defect C inverted: the original was *reporting a bot that spoke as mute*, this is
    *reporting a bot that was silent as having spoken*, and for a QA tool the second is worse
    because it looks like data. Found in the committed capture
    ``docs/validation/evidence/09-goal-flip-cli.txt`` by a review agent, three rounds after
    the capture shipped.
    """
    text = "" if reply is None else (reply if isinstance(reply, str) else str(reply))
    if not text.strip() or direction != "out":
        return False
    if text.strip() in SESSION_SENTINELS:
        return False
    return text.strip() != ("" if sent is None else str(sent)).strip()


def _await_reply(
    rest, bot_id: str, session_id: str, sent: str, *, sleep: Callable[[float], None] | None = None
) -> tuple[str | None, str | None]:
    """Wait briefly for the bot's answer to appear on the session row.

    Returns as soon as :func:`_is_bot_reply` holds — outbound *and* not an echo of the text
    just sent. Gives up after :data:`REPLY_ATTEMPTS` and returns ``(None, None)``: a
    give-up is "no reply", and returning the last row it saw is what let an echo through.

    ``sleep`` is a seam, defaulting to :func:`time.sleep`. It exists so the tests can drive
    the full give-up path — :data:`REPLY_ATTEMPTS` attempts, one real reply appearing on the
    last one, the echo case — in microseconds instead of the 15 s of wall-clock the
    production defaults spend, without touching those defaults. Keyword-only, so no existing
    call site changes.
    """
    sleep = time.sleep if sleep is None else sleep
    for attempt in range(REPLY_ATTEMPTS):
        reply, direction = _reply_from_session(rest, bot_id, session_id)
        if _is_bot_reply(reply, direction, sent):
            return reply, direction
        if attempt < REPLY_ATTEMPTS - 1:
            sleep(REPLY_WAIT_S)
    return None, None


def _reply_from_session(rest, bot_id: str, session_id: str) -> tuple[str | None, str | None]:
    """The latest message on a session row, and its direction.

    The send response carries no reply — the bot's answer appears on the session row as
    ``lastMessage``/``lastMessageDirection``. Reading it from the send result reported a
    live, answered turn as "(no reply text in the response)".

    Returns ``(None, None)`` rather than raising when the row cannot be found: a QA loop
    that dies because a lookup missed is worse than one that says it does not know.

    **Pacing failures are exempt, and the exemption is the whole subtlety.** The blanket
    ``except Exception`` this replaces swallowed :class:`~closewire_client.pacing.PacingHalt`,
    which is a ``ClosewireError``, which is an ``Exception``. If the breaker tripped during
    the six poll reads, the halt was eaten six times, the loop still slept its full 15 s, and
    ``test say`` exited **0** printing "no reply within the wait" — while the breaker was in
    fact OPEN and already persisted to disk. Phase 04's contract is that an open breaker
    surfaces as exit **2**; degrading it to a cheerful 0 is precisely the failure the breaker
    exists to prevent, delivered by the code meant to be forgiving. ``PacingBypassError`` is
    re-raised for the adjacent reason: it means a slot was mishandled, i.e. a defect in this
    process, and six silent repeats of a defect is not graceful degradation.

    Everything else still degrades: a 500 on the list, a read timeout, a shape without rows
    are all genuine lookup failures, and losing the send's result over one is the thing the
    blanket catch was right about.
    """
    try:
        rows = t.sessions_of(t.list_sessions(rest, bot_id))
    except (PacingHalt, PacingBypassError):
        raise
    except Exception:
        return None, None
    for row in rows:
        if str(row.get("id") or row.get("leadId")) == str(session_id):
            return row.get("lastMessage"), row.get("lastMessageDirection")
    return None, None

def _goals(result: Any) -> list[str]:
    if isinstance(result, dict):
        for key in ("goals_finished", "goalsFinished", "finished_goals"):
            value = result.get(key)
            if isinstance(value, list):
                return [str(v) for v in value]
    return []
