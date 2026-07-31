"""``cli.testing`` — the gate that was missing.

**Why this file exists.** Until it did, *nothing under ``tests/`` imported ``cli.testing``
at all*. Adversarial critics mutated four separate things in it — made ``_await_reply``
always return ``(None, None)``, set ``REPLY_ATTEMPTS = 0``, made ``show``'s
``rows = t.sessions_of(...)`` return ``[]``, and deleted ``_SPEND_NOTE`` — and **184 of 184
tests still passed**, with both verification harnesses still printing ALL CHECKS PASSED.
That is not "untested"; that is a module whose most expensive defect class (reporting a bot
that answered as mute, in the one command an operator uses to decide whether a bot works)
had already shipped once and had nothing standing between it and shipping again.

So the assertions here are chosen against **that** failure, not against the code's shape:

* every one of those four mutations is covered by a test that goes red — see the mutation
  matrix in the phase log; each test below names the mutation it kills;
* ``--json`` purity and the 0/1/2/3 exit codes are pinned per command, because a dispatcher
  that starts printing a human line onto stdout, or a traceback instead of a code, is the
  other half of what a CLI promises;
* **dry-run is asserted as "nothing further was sent"**, by inspecting the recorded calls,
  not by trusting the notice text. A suppressed ``say`` must not even poll for a reply.

**Speed.** ``_await_reply`` sleeps up to ``REPLY_ATTEMPTS - 1`` × ``REPLY_WAIT_S`` = 15 s of
real time by design. The unit tests inject a recording sleeper through the keyword-only
``sleep=`` seam and *assert on what it was asked to sleep*, so the production defaults are
pinned rather than weakened; the end-to-end tests that must run the full give-up path set
``REPLY_WAIT_S`` to 0 for their duration and restore it. Nothing here sleeps.

**Nothing here touches the network, and nothing here can send a message.** A recording stub
stands in for ``RestClient``; every assertion about a send is an assertion about what that
stub was asked to do.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import Any

from cli import testing as ct
from closewire_client.errors import (
    ClosebotAPIError,
    ClosewireError,
    ClosewireTransportError,
    RedactedValueError,
)
from closewire_client.live import LiveMessageError
from closewire_client.pacing import PacingBypassError, PacingHalt
from closewire_client.rest import DRY_RUN_RESULT

BOT = "bot_zzTEST"
LEAD = "lead_zzTEST"


# ── stubs ─────────────────────────────────────────────────────────────────────
def _resolve(value: Any) -> Any:
    """A stub reply: an exception instance is raised, a callable is called, else returned."""
    if isinstance(value, BaseException):
        raise value
    if callable(value):
        return value()
    return value


class _Stub:
    """Stands in for ``RestClient``; records the call instead of sending it.

    ``GET`` is only ever ``/bot/{id}/testSession`` in this module (``show`` and the reply
    poll both go through it), so one hook covers both; every other verb is the write.
    """

    def __init__(self, *, sessions: Any = None, write: Any = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.sessions = sessions if sessions is not None else {"leads": [], "total": 0}
        self.write = write if write is not None else {"ok": True}

    def request(self, method: str, path: str, *, json: Any = None, **_: Any) -> Any:
        self.calls.append((method, path, json))
        if method == "GET":
            return _resolve(self.sessions)
        return _resolve(self.write)

    @property
    def gets(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == "GET"]

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


def _rows(*rows: Any) -> dict[str, Any]:
    """The ``{"leads": [...]}`` shape — one of the two this endpoint really returns."""
    return {"leads": list(rows), "total": len(rows)}


def _row(last: str | None, direction: str | None = None, *, lead: str = LEAD) -> dict[str, Any]:
    return {
        "id": lead,
        "lastMessage": last,
        "lastMessageDirection": direction,
        "lastMessageTime": "2026-07-26T00:00:00Z",
    }


def _sequence(*payloads: Any):
    """A ``sessions`` hook that returns each payload in turn, then repeats the last."""
    box = list(payloads)

    def next_payload() -> Any:
        return _resolve(box.pop(0) if len(box) > 1 else box[0])

    return next_payload


class _Sleeper:
    """A sleeper that records instead of sleeping. The whole reason this suite is fast."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@contextmanager
def _no_wait():
    """Zero the poll interval for an end-to-end give-up run, then put it back."""
    original = ct.REPLY_WAIT_S
    ct.REPLY_WAIT_S = 0.0
    try:
        yield
    finally:
        ct.REPLY_WAIT_S = original


def _cli(action: str, rest: Any, *, as_json: bool = False, **fields: Any):
    """Run one ``test`` command through the real dispatcher. Returns (code, stdout, stderr)."""
    args = argparse.Namespace(action=action, bot=BOT, json=as_json, **fields)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ct.dispatch_test(args, rest, as_json)
    return code, out.getvalue(), err.getvalue()


