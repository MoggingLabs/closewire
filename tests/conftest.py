"""Test-wide guarantees that cannot be expressed as a static check.

Right now that means one: **every credential header leaving this process was issued by
`closewire_client.auth`.** The check itself lives in `tests/_provenance.py` as a plain context
manager, so the direct runner (`python tests/test_x.py`) can enter it too — this file only
makes it automatic under pytest.

Why the check exists at the wire rather than in a scan: `tests/test_auth_provenance.py`
asserts the same intent by looking for string literals, and that cannot close the class. Six
laundering shapes produce a working header name with no literal at all, and `AUTH_STYLES[0]`
*is* the string `"x-cb-key"` spelled with a name `auth.py` exports — unflaggable by any
literal rule that does not also flag every legitimate use of the tuple. See
`closewire_client.auth.ISSUED`.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from tests._provenance import asserting_auth_provenance


@pytest.fixture(autouse=True)
def _assert_auth_provenance() -> Iterator[None]:
    """Every test runs under the wire assertion — *unless it replaces `Client.send` itself*.

    That caveat is real and was found by two critics: `tests/test_probe_scripts.py` swaps
    `httpx.Client.send` wholesale to stub the network, which drops this hook for exactly the
    five tests driving the one script that can reach a metered endpoint. It now re-asserts the
    check inside its own stub. Any future stub that replaces `Client.send` must do the same —
    a stub that silently drops the hook it displaced is how a suite-wide guarantee acquires a
    hole nobody can see.

    See `tests/_provenance.py` for the exact property and its limit.
    """
    with asserting_auth_provenance():
        yield
