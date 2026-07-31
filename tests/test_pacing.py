"""Phase-04 pacing tests — deterministic, offline, instant.

Every test injects a fake clock/sleeper so a one-hour budget wait costs no wall-clock
time. Nothing here touches the network or a real key.

Several tests exist specifically as regressions for defects the phase-04 review council
found and that the first draft shipped green: the budget TOCTOU, the breaker not being
re-checked after a blocking wait, `Retry-After` truncation, `Session` sending unpaced,
search-POSTs being dry-run-suppressed, and a clock/sleeper mismatch spinning forever.

Run via pytest (``pytest tests/test_pacing.py``) or directly
(``python tests/test_pacing.py``), matching the phase-03 convention.
"""

from __future__ import annotations

import atexit
import logging
import random
import shutil
import tempfile
import threading
import time

import httpx

from closewire_client.auth import ApiKeyAuth
from closewire_client.config import Config, ConfigError
from closewire_client.pacing import (
    WINDOW_S,
    BreakerState,
    NestedSlotError,
    Pacer,
    PacingBypassError,
    PacingHalt,
)
from closewire_client.rest import RestClient
from closewire_client.session import Session

SECRET = "cb_TEST_KEY_never_real_7Q7Q"


class FakeClock:
    """A monotonic clock that only advances when something sleeps.

    The clock and the sleeper are deliberately *coupled* — this is the pattern later
    phases should copy. A sleeper that does not advance its clock is now rejected by the
    Pacer (see ``test_mismatched_clock_and_sleeper_raises_instead_of_spinning``).
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


_TEMP_DIRS: list[str] = []


def _temp_state_dir() -> str:
    """A throwaway state dir, cleaned up when the run ends (not left behind by the dozen)."""
    path = tempfile.mkdtemp(prefix="closewire-test-")
    _TEMP_DIRS.append(path)
    return path


@atexit.register
def _cleanup_temp_dirs() -> None:
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


def _config(**overrides: object) -> Config:
    base: dict[str, object] = {
        "api_key": SECRET,
        "min_delay_s": 1.0,
        "max_delay_s": 4.0,
        "jitter_s": 0.35,
        "write_delay_mult": 2.0,
        "max_ops_per_hour": 300,
        "max_writes_per_hour": 60,
        # Isolate persisted breaker state per test.
        "state_dir": _temp_state_dir(),
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _pacer(**overrides: object) -> tuple[Pacer, FakeClock]:
    clock = FakeClock()
    pacer = Pacer(
        _config(**overrides),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        rng=random.Random(20260725),  # seeded: randomized but reproducible
    )
    return pacer, clock


def _records(logger_name: str = "closewire.pacing") -> tuple[list[logging.LogRecord], object]:
    """Attach a capturing handler; returns (records, detach_callable)."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)

    def detach() -> None:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    return records, detach


# ── 1. Randomized think-time within the band ──────────────────────────────────
def test_delays_are_drawn_across_the_whole_band() -> None:
    """Not just 'inside the band' — the draws must actually span it.

    The weaker earlier assertion passed even when every delay collapsed to the 1.0s
    floor with only jitter on top, which would have made all traffic ~2.5x faster.
    """
    pacer, clock = _pacer(jitter_s=0.0)
    for _ in range(200):
        with pacer.acquire(write=False):
            pass
    gaps = clock.slept

    assert all(1.0 <= g <= 4.0 for g in gaps), (min(gaps), max(gaps))
    span = 4.0 - 1.0
    # Draws must reach into the bottom and top fifths of the band.
    assert min(gaps) < 1.0 + span * 0.2, f"never sampled the low end: min={min(gaps)}"
    assert max(gaps) > 4.0 - span * 0.2, f"never sampled the high end: max={max(gaps)}"
    mean = sum(gaps) / len(gaps)
    assert 2.2 < mean < 2.8, f"mean {mean} is not centred in [1.0, 4.0]"


def test_ten_calls_show_randomized_gaps_within_band() -> None:
    pacer, clock = _pacer()
    for _ in range(10):
        with pacer.acquire(write=False):
            pass
    gaps = clock.slept
    assert len(gaps) == 10
    assert all(1.0 <= g <= 4.0 + 0.35 for g in gaps), gaps
    assert len(set(gaps)) >= 8, f"gaps look fixed, not randomized: {gaps}"


def test_jitter_is_actually_applied() -> None:
    """With a zero-width band, every delay would be identical if jitter were dropped."""
    pacer, clock = _pacer(min_delay_s=2.0, max_delay_s=2.0, jitter_s=0.5)
    for _ in range(30):
        with pacer.acquire():
            pass
    gaps = clock.slept
    assert len(set(gaps)) > 20, "jitter is not being added"
    assert all(2.0 <= g <= 2.5 for g in gaps)
    assert max(gaps) - min(gaps) > 0.2, "jitter range is too narrow to be real"


def test_write_multiplier_is_the_configured_ratio() -> None:
    """Pins the actual 2.0x, not merely 'writes are slower by some epsilon'."""
    reads, rclock = _pacer(jitter_s=0.0)
    for _ in range(50):
        with reads.acquire(write=False):
            pass
    writes, wclock = _pacer(jitter_s=0.0)
    for _ in range(50):
        with writes.acquire(write=True):
            pass

    ratio = (sum(wclock.slept) / len(wclock.slept)) / (sum(rclock.slept) / len(rclock.slept))
    assert 1.95 < ratio < 2.05, f"write multiplier is {ratio:.2f}, expected ~2.0"


def test_budget_window_is_one_hour() -> None:
    """Pins the window width. Shrinking it to 60s would permit 60x the traffic."""
    assert WINDOW_S == 3600.0

    pacer, clock = _pacer(max_ops_per_hour=1, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0)
    with pacer.acquire():
        pass
    start = clock.now
    with pacer.acquire():
        pass
    assert 3599.0 <= clock.now - start <= 3601.0, "the budget window is not one hour"