# ── _is_bot_reply: the guard, as one predicate ────────────────────────────────
def test_is_bot_reply_requires_outbound_and_non_echo() -> None:
    assert ct._is_bot_reply("Got it.", "out", "hello") is True
    assert ct._is_bot_reply("hello", "in", "hello") is False, "inbound is our own turn"
    assert ct._is_bot_reply("Got it.", "in", "hello") is False
    assert ct._is_bot_reply(None, "out", "hello") is False
    assert ct._is_bot_reply("", "out", "hello") is False
    assert ct._is_bot_reply("   ", "out", "hello") is False
    assert ct._is_bot_reply("Got it.", None, "hello") is False


def test_is_bot_reply_rejects_the_echo_including_around_whitespace() -> None:
    """[kills: dropping the `!= sent` clause] The defect this predicate was extracted for."""
    for echoed in ("hello", " hello ", "hello\n", "\thello"):
        assert ct._is_bot_reply(echoed, "out", "hello") is False, echoed
    assert ct._is_bot_reply("hello there", "out", "hello") is True, "a superstring is a reply"


def test_is_bot_reply_stringifies_rather_than_dropping_an_odd_shape() -> None:
    """The response shape is undeclared; discarding a real answer is the costlier error."""
    assert ct._is_bot_reply(42, "out", "hello") is True


# ── _await_reply ──────────────────────────────────────────────────────────────
def test_await_reply_returns_the_answer_on_the_first_read_without_sleeping() -> None:
    """[kills: `_await_reply` -> (None, None); REPLY_ATTEMPTS = 0]"""
    rest = _Stub(sessions=_rows(_row("Got it. What's the address?", "out")))
    sleeper = _Sleeper()
    assert ct._await_reply(rest, BOT, LEAD, "hello", sleep=sleeper) == (
        "Got it. What's the address?",
        "out",
    )
    assert sleeper.slept == [], "it slept despite already having the reply"
    assert len(rest.gets) == 1


def test_await_reply_polls_until_the_reply_appears() -> None:
    """[kills: REPLY_ATTEMPTS = 0/1] The reply is asynchronous — the first read is our turn."""
    rest = _Stub(
        sessions=_sequence(
            _rows(_row("hello", "in")),
            _rows(_row("hello", "in")),
            _rows(_row("Got it.", "out")),
        )
    )
    sleeper = _Sleeper()
    assert ct._await_reply(rest, BOT, LEAD, "hello", sleep=sleeper) == ("Got it.", "out")
    assert len(rest.gets) == 3
    assert sleeper.slept == [ct.REPLY_WAIT_S, ct.REPLY_WAIT_S]


def test_await_reply_gives_up_as_no_reply_not_as_the_last_row_it_saw() -> None:
    """[kills: returning `(reply, direction)` on give-up] The B fix, at the source."""
    rest = _Stub(sessions=_rows(_row("hello", "in")))
    sleeper = _Sleeper()
    assert ct._await_reply(rest, BOT, LEAD, "hello", sleep=sleeper) == (None, None)
    assert len(rest.gets) == ct.REPLY_ATTEMPTS
    assert sleeper.slept == [ct.REPLY_WAIT_S] * (ct.REPLY_ATTEMPTS - 1), (
        "the poll budget or the interval changed"
    )


def test_await_reply_never_hands_back_an_echo() -> None:
    """[kills: the `!= sent` clause] Reproduced live-shaped: an echoing bot must read as mute.

    The row is outbound *and* is exactly what was sent, for every attempt. The old code
    returned it on give-up and the caller's weaker re-test printed `bot: hello`.
    """
    rest = _Stub(sessions=_rows(_row("hello", "out")))
    assert ct._await_reply(rest, BOT, LEAD, "hello", sleep=_Sleeper()) == (None, None)


def test_await_reply_takes_a_reply_arriving_on_the_very_last_attempt() -> None:
    """The off-by-one on the other side: the final read must still count."""
    tail = [_rows(_row("hello", "in"))] * (ct.REPLY_ATTEMPTS - 1)
    rest = _Stub(sessions=_sequence(*tail, _rows(_row("Late but here.", "out"))))
    sleeper = _Sleeper()
    assert ct._await_reply(rest, BOT, LEAD, "hello", sleep=sleeper) == ("Late but here.", "out")
    assert len(rest.gets) == ct.REPLY_ATTEMPTS


def test_the_production_poll_defaults_are_not_weakened() -> None:
    """The seam exists so the *tests* are fast. The shipped defaults must still wait."""
    assert ct.REPLY_ATTEMPTS >= 2, "a single read cannot catch an asynchronous reply"
    assert ct.REPLY_WAIT_S > 0, "a zero interval turns the poll into a busy loop"


