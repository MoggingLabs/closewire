"""Live Message runtime client tests.

The runtime endpoint spends credits on every accepted send, so these run entirely against
an ``httpx.MockTransport`` — nothing here can reach ``api.closebot.ai``.

Two properties get most of the attention, because both are things a caller cannot check for
themselves:

* **every documented status maps to its own type.** A caller has to be able to tell "top up
  the wallet" (420) from "fix your request" (430/440) from "just retry" (201), and a single
  generic error would make that impossible.
* **the key never appears in a log or a raised body**, including the ``api_key`` *body*
  form, which is the variant a payload log would otherwise print, and including the key
  sitting in a *value* — which is how the live 410 body talks about it.

**Where the expected values come from.** The vocabulary tests read
``schema/live-message.json`` rather than the constants they are checking. A test that
iterates ``CHANNELS`` to prove ``CHANNELS`` is right cannot fail; one that iterates the
spec's ``enum`` can. Same for the ``extra_prompt`` cap and the status codes.
"""

from __future__ import annotations

import atexit
import json
import logging
import random
import shutil
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from closewire_client.auth import ApiKeyAuth
from closewire_client.config import Config, redact_secret
from closewire_client.live import (
    CHANNELS,
    MAX_EXTRA_PROMPT,
    PAYLOAD_FIELDS,
    RETRY_STATUSES,
    STATUS_HELP,
    STATUS_MAP,
    AccountLimitReached,
    BotLimitReached,
    LiveMessageClient,
    LiveMessageError,
    LiveReply,
    MissingContactId,
    MissingMessage,
    NoAccount,
    NoCredits,
    RerunRequested,
    message_endpoint,
)
from closewire_client.pacing import RETRYABLE_STATUSES, Pacer
from closewire_client.redaction import REDACTED
from closewire_client.session import Session

SECRET = "cb_LIVE_TEST_KEY_never_real_4Q4Q"

#: The vendored runtime spec — the oracle for the channel, cap and status vocabularies.
SPEC = json.loads(
    (Path(__file__).resolve().parents[1] / "schema" / "live-message.json").read_text(
        encoding="utf-8"
    )
)
_PROPERTIES = SPEC["components"]["schemas"]["MessagePayload"]["properties"]
SPEC_CHANNELS: list[str] = _PROPERTIES["channel"]["enum"]
SPEC_MAX_EXTRA_PROMPT: int = _PROPERTIES["extra_prompt"]["maxLength"]
SPEC_STATUSES: set[int] = {int(code) for code in SPEC["paths"]["/message"]["post"]["responses"]}

#: A FRESH temp dir per run, removed at exit.
#:
#: A tripped breaker persists itself to `state_dir`, so these tests must not leave a latch
#: in `.closewire/` that halts the next real run. A *fixed* path under the system temp dir
#: was worse than it looked: a critic mutation-testing in a scratch copy tripped the breaker
#: there, and the latch then failed 30 tests in the pristine repo — from any checkout on the
#: machine, until someone deleted a file they had no reason to know about. Per-run and
#: self-cleaning removes the shared mutable state entirely.
STATE_DIR = tempfile.mkdtemp(prefix="closewire-live-tests-")
atexit.register(shutil.rmtree, STATE_DIR, True)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0
        #: Every duration the pacer asked to sleep, in order.
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    @property
    def backoffs(self) -> list[float]:
        """The non-zero sleeps.

        Only meaningful with :data:`RETRY_CONFIG`, which zeroes think-time and backoff
        jitter so that the only thing left that can sleep for a non-zero time is a computed
        backoff.
        """
        return [seconds for seconds in self.slept if seconds > 0]


#: Config for the retry tests. Think-time and backoff jitter are zeroed so each backoff is
#: exactly predictable; the 429 breaker is lifted out of the way because a retry loop reaches
#: its default threshold on its own and these tests are about the retry, not the breaker
#: (`tests/test_pacing.py` owns that).
RETRY_CONFIG: dict[str, Any] = {
    "min_delay_s": 0.0,
    "max_delay_s": 0.0,
    "jitter_s": 0.0,
    "backoff_jitter_s": 0.0,
    "breaker_429_threshold": 99,
}


