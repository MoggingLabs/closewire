"""Pacing / safety layer — be a good API citizen.

Every Closebot call — REST and live-message alike — passes through :class:`Pacer`.
The guarantee is structural, not conventional: :meth:`Pacer.acquire` marks the calling
thread as holding a slot, and :meth:`closewire_client.session.Session.request` refuses to
send unless that mark is present. A new endpoint function cannot skip pacing, and neither
can a future caller who builds a :class:`Session` by hand.

What a slot enforces, in order:

1. **Circuit breaker** — if OPEN, nothing goes out. Re-checked after every blocking wait,
   so a call parked inside the pacer when the breaker trips is stopped too.
2. **Concurrency** — writes are strictly serial (one in-flight); reads take one of a
   small bounded pool (``CLOSEWIRE_MAX_READ_CONCURRENCY``, default 3).
3. **Rolling budgets** — sliding one-hour windows for all ops and for writes. The slot is
   claimed *inside the same critical section that finds room*, so concurrent callers
   cannot all pass one free slot. On a hit the caller BLOCKS, logging
   ``pacing: waiting Ns for budget``.
4. **Human-timed delay** — a random duration in ``[min_delay_s, max_delay_s]`` plus jitter,
   never a fixed interval; writes are additionally multiplied by
   ``CLOSEWIRE_WRITE_DELAY_MULT`` (default 2.0) so they are strictly slower than reads.
5. **Dry-run** — when ``CLOSEWIRE_DRY_RUN`` is set, writes are logged (at WARNING, so they
   are visible with no logging config) and counted, but the slot reports
   ``dry_run_blocked`` and the caller sends nothing.

After each response the caller reports the status via :meth:`note_response`, which drives
exponential backoff (429/403) and the breaker (consecutive 401/403, or repeated 429s). An
explicit ``Retry-After`` is **honored as given**, never silently shortened; one longer than
``CLOSEWIRE_RETRY_AFTER_MAX_S`` stops the retry loop and surfaces instead of sleeping for
an unbounded time.

Clock, sleeper, and randomness are injectable so tests run instantly; the defaults are
real ``time.monotonic`` / ``time.sleep`` / ``random.Random`` — pacing is never silently
disabled. An injected sleeper that does not advance the injected clock is detected and
raises rather than spinning forever.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from closewire_client.errors import ClosewireError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.config import Config

__all__ = [
    "Pacer",
    "PacerStats",
    "PacingHalt",
    "PacingBypassError",
    "NestedSlotError",
    "RetryDecision",
    "Slot",
    "BreakerState",
    "WINDOW_S",
    "ENTITLEMENT_MARKERS",
    "ERROR_FIELDS",
    "is_entitlement_refusal",
]

log = logging.getLogger("closewire.pacing")

#: Width of both sliding budget windows, in seconds.
WINDOW_S = 3600.0

#: Statuses that earn a backoff-and-retry rather than an immediate raise.
RETRYABLE_STATUSES = frozenset({403, 429})

#: Phrases that mark a 401/403 as "your plan does not include this", not "your key is bad".
#:
#: Deliberately specific. The dangerous direction is a **false positive** — mistaking a
#: genuine auth failure for an entitlement refusal would stop the breaker from ever
#: tripping on a revoked key. A generic word like "limit" or "denied" could plausibly
#: appear in an auth error; "upgrade required" cannot. Only add a phrase here after
#: observing it live.
ENTITLEMENT_MARKERS: frozenset[str] = frozenset(
    {
        "upgrade required",   # observed live: POST /bot with usedBots == maxBots
        "upgrade your plan",
        "plan limit",
        "quota exceeded",
    }
)

#: JSON keys whose value is the API's **own** account of the failure.
#:
#: Markers are looked for *only* under these keys, never in the body at large. Several
#: write endpoints take caller-supplied free text (``bots.create(name)``,
#: ``create_with_ai(prompt)``, ``personas.create(description)`` — model-supplied once the
#: MCP server is in the loop), and an API that echoes the offending request back inside its
#: error body would otherwise let that text vote on the classification. It could then say
#: "plan limit" on behalf of a server that said "Unauthorized", and the auth breaker would
#: never trip on a revoked key. Note what is *absent*: ``name``, ``prompt``,
#: ``description``, ``request`` — the request's own field names are not error fields.
ERROR_FIELDS: frozenset[str] = frozenset(
    {
        "error",              # observed live: {"error": "upgrade required"}
        "error_description",  # OAuth-style
        "error_message",
        "errors",
        "message",
        "messages",
        "detail",
        "details",
        "title",              # RFC 7807 application/problem+json
        "reason",
    }
)

#: How much of any one message is examined, and — for a body that is not JSON — how long a
#: body may be before it stops counting as a message at all. Bounds the scan either way.
_MAX_SCAN_CHARS = 400

#: Bounds on the designated-field walk, so a pathological body cannot make it expensive.
_MAX_ERROR_DEPTH = 4
_MAX_ERROR_FIELDS = 20

#: Distinguishes "the body is not JSON" from "the body is the JSON value null".
_NOT_JSON = object()


def _body_as_text(body: Any) -> str | None:
    """Decode a raw body to text without raising, or ``None`` if it cannot be read.

    ``note_response(body_text=...)`` is documented as *the raw response body*, and a raw
    body is bytes as often as it is ``str`` (``httpx`` happens to hand us ``.text``, but
    that is the caller's choice, not a guarantee this function may assume). Undecodable
    bytes become replacement characters rather than an exception: a body we cannot fully
    read is a classification input, never a reason to fail the response.
    """
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray, memoryview)):
        try:
            return bytes(body).decode("utf-8", errors="replace")
        except (TypeError, ValueError):  # pragma: no cover - exotic buffer formats
            return None
    return None


def _error_messages(node: Any, depth: int = 0, *, designated: bool = False) -> list[str]:
    """Collect the strings a decoded body offers **as its error text**.

    Descent follows :data:`ERROR_FIELDS` and list elements only. A string is collected
    solely when it was reached through a designated key, so an echoed request payload
    sitting beside the real error — or nested inside one under its own field names —
    contributes nothing.
    """
    if depth > _MAX_ERROR_DEPTH:
        return []
    if isinstance(node, str):
        return [node] if designated else []
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.strip().lower() in ERROR_FIELDS:
                found.extend(_error_messages(value, depth + 1, designated=True))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_error_messages(item, depth + 1, designated=designated))
    return found


def _any_marker(messages: list[str]) -> bool:
    """True when any of these messages carries an entitlement marker (bounded, ci)."""
    for message in messages[:_MAX_ERROR_FIELDS]:
        head = message[:_MAX_SCAN_CHARS].lower()
        if any(marker in head for marker in ENTITLEMENT_MARKERS):
            return True
    return False


def is_entitlement_refusal(body_text: Any) -> bool:
    """True when a 401/403 body says the *plan* is the problem, not the credential.

    The match is keyed off the API's own error fields, not off the body as a whole:

    1. If the body parses as JSON, only values under :data:`ERROR_FIELDS` are examined
       (nested error objects and error arrays included). A body that parses but declares
       no error field matches nothing.
    2. If it does not parse, there are no fields to key off, so the body is treated as a
       bare message — but **only when the whole body is short enough to be one** (400
       characters). A longer non-JSON body is a document (an HTML error page, a proxy
       dump, an echoed payload), and a phrase quoted somewhere inside it is not the API
       calling this a plan problem.

    Both rules exist for the same reason: the dangerous direction is a false positive.
    Classifying a genuine auth failure as an entitlement refusal would keep the breaker
    from ever tripping on a revoked key, so anything that is not clearly the server's own
    message is read as *no evidence* — and no evidence means "assume the credential".

    Accepts ``str``, ``bytes``-like, an already-parsed body (``dict``/``list``), or
    ``None``. It is total: no input raises.
    """
    text = _body_as_text(body_text)
    if text is None:
        # Not text at all. An already-parsed body is still classifiable by field; anything
        # else is unreadable, which is no evidence, which is not an entitlement refusal.
        if isinstance(body_text, (dict, list, tuple)):
            return _any_marker(_error_messages(body_text))
        return False

    body = text.strip()
    if not body:
        return False

    parsed: Any = _NOT_JSON
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        parsed = _NOT_JSON

    # A JSON *string* body (`"upgrade required"`) has no fields either — it is a bare
    # message, so it takes the same path as unparsed text.
    if parsed is not _NOT_JSON and not isinstance(parsed, str):
        return _any_marker(_error_messages(parsed))

    message = parsed if isinstance(parsed, str) else body
    if len(message) > _MAX_SCAN_CHARS:
        return False
    return _any_marker([message])


#: Marker written into a persisted breaker whose file could not be parsed.
_CORRUPT_STATE_REASON = "breaker state file is unreadable"

#: Backstop on budget-wait iterations. Deliberately far above what lock contention can
#: produce (measured: ~7 extra rounds at the largest permitted read pool) — a low cap
#: mistakes contention for a fault and drops a call that should merely have waited. It
#: still bounds a sleeper that advances the clock asymptotically, which the elapsed-time
#: deadline alone cannot catch.
_MAX_BUDGET_ROUNDS = 256


class BreakerState:
    """Circuit-breaker states (a plain namespace — values are the log/stat strings)."""

    CLOSED = "closed"
    OPEN = "open"


class PacingHalt(ClosewireError):
    """The circuit breaker is OPEN — all traffic is stopped pending investigation.

    Raised instead of sending. Carries the reason that tripped it. Clearing this is a
    deliberate human act: run ``closewire pacing-reset`` (or call
    :meth:`Pacer.reset_breaker`) after checking the account.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"pacing: circuit breaker OPEN — {reason}. All Closebot traffic is halted. "
            "Investigate the account/key before continuing, then run "
            "`closewire pacing-reset` (or call Pacer.reset_breaker())."
        )