def test_await_reply_sleeps_for_real_when_no_sleeper_is_injected() -> None:
    """[kills: dropping the `time.sleep` default] Without this, `sleep=None` could no-op."""
    sleeper = _Sleeper()
    original = ct.time
    ct.time = type("_FakeTime", (), {"sleep": staticmethod(sleeper)})()
    try:
        rest = _Stub(sessions=_rows(_row("hello", "in")))
        assert ct._await_reply(rest, BOT, LEAD, "hello") == (None, None)
    finally:
        ct.time = original
    assert sleeper.slept == [ct.REPLY_WAIT_S] * (ct.REPLY_ATTEMPTS - 1)


# ── _reply_from_session ───────────────────────────────────────────────────────
def test_reply_from_session_finds_the_row_under_either_key() -> None:
    for key in ("id", "leadId"):
        rest = _Stub(sessions=_rows({key: LEAD, "lastMessage": "Hi", "lastMessageDirection": "out"}))
        assert ct._reply_from_session(rest, BOT, LEAD) == ("Hi", "out"), key


def test_reply_from_session_normalises_the_bare_array_shape() -> None:
    """The endpoint returns a bare list on some bots — `sessions_of`'s live two-shape bug."""
    rest = _Stub(sessions=[_row("Hi", "out")])
    assert ct._reply_from_session(rest, BOT, LEAD) == ("Hi", "out")


def test_reply_from_session_says_it_does_not_know_when_the_row_is_absent() -> None:
    rest = _Stub(sessions=_rows(_row("Hi", "out", lead="lead_OTHER")))
    assert ct._reply_from_session(rest, BOT, LEAD) == (None, None)


def test_a_genuine_lookup_failure_still_degrades_gracefully() -> None:
    """Losing the send's result over a 500 on the *poll* is what the blanket catch was right about."""
    for failure in (
        ClosebotAPIError(500, "GET", "/bot/x/testSession", body="boom"),
        ClosewireTransportError("read timeout"),
        ValueError("not json"),
        KeyError("leads"),
    ):
        rest = _Stub(sessions=failure)
        assert ct._reply_from_session(rest, BOT, LEAD) == (None, None), type(failure).__name__


def test_a_pacing_halt_during_the_poll_is_not_swallowed() -> None:
    """[kills: bare `except Exception`] Defect C, at the source.

    `PacingHalt` is a `ClosewireError` is an `Exception`, so the blanket catch ate it — six
    times, once per attempt — and `test say` exited 0 saying "no reply" while the breaker was
    OPEN and already written to disk.
    """
    rest = _Stub(sessions=PacingHalt("429 storm"))
    try:
        ct._reply_from_session(rest, BOT, LEAD)
        raise AssertionError("PacingHalt was swallowed by the poll")
    except PacingHalt:
        pass


def test_a_pacing_bypass_during_the_poll_is_not_swallowed() -> None:
    rest = _Stub(sessions=PacingBypassError("GET", "/bot/x/testSession"))
    try:
        ct._reply_from_session(rest, BOT, LEAD)
        raise AssertionError("PacingBypassError was swallowed by the poll")
    except PacingBypassError:
        pass


def test_the_halt_aborts_the_poll_instead_of_repeating_it() -> None:
    """It must escape `_await_reply` too, on the first attempt — not be retried five more times."""
    rest = _Stub(sessions=PacingHalt("429 storm"))
    sleeper = _Sleeper()
    try:
        ct._await_reply(rest, BOT, LEAD, "hello", sleep=sleeper)
        raise AssertionError("the halt did not escape _await_reply")
    except PacingHalt:
        pass
    assert len(rest.gets) == 1, "it kept polling through an open breaker"
    assert sleeper.slept == [], "it slept its full budget on an open breaker"


# ── say, end to end ───────────────────────────────────────────────────────────
def _say(rest: Any, message: str = "hello", *, as_json: bool = False):
    return _cli("say", rest, as_json=as_json, session=LEAD, message=message)


def test_say_prints_the_bots_reply() -> None:
    """[kills: `_await_reply` -> (None, None); `show`-style poll breakage]"""
    rest = _Stub(sessions=_rows(_row("Got it. What's the address?", "out")))
    code, out, err = _say(rest)
    assert code == ct.EXIT_OK
    assert "you: hello" in out
    assert "bot: Got it. What's the address?" in out
    assert "no reply within the wait" not in out


def test_say_reports_an_echo_as_no_reply_not_as_the_bot_speaking() -> None:
    """[kills: the caller's weaker `reply and direction == 'out'` re-test] Defect B, end to end.

    Reproduced exactly as the critic did: session row `{lastMessage: "hello",
    lastMessageDirection: "out"}` after `test say … "hello"` printed `bot: hello`.
    """
    rest = _Stub(sessions=_rows(_row("hello", "out")))
    with _no_wait():
        code, out, err = _say(rest, "hello")
    assert code == ct.EXIT_OK
    assert "bot: hello" not in out, "the contact's own message was reported as the bot's reply"
    assert "no reply within the wait" in out


