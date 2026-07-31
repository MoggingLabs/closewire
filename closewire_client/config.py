"""Configuration loading for Closewire.

Reads the local ``.env`` (via python-dotenv) plus process environment and produces a
typed, frozen :class:`Config`. This is the one part of ``closewire_client`` that is fully
implemented at scaffold time — everything else (auth/session/pacing/rest/live/endpoints)
is a stub until later phases.

Contract
--------
* ``CLOSEBOT_API_KEY`` is the only **required** secret. If it is missing, loading fails
  loudly with :class:`MissingConfigError`, which names every missing required variable.
* All ``CLOSEWIRE_*`` pacing knobs and the base URLs have safe typed defaults, so a
  ``.env`` only strictly needs the key.
* Secret values are **never** rendered in full. Use :func:`redact_secret` /
  :meth:`Config.redacted_summary` for any human-facing output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

__all__ = [
    "Config",
    "ConfigError",
    "MissingConfigError",
    "load_config",
    "redact_secret",
    "DEFAULT_API_BASE",
    "DEFAULT_LIVE_BASE",
    "DEFAULT_UI_BASE",
]

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_API_BASE = "https://api.closebot.com"
DEFAULT_LIVE_BASE = "https://api.closebot.ai/message"
# The Closebot web app. Confirmed 2026-07-25 in Chrome, both unauthenticated (redirects
# to /login?redirect_url=...) and authenticated (serves the dashboard). Routes observed:
# `/bots` (labelled "Agents" in the UI), `/settings/sources`, `/settings/knowledge`
# ("Uploads"), `/conversations` ("Chats"). Used only by the build-time UI validation loop
# (see prompts/00-README.md) — the client never calls it.
DEFAULT_UI_BASE = "https://app.closebot.com"

# Pacing defaults mirror `.env.example`; tune down in `.env`, rarely up.
DEFAULT_MIN_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 4.0
DEFAULT_MAX_OPS_PER_HOUR = 300
DEFAULT_MAX_WRITES_PER_HOUR = 60
DEFAULT_DRY_RUN = False

# Writes are held to a stricter standard than reads: serial, and this much slower.
DEFAULT_WRITE_DELAY_MULT = 2.0
DEFAULT_JITTER_S = 0.35
DEFAULT_MAX_READ_CONCURRENCY = 3

# Retry/backoff on 429/403. The cap bounds the *computed* exponential delay; an explicit
# `Retry-After` from the server is obeyed as given (see DEFAULT_RETRY_AFTER_MAX_S).
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE_S = 2.0
DEFAULT_BACKOFF_CAP_S = 60.0
DEFAULT_BACKOFF_JITTER_S = 1.0
# Longest `Retry-After` we will sit through. Beyond this we stop retrying and surface the
# wait to the operator rather than silently retrying earlier than the server asked.
DEFAULT_RETRY_AFTER_MAX_S = 900.0

# Circuit breaker: consecutive failures before all traffic halts.
DEFAULT_BREAKER_AUTH_THRESHOLD = 3
DEFAULT_BREAKER_429_THRESHOLD = 5

# Where a tripped breaker is recorded so the halt survives a process restart. Without
# this, "stop all traffic" would mean "until you run the command again".
DEFAULT_STATE_DIR = ".closewire"

# Auth header form. Re-exported from `closewire_client.auth`, which owns both the list and
# the header each style produces — this module previously declared its own copy, so the two
# could drift and `Config` could accept a style `ApiKeyAuth` would then reject at runtime.
# Safe to import at module scope: `auth` imports `Config` only under TYPE_CHECKING.
from closewire_client.auth import AUTH_STYLES, DEFAULT_AUTH_STYLE

_REQUIRED = ("CLOSEBOT_API_KEY",)


# ── Errors ────────────────────────────────────────────────────────────────────
class ConfigError(RuntimeError):
    """Raised when configuration is present but invalid (e.g. a non-numeric knob)."""


class MissingConfigError(ConfigError):
    """Raised when one or more required environment variables are absent.

    Carries the list of missing variable names plus the partially-loaded
    :class:`Config` (with an empty ``api_key``) so a caller such as the CLI can still
    render a redacted, non-secret summary of what *was* found.
    """

    def __init__(self, missing: list[str], partial: "Config") -> None:
        self.missing = list(missing)
        self.partial = partial
        joined = ", ".join(self.missing)
        super().__init__(
            "Missing required environment variable(s): "
            f"{joined}. Set them in a local `.env` (copy `.env.example`) or the "
            "process environment. Never commit real secrets."
        )


# ── Config object ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    """Typed, immutable Closewire configuration.

    ``api_key`` holds the raw secret — never print it directly; use
    :meth:`redacted_summary` or :func:`redact_secret`.
    """

    # repr=False so the raw key never appears in a traceback, log line, or `%r`.
    api_key: str = field(repr=False)
    api_base: str = DEFAULT_API_BASE
    live_base: str = DEFAULT_LIVE_BASE
    ui_base: str = DEFAULT_UI_BASE
    auth_style: str = DEFAULT_AUTH_STYLE
    min_delay_s: float = DEFAULT_MIN_DELAY_S
    max_delay_s: float = DEFAULT_MAX_DELAY_S
    max_ops_per_hour: int = DEFAULT_MAX_OPS_PER_HOUR
    max_writes_per_hour: int = DEFAULT_MAX_WRITES_PER_HOUR
    dry_run: bool = DEFAULT_DRY_RUN
    write_delay_mult: float = DEFAULT_WRITE_DELAY_MULT
    jitter_s: float = DEFAULT_JITTER_S
    max_read_concurrency: int = DEFAULT_MAX_READ_CONCURRENCY
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S
    backoff_jitter_s: float = DEFAULT_BACKOFF_JITTER_S
    retry_after_max_s: float = DEFAULT_RETRY_AFTER_MAX_S
    breaker_auth_threshold: int = DEFAULT_BREAKER_AUTH_THRESHOLD
    breaker_429_threshold: int = DEFAULT_BREAKER_429_THRESHOLD
    state_dir: str = DEFAULT_STATE_DIR
    # Names of required vars that were absent at load time (empty when fully valid).
    missing: tuple[str, ...] = field(default=(), repr=False)

    @property
    def has_api_key(self) -> bool:
        """True when a non-empty API key was loaded."""
        return bool(self.api_key)

    def redacted_summary(self) -> str:
        """Return a multi-line, secret-free summary safe to print or log."""
        rows = [
            ("api_key", redact_secret(self.api_key)),
            ("api_base", self.api_base),
            ("live_base", self.live_base),
            ("ui_base", self.ui_base),
            ("auth_style", self.auth_style),
            ("min_delay_s", f"{self.min_delay_s:g}"),
            ("max_delay_s", f"{self.max_delay_s:g}"),
            ("write_delay_mult", f"{self.write_delay_mult:g}"),
            ("jitter_s", f"{self.jitter_s:g}"),
            ("max_read_concurrency", str(self.max_read_concurrency)),
            ("max_ops_per_hour", str(self.max_ops_per_hour)),
            ("max_writes_per_hour", str(self.max_writes_per_hour)),
            ("max_retries", str(self.max_retries)),
            ("backoff_base_s", f"{self.backoff_base_s:g}"),
            ("backoff_cap_s", f"{self.backoff_cap_s:g}"),
            ("breaker_auth_threshold", str(self.breaker_auth_threshold)),
            ("breaker_429_threshold", str(self.breaker_429_threshold)),
            ("dry_run", str(self.dry_run)),
        ]
        width = max(len(label) for label, _ in rows)
        return "\n".join(f"  {label.ljust(width)}  {value}" for label, value in rows)

    def scrub(self, text: str) -> str:
        """Replace any occurrence of the raw API key in ``text`` with its redacted form.

        A defensive last line before printing/logging anything that might echo the key
        (error bodies, transport messages). No-op when no key is set.
        """
        if self.api_key and self.api_key in text:
            text = text.replace(self.api_key, redact_secret(self.api_key))
        return text


# ── Redaction ─────────────────────────────────────────────────────────────────
def redact_secret(value: str) -> str:
    """Mask a secret for display.

    Shows only a short suffix hint so a user can recognize *which* key is loaded
    without the full value ever being printed. Returns ``<not set>`` for empties and
    a fully-masked marker for values too short to hint safely.
    """
    if not value:
        return "<not set>"
    if len(value) <= 6:
        return "*" * len(value)
    return "..." + value[-4:]


# ── Env parsing helpers ───────────────────────────────────────────────────────
def _get(name: str, default: str) -> str:
    """Return a stripped env value, falling back to ``default`` when unset/blank."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