class PacingBypassError(ClosewireError):
    """A request was attempted without an unused pacing slot.

    A slot authorizes exactly **one** send. The transport refuses unless the calling
    thread holds one that has not been spent. Seeing this means either a caller reached
    :class:`~closewire_client.session.Session` directly instead of going through
    :class:`~closewire_client.rest.RestClient`, or a caller tried to reuse one slot for
    several requests (which would pay one think-time for N calls).
    """

    def __init__(self, method: str, url: str, *, spent: bool = False) -> None:
        why = (
            "its pacing slot was already spent — a slot authorizes exactly one send"
            if spent
            else "the calling thread holds no pacing slot"
        )
        super().__init__(
            f"pacing: refused to send {method} {url} — {why}. Every Closebot call must go "
            "through its own Pacer.acquire() (RestClient does this for you). This is the "
            "no-bypass guarantee; do not work around it."
        )


class NestedSlotError(ClosewireError):
    """A pacing slot was requested while the thread already held one.

    Nesting is never legitimate: the write lane is a plain mutex, so a write inside a
    write would deadlock silently and forever. Raising is strictly better than hanging.
    Take slots sequentially, not nested — a composite operation is N slots, not one.
    """

    def __init__(self) -> None:
        super().__init__(
            "pacing: this thread already holds a pacing slot. Nesting would deadlock the "
            "write lane. Issue calls sequentially — a composite operation costs one slot "
            "per underlying call, which is the point of the budget."
        )