def test_says_own_guard_rejects_an_echo_even_if_await_reply_hands_one_over() -> None:
    """[kills: `said()` re-testing only `reply and direction == "out"`] Defect B's second half.

    The two halves of B are separately mutable, and the first mutation matrix run proved it:
    with `_await_reply` fixed to return `(None, None)` on give-up, weakening the caller's
    re-test back to its original form killed **nothing** — every other B test passes through
    `_await_reply`, so the caller's guard was defended only by the callee's. That is exactly
    the coupling that let B ship: two copies of one condition, and the weaker copy invisible
    while the stronger one held.

    So this test attacks the caller *directly*: `_await_reply` is replaced with one that hands
    back the echo, and `said()` must still refuse to print it. The guard is now pinned at both
    ends, which is what makes `_is_bot_reply` a single predicate rather than a convention.
    """
    original = ct._await_reply
    ct._await_reply = lambda *a, **k: ("hello", "out")
    try:
        code, out, _ = _say(_Stub(), "hello")
    finally:
        ct._await_reply = original
    assert code == ct.EXIT_OK
    assert "bot: hello" not in out, "said() printed an echo its own guard should have caught"
    assert "no reply within the wait" in out


def test_say_says_so_when_the_bot_stays_quiet() -> None:
    rest = _Stub(sessions=_rows(_row("hello", "in")))
    with _no_wait():
        code, out, _ = _say(rest)
    assert code == ct.EXIT_OK
    assert "no reply within the wait" in out
    assert "test show" in out, "the give-up line must say what to do next"


def test_say_surfaces_an_open_breaker_as_exit_2() -> None:
    """[kills: bare `except Exception` in `_reply_from_session`] Defect C, end to end.

    The send succeeded; the breaker tripped on the poll. Phase 04's contract is exit 2, not a
    cheerful 0 with "no reply".
    """
    rest = _Stub(sessions=PacingHalt("429 storm"), write={"ok": True})
    with _no_wait():
        code, out, err = _say(rest)
    assert code == ct.EXIT_HALTED, "an open breaker was reported as success"
    assert "breaker OPEN" in err
    assert "no reply within the wait" not in out


def test_say_still_reports_no_reply_when_only_the_lookup_failed() -> None:
    """The other direction: a 500 on the poll must not become exit 2 either."""
    rest = _Stub(sessions=ClosebotAPIError(500, "GET", "/bot/x/testSession", body="boom"))
    with _no_wait():
        code, out, _ = _say(rest)
    assert code == ct.EXIT_OK
    assert "no reply within the wait" in out


def test_say_sends_exactly_one_message_with_the_declared_body() -> None:
    rest = _Stub(sessions=_rows(_row("Got it.", "out")))
    _say(rest, "what is the price?")
    assert [c[:2] for c in rest.writes] == [("POST", f"/bot/{BOT}/testSession/message")]
    assert rest.writes[0][2] == {"leadId": LEAD, "message": "what is the price?"}


def test_say_reports_finished_goals() -> None:
    rest = _Stub(
        sessions=_rows(_row("Got it.", "out")),
        write={"goalsFinished": ["qualified", "booked"]},
    )
    _, out, _ = _say(rest)
    assert "goals finished: qualified, booked" in out


# ── _SPEND_NOTE (defect F) ────────────────────────────────────────────────────
def test_say_prints_a_spend_note_pointing_at_the_account() -> None:
    """[kills: deleting `_SPEND_NOTE`] Asserted on literal text, not on the constant.

    Reading `ct._SPEND_NOTE` back would be a test that cannot fail: it would compare the code
    to itself. These are the two things the note has to *do* — name the meter and name the
    command that reads it.
    """
    rest = _Stub(sessions=_rows(_row("Got it.", "out")))
    _, out, _ = _say(rest)
    assert "usedResponses" in out, "the note stopped naming the meter"
    assert "closewire" in out and "ping" in out, "the note stopped naming how to read it"


def test_the_spend_note_does_not_assert_a_spend_it_never_measured() -> None:
    """[kills: reinstating "This send spent credit."] Defect F.

    The phase's only measurement is `usedResponses` 4.0 -> 4.0 across three real replies. A
    flat assertion of the spend contradicts it, and is the same unsupported-claim class the
    docstring above the constant already retracts once for "1 credit".
    """
    note = ct._SPEND_NOTE
    assert "This send spent credit" not in note
    assert "may have spent" in note, "the claim must be hedged, not merely reworded"
    assert "not established" in note.lower() or "unknown" in note.lower(), (
        "the note must say the metering question is open, not just soften the verb"
    )


