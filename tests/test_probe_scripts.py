"""Gates on the probe scripts — the ones that can reach a metered endpoint.

`scripts/probe_runtime_auth.py` posts to the credit-spending runtime host. It is the most
dangerous file in the repo and, until phase 09 round 13, the least gated: four separate
defects survived three review rounds each, every one filed "non-blocking" because none of
them *had* spent money yet.

* it cleared `CLOSEWIRE_DRY_RUN` for you — an operator's safety belt became five chargeable
  POSTs, from the command the docstring tells them to run;
* it built a fresh `Pacer` per probe, so one invocation granted itself six hourly write
  budgets and a 1/hour ceiling stopped meaning anything;
* it scrubbed responses with `config.scrub` alone — the *value*-based half — so a third
  party's credential echoed in a 410 would have reached stdout and the committed evidence;
* it fetched a paced meter read and immediately `del`'d it.

Each was found by reading, not by running, because nothing exercised this file. That is the
gap this closes: the script now has tests, and they run offline with the network stubbed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from tests._provenance import AuthProvenanceError, receipt
from closewire_client.auth import ISSUED
from closewire_client.redaction import is_secret_name


def _check_provenance(request: "httpx.Request") -> None:
    """The same assertion `tests/conftest.py` makes, applied inside the stub.

    Duplicated deliberately and minimally: the stub replaces `httpx.Client.send` wholesale,
    so the autouse hook is no longer on the call path. Re-asserting here is what keeps these
    five tests — the only ones driving the script that can reach a metered endpoint — under
    the same credential guarantee as the rest of the suite.
    """
    for name, value in request.headers.items():
        if is_secret_name(name) and receipt(name, value) not in ISSUED:
            raise AuthProvenanceError(
                f"{request.method} {request.url}: header {name!r} carries a credential "
                "that closewire_client.auth never issued."
            )

PROBE = ROOT / "scripts" / "probe_runtime_auth.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Stands in for the network. Records every request; answers everything plausibly."""

    def __init__(self, body: str = '{"ok": true}') -> None:
        self.requests: list[tuple[str, str]] = []
        self.body = body

    def handler(self, request: "httpx.Request", *, record: bool = True) -> "httpx.Response":
        if record:
            self.requests.append((request.method, str(request.url)))
        if "usage" in str(request.url):
            return httpx.Response(200, json={"usedResponses": 4.0, "maxResponses": 500})
        if request.url.path == "/bot":
            return httpx.Response(200, json=[{"id": "bot_zzTEST"}])
        return httpx.Response(410, text=self.body)


@contextlib.contextmanager
def _stubbed(recorder: _Recorder):
    """Route every httpx request through `recorder`, whatever client built it.

    **Chains to whatever hook is already installed** rather than replacing it. An earlier
    version captured `httpx.Client.send` as `original` and never called it, which silently
    disabled `tests/conftest.py`'s autouse credential-provenance assertion for exactly these
    five tests — the ones exercising the one script that can reach the credit-spending
    runtime host. Two critics filed it independently. A stub that drops the hook it displaced
    is how a suite-wide guarantee acquires a hole nobody can see.
    """
    original = httpx.Client.send

    def send(self: httpx.Client, request: "httpx.Request", **kwargs: Any) -> "httpx.Response":
        # Let the provenance hook (and anything else layered on) inspect the request first;
        # it raises on a credential this client did not issue. Only then answer from the
        # recorder instead of the network.
        recorder.requests.append((request.method, str(request.url)))
        _check_provenance(request)
        return recorder.handler(request, record=False)

    httpx.Client.send = send  # type: ignore[method-assign]
    try:
        yield
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]


@contextlib.contextmanager
def _env(**values: str):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, was in saved.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was


def test_the_runtime_probe_refuses_to_run_under_dry_run() -> None:
    """It must refuse, not correct.

    `dataclasses.replace(load_config(), dry_run=False)` is a one-line way to turn an
    operator's explicit safety setting into real spending. The house rule everywhere else in
    this codebase — `live.py`'s `session=`, `rest.py`'s `_require_flag` — is structural
    refusal over silent correction, and this was the file that broke it.
    """
    probe = _load("_probe_dryrun", PROBE)
    recorder = _Recorder()
    out, err = io.StringIO(), io.StringIO()
    with _env(CLOSEWIRE_DRY_RUN="1"), _stubbed(recorder):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = probe.main([])
    assert code == 2, "the probe ran under dry-run instead of refusing"
    assert not recorder.requests, (
        f"the probe sent {len(recorder.requests)} request(s) with CLOSEWIRE_DRY_RUN set: "
        f"{recorder.requests}"
    )
    assert "CLOSEWIRE_DRY_RUN is set" in err.getvalue()