_TRUE = {"1", "true", "yes", "y", "on", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disabled"}


def _resolve_state_dir(raw: str) -> str:
    """Anchor the state dir to the project, not the current working directory.

    A relative path would scope a persisted circuit-breaker halt to whatever directory
    the command happened to run from — so `cd ..` would silently escape a halt, and
    `pacing-reset` from the wrong place would report "nothing to reset" while the real
    latch survived elsewhere. Anchor to the directory holding `.env`.

    **The `.env`-less fallback used to be `Path.cwd()`, which reopened the exact hole this
    function exists to close.** A checkout with no `.env` — CI, a scratch clone, a critic's
    mutation copy — put the latch under whatever directory the command ran from, so `cd`
    escaped a persisted halt after all. It was filed non-blocking for six review rounds on
    the grounds that it cannot leak or spend; it cannot, but it weakens a stop, and a stop
    that is escapable by accident is not a stop.

    The fallback is now this package's own parent directory, which is fixed for an
    installation and identical from every cwd. `CLOSEWIRE_STATE_DIR` with an absolute path
    remains the way to put it somewhere else deliberately.
    """
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    found = find_dotenv(usecwd=True)
    anchor = Path(found).parent if found else Path(__file__).resolve().parents[1]
    return str((anchor / path).resolve())


def _get_bool(name: str, default: bool) -> bool:
    """Parse a boolean knob, raising on anything unrecognized.

    A safety flag must never fail open on a typo: ``CLOSEWIRE_DRY_RUN=ture`` has to be an
    error, not a silent "writes are live".
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(
        f"{name} must be one of {sorted(_TRUE)} or {sorted(_FALSE)}, got {raw!r}. "
        "Refusing to guess — a safety flag that fails open on a typo is worse than an error."
    )


# ── Loader ────────────────────────────────────────────────────────────────────
def load_config(*, strict: bool = True) -> Config:
    """Load and validate Closewire configuration.

    Loads the nearest ``.env`` (without overriding already-set process env vars),
    applies typed defaults, and validates required secrets.

    Args:
        strict: When True (default), raise :class:`MissingConfigError` if any required
            variable is absent — the "fail loudly" contract. When False, return a
            :class:`Config` whose ``missing`` tuple lists the gaps instead of raising
            (useful for CLIs that want to print a friendly message themselves).

    Returns:
        A frozen :class:`Config`.

    Raises:
        MissingConfigError: A required variable is absent and ``strict`` is True.
        ConfigError: A knob is present but cannot be parsed to its declared type.
    """
    load_dotenv()  # no-op if `.env` is absent; never overrides real env vars

    api_key = _get("CLOSEBOT_API_KEY", "")
    missing = [name for name in _REQUIRED if not _get(name, "")]

    auth_style = _get("CLOSEWIRE_AUTH_STYLE", DEFAULT_AUTH_STYLE).lower()
    if auth_style not in AUTH_STYLES:
        raise ConfigError(
            f"CLOSEWIRE_AUTH_STYLE must be one of {list(AUTH_STYLES)}, got {auth_style!r}"
        )

    config = Config(
        api_key=api_key,
        api_base=_get("CLOSEBOT_API_BASE", DEFAULT_API_BASE),
        live_base=_get("CLOSEBOT_LIVE_BASE", DEFAULT_LIVE_BASE),
        ui_base=_get("CLOSEBOT_UI_BASE", DEFAULT_UI_BASE),
        auth_style=auth_style,
        min_delay_s=_get_float("CLOSEWIRE_MIN_DELAY_S", DEFAULT_MIN_DELAY_S),
        max_delay_s=_get_float("CLOSEWIRE_MAX_DELAY_S", DEFAULT_MAX_DELAY_S),
        max_ops_per_hour=_get_int("CLOSEWIRE_MAX_OPS_PER_HOUR", DEFAULT_MAX_OPS_PER_HOUR),
        max_writes_per_hour=_get_int("CLOSEWIRE_MAX_WRITES_PER_HOUR", DEFAULT_MAX_WRITES_PER_HOUR),
        dry_run=_get_bool("CLOSEWIRE_DRY_RUN", DEFAULT_DRY_RUN),
        write_delay_mult=_get_float("CLOSEWIRE_WRITE_DELAY_MULT", DEFAULT_WRITE_DELAY_MULT),
        jitter_s=_get_float("CLOSEWIRE_JITTER_S", DEFAULT_JITTER_S),
        max_read_concurrency=_get_int("CLOSEWIRE_MAX_READ_CONCURRENCY", DEFAULT_MAX_READ_CONCURRENCY),
        max_retries=_get_int("CLOSEWIRE_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        backoff_base_s=_get_float("CLOSEWIRE_BACKOFF_BASE_S", DEFAULT_BACKOFF_BASE_S),
        backoff_cap_s=_get_float("CLOSEWIRE_BACKOFF_CAP_S", DEFAULT_BACKOFF_CAP_S),
        backoff_jitter_s=_get_float("CLOSEWIRE_BACKOFF_JITTER_S", DEFAULT_BACKOFF_JITTER_S),
        retry_after_max_s=_get_float("CLOSEWIRE_RETRY_AFTER_MAX_S", DEFAULT_RETRY_AFTER_MAX_S),
        breaker_auth_threshold=_get_int(
            "CLOSEWIRE_BREAKER_AUTH_THRESHOLD", DEFAULT_BREAKER_AUTH_THRESHOLD
        ),
        breaker_429_threshold=_get_int(
            "CLOSEWIRE_BREAKER_429_THRESHOLD", DEFAULT_BREAKER_429_THRESHOLD
        ),
        state_dir=_resolve_state_dir(_get("CLOSEWIRE_STATE_DIR", DEFAULT_STATE_DIR)),
        missing=tuple(missing),
    )

    if strict and missing:
        raise MissingConfigError(missing, config)
    return config
