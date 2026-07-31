"""Tier-1 persona mutations: create and update.

A persona is the voice layer — tone, typo rate, response delay — applied to a bot's replies.
Both operations are ordinary paced writes, suppressed by ``CLOSEWIRE_DRY_RUN``.
"""

from __future__ import annotations

from typing import Any

from ..rest import RestClient
from ._tier import reject_tier2_fields

__all__ = ["create", "update", "CREATE_FIELDS", "UPDATE_FIELDS", "DEFAULT_AI_PROVIDERS"]

#: Used when a caller expresses no preference. Matches what the live account's existing
#: personas carry, so a created persona behaves like the ones already there.
DEFAULT_AI_PROVIDERS: tuple[str, ...] = ("anthropic", "openai")

#: Fields ``POST /persona`` accepts, per ``CreatePersonaInput``.
CREATE_FIELDS: frozenset[str] = frozenset(
    {
        "personaName",
        "description",
        "voiceStyles",
        "howToRespond",
        "typoPercent",
        "breakupLargeMessagePercent",
        "responseTime",
        "responseDelay",
        "aiProviderPreferences",
        "color",
        "imageData",
    }
)

#: Fields ``PUT /persona/{id}`` accepts — the create set plus organisation and state flags.
UPDATE_FIELDS: frozenset[str] = CREATE_FIELDS | {
    "folderId",
    "favorited",
    "trash",
    "default",
}


def create(
    client: RestClient,
    name: str,
    *,
    ai_provider_preferences: list[str] | None = None,
    **fields: Any,
) -> Any:
    """Create a persona. ``POST /persona``

    ``name`` maps to the API's ``personaName``; the rest of ``CreatePersonaInput`` is passed
    through. Unknown keys raise rather than being dropped silently — a typo'd
    ``typo_percent`` that vanished would look like the API ignored a valid setting.

    ``ai_provider_preferences`` is an ordered list of model providers, e.g.
    ``["anthropic", "openai"]``. **The API requires it**, even though the vendored spec
    marks no field on ``CreatePersonaInput`` as required — omitting it returns a 400 naming
    it explicitly (observed live). It is a named parameter here rather than one more
    ``**fields`` key so the requirement is visible at the call site instead of being
    discovered from a deserialization error; :data:`DEFAULT_AI_PROVIDERS` fills it in when
    the caller has no preference.
    """
    body: dict[str, Any] = {
        "personaName": name,
        "aiProviderPreferences": list(
            ai_provider_preferences
            if ai_provider_preferences is not None
            else DEFAULT_AI_PROVIDERS
        ),
        **fields,
    }
    unknown = sorted(set(body) - CREATE_FIELDS)
    if unknown:
        raise ValueError(
            f"create(): unknown field(s) {unknown} — CreatePersonaInput accepts "
            f"{sorted(CREATE_FIELDS)}"
        )
    return client.request("POST", "/persona", json=body)


def update(client: RestClient, persona_id: str, **fields: Any) -> Any:
    """Update a persona. ``PUT /persona/{id}``

    Partial by construction: only the fields passed are sent. ``trash=True`` is refused —
    see :mod:`closewire_client.writes._tier`.
    """
    if not fields:
        raise ValueError("update() needs at least one field to change")
    unknown = sorted(set(fields) - UPDATE_FIELDS)
    if unknown:
        raise ValueError(
            f"update(): unknown field(s) {unknown} — UpdatePersonaInput accepts "
            f"{sorted(UPDATE_FIELDS)}"
        )
    reject_tier2_fields(fields, operation="personas.update")
    return client.request("PUT", f"/persona/{persona_id}", json=dict(fields))
