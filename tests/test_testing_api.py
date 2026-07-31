"""Bot Testing API tests — deliverable 2, and the shape normaliser.

These exist because a critic pointed out that phase 09's log called deliverable 2
"unit-proven" while **nothing under `tests/` referenced it at all**: all 34 new tests covered
the runtime client. `send` is the one credit-spending call in the module, and a wrong route
or body would have shipped with nothing able to catch it.

Two things get pinned hardest:

* **routes and bodies against the vendored spec**, not against the code — a test that reads
  the same constant the code uses cannot detect a wrong value;
* **`sessions_of`**, because the live API returns two different shapes for one declared
  response type, and the single-shape assumption it replaced was a real bug.

Nothing here touches the network: a recorder stub stands in for `RestClient`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from closewire_client.writes import testing as t

#: The vendored REST spec — the oracle for routes and request bodies.
SPEC = json.loads(
    (Path(__file__).resolve().parents[1] / "schema" / "openapi.json").read_text(
        encoding="utf-8"
    )
)

BOT = "bot_zzTEST"
LEAD = "lead_zzTEST"


class _Recorder:
    """Stands in for RestClient; records the call instead of sending it."""

    def __init__(self, returns: Any = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._returns = returns if returns is not None else {"ok": True}

    def request(self, method: str, path: str, *, json: Any = None, **_: Any) -> Any:
        self.calls.append((method, path, json))
        return self._returns


def _spec_body_fields(path: str, method: str) -> set[str]:
    """The declared request-body fields for a route, resolved through its ``$ref``."""
    operation = SPEC["paths"][path][method]
    blob = json.dumps(operation.get("requestBody", {}))
    if "#/components/schemas/" not in blob:
        return set()
    ref = blob.split("#/components/schemas/")[-1].split('"')[0]
    return set(SPEC["components"]["schemas"][ref].get("properties") or {})


# ── Routes: every operation hits the route the spec declares ──────────────────
def test_every_operation_hits_its_declared_route() -> None:
    """The routes come from the spec, so a wrong path in the code fails here."""
    cases: list[tuple[Any, tuple[Any, ...], str, str]] = [
        (t.create_session, (BOT,), "POST", "/bot/{botId}/testSession"),
        (t.list_sessions, (BOT,), "GET", "/bot/{botId}/testSession"),
        (t.get_messages, (BOT, LEAD), "GET", "/bot/{botId}/testSession/messages/{leadId}"),
        (t.send, (BOT, LEAD, "hi"), "POST", "/bot/{botId}/testSession/message"),
        (t.force_step, (BOT, LEAD), "POST", "/bot/{botId}/testSession/{leadId}/force-step"),
        (t.rollback, (BOT, LEAD, "msg_1"), "POST",
         "/bot/{botId}/testSession/{leadId}/rollback"),
        (t.delete_session, (BOT, LEAD), "DELETE", "/bot/{botId}/testSession/{leadId}"),
    ]
    for fn, args, method, template in cases:
        assert template in SPEC["paths"], f"{template} is not in the spec"
        assert method.lower() in SPEC["paths"][template], f"{method} {template} not declared"
        client = _Recorder()
        fn(client, *args)
        sent_method, sent_path, _ = client.calls[0]
        expected = template.replace("{botId}", BOT).replace("{leadId}", LEAD)
        assert (sent_method, sent_path) == (method, expected), f"{fn.__name__}"


def test_update_session_hits_its_declared_route() -> None:
    client = _Recorder()
    t.update_session(client, BOT, LEAD, mimicSourceId="src_1")
    assert client.calls[0][:2] == ("PUT", f"/bot/{BOT}/testSession/{LEAD}")


def test_send_carries_the_lead_id_in_the_body_not_the_path() -> None:
    """The API's own asymmetry — every other per-session call puts it in the path."""
    client = _Recorder()
    t.send(client, BOT, LEAD, "hello")
    method, path, body = client.calls[0]
    assert LEAD not in path, f"the lead id leaked into the path: {path}"
    assert body == {"leadId": LEAD, "message": "hello"}


