"""Gate: only `auth.py` may put a credential header on the wire.

Phase 09 shipped `live.py` with its own hand-written `headers["X-CB-KEY"] = config.api_key`,
duplicating logic that `auth.py` had carried since phase 03. The duplicate was not merely
redundant — it was *narrower*. `ApiKeyAuth` supports three header forms; the copy supported
one. So two of the three configured styles, including the `Authorization: Bearer` form
`RESEARCH.md` ties to `api.closebot.ai` specifically, were unreachable on the runtime host,
and ten live 410s were then written up as having exhausted the credential's shapes when only
its *placement* (header vs body) had actually been varied.

The instance is one line. The class is "a module puts a credential on the wire without going
through `auth.py`", and it recurs every time a new surface is added — phase 11's MCP
transport is the next candidate. So the property is asserted structurally, over the AST of
every module in every shipped package, rather than by fixing the one line and hoping.

Two halves, because either alone is escapable:

* **Structural** — outside the three modules named in `_OWNERS`, an auth header name or a
  ``Bearer ``/``Key `` credential prefix may not appear as a string literal *anywhere*, in
  any position, in any scanned package. Catches a *new* surface that never had a test.
* **Behavioural** — both surfaces that authenticate (`LiveMessageClient` and `Session`) must
  produce byte-identical headers to `ApiKeyAuth` for all three styles, compared against
  `auth.py`'s own output rather than a hard-coded expectation table.

The first version of the structural half only looked at assignments whose target *name
contained "header"*, and only at dict keys. A critic broke it three ways in one file:
`h["X-CB-KEY"] = key` (the original defect with one identifier renamed) passed because the
target was not called `headers`; `_HDR = "X-CB-KEY"; headers[_HDR] = key` passed because the
literal had been laundered through a module constant; and a list-of-tuples header form passed
because it was neither an assignment nor a dict. The lesson is that **position is not the
property** — a credential header name has no legitimate reason to be typed anywhere outside
`auth.py`, so the rule is now about the literal alone and does not care where it sits.

That same critic found the scan covered only `closewire_client/`, while `mcp_server/` — the
one future surface the docstring named — is a separate top-level package and was invisible.
Every package in `PACKAGES` is scanned now.

Docstrings are exempt: `endpoints/*.py` are generated with `X-CB-KEY` in their prose, and
documenting a header is not sending one. Comments need no exemption; they are not in the AST.

The structural half also asserts its own patterns are live: `auth.py` *must* match all three.
A rename that quietly makes the rule vacuous fails loudly instead of passing — the round-8
lesson about checks that cannot fail.
"""

from __future__ import annotations

import ast
import atexit
import sys
import shutil
import tempfile
from pathlib import Path
from typing import Any

from closewire_client.auth import AUTH_STYLES, ApiKeyAuth

# The repo root, so `tests._provenance` imports under BOTH execution paths this project
# promises. `pytest` runs from the root; `python tests/test_auth_provenance.py` puts `tests/`
# on `sys.path` instead and would not find the package at all. The two paths disagreeing
# about a security property is exactly what `scripts/verify_runners.py` exists to catch, and
# it caught this.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._provenance import asserting_auth_provenance

#: A fresh temp state dir per run, removed at exit — the same discipline `tests/test_live.py`
#: documents. Without it these tests build a `Pacer` against `.closewire` relative to the
#: CWD, so a breaker latch left by a real run makes them fail with `PacingHalt` instead of an
#: auth verdict, and a future non-200 fixture here would write a latch into the operator's
#: repo. Two critics filed this independently.
_STATE_DIR = tempfile.mkdtemp(prefix="closewire-authprov-")
atexit.register(shutil.rmtree, _STATE_DIR, True)

#: Zeroed pacing. These tests are about which header goes on the wire, not about timing, and
#: on the real clock the six of them cost ~50 s against a ~45 s full suite — a single-purpose
#: gate became the dominant cost of the suite whose speed is what makes the gates get run.
_FAST: dict[str, Any] = {
    "min_delay_s": 0.0,
    "max_delay_s": 0.0,
    "jitter_s": 0.0,
    "backoff_jitter_s": 0.0,
}


def _state_dir() -> str:
    return _STATE_DIR

ROOT = Path(__file__).resolve().parents[1]

#: Every package that ships. `mcp_server` is listed because phase 11 will add an MCP
#: transport there, and a critic pointed out that the one future surface this gate names was
#: the one it structurally could not see.
PACKAGES = ("closewire_client", "cli", "mcp_server", "scripts")