def test_the_runtime_probe_builds_exactly_one_pacer() -> None:
    """Budgets are per-`Pacer`; six Pacers is six hourly budgets.

    Asserted on the instance count rather than on a budget symptom, because a generous test
    config would hide the symptom while leaving the cause in place.
    """
    from closewire_client import pacing

    probe = _load("_probe_pacer", PROBE)
    built = {"n": 0}
    original = pacing.Pacer.__init__

    def counting(self: Any, *args: Any, **kwargs: Any) -> None:
        built["n"] += 1
        original(self, *args, **kwargs)

    pacing.Pacer.__init__ = counting  # type: ignore[method-assign]
    try:
        recorder = _Recorder()
        with _env(CLOSEWIRE_DRY_RUN="0", CLOSEWIRE_MIN_DELAY_S="0",
                  CLOSEWIRE_MAX_DELAY_S="0"), _stubbed(recorder):
            with contextlib.redirect_stdout(io.StringIO()):
                probe.main(["--live"])
    finally:
        pacing.Pacer.__init__ = original  # type: ignore[method-assign]
    assert built["n"] == 1, (
        f"the probe built {built['n']} Pacers in one invocation; each carries its own hourly "
        "budget, so N Pacers is N times the ceiling the operator configured"
    )


def test_the_runtime_probe_reads_the_meter_exactly_twice() -> None:
    """Before and after. A third read was dead work that cost a real paced call."""
    probe = _load("_probe_meter", PROBE)
    recorder = _Recorder()
    with _env(CLOSEWIRE_DRY_RUN="0", CLOSEWIRE_MIN_DELAY_S="0",
              CLOSEWIRE_MAX_DELAY_S="0"), _stubbed(recorder):
        with contextlib.redirect_stdout(io.StringIO()):
            probe.main(["--live"])
    reads = [url for _method, url in recorder.requests if "usage" in url]
    assert len(reads) == 2, f"expected 2 meter reads (before/after), got {len(reads)}"


def test_the_bot_id_probe_masks_a_third_party_credential() -> None:
    """Its stdout is committed as evidence, so a name-based miss ships to the repo.

    `config.scrub` finds our own key by value and nothing else. A credential belonging to
    someone else — echoed back in an error body — is found only by the name-based rule, and
    this path applied just the first half.
    """
    third_party = "sk-THIRD-PARTY-abcdefghijklmnop"
    probe = _load("_probe_scrub", PROBE)
    recorder = _Recorder(body=f'{{"echo": {{"X-CB-KEY": "{third_party}"}}}}')
    out = io.StringIO()
    with _env(CLOSEWIRE_DRY_RUN="0", CLOSEWIRE_MIN_DELAY_S="0",
              CLOSEWIRE_MAX_DELAY_S="0"), _stubbed(recorder):
        with contextlib.redirect_stdout(out):
            probe.main(["--live"])
    printed = out.getvalue()
    assert third_party not in printed, (
        "a third-party credential reached stdout unmasked — this output is committed under "
        "docs/validation/evidence/"
    )
    assert "<redacted>" in printed


def test_the_probe_scrubber_is_the_one_the_live_client_uses() -> None:
    """Two copies of a masking pipeline is one copy too many.

    The defect was not that the probe scrubbed badly — it was that it scrubbed *separately*,
    so `live.py` could be fixed and the probe could not follow.
    """
    from closewire_client.config import Config
    from closewire_client.live import scrub_body

    source = PROBE.read_text(encoding="utf-8")
    assert "scrub_body(" in source, "the probe no longer routes through live.scrub_body"

    config = Config(api_key="sk-ours-1234567890")
    body = '{"ours": "sk-ours-1234567890", "theirs": {"Authorization": "Bearer sk-x"}}'
    masked = str(scrub_body(config, body))
    assert "sk-ours-1234567890" not in masked, "value-based masking is missing"
    assert "sk-x" not in masked, "name-based masking is missing"


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} probe-script tests passed.")