def test_the_prospective_warning_stays_conservative() -> None:
    """`--help` governs whether to run the command at all; understating cost there is worse."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true")
    ct.add_test_parsers(sub, shared)
    helps = sub.choices["test"]._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert "CREDIT" in (helps["say"].format_usage() + str(sub.choices["test"].format_help()))


# ── show (defect E, and the branch a critic emptied) ──────────────────────────
def test_show_prints_the_session_rows_latest_turn() -> None:
    """[kills: `rows = t.sessions_of(...)` -> []] The branch that had no test at all."""
    rest = _Stub(sessions=_rows(_row("Got it. What's the address?", "out")))
    code, out, err = _cli("show", rest, session=LEAD)
    assert code == ct.EXIT_OK, err
    assert f"session {LEAD}:" in out
    assert "Got it. What's the address?" in out
    assert "out" in out


def test_show_states_that_it_is_not_the_full_transcript() -> None:
    """[kills: reinstating "prints the transcript so far"] Defect E, in the output itself."""
    rest = _Stub(sessions=_rows(_row("Hi", "out")))
    _, out, _ = _cli("show", rest, session=LEAD)
    assert "only the latest turn" in out
    assert "does not return" in out


def test_show_help_does_not_promise_a_transcript_it_cannot_produce() -> None:
    """[kills: reinstating "Print the transcript so far."] `--help` is what the operator reads."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true")
    ct.add_test_parsers(sub, shared)
    text = sub.choices["test"].format_help()
    assert "transcript so far" not in text, text
    assert "latest turn" in text


def test_the_module_docstring_does_not_promise_a_transcript_either() -> None:
    assert "prints the transcript so far" not in (ct.__doc__ or "")


def test_show_fails_when_the_session_is_not_on_the_bot() -> None:
    rest = _Stub(sessions=_rows(_row("Hi", "out", lead="lead_OTHER")))
    code, out, err = _cli("show", rest, session=LEAD)
    assert code == ct.EXIT_FAILURE
    assert out == "", "a failure wrote to stdout"
    assert LEAD in err


def test_show_survives_a_row_shape_the_endpoint_never_declared() -> None:
    """`sessions_of` validates the container, never the elements. A list of strings is legal."""
    rest = _Stub(sessions=["lead_1", "lead_2"])
    code, out, err = _cli("show", rest, session=LEAD)
    assert code == ct.EXIT_FAILURE, "it should degrade, not traceback"
    assert "unexpected response shape" in err
    assert out == ""


def test_show_reads_and_never_writes() -> None:
    rest = _Stub(sessions=_rows(_row("Hi", "out")))
    _cli("show", rest, session=LEAD)
    assert rest.writes == [], "a read command sent a mutation"


# ── start / end ───────────────────────────────────────────────────────────────
def test_start_prints_the_lead_id_and_the_exact_next_command() -> None:
    rest = _Stub(write={"leadId": LEAD})
    code, out, _ = _cli("start", rest)
    assert code == ct.EXIT_OK
    assert LEAD in out
    assert f"closewire test say {LEAD}" in out
    assert f"--bot {BOT}" in out, "the next-command line must carry --bot, which is required"


def test_lead_id_probes_the_keys_this_api_has_used() -> None:
    for key in ("leadId", "lead_id", "id", "sessionId"):
        assert ct._lead_id({key: LEAD}) == LEAD, key
    assert "could not find" in ct._lead_id({"nothing": 1})
    assert "could not find" in ct._lead_id("a string body")


def test_end_deletes_the_session() -> None:
    rest = _Stub(write={"ok": True})
    code, out, _ = _cli("end", rest, session=LEAD)
    assert code == ct.EXIT_OK
    assert [c[:2] for c in rest.writes] == [("DELETE", f"/bot/{BOT}/testSession/{LEAD}")]
    assert f"session {LEAD} deleted" in out


def test_an_unknown_action_fails_instead_of_passing_silently() -> None:
    code, out, err = _cli("teleport", _Stub())
    assert code == ct.EXIT_FAILURE
    assert out == ""
    assert "unknown test command" in err


def test_every_declared_test_command_dispatches() -> None:
    """`TEST_COMMANDS` is what `cli.main`'s routing assertion is built from."""
    assert ct.TEST_COMMANDS == ("test start", "test say", "test show", "test end")
    for command in ct.TEST_COMMANDS:
        action = command.split()[1]
        rest = _Stub(sessions=_rows(_row("Got it.", "out")), write={"leadId": LEAD})
        fields = {"session": LEAD} if action != "start" else {}
        if action == "say":
            fields["message"] = "hello"
        code, _, err = _cli(action, rest, **fields)
        assert code == ct.EXIT_OK, f"{command} -> {code}: {err}"