#: Three narrow, named exemptions. Everything else must call `ApiKeyAuth(...).headers()`.
#:
#: * `auth.py` builds the credential headers — it is the module this gate exists to funnel
#:   every other module through.
#: * `redaction.py` names them in order to *scrub* them, which is the opposite of putting one
#:   on the wire: its `SECRET_FIELDS` must contain `authorization` or the redaction layer
#:   stops masking it.
#: * `codegen.py` emits the string into the *docstrings* of generated `endpoints/*.py`
#:   modules. Those docstrings are already exempt where they land, so exempting the generator
#:   is the same rule applied one step earlier; it makes no request and imports no client.
#:
#: `config.py` used to need a fourth exemption because it declared its own copy of
#: `AUTH_STYLES`. It now imports them from `auth.py`, which removed a real second source of
#: truth — the gate found a duplication defect rather than needing to be widened around it.
#:
#: **Repo-relative paths, not basenames.** This was `{"auth.py", "redaction.py", "codegen.py"}`
#: matched against `path.name`, over a scan that walks four package trees — so a hand-rolled
#: `mcp_server/auth.py` was wholly exempt, and `mcp_server/` is the *one future surface this
#: file's own docstring names*. The exemption was written when the scan covered a single flat
#: package and was never re-derived when a critic widened `PACKAGES`; widening fixed reach and
#: left identity behind.
_OWNERS: frozenset = frozenset({
    Path("closewire_client/auth.py"),
    Path("closewire_client/redaction.py"),
    Path("scripts/codegen.py"),
})

#: The module whose literals prove the scan still sees anything at all.
_OWNER = "auth.py"

#: Header names that carry a credential.
_AUTH_HEADERS = {"x-cb-key", "authorization"}

#: Prefixes that turn a bare key into a credential value.
_CREDENTIAL_PREFIXES = ("bearer ", "key ")

_KEY = "sk-test-provenance-0123456789"


def _modules() -> list[Path]:
    found: list[Path] = []
    for package in PACKAGES:
        directory = ROOT / package
        if directory.is_dir():
            found.extend(sorted(directory.rglob("*.py")))
    return found


def _string_constants(node: ast.AST) -> list[str]:
    """Every string literal in `node`, including the pieces of an f-string.

    f-strings matter: `f"Bearer {key}"` is a JoinedStr whose literal half is a Constant, so a
    scan that only looked at bare Constants would miss the exact shape used to build a
    credential value.
    """
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
    return found


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """`id()` of every Constant that is a docstring.

    Documenting a header is not sending one, and `endpoints/*.py` are generated with
    `X-CB-KEY` in their prose. Identified structurally — first statement of a module, class
    or function — rather than by guessing from content.
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                exempt.add(id(first.value))
    return exempt


def _offences(path: Path) -> list[str]:
    """Auth-header literals typed anywhere in `path`, as `line: description` strings.

    **Position-independent on purpose.** An earlier version only inspected assignments whose
    target was named `headers` and dict keys, and a critic escaped it three ways in one file
    by renaming the target, hoisting the literal into a module constant, and using a
    list-of-tuples header form. There is no legitimate reason to type a credential header
    name outside `auth.py`, so the rule is about the literal, not about where it appears.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    exempt = _docstring_nodes(tree)
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in exempt:
            continue
        text = node.value
        if text.strip().lower() in _AUTH_HEADERS:
            problems.append(f"{node.lineno}: names the auth header {text!r}")
        elif text.lower().startswith(_CREDENTIAL_PREFIXES):
            problems.append(f"{node.lineno}: builds a credential value {text!r}")
    return problems


def test_only_auth_py_spells_an_auth_header() -> None:
    """The class gate. A new surface that hand-rolls auth fails here before it ships."""
    problems: list[str] = []
    for path in _modules():
        if path.relative_to(ROOT) in _OWNERS:
            continue
        for offence in _offences(path):
            problems.append(f"{path.relative_to(ROOT)}:{offence}")
    assert not problems, (
        "these modules put a credential on the wire without going through auth.py, so "
        "CLOSEWIRE_AUTH_STYLE cannot reach them and one auth form is silently pinned:\n"
        + "\n".join(problems)
    )


