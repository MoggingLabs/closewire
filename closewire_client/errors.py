"""Typed errors for Closewire's HTTP client.

These never carry the raw API key: callers scrub error bodies with
:meth:`~closewire_client.config.Config.scrub` before constructing an exception.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ClosewireError",
    "ClosewireTransportError",
    "ClosebotAPIError",
    "SecretsNotPermittedError",
    "RedactedValueError",
]

_MAX_DETAIL = 600


def _stringify(body: Any) -> str:
    if body is None:
        return "(no body)"
    if isinstance(body, str):
        text = body
    else:
        try:
            text = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(body)
    text = text.strip()
    return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL] + "…(truncated)"


class ClosewireError(Exception):
    """Base class for all Closewire client errors."""


class SecretsNotPermittedError(ClosewireError, PermissionError):
    """A caller asked for unmasked credentials without the client capability.

    Subclasses :class:`ClosewireError` so the CLI's handler catches it, and
    ``PermissionError`` so ordinary Python handling still applies.
    """


class RedactedValueError(ClosewireError, ValueError):
    """A write body carried the redaction sentinel and was refused before sending."""


class ClosewireTransportError(ClosewireError):
    """A network/transport failure before any HTTP status was received."""


class ClosebotAPIError(ClosewireError):
    """Closebot returned a non-2xx response.

    All attributes are expected to be key-redacted by the caller.

    Attributes:
        status_code: The HTTP status code.
        method: The HTTP method of the failing request.
        path: The request path (base URL omitted).
        body: The parsed JSON error body, or the raw text, or ``None``.
    """

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        *,
        body: Any = None,
        message: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        self.body = body
        detail = message or _stringify(body)
        super().__init__(f"Closebot API {method} {path} -> HTTP {status_code}: {detail}")