# ── _was_suppressed ───────────────────────────────────────────────────────────
def test_was_suppressed_recognises_the_sentinel_the_client_actually_returns() -> None:
    result = dict(DRY_RUN_RESULT, method="POST", path=f"/bot/{BOT}/testSession/message")
    assert ct._was_suppressed(result) is True
    assert ct._was_suppressed(dict(DRY_RUN_RESULT)) is True


def test_was_suppressed_is_not_fooled_by_a_real_body_carrying_sent_false() -> None:
    """The reproduced live bug: a real reply was announced as DRY RUN, after the credit went."""
    assert ct._was_suppressed({"message": "Hi Ada!", "sent": False}) is False


def test_was_suppressed_asks_the_sentinel_about_every_key_it_declares() -> None:
    """Drop any one key and it is no longer the sentinel — this is what pins it to the source."""
    for key in DRY_RUN_RESULT:
        partial = {k: v for k, v in DRY_RUN_RESULT.items() if k != key}
        assert ct._was_suppressed(partial) is False, key


def test_was_suppressed_says_no_to_everything_that_is_not_a_dict() -> None:
    for value in (None, [], "", 0, "dry_run", [DRY_RUN_RESULT]):
        assert ct._was_suppressed(value) is False, value


# ── dry run: nothing sent, and nothing polled either ──────────────────────────
def _suppressed(method: str, path: str) -> dict[str, Any]:
    return dict(DRY_RUN_RESULT, method=method, path=path)


def test_a_dry_run_say_sends_nothing_and_does_not_even_poll() -> None:
    """Asserted against the recorded calls, not the notice text.

    The reply poll is a *read*, which dry-run does not suppress — so a suppressed `say` that
    still polled would burn think-time waiting 15 s for an answer to a message that was never
    sent, and could print `bot: …` from a previous turn under a DRY RUN banner.
    """
    rest = _Stub(
        sessions=_rows(_row("stale reply from an earlier turn", "out")),
        write=_suppressed("POST", f"/bot/{BOT}/testSession/message"),
    )
    code, out, err = _say(rest)
    assert code == ct.EXIT_OK
    assert rest.gets == [], "a suppressed send still polled the session list"
    assert out == "", "a dry run wrote a result to stdout in table mode"
    assert "DRY RUN" in err and "never sent" in err
    assert "stale reply" not in out + err


def test_a_dry_run_say_still_writes_the_sentinel_to_stdout_under_json() -> None:
    """The phase-06 contract: stdout carries the machine-readable result, or it is unparseable."""
    rest = _Stub(write=_suppressed("POST", f"/bot/{BOT}/testSession/message"))
    code, out, err = _say(rest, as_json=True)
    assert code == ct.EXIT_OK
    payload = json.loads(out)
    assert payload["dry_run"] is True and payload["sent"] is False
    assert "DRY RUN" in err


def test_dry_run_suppression_is_reported_on_every_write_command() -> None:
    for action, fields, route in (
        ("start", {}, f"/bot/{BOT}/testSession"),
        ("say", {"session": LEAD, "message": "hello"}, f"/bot/{BOT}/testSession/message"),
        ("end", {"session": LEAD}, f"/bot/{BOT}/testSession/{LEAD}"),
    ):
        rest = _Stub(write=_suppressed("POST", route))
        code, out, err = _cli(action, rest, **fields)
        assert code == ct.EXIT_OK, action
        assert out == "", f"{action} printed a past-tense summary for a call that never happened"
        assert "DRY RUN — NOTHING HAPPENED." in err, action
        assert route in err, action


# ── --json purity ─────────────────────────────────────────────────────────────
def test_json_mode_puts_json_and_nothing_else_on_stdout() -> None:
    """The promise that makes `| jq` safe, per command."""
    cases = [
        ("start", {}, _Stub(write={"leadId": LEAD})),
        ("say", {"session": LEAD, "message": "hello"},
         _Stub(sessions=_rows(_row("Got it.", "out")), write={"ok": True})),
        ("show", {"session": LEAD}, _Stub(sessions=_rows(_row("Got it.", "out")))),
        ("end", {"session": LEAD}, _Stub(write={"ok": True})),
    ]
    for action, fields, rest in cases:
        code, out, _ = _cli(action, rest, as_json=True, **fields)
        assert code == ct.EXIT_OK, action
        json.loads(out)  # raises if anything non-JSON leaked onto stdout
        for human in ("you: ", "bot: ", "session started", "deleted", "NOTE:", "[This send"):
            assert human not in out, f"{action} leaked a human line into --json: {human!r}"


