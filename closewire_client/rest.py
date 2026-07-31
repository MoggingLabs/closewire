"""REST client base for the Closebot management API (``https://api.closebot.com``).

The single choke point for all REST HTTP. Every generated endpoint function in
:mod:`closewire_client.endpoints` calls :meth:`RestClient.request`, which:

1. opens a :meth:`~closewire_client.pacing.Pacer.acquire` slot — concurrency lane, hourly
   budget, jittered think-time, and the dry-run gate, all before a byte is sent,
2. sends the request via the authenticated :class:`~closewire_client.session.Session`,
3. reports the status to the Pacer, retrying 429/403 with exponential backoff and
   halting entirely if the circuit breaker trips,
4. raises a redacted :class:`~closewire_client.errors.ClosebotAPIError` on any non-2xx,
5. JSON-decodes 2xx bodies (or returns ``None`` for empty / text otherwise).

**There is no unpaced path.** ``pacer`` is optional only as an injection point: when it is
omitted a real :class:`~closewire_client.pacing.Pacer` is constructed from ``config``.
Tests inject one with a no-op sleeper rather than skipping pacing.
"""

from __future__ import annotations

import inspect
import json as _json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, overload

from closewire_client.auth import ApiKeyAuth
from closewire_client.errors import (
    ClosebotAPIError,
    RedactedValueError,
    SecretsNotPermittedError,
)
from closewire_client.pacing import Pacer
from closewire_client.redaction import (
    REDACTED,
    contains_redacted,
    redact_secrets,
    redact_text,
    warn_unmasked,
)
from closewire_client.session import Session

log = __import__("logging").getLogger("closewire.rest")

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

    from closewire_client.config import Config

__all__ = ["RestClient", "DRY_RUN_RESULT"]

#: Returned in place of a decoded body when dry-run suppresses a write.
DRY_RUN_RESULT: dict[str, Any] = {"dry_run": True, "sent": False}

#: Paths whose HTTP verb is mutating but whose semantics are a pure read, and which may
#: therefore be routed with ``write=False``.
#:
#: An allowlist rather than a free-form flag: ``write=False`` waives the write lane, the
#: write budget, dry-run suppression, the redaction-sentinel guard, and the params check.
#: While the client was read-only that was theoretical. From phase 07 there are real
#: mutations in the same codebase, and one misapplied override would let one of them run
#: concurrently, unbudgeted, and — worst — *unsuppressed by dry-run*.
READ_ONLY_POSTS: frozenset[str] = frozenset({"/lead/search"})


@overload
def _require_flag(name: str, value: object, *, allow_none: Literal[False] = False) -> bool: ...


@overload
def _require_flag(name: str, value: object, *, allow_none: Literal[True]) -> bool | None: ...


def _require_flag(name: str, value: object, *, allow_none: bool = False) -> bool | None:
    """Return ``value`` when it is a real boolean (or ``None`` where allowed) — else raise.

    **The defect this removes.** Every flag on :meth:`RestClient.request` waives a
    protection, and each is read two different ways. The guards ask *"did the caller
    override this?"* by identity — ``if write is False`` — while every consumer asks *"is
    this a write?"* by truthiness: the lane choice, the write budget, and the dry-run gate
    in :mod:`closewire_client.pacing` are all ``if write``. Those two readings agree only
    for ``True`` / ``False`` / ``None``. A falsy non-bool splits them, and the split is not
    academic: ``write=0`` is not ``False``, so the :data:`READ_ONLY_POSTS` guard stayed
    silent, yet it *is* falsy, so the Pacer put the call on the concurrent read lane,
    charged it no write budget, and did not suppress it under ``CLOSEWIRE_DRY_RUN`` — a
    real ``POST /bot`` on the wire, from an argument shaped like a request for *less*
    privilege. Normalising every flag at the top of the one choke point is what stops the
    two readings diverging: past this line, only ``True`` and ``False`` exist.

    **Reject, not coerce** (``bool(value)``), for three reasons:

    1. Coercion has to guess, and guesses wrong exactly where it costs something.
       ``bool(0)`` is ``False`` — "this mutation is really a read" — which is the one claim
       :data:`READ_ONLY_POSTS` exists to stop anybody making by accident.
    2. A non-bool in a flag is never a considered choice. It is a variable that carried the
       wrong thing (``write=len(changes)``, ``include_secrets=os.getenv(...)``). Coercing
       it keeps that confusion running *underneath a safety flag*; raising surfaces it at
       the line that wrote it, before a byte is sent — the cheapest place to learn of it.
    3. It is this codebase's style: structural refusal over silent correction. The
       ``include_secrets`` capability gate, the :data:`READ_ONLY_POSTS` allowlist, the
       redaction sentinel and ``PacingBypassError`` all refuse rather than fix up. A flag
       that quietly reinterpreted its input would be the one soft edge in that set — and
       the softest possible one, since this flag guards the others.

    :exc:`TypeError` because the argument is wrongly *typed*; the allowlist guard next door
    raises the builtin :exc:`ValueError` for one that is wrongly *valued*.
    """
    # Identity against the two singletons rather than ``isinstance``: ``bool`` is a
    # subclass of ``int``, so the int/bool asymmetry (``isinstance(True, int)`` is True,
    # ``isinstance(1, bool)`` is False) decides this check. The guard the whole file rests
    # on should not depend on which way round that falls.
    if value is True or value is False:
        return value
    if allow_none and value is None:
        return None
    allowed = "True, False, or None" if allow_none else "True or False"
    raise TypeError(
        f"{name}= must be exactly {allowed}, got {value!r} ({type(value).__name__}). "
        "Refused rather than coerced: each of these flags waives a protection, and a "
        f"falsy non-bool such as 0, '' or [] reads as {name}=False to everything "
        "downstream while reading as 'no override given' to the guard above it. That "
        "disagreement is how a write reaches the network on the read lane — unbudgeted, "
        "and unsuppressed by CLOSEWIRE_DRY_RUN. Pass a literal bool; if a computed value "
        "really is meant, say so with bool(x) at the call site."
    )