@dataclass(frozen=True)
class RetryDecision:
    """What the caller should do after a response."""

    should_retry: bool
    backoff_s: float = 0.0
    reason: str = ""


@dataclass
class Slot:
    """A granted pacing slot, yielded by :meth:`Pacer.acquire`.

    Attributes:
        write: Whether this slot is on the write lane.
        dry_run_blocked: True when dry-run suppressed the send. The caller MUST NOT
            issue the request when this is set.
    """

    write: bool
    dry_run_blocked: bool = False


@dataclass(frozen=True)
class PacerStats:
    """A point-in-time, secret-free snapshot of pacer state (safe to print).

    Note: state is in-memory and per-process. A fresh ``closewire`` invocation starts with
    empty windows — the hourly ceilings bound one process, not the account globally.
    """

    ops_last_hour: int
    writes_last_hour: int
    max_ops_per_hour: int
    max_writes_per_hour: int
    total_ops: int
    total_writes: int
    dry_run_blocked: int
    breaker_state: str
    breaker_reason: str
    recent_auth_failures: int
    recent_rate_limits: int
    current_backoff_s: float
    budget_waits: int
    total_budget_wait_s: float
    dry_run: bool

    def render(self) -> str:
        """Multi-line human summary — no secrets, safe for CLI/MCP output."""
        breaker = self.breaker_state
        if self.breaker_reason:
            breaker += f" ({self.breaker_reason})"
        rows: list[tuple[str, str]] = [
            ("ops (last hour)", f"{self.ops_last_hour} / {self.max_ops_per_hour}"),
            ("writes (last hour)", f"{self.writes_last_hour} / {self.max_writes_per_hour}"),
            ("ops (total)", str(self.total_ops)),
            ("writes (total)", str(self.total_writes)),
            ("dry-run", str(self.dry_run)),
            ("dry-run blocked", str(self.dry_run_blocked)),
            ("breaker", breaker),
            ("recent 401/403", str(self.recent_auth_failures)),
            ("recent 429", str(self.recent_rate_limits)),
            ("current backoff", f"{self.current_backoff_s:g}s"),
            ("budget waits", f"{self.budget_waits} ({self.total_budget_wait_s:.0f}s total)"),
        ]
        width = max(len(label) for label, _ in rows)
        body = "\n".join(f"  {label.ljust(width)}  {value}" for label, value in rows)
        return body + "\n\n  (in-memory, per-process — counters reset each run)"

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form for `--json` CLI output and the MCP `pacing_status` tool."""
        return {
            "ops_last_hour": self.ops_last_hour,
            "writes_last_hour": self.writes_last_hour,
            "max_ops_per_hour": self.max_ops_per_hour,
            "max_writes_per_hour": self.max_writes_per_hour,
            "total_ops": self.total_ops,
            "total_writes": self.total_writes,
            "dry_run": self.dry_run,
            "dry_run_blocked": self.dry_run_blocked,
            "breaker_state": self.breaker_state,
            "breaker_reason": self.breaker_reason,
            "recent_auth_failures": self.recent_auth_failures,
            "recent_rate_limits": self.recent_rate_limits,
            "current_backoff_s": self.current_backoff_s,
            "budget_waits": self.budget_waits,
            "total_budget_wait_s": self.total_budget_wait_s,
            "scope": "in-memory, per-process",
        }


class Pacer:
    """Rate-limits and paces Closewire's calls to Closebot.

    Thread-safe: internal state is guarded by a lock, writes serialize on a mutex, and
    reads share a bounded semaphore. Sleeping never happens while holding the state lock.

    Args:
        config: Loaded configuration supplying the pacing knobs.
        monotonic: Injectable clock (defaults to :func:`time.monotonic`).
        sleeper: Injectable sleep (defaults to :func:`time.sleep`).
        rng: Injectable randomness (defaults to a fresh :class:`random.Random`).

    Raises:
        ValueError: A knob is set to a value that would disable or invert pacing.
    """

    def __init__(
        self,
        config: "Config",
        *,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        rng: "random.Random | None" = None,
    ) -> None:
        self._config = config
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep
        self._random = rng or random.Random()
        self._validate(config)

        # Locks. `_state` guards the counters below; never sleep while holding it.
        self._state = threading.RLock()
        self._write_lane = threading.Lock()
        self._read_lane = threading.Semaphore(max(1, config.max_read_concurrency))
        # Per-thread slot depth — the transport checks this to refuse unpaced sends.
        self._local = threading.local()

        # Sliding windows of op timestamps (monotonic seconds).
        self._ops: deque[float] = deque()
        self._writes: deque[float] = deque()

        # Cumulative counters + breaker state.
        self._total_ops = 0
        self._total_writes = 0
        self._dry_run_blocked = 0
        self._recent_auth = 0
        self._recent_429 = 0
        self._breaker_state = BreakerState.CLOSED
        self._breaker_reason = ""
        self._current_backoff = 0.0
        self._budget_waits = 0
        self._total_budget_wait_s = 0.0

        # A tripped breaker is persisted so the halt survives a restart — otherwise
        # "stop all traffic" would only last until the next `closewire` invocation.
        self._breaker_path = Path(config.state_dir) / "breaker.json"
        self._load_breaker()

    # ── Breaker persistence ───────────────────────────────────────────────────
    def _load_breaker(self) -> None:
        """Re-open the breaker if a previous run left it tripped.

        A *missing* file is the normal case and means no halt. A file that exists but
        cannot be parsed halts anyway: this is a safety latch, so an unreadable latch is
        treated as engaged rather than silently ignored. `closewire pacing-reset` clears it.
        """
        # A state dir that is not a directory is a misconfiguration, not "no halt" — on
        # Windows the read below would raise FileNotFoundError and look like a clean slate.
        state_dir = self._breaker_path.parent
        if state_dir.exists() and not state_dir.is_dir():
            self._open_from_disk(
                f"{_CORRUPT_STATE_REASON}: CLOSEWIRE_STATE_DIR ({state_dir}) is not a directory"
            )
            return

        try:
            raw = self._breaker_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError — a latch full of binary garbage must
            # still be recoverable via `pacing-reset`, not crash every command.
            self._open_from_disk(f"{_CORRUPT_STATE_REASON} ({type(exc).__name__}: {exc})")
            return

        try:
            saved = json.loads(raw)
            reason = saved["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("`reason` is missing or not a non-empty string")
        except (ValueError, KeyError, TypeError) as exc:
            self._open_from_disk(f"{_CORRUPT_STATE_REASON} ({exc})")
            return

        # Bound both fields: this text is echoed to stdout by `pacing-status`, and an
        # unbounded `opened_at` would dump the whole file at the operator.
        when = str(saved.get("opened_at", "an earlier run"))[:64]
        self._open_from_disk(f"{reason.strip()[:300]} (tripped at {when})")

    def _open_from_disk(self, reason: str) -> None:
        self._breaker_state = BreakerState.OPEN
        self._breaker_reason = f"{reason}; still halted from a previous run"
        log.error(
            "pacing: breaker is OPEN from a previous run — %s. Run `closewire "
            "pacing-reset` once you have investigated.",
            self._breaker_reason,
        )

    def _persist_breaker(self, reason: str) -> None:
        """Record the halt atomically, so a crash mid-write cannot truncate the latch."""
        payload = json.dumps(
            {"reason": reason, "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            indent=2,
        )
        # The temp name must be unique per writer. A shared one loses the latch entirely
        # when several trips race — and a revoked key trips every in-flight call at once,
        # which is exactly when the halt matters most.
        tmp = self._breaker_path.with_name(
            f"breaker.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self._breaker_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._breaker_path)  # atomic on POSIX and Windows
        except OSError as exc:  # non-fatal: the in-process breaker still holds
            log.warning(
                "pacing: could not persist the breaker to %s (%s) — the halt holds for "
                "this process but will NOT survive a restart",
                self._breaker_path,
                exc,
            )
            try:
                tmp.unlink()  # safe: this name is ours alone
            except OSError:
                pass

    def _clear_persisted_breaker(self) -> bool:
        """Remove the persisted latch. Returns False if it is still there afterwards."""
        try:
            self._breaker_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error(
                "pacing: could NOT clear the persisted halt at %s (%s) — this run is "
                "unhalted but the next one will start halted again",
                self._breaker_path,
                exc,
            )
            return False
        # The state dir itself can be the problem (e.g. it is a regular file), in which
        # case _load_breaker will keep latching on every start.
        parent = self._breaker_path.parent
        return not (parent.exists() and not parent.is_dir())

    @staticmethod
    def _validate(config: "Config") -> None:
        """Reject knob values that would disable, invert, or deadlock the pacing layer."""
        checks: list[tuple[bool, str]] = [
            (config.max_ops_per_hour >= 1, "CLOSEWIRE_MAX_OPS_PER_HOUR must be >= 1"),
            (config.max_writes_per_hour >= 1, "CLOSEWIRE_MAX_WRITES_PER_HOUR must be >= 1"),
            (config.max_read_concurrency >= 1, "CLOSEWIRE_MAX_READ_CONCURRENCY must be >= 1"),
            (config.max_read_concurrency <= 8,
             "CLOSEWIRE_MAX_READ_CONCURRENCY must be <= 8 — a polite client does not open "
             "a large read pool; the documented range is 2-3"),
            (config.min_delay_s >= 0, "CLOSEWIRE_MIN_DELAY_S must be >= 0"),
            (config.max_delay_s >= config.min_delay_s,
             "CLOSEWIRE_MAX_DELAY_S must be >= CLOSEWIRE_MIN_DELAY_S"),
            (config.jitter_s >= 0, "CLOSEWIRE_JITTER_S must be >= 0"),
            (config.write_delay_mult >= 1.0,
             "CLOSEWIRE_WRITE_DELAY_MULT must be >= 1.0 — writes are never faster than reads"),
            (config.max_retries >= 0, "CLOSEWIRE_MAX_RETRIES must be >= 0"),
            (config.max_retries <= 32, "CLOSEWIRE_MAX_RETRIES must be <= 32"),
            (config.backoff_base_s > 0, "CLOSEWIRE_BACKOFF_BASE_S must be > 0"),
            (config.backoff_cap_s >= config.backoff_base_s,
             "CLOSEWIRE_BACKOFF_CAP_S must be >= CLOSEWIRE_BACKOFF_BASE_S"),
            (config.backoff_jitter_s >= 0, "CLOSEWIRE_BACKOFF_JITTER_S must be >= 0"),
            (config.retry_after_max_s > 0, "CLOSEWIRE_RETRY_AFTER_MAX_S must be > 0"),
            (config.breaker_auth_threshold >= 1, "CLOSEWIRE_BREAKER_AUTH_THRESHOLD must be >= 1"),
            (config.breaker_429_threshold >= 1, "CLOSEWIRE_BREAKER_429_THRESHOLD must be >= 1"),
        ]
        bad = [message for ok, message in checks if not ok]
        if bad:
            raise ValueError(
                "pacing configuration would disable or invert the safety layer:\n  - "
                + "\n  - ".join(bad)
            )

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def dry_run(self) -> bool:
        """Whether writes should be previewed rather than sent."""
        return self._config.dry_run

    @property
    def max_retries(self) -> int:
        """How many times a retryable (429/403) response is retried before raising."""
        return self._config.max_retries

    @property
    def breaker_open(self) -> bool:
        """True when the breaker has tripped and all traffic is halted."""
        with self._state:
            return self._breaker_state == BreakerState.OPEN

    @property
    def in_slot(self) -> bool:
        """True when the calling thread is currently inside :meth:`acquire`."""
        return getattr(self._local, "depth", 0) > 0

    @property
    def sends_left(self) -> int:
        """Unspent sends authorized by the current slot (0 or 1)."""
        return getattr(self._local, "sends_left", 0)

    # ── The one entry point every call goes through ───────────────────────────
    @contextmanager
    def acquire(self, *, write: bool = False, description: str = "") -> Iterator[Slot]:
        """Reserve a paced slot for one Closebot call.

        Blocks for concurrency, budget, and think-time, then yields a :class:`Slot`.
        The caller must check :attr:`Slot.dry_run_blocked` and send nothing when set.

        Args:
            write: True for mutating calls (serial lane, stricter delay, write budget).
            description: Short non-secret label used in logs (e.g. ``"POST /bot"``).

        Yields:
            The granted :class:`Slot`. It authorizes exactly one send.

        Raises:
            NestedSlotError: This thread already holds a slot (would deadlock).
            PacingHalt: The circuit breaker is (or became) OPEN.
        """
        if self.in_slot:
            raise NestedSlotError()
        self._raise_if_open()
        lane = self._write_lane if write else self._read_lane
        lane.acquire()
        entered = False
        try:
            # The breaker may have tripped while we queued for the lane.
            self._raise_if_open()
            self._reserve(write=write)

            if write and self.dry_run:
                with self._state:
                    self._dry_run_blocked += 1
                # WARNING, not INFO: a suppressed write must be visible with no logging
                # configuration at all, or "dry-run logs the write" is not true in practice.
                log.warning("pacing: DRY RUN — suppressed write %s", description or "(write)")
                # Mark the thread (so the nesting guard sees the held lane) but authorize
                # ZERO sends, so a suppressed write is structurally unable to reach the
                # transport. Skipping the mark entirely would leave the lane held with
                # `in_slot` False — and a nested acquire would then hang on it forever,
                # which is precisely the deadlock NestedSlotError exists to prevent.
                self._enter(sends=0)
                entered = True
                yield Slot(write=write, dry_run_blocked=True)
                return

            self._delay(write=write, description=description)
            # Final gate: the breaker can trip while we sit in the budget or think-time
            # wait. Without this, a call already inside the pacer would still be sent.
            self._raise_if_open()
            self._enter(sends=1)
            entered = True
            yield Slot(write=write)
        finally:
            # Only unwind what we actually set. Calling _exit() on a path where _enter()
            # never ran would decrement someone else's mark.
            if entered:
                self._exit()
            lane.release()

    def _enter(self, *, sends: int) -> None:
        self._local.depth = getattr(self._local, "depth", 0) + 1
        self._local.sends_left = sends  # one slot, at most one send

    def _exit(self) -> None:
        self._local.depth = max(0, getattr(self._local, "depth", 0) - 1)
        self._local.sends_left = 0

    def assert_in_slot(self, method: str, url: str) -> None:
        """Consume this thread's send authorization, or raise :class:`PacingBypassError`.

        The token is one-shot: holding a slot open and issuing a second request through it
        raises rather than riding on one think-time. A dry-run slot carries no token at
        all, so a suppressed write cannot reach the wire even if the caller ignores
        :attr:`Slot.dry_run_blocked`.
        """
        if not self.in_slot:
            raise PacingBypassError(method, url)
        if self.sends_left <= 0:
            raise PacingBypassError(method, url, spent=True)
        self._local.sends_left -= 1

    # ── Response feedback: backoff + breaker ──────────────────────────────────
    def note_response(
        self,
        status_code: int,
        *,
        retry_after: float | None = None,
        attempt: int = 0,
        body_text: "str | bytes | None" = None,
    ) -> RetryDecision:
        """Record a response status and decide whether the caller should retry.

        Args:
            status_code: The HTTP status just received.
            retry_after: Parsed ``Retry-After`` value in seconds, when present.
            attempt: How many attempts have already been made (0 on the first try).
            body_text: The raw response body, when available — text, bytes, or an
                already-parsed body. Used **only** to tell an entitlement refusal from an
                auth failure — see :func:`is_entitlement_refusal`. It is never logged or
                stored.

        Returns:
            A :class:`RetryDecision`; when ``should_retry`` is set, sleep ``backoff_s``
            first (the pacer does not sleep for you here).

        Raises:
            PacingHalt: This response tripped the circuit breaker.
        """
        # Closebot answers "your plan is maxed" with **401 upgrade required** — the same
        # status as a bad key (observed live: POST /bot at usedBots == maxBots). Counting
        # that toward the auth breaker would OPEN it after
        # CLOSEWIRE_BREAKER_AUTH_THRESHOLD attempts and persist a halt to disk, forcing a
        # manual reset over a perfectly valid key. Worse, an entitlement 403 would be
        # *retried* with backoff, since 403 is in RETRYABLE_STATUSES.
        #
        # Both bugs are one root cause: the status code alone does not identify the
        # failure, so the body is consulted before either rule is applied.
        entitlement = status_code in (401, 403) and is_entitlement_refusal(body_text)
        if entitlement:
            log.warning(
                "pacing: HTTP %d is a plan/entitlement refusal, not an auth failure — "
                "not counted toward the breaker and not retried",
                status_code,
            )

        # An entitlement refusal changes *which rules apply* — it does not exempt the
        # response from being recorded. Expressing it as an early return (as this first
        # shipped) silently opted it out of every shared post-condition below, notably the
        # `_current_backoff = 0.0` reset that ends every non-retrying path: a 429 followed
        # by an entitlement 403 left `stats()` — and so `pacing-status` and the MCP
        # `pacing_status` tool — reporting a backoff nobody was waiting out. That is the
        # same stale-backoff defect `test_retries_are_exhausted_then_surfaced` already
        # pins, re-introduced on a path it does not reach. So: one exit, shared bookkeeping.
        trip_reason = ""
        with self._state:
            # The single behavioural difference. An entitlement refusal is not evidence
            # about the credential, so it must not feed the auth breaker — the rest of
            # this block is inert for a 401/403 anyway (the 429 counter is untouched, and
            # the success reset cannot fire because 401 and 403 are both >= 400), which is
            # why the early return could look harmless.
            if status_code in (401, 403) and not entitlement:
                self._recent_auth += 1
            if status_code == 429:
                self._recent_429 += 1
            if 200 <= status_code < 400:
                self._recent_auth = 0
                self._recent_429 = 0
                self._current_backoff = 0.0

            if self._recent_auth >= self._config.breaker_auth_threshold:
                trip_reason = (
                    f"{self._recent_auth} recent 401/403 responses "
                    "(bad or revoked key, or blocked account)"
                )
            elif self._recent_429 >= self._config.breaker_429_threshold:
                trip_reason = (
                    f"{self._recent_429} recent 429 responses "
                    "(sustained rate limiting — back off hard)"
                )

            if trip_reason:
                self._breaker_state = BreakerState.OPEN
                self._breaker_reason = trip_reason

        if trip_reason:
            log.error("pacing: breaker OPEN — %s", trip_reason)
            self._persist_breaker(trip_reason)
            raise PacingHalt(trip_reason)

        # 403 is retryable, but an entitlement 403 is not: retrying cannot change the plan.
        # It leaves here rather than earlier so it shares the reset above it.
        if entitlement or status_code not in RETRYABLE_STATUSES:
            with self._state:
                self._current_backoff = 0.0
            return RetryDecision(should_retry=False)

        if attempt >= self.max_retries:
            with self._state:
                self._current_backoff = 0.0
            return RetryDecision(
                should_retry=False,
                reason=f"exhausted {self.max_retries} retries on HTTP {status_code}",
            )

        # An explicit Retry-After is obeyed as given. If the server asks for longer than
        # we are willing to sit on, surface it rather than silently retrying early.
        if retry_after is not None and retry_after > self._config.retry_after_max_s:
            with self._state:
                self._current_backoff = 0.0
            log.error(
                "pacing: server asked for Retry-After %.0fs, over the %.0fs limit — not retrying",
                retry_after,
                self._config.retry_after_max_s,
            )
            return RetryDecision(
                should_retry=False,
                reason=(
                    f"server asked for Retry-After {retry_after:.0f}s, above "
                    f"CLOSEWIRE_RETRY_AFTER_MAX_S ({self._config.retry_after_max_s:.0f}s)"
                ),
            )

        backoff = self._backoff_for(attempt, retry_after)
        with self._state:
            self._current_backoff = backoff
        log.warning(
            "pacing: HTTP %s — backing off %.1fs (attempt %s/%s)",
            status_code,
            backoff,
            attempt + 1,
            self.max_retries,
        )
        return RetryDecision(should_retry=True, backoff_s=backoff, reason=f"HTTP {status_code}")

    def sleep_for_backoff(self, seconds: float) -> None:
        """Sleep a computed backoff through the injected sleeper (so tests stay fast)."""
        if seconds > 0:
            self._sleep(seconds)

    def _backoff_for(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter, capped. An explicit ``Retry-After`` is obeyed."""
        if retry_after is not None and retry_after > 0:
            return float(retry_after)
        # Cap the exponent before the multiply so a large max_retries cannot overflow.
        exponent = min(attempt, 32)
        base = min(self._config.backoff_base_s * (2 ** exponent), self._config.backoff_cap_s)
        jitter = self._random.uniform(0.0, self._config.backoff_jitter_s)
        return min(base + jitter, self._config.backoff_cap_s)

    # ── Breaker control ───────────────────────────────────────────────────────
    def reset_breaker(self) -> bool:
        """Manually close the breaker after investigating. Clears the failure counters.

        Returns:
            True when the halt is fully cleared. **False** when the in-process breaker was
            closed but the persisted latch could not be removed — the next run would come
            back halted, so the caller must not report success.
        """
        with self._state:
            was = self._breaker_state
            self._breaker_state = BreakerState.CLOSED
            self._breaker_reason = ""
            self._recent_auth = 0
            self._recent_429 = 0
            self._current_backoff = 0.0
        cleared = self._clear_persisted_breaker()
        if was == BreakerState.OPEN and cleared:
            log.warning("pacing: breaker manually reset — traffic resumed")
        return cleared

    def decline_backoff(self) -> None:
        """Tell the Pacer a caller is **not** going to wait out the backoff it just offered.

        :meth:`note_response` sets a backoff whenever it classifies a status as retryable,
        and every non-retrying path *inside* this module clears it again — that reset is
        what stops :meth:`stats` reporting a delay nobody is observing, and phase 07 already
        fixed one instance of exactly that.

        Phase 09 created a path this module cannot see: the live-message surface narrows the
        retry vocabulary to ``429`` and declines the Pacer's offer on ``403``. Without a way
        to say so, ``current_backoff_s`` stayed set for the life of the process — invisible
        in a short CLI run, and reported for hours by the long-lived MCP server of phase 11.

        Exposed here rather than letting the caller assign ``pacer._current_backoff``,
        because the state belongs to this object and a private write from another module is
        a coupling that breaks silently the next time this one is refactored.
        """
        with self._state:
            self._current_backoff = 0.0

    def _raise_if_open(self) -> None:
        with self._state:
            if self._breaker_state != BreakerState.OPEN:
                return
            reason = self._breaker_reason
        raise PacingHalt(reason)

    # ── Budget accounting ─────────────────────────────────────────────────────
    def _reserve(self, *, write: bool) -> None:
        """Claim a slot in the sliding windows, blocking until there is room.

        The claim happens inside the *same* critical section that finds room, so
        concurrent callers cannot all pass on one free slot.
        """
        counted = False
        rounds = 0
        # Two independent backstops, because neither catches the other's case: the elapsed
        # deadline catches a ceiling set below the offered load, the round cap catches a
        # sleeper whose clock converges on the target without reaching it.
        deadline = self._monotonic() + (WINDOW_S * 2) + 60.0
        while True:
            with self._state:
                now = self._monotonic()
                self._prune(now)
                waits: list[float] = []
                if len(self._ops) >= self._config.max_ops_per_hour:
                    waits.append(self._ops[0] + WINDOW_S - now)
                if write and len(self._writes) >= self._config.max_writes_per_hour:
                    waits.append(self._writes[0] + WINDOW_S - now)

                if not waits:
                    self._ops.append(now)
                    self._total_ops += 1
                    if write:
                        self._writes.append(now)
                        self._total_writes += 1
                    return

                wait = max(waits)
                if not counted:
                    self._budget_waits += 1
                    counted = True

            rounds += 1
            if self._monotonic() > deadline or rounds > _MAX_BUDGET_ROUNDS:
                raise ClosewireError(
                    "pacing: could not obtain budget after "
                    f"{rounds} attempts / {WINDOW_S * 2:.0f}s. A sliding-hour ceiling "
                    "should free within one window, so this means either the ceiling is "
                    "set far below the offered load (check CLOSEWIRE_MAX_OPS_PER_HOUR "
                    "and CLOSEWIRE_MAX_WRITES_PER_HOUR) or an injected sleeper is not "
                    "advancing its clock properly."
                )

            log.warning("pacing: waiting %.1fs for budget", wait)
            before = self._monotonic()
            self._sleep(wait)
            advanced = self._monotonic() - before
            with self._state:
                self._total_budget_wait_s += max(0.0, advanced)

            if wait > 0 and advanced < wait * 0.5:
                raise ClosewireError(
                    f"pacing: asked to wait {wait:.1f}s for budget but the clock advanced "
                    f"only {advanced:.3f}s — the injected sleeper and clock disagree, so "
                    "this wait can never finish. Inject a clock the sleeper advances (see "
                    "FakeClock in tests/test_pacing.py), or use the real defaults."
                )

    def _prune(self, now: float) -> None:
        """Drop timestamps that have fallen out of the sliding window. Call under lock."""
        cutoff = now - WINDOW_S
        while self._ops and self._ops[0] <= cutoff:
            self._ops.popleft()
        while self._writes and self._writes[0] <= cutoff:
            self._writes.popleft()

    # ── Think-time ────────────────────────────────────────────────────────────
    def _delay(self, *, write: bool, description: str = "") -> None:
        """Sleep a randomized think-time; writes are strictly slower than reads."""
        seconds = self._random.uniform(self._config.min_delay_s, self._config.max_delay_s)
        if write:
            seconds *= self._config.write_delay_mult
        seconds += self._random.uniform(0.0, self._config.jitter_s)
        log.debug("pacing: sleeping %.2fs before %s", seconds, description or "call")
        self._sleep(seconds)

    # ── Observability ─────────────────────────────────────────────────────────
    def stats(self) -> PacerStats:
        """Snapshot the pacer's state for the CLI / MCP `pacing_status` surface."""
        with self._state:
            self._prune(self._monotonic())
            return PacerStats(
                ops_last_hour=len(self._ops),
                writes_last_hour=len(self._writes),
                max_ops_per_hour=self._config.max_ops_per_hour,
                max_writes_per_hour=self._config.max_writes_per_hour,
                total_ops=self._total_ops,
                total_writes=self._total_writes,
                dry_run_blocked=self._dry_run_blocked,
                breaker_state=self._breaker_state,
                breaker_reason=self._breaker_reason,
                recent_auth_failures=self._recent_auth,
                recent_rate_limits=self._recent_429,
                current_backoff_s=self._current_backoff,
                budget_waits=self._budget_waits,
                total_budget_wait_s=self._total_budget_wait_s,
                dry_run=self.dry_run,
            )