# ── 2. Concurrency: writes serial, reads bounded ──────────────────────────────
def test_writes_are_serial_and_reads_are_bounded() -> None:
    for write, limit in ((True, 1), (False, 3)):
        pacer, _ = _pacer(max_read_concurrency=3)
        peak = 0
        current = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal peak, current
            with pacer.acquire(write=write):
                with guard:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.02)  # real overlap window
                with guard:
                    current -= 1

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads), "pacing deadlocked"
        assert peak <= limit, f"{'write' if write else 'read'} concurrency {peak} exceeded {limit}"
        assert peak >= 1


class _BudgetWouldBlock(Exception):
    """Raised by the test sleeper instead of actually waiting out an hour."""


def test_op_ceiling_holds_under_concurrent_readers() -> None:
    """REGRESSION: the ceiling was check-then-act, so concurrent readers all passed.

    Before the fix, N readers each saw the same pre-record count, all passed the check,
    then all recorded — putting more calls on the wire than the ceiling permits, with
    zero budget waits logged.
    """
    ceiling = 4
    real_sleep = time.sleep

    def sleeper(seconds: float) -> None:
        if seconds > 60:  # a budget wait — don't actually sit out the hour
            raise _BudgetWouldBlock
        real_sleep(seconds)

    pacer = Pacer(
        _config(
            max_ops_per_hour=ceiling,
            max_read_concurrency=8,
            min_delay_s=0.01,
            max_delay_s=0.03,
            jitter_s=0.0,
        ),
        sleeper=sleeper,
        rng=random.Random(1),
    )

    start = threading.Barrier(12)
    granted = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal granted
        start.wait()
        try:
            with pacer.acquire(write=False):
                with guard:
                    granted += 1
        except _BudgetWouldBlock:
            pass

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "pacing deadlocked"

    assert granted == ceiling, f"{granted} calls passed a ceiling of {ceiling}"
    assert pacer.stats().ops_last_hour == ceiling


def test_op_budget_blocks_until_window_frees() -> None:
    pacer, clock = _pacer(max_ops_per_hour=2, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0)
    for _ in range(2):
        with pacer.acquire():
            pass
    assert pacer.stats().ops_last_hour == 2

    start = clock.now
    with pacer.acquire():
        pass
    waited = clock.now - start
    assert waited >= WINDOW_S - 1, f"expected a ~{WINDOW_S:.0f}s budget wait, waited {waited}"
    assert pacer.stats().budget_waits == 1
    assert pacer.stats().ops_last_hour == 1


def test_budget_waits_are_counted_once_per_episode() -> None:
    """One blocking episode counts once, however many internal rounds it takes.

    At a ceiling of 1, calls 2 and 3 each block for their own window: two episodes.
    """
    pacer, clock = _pacer(max_ops_per_hour=1, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0)
    for _ in range(3):
        with pacer.acquire():
            pass
    stats = pacer.stats()
    assert stats.budget_waits == 2, stats.budget_waits
    # Recorded wait time is what the clock actually advanced, not a computed guess.
    assert abs(stats.total_budget_wait_s - 2 * WINDOW_S) < 1.0, stats.total_budget_wait_s