def _client(
    status: int | Sequence[int] = 200,
    body: Any = None,
    *,
    dry_run: bool = False,
    seen: dict[str, Any] | None = None,
    response_headers: dict[str, str] | None = None,
    clock: _Clock | None = None,
    config_kwargs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[LiveMessageClient, Pacer]:
    """A client wired to a MockTransport.

    ``status`` may be a sequence, in which case successive requests get successive statuses
    and the last one repeats — that is how a retry is observed without a live server.
    """
    config = Config(
        api_key=SECRET, dry_run=dry_run, state_dir=STATE_DIR, **(config_kwargs or {})
    )
    clock = clock or _Clock()
    pacer = Pacer(config, monotonic=clock.monotonic, sleeper=clock.sleep,
                  rng=random.Random(20260726))
    statuses = [status] if isinstance(status, int) else list(status)
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        if seen is not None:
            seen["url"] = str(request.url)
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            seen["body"] = json.loads(request.content or b"{}")
            seen["count"] = calls["n"]
            # Sampled from *inside* the transport: at this instant the sending thread must
            # hold a pacing slot whose single send authorization has already been spent.
            seen["in_slot"] = pacer.in_slot
            seen["sends_left"] = pacer.sends_left
        return httpx.Response(
            statuses[index],
            json=body if body is not None else {},
            headers=response_headers,
        )

    client = LiveMessageClient(
        config, pacer=pacer, transport=httpx.MockTransport(handle), **kwargs
    )
    return client, pacer


def _capture(action: Callable[[], Any]) -> str:
    """Everything ``closewire.live`` logged while ``action`` ran, formatted, joined."""
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("closewire.live")
    handler = Capture()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        action()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return "\n".join(records)


# ── Endpoint resolution ───────────────────────────────────────────────────────
def test_endpoint_resolves_both_spellings_of_live_base() -> None:
    """Regression: `live_base` defaults to a full endpoint, but the spec's server is a base.

    Appending unconditionally produced `/message/message`, which API Gateway answers with
    `403 Missing Authentication Token` — a message that reads like a credentials problem.
    """
    for value in (
        "https://api.closebot.ai",
        "https://api.closebot.ai/",
        "https://api.closebot.ai/message",
        "https://api.closebot.ai/message/",
    ):
        assert message_endpoint(value) == "https://api.closebot.ai/message", value


def test_the_client_posts_to_the_resolved_endpoint() -> None:
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(id="lead_1", message="hi")
    assert seen["url"] == "https://api.closebot.ai/message", seen["url"]


def test_the_dry_run_log_prints_the_url_the_send_would_really_use() -> None:
    """Regression: the dry-run log printed `/message/message` while the POST went elsewhere.

    Verbatim the string that cost this module a 403-Missing-Authentication-Token
    misdiagnosis — reproduced in the one place whose whole job is to show the operator the
    request they are about to authorize. The expected URL is not written out here: it is
    taken from what the transport actually received, so the log and the wire cannot drift.
    """
    seen: dict[str, Any] = {}
    sending, _ = _client(seen=seen)
    sending.send_message(id="lead_1", message="hi")
    real_url = seen["url"]

    previewing, _ = _client(dry_run=True)
    blob = _capture(lambda: previewing.send_message(id="lead_1", message="hi"))

    printed = re.search(r"would send POST (\S+)", blob)
    assert printed, f"the dry run did not name a URL at all: {blob!r}"
    assert printed.group(1) == real_url, "the dry-run log names a URL the send does not use"
    assert "/message/message" not in blob


# ── Status codes ──────────────────────────────────────────────────────────────
def test_every_documented_status_maps_to_its_own_type() -> None:
    expected = {
        201: RerunRequested, 410: NoAccount, 420: NoCredits, 430: MissingContactId,
        440: MissingMessage, 450: BotLimitReached, 460: AccountLimitReached,
    }
    assert STATUS_MAP == expected
    for code, exc_type in expected.items():
        client, _ = _client(code, {"error": "x"})
        try:
            client.send_message(id="lead_1", message="hi")
            raise AssertionError(f"{code} did not raise")
        except LiveMessageError as exc:
            assert type(exc) is exc_type, f"{code} raised {type(exc).__name__}"
            # `exc.status_code == code` alone proves nothing — each class hardcodes it as a
            # class attribute, so it holds however wrong the table is. What can actually
            # drift is the class attribute disagreeing with the key it is filed under.
            assert exc_type.status_code == code, f"{exc_type.__name__} disagrees with the map"
            assert exc.status_code == code
            assert exc.body == {"error": "x"}


def test_the_status_vocabulary_is_the_spec_s() -> None:
    """Pinned against `schema/live-message.json`, not against `STATUS_MAP` itself."""
    assert set(STATUS_MAP) == SPEC_STATUSES - {200}, "the map and the spec disagree"
    assert 200 in SPEC_STATUSES, "200 must stay the one success the spec declares"
    assert LiveMessageError.status_code is None, (
        "the base class must not claim a code, or an undocumented status would be reported "
        "as whatever it inherited"
    )


def test_every_mapped_status_has_help_text() -> None:
    """A typed error nobody can act on is only marginally better than a generic one."""
    assert set(STATUS_HELP) == set(STATUS_MAP)
    for code, text in STATUS_HELP.items():
        assert text and len(text) > 10, code


def test_out_of_credits_says_so_in_words() -> None:
    client, _ = _client(420, {"error": "no credits"})
    try:
        client.send_message(id="lead_1", message="hi")
        raise AssertionError("420 did not raise")
    except NoCredits as exc:
        assert "CREDITS" in str(exc).upper()


def test_an_undocumented_status_is_not_silently_a_success() -> None:
    client, _ = _client(418, {"teapot": True})
    try:
        client.send_message(id="lead_1", message="hi")
        raise AssertionError("418 was treated as success")
    except LiveMessageError as exc:
        # This one *is* meaningful: the base class defaults to None, so the code can only be
        # here if it was carried off the response.
        assert exc.status_code == 418
        assert "undocumented" in str(exc)


def test_a_200_returns_the_reply_and_goals() -> None:
    client, _ = _client(200, {"message": "Hello there.", "goals_finished": ["greet"]})
    reply = client.send_message(id="lead_1", message="hi")
    assert isinstance(reply, LiveReply)
    assert reply.text == "Hello there."
    assert reply.goals_finished == ["greet"]
    assert reply.sent is True


def test_a_reply_with_no_recognisable_text_returns_none_rather_than_inventing_one() -> None:
    """The response shape is undeclared, so a missing reply must read as missing."""
    client, _ = _client(200, {"unexpected": "shape"})
    assert client.send_message(id="lead_1", message="hi").text is None


# ── Pacing, retries and dry run ───────────────────────────────────────────────
def test_a_send_takes_the_write_lane() -> None:
    """It spends a credit, so it is a write — not a read."""
    client, pacer = _client(200, {"message": "ok"})
    client.send_message(id="lead_1", message="hi")
    stats = pacer.stats()
    assert stats.writes_last_hour == 1
    assert stats.ops_last_hour == 1, "a send must not land in the read lane"


def test_a_send_spends_the_pacers_one_shot_token() -> None:
    """`assert_in_slot` is what makes "no unpaced route to the runtime endpoint" structural.

    Observed from inside the transport rather than by patching the pacer: at the moment the
    request is handed over, the thread must hold a slot whose single send authorization has
    already been consumed. Delete the `assert_in_slot` call and `sends_left` is still 1 here,
    which is the state in which a second send would go out on one think-time.
    """
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(id="lead_1", message="hi")
    assert seen["in_slot"] is True, "the send did not happen inside a pacing slot"
    assert seen["sends_left"] == 0, "the slot's one-shot send token was never consumed"


def test_dry_run_sends_nothing_and_says_so() -> None:
    seen: dict[str, Any] = {}
    client, pacer = _client(200, {"message": "ok"}, dry_run=True, seen=seen)
    reply = client.send_message(id="lead_1", message="hi")
    assert reply.sent is False
    assert reply.status_code == 0
    assert seen == {}, f"dry run reached the transport: {seen}"
    assert pacer.stats().dry_run_blocked == 1


def test_a_throttled_send_is_retried_with_the_pacers_backoff() -> None:
    """Regression: the retry decision was computed and then `del`-eted for every status.

    The Pacer logged `backing off 2.5s` while nothing backed off and the caller got
    `undocumented status 429` on the first try — phase 04's guarantee, silently absent on
    this surface.
    """
    clock = _Clock()
    seen: dict[str, Any] = {}
    client, pacer = _client(
        [429, 429, 200], {"message": "ok"}, seen=seen, clock=clock,
        config_kwargs=dict(RETRY_CONFIG, max_retries=3),
    )
    reply = client.send_message(id="lead_1", message="hi")
    assert reply.text == "ok"
    assert seen["count"] == 3, "the 429s were not re-sent"
    # backoff_base_s 2.0, no jitter: 2 * 2**0, then 2 * 2**1.
    assert clock.backoffs == [2.0, 4.0], clock.slept
    assert pacer.stats().writes_last_hour == 3, "a re-send must take its own paced slot"


def test_an_explicit_retry_after_is_honoured_as_given() -> None:
    """The Pacer promises to obey `Retry-After` rather than guess; that has to reach here."""
    clock = _Clock()
    seen: dict[str, Any] = {}
    client, _ = _client(
        [429, 200], {"message": "ok"}, seen=seen, clock=clock,
        response_headers={"Retry-After": "7"},
        config_kwargs=dict(RETRY_CONFIG, max_retries=1),
    )
    client.send_message(id="lead_1", message="hi")
    assert seen["count"] == 2
    assert clock.backoffs == [7.0], (
        f"slept {clock.backoffs} instead of the 7s the server asked for — the header never "
        "reached the pacer"
    )


def test_retries_are_bounded_and_the_status_still_surfaces() -> None:
    clock = _Clock()
    seen: dict[str, Any] = {}
    client, _ = _client(
        429, {"error": "slow down"}, seen=seen, clock=clock,
        config_kwargs=dict(RETRY_CONFIG, max_retries=2),
    )
    try:
        client.send_message(id="lead_1", message="hi")
        raise AssertionError("an unrelenting 429 was treated as a success")
    except LiveMessageError as exc:
        assert exc.status_code == 429
    assert seen["count"] == 3, "expected the first send plus max_retries re-sends"
    assert clock.backoffs == [2.0, 4.0]


def test_the_runtimes_own_statuses_are_never_re_sent() -> None:
    """A re-send is a second charged message, so a semantic answer must not trigger one."""
    for code in sorted(STATUS_MAP):
        seen: dict[str, Any] = {}
        client, _ = _client(
            code, {"error": "x"}, seen=seen, config_kwargs=dict(RETRY_CONFIG, max_retries=3)
        )
        try:
            client.send_message(id="lead_1", message="hi")
        except LiveMessageError:
            pass
        assert seen["count"] == 1, f"{code} was re-sent {seen['count']} times"


def test_a_403_is_not_re_sent_on_this_surface_even_though_the_pacer_retries_it() -> None:
    """403 means rate limiting on the REST host and a misrouted request on this one.

    On `api.closebot.ai` it is API Gateway's `Missing Authentication Token`. Re-sending
    cannot fix a wrong path, and every attempt feeds the auth breaker — so retrying would
    turn one mistyped `live_base` into a persisted halt on all Closebot traffic.
    """
    assert 403 in RETRYABLE_STATUSES, "the pacer no longer retries 403; revisit this rule"
    assert 403 not in RETRY_STATUSES
    seen: dict[str, Any] = {}
    client, _ = _client(
        403, {"message": "Missing Authentication Token"}, seen=seen,
        config_kwargs=dict(RETRY_CONFIG, max_retries=3),
    )
    raised: list[LiveMessageError] = []

    def send() -> None:
        try:
            client.send_message(id="lead_1", message="hi")
        except LiveMessageError as exc:
            raised.append(exc)

    blob = _capture(send)
    assert raised and raised[0].status_code == 403, "403 was treated as a success"
    assert seen["count"] == 1, "a misrouted request was re-sent"
    # The pacer has already logged "backing off 2.0s" at WARNING for this status. A backoff
    # line with nothing backing off after it is the trail that led to `del decision`, so the
    # narrowing has to say so rather than leave the same trail behind.
    assert "not re-sending" in blob, "the declined backoff was left unexplained in the log"


def test_a_status_the_runtime_declares_wins_over_the_pacers_retry_vocabulary() -> None:
    """The guard that makes "nothing in STATUS_MAP is auto-resent" a property, not luck.

    `RETRY_STATUSES` and `STATUS_MAP` are disjoint today, so this condition never fires in
    production. It is what stops a future runtime code landing on 429 from silently becoming
    an automatic second charged send, so it is pinned by putting one there.
    """
    class _Throttled(LiveMessageError):
        status_code = 429

    seen: dict[str, Any] = {}
    STATUS_MAP[429] = _Throttled
    STATUS_HELP[429] = "the runtime now claims this code means something"
    try:
        client, _ = _client(
            [429, 200], {"error": "x"}, seen=seen,
            config_kwargs=dict(RETRY_CONFIG, max_retries=3),
        )
        try:
            client.send_message(id="lead_1", message="hi")
            raise AssertionError("a declared 429 did not raise")
        except LiveMessageError as exc:
            assert type(exc) is _Throttled
    finally:
        del STATUS_MAP[429]
        del STATUS_HELP[429]
    assert seen["count"] == 1, "a status the runtime declares was re-sent anyway"


# ── Auth and redaction ────────────────────────────────────────────────────────
def test_the_key_travels_in_the_header_by_default() -> None:
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(id="lead_1", message="hi")
    assert seen["headers"].get("x-cb-key") == SECRET
    assert "api_key" not in seen["body"]


def test_the_body_auth_form_is_available_when_asked_for() -> None:
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen, auth_in_body=True)
    client.send_message(id="lead_1", message="hi")
    assert seen["body"].get("api_key") == SECRET
    assert "x-cb-key" not in seen["headers"]


