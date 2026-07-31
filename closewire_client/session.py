"""HTTP session/transport shared by the REST and live-message clients.

Owns an ``httpx.Client`` bound to the configured base URL, injects the auth headers from
:mod:`closewire_client.auth` onto every request, and returns the raw ``httpx.Response``.
Status handling, JSON decoding, pacing, and typed errors live one layer up in
:class:`~closewire_client.rest.RestClient` (the single call-site) — this layer is pure
transport so it can be reused by the live-message client too.

Network failures are wrapped in :class:`~closewire_client.errors.ClosewireTransportError`
with the key scrubbed. A custom ``transport`` may be injected (e.g. ``httpx.MockTransport``)
for tests without touching the network.

**This layer enforces the no-bypass guarantee.** A :class:`Pacer` is required, and
:meth:`Session.request` refuses to send unless the calling thread holds a
:meth:`~closewire_client.pacing.Pacer.acquire` slot. Reaching for ``Session`` directly to
"just send one request" raises :class:`~closewire_client.pacing.PacingBypassError` rather
than quietly putting an unpaced, authenticated request on the wire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from closewire_client import __version__
from closewire_client.errors import ClosewireTransportError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from closewire_client.auth import ApiKeyAuth
    from closewire_client.config import Config
    from closewire_client.pacing import Pacer

__all__ = ["Session"]

DEFAULT_TIMEOUT = 30.0


class Session:
    """A thin, authenticated HTTP session over ``httpx``.

    Args:
        config: Loaded configuration (supplies the base URL + key for scrubbing).
        auth: Auth strategy whose headers are merged into every request.
        pacer: The pacing layer. Required — the transport verifies the caller holds one
            of its slots before sending. Omitting it builds a real one from ``config``.
        transport: Optional ``httpx`` transport override (tests inject a MockTransport).
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        config: "Config",
        auth: "ApiKeyAuth",
        pacer: "Pacer | None" = None,
        *,
        transport: "httpx.BaseTransport | None" = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        from closewire_client.pacing import Pacer as _Pacer

        self._config = config
        self._auth = auth
        # Never None: an unpaced Session is the bypass this layer exists to prevent.
        self._pacer = pacer if pacer is not None else _Pacer(config)
        self._client = httpx.Client(
            base_url=config.api_base,
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": f"closewire/{__version__}",
            },
        )

    @property
    def pacer(self) -> "Pacer":
        """The pacing layer this transport checks before sending."""
        return self._pacer

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one authenticated request and return the raw response.

        Does not raise on non-2xx — that is the caller's decision. Raises
        :class:`ClosewireTransportError` only on a network/transport failure, and
        :class:`~closewire_client.pacing.PacingBypassError` if the calling thread is not
        inside a pacing slot.
        """
        self._pacer.assert_in_slot(method, url)
        merged = dict(self._auth.headers())
        if headers:
            merged.update(headers)
        try:
            return self._client.request(method, url, json=json, params=params, headers=merged)
        except httpx.HTTPError as exc:
            raise ClosewireTransportError(
                self._config.scrub(f"{type(exc).__name__}: {exc}")
            ) from exc

    def close(self) -> None:
        """Close the underlying ``httpx`` client."""
        self._client.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