def test_a_multi_round_wait_still_counts_as_one_episode() -> None:
    """The coupled FakeClock always frees the window in one round, so it never exercises
    the once-per-episode guard. Under-advance the clock to force several rounds."""
    rounds: list[float] = []

    class SlowClock:
        def __init__(self) -> None:
            self.now = 1_000.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            rounds.append(seconds)
            self.now += seconds * 0.6  # honors the sleep only partially

    clock = SlowClock()
    pacer = Pacer(
        _config(max_ops_per_hour=1, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        rng=random.Random(4),
    )
    with pacer.acquire():
        pass
    with pacer.acquire():
        pass

    assert len(rounds) > 1, "the wait did not take multiple rounds; guard not exercised"
    assert pacer.stats().budget_waits == 1, (
        f"one blocking episode counted as {pacer.stats().budget_waits}"
    )


def test_write_budget_is_separate_from_op_budget() -> None:
    pacer, clock = _pacer(
        max_ops_per_hour=100, max_writes_per_hour=1,
        min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0,
    )
    with pacer.acquire(write=True):
        pass
    start = clock.now
    with pacer.acquire(write=True):
        pass
    assert clock.now - start >= WINDOW_S - 1, "second write should wait on the write budget"
    start = clock.now
    with pacer.acquire(write=False):
        pass
    assert clock.now - start < 1.0, "reads must be unaffected by a saturated write budget"


def test_budget_wait_is_logged_without_secrets() -> None:
    records, detach = _records()
    try:
        pacer, _ = _pacer(max_ops_per_hour=1, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0)
        with pacer.acquire():
            pass
        with pacer.acquire():
            pass
    finally:
        detach()

    messages = [r.getMessage() for r in records]
    assert any("waiting" in m and "budget" in m for m in messages), messages
    assert all(SECRET not in m for m in messages)


def test_mismatched_clock_and_sleeper_raises_instead_of_spinning() -> None:
    """REGRESSION: a no-op sleeper with a real clock used to spin forever in the budget wait.

    Phase 04 originally shipped exactly that construction as the recommended test helper.
    """
    pacer = Pacer(
        _config(max_ops_per_hour=1, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0),
        sleeper=lambda _seconds: None,  # never advances the (real) clock
        rng=random.Random(3),
    )
    with pacer.acquire():
        pass

    started = time.monotonic()
    try:
        with pacer.acquire():
            raise AssertionError("second call should not have been granted")
    except Exception as exc:
        assert "clock" in str(exc).lower(), f"unhelpful error: {exc}"
    assert time.monotonic() - started < 5, "the budget wait spun instead of failing fast"


# ── 3. Backoff + circuit breaker ──────────────────────────────────────────────
def test_repeated_429s_trip_the_breaker_with_a_clear_halt() -> None:
    pacer, _ = _pacer(breaker_429_threshold=5, max_retries=32)
    for attempt in range(4):
        assert pacer.note_response(429, attempt=attempt).should_retry

    try:
        pacer.note_response(429, attempt=4)
    except PacingHalt as exc:
        assert "429" in str(exc)
        assert "circuit breaker OPEN" in str(exc)
        assert "pacing-reset" in str(exc), "the halt must say how to recover"
    else:
        raise AssertionError("expected PacingHalt after repeated 429s")

    assert pacer.breaker_open
    for write in (False, True):
        try:
            with pacer.acquire(write=write):
                raise AssertionError("breaker did not block the call")
        except PacingHalt:
            pass

    pacer.reset_breaker()
    assert not pacer.breaker_open
    assert pacer.stats().breaker_state == BreakerState.CLOSED
    with pacer.acquire():
        pass


def test_breaker_stops_a_call_already_inside_the_pacer() -> None:
    """REGRESSION: a call parked in the budget/think-time wait used to be sent anyway.

    The breaker was checked before the waits but not after, so anything queued inside the
    safety layer when it tripped was still released onto the wire.
    """
    pacer, _ = _pacer(min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0, breaker_auth_threshold=1)

    tripped = threading.Event()
    original_delay = pacer._delay

    def slow_delay(**kwargs: object) -> None:
        # Trip the breaker while this call sits in its think-time.
        try:
            pacer.note_response(401)
        except PacingHalt:
            pass
        tripped.set()
        original_delay(**kwargs)  # type: ignore[arg-type]

    pacer._delay = slow_delay  # type: ignore[method-assign]

    try:
        with pacer.acquire(write=False):
            raise AssertionError("call was released after the breaker opened")
    except PacingHalt:
        pass
    assert tripped.is_set()
    assert pacer.breaker_open


def test_breaker_survives_a_restart() -> None:
    """A halt must outlive the process, or 'stop all traffic' means 'until you re-run'."""
    config = _config(breaker_auth_threshold=1)
    first = Pacer(config, monotonic=FakeClock().monotonic, sleeper=lambda _s: None)
    try:
        first.note_response(401)
    except PacingHalt:
        pass
    assert first.breaker_open

    revived = Pacer(config, monotonic=FakeClock().monotonic, sleeper=lambda _s: None)
    assert revived.breaker_open, "a tripped breaker did not survive the restart"
    assert "previous run" in revived.stats().breaker_reason

    revived.reset_breaker()
    assert not Pacer(config).breaker_open, "reset did not clear the persisted halt"


def test_a_corrupt_breaker_file_halts_rather_than_failing_open() -> None:
    """An unreadable safety latch is treated as engaged, and says so loudly.

    Silently starting CLOSED on malformed state would mean a crash mid-write quietly
    resumes traffic against whatever tripped the breaker in the first place.
    """
    import json as _json
    from pathlib import Path

    for payload in ("{not json", "", "null", "[]", '{"opened_at": "x"}', '{"reason": ""}'):
        cfg = _config()
        path = Path(cfg.state_dir) / "breaker.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

        records, detach = _records()
        try:
            pacer = Pacer(cfg, sleeper=lambda _s: None)
        finally:
            detach()

        assert pacer.breaker_open, f"corrupt state {payload!r} silently failed open"
        assert "unreadable" in pacer.stats().breaker_reason
        assert any(r.levelno >= logging.ERROR for r in records), "corruption was not logged"

        pacer.reset_breaker()
        assert not Pacer(cfg, sleeper=lambda _s: None).breaker_open, "reset did not recover"

    # A well-formed halt still round-trips, and a missing file still means no halt.
    cfg = _config()
    path = Path(cfg.state_dir) / "breaker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"reason": "3 recent 401/403", "opened_at": "t"}), encoding="utf-8")
    assert "401/403" in Pacer(cfg, sleeper=lambda _s: None).stats().breaker_reason
    assert not Pacer(_config(), sleeper=lambda _s: None).breaker_open


def test_an_unreadable_latch_halts_and_stays_recoverable() -> None:
    """REGRESSION: invalid UTF-8 raised `UnicodeDecodeError` out of `Pacer.__init__`,
    which is not an OSError — so it crashed every command and `pacing-reset` could not
    clear it. Fails closed, but defeated the documented recovery path."""
    from pathlib import Path

    cfg = _config()
    path = Path(cfg.state_dir) / "breaker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00binary garbage\xc3\x28")

    pacer = Pacer(cfg, sleeper=lambda _s: None)
    assert pacer.breaker_open, "an unreadable latch must be treated as engaged"
    assert "unreadable" in pacer.stats().breaker_reason
    assert pacer.reset_breaker() is True, "pacing-reset must be able to clear it"
    assert not Pacer(cfg, sleeper=lambda _s: None).breaker_open


def test_a_state_dir_that_is_not_a_directory_halts() -> None:
    """A misconfigured state dir must not read as 'no halt' (it did on Windows)."""
    from pathlib import Path

    parent = Path(_temp_state_dir())
    not_a_dir = parent / "state-file"
    not_a_dir.write_text("i am a file", encoding="utf-8")

    pacer = Pacer(_config(state_dir=str(not_a_dir)), sleeper=lambda _s: None)
    assert pacer.breaker_open, "a non-directory state dir silently disabled persistence"
    assert pacer.reset_breaker() is False, "reset must report that it could not clear"