#: The flags :func:`_require_flag` is applied to. Checked against the real signatures at
#: import time by :func:`_assert_every_flag_is_validated`, so this cannot drift.
_VALIDATED_FLAGS: frozenset[str] = frozenset(
    {"write", "include_secrets", "static_schema", "allow_secrets"}
)


class RestClient:
    """Typed client over Closebot's REST management API.

    Args:
        config: Loaded configuration (supplies ``api_base``, key, and auth style).
        session: Optional pre-built :class:`~closewire_client.session.Session`. When
            omitted, one is constructed from ``config`` with :class:`ApiKeyAuth`.
        pacer: Injection point for the pacing layer. When omitted a real
            :class:`Pacer` is built from ``config`` — pacing is never skipped.
        allow_secrets: Whether this client may ever return unmasked third-party
            credentials. **Off by default**, and a capability rather than a per-call
            choice: ``request(include_secrets=True)`` raises unless it is set.
            Precisely: an ordinary client cannot be *argued* into unmasking — no wrapper
            forwards ``**kwargs`` into :meth:`request`, so a phase-11 tool cannot reach a
            credential however its arguments are shaped. It is not a sandbox: source that
            calls :meth:`request` directly can still pass ``static_schema=True``, which
            skips scrubbing without this capability (see that parameter).

            Must be a literal ``bool``. It is the *only* thing standing between a caller
            and an unmasked OAuth token, and it is read for truth (``not
            self._allow_secrets``), so a truthy non-bool — ``allow_secrets="false"``,
            ``"no"``, ``"0"``, all of which are true — would silently *grant* the
            capability while reading, at the call site, as a denial of it. Anything other
            than ``True``/``False`` raises :exc:`TypeError`; see :func:`_require_flag`.
    """

    def __init__(
        self,
        config: "Config",
        session: "Session | None" = None,
        *,
        pacer: "Pacer | None" = None,
        allow_secrets: bool = False,
    ) -> None:
        self._config = config
        # Normalised, not merely stored: the gate in `request` reads this for truth, so a
        # truthy non-bool would open the capability. Storing a real bool also keeps the
        # `_allow_secrets is False` identity assertions in the suite meaningful.
        self._allow_secrets = _require_flag("allow_secrets", allow_secrets)
        self.base_url = config.api_base.rstrip("/")
        if session is None:
            self._pacer = pacer if pacer is not None else Pacer(config)
            self._session = Session(config, ApiKeyAuth.from_config(config), self._pacer)
        else:
            # The transport checks *its* pacer for a slot, so the two must be the same
            # object. Silently holding two would make every request look like a bypass.
            if pacer is not None and session.pacer is not pacer:
                raise ValueError(
                    "RestClient and its Session must share one Pacer — the transport "
                    "verifies the slot against its own. Pass the same instance to both, "
                    "or omit `session` and let RestClient build it."
                )
            self._pacer = session.pacer
            self._session = session

    @property
    def pacer(self) -> Pacer:
        """The pacing layer guarding this client (exposed for `pacing-status`)."""
        return self._pacer

    @staticmethod
    def _is_write(method: str) -> bool:
        """Classify a verb for lane/budget/dry-run purposes.

        Only the known-safe read verbs are treated as reads; anything unrecognized is
        treated as a **write**. A safety gate should fail toward the stricter lane.

        Verb alone is not always right — ``POST /lead/search`` is semantically a read —
        so :meth:`request` accepts an explicit ``write=`` override for those cases.
        """
        return method.upper() not in {"GET", "HEAD", "OPTIONS"}

    @staticmethod
    def _retry_after(response: "httpx.Response") -> float | None:
        """Parse ``Retry-After`` in either the delay-seconds or the HTTP-date form."""
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
        try:  # RFC 7231 HTTP-date form
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
        return seconds if seconds > 0 else None

    # ── The single HTTP call-site ─────────────────────────────────────────────
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        write: bool | None = None,
        include_secrets: bool = False,
        static_schema: bool = False,
    ) -> Any:
        """Issue a paced, authenticated request and return the decoded body.

        Retries 429/403 with the Pacer's exponential backoff, up to
        ``CLOSEWIRE_MAX_RETRIES``. Under ``CLOSEWIRE_DRY_RUN`` a write is logged and
        counted but never sent, and :data:`DRY_RUN_RESULT` is returned.

        Args:
            method: HTTP verb.
            path: Request path (base URL omitted).
            json: JSON body, when any.
            params: Query parameters, when any.
            write: Override the verb-based read/write classification.

                Passing ``False`` is only permitted for a path in
                :data:`READ_ONLY_POSTS`; anything else raises. The override grants the read
                lane, the cheaper op budget, the 1x delay, and exemption from dry-run, the
                redaction sentinel, and the params check — five protections — so it must not
                be a general escape hatch once mutations exist. Passing ``True`` to force
                the strict lane is always allowed.

                ``True``, ``False`` and ``None`` are the only accepted values. ``write=0``
                is **not** a spelling of ``write=False``: it raises, because the guard
                above tests identity while the Pacer below tests truthiness, and only real
                booleans make those two agree. See :func:`_require_flag`.
            include_secrets: Return third-party credentials unmasked. Off by default —
                every response is scrubbed at this boundary (see
                :mod:`closewire_client.redaction`). Requires ``allow_secrets=True`` on the
                client and logs a warning.
            static_schema: **Disables scrubbing entirely** for a 2xx body. Mechanically
                identical to ``include_secrets`` — it is a second bypass, not a safe
                variant — but it is not capability-gated because it cannot be reached from
                caller data: no wrapper forwards ``**kwargs`` into this method, so setting
                it requires editing source. Use it only where "contains no account data"
                has been verified against a live account. One caller today:
                ``bots.node_descriptors``. It logs, like the other bypass.

                **It is no longer load-bearing, and the reason it once was is gone.** The
                original rationale was that the catalogue uses ``key`` for node property
                names, which scrubbing would corrupt. Phase 07's slot-aware redaction fixed
                that at the source — a ``key`` in a name/value *label* position is no longer
                treated as a credential — and the vendored catalogue now scrubs to a
                byte-identical document (17 values changed before, 0 after). The flag is
                kept rather than removed because deleting a safety-relevant branch is not a
                change to make late in a phase without review; but a scrubbing bypass that
                provably does nothing is a latent hazard, not a neutral one — if this
                endpoint ever returns account data, the bypass would pass it straight
                through. **Phase 08 should delete it**, and the reason it survives today is
                caution about timing, not a defence of its existence.

        Raises:
            TypeError: A flag was passed something that is not a literal ``bool`` (nor
                ``None``, for ``write``). Refused, never coerced — see :func:`_require_flag`.
            PacingHalt: The circuit breaker is (or just went) OPEN.
            ClosebotAPIError: The response status was not 2xx (body key-redacted).
            ClosewireTransportError: A network/transport failure occurred.
        """
        # FIRST, before any guard reads one. Each flag below is tested for identity by its
        # guard and for truth by its consumer, and those two questions only have the same
        # answer for real booleans. Normalising here is what lets every line after this
        # point assume that `write` is a bool and nothing else. See _require_flag.
        write = _require_flag("write", write, allow_none=True)
        include_secrets = _require_flag("include_secrets", include_secrets)
        static_schema = _require_flag("static_schema", static_schema)

        if include_secrets and not self._allow_secrets:
            raise SecretsNotPermittedError(
                "include_secrets=True requires a client built with "
                "RestClient(..., allow_secrets=True). Unmasking a client's third-party "
                "credentials is a capability of the client, not a per-call argument, so "
                "that a caller which was never granted it cannot request one."
            )
        if write is False and path.split("?")[0].rstrip("/") not in READ_ONLY_POSTS:
            raise ValueError(
                f"write=False is not permitted for {method} {path}. It waives the write "
                "lane, the write budget, dry-run suppression, and both sentinel guards, so "
                "it is restricted to paths whose verb is mutating but whose semantics are a "
                f"pure read: {sorted(READ_ONLY_POSTS)}. If this path really is a read, add "
                "it there deliberately."
            )
        write = self._is_write(method) if write is None else write
        if write and (contains_redacted(json) or contains_redacted(params)):
            # Read-modify-write is the natural shape for an update, and a body built from
            # a redacted read carries the mask. Sending it would overwrite a live client's
            # OAuth credential with the literal string "<redacted>".
            raise RedactedValueError(
                f"refusing to send {method} {path}: the request body contains the "
                f"redaction sentinel {REDACTED!r}. It was almost certainly built from a "
                "masked read — re-fetch the field with include_secrets=True, or strip it "
                "from the body before writing."
            )
        attempt = 0
        while True:
            with self._pacer.acquire(write=write, description=f"{method} {path}") as slot:
                if slot.dry_run_blocked:
                    # The Pacer logs the suppression, but it is handed only a description
                    # string — it never sees a body, so it cannot say *what* would have
                    # been sent. That is the whole point of a dry run: the operator is
                    # deciding whether to let this payload through. So the payload is
                    # logged here, the one place that holds it.
                    #
                    # Redacted, and via the same function the response path uses: a body
                    # can carry a credential (a source's OAuth token on an update), and a
                    # dry run must not be the one code path that prints it.
                    log.warning(
                        "DRY RUN would send %s %s\n  params: %s\n  body: %s",
                        method,
                        path,
                        self._config.scrub(_json.dumps(redact_secrets(params), default=str))
                        if params
                        else "(none)",
                        self._config.scrub(
                            _json.dumps(redact_secrets(json), indent=2, default=str)
                        )
                        if json is not None
                        else "(none)",
                    )
                    return dict(DRY_RUN_RESULT, method=method, path=path)
                response = self._session.request(method, path, json=json, params=params)

            decision = self._pacer.note_response(
                response.status_code,
                retry_after=self._retry_after(response),
                attempt=attempt,
                # The Pacer needs the body to tell "your plan is maxed" (HTTP 401
                # "upgrade required") from "your key is bad", which share a status here.
                # Guarded: a body that cannot be decoded must not mask the status.
                body_text=self._body_text(response),
            )
            if not decision.should_retry:
                return self._handle(
                    response, method, path,
                    include_secrets=include_secrets, static_schema=static_schema,
                )
            self._pacer.sleep_for_backoff(decision.backoff_s)
            attempt += 1

    @staticmethod
    def _body_text(response: "httpx.Response") -> str | None:
        """The raw body, or ``None`` if it cannot be read.

        Only fed to the Pacer's entitlement check, never logged. Failing to read a body is
        not an error worth raising here — it just means the classification falls back to
        status-code-only behaviour, which is the pre-existing conservative default.
        """
        try:
            return response.text
        except Exception:
            return None

    def _handle(
        self,
        response: "httpx.Response",
        method: str,
        path: str,
        *,
        include_secrets: bool = False,
        static_schema: bool = False,
    ) -> Any:
        # Decode ONCE, scrub ONCE, and only then branch on status. The error branch used
        # to return before the scrubber ran, so every non-2xx body — which `cli/main.py`
        # prints and `errors.py` embeds in the exception message — skipped the boundary
        # this method is supposed to *be*. Structuring it this way means there is no exit
        # from `_handle` that has not passed through the scrubber.
        decoded = self._decode(response)
        decoded = self._scrub_credentials(
            decoded,
            path,
            include_secrets=include_secrets,
            # An error body is never a static catalogue, whatever the path.
            static_schema=static_schema and response.status_code < 400,
        )

        if response.status_code >= 400:
            raise ClosebotAPIError(response.status_code, method, path, body=decoded)
        return decoded

    def _decode(self, response: "httpx.Response") -> Any:
        """Decode a body, with **our** API key scrubbed first. No status logic.

        Scrub-then-parse, not parse-then-maybe-scrub. :meth:`Config.scrub` is value-based
        — it replaces the literal key wherever it appears — so doing it on the raw text
        covers JSON and non-JSON alike. Parsing first and scrubbing only the text fallback
        left our own key raw in every JSON error body, which is the one redaction this
        phase's constraints name explicitly.
        """
        if response.status_code == 204 or not response.content:
            return None
        text = self._config.scrub(response.text)
        content_type = response.headers.get("content-type", "")
        if "json" in content_type.lower():
            try:
                return _json.loads(text)
            except ValueError:
                pass
        return text

    def _scrub_credentials(
        self, decoded: Any, path: str, *, include_secrets: bool, static_schema: bool = False
    ) -> Any:
        """Mask third-party credentials in every response, unless explicitly opted out.

        Default-deny at the one boundary every response passes through, so a new endpoint
        is safe without its author remembering anything. See
        :mod:`closewire_client.redaction` for why per-call-site redaction was abandoned,
        and why the earlier path-based exemption was removed rather than tightened.
        """
        if include_secrets:
            warn_unmasked(path)
            return decoded
        if static_schema:
            # Not the credential warning: nothing here is a credential. Logged at DEBUG so
            # the audit trail exists without claiming something false on every call.
            log.debug("redaction skipped for static schema response: %s", path)
            return decoded
        if isinstance(decoded, str):
            # JSON served with a non-JSON content type would otherwise skip redaction
            # entirely, since a bare string has no structure to walk.
            return redact_text(decoded)
        return redact_secrets(decoded)


    # ── Convenience verbs (all route through request) ─────────────────────────
    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        write: bool | None = None,
    ) -> Any:
        """POST. Pass ``write=False`` for a search-style POST that only reads."""
        return self.request("POST", path, json=json, params=params, write=write)

    def put(self, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, json=json, params=params)

    def patch(self, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None) -> Any:
        return self.request("PATCH", path, json=json, params=params)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params=params)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "RestClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _assert_every_flag_is_validated() -> None:
    """Fail the import if a ``bool`` flag was added without :func:`_require_flag`.

    Normalising the four flags that exist today says nothing about the fifth, and the
    failure mode of forgetting one is silent: it looks like a working call and behaves
    like a bypass. So the list is *verified against the real signatures* instead of
    trusted — a new bool-typed argument on the constructor or on any public entry point
    that is not in :data:`_VALIDATED_FLAGS` breaks the import, in every test at once,
    rather than quietly reopening this hole months from now.

    Detection is deliberately over-broad — any annotation *mentioning* bool, which under
    ``from __future__ import annotations`` is a plain string. A false positive costs one
    line in :data:`_VALIDATED_FLAGS`; a false negative costs a live write.

    Annotation matching alone was not enough: an **unannotated** flag
    (``def get(self, path, *, sneaky=False)``) mentions no type at all and sailed straight
    through, so the guarantee in the summary line above did not actually hold for the
    easiest way to write the mistake. A parameter defaulting to a literal ``bool`` is
    therefore flagged on its default as well as its annotation.
    """
    entry_points = (
        RestClient.__init__,
        RestClient.request,
        RestClient.get,
        RestClient.post,
        RestClient.put,
        RestClient.patch,
        RestClient.delete,
    )
    unvalidated = sorted(
        {
            name
            for func in entry_points
            for name, param in inspect.signature(func).parameters.items()
            if name not in _VALIDATED_FLAGS
            and (
                "bool" in str(param.annotation)
                # `param.default is True/False` — identity, so a stray `0` default on an
                # int parameter is not mistaken for a flag.
                or param.default is True
                or param.default is False
            )
        }
    )
    if unvalidated:
        raise RuntimeError(
            f"closewire_client.rest: bool flag(s) {unvalidated} are declared on the public "
            "surface but are not normalised through _require_flag(). Every such flag "
            "waives a protection and is read by identity in one place and by truth in "
            "another; leaving one unnormalised is how `write=0` once reached the network. "
            "Call _require_flag() on it at the top of the method and add its name to "
            "_VALIDATED_FLAGS."
        )


_assert_every_flag_is_validated()