def test_a_failure_writes_nothing_to_stdout_in_either_mode() -> None:
    """Exit non-zero *and* empty stdout — a consumer must not parse half an answer."""
    for as_json in (False, True):
        rest = _Stub(sessions=_rows(_row("Hi", "out", lead="lead_OTHER")))
        code, out, err = _cli("show", rest, as_json=as_json, session=LEAD)
        assert code == ct.EXIT_FAILURE
        assert out == "", f"as_json={as_json}"


# ── exit codes ────────────────────────────────────────────────────────────────
def test_the_exit_codes_are_the_cli_wide_numbers() -> None:
    """Operators' wrappers switch on the numbers, not the constants."""
    assert (ct.EXIT_OK, ct.EXIT_FAILURE, ct.EXIT_HALTED, ct.EXIT_REFUSED) == (0, 1, 2, 3)


def test_every_error_family_becomes_its_own_exit_code_never_a_traceback() -> None:
    """`dispatch_test` promises to raise nothing. Each arm is exercised through a real command."""
    cases: list[tuple[BaseException, int, str]] = [
        (PacingHalt("429 storm"), ct.EXIT_HALTED, "breaker OPEN"),
        (LiveMessageError("out of credits", status_code=402), ct.EXIT_FAILURE, "credits"),
        (ClosebotAPIError(404, "GET", "/bot/x/testSession", body="no such bot"),
         ct.EXIT_FAILURE, "HTTP 404"),
        (ClosewireTransportError("read timed out"), ct.EXIT_FAILURE, "request failed"),
        (ValueError("bad id"), ct.EXIT_REFUSED, "bad id"),
        (TypeError("bad type"), ct.EXIT_REFUSED, "bad type"),
        (RedactedValueError("that value is a secret"), ct.EXIT_REFUSED, "secret"),
        (AttributeError("'str' object has no attribute 'get'"),
         ct.EXIT_FAILURE, "unexpected response shape"),
        (KeyError("leads"), ct.EXIT_FAILURE, "unexpected response shape"),
    ]
    for exc, expected, fragment in cases:
        rest = _Stub(sessions=exc)
        code, out, err = _cli("show", rest, session=LEAD)
        label = type(exc).__name__
        assert code == expected, f"{label} -> {code}, expected {expected}"
        assert fragment in err, f"{label}: stderr did not explain itself: {err!r}"
        assert out == "", f"{label} wrote to stdout"


def test_an_unnamed_closewire_error_still_lands_on_a_code() -> None:
    """The base-class arm: tomorrow's subclass must not become a traceback path."""

    class _FutureError(ClosewireError):
        pass

    code, _, err = _cli("show", _Stub(sessions=_FutureError("something new")), session=LEAD)
    assert code == ct.EXIT_FAILURE
    assert "request failed" in err


def test_the_dead_helpers_are_gone_and_stay_gone() -> None:
    """`_transcript_lines` rendered a route that hangs; `_reply_text` read the reply off the
    send response, which is the exact bug this phase fixed. Either one back in the module is
    a wrong lookup one call away from `said()`."""
    assert not hasattr(ct, "_transcript_lines")
    assert not hasattr(ct, "_reply_text")


def test_the_exception_arm_is_justified_by_a_path_that_still_exists() -> None:
    """A live rationale pointing at deleted code is how the next reader deletes the wrong thing.

    The `except (AttributeError, KeyError)` arm used to justify itself by `_transcript_lines`,
    which had no callers — so the comment argued for an unreachable path while the reachable
    one (`show` probing unvalidated `sessions_of` rows, pinned by
    `test_show_survives_a_row_shape_the_endpoint_never_declared`) went unmentioned. The arm
    may still *mention* the deleted helper as history; it must not rest on it.
    """
    import inspect

    # `explain` now holds the single exception table; `dispatch_test` delegates to it. The
    # rationale must live wherever the mapping does, so both are searched.
    source = inspect.getsource(ct.dispatch_test) + inspect.getsource(ct.explain)
    assert "sessions_of" in source, "the arm no longer names the path that reaches it"
    if "_transcript_lines" in source:
        assert "now gone" in source or "deleted" in source, (
            "the arm cites _transcript_lines without saying it no longer exists"
        )


def test_show_finds_a_row_keyed_only_by_leadid() -> None:
    """Regression: `show`'s `leadId` fallback was unpinned while the identical one in
    `_reply_from_session` was tested.

    `sessions_of` is documented to return two live shapes, and two critics independently
    mutated `str(r.get("id") or r.get("leadId"))` down to `str(r.get("id"))` with the whole
    suite still green. A row keyed only by `leadId` would then make `show` report
    "no session <id> on bot" — exit 1 — for a session that exists.
    """
    import argparse

    from cli import testing as ct

    session = "lead_only_by_leadid"

    class _Rest:
        def request(self, method, path, *, json=None, **_):
            # The `leadId`-keyed shape, with no `id` at all.
            return {"leads": [{"leadId": session, "lastMessage": "hi",
                               "lastMessageDirection": "out", "lastMessageTime": "t"}],
                    "total": 1}

    args = argparse.Namespace(command="test", action="show", session=session, bot="bot_X")
    code = ct.dispatch_test(args, _Rest(), as_json=False)
    assert code == ct.EXIT_OK, f"show could not find a leadId-keyed row (exit {code})"