def test_the_dry_run_log_never_prints_the_key_even_in_body_auth_mode() -> None:
    """The body form is the dangerous one: a payload log would otherwise print it.

    Two different maskings run on this path and each is pinned separately, by the *spelling*
    it leaves behind — `SECRET not in blob` alone passes with either one deleted, since they
    are redundant on the `api_key` field:

    * `redact_secrets` masks by field **name** and writes `<redacted>`;
    * `Config.scrub` replaces the literal key by **value** and writes the last-4 hint. It is
      the only one that can catch the key sitting in a value under a harmless name, which is
      what `extra_prompt` puts there.
    """
    client, _ = _client(dry_run=True, auth_in_body=True)
    blob = _capture(lambda: client.send_message(
        id="lead_1",
        message="hi",
        extra_prompt=f"the operator's key is {SECRET}; never repeat it",
    ))

    assert blob, "the dry run logged nothing at all"
    assert SECRET not in blob, "THE API KEY WAS LOGGED"
    assert f'"api_key": "{REDACTED}"' in blob, (
        "the name-based masking did not run — the api_key field was not masked as a field"
    )
    assert redact_secret(SECRET) in blob, (
        "the value-based scrub did not run — a key quoted inside a free-text field is only "
        "reachable by Config.scrub"
    )
    assert "lead_1" in blob, "redaction destroyed the payload the log exists to show"