def test_reset_reports_failure_rather_than_false_success() -> None:
    """Two distinct failure routes: an unusable state dir, and an unremovable latch."""
    from pathlib import Path

    # (a) the state dir itself is a regular file
    parent = Path(_temp_state_dir())
    not_a_dir = parent / "blocked"
    not_a_dir.write_text("x", encoding="utf-8")
    pacer = Pacer(_config(state_dir=str(not_a_dir)), sleeper=lambda _s: None)
    assert pacer.reset_breaker() is False

    # (b) breaker.json exists but cannot be unlinked (it is a non-empty directory)
    cfg = _config()
    latch = Path(cfg.state_dir) / "breaker.json"
    latch.mkdir(parents=True, exist_ok=True)
    (latch / "occupant").write_text("blocks rmdir", encoding="utf-8")
    stuck = Pacer(cfg, sleeper=lambda _s: None)
    assert stuck.breaker_open, "an unreadable latch should still halt"
    assert stuck.reset_breaker() is False, "reset claimed success on an unremovable latch"

    # (c) the healthy path still reports success
    assert Pacer(_config(), sleeper=lambda _s: None).reset_breaker() is True


def test_concurrent_trips_do_not_lose_the_persisted_latch() -> None:
    """REGRESSION: a shared temp filename made concurrent trips drop the latch 53-83% of
    the time — and a revoked key trips every in-flight call at once, which is exactly
    when the halt matters."""
    from pathlib import Path

    cfg = _config(breaker_auth_threshold=1)
    pacers = [Pacer(cfg, sleeper=lambda _s: None) for _ in range(8)]
    barrier = threading.Barrier(len(pacers))

    def trip(p: Pacer) -> None:
        barrier.wait()
        try:
            p.note_response(401)
        except PacingHalt:
            pass

    threads = [threading.Thread(target=trip, args=(p,)) for p in pacers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    latch = Path(cfg.state_dir) / "breaker.json"
    assert latch.exists(), "concurrent trips lost the persisted halt entirely"
    assert Pacer(cfg, sleeper=lambda _s: None).breaker_open
    strays = list(Path(cfg.state_dir).glob("*.tmp"))
    assert not strays, f"temp files left behind: {strays}"


def test_state_dir_is_anchored_to_the_project_not_the_cwd() -> None:
    """REGRESSION: a relative state dir let a halt be escaped by `cd`, and made
    `pacing-reset` report 'nothing to reset' while the real latch survived elsewhere."""
    import os
    from pathlib import Path

    from closewire_client.config import _resolve_state_dir

    assert Path(_resolve_state_dir(".closewire")).is_absolute()

    origin = os.getcwd()
    try:
        from_root = _resolve_state_dir(".closewire")
        os.chdir(Path(origin) / "closewire_client")
        from_subdir = _resolve_state_dir(".closewire")
    finally:
        os.chdir(origin)
    assert from_root == from_subdir, (
        f"state dir moved with the cwd: {from_root} vs {from_subdir}"
    )

    absolute = str(Path(origin) / "elsewhere")
    assert _resolve_state_dir(absolute) == absolute, "an absolute path must pass through"


def test_consecutive_auth_failures_trip_the_breaker() -> None:
    pacer, _ = _pacer(breaker_auth_threshold=3)
    pacer.note_response(401)
    pacer.note_response(403, attempt=0)
    try:
        pacer.note_response(401)
    except PacingHalt as exc:
        assert "401/403" in str(exc)
    else:
        raise AssertionError("expected PacingHalt after 3 recent 401/403")


def test_success_resets_the_failure_counters() -> None:
    pacer, _ = _pacer(breaker_auth_threshold=3, breaker_429_threshold=3)
    pacer.note_response(401)
    pacer.note_response(401)
    pacer.note_response(200)
    assert pacer.stats().recent_auth_failures == 0
    pacer.note_response(401)
    pacer.note_response(401)
    assert not pacer.breaker_open

    # The 429 counter must reset too, or scattered rate limits across a long run would
    # eventually halt everything despite every one of them succeeding on retry.
    pacer.reset_breaker()
    for _ in range(10):
        pacer.note_response(429, attempt=0)
        pacer.note_response(200)
        assert pacer.stats().recent_rate_limits == 0
    assert not pacer.breaker_open, "429s that each recovered should never trip the breaker"


def test_403_gets_backoff_and_retry_like_429() -> None:
    """Deliverable 4 names both statuses; only 429 was covered before."""
    pacer, _ = _pacer(
        backoff_base_s=2.0, backoff_cap_s=60.0, backoff_jitter_s=0.0,
        max_retries=6, breaker_auth_threshold=99,
    )
    for attempt, expected in enumerate([2.0, 4.0, 8.0]):
        decision = pacer.note_response(403, attempt=attempt)
        assert decision.should_retry, "a 403 must back off and retry, not fall straight through"
        assert decision.backoff_s == expected

    pacer.reset_breaker()
    honored = pacer.note_response(403, retry_after=45.0, attempt=0)
    assert honored.should_retry and honored.backoff_s == 45.0


def test_rest_retries_403_then_succeeds() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(403, json={"error": "throttled"})
        return httpx.Response(200, json={"ok": True})

    rest, _ = _mock_rest(handler, backoff_jitter_s=0.0, breaker_auth_threshold=99)
    assert rest.get("/bot") == {"ok": True}
    assert len(calls) == 2, "the 403 should have been retried once"


def test_backoff_is_exponential_and_capped() -> None:
    pacer, _ = _pacer(
        backoff_base_s=2.0, backoff_cap_s=60.0, backoff_jitter_s=0.0,
        max_retries=10, breaker_429_threshold=99,
    )
    delays = [pacer.note_response(429, attempt=n).backoff_s for n in range(6)]
    assert delays[:5] == [2.0, 4.0, 8.0, 16.0, 32.0], delays
    assert delays[5] == 60.0, "backoff must cap"


def test_backoff_never_exceeds_the_cap_even_with_jitter() -> None:
    pacer, _ = _pacer(
        backoff_base_s=2.0, backoff_cap_s=10.0, backoff_jitter_s=5.0,
        max_retries=30, breaker_429_threshold=99,
    )
    for attempt in range(30):
        assert pacer.note_response(429, attempt=attempt).backoff_s <= 10.0


def test_retry_after_is_honored_not_truncated() -> None:
    """REGRESSION: `Retry-After: 300` used to be silently shortened to the 60s cap.

    Retrying earlier than the server explicitly asked is the exact opposite of being a
    good API citizen, and the old test asserted the truncation as correct.
    """
    pacer, _ = _pacer(
        backoff_cap_s=60.0, backoff_jitter_s=0.0, max_retries=10,
        breaker_429_threshold=99, retry_after_max_s=900.0,
    )
    for asked in (7.0, 90.0, 300.0, 900.0):
        decision = pacer.note_response(429, retry_after=asked, attempt=0)
        assert decision.should_retry
        assert decision.backoff_s == asked, (
            f"server asked for {asked}s, pacer chose {decision.backoff_s}s"
        )


def test_retry_after_beyond_the_limit_surfaces_instead_of_retrying_early() -> None:
    pacer, _ = _pacer(max_retries=10, breaker_429_threshold=99, retry_after_max_s=900.0)
    decision = pacer.note_response(429, retry_after=3600.0, attempt=0)
    assert not decision.should_retry
    assert "3600" in decision.reason and "RETRY_AFTER_MAX" in decision.reason


def test_retries_are_exhausted_then_surfaced() -> None:
    pacer, _ = _pacer(max_retries=2, breaker_429_threshold=99)
    assert pacer.note_response(429, attempt=0).should_retry
    assert pacer.note_response(429, attempt=1).should_retry
    final = pacer.note_response(429, attempt=2)
    assert not final.should_retry and "exhausted" in final.reason
    assert pacer.stats().current_backoff_s == 0.0, "stale backoff still reported"


def test_non_retryable_statuses_pass_straight_through() -> None:
    pacer, _ = _pacer()
    for status in (200, 201, 404, 500):
        assert not pacer.note_response(status).should_retry


# ── 4. Dry-run ────────────────────────────────────────────────────────────────
def test_dry_run_blocks_the_write_but_still_logs_and_counts() -> None:
    records, detach = _records()
    try:
        pacer, _ = _pacer(dry_run=True)
        with pacer.acquire(write=True, description="POST /bot") as slot:
            assert slot.dry_run_blocked, "dry-run must tell the caller not to send"
    finally:
        detach()

    stats = pacer.stats()
    assert stats.dry_run is True
    assert stats.dry_run_blocked == 1
    assert stats.total_writes == 1, "a suppressed write still consumes the write budget"
    assert stats.total_ops == 1, "a suppressed write still consumes the op budget"
    assert stats.writes_last_hour == 1

    # The "logs it" half of the requirement, and at a level visible with no logging
    # config at all — log.info would be swallowed by logging's WARNING lastResort floor.
    dry = [r for r in records if "DRY RUN" in r.getMessage()]
    assert dry, "a suppressed write must be logged"
    assert dry[0].levelno >= logging.WARNING, "dry-run log is invisible by default"
    assert "POST /bot" in dry[0].getMessage()


def test_dry_run_leaves_reads_alone() -> None:
    pacer, _ = _pacer(dry_run=True)
    with pacer.acquire(write=False) as slot:
        assert not slot.dry_run_blocked, "reads are never suppressed by dry-run"


# ── 5. No bypass path ─────────────────────────────────────────────────────────
def test_rest_client_always_has_a_pacer() -> None:
    rest = RestClient(_config())
    assert rest.pacer is not None, "RestClient must never run unpaced"


def test_client_and_session_must_share_one_pacer() -> None:
    """Two Pacers would make every send look like a bypass and blame the wrong thing."""
    cfg = _config()
    one = Pacer(cfg, sleeper=lambda _s: None)
    two = Pacer(cfg, sleeper=lambda _s: None)
    session = Session(cfg, ApiKeyAuth.from_config(cfg), one,
                      transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    try:
        RestClient(cfg, session, pacer=two)
    except ValueError as exc:
        assert "share one Pacer" in str(exc)
    else:
        raise AssertionError("a mismatched Pacer was accepted")

    assert RestClient(cfg, session, pacer=one).pacer is one
    assert RestClient(cfg, session).pacer is one, "the session's pacer should be adopted"


def test_session_refuses_to_send_outside_a_pacing_slot() -> None:
    """REGRESSION: `Session` was a fully-authenticated, entirely unpaced network primitive.

    Its `pacer` argument was stored and never used, so any caller could construct one and
    send at full speed with the API key attached.
    """
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    cfg = _config()
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(5))
    session = Session(cfg, ApiKeyAuth.from_config(cfg), pacer,
                      transport=httpx.MockTransport(handler))

    try:
        session.request("POST", "/bot", json={"name": "unpaced"})
    except PacingBypassError as exc:
        assert "no pacing slot" in str(exc)
    else:
        raise AssertionError("Session sent an unpaced request")
    assert sent == [], "an unpaced request reached the wire"

    with pacer.acquire(write=True):
        session.request("POST", "/bot", json={"name": "paced"})
    assert len(sent) == 1, "a properly paced request should go through"

    # BOTH directions. The earlier version stopped here, so a mark that was set but never
    # *cleared* passed the suite while allowing unlimited unpaced traffic afterwards.
    try:
        session.request("POST", "/bot", json={"name": "after-slot"})
    except PacingBypassError:
        pass
    else:
        raise AssertionError("the slot mark leaked past the acquire() block")
    assert len(sent) == 1, "a request escaped after the slot closed"


def test_a_slot_authorizes_exactly_one_send() -> None:
    """A slot is a one-shot token, not a gate held open for a batch.

    Holding one slot and sending N requests would pay one think-time for N calls and
    count them as one op against the hourly ceiling.
    """
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    cfg = _config(max_ops_per_hour=2)
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(11))
    session = Session(cfg, ApiKeyAuth.from_config(cfg), pacer,
                      transport=httpx.MockTransport(handler))

    with pacer.acquire(write=True):
        session.request("POST", "/bot", json={"n": 1})
        try:
            session.request("POST", "/bot", json={"n": 2})
        except PacingBypassError as exc:
            assert "already spent" in str(exc)
        else:
            raise AssertionError("one slot authorized a second send")
    assert len(sent) == 1
    assert pacer.stats().ops_last_hour == 1


