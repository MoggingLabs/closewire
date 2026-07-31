"""Tier-2 confirmation gate tests.

The gate is the only thing standing between a caller and a deleted bot or a real payment,
so it is tested for both failure directions:

* it must **refuse** without a valid, target-matching confirmation;
* it must **allow** with one — a gate that refused unconditionally would pass every refusal
  test while making the whole Tier-2 surface unusable, and nothing else in the suite would
  notice.

Nothing here touches the network: `require_confirm` is pure, and the few client-level tests
use a recording stub.

**Assert on `_guidance(exc)`, never on `str(exc)`.** A refusal is two lines: the first states
what was refused, the second says what to type instead. `ConfirmationRequired.__init__`
interpolates the target into the *first* line, so `assert BOT in str(exc)` is satisfied by the
refusal alone and says nothing about the guidance — a critic deleted the entire second line
and every test here still passed. The guidance is what deliverables 2 and 4 mean by "refuses
and explains", so it is pinned by extracting the token it advises and feeding that token back
to the gate: the message does not merely mention a token, it names one that works.
"""

from __future__ import annotations

import re
from ast import literal_eval
from typing import Any

from closewire_client.tier2 import ConfirmationRequired, describe_intent, require_confirm
from closewire_client.tier2 import billing, bots, leads, personas, sources

BOT = "bot_zzTEST"


