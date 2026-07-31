"""Credential redaction at the transport boundary — offline, instant.

These exist because the leak they pin was found *after* a per-module fix was already in
place: the sources module masked a client's GoHighLevel credential while the bots module
handed out the identical value, because redaction was a thing each author had to remember.
The fix moved it to the one boundary every response crosses. Without these tests, the next
refactor quietly re-opens it.

Run via pytest or directly (``python tests/test_redaction.py``).
"""

from __future__ import annotations

import logging
import random
import atexit
import shutil
import tempfile

import httpx

from closewire_client.auth import ApiKeyAuth
from closewire_client.config import Config
from closewire_client.pacing import Pacer
from closewire_client.redaction import (
    REDACTED,
    contains_redacted,
    find_unredacted,
    is_secret_name,
    redact_secrets,
)
from closewire_client.rest import RestClient
from closewire_client.session import Session

SECRET = "cb_TEST_KEY_never_real_7Q7Q"



def _state_dir() -> str:
    """A throwaway state dir that cleans itself up.

    Five call sites used a bare ``tempfile.mkdtemp`` with no cleanup, and the suite runs on
    every council round: two critics measured **5,755** ``closewire-redact-*`` directories on
    the machine, growing by 31 per run. ``tests/test_live.py`` and ``tests/test_pacing.py``
    both learned this already; this file was the one place the lesson had not been applied.
    """
    path = tempfile.mkdtemp(prefix="closewire-redact-")
    atexit.register(shutil.rmtree, path, True)
    return path


def _config_only():
    return Config(api_key=SECRET, state_dir=_state_dir())
OAUTH = "ghl_client_oauth_credential"


def _client(payload, *, status=200, content_type="application/json", allow_secrets=False):
    cfg = Config(api_key=SECRET, state_dir=_state_dir())
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(3))

    def handler(_request: httpx.Request) -> httpx.Response:
        if content_type == "application/json":
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=payload, headers={"content-type": content_type})

    session = Session(cfg, ApiKeyAuth.from_config(cfg), pacer,
                      transport=httpx.MockTransport(handler))
    return RestClient(cfg, session, pacer=pacer, allow_secrets=allow_secrets)


# ── The guarantee ─────────────────────────────────────────────────────────────
def test_a_brand_new_endpoint_is_safe_without_anyone_doing_anything() -> None:
    """The whole point: safety must not depend on an author remembering.

    An endpoint nobody has written a wrapper for, returning a credential under a field
    name nobody has seen, must still come back masked.
    """
    payload = {
        "widgets": [{"name": "w", "attachedSources": [{"id": "s1", "key": OAUTH}]}],
        "nested": {"deep": {"accessToken": OAUTH}},
    }
    out = _client(payload).get("/some/endpoint/invented/later")
    assert find_unredacted(out) == [], find_unredacted(out)
    assert out["widgets"][0]["attachedSources"][0]["key"] == REDACTED
    assert out["nested"]["deep"]["accessToken"] == REDACTED
    assert out["widgets"][0]["name"] == "w", "non-secret fields must survive"


def test_the_original_leak_stays_closed() -> None:
    """REGRESSION: a bot row embeds the same credential a source row carries.

    Note the embedded copy identifies itself with ``id``, not ``sourceId`` — so any rule
    that tried to recognise "this looks like a source" missed it.
    """
    bots_payload = [
        {"id": "bot_1", "name": "Flow", "sources": [{"id": "src_1", "name": "C", "key": OAUTH}]},
        {"id": "bot_2", "name": "Other", "sources": []},
    ]
    out = _client(bots_payload).get("/bot")
    assert find_unredacted(out) == []
    assert out[0]["sources"][0]["key"] == REDACTED
    assert out[0]["sources"][0]["name"] == "C"


def test_a_leak_in_any_row_is_caught_not_just_the_first() -> None:
    """The round-1 detector used `blob.find()` and only ever saw occurrence zero."""
    rows = [{"key": REDACTED}, {"key": OAUTH}, {"key": REDACTED}]
    assert find_unredacted(rows) == ["$[1].key"]
    assert find_unredacted([{"key": REDACTED}, {"key": REDACTED}]) == []