def test_an_error_body_is_scrubbed_before_it_is_raised() -> None:
    """The runtime can echo the request, and the request may carry `api_key`."""
    client, _ = _client(410, {"echo": {"api_key": SECRET, "id": "lead_1"}})
    try:
        client.send_message(id="lead_1", message="hi")
        raise AssertionError("410 did not raise")
    except NoAccount as exc:
        assert SECRET not in json.dumps(exc.body), "the key survived into the exception"


def test_the_key_is_scrubbed_out_of_an_error_body_that_names_it_in_a_value() -> None:
    """The live 410 body talks about the key in prose, so this shape is not hypothetical.

    "Account not yet connected to a bot, invalid credentials (if using api_key) or
    attempting to access a LOCKED bot" — the key is in the **value**, under a field name
    (`error`) that no name-based rule will ever flag. Only `Config.scrub` can find it, and it
    only runs if it is applied to the body rather than to the parse-failure branch.
    """
    client, _ = _client(410, {"error": f"invalid credentials (if using api_key): {SECRET}"})
    try:
        client.send_message(id="lead_1", message="hi")
        raise AssertionError("410 did not raise")
    except NoAccount as exc:
        rendered = json.dumps(exc.body)
        assert SECRET not in rendered, f"THE API KEY SURVIVED INTO NoAccount.body: {rendered}"
        assert redact_secret(SECRET) in rendered
        assert "invalid credentials" in rendered, "scrubbing ate the diagnosis"


