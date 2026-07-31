"""Authentication for the Closebot REST API.

Per ``RESEARCH.md``, ``X-CB-KEY: <key>`` is canonical for ``api.closebot.com``, but the
community has shipped other conventions (``Authorization: Key ...``,
``Authorization: Bearer ...``). Closewire keeps the header form **configurable** via
``CLOSEWIRE_AUTH_STYLE`` so alternate deployments work without code changes:

* ``x-cb-key`` (default)      -> ``X-CB-KEY: <key>``
* ``authorization-key``       -> ``Authorization: Key <key>``
* ``authorization-bearer``    -> ``Authorization: Bearer <key>``

The key is held only in memory and is never logged (``__repr__`` redacts it).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.config import Config

__all__ = ["ApiKeyAuth", "AUTH_STYLES", "DEFAULT_AUTH_STYLE", "ISSUED", "receipt"]

DEFAULT_AUTH_STYLE = "x-cb-key"
AUTH_STYLES = ("x-cb-key", "authorization-key", "authorization-bearer")

#: Receipts for every credential header this module has issued, as ``(lower name, digest)``.
#:
#: **Why a receipt book exists.** ``tests/test_auth_provenance.py`` asserts that only this
#: module spells a credential header, by scanning for string literals. That scan cannot close
#: the class and a critic proved it: ``b"X-CB-KEY"``, ``"X-CB" + "-KEY"``, ``"".join([...])``,
#: ``"%s-CB-KEY" % "X"``, ``"{}-CB-KEY".format("X")`` and ``chr(88) + "-CB-KEY"`` all evaluate
#: to a working header name and none is a literal. Worst of all, ``AUTH_STYLES[0]`` *is* the
#: string ``"x-cb-key"`` — spelled with a name this very module exports — so no literal rule
#: can flag it without flagging every legitimate use of the tuple. Deciding "does this
#: expression evaluate to a header name" is undecidable in general and an arms race in
#: practice.
#:
#: The property is about **bytes leaving the process**, so it is asserted there. A test
#: fixture hooks ``httpx.Client.send`` and refuses any credential-named header whose value
#: this module did not issue. That works for both surfaces, needs no network (``MockTransport``
#: goes through the same call), and costs ~11 µs — four orders of magnitude under the pacer's
#: think-time floor.
#:
#: A **digest**, never the value: this registry must not become a second in-memory copy of the
#: key, and a hash stays printable in a failure message.
ISSUED: set[tuple[str, str]] = set()


def receipt(name: str, value: str) -> tuple[str, str]:
    """The ``(header name, value digest)`` pair recorded in :data:`ISSUED`."""
    return (
        name.strip().lower(),
        hashlib.blake2b(value.encode("utf-8"), digest_size=16).hexdigest(),
    )


class ApiKeyAuth:
    """API-key authentication whose header form is selected by ``style``.

    Args:
        api_key: The raw Closebot API key. Held only in memory; never logged.
        style: One of :data:`AUTH_STYLES`.
    """

    def __init__(self, api_key: str, style: str = DEFAULT_AUTH_STYLE) -> None:
        if style not in AUTH_STYLES:
            raise ValueError(f"unknown auth style {style!r}; expected one of {AUTH_STYLES}")
        self._api_key = api_key
        self.style = style

    @classmethod
    def from_config(cls, config: "Config") -> "ApiKeyAuth":
        """Build an :class:`ApiKeyAuth` from a loaded :class:`Config`."""
        return cls(config.api_key, getattr(config, "auth_style", DEFAULT_AUTH_STYLE))

    def headers(self) -> dict[str, str]:
        """Return the auth headers to merge into every REST request.

        Returns an empty dict when no key is set (so callers fail with the API's own
        auth error rather than sending a malformed header).

        Every header returned is also recorded in :data:`ISSUED` — see it for why.
        """
        if not self._api_key:
            return {}
        if self.style == "x-cb-key":
            built = {"X-CB-KEY": self._api_key}
        elif self.style == "authorization-key":
            built = {"Authorization": f"Key {self._api_key}"}
        elif self.style == "authorization-bearer":
            built = {"Authorization": f"Bearer {self._api_key}"}
        else:  # unreachable: style validated in __init__
            return {}
        ISSUED.update(receipt(name, value) for name, value in built.items())
        return built

    @property
    def header_name(self) -> str:
        """The HTTP header name this style writes to."""
        return "X-CB-KEY" if self.style == "x-cb-key" else "Authorization"

    def __repr__(self) -> str:  # never expose the key
        return f"{type(self).__name__}(style={self.style!r}, api_key=<redacted>)"