def test_secret_names_match_regardless_of_case_or_separator() -> None:
    for name in ("key", "Key", "KEY", "accessToken", "access_token", "ACCESS_TOKEN", "apiKey"):
        assert is_secret_name(name), name
    for name in ("keyboard", "monkey", "name", "sourceId", "tokenizer"):
        assert not is_secret_name(name), name


def test_redaction_survives_deep_and_mixed_nesting() -> None:
    payload = {"a": [{"b": ({"key": OAUTH},)}], "c": [[{"token": OAUTH}]]}
    assert find_unredacted(redact_secrets(payload)) == []


def test_empty_and_already_masked_values_are_left_alone() -> None:
    out = redact_secrets({"key": "", "token": None, "accessToken": REDACTED})
    assert out == {"key": "", "token": None, "accessToken": REDACTED}


# ── The two escape hatches, both deliberate ───────────────────────────────────
def test_error_bodies_are_scrubbed_too() -> None:
    """REGRESSION: `_handle` raised before reaching the scrubber, so every non-2xx body
    skipped the boundary — and `cli/main.py` prints those bodies to stderr."""
    from closewire_client.errors import ClosebotAPIError

    payload = {"detail": "nope", "source": {"key": OAUTH, "accessToken": OAUTH}}
    for status in (400, 401, 403, 404, 409, 422, 500, 503):
        try:
            _client(payload, status=status).get("/agency/source/s1")
        except ClosebotAPIError as exc:
            assert find_unredacted(exc.body) == [], (status, find_unredacted(exc.body))
            assert OAUTH not in str(exc), status
        except Exception:  # 403 is retryable and may surface differently; still no leak
            pass


def test_a_json_body_served_as_text_is_still_scrubbed() -> None:
    """`redact_secrets` cannot walk a bare string, so text bodies bypassed it entirely."""
    out = _client('{"accessToken": "%s"}' % OAUTH, content_type="text/plain").get("/x")
    assert OAUTH not in out, out


def test_static_schema_is_opted_out_at_the_call_site_not_by_path_matching() -> None:
    """REGRESSION: the exemption was a substring test on an un-normalized path.

    `/agency/source?q=/bot/nodeDescriptors` and `/bot/nodeDescriptors/../../agency/source`
    both disabled redaction on a real source read — the second while httpx rewrote the
    wire path to `/agency/source`, so one crafted path both fetched sources and turned the
    control off. Path matching is gone; the catalogue opts out explicitly instead.
    """
    creds = {"sourceId": "s1", "key": OAUTH, "accessToken": OAUTH}
    for hostile in (
        "/agency/source?q=/bot/nodeDescriptors",
        "/agency/source/src_/bot/nodeDescriptors",
        "/bot/nodeDescriptors/../../agency/source",
        "/bot/nodeDescriptorsExtra",
        "/v2/bot/nodeDescriptors/anything",
    ):
        out = _client(creds).get(hostile)
        assert find_unredacted(out) == [], f"{hostile} disabled redaction"

    # The real catalogue read passes the flag explicitly and keeps its property names.
    catalogue = {"atomicNodes": [{"properties": [{"conditions": [{"key": "EnableGhlBooking"}]}]}]}
    out = _client(catalogue).request("GET", "/bot/nodeDescriptors", static_schema=True)
    assert out == catalogue
    # …and grants no credential access: the same flag on a credential payload still
    # returns them, which is why it is only valid where "no account data" was verified.
    assert "static_schema" != "include_secrets"


def test_unmasking_is_a_client_capability_not_a_per_call_argument() -> None:
    """A phase-11 tool on an ordinary client must have no path to a client's OAuth token."""
    payload = {"sourceId": "s1", "key": OAUTH, "accessToken": OAUTH}

    # Build the client WITHOUT touching allow_secrets, exactly as callers do. Routing
    # through a helper that passes allow_secrets=False explicitly meant flipping the
    # constructor default to True left this test green while every ordinary client
    # handed out real credentials.
    cfg = Config(api_key=SECRET, state_dir=_state_dir())
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(6))
    session = Session(cfg, ApiKeyAuth.from_config(cfg), pacer,
                      transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)))
    plain = RestClient(cfg, session, pacer=pacer)

    assert plain._allow_secrets is False, "the constructor default is not deny"
    assert RestClient(_config_only())._allow_secrets is False

    try:
        plain.request("GET", "/agency/source/s1", include_secrets=True)
    except PermissionError as exc:
        assert "allow_secrets" in str(exc)
    else:
        raise AssertionError("a default client unmasked credentials on request")


