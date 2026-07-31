"""Credential redaction, enforced at the transport boundary.

Closebot returns third-party credentials in ordinary read responses: every source
carries the GoHighLevel ``accessToken`` and ``key`` for a *client's* CRM sub-account, and
those same values are embedded inside unrelated payloads — a bot row nests
``sources: [{id, name, key, …}]`` holding the identical credential.

Redacting per call-site does not work. It was tried first, and the leak simply moved: the
sources module masked the credential while the bots module handed it out, because
protection was a thing each author had to remember. A field name is also not enough on
its own to locate the risk — the embedded copy uses ``id`` rather than ``sourceId``, so
any rule keyed on "looks like a source" misses it.

**So redaction is default-deny at the single response boundary**
(:meth:`~closewire_client.rest.RestClient._handle`). Note this is a *different* layer from
pacing, which is enforced one level lower in
:meth:`~closewire_client.session.Session.request`: code that uses ``Session`` directly gets
pacing but **not** redaction, so every surface must go through ``RestClient``.
Every response — generated endpoint or hand-written, existing or added later — is scrubbed
unless the caller explicitly asks otherwise. A new endpoint is safe without its author
doing anything, which is the only property that survives contact with future phases.

**There are two escape hatches**, both on :meth:`RestClient.request`, both logged:

* ``include_secrets=True`` — returns credentials unmasked. Requires a client built with
  ``allow_secrets=True``; an ordinary client raises. Phase 10 needs it to read a real OAuth
  token when wiring a sub-account.
* ``static_schema=True`` — skips scrubbing a 2xx catalogue response. **Mechanically the
  same bypass**, and not capability-gated: it is unreachable from caller data (no wrapper
  forwards ``**kwargs`` here), so setting it requires editing source. One caller,
  ``bots.node_descriptors``.

  Its stated reason — that the response "uses ``key`` for node property names
  (``EnableGhlBooking``, ``UseAI``, …) and would be corrupted by scrubbing" — **is no longer
  true**, and saying so is the point of writing it down. Every one of those ``key`` fields is
  the label of a ``BotNodePropertyCondition`` (``{"key": "BodyType", "value": "formdata"}``),
  and slot-awareness now leaves all of them alone: scrubbing the vendored catalogue changes
  17 values before this fix and 0 after. The flag stays anyway, for the reason it should have
  been justified by in the first place — it is a deliberate, greppable statement that this
  one response carries no account data — and not on the strength of a redactor behaviour that
  has already changed once.

An earlier design exempted the catalogue by matching its **path** instead. That was wrong
and was removed: the match was a substring test against an un-normalized path, so
``/agency/source?q=/bot/nodeDescriptors`` disabled redaction on a real source read, and
``/bot/nodeDescriptors/../../agency/source`` fetched the source list (httpx normalizes the
wire path) while the exemption had already been granted on the pre-normalization string.
Pattern-matching a caller-influenceable string is not a control. An explicit flag at one
greppable call site is.

**A credential's name is not always a field name.** Matching field names alone was blind to
a whole shape: a *name/value pair*, where the name lives in **data** position and the
generic key next to it holds the secret. The catalogue declares two of them —
``BotAtomicNodeWebHookHeader`` (``Header`` / ``Value``) and ``BotAtomicNodeWebHookParameter``
(``Key`` / ``Value``) — and the API declares more (``BotVariableDto``, ``LeadFieldDto``,
``BotNodePropertyCondition``). So a legal Job-Flow graph carries
``{"Header": "Authorization", "Value": "Bearer sk_live_…"}`` past a rule keyed on ``Header``
and ``Value``, and phase 07's dry-run payload log printed it verbatim at WARNING. The same
credential written ``{"authorization": "Bearer sk_live_…"}`` was masked — identical facts,
opposite outcomes, purely because of which slot the *name* happened to occupy.

The fix is not a WebHook special case. **One vocabulary of credential names is applied
wherever a name can appear** — as a field name and as the value of a pair's name slot — but
*which* of those a given key is, is decided **once per mapping, structurally**, before any
rule fires (:func:`_pair_slots`).

**Why slot-awareness rather than one more rule.** The first attempt ran the field-name rule
and the pair rule as two independent passes over the same mapping. Their vocabularies
overlap — ``key`` is a genuine credential field on ``SourceDto`` *and* the label of
``BotAtomicNodeWebHookParameter`` — and neither pass knew which slot of a structure it was
looking at, so on ``{"Key": "page", "Value": "3"}`` the field rule masked the harmless label
while the pair rule (correctly) declined to mask anything. The pair came out **inverted**:
``{"Key": "<redacted>", "Value": "3"}`` for a page number, and ``{"Key": "<redacted>",
"Value": "sk_live_…"}`` for a real one. Both directions failed at once — the credential
printed, and every WebHook-bearing flow acquired a sentinel, which :func:`contains_redacted`
turns into a refused ``bots.save``, making read-modify-write of a real bot impossible.

So a key gets **one role**:

* **label** — a key in :data:`_PAIR_NAME_KEYS` holding a string, in a mapping that also has a
  value slot. Its value is a *name*, i.e. data. The field-name rule is not applied to it,
  because ``Key`` here does not mean "this holds a credential", it means "this says what the
  neighbour holds".
* **payload** — a key in :data:`_PAIR_VALUE_KEYS` in that same mapping. Masked iff a label
  names a credential, and then *only* it — the label stays readable, which is the whole
  point of the dry-run log.
* **field** — everything else, including every key of a mapping that is not a pair. The
  field-name rule applies exactly as before.

The structural gate is the presence of a **value slot**, and it is what makes the demotion
safe rather than a guess: across all 130 declared schemas in ``schema/openapi.json`` (150 in
the live spec) the only one pairing a credential-vocabulary name with a ``value``-ish sibling
is ``BotNodePropertyCondition {key, value}`` — where ``key`` is a label (``"BodyType"``).
Every schema that genuinely carries a credential — ``SourceDto``, ``AddSourceInput``,
``AgencySourceChannelDto``, ``CreateSourceChannelInput``, ``BotSourceMcpAuthInput`` — has no
value slot at all, so none of them is ever read as a pair and ``key`` stays a credential
there. A label's value is still walked, so a demoted label holding ``Bearer …`` is masked by
the string rule below anyway.

A string may also name itself, in three ways: an ``Authorization``-style value in value
position (``Bearer <token>``); a query parameter with a credential name inside a URL, because
``?api_key=…`` is a name/value pair that happens to be spelled in string syntax; and a JSON
document carried *inside* a string field, because ``WebHook.JsonBody`` is declared free text
and is where a webhook's key goes when it is not a header — the identical bytes were masked
when they arrived as a whole response body and survived when they arrived as a field value.
Nothing else about a value — length, entropy, vendor prefix — is evidence, and nothing else
is redacted; see :func:`_redact_string`.

:func:`find_unredacted` is kept honest by construction rather than by a parallel rule set:
it derives slot roles from the same :func:`_pair_slots` and asks whether the redactor would
*still* change the payload, so the two cannot drift. That works because every rule here is
idempotent.

This never touches the Closebot API key itself; that is masked separately by
:meth:`Config.scrub`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, NamedTuple

__all__ = [
    "SECRET_FIELDS",
    "REDACTED",
    "is_secret_name",
    "redact_secrets",
    "redact_text",
    "find_unredacted",
    "contains_redacted",
    "warn_unmasked",
]

log = logging.getLogger("closewire.redaction")

#: The masked value substituted for a credential.
REDACTED = "<redacted>"

#: Names treated as credential-bearing. Matched case- and separator-insensitively, so
#: ``Key`` / ``access_token`` / ``API_KEY`` / ``X-Api-Key`` cannot slip through a future API
#: rename — and matched **wherever a name appears**, whether as a field name or as the value
#: of a name/value pair's name slot. One vocabulary, but never both readings of the same
#: key at once: :func:`_pair_slots` decides which position a key occupies before either
#: reading is applied. That is the difference between ``Key`` on a source row and ``Key`` on
#: a WebHook parameter (see the module docstring).
#:
#: The header/parameter names in the second group are the reason a pair can be recognised at
#: all; ``authToken`` and ``authorizationCode`` are real fields in the vendored spec
#: (``AgencySourceChannelDto`` puts ``authToken`` beside ``refreshToken`` and an
#: ``expiration``; ``BotSourceMcpAuthInput`` carries an OAuth ``authorizationCode``), and
#: both were leaking for want of an entry here.
#:
#: Bare ``auth`` is in that group for the same reason and with the same evidence: it is a
#: name that means credential — a header or query parameter called ``auth`` carries one —
#: and ``{"Key": "auth", "Value": "sk_live_…"}`` is a shape a WebHook node really holds. It
#: collides with nothing: no property named exactly ``auth`` is declared anywhere in
#: ``schema/openapi.json``, ``schema/openapi.live.json``, or the node catalogue, and the
#: near-misses that *are* declared (``authId``, ``authorizeEndpoint``) do not fold to it.
SECRET_FIELDS = frozenset(
    {
        "accessToken",
        "refreshToken",
        "key",
        "token",
        "apiKey",
        "secret",
        "clientSecret",
        "password",
        "webhookCallback",
        # Credential-bearing header and query-parameter names.
        "auth",
        "authorization",
        "proxyAuthorization",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        # Closebot's own header name. Its absence was carried as "non-blocking" for six
        # review rounds on the reasoning that `Config.scrub` catches *our* key by value —
        # true, and irrelevant to a **third party's** key arriving as `{"X-CB-KEY": "..."}`
        # in a webhook body or a Job-Flow payload, which this set is what would mask. A
        # one-token omission does not earn a permanent footnote.
        "x-cb-key",
        "authToken",
        "apiToken",
        "authorizationCode",
    }
)


_SEPARATORS = re.compile(r"[\s_.\-]+")


def _normalize(name: str) -> str:
    """Fold a name to its comparison form: no case, no ``_ - .`` or spaces.

    ``-`` and whitespace matter now that names arrive as *data*: a header is written
    ``X-Api-Key``, a lead field ``API Key``, a JSON field ``api_key``. All three name the
    same credential and must fold together.
    """
    return _SEPARATORS.sub("", name).lower()


_SECRET_NAMES = frozenset(_normalize(name) for name in SECRET_FIELDS)

#: Keys whose *value* is a name — the name slot of a name/value pair, and therefore the keys
#: :func:`_pair_slots` may exempt from the field-name rule. Deliberately a small list of
#: label-ish keys rather than "any sibling": ``{"type": "token", "value": 512}`` in some
#: future usage row must not be read as a credential named ``token``.
_PAIR_NAME_KEYS = frozenset(
    {
        "name",
        "key",
        "header",
        "headername",
        "field",
        "fieldname",
        "parameter",
        "param",
        "paramname",
        "property",
        "propertyname",
        "label",
        "variable",
    }
)

#: Keys that hold the pair's payload — the only keys a pair rule may mask. A sibling
#: ``Description`` or ``Type`` is metadata about the credential, not the credential, and
#: masking it would cost legibility for nothing.
_PAIR_VALUE_KEYS = frozenset({"value", "values", "defaultvalue"})

#: An ``Authorization``-style value that names its own scheme, in *value position* only:
#: at the start of the string, or straight after a quote/colon/equals. "the Bearer token"
#: in a prompt is preceded by a space and never matches. The credential must also be 16+
#: characters and contain a digit, so ``Bearer authentication`` stays intact.
_BEARER_VALUE = re.compile(
    r"""(?:^|(?<=["'=:]))(\s*Bearer\s+)(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}""",
    re.IGNORECASE,
)

#: The same, for HTTP Basic. Restricted to the base64 alphabet — no ``-`` or ``_`` — which
#: is what makes it safe: ``Basic 3-bedroom-listing-info`` cannot match, a base64 blob can.
_BASIC_VALUE = re.compile(
    r"""(?:^|(?<=["'=:]))(\s*Basic\s+)(?=[A-Za-z0-9+/]*[0-9])[A-Za-z0-9+/]{16,}={0,2}""",
    re.IGNORECASE,
)

#: One ``name=value`` in a URL query string. Only applied to strings containing ``://``, so
#: prose that mentions ``api_key=YOUR_KEY`` is left readable.
_URL_PARAM = re.compile(r"""([?&])([A-Za-z0-9_.\-]{1,64})=([^&#\s"']*)""")

#: A string that might be a JSON document rather than prose. Only a cheap gate before
#: ``json.loads`` decides for real — matching, not slicing, so a large body is not copied.
_JSON_DOCUMENT = re.compile(r"\s*[{\[]")


def is_secret_name(name: object) -> bool:
    """True when a name denotes a credential, ignoring case and separators.

    Applies to a name in either position — ``{"apiKey": …}`` and
    ``{"Header": "X-Api-Key", "Value": …}`` state the same fact. It answers only *"is this
    string a credential name?"*; **which** position a given key occupies, and therefore
    whether this answer is the relevant one, is :func:`_pair_slots`'s decision. Asking this
    of a key without asking that first is what inverted ``{"Key": "page", "Value": "3"}``.
    """
    return isinstance(name, str) and _normalize(name) in _SECRET_NAMES


class _Slots(NamedTuple):
    """Which role each key of one mapping plays. Computed once; both rules read it.

    ``labels`` are the keys the field-name rule must **not** fire on, because their value is
    a name rather than a credential. ``masked`` are the value-slot keys the pair rule masks
    because a label named a credential — empty for a pair that names something harmless.
    Both are empty for a mapping that is not a pair, which is the overwhelming majority and
    behaves exactly as it did before slots existed.
    """

    labels: frozenset[str]
    masked: frozenset[str]


#: A mapping that is not a name/value pair: every key keeps the plain field-name rule.
_NOT_A_PAIR = _Slots(frozenset(), frozenset())


def _pair_slots(mapping: dict) -> _Slots:
    """Assign each key of ``mapping`` its slot role, structurally and before any masking.

    A mapping is a name/value **pair** when it has both halves of one: at least one
    label-ish key (:data:`_PAIR_NAME_KEYS`) holding a **string** — a name is a string, and a
    ``key`` holding an object is not labelling anything — and at least one value-ish key
    (:data:`_PAIR_VALUE_KEYS`) for that name to be about.

    Both halves are required, and that is the entire safety argument for demoting a label:

    * **No value slot ⇒ not a pair.** ``{"id": "src_1", "name": "C", "key": <oauth>}`` — the
      embedded source row this module exists for — has no ``value`` sibling, so ``key``
      keeps the field rule and stays masked. Every credential-carrying schema in the spec
      (``SourceDto``, ``AddSourceInput``, ``AgencySourceChannelDto``,
      ``CreateSourceChannelInput``, ``BotSourceMcpAuthInput``) is of that shape.
    * **A value slot ⇒ the label-ish keys are labels.** ``{"Key": "page", "Value": "3"}``,
      ``{"Header": "Authorization", "Value": …}``, ``{"key": "BodyType", "value":
      "formdata"}``. The one declared schema that puts a credential-vocabulary name beside a
      ``value`` is ``BotNodePropertyCondition {key, value}``, where ``key`` is a label.

    *Any* label-ish key of a pair may carry the name — ``LeadFieldDto`` declares
    ``{field, name, value, aiProduced}`` and either of ``field``/``name`` can be the one, so
    the masking gate asks all of them.

    **Demotion is narrower than the gate, and deliberately so.** Only one key in the label
    vocabulary is also a credential field name — ``key`` — so it is the only key whose role
    is genuinely contested, and it is contested only when *another* label-ish key is present
    to do the labelling instead. A hypothetical ``{"id": …, "name": "C", "key": <oauth>,
    "value": …}`` has two claimants; ``name`` is plainly the label, which leaves ``key`` a
    credential, so it keeps the field rule and stays masked. ``{"Key": "page", "Value":
    "3"}`` has one claimant and no ambiguity to resolve. The asymmetry runs the safe way in
    both directions: an extra claimant can only cause *more* masking, never less, so no
    credential masked before this change is unmasked by it.

    The gate on masking is unchanged: ``{"Header": "Content-Type", "Value":
    "application/json"}`` and ``{"Key": "page", "Value": "3"}`` come back untouched;
    ``{"Header": "Authorization", …}`` does not.
    """
    naming = frozenset(
        key
        for key, value in mapping.items()
        if isinstance(key, str)
        and _normalize(key) in _PAIR_NAME_KEYS
        and isinstance(value, str)
    )
    if not naming:
        return _NOT_A_PAIR
    values = frozenset(
        key
        for key in mapping
        if isinstance(key, str) and _normalize(key) in _PAIR_VALUE_KEYS
    )
    if not values:
        return _NOT_A_PAIR
    labels = naming
    if len(naming) > 1:
        # Contested name slot: a key that is a credential field name in its own right is
        # only read as a label when nothing else is claiming that role.
        labels = frozenset(key for key in naming if not is_secret_name(key))
    names_a_credential = any(is_secret_name(mapping[key]) for key in naming)
    return _Slots(labels, values if names_a_credential else frozenset())


def _is_credential_slot(name: object, slots: _Slots) -> bool:
    """Whether this key of a mapping holds a credential — the one question both rules ask.

    Order matters and is the fix: the pair rule is consulted first, then the field-name rule
    is applied *only to keys that are not a label*. Two passes with overlapping vocabularies
    is what inverted ``{"Key": …, "Value": …}``; there is one pass now.
    """
    if name in slots.masked:
        return True
    return name not in slots.labels and is_secret_name(name)


def _redact_string(text: str) -> str:
    """Mask a credential that a *string* names itself, leaving everything else alone.

    Three shapes only, all self-declaring — the string says "this is a credential", nothing
    is inferred from how the value looks:

    * an auth-scheme value (``Bearer <token>``, ``Basic <base64>``) in value position,
    * a URL query parameter whose *name* is a credential name (``?api_key=…``), which is a
      name/value pair spelled in string syntax, and
    * a **JSON document carried inside the string** (:func:`_redact_embedded_json`), whose
      field names are names in exactly the sense the rest of this module means.

    Deliberately **not** here: entropy, length, or vendor prefixes (``sk_``, ``ghp_``). A
    graph is full of opaque node ids, GUIDs and base64; scoring them would mask ordinary
    data, and — because :func:`contains_redacted` refuses a write built from a masked read —
    turn a legible dry-run log into an unwritable graph.

    Idempotent: masking twice changes nothing, which is what lets :func:`find_unredacted`
    detect a leak by asking whether this function would still change the value.
    """
    lowered = text.lower()
    if "bearer" in lowered:
        text = _BEARER_VALUE.sub(r"\g<1>" + REDACTED, text)
    if "basic" in lowered:
        text = _BASIC_VALUE.sub(r"\g<1>" + REDACTED, text)
    if "://" in text:
        text = _URL_PARAM.sub(_mask_url_param, text)
    return _redact_embedded_json(text)


def _redact_embedded_json(text: str) -> str:
    """Scrub a JSON document that arrived as a *string field value*, not as a body.

    ``WebHook.JsonBody`` is a declared catalogue property holding free text, and it is where
    a webhook's key goes when it is not a header. The identical bytes were already masked
    correctly by :func:`redact_text` when they arrived as a whole response body; as a field
    value they went straight out, at WARNING, in the dry-run payload log — and
    :func:`find_unredacted` reported no leak, so the verification harnesses structurally
    could not see it. Structure does not stop being structure because it is quoted.

    **Returns ``text`` byte-for-byte unless a credential was actually masked.** That is not
    an optimisation, it is the correctness condition. Re-serializing unconditionally would
    reformat every JSON-bearing field of every response — ``{"a":1}`` → ``{"a": 1}`` — which
    would make :func:`find_unredacted` report a leak for a payload holding no credential at
    all, and would rewrite a caller's body under them. Only a real mask earns a rewrite; the
    rewrite then costs the graph a :func:`contains_redacted` refusal, which is the correct
    price for a graph that genuinely holds a credential.

    Terminates without a depth limit: parsing consumes at least the enclosing braces and
    quotes, so any JSON nested inside the result is strictly shorter than what produced it.
    """
    if not _JSON_DOCUMENT.match(text):
        # Not even shaped like a document. Skips `json.loads` for the overwhelming majority
        # of strings — node ids, prose, URLs — and keeps prose such as "{see the api key
        # doc}" readable, since it will not parse either way.
        return text
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    scrubbed = redact_secrets(parsed)
    return text if scrubbed == parsed else json.dumps(scrubbed)


def _mask_url_param(match: "re.Match[str]") -> str:
    """Mask one ``?name=value`` when ``name`` is a credential name."""
    lead, name, _value = match.groups()
    return f"{lead}{name}={REDACTED}" if is_secret_name(name) else match.group(0)


def redact_secrets(payload: Any) -> Any:
    """Return a copy of ``payload`` with every credential-bearing field masked.

    Walks dicts, lists, and tuples to any depth, so a credential nested inside an
    unrelated object is masked just as reliably as a top-level one. Each mapping's slot
    roles are resolved once (:func:`_pair_slots`), then a value is masked when its key is a
    credential slot (:func:`_is_credential_slot`) — its **field name** is a credential name
    and it is not a pair's label, or a sibling label names it as one — or when the string
    itself does (:func:`_redact_string`). Everything else is returned unchanged, including a
    pair's label, and an already-masked value is left alone.
    """
    if isinstance(payload, dict):
        slots = _pair_slots(payload)
        return {
            name: (
                REDACTED
                if _is_credential_slot(name, slots) and value
                else redact_secrets(value)
            )
            for name, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact_secrets(item) for item in payload]
    if isinstance(payload, str):
        return _redact_string(payload)
    return payload


def redact_text(body: str) -> str:
    """Mask credentials in a body that arrived as text rather than decoded JSON.

    Some endpoints return JSON with a non-JSON content type, so the transport hands back a
    ``str`` and structural redaction would skip it entirely. When the text parses as JSON
    it is scrubbed and re-serialized.

    When it does not parse there is no structure to key on, but a string can still name its
    own credential (``Bearer <token>``, ``?api_key=…``), so it gets the same
    :func:`_redact_string` pass a string inside a decoded body gets. Leaving this branch
    alone would have made the *text* path — the one ``rest.py`` uses for every body served
    with the wrong content type — the weakest one.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return _redact_string(body) if isinstance(body, str) else body
    scrubbed = redact_secrets(parsed)
    return body if scrubbed == parsed else json.dumps(scrubbed)


def contains_redacted(payload: Any) -> bool:
    """True when ``payload`` carries the :data:`REDACTED` sentinel anywhere.

    Used to refuse a *write* built from a redacted read. Read-modify-write is the natural
    shape for updating a bot, and sending the masked value back would overwrite a live
    client's OAuth credential with the literal string ``"<redacted>"``.
    """
    if isinstance(payload, str):
        # Substring, not equality: a value built by interpolating a masked read
        # ("pre<redacted>post") is just as wrong to send as the bare sentinel.
        return REDACTED in payload
    if isinstance(payload, dict):
        return any(contains_redacted(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(contains_redacted(item) for item in payload)
    return False


def find_unredacted(payload: Any, path: str = "$") -> list[str]:
    """Paths to every credential that is present and *not* masked.

    The inverse of :func:`redact_secrets`, for tests and verification scripts. Walks the
    whole structure — checking only the first occurrence of a field name would let a
    masked first row hide an unmasked second one.

    It asks the *same* questions :func:`redact_secrets` asks, in the same order — the same
    :func:`_pair_slots` roles, the same :func:`_is_credential_slot` test — and for a string
    it asks the question directly: "would the redactor still change this?". A rule added to
    one function therefore cannot go missing from the other, which matters because
    ``scripts/verify_reads.py`` and ``scripts/verify_cli.py`` treat this function as the
    definition of a leak. That is how a credential inside a JSON-bearing string field is
    reported: at the path of the *string*, since that is the value the redactor rewrites.
    """
    leaks: list[str] = []
    if isinstance(payload, dict):
        slots = _pair_slots(payload)
        for name, value in payload.items():
            here = f"{path}.{name}"
            if _is_credential_slot(name, slots) and value and value != REDACTED:
                leaks.append(here)
            else:
                leaks.extend(find_unredacted(value, here))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            leaks.extend(find_unredacted(item, f"{path}[{index}]"))
    elif isinstance(payload, str) and _redact_string(payload) != payload:
        leaks.append(path)
    return leaks


def warn_unmasked(where: str) -> None:
    """Log that unmasked third-party credentials are being returned.

    At WARNING so it is visible with no logging configuration at all — the same reasoning
    that puts dry-run suppression at WARNING in the pacing layer.
    """
    log.warning(
        "%s: returning UNMASKED third-party credentials — do not log, print, or pass "
        "this into a model context",
        where,
    )
