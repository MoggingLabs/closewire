"""Tier-0 reads for personas — the curated surface over generated :mod:`.persona`.

Hand-written; ``scripts/codegen.py`` will not overwrite it. **Read-only.**

``GET /persona`` is not paginated — it returns a bare list. Each persona carries
``id``, ``personaName``, ``description``, ``voiceStyles``, ``howToRespond``,
``typoPercent``, ``responseTime``/``responseDelay``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from closewire_client.endpoints import persona as _gen
from closewire_client.endpoints._reads import as_list

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.rest import RestClient

__all__ = ["list_personas", "get"]


def list_personas(client: "RestClient") -> list[dict[str, Any]]:
    """Every persona on the account."""
    return as_list(_gen.get_persona(client))


def get(client: "RestClient", persona_id: str) -> dict[str, Any]:
    """One persona by id."""
    return _gen.get_persona_id(client, persona_id)