def test_dry_run_slot_cannot_reach_the_transport() -> None:
    """A suppressed write must be structurally unable to send, not merely expected to check."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    cfg = _config(dry_run=True)
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(12))
    session = Session(cfg, ApiKeyAuth.from_config(cfg), pacer,
                      transport=httpx.MockTransport(handler))

    with pacer.acquire(write=True, description="POST /bot") as slot:
        assert slot.dry_run_blocked
        try:
            session.request("POST", "/bot", json={"name": "should not send"})
        except PacingBypassError:
            pass
        else:
            raise AssertionError("a dry-run-suppressed write reached the transport")
    assert sent == []


def _nesting_raises(pacer: Pacer, *, outer_write: bool, inner_write: bool) -> bool:
    """Run a nested acquire on a watchdog thread; True if it raised instead of hanging."""
    done = threading.Event()

    def worker() -> None:
        try:
            with pacer.acquire(write=outer_write):
                try:
                    with pacer.acquire(write=inner_write):
                        pass
                except NestedSlotError:
                    done.set()
        except Exception:
            pass

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=5)
    return done.is_set()


def test_nested_slots_raise_instead_of_deadlocking() -> None:
    """Nesting a write inside a write would hang forever on the non-reentrant lane."""
    assert _nesting_raises(_pacer()[0], outer_write=True, inner_write=True)
    assert _nesting_raises(_pacer()[0], outer_write=True, inner_write=False)


def test_nesting_is_guarded_under_dry_run_too() -> None:
    """REGRESSION: the dry-run path skipped the thread mark, so `in_slot` was False while
    the write lane was held — a nested acquire hung forever, the exact deadlock the guard
    exists to prevent. Phase 07 is required to exercise its write path under dry-run
    first, so this was the mode new composite code would meet first.
    """
    for inner_write in (True, False):
        pacer, _ = _pacer(dry_run=True)
        assert _nesting_raises(pacer, outer_write=True, inner_write=inner_write), (
            f"nested acquire (inner_write={inner_write}) hung under dry-run"
        )


def test_a_failed_inner_acquire_does_not_clear_the_outer_mark() -> None:
    """The finally must only unwind what it actually set."""
    pacer, _ = _pacer(max_ops_per_hour=1, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0)
    with pacer.acquire(write=False):
        assert pacer.in_slot
        try:
            with pacer.acquire(write=False):  # nested -> raises before entering
                pass
        except NestedSlotError:
            pass
        assert pacer.in_slot, "a failed inner acquire cleared the outer thread's mark"
        assert pacer.sends_left == 1, "a failed inner acquire spent the outer token"


def _mock_rest(handler, **overrides: object) -> tuple[RestClient, FakeClock]:
    cfg = _config(**overrides)
    clock = FakeClock()
    pacer = Pacer(cfg, monotonic=clock.monotonic, sleeper=clock.sleep, rng=random.Random(7))
    session = Session(cfg, ApiKeyAuth.from_config(cfg), pacer,
                      transport=httpx.MockTransport(handler))
    return RestClient(cfg, session, pacer=pacer), clock


def test_rest_request_paces_and_counts_every_call() -> None:
    rest, clock = _mock_rest(lambda _r: httpx.Response(200, json={"ok": True}))
    rest.get("/bot")
    rest.get("/bot")
    assert rest.pacer.stats().ops_last_hour == 2
    assert len(clock.slept) == 2, "every request must sleep its think-time first"


def test_rest_retries_429_then_succeeds() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow down"})
        return httpx.Response(200, json={"ok": True})

    rest, clock = _mock_rest(handler, backoff_jitter_s=0.0)
    assert rest.get("/bot") == {"ok": True}
    assert len(calls) == 2, "the 429 should have been retried once"
    assert 3.0 in clock.slept, "Retry-After must be honored"


def test_rest_parses_http_date_retry_after() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=120)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(when)})
    parsed = RestClient._retry_after(response)
    assert parsed is not None and 110 < parsed < 130, parsed


def test_rest_dry_run_sends_nothing_for_writes() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    rest, _ = _mock_rest(handler, dry_run=True)
    result = rest.post("/bot", json={"name": "zz-closewire-test"})
    assert result["dry_run"] is True and result["sent"] is False
    assert sent == [], "dry-run must not put a write on the wire"

    rest.get("/bot")
    assert len(sent) == 1, "reads still go through under dry-run"


def test_search_style_post_can_be_marked_a_read() -> None:
    """REGRESSION: `POST /lead/search` is a read phase 05 ships, but the verb-based
    classifier suppressed it under dry-run and returned fabricated success data."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=[{"id": "lead_1"}])

    rest, _ = _mock_rest(handler, dry_run=True)
    out = rest.post("/lead/search", json={"query": "acme"}, write=False)
    assert out == [{"id": "lead_1"}], "a search POST must return real data under dry-run"
    assert len(sent) == 1
    stats = rest.pacer.stats()
    assert stats.writes_last_hour == 0, "a search POST must not consume the write budget"
    assert stats.ops_last_hour == 1