# ── The output mode is a rendering choice, never a semantic one ───────────────
#
# Two defects, six review rounds each, one cause: `--json` decided what the command DID.
# `say --json` never polled (the poll lived inside the human renderer), and a breaker trip
# during the poll unwound past the already-paid send so stdout was empty. The instance fixes
# are "poll in both modes" and "catch the halt"; the class fix is that no producer may sit
# behind a branch on `as_json`, and that is what these assert.

def test_say_makes_the_same_api_calls_in_both_output_modes() -> None:
    """The behavioural statement of the rule. --json must not change the call sequence."""
    seen = {}
    for as_json in (False, True):
        rest = _Stub(sessions=_rows(_row("Got it.", "out")))
        with _no_wait():
            _say(rest, as_json=as_json)
        seen[as_json] = [(c[0], c[1]) for c in rest.calls]
    assert seen[False] == seen[True], (
        f"--json changed what the command did: human={seen[False]} json={seen[True]}"
    )


def test_json_say_carries_the_reply_it_paid_for() -> None:
    """A consumer that spent a credit must receive what it bought."""
    rest = _Stub(sessions=_rows(_row("Got it.", "out")))
    with _no_wait():
        _code, out, _err = _say(rest, as_json=True)
    facts = json.loads(out)[ct.FACTS_KEY]
    assert facts["reply"] == "Got it." and facts["answered"] is True, facts


def test_a_breaker_trip_during_the_poll_does_not_erase_the_paid_send() -> None:
    """The send already happened. The record of it must survive the failure, in both modes."""
    for as_json in (False, True):
        rest = _Stub(sessions=PacingHalt("breaker OPEN: 429 storm"), write={"ok": True, "messageId": "msg_42"})
        with _no_wait():
            code, out, err = _say(rest, as_json=as_json)
        assert code == ct.EXIT_HALTED, (as_json, code)
        assert "breaker OPEN" in err, as_json
        assert out.strip(), f"as_json={as_json}: the record of a paid send was discarded"
        if as_json:
            payload = json.loads(out)
            assert payload[ct.FACTS_KEY]["poll"] == "aborted", payload


def test_no_producer_sits_behind_a_branch_on_the_output_mode() -> None:
    """The class gate. `as_json` may not appear in a branch inside a producing function.

    Both defects were `as_json` deciding WHAT rather than HOW. Asserting the property over
    the AST stops the next producer being written behind the same branch, which a purely
    behavioural test would only catch once someone thought to probe that call.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ct))
    problems: list[str] = []
    for name in ("_run", "_poll_for_reply"):
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert node is not None, f"{name} has been renamed — re-point this gate"
        for branch in ast.walk(node):
            if not isinstance(branch, (ast.If, ast.IfExp)):
                continue
            names = {x.id for x in ast.walk(branch.test) if isinstance(x, ast.Name)}
            if "as_json" in names:
                problems.append(f"{name}: branches on as_json at line {branch.lineno}")
    assert not problems, (
        "a producing function branches on the output mode: " + "; ".join(problems)
        + ". --json selects a rendering. It must never select behaviour."
    )


def test_a_session_sentinel_is_not_reported_as_the_bots_reply() -> None:
    """`*started` is an outbound marker, not speech.

    It satisfied every clause of the old three-clause predicate — outbound, non-empty,
    differs from what was sent — so `test say` rendered `bot: *started` over a turn the bot
    had not answered, and the phase log then recorded that turn as a substantive reply.
    Reporting silence as speech is worse than the reverse for a QA tool, because it looks
    like data.
    """
    rest = _Stub(sessions=_rows(_row("*started", "out")))
    with _no_wait():
        code, out, _err = _say(rest)
    assert code == ct.EXIT_OK
    assert "bot: *started" not in out, "a session sentinel was rendered as the bot's reply"
    assert "no reply within the wait" in out


def test_a_reply_that_merely_starts_with_an_asterisk_is_still_a_reply() -> None:
    """The control. A `startswith("*")` heuristic would eat real markdown emphasis."""
    rest = _Stub(sessions=_rows(_row("*Absolutely* — what's the address?", "out")))
    with _no_wait():
        _code, out, _err = _say(rest)
    assert "bot: *Absolutely* — what's the address?" in out


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} cli.testing tests passed.")