def test_the_key_is_scrubbed_out_of_a_body_that_is_not_an_object() -> None:
    """A JSON body may be a bare string, and a bare string is not a dict or a list.

    That was the whole condition guarding redaction, so a body of `"rejected key <KEY>"`
    parsed cleanly and then had nothing applied to it at all.
    """
    client, _ = _client(410, f"rejected key {SECRET}")
    try:
        client.send_message(id="lead_1", message="hi")
        raise AssertionError("410 did not raise")
    except NoAccount as exc:
        assert isinstance(exc.body, str)
        assert SECRET not in exc.body, f"THE API KEY SURVIVED INTO NoAccount.body: {exc.body}"
        assert redact_secret(SECRET) in exc.body


def test_the_key_is_scrubbed_out_of_a_success_body_and_out_of_the_replys_repr() -> None:
    """`LiveReply.raw` is kept whole and the dataclass `repr` prints all of it."""
    client, _ = _client(200, {"message": "hi", "debug": {"receivedKey": SECRET}})
    reply = client.send_message(id="lead_1", message="hi")
    assert SECRET not in json.dumps(reply.raw), "THE API KEY SURVIVED INTO LiveReply.raw"
    assert SECRET not in repr(reply), "THE API KEY IS PRINTED BY repr(LiveReply)"
    assert reply.text == "hi", "scrubbing destroyed the reply the call exists to return"


