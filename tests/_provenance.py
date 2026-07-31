"""The wire-level credential-provenance check, as a plain context manager.

Importable, so it works from **both** execution paths this repo promises: pytest (via the
autouse fixture in `conftest.py`) and `python tests/test_x.py` (by entering it directly).

That split is not decoration. The first version put the hook only in `conftest.py`, which
pytest loads and the direct runner does not — so `python tests/test_auth_provenance.py`
reported all eight laundering shapes escaping while `pytest` reported them caught. Two
execution paths disagreeing about whether a security property holds is worse than either
answer. `scripts/verify_runners.py` caught it within a minute of being written, which is the
whole argument for that check existing.

See `closewire_client.auth.ISSUED` for why the assertion is at the wire rather than in a
static scan.

**The exact property, and its limit.** `ISSUED` records `(header name, value digest)` pairs
and only grows within a process, so what is actually asserted is *"this header **value** was
issued by `auth.py` at some point in this process"* — not *"this particular header was"*. A
laundered `{"X-CB-KEY": key}` is caught cold; the same laundering **passes** if a legitimate
`ApiKeyAuth(key).headers()` call earlier in the same process already banked that value. Two
critics found this independently, and it is stated here rather than papered over.

Why that is still worth having: the defect this exists for — a module hand-rolling its own
header instead of calling `auth.py` — is caught in the shape it actually ships, because a
laundering module's key only enters the book if some *other* code path used the same value
first. Reintroducing the original phase-09 defect in `live.py` was verified to trip it. The
residual gap is a module that launders **after** a legitimate call in the same process, which
`tests/test_auth_provenance.py`'s literal scan still covers from the other side.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

import httpx

from closewire_client.auth import ISSUED, receipt
from closewire_client.redaction import is_secret_name


class AuthProvenanceError(AssertionError):
    """A credential-named header went out that `auth.py` never issued."""


#: Header names a caller may legitimately send that are *not* credentials this client issues.
#: Empty on purpose — an entry here is a hole, and it should have to be argued for.
ALLOWED_FOREIGN: frozenset[str] = frozenset()


@contextlib.contextmanager
def asserting_auth_provenance() -> Iterator[None]:
    """Refuse any outgoing credential header `ApiKeyAuth` did not issue.

    Hooks `httpx.Client.send`, which every request passes through regardless of transport —
    so it needs no network, sees `MockTransport` traffic, and covers both `Session` and
    `LiveMessageClient` without knowing either exists.

    `is_secret_name` is reused rather than re-listing header names, so the vocabulary lives
    in exactly one place: it already folds case and separators and already knows `x-cb-key`,
    `authorization`, `x-api-key` and `x-auth-token`.
    """
    original = httpx.Client.send

    def send(self: httpx.Client, request: "httpx.Request", **kwargs: Any) -> "httpx.Response":
        for name, value in request.headers.items():
            if not is_secret_name(name) or name.lower() in ALLOWED_FOREIGN:
                continue
            if receipt(name, value) not in ISSUED:
                raise AuthProvenanceError(
                    f"{request.method} {request.url}: header {name!r} carries a credential "
                    "that closewire_client.auth never issued. Every credential header must "
                    "come from ApiKeyAuth(...).headers() — see tests/_provenance.py."
                )
        return original(self, request, **kwargs)

    httpx.Client.send = send  # type: ignore[method-assign]
    try:
        yield
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]
