"""Offline transport tests: auth styles, JSON decode, query passthrough, redaction.

Uses ``httpx.MockTransport`` — no network, no real key. Runnable either via pytest
(``pytest tests/test_transport.py``) or directly (``python tests/test_transport.py``),
so the phase-03 plumbing proof holds even before dev deps are installed.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import httpx

from closewire_client.auth import AUTH_STYLES, ApiKeyAuth
from closewire_client.config import Config
from closewire_client.endpoints import agency, bot
from closewire_client.errors import ClosebotAPIError
from closewire_client.pacing import Pacer
from closewire_client.rest import RestClient
from closewire_client.session import Session

SECRET = "cb_SECRET_KEY_do_not_leak_9Z9Z"


class _FakeClock:
    """Coupled clock + sleeper: sleeping advances the clock, so budget waits terminate.

    Do NOT stub the sleeper alone — a sleeper that leaves the clock still makes a budget
    wait unsatisfiable, and the Pacer now raises rather than spinning.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _fast_pacer(cfg: Config) -> Pacer:
    """A real Pacer on a fake clock — pacing is exercised, not skipped."""
    clock = _FakeClock()
    return Pacer(cfg, monotonic=clock.monotonic, sleeper=clock.sleep)


def _handler(captured: list[httpx.Request]):
    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path == "/agency/current":
            return httpx.Response(200, json={"name": "Acme Agency", "id": "ag_123"})
        if path == "/agency/usage":
            return httpx.Response(200, json={"messages": 42})
        if path == "/bot":
            return httpx.Response(200, json=[{"id": "bot_1", "name": "Setter A"}])
        if path == "/leak":
            return httpx.Response(400, text=f"bad key {SECRET} rejected")
        return httpx.Response(404, json={"error": "nope"})

    return handle


def _rest(style: str, captured: list[httpx.Request]) -> RestClient:
    cfg = Config(api_key=SECRET, auth_style=style)
    pacer = _fast_pacer(cfg)  # one Pacer, shared by the transport and the client
    session = Session(
        cfg, ApiKeyAuth.from_config(cfg), pacer, transport=httpx.MockTransport(_handler(captured))
    )
    return RestClient(cfg, session, pacer=pacer)


def test_auth_header_per_style_and_json_decode() -> None:
    expected = {
        "x-cb-key": ("X-CB-KEY", SECRET),
        "authorization-key": ("Authorization", f"Key {SECRET}"),
        "authorization-bearer": ("Authorization", f"Bearer {SECRET}"),
    }
    for style in AUTH_STYLES:
        captured: list[httpx.Request] = []
        with _rest(style, captured) as rest:
            out = agency.get_agency_current(rest)
        header, value = expected[style]
        assert captured[-1].headers.get(header) == value
        if style == "x-cb-key":
            assert "authorization" not in captured[-1].headers
        assert out == {"name": "Acme Agency", "id": "ag_123"}


def test_query_params_passthrough() -> None:
    captured: list[httpx.Request] = []
    with _rest("x-cb-key", captured) as rest:
        agency.get_agency_usage(rest, scopes="billing")
        assert dict(captured[-1].url.params) == {"scopes": "billing"}
        agency.get_agency_usage(rest)  # None dropped, not sent
        assert "scopes" not in captured[-1].url.params


def test_list_endpoint_returns_list() -> None:
    captured: list[httpx.Request] = []
    with _rest("x-cb-key", captured) as rest:
        bots = bot.get_bot(rest)
    assert isinstance(bots, list) and bots[0]["id"] == "bot_1"


def test_non_2xx_raises_typed_error() -> None:
    captured: list[httpx.Request] = []
    with _rest("x-cb-key", captured) as rest:
        try:
            rest.request("GET", "/agency/forbidden")
        except ClosebotAPIError as exc:
            assert exc.status_code == 404
            assert exc.method == "GET" and exc.path == "/agency/forbidden"
        else:
            raise AssertionError("expected ClosebotAPIError")


def test_key_redacted_in_error_body() -> None:
    captured: list[httpx.Request] = []
    with _rest("x-cb-key", captured) as rest:
        try:
            rest.request("GET", "/leak")
        except ClosebotAPIError as exc:
            assert SECRET not in str(exc)
            assert SECRET not in str(exc.body)
            assert "9Z9Z" in str(exc.body)  # redacted suffix hint survives
        else:
            raise AssertionError("expected ClosebotAPIError")


def test_whoami_printer_leaks_no_key() -> None:
    from cli.main import _print_whoami

    cfg = Config(api_key=SECRET, auth_style="x-cb-key")
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_whoami(cfg, {"name": "Acme Agency", "id": "ag_123"}, {"messages": 42},
                      [{"id": "bot_1", "name": "Setter A"}])
    printed = buf.getvalue()
    assert SECRET not in printed
    assert "Acme Agency" in printed and "bot_1" in printed


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} offline transport tests passed.")
