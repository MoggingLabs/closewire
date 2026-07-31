"""The gate for "the spec understates required fields".

This class has bitten twice — `personas.create`'s `aiProviderPreferences` and
`bots.create_with_ai`'s `name` — each time as a live 400 after the body had already been
built and a paced round-trip spent. Neither is derivable from `schema/`, because the vendored
document declares no `required` arrays at all.

So the gate is not "does the spec say required" (it never does). It is: **for every field we
have *proven* the API requires, is it impossible to call the function without it?** If the
signature is loosened, or a new create-style function is added and registered without being
tightened, this fails.

It also fails in the other direction: a registry row naming a parameter the function does not
have is a stale row, and stale safety data is worse than none.
"""

from __future__ import annotations

import inspect
from typing import Any

from closewire_client.writes import bots, personas
from closewire_client.writes._required import EVIDENCE, REQUIRED_IN_PRACTICE

_MODULES: dict[str, Any] = {"bots": bots, "personas": personas}


def _resolve(dotted: str):
    module_name, function_name = dotted.split(".", 1)
    module = _MODULES[module_name]
    return getattr(module, function_name)


class _Recorder:
    """Stands in for RestClient. Records instead of sending, so a leak through the gate
    shows up as a recorded call rather than as a real request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, json: Any = None, **_: Any) -> Any:
        self.calls.append((method, path, json))
        return {"ok": True}


def test_every_registered_field_is_impossible_to_omit() -> None:
    """The gate. Omitting a proven-required field must fail *locally*, before any send."""
    for dotted, fields in REQUIRED_IN_PRACTICE.items():
        function = _resolve(dotted)
        parameters = inspect.signature(function).parameters
        for field in fields:
            assert field in parameters, f"{dotted}: registry names unknown parameter {field!r}"
            parameter = parameters[field]
            assert parameter.default is inspect.Parameter.empty, (
                f"{dotted}: {field!r} has a default, so a caller can omit it and the API "
                f"will answer 400. It is required in practice — see EVIDENCE[{dotted!r}]."
            )


def test_omitting_a_required_field_raises_before_anything_is_sent() -> None:
    """Behavioural half: a missing field is a TypeError, not a paced round-trip."""
    client = _Recorder()
    cases = [
        (personas.create, (client,)),                    # `name` omitted
        (bots.create_with_ai, (client, "a description")),  # `name` omitted
    ]
    for function, args in cases:
        try:
            function(*args)
            raise AssertionError(f"{function.__name__} accepted a call missing a required field")
        except TypeError:
            pass
    assert client.calls == [], f"a call missing a required field reached the client: {client.calls}"


def test_supplying_the_required_fields_works() -> None:
    """The control. A gate that refused everything would pass the two tests above."""
    client = _Recorder()
    personas.create(client, "zz-name")
    bots.create_with_ai(client, "a description", "zz-name")
    assert [call[0] for call in client.calls] == ["POST", "POST"]
    # And the proven-required wire fields are actually on the wire.
    persona_body, ai_body = client.calls[0][2], client.calls[1][2]
    assert "aiProviderPreferences" in persona_body, persona_body
    assert ai_body.get("name") == "zz-name", ai_body
    assert ai_body.get("description") == "a description", ai_body


def test_every_registry_row_cites_its_evidence() -> None:
    """A row with no evidence is a guess, and guesses are how this class started."""
    assert set(EVIDENCE) == set(REQUIRED_IN_PRACTICE), (
        "every registered function must record the live response that proved it"
    )
    for dotted, text in EVIDENCE.items():
        assert "400" in text, f"{dotted}: evidence does not cite a live response"


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} required-field gate tests passed.")