def test_the_scan_actually_matches_something() -> None:
    """A rule whose patterns match nothing is a check that cannot fail.

    `auth.py` is the one module that must trip every branch of the scan. If a refactor
    renames the header constants or moves the builder, this goes red rather than leaving the
    gate above silently green over a codebase it no longer understands.
    """
    offences = _offences(ROOT / "closewire_client" / _OWNER)
    assert offences, f"{_OWNER} matched no pattern — the scan has gone blind"
    joined = " ".join(offences).lower()
    for expected in ("x-cb-key", "bearer ", "key "):
        assert expected in joined, (
            f"{_OWNER} no longer matches {expected!r}; the corresponding branch of the "
            "structural scan can no longer fire on any module"
        )


class _Capture:
    """Records the request instead of sending it."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.body: Any = None


def _config_for_provenance():
    """A `Config` with pacing zeroed and state isolated — the shape every test here wants."""
    from closewire_client.config import Config

    return Config(api_key=_KEY, dry_run=False, state_dir=_state_dir(), **_FAST)


def _build_runtime_client(config, **kwargs: Any):
    """A `LiveMessageClient` on a MockTransport. Builds only; sends nothing."""
    import httpx

    from closewire_client.live import LiveMessageClient

    return LiveMessageClient(
        config,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": "ok"})),
        **kwargs,
    )


def _capture_runtime_headers(config) -> dict[str, str]:
    """The headers the runtime surface actually puts on the wire for `config`."""
    import httpx

    from closewire_client.live import LiveMessageClient

    seen: dict[str, str] = {}

    def handler(request: "httpx.Request") -> "httpx.Response":
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"message": "ok"})

    client = LiveMessageClient(config, transport=httpx.MockTransport(handler))
    client.send_message(id="contact-1", message="hello")
    return seen


def _send_with(style: str, *, in_body: bool = False) -> _Capture:
    import httpx

    from closewire_client.config import Config
    from closewire_client.live import LiveMessageClient

    captured = _Capture()

    def handler(request: "httpx.Request") -> "httpx.Response":
        captured.headers = dict(request.headers)
        captured.body = request.read().decode()
        return httpx.Response(200, json={"message": "ok"})

    config = Config(api_key=_KEY, dry_run=False, state_dir=_state_dir(), **_FAST)
    client = LiveMessageClient(
        config,
        transport=httpx.MockTransport(handler),
        auth_in_body=in_body,
        auth_style=style,
    )
    client.send_message(id="contact-1", message="hello")
    return captured


def test_every_auth_style_reaches_the_runtime_surface() -> None:
    """The instance gate, phrased so it cannot rot into a constant table.

    Expected headers come from `ApiKeyAuth` itself. Phase 09 already recorded four tests that
    asserted a constant against the constant they were meant to guard; this compares one
    implementation against the only other one that is allowed to exist.
    """
    for style in AUTH_STYLES:
        expected = ApiKeyAuth(_KEY, style).headers()
        captured = _send_with(style)
        for name, value in expected.items():
            actual = captured.headers.get(name.lower())
            assert actual == value, (
                f"auth_style={style!r}: expected {name}: {value!r} on the runtime request, "
                f"got {actual!r}. The runtime surface is pinned to one form again."
            )


def _session_headers(style: str) -> dict[str, str]:
    """Headers `Session` actually puts on the wire for `style`."""
    import httpx

    from closewire_client.auth import ApiKeyAuth as _Auth
    from closewire_client.config import Config
    from closewire_client.session import Session

    captured: dict[str, str] = {}

    def handler(request: "httpx.Request") -> "httpx.Response":
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    config = Config(api_key=_KEY, dry_run=False, state_dir=_state_dir(), **_FAST)
    session = Session(
        config, _Auth(_KEY, style), transport=httpx.MockTransport(handler)
    )
    with session.pacer.acquire(write=False, description="GET /probe"):
        session.request("GET", f"{config.api_base}/agency")
    return captured


def test_the_other_authenticating_surface_also_derives_its_headers_from_auth_py() -> None:
    """`Session` is the second surface that puts the key on the wire.

    A critic pointed out that this file's docstring claimed *every* surface was compared
    against `ApiKeyAuth` while only `LiveMessageClient` was. That is the exact shape of the
    defect the file was added for — a claim of coverage wider than the coverage — so the
    claim is now backed by a test rather than narrowed in prose.
    """
    for style in AUTH_STYLES:
        expected = ApiKeyAuth(_KEY, style).headers()
        captured = _session_headers(style)
        for name, value in expected.items():
            assert captured.get(name.lower()) == value, (
                f"Session, auth_style={style!r}: expected {name}: {value!r}, got "
                f"{captured.get(name.lower())!r}"
            )


def test_the_three_styles_are_actually_distinguishable() -> None:
    """Control. The test above would pass if every style produced the same header."""
    sent = {style: tuple(sorted(_send_with(style).headers.items())) for style in AUTH_STYLES}
    assert len(set(sent.values())) == len(AUTH_STYLES), (
        f"the three auth styles are not distinguishable on the wire: {sent}"
    )


def test_body_auth_puts_no_credential_in_any_header() -> None:
    """`auth_in_body=True` is this module's one auth guarantee — the key leaves via the body.

    Asserted against every style, because the failure mode is a header form that leaks past
    the `if` and lands alongside the body field, charging the guarantee to a form nobody
    tested.
    """
    for style in AUTH_STYLES:
        captured = _send_with(style, in_body=True)
        assert _KEY in (captured.body or ""), f"{style}: key not in body under auth_in_body"
        for name in _AUTH_HEADERS:
            assert _KEY not in captured.headers.get(name, ""), (
                f"auth_style={style!r}, auth_in_body=True: the key is in the {name!r} header "
                "as well as the body"
            )


def test_an_unknown_auth_style_is_refused_at_construction() -> None:
    """Not at send time. A bad style must not be discovered by spending a credit on a 401."""
    import httpx

    from closewire_client.config import Config
    from closewire_client.live import LiveMessageClient

    try:
        LiveMessageClient(
            Config(api_key=_KEY, dry_run=False, state_dir=_state_dir(), **_FAST),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
            auth_style="nonsense",
        )
        raise AssertionError("an unknown auth_style was accepted")
    except ValueError as error:
        assert "auth_style" in str(error), error


def test_every_owner_is_a_repo_relative_path_that_exists() -> None:
    """A basename exemption is a hole across four package trees; a moved owner is a dead one.

    Both failure modes are asserted because both are silent: a string entry would exempt any
    file with that name anywhere, and a path pointing at a file that moved would exempt
    nothing while looking like it still guarded something.
    """
    for owner in _OWNERS:
        assert isinstance(owner, Path) and len(owner.parts) >= 2, (
            f"{owner!r} is not a repo-relative path. A bare basename exempts that filename "
            "in every scanned package — including mcp_server/, the surface this gate names "
            "as the next candidate."
        )
        assert (ROOT / owner).is_file(), (
            f"_OWNERS names {owner}, which does not exist. A stale exemption guards nothing "
            "and hides that it guards nothing."
        )


#: Every place in the shipped packages that builds its own `httpx.Client`.
#:
#: A **decidable** syntactic question, unlike "does this expression evaluate to a header
#: name" — which is why this is the right shape for the residual gap. The wire assertion in
#: `tests/conftest.py` sees only requests some test actually drives, so a brand-new surface
#: with no test is unobserved. Requiring the construction-site census to be declared closes
#: that from the other side: adding a fourth client is a red test whose remedy is "register
#: it, and register the test that drives it".
_HTTPX_CLIENT_SITES = {
    "closewire_client/live.py",      # the runtime surface
    "closewire_client/session.py",   # the REST surface
    "scripts/probe_runtime_auth.py", # the bot_id probe; excluded from CI, never run by tests
}


def test_the_httpx_client_census_is_declared() -> None:
    """A new client is a new surface, and a new surface must be registered before it ships.

    The wire-level provenance fixture cannot see a surface no test drives. This is the half
    that notices the surface exists at all.
    """
    found: set[str] = set()
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"Client", "AsyncClient"}:
                found.add(path.relative_to(ROOT).as_posix())

    unexpected = sorted(found - _HTTPX_CLIENT_SITES)
    assert not unexpected, (
        f"these modules build their own httpx client and are not declared: {unexpected}. "
        "Every client is a surface that can put a credential on the wire — add it to "
        "_HTTPX_CLIENT_SITES, and add a test that drives it so tests/conftest.py's wire "
        "assertion can see it."
    )
    stale = sorted(_HTTPX_CLIENT_SITES - found)
    assert not stale, (
        f"_HTTPX_CLIENT_SITES names {stale}, which no longer builds a client. A stale entry "
        "makes the census look complete while covering nothing."
    )


def test_the_wire_assertion_refuses_a_credential_auth_never_issued() -> None:
    """The guarantee a static scan cannot give, asserted directly.

    Six of these shapes contain no string literal at all, and `AUTH_STYLES[0]` is spelled
    with a name `auth.py` exports — no literal rule can flag it without flagging every
    legitimate use of the tuple. All of them are working header names, and all of them are
    refused at the wire.
    """
    import httpx

    key = "sk-live-PROVENANCE"
    shapes = {
        "bytes": {b"X-CB-KEY": key.encode()},
        "concatenation": {"X-CB" + "-KEY": key},
        "join": {"".join(["X-CB", "-KEY"]): key},
        "printf": {"%s-CB-KEY" % "X": key},
        "format": {"{}-CB-KEY".format("X"): key},
        "AUTH_STYLES[0]": {AUTH_STYLES[0]: key},
        "chr()": {chr(88) + "-CB-KEY": key},
        "bearer f-string": {"Authorization": f"Bearer {key}"},
    }

    def _send(headers: Any) -> None:
        with httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ) as client:
            client.get("https://api.closebot.com/agency", headers=headers)

    # Entered explicitly rather than relying on conftest's autouse fixture, so this test
    # gives the same answer under `pytest` and under `python tests/test_auth_provenance.py`.
    # It did not: conftest is a pytest concept, the direct runner never loaded it, and the
    # two paths disagreed about whether a security property held. `scripts/verify_runners.py`
    # found that within a minute of being written.
    escaped: list[str] = []
    with asserting_auth_provenance():
        for label, headers in shapes.items():
            try:
                _send(headers)
            except AssertionError as error:
                if "never issued" in str(error):
                    continue
                raise
            escaped.append(label)
    assert not escaped, f"these laundering shapes reached the wire unchallenged: {escaped}"


def test_the_receipt_book_is_live() -> None:
    """Anti-vacuity control. An empty book would make the check above refuse everything...

    ...and a book that is never written to would make it refuse *legitimate* traffic, which
    someone would then "fix" by deleting the hook. So both directions are asserted: a header
    `ApiKeyAuth` issued must pass, under every style.
    """
    import httpx

    with asserting_auth_provenance():
        for style in AUTH_STYLES:
            with httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            ) as client:
                client.get("https://api.closebot.com/agency",
                           headers=ApiKeyAuth(_KEY, style).headers())


def test_the_runtime_client_still_refuses_to_inherit_the_rest_auth_style() -> None:
    """The refusal is correct and must survive the fix for its silence.

    Two hosts, two documented conventions. Inheriting `CLOSEWIRE_AUTH_STYLE` would let a
    value set to make the REST host authenticate silently change what goes on the wire to the
    credit-spending endpoint. This asserts the refusal so that "tell the operator" cannot
    drift into "just inherit it".
    """
    import dataclasses

    config = dataclasses.replace(
        _config_for_provenance(), auth_style="authorization-bearer"
    )
    captured = _capture_runtime_headers(config)
    assert "x-cb-key" in {name.lower() for name in captured}, (
        f"the runtime client inherited the REST host's auth style: {sorted(captured)}"
    )


def test_it_says_so_when_the_env_style_cannot_reach_the_runtime_host() -> None:
    """A knob that appears to turn is worse than one documented as fixed.

    The refusal was right and invisible: an operator setting `authorization-bearer` to probe
    a Bearer deployment got `x-cb-key` with no warning, no log line and no error.
    """
    import dataclasses
    import io
    import logging

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger = logging.getLogger("closewire.live")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        _build_runtime_client(
            dataclasses.replace(_config_for_provenance(), auth_style="authorization-bearer")
        )
        warned = buffer.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert "REST host" in warned and "does not inherit" in warned, (
        f"no warning names the scope; the divergence is silent again. Got: {warned!r}"
    )


def test_it_stays_quiet_when_the_caller_chose_the_style() -> None:
    """An explicit choice is a decision, not a divergence. Warning on it would train it out."""
    import io
    import logging

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger = logging.getLogger("closewire.live")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        _build_runtime_client(_config_for_provenance(), auth_style="x-cb-key")
        quiet = buffer.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert quiet.strip() == "", f"warned on an explicit choice: {quiet!r}"


def test_env_example_scopes_the_auth_style_to_the_rest_host() -> None:
    """The documentation half. The knob's own description promised global scope."""
    block = (ROOT / ".env.example").read_text(encoding="utf-8")
    section = block[block.index("CLOSEWIRE_AUTH_STYLE") - 700: block.index("CLOSEWIRE_AUTH_STYLE") + 60]
    assert "api.closebot.ai" in section and "REST host ONLY" in section, (
        ".env.example no longer scopes CLOSEWIRE_AUTH_STYLE to the REST host"
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} auth-provenance tests passed.")