# ── Bodies: match the spec, which sets additionalProperties: false ────────────
def test_the_declared_field_sets_match_the_spec() -> None:
    """Read from the spec, not from the module's own constants."""
    assert set(t.MESSAGE_FIELDS) == _spec_body_fields(
        "/bot/{botId}/testSession/message", "post"
    )
    assert set(t.UPDATE_FIELDS) == _spec_body_fields(
        "/bot/{botId}/testSession/{leadId}", "put"
    )
    assert set(t.ROLLBACK_FIELDS) == _spec_body_fields(
        "/bot/{botId}/testSession/{leadId}/rollback", "post"
    )


def test_sent_bodies_carry_only_declared_keys() -> None:
    """`additionalProperties: false` means an extra key is a protocol error."""
    client = _Recorder()
    t.send(client, BOT, LEAD, "hi")
    t.rollback(client, BOT, LEAD, "msg_1")
    t.update_session(client, BOT, LEAD, mimicSourceId="src_1")
    for (_, _, body), allowed in zip(
        client.calls, (t.MESSAGE_FIELDS, t.ROLLBACK_FIELDS, t.UPDATE_FIELDS)
    ):
        assert set(body) <= set(allowed), body


def test_the_bodyless_operations_send_no_body() -> None:
    client = _Recorder()
    t.create_session(client, BOT)
    t.force_step(client, BOT, LEAD)
    t.delete_session(client, BOT, LEAD)
    assert [body for _, _, body in client.calls] == [None, None, None]


def test_update_session_refuses_unknown_and_empty() -> None:
    client = _Recorder()
    for kwargs in ({}, {"nonsense": 1}, {"mimicSourceId": "x", "extra": 2}):
        try:
            t.update_session(client, BOT, LEAD, **kwargs)
            raise AssertionError(f"{kwargs} was accepted")
        except ValueError:
            pass
    assert client.calls == [], "a refused update sent something"


# ── sessions_of: the live two-shape bug ──────────────────────────────────────
def test_sessions_of_normalises_the_two_live_shapes() -> None:
    """Observed live: a bare array on one bot, `{"leads": [...], "total": N}` on others."""
    assert t.sessions_of([]) == []
    assert t.sessions_of([{"leadId": "a"}]) == [{"leadId": "a"}]
    assert t.sessions_of({"leads": [], "total": 0}) == []
    rows = [{"leadId": "a"}, {"leadId": "b"}]
    assert t.sessions_of({"leads": rows, "total": 2}) == rows


def test_sessions_of_survives_shapes_it_has_never_seen() -> None:
    """A QA helper that raises on an unfamiliar payload is worse than one that returns none."""
    for payload in (None, {}, "", 0, {"total": 3}, {"leads": "not a list"}, 42):
        assert t.sessions_of(payload) == [], payload


def test_sessions_of_cannot_make_a_call() -> None:
    """It is a pure function over an already-fetched payload — no client, no call.

    An earlier version of this test built a ``_Recorder``, never passed it to
    ``sessions_of``, and asserted the recorder saw nothing. **That could not fail** — no
    change to ``sessions_of`` could have made a call through a client it was never given.
    A critic caught it.

    The property is checked at the signature instead: the function takes exactly one
    parameter and it is not a client, so it has nothing to call *with*. That does fail —
    adding a client parameter breaks it, which is the change that would make it cost
    something.
    """
    import inspect

    parameters = list(inspect.signature(t.sessions_of).parameters)
    assert parameters == ["payload"], parameters
    # And it works with no client in existence at all.
    assert t.sessions_of({"leads": [{"leadId": "a"}], "total": 1}) == [{"leadId": "a"}]


# ── listen is an alias, not a second implementation ───────────────────────────
def test_listen_is_get_messages() -> None:
    assert t.listen is t.get_messages


def test_every_brief_operation_is_reachable() -> None:
    """The brief names eight operations; four are spelled out. All must exist."""
    for name in (
        "create_session", "list_sessions", "get_messages", "send",
        "listen", "force_step", "rollback", "delete_session",
    ):
        assert callable(getattr(t, name, None)), name


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} testing-API tests passed.")
