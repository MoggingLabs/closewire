"""Live Message client — the Closebot **runtime** endpoint.

A thin client over the single ``POST https://api.closebot.ai/message``. This is a
**different surface** from the REST management API: a different host, a different auth
convention, and a status vocabulary that has little to do with HTTP's.

**It is a write, and it spends money.** Every accepted send consumes account credits, so it
takes the Pacer's serial write lane, is charged to the write budget, and is suppressed by
``CLOSEWIRE_DRY_RUN`` exactly like a REST mutation.

**The status codes are the point of this module.** Closebot answers with codes that are not
HTTP failures in the usual sense — ``420`` means "out of credits", ``450`` means "this bot
hit its per-contact cap", ``201`` means "resend, something arrived mid-processing".
Collapsing those into one error would leave a caller unable to tell *top up the wallet*
from *fix your request* from *just retry*. Each maps to its own type; see :data:`STATUS_MAP`.

**Auth, and why the header is the default.** The key may travel as ``X-CB-KEY`` *or* as an
``api_key`` field in the JSON body. Both work; the body form is riskier, because the body
is the thing a debug log prints. So the header is the default, and the payload logger routes
through :func:`~closewire_client.redaction.redact_secrets` rather than re-deriving the rule —
``api_key`` is already in ``SECRET_FIELDS``, and one masking vocabulary cannot drift from
itself.

**Two maskings, and both are load-bearing here.** ``redact_secrets`` masks by *name*, which
is the only rule that can find a third-party credential; :meth:`Config.scrub` replaces the
literal configured key wherever it appears, which is the only rule that can find our own key
in a **value**. This is the one surface in the client that puts the key in the request
*body*, and the live ``410`` body discusses it in prose — ``"invalid credentials (if using
api_key)"`` — so a body that echoes it is not hypothetical. Every body this module hands back
or raises therefore goes through :meth:`LiveMessageClient._scrub`: scrub the raw text first,
parse second, mask by name third, branch on status last. That is
:meth:`~closewire_client.rest.RestClient._handle`'s order, deliberately, rather than a second
pipeline that can drift from it.

**Retries.** The Pacer owns backoff for the whole client, but its retry vocabulary is the
REST host's. Only :data:`RETRY_STATUSES` is re-sent here, and never a code the runtime
declares — a re-send is a second *charged* message, so the rule has to be about which
statuses can be known to have cost nothing. See :data:`RETRY_STATUSES` and :func:`_may_retry`.
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from closewire_client.auth import AUTH_STYLES, DEFAULT_AUTH_STYLE, ApiKeyAuth
from closewire_client.errors import ClosewireError
from closewire_client.redaction import redact_secrets, redact_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

    from closewire_client.config import Config
    from closewire_client.pacing import Pacer
    from closewire_client.session import Session

log = logging.getLogger("closewire.live")

__all__ = [
    "LiveMessageClient",
    "scrub_body",
    "LiveReply",
    "LiveMessageError",
    "RerunRequested",
    "NoAccount",
    "NoCredits",
    "MissingContactId",
    "MissingMessage",
    "BotLimitReached",
    "AccountLimitReached",
    "STATUS_MAP",
    "STATUS_HELP",
    "RETRY_STATUSES",
    "CHANNELS",
    "MAX_EXTRA_PROMPT",
    "PAYLOAD_FIELDS",
    "message_endpoint",
]

#: ``channel`` accepts exactly these. An unknown value is refused locally rather than
#: spending a paced write to learn the API's opinion.
CHANNELS: frozenset[str] = frozenset({"Live_Chat", "SMS", "FB", "Email", "IG", "WhatsApp"})

#: ``extra_prompt`` is documented as one-shot and capped, and an overrun is knowable
#: without spending a credit.
MAX_EXTRA_PROMPT = 500

#: Every field ``MessagePayload`` declares in ``schema/live-message.json``. Custom injected
#: variables are ``@``-prefixed and are *not* in this set — see ``variables`` on
#: :meth:`LiveMessageClient.send_message` for why they are allowed through anyway.
PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "id", "api_key", "@rep_name", "@lead_name", "message", "goals_finished",
        "limit", "extra_prompt", "bot", "model", "followup", "is_update",
        "character_limit", "delay", "channel",
    }
)


# ── Errors ────────────────────────────────────────────────────────────────────
class LiveMessageError(ClosewireError):
    """Base for every runtime-endpoint failure. Carries the code and the scrubbed body."""

    status_code: int | None = None

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        if status_code is not None:
            self.status_code = status_code
        self.body = body
        super().__init__(message)


class RerunRequested(LiveMessageError):
    """``201`` — another message arrived mid-processing; send it again.

    An exception rather than a return value, so a caller that ignores it cannot mistake
    "retry me" for "here is your reply".
    """

    status_code = 201


class NoAccount(LiveMessageError):
    """``410`` — the resolved account is missing."""

    status_code = 410


class NoCredits(LiveMessageError):
    """``420`` — insufficient credits. **Top up the wallet.**"""

    status_code = 420


class MissingContactId(LiveMessageError):
    """``430`` — ``id`` was absent. A request bug, not an account problem."""

    status_code = 430


class MissingMessage(LiveMessageError):
    """``440`` — ``message`` was absent."""

    status_code = 440


class BotLimitReached(LiveMessageError):
    """``450`` — this bot has answered this contact as often as it is allowed."""

    status_code = 450


class AccountLimitReached(LiveMessageError):
    """``460`` — an account-level limit was hit; the threshold is undocumented."""

    status_code = 460


#: Status code → exception. ``200`` is the only success.
STATUS_MAP: dict[int, type[LiveMessageError]] = {
    201: RerunRequested,
    410: NoAccount,
    420: NoCredits,
    430: MissingContactId,
    440: MissingMessage,
    450: BotLimitReached,
    460: AccountLimitReached,
}

#: What each code means, in the words a CLI should print.
STATUS_HELP: dict[int, str] = {
    201: "another message arrived while this one was processing — send it again",
    410: "no such account for the resolved key/contact",
    420: "OUT OF CREDITS — top up the wallet before sending again",
    430: "the request carried no contact `id`",
    440: "the request carried no `message`",
    450: "BOT LIMIT — this bot has answered this contact as often as it is allowed",
    460: "ACCOUNT LIMIT reached (threshold is undocumented)",
}

#: The statuses this surface re-sends on, and the only ones.
#:
#: The Pacer classifies **both** 403 and 429 as retryable
#: (:data:`~closewire_client.pacing.RETRYABLE_STATUSES`), and that is right for the REST
#: host, where 403 is how the edge expresses rate limiting. It does not transfer here:
#:
#: * ``429`` means "you are going too fast" on any host, and a throttled request is rejected
#:   at the front door — it never reached a bot, so nothing was charged and re-sending
#:   cannot double-charge. Retried, with the Pacer's backoff and its ``Retry-After`` rule.
#: * ``403`` on ``api.closebot.ai`` is API Gateway's ``Missing Authentication Token`` — the
#:   wrong-path artefact :func:`message_endpoint` exists to prevent, and the one this
#:   module's history records being misread as a credentials failure. Re-sending a
#:   misrouted request cannot help, and each attempt feeds the Pacer's auth breaker, so
#:   retrying would turn one mistyped ``live_base`` into three 403s, a persisted halt, and a
#:   manual ``closewire pacing-reset`` — on **all** Closebot traffic, REST included. Left
#:   out deliberately; this is a narrowing of the Pacer's vocabulary, not an oversight.
RETRY_STATUSES: frozenset[int] = frozenset({429})


def _may_retry(status: int) -> bool:
    """Whether re-sending after ``status`` is both safe and useful **on this surface**.

    Two independent conditions, because they answer different questions:

    * ``status in`` :data:`RETRY_STATUSES` — is a re-send *useful*? See that constant for
      why 403 is excluded here but not in ``rest.py``.
    * ``status not in`` :data:`STATUS_MAP` — is a re-send *safe*? Every key of that table is
      the runtime's semantic answer about the request or the account, and a re-send is a
      second message against the same contact, i.e. a second credit. ``201`` in particular
      *is* a "send it again", but the decision belongs to the caller who knows whether the
      conversation still wants it — which is why it is raised rather than looped on here.

    The two sets are disjoint today, so the second condition never fires. It is kept because
    it is what makes "a code the runtime declares is never auto-resent" a property of this
    function, rather than an accident of two tables in two modules not happening to overlap.
    """
    return status in RETRY_STATUSES and status not in STATUS_MAP


@dataclass(frozen=True)
class LiveReply:
    """A successful send.

    The runtime's *response* shape is not declared in the vendored spec — only the request
    is — so ``raw`` is kept whole rather than parsed into fields that may not exist.
    :attr:`text` searches the plausible keys and returns ``None`` rather than inventing one.

    ``status_code == 0`` marks a dry-run suppression, so a caller can tell a suppressed send
    from a real one without re-reading configuration.
    """

    status_code: int
    raw: Any

    @property
    def sent(self) -> bool:
        """False when dry-run suppressed this send."""
        return self.status_code != 0

    @property
    def text(self) -> str | None:
        """The reply text, when the payload carries something recognisable as one."""
        if isinstance(self.raw, str):
            return self.raw or None
        if not isinstance(self.raw, dict):
            return None
        for key in ("message", "reply", "response", "text", "content", "body"):
            value = self.raw.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def goals_finished(self) -> list[str]:
        """Goals the runtime reports finished, when it reports any."""
        if isinstance(self.raw, dict):
            for key in ("goals_finished", "goalsFinished", "finished_goals"):
                value = self.raw.get(key)
                if isinstance(value, list):
                    return [str(v) for v in value]
        return []


def loggable(payload: dict[str, Any]) -> str:
    """A payload rendered for logging with the key masked.

    ``api_key`` is a declared *body* field here, so "the key only ever lives in a header"
    does not hold on this surface and a naive payload log would print it.
    """
    return _json.dumps(redact_secrets(payload), indent=2, default=str)


def scrub_body(config: "Config", text: str) -> Any:
    """Decode one response body with **both** maskings applied, in the order that works.

    Public and module-level so there is exactly one masking pipeline. Each step catches
    something the other structurally cannot:

    1. :meth:`Config.scrub` is *value*-based — it replaces the literal configured key
       wherever it appears — so it is the only rule that finds **our** key in a value
       (``{"error": "invalid credentials (if using api_key): <key>"}``, the shape of the
       live 410) or in a body that is a bare JSON string. Doing it on the raw text covers
       JSON and non-JSON alike.
    2. :func:`redact_secrets` / :func:`redact_text` are *name*-based, and are the only rules
       that find a **third-party** credential, whose value we do not know.

    Parsing happens regardless of ``Content-Type``: the runtime endpoint declares no response
    schema at all (spec deviation 24), so its content type is not something to decide on.

    A `str` after parsing is a body with no structure to walk — either not JSON, or the JSON
    value of a bare string. `redact_text` handles both, and is what keeps a credential-bearing
    document that arrived quoted from being the one path that skips masking.
    """
    text = config.scrub(text)
    try:
        decoded: Any = _json.loads(text)
    except ValueError:
        decoded = text
    return redact_text(decoded) if isinstance(decoded, str) else redact_secrets(decoded)


def message_endpoint(live_base: str) -> str:
    """The absolute URL of ``POST /message``, from whatever ``live_base`` holds.

    ``live_base`` is ambiguously named and ambiguously defaulted, so this resolves both
    readings rather than picking one and breaking the other:

    * ``schema/live-message.json`` declares the **server** as ``https://api.closebot.ai``
      with the **path** ``/message`` — so a spec-faithful value needs ``/message`` appended;
    * ``config.DEFAULT_LIVE_BASE`` is ``https://api.closebot.ai/message`` — the full
      endpoint, already carrying the path.

    An earlier revision appended unconditionally and posted to ``/message/message``, which
    API Gateway answers with ``403 Missing Authentication Token`` — a message that reads
    like an auth failure and sent me looking at the key for the wrong reason. Appending only
    when the path is absent makes both spellings work and neither look like a credentials
    problem.
    """
    trimmed = live_base.rstrip("/")
    return trimmed if trimmed.endswith("/message") else f"{trimmed}/message"


class LiveMessageClient:
    """Paced client for ``POST /message`` on the runtime host.

    Args:
        config: Supplies ``live_base``, the API key, and the dry-run flag.
        session: **Not supported — passing one raises.** See :meth:`__init__`.
        pacer: The pacing layer. **Pass ``RestClient.pacer``** so both surfaces draw on one
            set of hourly budgets and one serial write lane; omitting it builds a *separate*
            Pacer with independent budgets, which is almost never what you want — two
            unrelated budgets means twice the traffic the operator thought they configured.
        transport: Optional ``httpx`` transport override (tests inject a MockTransport).
        auth_in_body: Send the key as the ``api_key`` body field instead of the header.
            Off by default — see the module docstring.
        auth_style: Which header form to use, one of
            :data:`~closewire_client.auth.AUTH_STYLES`. Defaults to ``x-cb-key``, which is
            what the runtime host documents. Deliberately **not** inherited from
            ``config.auth_style``: that setting describes ``api.closebot.com``, and the
            refusal below explains why this surface does not adopt the REST host's
            convention by default. It is a parameter rather than a constant because
            ``RESEARCH.md`` records community deployments using ``Authorization: Bearer``
            against ``api.closebot.ai`` — the runtime host — so the form is a real variable,
            and an operator hitting such a deployment must be able to vary it without
            editing this module.
    """

    def __init__(
        self,
        config: "Config",
        session: "Session | None" = None,
        *,
        pacer: "Pacer | None" = None,
        transport: "httpx.BaseTransport | None" = None,
        auth_in_body: bool = False,
        auth_style: str | None = None,
    ) -> None:
        """Build a paced runtime client.

        ``session`` is refused rather than honoured, and rather than ignored. It used to be
        accepted, stored, and never used — sends went through a private ``httpx.Client``
        built from ``transport=`` instead — so a caller who built a
        :class:`~closewire_client.session.Session` around an ``httpx.MockTransport`` got a
        **real**, credit-spending ``POST`` and no indication that their transport had been
        dropped. Of the three possible endings, silence was the only unacceptable one; of the
        remaining two, refusal is right here:

        * :class:`Session` is not a neutral transport for this surface. It is bound to
          ``config.api_base`` — the *REST* host — and merges
          :class:`~closewire_client.auth.ApiKeyAuth`'s headers into every request. Routing a
          runtime send through it would put the key in a header even under
          ``auth_in_body=True``, silently falsifying this module's one auth guarantee, and
          would send it in whatever ``CLOSEWIRE_AUTH_STYLE`` spells for ``api.closebot.com``
          to a host that documents ``X-CB-KEY``. "Use it" is not a drop-in; it changes what
          goes on the wire.
        * Borrowing only its transport is not available: ``httpx.Client`` exposes none, so it
          would mean reaching into ``session._client._transport`` — a private coupling that
          fails on an httpx upgrade in the direction of a real POST.

        So it refuses, in the style the rest of this codebase already uses for arguments that
        waive a protection (``_require_flag``, ``READ_ONLY_POSTS``, ``PacingBypassError``):
        structural refusal over silent correction. Both things a caller actually wants from
        it remain available and are named in the error — ``transport=`` for the transport,
        ``pacer=session.pacer`` for one shared set of budgets and one write lane.

        Raises:
            TypeError: ``session`` was passed.
        """
        import httpx

        from closewire_client.pacing import Pacer as _Pacer

        if session is not None:
            raise TypeError(
                "LiveMessageClient(session=...) is not supported. It was previously "
                "accepted and silently ignored: sends went through this client's own "
                "httpx.Client, so a Session built with an httpx.MockTransport was dropped "
                "and the send reached the real runtime endpoint and spent a credit. It is "
                "refused rather than honoured because Session is bound to config.api_base "
                "(the REST host) and injects the REST auth header on every request, which "
                "would put the key in a header even under auth_in_body=True. Pass what you "
                "actually want instead: transport=<your transport> to inject a transport "
                "(e.g. httpx.MockTransport in tests), and pacer=session.pacer to share one "
                "set of hourly budgets and one serial write lane."
            )

        self._config = config
        self._pacer = pacer if pacer is not None else _Pacer(config)
        self.endpoint = message_endpoint(config.live_base)
        self._auth_in_body = auth_in_body
        # `None` means "the caller did not choose", which is a different fact from "the
        # caller chose x-cb-key" — and only the first can be a *silent* divergence from what
        # the environment says. That distinction is the whole fix: the refusal to inherit
        # `config.auth_style` is correct (two hosts, two conventions, and inheriting would
        # change what goes on the wire to the credit-spending endpoint), but it was silent.
        # An operator who set CLOSEWIRE_AUTH_STYLE=authorization-bearer to reach a Bearer
        # deployment got x-cb-key here, with no warning, no log line and no error — a knob
        # that appears to turn. The refusal stays; the silence goes.
        chosen = auth_style is not None
        resolved = auth_style if chosen else DEFAULT_AUTH_STYLE
        if resolved not in AUTH_STYLES:
            raise ValueError(
                f"LiveMessageClient(auth_style={auth_style!r}): expected one of {AUTH_STYLES}"
            )
        self._auth_style = resolved
        env_style = getattr(config, "auth_style", DEFAULT_AUTH_STYLE)
        if not chosen and env_style != resolved:
            log.warning(
                "CLOSEWIRE_AUTH_STYLE=%r applies to the REST host (%s) ONLY. This runtime "
                "client posts to %s using %r, and deliberately does not inherit it — the two "
                "hosts document different conventions (see deviation 29). To vary it here, "
                "pass LiveMessageClient(auth_style=...).",
                env_style, config.api_base, message_endpoint(config.live_base), resolved,
            )
        # No `base_url`: the endpoint is a full URL, because `live_base` may or may not
        # already carry the path — see `message_endpoint`. Posting an absolute URL keeps
        # the resolution in one place instead of splitting it between here and the call.
        self._client = httpx.Client(timeout=60.0, transport=transport)

    @property
    def pacer(self) -> "Pacer":
        """The pacing layer guarding this client."""
        return self._pacer

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LiveMessageClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── The one operation ─────────────────────────────────────────────────────
    def send_message(
        self,
        *,
        id: str,
        message: str | None = None,
        bot: Any = None,
        channel: str = "Live_Chat",
        extra_prompt: str | None = None,
        character_limit: int | None = None,
        followup: bool | None = None,
        is_update: bool = False,
        model: str | None = None,
        goals_finished: list[str] | None = None,
        delay: int | None = None,
        limit: int | None = None,
        variables: dict[str, Any] | None = None,
    ) -> LiveReply:
        """Deliver a message into a bot conversation and return the reply.

        **Spends a credit.** Serial write lane, write budget, suppressed by
        ``CLOSEWIRE_DRY_RUN`` — under dry run the request is logged, at the URL it would
        really be sent to and with the key masked, and a :class:`LiveReply` with
        ``status_code=0`` is returned.

        A :data:`RETRY_STATUSES` response is re-sent with the Pacer's backoff, up to
        ``CLOSEWIRE_MAX_RETRIES``, each attempt taking its own slot. Nothing the runtime
        declares in :data:`STATUS_MAP` is ever re-sent — see :func:`_may_retry`.

        Args:
            id: The contact id. A test-session lead id is the safe choice for QA.
            message: The inbound text. **Not** required locally: ``followup`` and
                ``is_update`` sends legitimately omit it, and guessing otherwise would
                block a valid call. The API answers ``440`` when it is genuinely needed.
            bot: Target bot. The spec types this ``integer`` while every bot id in the REST
                API is a ``bot_…`` string, so it is passed through untyped rather than
                coerced to one shape or the other — see docs/validation/09-runtime.md.
            channel: One of :data:`CHANNELS`.
            extra_prompt: One-shot instruction, ≤ :data:`MAX_EXTRA_PROMPT` chars.
            variables: Injected variables such as ``{"@lead_name": "Ada"}``. Keys must be
                ``@``-prefixed. They are **not** validated against
                :data:`PAYLOAD_FIELDS` because custom variables are user-defined per bot —
                the declared ``@lead_name``/``@rep_name`` are only the two the spec happens
                to name, and refusing the rest would break the documented feature.

        Raises:
            ValueError: A locally-knowable problem — blank id, bad channel, over-long
                extra prompt, non-``@`` variable. Raised **before** the paced slot is
                taken, so a malformed call costs neither wall-clock nor budget.
            RerunRequested, NoAccount, NoCredits, MissingContactId, MissingMessage,
            BotLimitReached, AccountLimitReached: the corresponding status code.
            LiveMessageError: any other non-200.
        """
        if not str(id or "").strip():
            raise ValueError("send_message(): `id` is required and cannot be blank")
        if channel is not None and channel not in CHANNELS:
            raise ValueError(
                f"send_message(): channel {channel!r} is not one of {sorted(CHANNELS)}"
            )
        if extra_prompt is not None and len(extra_prompt) > MAX_EXTRA_PROMPT:
            raise ValueError(
                f"send_message(): extra_prompt is {len(extra_prompt)} chars, over the "
                f"{MAX_EXTRA_PROMPT}-char maximum"
            )
        for key in variables or {}:
            if not str(key).startswith("@"):
                raise ValueError(
                    f"send_message(): injected variable {key!r} must be @-prefixed"
                )

        payload: dict[str, Any] = {"id": str(id).strip()}
        for key, value in (
            ("message", message),
            ("bot", bot),
            ("channel", channel),
            ("extra_prompt", extra_prompt),
            ("character_limit", character_limit),
            ("followup", followup),
            ("model", model),
            ("goals_finished", goals_finished),
            ("delay", delay),
            ("limit", limit),
        ):
            if value is not None:
                payload[key] = value
        if is_update:
            payload["is_update"] = True
        payload.update(variables or {})

        headers: dict[str, str] = {}
        if self._auth_in_body:
            payload["api_key"] = self._config.api_key
        else:
            # Built by `ApiKeyAuth`, not spelled here. This line used to read
            # `headers["X-CB-KEY"] = self._config.api_key`, which made the header *form* the
            # one axis of this surface that no caller could vary: `Config` has carried three
            # styles since phase 03, and two of them — including the `Authorization: Bearer`
            # form RESEARCH.md ties to `api.closebot.ai` specifically — were unreachable on
            # the runtime host. Ten live 410s were then recorded as having exhausted the
            # credential's shapes when only its *placement* had been varied.
            headers.update(ApiKeyAuth(self._config.api_key, self._auth_style).headers())

        attempt = 0
        while True:
            with self._pacer.acquire(write=True, description="POST /message") as slot:
                if slot.dry_run_blocked:
                    # `self.endpoint` is the **whole** URL — `message_endpoint` has already
                    # resolved the path onto it. Appending "/message" to the format string
                    # here printed `.../message/message`, which is verbatim the string that
                    # cost this module a 403-Missing-Authentication-Token misdiagnosis, and
                    # made the dry-run preview show a request that would never be sent. A
                    # dry run's whole job is to print the request; a URL that is not the
                    # one the send uses is not the request.
                    log.warning(
                        "DRY RUN would send POST %s\n  body: %s",
                        self.endpoint,
                        self._config.scrub(loggable(payload)),
                    )
                    return LiveReply(status_code=0, raw={"dry_run": True, "sent": False})
                # Consume this thread's one-shot send authorization, exactly as `Session`
                # does. Not ceremony: it is what makes "no unpaced route to the runtime
                # endpoint" structural rather than a promise — a second send inside one
                # slot, or any send outside one, raises PacingBypassError instead of
                # spending a credit.
                self._pacer.assert_in_slot("POST", self.endpoint)
                response = self._client.post(self.endpoint, json=payload, headers=headers)

            decision = self._pacer.note_response(
                response.status_code,
                retry_after=self._retry_after(response),
                attempt=attempt,
                # The Pacer needs the body to tell "your plan is maxed" from "your key is
                # bad", which share a status. Guarded: a body we cannot read must not mask
                # the status. It is classification input only — never logged or stored.
                body_text=self._body_text(response),
            )
            # The decision used to be computed and then `del`-eted, on the reasoning that
            # the runtime's 4xx codes are semantic rather than rate limits. True of those
            # codes — and it silently dropped phase 04's 429 backoff on this surface too,
            # so a throttled send raised "undocumented status 429" immediately while the
            # Pacer logged a backoff nobody waited out. The reasoning is now expressed as
            # the condition it always was, per status, in `_may_retry`.
            if not decision.should_retry:
                return self._handle(response)
            if not _may_retry(response.status_code):
                # The Pacer has already logged "backing off Ns" at WARNING — that is its
                # own policy speaking, and this surface is about to narrow it. Saying so is
                # not noise: a backoff line with no backoff after it is exactly the trail a
                # critic followed to `del decision`, and silence here would leave the same
                # trail for the statuses that are *correctly* not re-sent.
                log.warning(
                    "live message %s: not re-sending. The pacer offered a %.1fs backoff, "
                    "but this surface only re-sends %s — see live.RETRY_STATUSES.",
                    response.status_code,
                    decision.backoff_s,
                    sorted(RETRY_STATUSES),
                )
                # Declining the offer means nobody will wait it out, so the Pacer must not
                # go on reporting it. Two reviewers caught `pacing-status` showing a backoff
                # that was never observed — the same stale-backoff defect phase 07 fixed
                # once, re-created by this narrowing path.
                self._pacer.decline_backoff()
                return self._handle(response)
            # A fresh slot per attempt: think-time, budget and the serial write lane apply
            # to a re-send exactly as to a first send. Same shape as `RestClient.request`.
            self._pacer.sleep_for_backoff(decision.backoff_s)
            attempt += 1

    @staticmethod
    def _body_text(response: "httpx.Response") -> str | None:
        """The response body as text, or ``None`` if it cannot be read.

        Broad on purpose: this feeds the Pacer's classifier, which needs the body to tell
        "your plan is maxed" from "your key is bad" — two conditions that share a status. A
        body we cannot decode must not *mask the status*, so any failure here degrades to
        ``None`` and the status still speaks. It is classification input only: never logged,
        never stored.

        (This rationale lived in a `# noqa: BLE001` comment, which the round-14 `RUF100` sweep
        removed wholesale along with the directive — the comment was attached to a suppression
        rather than to the code. Its twin in `rest.py` survived because it was a docstring.
        That is the argument for putting reasoning in docstrings, not beside directives.)
        """
        try:
            return response.text
        except Exception:
            # Broad by design — see the docstring. BLE001 is not in the enabled rule set, so
            # no suppression is written here: RUF100 would flag a directive that suppresses
            # nothing, which is exactly the defect that removed this comment in the first place.
            return None

    @staticmethod
    def _retry_after(response: "httpx.Response") -> float | None:
        """The server's ``Retry-After`` in seconds, when it sent one.

        Delegates to ``rest.RestClient``'s parser instead of re-deriving it. The header has
        two legal spellings (delay-seconds and an HTTP-date), and a second implementation of
        that is precisely the drift this module's docstring refuses for the masking
        vocabulary — with the added edge that the failure mode is silent: a date-form header
        an unaware parser drops becomes an exponential backoff that ignores what the server
        asked for. It is private to ``rest`` today, which is the real smell; the honest home
        is a shared HTTP helper. Because this is called on *every* response, a rename over
        there fails this module's entire suite at once rather than only on a 429.
        """
        from closewire_client.rest import RestClient

        return RestClient._retry_after(response)

    def _scrub(self, response: "httpx.Response") -> Any:
        """Decode one body with both maskings applied, in the order that makes them work.

        **Scrub the raw text, then parse, then mask by name.** Each step catches something
        the others structurally cannot:

        1. :meth:`Config.scrub` is *value*-based — it replaces the literal configured key
           wherever it appears — so it is the only rule that finds our key in a **value**
           (``{"error": "invalid credentials (if using api_key): <key>"}``, which is the
           shape of the live 410) or in a body that is a bare JSON string
           (``"rejected key <key>"``). Doing it on the text covers JSON and non-JSON alike.
        2. :func:`redact_secrets` / :func:`redact_text` are *name*-based, and are the only
           rules that find a **third-party** credential, whose value we do not know.

        The previous order — parse first, scrub only the branch where parsing *failed*, mask
        by name only when the result was a ``dict``/``list`` — left our own key intact in
        every JSON body and applied no redaction at all to a JSON-string body, on the one
        surface in this client that puts the key in the request body.

        This is :meth:`~closewire_client.rest.RestClient._decode` plus
        :meth:`~closewire_client.rest.RestClient._scrub_credentials`, in their order and for
        their reasons. It parses regardless of ``Content-Type``, which ``rest`` does not:
        this endpoint declares no response schema at all (spec deviation 24), so its content
        type is not something to make a decoding decision on, and the previous
        ``response.json()`` behaved this way too.

        The pipeline itself lives in :func:`scrub_body` so that a caller who cannot build a
        :class:`LiveMessageClient` can still reuse it. ``scripts/probe_runtime_auth.py``
        could not — it posts a ``bot_id`` field the spec does not declare, so it constructs
        its own request — and it therefore re-implemented *half* of this, applying
        ``config.scrub`` and skipping the name-based rule. Its output is committed as
        evidence, so a third party's credential echoed in a 410 body would have shipped
        unmasked. Half a two-part pipeline is the failure mode a private method invites.
        """
        return scrub_body(self._config, response.text)

    def _handle(self, response: "httpx.Response") -> LiveReply:
        """Map a runtime response onto a reply or a typed error.

        Decode once, scrub once (:meth:`_scrub`), branch on status last — so there is no
        exit from this method, success or failure, that has not passed the scrubber. An
        error from this endpoint can echo the request, and on this surface the request may
        carry ``api_key`` in the body; a 200 is scrubbed on the same footing, because
        ``LiveReply.raw`` is kept whole and its ``repr`` prints whatever is in it.
        """
        status = response.status_code
        body = self._scrub(response)

        if status == 200:
            return LiveReply(status_code=status, raw=body)

        exc_type = STATUS_MAP.get(status)
        if exc_type is not None:
            raise exc_type(
                f"live message {status}: {STATUS_HELP.get(status, 'see the runtime docs')}",
                status_code=status,
                body=body,
            )
        raise LiveMessageError(
            f"live message returned an undocumented status {status}",
            status_code=status,
            body=body,
        )