def test_a_session_is_refused_rather_than_accepted_and_ignored() -> None:
    """`session=` was stored and never used: sends went through this client's own transport.

    So a caller who wired a Session to an `httpx.MockTransport` — the documented way to keep
    a test off the network — had it silently dropped and spent a real credit. Refusing names
    the two things they can pass instead.
    """
    config = Config(api_key=SECRET, state_dir=STATE_DIR)
    clock = _Clock()
    pacer = Pacer(config, monotonic=clock.monotonic, sleeper=clock.sleep,
                  rng=random.Random(20260726))
    session = Session(
        config,
        ApiKeyAuth.from_config(config),
        pacer,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        LiveMessageClient(config, session, pacer=pacer)
        raise AssertionError("a Session was accepted, and would have been ignored")
    except TypeError as exc:
        message = str(exc)
        assert "transport=" in message, "the refusal must name the argument to use instead"
        assert "pacer=" in message, "the refusal must say how to keep sharing one Pacer"


# ── The wire ──────────────────────────────────────────────────────────────────
def test_the_payload_that_reaches_the_wire_is_the_one_that_was_asked_for() -> None:
    """Nothing else checked that the message text or `is_update` leave the process."""
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(
        id="  lead_1  ", message="please call me back", channel="SMS", is_update=True,
        extra_prompt="be brief", character_limit=120, followup=False, bot="bot_9",
        goals_finished=["greet"], delay=3, limit=7, model="gpt-4",
    )
    body = seen["body"]
    assert body["id"] == "lead_1", "the id was not trimmed on its way to the wire"
    assert body["message"] == "please call me back"
    assert body["is_update"] is True
    assert body["channel"] == "SMS"
    assert body["extra_prompt"] == "be brief"
    assert body["character_limit"] == 120
    assert body["followup"] is False
    assert body["bot"] == "bot_9"
    assert body["goals_finished"] == ["greet"]
    assert body["delay"] == 3
    assert body["limit"] == 7
    assert body["model"] == "gpt-4"
    undeclared = set(body) - PAYLOAD_FIELDS
    assert not undeclared, f"fields on the wire the spec does not declare: {undeclared}"


def test_is_update_is_absent_unless_it_was_asked_for() -> None:
    """The spec defaults it to false; sending it anyway would state an intent nobody had."""
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(id="lead_1", message="hi")
    assert "is_update" not in seen["body"]


# ── Local refusals: cheap mistakes must not cost a credit ─────────────────────
def test_locally_knowable_mistakes_are_refused_before_the_slot_is_taken() -> None:
    seen: dict[str, Any] = {}
    client, pacer = _client(seen=seen)
    for kwargs, why in [
        ({"id": "", "message": "hi"}, "blank id"),
        ({"id": "   ", "message": "hi"}, "whitespace id"),
        ({"id": "lead_1", "message": "hi", "channel": "Telepathy"}, "bad channel"),
        # Derived from the SPEC's maxLength, not from MAX_EXTRA_PROMPT — deriving it from
        # the constant under test makes a wrong cap undetectable.
        ({"id": "lead_1", "message": "x" * 10,
          "extra_prompt": "y" * (SPEC_MAX_EXTRA_PROMPT + 1)}, "over-long extra_prompt"),
        ({"id": "lead_1", "message": "hi", "variables": {"lead_name": "Ada"}},
         "variable without @"),
    ]:
        try:
            client.send_message(**kwargs)  # type: ignore[arg-type]
            raise AssertionError(f"{why} was accepted")
        except ValueError:
            pass
    assert seen == {}, "a refused call reached the transport"
    assert pacer.stats().ops_last_hour == 0, "a refused call consumed budget"


def test_the_extra_prompt_cap_is_the_one_the_spec_declares() -> None:
    """Both sides of the cap, so neither a loose one nor a tight one can hide."""
    assert MAX_EXTRA_PROMPT == SPEC_MAX_EXTRA_PROMPT, "the cap and the spec disagree"
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(id="lead_1", message="hi", extra_prompt="y" * SPEC_MAX_EXTRA_PROMPT)
    assert len(seen["body"]["extra_prompt"]) == SPEC_MAX_EXTRA_PROMPT, (
        "a prompt of exactly the documented length was not sent"
    )


def test_the_channel_vocabulary_is_the_one_the_spec_declares() -> None:
    assert CHANNELS == frozenset(SPEC_CHANNELS), "the channel list and the spec disagree"


def test_every_channel_the_spec_declares_is_accepted() -> None:
    """Iterates the SPEC, not `CHANNELS` — the control has to come from outside the code."""
    for channel in SPEC_CHANNELS:
        seen: dict[str, Any] = {}
        client, _ = _client(seen=seen)
        client.send_message(id="lead_1", message="hi", channel=channel)
        assert seen["body"]["channel"] == channel


def test_custom_at_variables_are_passed_through() -> None:
    """Custom variables are per-bot, so an allowlist would break the documented feature."""
    seen: dict[str, Any] = {}
    client, _ = _client(seen=seen)
    client.send_message(id="lead_1", message="hi",
                        variables={"@lead_name": "Ada", "@custom_thing": "x"})
    assert seen["body"]["@lead_name"] == "Ada"
    assert seen["body"]["@custom_thing"] == "x"


def test_declining_a_backoff_clears_it_so_pacing_status_does_not_lie() -> None:
    """Regression: a declined 403 backoff was left set on the Pacer forever.

    `note_response` sets a backoff for any status it classifies retryable, and every
    non-retrying path inside `pacing.py` clears it again. This surface narrows the retry
    vocabulary to 429, which created a path `pacing.py` cannot see — so `pacing-status`
    reported a delay nobody was waiting out. Invisible in a short CLI run; reported for
    hours by phase 11's long-lived MCP process.
    """
    client, pacer = _client(403, {"error": "x"})
    try:
        client.send_message(id="lead_1", message="hi")
    except LiveMessageError:
        pass
    assert pacer.stats().current_backoff_s == 0.0, "a declined backoff was left reported"


def test_a_retried_429_still_reports_no_stale_backoff_once_it_succeeds() -> None:
    """The control: clearing on decline must not break the path that genuinely backs off."""
    clock = _Clock()
    client, pacer = _client([429, 200], {"message": "ok"}, clock=clock,
                            config_kwargs=RETRY_CONFIG)
    reply = client.send_message(id="lead_1", message="hi")
    assert reply.text == "ok"
    assert clock.backoffs, "the 429 was not backed off at all"
    assert pacer.stats().current_backoff_s == 0.0

if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} live-runtime tests passed.")