class _Recorder:
    """Stands in for RestClient; records the call instead of sending it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, json: Any = None, **_: Any) -> Any:
        self.calls.append((method, path, json))
        return {"ok": True}


def _refused(fn, *args, **kwargs) -> ConfirmationRequired | None:
    try:
        fn(*args, **kwargs)
        return None
    except ConfirmationRequired as exc:
        return exc


def _guidance(exc: ConfirmationRequired) -> str:
    """Everything *after* the first line of a refusal — the copy-pasteable next step.

    Empty when the refusal is one line, which is the failure this exists to catch: the first
    line names the target on its own, so an assertion against the whole message cannot tell a
    refusal that explains itself from one that does not.
    """
    return "\n".join(str(exc).splitlines()[1:])


def _advised_token(exc: ConfirmationRequired) -> Any:
    """The token the guidance tells the operator to pass, as a value.

    Parsed out of `confirm=<repr>` rather than compared as a substring, so the test can hand
    it straight back to the gate. That turns "the message mentions the token" — which the
    action line satisfies by accident — into "the message names a token that is accepted".
    """
    match = re.search(r"confirm=(\S+)\s*$", _guidance(exc))
    assert match, f"the refusal offers no confirm= token to copy: {str(exc)!r}"
    return literal_eval(match.group(1))


# ── require_confirm, directly ─────────────────────────────────────────────────
def test_no_confirmation_refuses() -> None:
    assert _refused(require_confirm, "delete bot", BOT, None) is not None
    assert _refused(require_confirm, "delete bot", BOT, False) is not None


def test_confirm_true_is_refused_for_a_destructive_op() -> None:
    """The dangerous near-miss: reads as confirmed, names no target."""
    exc = _refused(require_confirm, "delete bot", BOT, True)
    assert exc is not None
    assert "not enough" in str(exc)


def test_a_matching_token_is_accepted() -> None:
    assert require_confirm("delete bot", BOT, BOT) is None


def test_a_mismatched_token_is_refused() -> None:
    assert _refused(require_confirm, "delete bot", BOT, "bot_OTHER") is not None


def test_tokens_compare_as_text_so_int_and_str_agree() -> None:
    """A CLI can only hand over a string; Python callers pass an int amount."""
    assert require_confirm("refill", 5, "5") is None
    assert require_confirm("refill", 5, 5) is None
    assert _refused(require_confirm, "refill", 5, "50") is not None


def test_whitespace_around_a_token_does_not_refuse_it() -> None:
    assert require_confirm("delete bot", BOT, f" {BOT}\n") is None


def test_publish_style_accepts_true_but_not_a_truthy_string() -> None:
    assert require_confirm("publish", BOT, True, token_required=False) is None
    assert _refused(require_confirm, "publish", BOT, "yes", token_required=False) is not None
    assert _refused(require_confirm, "publish", BOT, None, token_required=False) is not None


def test_the_refusal_message_names_the_token_that_would_work() -> None:
    """Named literally: the advised token is extracted and then *used*.

    `BOT in str(exc)` was the old assertion and it could not fail — `__init__` puts the target
    in the first line, so the check passed with the whole guidance line deleted. The property
    that matters is a round trip: read what the refusal says to type, type it, be accepted.
    """
    exc = _refused(require_confirm, "delete bot", BOT, None)
    assert exc is not None
    guidance = _guidance(exc)
    assert guidance, f"the refusal lost its guidance line: {str(exc)!r}"
    assert "Nothing was sent" in guidance, guidance
    assert require_confirm("delete bot", BOT, _advised_token(exc)) is None, guidance


def test_every_refusal_shape_carries_a_usable_next_step() -> None:
    """The guidance must survive on *every* refusal path, not just the missing-token one.

    Four reasons produce four different messages; a change that dropped the next step from
    one of them would leave an operator stuck on exactly the path that stopped them.
    """
    for reason, confirm in [
        ("no confirmation", None),
        ("confirm=False", False),
        ("bare confirm=True", True),
        ("mismatched token", "bot_OTHER"),
    ]:
        exc = _refused(require_confirm, "delete bot", BOT, confirm)
        assert exc is not None, reason
        assert "Nothing was sent" in _guidance(exc), f"{reason}: {str(exc)!r}"
        assert require_confirm("delete bot", BOT, _advised_token(exc)) is None, reason

    # Publish now round-trips too. It did not: the message was built from the target and
    # advised `confirm='bot_zzTEST'`, while `token_required=False` accepts only a literal
    # `True` — so following the refusal's own instruction was refused again. The advice is
    # now derived from the same flag that decides what is accepted, so this is the check
    # that stops the two drifting apart again.
    for confirm in (None, False, "yes", 1):
        exc = _refused(require_confirm, "publish", BOT, confirm, token_required=False)
        assert exc is not None, confirm
        assert "Nothing was sent" in _guidance(exc), str(exc)
        advised = _advised_token(exc)
        assert advised is True, f"publish should advise True, got {advised!r}"
        # The round trip: following the advice must be accepted. `require_confirm` returns
        # None on success and raises otherwise, so reaching the next line is the assertion.
        require_confirm("publish", BOT, advised, token_required=False)


# ── Through the operations ────────────────────────────────────────────────────
def test_every_destructive_op_refuses_and_sends_nothing() -> None:
    client = _Recorder()
    cases = [
        (bots.delete, BOT),
        (personas.delete, "pers_x"),
        (sources.delete, "src_x"),
        (leads.delete, "lead_x"),
    ]
    for fn, target in cases:
        assert _refused(fn, client, target) is not None
        assert _refused(fn, client, target, confirm=True) is not None
        assert _refused(fn, client, target, confirm="something-else") is not None
    assert client.calls == [], f"a refused op sent something: {client.calls}"


def test_every_destructive_op_proceeds_with_a_matching_token() -> None:
    """The control. Without this the gate could refuse unconditionally and look correct."""
    client = _Recorder()
    bots.delete(client, BOT, confirm=BOT)
    personas.delete(client, "pers_x", confirm="pers_x")
    sources.delete(client, "src_x", confirm="src_x")
    leads.delete(client, "lead_x", confirm="lead_x")
    bots.publish(client, BOT, confirm=True)
    assert [c[0] for c in client.calls] == ["DELETE", "DELETE", "DELETE", "DELETE", "POST"]


def test_export_needs_no_confirmation_because_it_is_a_read() -> None:
    client = _Recorder()
    bots.export(client, BOT)
    assert client.calls == [("GET", f"/bot/{BOT}/export", None)]


# ── Money ─────────────────────────────────────────────────────────────────────
def test_refill_refuses_without_a_matching_amount() -> None:
    client = _Recorder()
    assert _refused(billing.refill, client, 5) is not None
    assert _refused(billing.refill, client, 5, confirm=True) is not None
    assert _refused(billing.refill, client, 5, confirm=50) is not None
    assert client.calls == []


def test_refill_rejects_amounts_that_are_not_positive_ints() -> None:
    client = _Recorder()
    for bad in (0, -1):
        try:
            billing.refill(client, bad, confirm=bad)
            raise AssertionError(f"{bad} was accepted")
        except ValueError:
            pass
    for bad in (5.0, True, "5"):
        try:
            billing.refill(client, bad, confirm=bad)  # type: ignore[arg-type]
            raise AssertionError(f"{bad!r} was accepted")
        except TypeError:
            pass
    assert client.calls == []


def test_refill_sends_the_declared_body_when_confirmed() -> None:
    """`CreateRefillDto` is {amount, currency}. Recorded, never sent to a real transport."""
    client = _Recorder()
    billing.refill(client, 5, confirm=5)
    assert client.calls == [("POST", "/agency/billing/refill", {"amount": 5, "currency": "usd"})]


def test_billing_reads_need_no_confirmation() -> None:
    client = _Recorder()
    billing.balance(client)
    billing.options(client)
    billing.transactions(client)
    assert [c[0] for c in client.calls] == ["GET", "GET", "GET"]


def test_set_options_is_gated_because_it_arms_future_spending() -> None:
    client = _Recorder()
    exc = _refused(billing.set_options, client, autoRefillEnabled=True)
    assert exc is not None
    assert client.calls == []
    assert "autoRefillEnabled" in str(exc), "the refusal must name the field being armed"
    # The advised token is taken from the refusal and handed straight back, rather than
    # hard-coded. That pins the property — the gate names what it demands, and what it names
    # is accepted — instead of one spelling of the change set, which is an implementation
    # choice `billing.set_options` is free to sharpen.
    billing.set_options(client, autoRefillEnabled=True, confirm=_advised_token(exc))
    assert client.calls[0][0] == "PUT"


def test_set_options_rejects_unknown_fields() -> None:
    client = _Recorder()
    try:
        billing.set_options(client, nonsense=True, confirm="nonsense")
        raise AssertionError("an unknown billing field was accepted")
    except ValueError as exc:
        assert "nonsense" in str(exc)


# ── The safe no-op ────────────────────────────────────────────────────────────
def test_describe_intent_says_nothing_was_sent() -> None:
    text = describe_intent("DELETE bot", BOT, effect="permanent")
    assert BOT in text and "Nothing has been sent" in text


def test_previews_do_not_need_a_client() -> None:
    """The default path must explain itself without opening a connection."""
    for text in (
        bots.preview_delete(BOT),
        bots.preview_publish(BOT),
        personas.preview_delete("pers_x"),
        sources.preview_delete("src_x"),
        leads.preview_delete("lead_x"),
        billing.preview_refill(5),
    ):
        assert "Nothing has been sent" in text


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} tier-2 tests passed.")