def test_unknown_verbs_default_to_the_write_lane() -> None:
    """A safety gate must fail toward the stricter lane on a verb it does not know."""
    for verb in ("GET", "HEAD", "OPTIONS"):
        assert RestClient._is_write(verb) is False
    for verb in ("POST", "PUT", "PATCH", "DELETE", "MERGE", "PURGE", "LINK", "lock"):
        assert RestClient._is_write(verb) is True, verb


# ── 6. Config safety ──────────────────────────────────────────────────────────
def test_pacing_rejects_knobs_that_would_disable_it() -> None:
    bad: list[dict[str, object]] = [
        {"max_ops_per_hour": 0},
        {"max_writes_per_hour": 0},
        {"max_read_concurrency": 0},
        {"min_delay_s": -1.0},
        {"max_delay_s": 0.5, "min_delay_s": 1.0},
        {"write_delay_mult": 0.1},  # would make writes FASTER than reads
        {"backoff_base_s": 0.0},
        {"jitter_s": -1.0},
        {"breaker_auth_threshold": 0},
        {"max_read_concurrency": 9},  # a polite client does not open a big read pool
        {"max_read_concurrency": 64},
        {"max_retries": 33},
    ]
    for overrides in bad:
        try:
            Pacer(_config(**overrides))
        except ValueError:
            continue
        raise AssertionError(f"accepted pacing-disabling config: {overrides}")


