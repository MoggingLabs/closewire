"""Fields the API requires that the vendored spec does not mark required.

**Why this file exists.** Closebot's OpenAPI document is unreliable about required-ness. It
declares no ``required`` array on its input schemas at all, and marks fields ``nullable:
true`` that the server rejects a body for omitting. Twice now that has shipped as a bug found
only by a live call:

* ``POST /persona`` → ``aiProviderPreferences``, discovered as a 400 naming it;
* ``POST /bot/ai`` → ``name``, same, ``"missing required properties including: 'name'"``.

Both were *local* mistakes with a *remote* diagnosis: the client happily built a body the
server was always going to refuse, so the cost of learning was a paced round-trip. The
knowledge cannot be derived from ``schema/`` — that is the whole point — so it has to live
somewhere deliberate, and be enforced rather than merely written down.

**The gate.** :data:`REQUIRED_IN_PRACTICE` names, per function, the fields that must be
impossible to omit. ``tests/test_required_fields.py`` asserts, for every entry, that the
function *cannot* be called without it — the parameter is positional-or-required in the
signature, so omitting it is a ``TypeError`` before any budget is spent. Adding a row here
without tightening the signature fails the suite; tightening a signature without a row here
is fine but undocumented. A new create-style function that hits this class again gets one row
and one test, instead of one more live 400.

This registry is **evidence, not inference**. Every row cites the live response that
established it. Nothing goes in on the grounds that it "looks required".
"""

from __future__ import annotations

__all__ = ["REQUIRED_IN_PRACTICE", "EVIDENCE"]

#: ``"module.function"`` → the parameter names a caller must supply.
#:
#: These are *parameter* names, not wire field names, because what the gate checks is that
#: the Python signature makes omission impossible.
REQUIRED_IN_PRACTICE: dict[str, tuple[str, ...]] = {
    "personas.create": ("name",),
    "bots.create_with_ai": ("description", "name"),
    "bots.attach_source": ("channels",),
}

#: What proved each row. Kept beside the data so a future reader can tell a verified
#: requirement from a guess, and re-run the probe if the API changes.
EVIDENCE: dict[str, str] = {
    "personas.create": (
        "POST /persona without aiProviderPreferences -> 400: "
        "\"JSON deserialization ... was missing required properties including: "
        "'aiProviderPreferences'.\" The spec declares no required array and marks the field "
        "nullable. "
        "NOTE, and a limit of this gate that a critic caught: the registered *parameter* is "
        "`name`, but the live 400 above proves `aiProviderPreferences`. They are not the "
        "same thing. `aiProviderPreferences` cannot be gated the same way because "
        "`create` guarantees it by always sending DEFAULT_AI_PROVIDERS rather than by "
        "demanding it from the caller — a defaulted keyword, which the signature check is "
        "designed to reject. So the wire field is protected by construction and the "
        "signature check protects `name` instead; if a future edit stopped `create` sending "
        "aiProviderPreferences, THIS GATE WOULD NOT NOTICE. That gap is covered instead by "
        "tests/test_required_fields.py::test_supplying_the_required_fields_works, which "
        "asserts the key is on the wire. `name` itself has no live-400 citation: it is "
        "required here because a persona with no name is not something a caller means to "
        "make, which is a design choice, not an API finding."
    ),
    "bots.create_with_ai": (
        "POST /bot/ai with only {description} -> 400: \"... was missing required properties "
        "including: 'name'.\" plus \"The input field is required.\" The spec marks name "
        "nullable: true and declares no required array. `name` is positional as a result."
    ),
    "bots.attach_source": (
        "POST /bot/{id}/source/{sourceId} with no body -> 415 Unsupported Media Type (no "
        "Content-Type is emitted for json=None); with {} -> 400: \"... was missing required "
        "properties including: 'channels'.\" `{\"channels\": []}` succeeds and the source "
        "reads back attached. All four fields are nullable in the spec with no required "
        "array. Third instance of this class, which is why the registry exists."
    ),
}