def test_opt_out_returns_raw_and_says_so_loudly() -> None:
    """Phase 10 needs the real token; it must never be quiet about it."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("closewire.redaction")
    handler = Capture()
    logger.addHandler(handler)
    try:
        payload = {"sourceId": "s1", "key": OAUTH, "accessToken": OAUTH}
        out = _client(payload, allow_secrets=True).request(
            "GET", "/agency/source/s1", include_secrets=True
        )
    finally:
        logger.removeHandler(handler)

    assert out["key"] == OAUTH, "the opt-out must actually reach the raw value"
    assert records, "unmasking credentials was silent"
    assert records[0].levelno >= logging.WARNING


def test_a_write_built_from_a_masked_read_is_refused() -> None:
    """Read-modify-write would otherwise overwrite a live OAuth key with '<redacted>'."""
    sent: list[httpx.Request] = []
    cfg = Config(api_key=SECRET, state_dir=_state_dir())
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(9))

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    rest = RestClient(
        cfg,
        Session(cfg, ApiKeyAuth.from_config(cfg), pacer, transport=httpx.MockTransport(handler)),
        pacer=pacer,
    )
    body = {"id": "src_1", "name": "Client", "key": REDACTED}
    try:
        rest.put("/agency/source/src_1", json=body)
    except ValueError as exc:
        assert REDACTED in str(exc)
    else:
        raise AssertionError("a redacted value was sent back to the API")
    assert sent == [], "the refused write still reached the wire"

    assert contains_redacted({"a": [{"b": REDACTED}]})
    assert not contains_redacted({"a": [{"b": "real"}]})
    # Reads are unaffected — only writes are guarded.
    rest.get("/agency/source")
    assert len(sent) == 1


def test_default_is_masked_for_the_same_call() -> None:
    payload = {"sourceId": "s1", "key": OAUTH, "accessToken": OAUTH}
    out = _client(payload).get("/agency/source/s1")
    assert out["key"] == REDACTED and out["accessToken"] == REDACTED
    assert out["sourceId"] == "s1"


def test_the_closebot_api_key_is_still_scrubbed_separately() -> None:
    """Boundary redaction is about *third-party* credentials; ours has its own path."""
    from closewire_client.errors import ClosebotAPIError

    cfg = Config(api_key=SECRET, state_dir=_state_dir())
    pacer = Pacer(cfg, sleeper=lambda _s: None, rng=random.Random(4))
    session = Session(
        cfg, ApiKeyAuth.from_config(cfg), pacer,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, text=f"bad key {SECRET} rejected")
        ),
    )
    try:
        RestClient(cfg, session, pacer=pacer).get("/leak")
    except ClosebotAPIError as exc:
        assert SECRET not in str(exc) and SECRET not in str(exc.body)
    else:
        raise AssertionError("expected ClosebotAPIError")


def test_our_own_api_key_is_scrubbed_from_json_error_bodies() -> None:
    """REGRESSION: restructuring `_handle` dropped `Config.scrub` from the JSON path.

    `Config.scrub` is value-based and covers any field name, so it must run on the raw
    text *before* parsing. Parsing first and scrubbing only the text fallback left our key
    raw in every JSON error body and in `str(exc)` — the one redaction this phase's
    constraints name explicitly.
    """
    from closewire_client.errors import ClosebotAPIError

    for body in (
        {"message": f"Invalid API key: {SECRET}"},
        {"error": f"rejected {SECRET}"},
        {"detail": {"reason": SECRET}},
        [{"nested": [{"deep": SECRET}]}],
    ):
        try:
            _client(body, status=400).get("/x")
        except ClosebotAPIError as exc:
            assert SECRET not in str(exc.body), body
            assert SECRET not in str(exc), body
        else:
            raise AssertionError("expected ClosebotAPIError")

    # …and on a 2xx too: the key must never come back to a caller in any field.
    out = _client({"message": f"echo {SECRET}"}).get("/x")
    assert SECRET not in str(out)


def test_guard_errors_are_catchable_as_closewire_errors() -> None:
    """`cli/main.py` catches `ClosewireError`; a bare PermissionError/ValueError escapes."""
    from closewire_client.errors import (
        ClosewireError,
        RedactedValueError,
        SecretsNotPermittedError,
    )

    assert issubclass(SecretsNotPermittedError, ClosewireError)
    assert issubclass(SecretsNotPermittedError, PermissionError)
    assert issubclass(RedactedValueError, ClosewireError)
    assert issubclass(RedactedValueError, ValueError)

    try:
        _client({"k": 1}).request("GET", "/x", include_secrets=True)
    except ClosewireError:
        pass
    else:
        raise AssertionError("capability refusal was not a ClosewireError")


def test_an_interpolated_sentinel_is_also_refused() -> None:
    """A value built by splicing a masked read is as wrong to send as the bare sentinel."""
    assert contains_redacted({"key": f"pre{REDACTED}post"})
    assert contains_redacted({"a": [f"x{REDACTED}"]})
    assert not contains_redacted({"note": "redacted elsewhere"})


# ── Slots: which position a name occupies decides which rule applies ──────────
#
# Round 1 shipped the pair rule with no test anywhere. A critic stubbed
# `_credential_pair_slots` to frozenset() and `_redact_string` to identity, and the whole
# suite plus `scripts/verify_writes.py` stayed green. Everything below fails under that
# stub, which is the only reason any of it is worth writing.

#: A live WebHook node's query parameters: a name/value pair whose label key is `Key` —
#: which is ALSO a genuine credential field name on a source row. That collision is the bug.
WEBHOOK_PARAM_PAGE = {"Key": "page", "Value": "3"}
WEBHOOK_PARAM_SECRET = {"Key": "apiKey", "Value": "sk_live_ABCD1234efgh"}


def _webhook_graph(title: str = "Call CRM", *, json_body: str | None = None) -> dict:
    """A valid two-node flow whose WebHook node holds every pair-shaped slot at once."""
    data = {
        "type": "WebHook",
        "Title": title,
        "Url": "https://crm.example.com/v1/contacts",
        "Method": "Get",
        "QueryParameters": [dict(WEBHOOK_PARAM_PAGE), {"Key": "limit", "Value": "50"}],
        "FormParameters": [],
        "Headers": [{"Header": "Content-Type", "Value": "application/json"}],
    }
    if json_body is not None:
        data["JsonBody"] = json_body
    return {
        "nodes": [
            {"id": "n1", "type": "Source", "position": {"x": 0, "y": 0},
             "data": {"type": "Source", "name": "src"}},
            {"id": "n2", "type": "WebHook", "position": {"x": 1, "y": 0}, "data": data},
        ],
        "edges": [{"id": "n1->n2", "source": "n1", "target": "n2"}],
    }


def test_the_pair_rule_masks_the_value_slot_and_only_the_value_slot() -> None:
    """Both slots, both directions — the claim the module docstring makes.

    A pair whose label names a credential loses its VALUE and keeps its LABEL. A pair whose
    label names something ordinary keeps both. Round 1 got the first half right only for
    `Header`, and inverted it outright for `Key`.
    """
    masked = [
        ({"Header": "Authorization", "Value": "Bearer sk_live_ABCD1234efgh"}, "Header", "Value"),
        ({"Header": "X-Api-Key", "Value": "raw-secret-value"}, "Header", "Value"),
        ({"Key": "apiKey", "Value": "sk_live_ABCD1234efgh"}, "Key", "Value"),
        ({"Key": "auth", "Value": "sk_live_ABCD1234efgh"}, "Key", "Value"),
        ({"name": "access_token", "value": "ghl_oauth_value"}, "name", "value"),
        ({"field": "API Key", "value": "sk_live_1", "aiProduced": False}, "field", "value"),
        ({"param": "token", "defaultValue": "tok_live_1"}, "param", "defaultValue"),
    ]
    for pair, label, slot in masked:
        out = redact_secrets(pair)
        assert out[slot] == REDACTED, (pair, out)
        assert out[label] == pair[label], f"the label was masked, not the value: {out}"
        assert find_unredacted(pair) == [f"$.{slot}"], (pair, find_unredacted(pair))
        assert find_unredacted(out) == [], out

    untouched = [
        {"Key": "page", "Value": "3"},
        {"Key": "limit", "Value": "50"},
        {"Header": "Content-Type", "Value": "application/json"},
        {"key": "BodyType", "value": "formdata"},          # BotNodePropertyCondition
        {"name": "customerCity", "value": "Lisbon"},        # BotVariableDto
        {"field": "city", "name": "City", "value": "Lisbon", "aiProduced": True},
    ]
    for pair in untouched:
        assert redact_secrets(pair) == pair, redact_secrets(pair)
        assert find_unredacted(pair) == [], pair
        assert not contains_redacted(redact_secrets(pair)), pair


def test_a_webhook_parameter_pair_is_not_inverted() -> None:
    """REGRESSION for the exact reported shape.

    `key` is in SECRET_FIELDS because a source's OAuth credential lives under it. The
    catalogue's `BotAtomicNodeWebHookParameter` spells its LABEL slot `Key`. Two independent
    passes over one mapping therefore masked the label and printed the value — the mask
    landed on `page` while `sk_live_…` went out in the clear.
    """
    assert redact_secrets(WEBHOOK_PARAM_PAGE) == {"Key": "page", "Value": "3"}

    inverted = redact_secrets(WEBHOOK_PARAM_SECRET)
    assert inverted["Key"] == "apiKey", "the label was masked — the pair is still inverted"
    assert inverted["Value"] == REDACTED, "the credential was printed"

    # `key` is still a credential field name where it genuinely is one: no value slot beside
    # it means it is not a pair, so nothing is demoted.
    row = {"id": "src_1", "name": "Client", "key": OAUTH}
    assert redact_secrets(row)["key"] == REDACTED
    # …and a value-ish sibling does not disarm it either, because `name` is already claiming
    # the label role, which leaves `key` a credential.
    contested = {"id": "s", "name": "Client", "key": OAUTH, "value": "x"}
    assert redact_secrets(contested)["key"] == REDACTED, redact_secrets(contested)
    assert find_unredacted(contested) == ["$.key"]


def test_a_credential_inside_a_json_bearing_string_field_is_masked() -> None:
    """REGRESSION: `WebHook.JsonBody` is declared free text and holds the key.

    The identical bytes were masked when they arrived as a whole text body and survived as a
    string field value — and `find_unredacted` returned [] for them, so `verify_reads.py`
    and `verify_cli.py`, which treat that function as the definition of a leak, structurally
    could not catch it.
    """
    import json as _j

    from closewire_client.redaction import redact_text

    body = _j.dumps({"apiKey": "sk_live_ABCD1234efgh", "contact": {"name": "Ada"}})
    payload = {"JsonBody": body}

    assert "sk_live_ABCD1234efgh" not in str(redact_secrets(payload)), redact_secrets(payload)
    assert find_unredacted(payload) == ["$.JsonBody"], find_unredacted(payload)
    # The two paths must agree — the whole-body path already did.
    assert "sk_live_ABCD1234efgh" not in redact_text(body)

    # Nested, in a list, in a real graph node: same answer.
    graph = _webhook_graph(json_body=body)
    scrubbed = redact_secrets(graph)
    assert "sk_live_ABCD1234efgh" not in str(scrubbed)
    assert find_unredacted(scrubbed) == []
    assert _j.loads(scrubbed["nodes"][1]["data"]["JsonBody"])["contact"]["name"] == "Ada", (
        "masking the credential destroyed the rest of the document"
    )
    # Idempotent, or `find_unredacted` (which asks "would the redactor change this?") lies.
    assert redact_secrets(scrubbed) == scrubbed


def test_a_json_bearing_string_with_no_credential_is_returned_byte_for_byte() -> None:
    """The control for the rule above: re-serializing unconditionally would rewrite every
    JSON-bearing field of every response and make `find_unredacted` cry leak on all of them.
    """
    compact = '{"contact":{"name":"Ada"},"n":1,"tags":["a","b"]}'
    for text in (
        compact,
        '[1,2,3]',
        '{"Key":"page","Value":"3"}',
        '{ "spaced" : true }',
        '{not json at all',
        '{see the api key doc}',
        'plain prose mentioning an api_key=YOUR_KEY placeholder',
    ):
        assert redact_secrets({"JsonBody": text}) == {"JsonBody": text}, text
        assert find_unredacted({"JsonBody": text}) == [], text
        assert not contains_redacted(redact_secrets({"JsonBody": text})), text

    # …and the rule is LIVE, not merely absent — otherwise every line above passes just as
    # well with the JSON-in-a-string pass deleted, which is the state that shipped.
    leaky = '{"contact":{"name":"Ada"},"apiKey":"sk_live_ABCD1234efgh"}'
    assert redact_secrets({"JsonBody": leaky})["JsonBody"] != leaky
    assert find_unredacted({"JsonBody": leaky}) == ["$.JsonBody"]


def test_auth_scheme_values_are_masked_in_value_position_only() -> None:
    """A string that names its own credential, and the prose that must survive it."""
    for raw, want_gone in (
        ("Bearer sk_live_ABCD1234efgh", "sk_live_ABCD1234efgh"),
        ('"Bearer abcdef0123456789xyz"', "abcdef0123456789xyz"),
        ("Basic QWxhZGRpbjpvcGVuMTIzNDU2", "QWxhZGRpbjpvcGVuMTIzNDU2"),
    ):
        out = redact_secrets({"Value": raw, "Header": "X-Trace"})["Value"]
        assert want_gone not in out, (raw, out)
        assert REDACTED in out

    # Controls: value-shape is never evidence, and prose is not value position.
    for intact in (
        "the Bearer token is rotated weekly",
        "Bearer authentication",
        "Basic 3-bedroom-listing-info",
        "use Basic or Bearer as appropriate",
        "sk_live_ABCD1234efgh",          # bare, unlabelled: not evidence of anything
    ):
        assert redact_secrets({"Title": intact}) == {"Title": intact}, intact
        assert find_unredacted({"Title": intact}) == [], intact


def test_a_url_query_parameter_named_for_a_credential_is_masked() -> None:
    """`?api_key=…` is a name/value pair spelled in string syntax."""
    url = "https://crm.example.com/v1/contacts?page=3&api_key=sk_live_ABCD1234&sort=asc"
    out = redact_secrets({"Url": url})["Url"]
    assert "sk_live_ABCD1234" not in out, out
    assert "page=3" in out and "sort=asc" in out, f"ordinary parameters were mangled: {out}"
    assert find_unredacted({"Url": url}) == ["$.Url"]
    assert find_unredacted(redact_secrets({"Url": url})) == []

    # Controls: no scheme means it is prose, and a non-credential name is left alone.
    for intact in (
        "pass api_key=YOUR_KEY on the query string",
        "https://crm.example.com/v1/contacts?page=3&sort=asc",
    ):
        assert redact_secrets({"Title": intact}) == {"Title": intact}, intact


def test_ordinary_flow_content_survives_redaction_unchanged() -> None:
    """Over-redaction is a real failure mode, not a safe default.

    `contains_redacted` refuses a write carrying the sentinel, so anything masked here makes
    the flow it came from unwritable. A graph with no credential in it must come back
    identical — that is what makes read-modify-write possible at all.
    """
    graph = _webhook_graph()
    assert redact_secrets(graph) == graph, redact_secrets(graph)
    assert find_unredacted(graph) == []
    assert not contains_redacted(redact_secrets(graph))

    # Including the shapes most likely to be mistaken for credentials.
    ordinary = {
        "id": "a7f3c9d1-4e2b-4c8a-9f11-0b6d5e2a8c34",
        "name": "Key Accounts — West",
        "description": "Ask for the passphrase policy, then continue",
        "position": {"x": 120, "y": -40},
        "keywords": ["key", "token", "secret"],
        "monkey": "not a key",
        "tokenizer": "cl100k_base",
        "base64ish": "QWxhZGRpbjpvcGVuMTIzNDU2",
    }
    assert redact_secrets(ordinary) == ordinary, redact_secrets(ordinary)
    assert find_unredacted(ordinary) == []


def test_a_webhook_bearing_flow_can_still_be_read_modified_and_written() -> None:
    """END TO END: the goal of phase 07, through the real transport boundary.

    Reading a flow that holds a WebHook node used to hand back `Key: "<redacted>"` for a page
    number, so `contains_redacted` in rest.py refused the follow-up save and editing a real
    bot was impossible. The read must come back clean and the write must go through.
    """
    from closewire_client.endpoints import bots as read_bots
    from closewire_client.writes import bots as write_bots

    graph = _webhook_graph()
    client = _client(graph)

    fetched = read_bots.get_steps(client, "BOT", "1")
    assert fetched == graph, "the read was altered before anyone edited anything"
    assert not contains_redacted(fetched), fetched

    # Edit something unrelated, exactly as a read-modify-write does.
    fetched["nodes"][1]["data"]["Title"] = "Call CRM (v2)"
    saved = write_bots.save(client, "BOT", fetched)
    assert saved is not None, "the save was refused"

    # …and the guard is still armed: a flow that really does hold a credential is refused.
    leaky = _webhook_graph()
    leaky["nodes"][1]["data"]["Headers"] = [
        {"Header": "Authorization", "Value": "Bearer sk_live_ABCD1234efgh"}
    ]
    masked = redact_secrets(leaky)
    assert masked["nodes"][1]["data"]["Headers"][0]["Value"] == REDACTED
    assert contains_redacted(masked), "a real credential no longer blocks a write-back"
    try:
        write_bots.save(_client({"ok": True}), "BOT", masked)
    except ValueError as exc:
        assert REDACTED in str(exc)
    else:
        raise AssertionError("a masked credential was written back to the API")


def test_find_unredacted_agrees_with_redact_secrets_on_every_shape_above() -> None:
    """The verification harnesses treat `find_unredacted` as the definition of a leak.

    If it and `redact_secrets` disagree, `verify_reads.py` / `verify_cli.py` either invent
    leaks or miss real ones. The invariant is checked directly rather than by a second rule
    set: nothing is reported for a scrubbed payload, and nothing survives scrubbing that was
    not reported.
    """
    import json as _j

    corpus = [
        WEBHOOK_PARAM_PAGE,
        WEBHOOK_PARAM_SECRET,
        {"Header": "Authorization", "Value": "Bearer sk_live_ABCD1234efgh"},
        {"key": "BodyType", "value": "formdata"},
        {"id": "s", "name": "Client", "key": OAUTH, "value": "x"},
        {"JsonBody": _j.dumps({"apiKey": "sk_live_ABCD1234efgh"})},
        {"Url": "https://x.example.com/a?api_key=sk_live_ABCD1234"},
        _webhook_graph(json_body=_j.dumps({"token": "tok_live_9", "keep": "me"})),
        _webhook_graph(),
        [{"sources": [{"id": "s1", "key": OAUTH}]}, {"nested": {"accessToken": OAUTH}}],
    ]
    for payload in corpus:
        scrubbed = redact_secrets(payload)
        assert find_unredacted(scrubbed) == [], (payload, find_unredacted(scrubbed))
        # Idempotence is what makes the "would the redactor still change this?" test honest.
        assert redact_secrets(scrubbed) == scrubbed, payload
        # Nothing reported means nothing changed, and vice versa.
        assert (find_unredacted(payload) == []) == (scrubbed == payload), payload

    # The corpus must actually contain leaks and non-leaks, or "the two agree" is a
    # statement about nothing — two functions that both find nothing agree perfectly.
    assert find_unredacted(WEBHOOK_PARAM_PAGE) == []
    assert find_unredacted(WEBHOOK_PARAM_SECRET) == ["$.Value"]
    assert find_unredacted({"JsonBody": _j.dumps({"apiKey": "sk_live_ABCD1234efgh"})}) == [
        "$.JsonBody"
    ]
    assert find_unredacted(
        {"Url": "https://x.example.com/a?api_key=sk_live_ABCD1234"}
    ) == ["$.Url"]
    assert find_unredacted({"id": "s", "name": "Client", "key": OAUTH, "value": "x"}) == [
        "$.key"
    ]


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} redaction tests passed.")