def test_api_key_never_appears_in_config_repr() -> None:
    cfg = _config()
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg)


def test_stats_never_carry_the_key() -> None:
    pacer, _ = _pacer()
    with pacer.acquire():
        pass
    stats = pacer.stats()
    assert SECRET not in stats.render()
    assert SECRET not in str(stats.as_dict())


def test_boolean_knobs_refuse_to_fail_open_on_a_typo() -> None:
    """`CLOSEWIRE_DRY_RUN=ture` must be an error, not a silent 'writes are live'."""
    import os

    os.environ["CLOSEWIRE_DRY_RUN"] = "ture"
    try:
        from closewire_client.config import load_config

        try:
            load_config(strict=False)
        except ConfigError as exc:
            assert "CLOSEWIRE_DRY_RUN" in str(exc)
        else:
            raise AssertionError("a malformed safety flag was silently accepted")
    finally:
        del os.environ["CLOSEWIRE_DRY_RUN"]


# ── Entitlement refusals vs auth failures ─────────────────────────────────────
# Closebot answers "your plan is maxed" with HTTP 401 "upgrade required" — the same status
# as a bad key. Observed live: POST /bot with usedBots == maxBots. Counting it as an auth
# failure would trip the breaker and persist a halt over a valid key.

UPGRADE_BODY = '{"error": "upgrade required"}'
REAL_AUTH_BODY = '{"error": "invalid api key"}'


def test_entitlement_401_does_not_trip_the_auth_breaker() -> None:
    pacer, _ = _pacer(breaker_auth_threshold=2)
    for _ in range(10):  # far beyond the threshold
        decision = pacer.note_response(401, body_text=UPGRADE_BODY)
        assert not decision.should_retry, "an entitlement refusal must not be retried"
    stats = pacer.stats()
    assert stats.recent_auth_failures == 0, stats.recent_auth_failures
    assert stats.breaker_state == "closed", stats.breaker_state


def test_real_401_still_trips_the_breaker() -> None:
    """The control. Without this, the fix above could be 'never trip' and look correct."""
    pacer, _ = _pacer(breaker_auth_threshold=2)
    pacer.note_response(401, body_text=REAL_AUTH_BODY)
    try:
        pacer.note_response(401, body_text=REAL_AUTH_BODY)
    except PacingHalt as exc:
        assert "401/403" in str(exc), str(exc)
    else:
        raise AssertionError("a genuine repeated 401 must still open the breaker")


def test_401_with_no_body_is_treated_as_an_auth_failure() -> None:
    """Absent evidence, assume the credential is bad — the conservative direction."""
    pacer, _ = _pacer(breaker_auth_threshold=2)
    pacer.note_response(401, body_text=None)
    assert pacer.stats().recent_auth_failures == 1


def test_entitlement_403_is_not_retried() -> None:
    """403 is in RETRYABLE_STATUSES, so an entitlement 403 would otherwise burn retries."""
    pacer, _ = _pacer()
    assert not pacer.note_response(403, body_text=UPGRADE_BODY).should_retry
    # ...while an ordinary 403 keeps its backoff behaviour.
    assert pacer.note_response(403, body_text=None).should_retry


def test_entitlement_marker_matching_is_bounded_and_case_insensitive() -> None:
    from closewire_client.pacing import is_entitlement_refusal

    assert is_entitlement_refusal("HTTP 401: UPGRADE REQUIRED")
    assert not is_entitlement_refusal(None)
    assert not is_entitlement_refusal("")
    assert not is_entitlement_refusal('{"error": "invalid api key"}')
    # A marker buried past the scanned window must not reclassify an auth failure.
    assert not is_entitlement_refusal('{"error":"invalid key"}' + "x" * 500 + "upgrade required")


# ── Entitlement refusals: shared bookkeeping, and matching that cannot be spoofed ─────
# Three defects, all on the entitlement path added above:
#   A. it returned early, skipping the backoff reset every other non-retrying path performs;
#   B. it matched markers anywhere in the body, so caller-supplied text echoed back by the
#      API could permanently stop the auth breaker tripping on a revoked key;
#   C. it crashed on a bytes body, though `body_text` is documented as the *raw* body.

#: A 401 whose message really is an auth failure, but which echoes the submitted request —
#: `bots.create(name=...)` / `personas.create(description=...)`, model-supplied under MCP.
ECHOED_AUTH_BODY = (
    '{"error": "Unauthorized", "request": {"name": "Acme plan limit helper", '
    '"description": "upgrade required for the Acme team"}}'
)


def test_entitlement_refusal_clears_the_stale_backoff() -> None:
    """REGRESSION (A): the entitlement early return skipped the shared backoff reset.

    `stats().current_backoff_s` feeds `pacing-status` and the MCP `pacing_status` tool.
    Reporting a backoff nobody is waiting out is the same defect
    `test_retries_are_exhausted_then_surfaced` pins, on a path it does not reach.
    """
    pacer, _ = _pacer(max_retries=5, breaker_429_threshold=99)
    assert pacer.note_response(429, attempt=0).should_retry
    assert pacer.stats().current_backoff_s > 0.0, "precondition: a backoff is pending"

    pacer.note_response(403, body_text=UPGRADE_BODY)
    assert pacer.stats().current_backoff_s == 0.0, "stale backoff still reported"

    # The control: any other non-retrying status has always cleared it.
    control, _ = _pacer(max_retries=5, breaker_429_threshold=99)
    control.note_response(429, attempt=0)
    control.note_response(404)
    assert control.stats().current_backoff_s == 0.0


def test_entitlement_refusal_leaves_the_auth_counter_exactly_as_it_found_it() -> None:
    """It must neither count toward the breaker nor forgive the 401s already counted."""
    pacer, _ = _pacer(breaker_auth_threshold=3)
    pacer.note_response(401, body_text=REAL_AUTH_BODY)
    pacer.note_response(401, body_text=REAL_AUTH_BODY)
    assert pacer.stats().recent_auth_failures == 2

    pacer.note_response(401, body_text=UPGRADE_BODY)  # must not trip, must not reset
    assert pacer.stats().recent_auth_failures == 2, "an entitlement refusal rewrote history"
    assert pacer.stats().breaker_state == BreakerState.CLOSED

    try:
        pacer.note_response(401, body_text=REAL_AUTH_BODY)
    except PacingHalt:
        pass
    else:
        raise AssertionError("the third genuine 401 must still open the breaker")


def test_echoed_caller_text_cannot_suppress_the_auth_breaker() -> None:
    """REGRESSION (B): the marker scan read the whole body, so echoed input could vote.

    A transport that quotes the request back in its error body is common. With a
    caller-controlled marker anywhere in it, every genuine 401 was reclassified and a
    revoked key could never open the breaker — the one thing it exists for.
    """
    pacer, _ = _pacer(breaker_auth_threshold=3)
    seen = 0
    try:
        for _ in range(6):
            pacer.note_response(401, body_text=ECHOED_AUTH_BODY)
            seen += 1
    except PacingHalt as exc:
        assert "401/403" in str(exc), str(exc)
    else:
        raise AssertionError(
            f"{seen} genuine 401s carrying echoed caller text never tripped the breaker"
        )


def test_entitlement_match_keys_off_the_apis_own_error_field() -> None:
    from closewire_client.pacing import is_entitlement_refusal

    # Positive: the shape proven live, plus the shapes the same server could grow into.
    assert is_entitlement_refusal(UPGRADE_BODY), "the live plan refusal must still match"
    assert is_entitlement_refusal('{"error": {"message": "Upgrade your plan to add bots"}}')
    assert is_entitlement_refusal('{"errors": [{"detail": "quota exceeded"}]}')
    assert is_entitlement_refusal('{"title": "Plan limit reached"}')
    assert is_entitlement_refusal("HTTP 401: UPGRADE REQUIRED"), "a non-JSON body is a message"
    assert is_entitlement_refusal('"upgrade required"'), "a bare JSON string is a message"

    # Negative: the marker is present, but never as the API's own account of the failure.
    # Each of these would suppress the breaker again if the fix degenerated to a body scan.
    assert not is_entitlement_refusal(ECHOED_AUTH_BODY)
    assert not is_entitlement_refusal('{"error": "Unauthorized", "name": "plan limit bot"}')
    assert not is_entitlement_refusal('{"description": "we hit our plan limit"}')
    assert not is_entitlement_refusal('{"prompt": "a bot that explains quota exceeded"}')
    assert not is_entitlement_refusal('{"error": "invalid api key"}')
    # Still bounded: a long non-JSON body is a document, not a message.
    assert not is_entitlement_refusal("x" * 500 + " upgrade required")


def test_entitlement_classifier_is_total_on_any_body_type() -> None:
    """REGRESSION (C): `is_entitlement_refusal(b'...')` raised TypeError.

    `note_response(body_text=...)` is public and documented as the *raw* response body; a
    raw body is bytes as often as str. A classifier input must never fail the response.
    """
    from closewire_client.pacing import is_entitlement_refusal

    assert is_entitlement_refusal(UPGRADE_BODY.encode()) is True
    assert is_entitlement_refusal(bytearray(UPGRADE_BODY.encode())) is True
    assert is_entitlement_refusal(memoryview(UPGRADE_BODY.encode())) is True
    assert is_entitlement_refusal(REAL_AUTH_BODY.encode()) is False
    assert is_entitlement_refusal(b"\xff\xfe upgrade required") is True, "undecodable bytes"
    assert is_entitlement_refusal(b"\xff\xfe\x00") is False

    # Nothing else may raise either — every one of these means "no evidence", and no
    # evidence about the credential is the conservative answer: assume the key is bad.
    for body in (None, "", b"", {"error": "upgrade required"}, {"name": "plan limit"},
                 ["upgrade required"], 404, 4.2, object(), True):
        result = is_entitlement_refusal(body)
        assert result is True or result is False, (body, result)
    assert is_entitlement_refusal({"error": "upgrade required"}) is True, "parsed body"
    assert is_entitlement_refusal(["upgrade required"]) is False, "a bare list has no fields"


def test_note_response_handles_a_bytes_body_on_both_sides() -> None:
    pacer, _ = _pacer(breaker_auth_threshold=2)
    for _ in range(5):
        assert not pacer.note_response(403, body_text=UPGRADE_BODY.encode()).should_retry
    assert pacer.stats().recent_auth_failures == 0

    # Control: a bytes body with no marker is still a plain auth failure.
    strict, _ = _pacer(breaker_auth_threshold=2)
    strict.note_response(401, body_text=REAL_AUTH_BODY.encode())
    assert strict.stats().recent_auth_failures == 1


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} pacing tests passed.")
