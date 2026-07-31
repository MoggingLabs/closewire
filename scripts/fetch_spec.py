#!/usr/bin/env python3
"""Fetch the live Closebot OpenAPI spec and report drift vs the vendored oracle.

Downloads the real Swagger from the ``megastream25`` origin (mirror fallback) to
``schema/openapi.live.json``, then diffs its operation set against the vendored
``schema/openapi.json`` and prints added/removed operations so spec drift is visible
before regenerating the client.

The spec is public; **no API key is sent or required**. Uses only the standard library.

WARNING (see RESEARCH.md): ``https://developers.closebot.com/api-reference/openapi.json``
is a Mintlify "Plant Store" placeholder — never use it. The real spec lives at the URLs
below.

Usage: ``python scripts/fetch_spec.py``
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
VENDORED = SCHEMA_DIR / "openapi.json"
LIVE_OUT = SCHEMA_DIR / "openapi.live.json"

# Primary origin first, then the api.closebot.com mirror (RESEARCH.md).
SPEC_URLS = (
    "https://megastream25-api.closebot.com/swagger/v1/swagger.json",
    "https://api.closebot.com/swagger/v1/swagger.json",
)

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")


def synth_id(method: str, path: str) -> str:
    """Reproduce the toolkit's operationId style, e.g. ``delete_account-apikey-keyid``."""
    slug = path.strip("/").replace("{", "").replace("}", "").lower().replace("/", "-")
    return f"{method.lower()}_{slug}"


def operations(spec: dict) -> dict[tuple[str, str], str]:
    """Map ``(METHOD, path) -> synthesized operationId`` for every operation."""
    ops: dict[tuple[str, str], str] = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            ops[(method.upper(), path)] = op.get("operationId") or synth_id(method, path)
    return ops


def download(url: str, timeout: float = 30.0) -> bytes:
    # Checked, not asserted in a comment. This function used to carry a suppression reading
    # "trusted https" — placed on the wrong line, so it suppressed nothing, and unknowable
    # because the rule was not enabled. That is the case against decorative suppressions:
    # they are unverified claims about what the linter does. S310 warns that `urlopen`
    # accepts `file:` and custom schemes; this makes "https only" actually true.
    if not url.startswith("https://"):
        raise ValueError(f"fetch_spec downloads over https only, got {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "closewire-fetch-spec/0.1"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def fetch_live() -> dict | None:
    """Try each spec URL; write and return the first that yields valid JSON."""
    for url in SPEC_URLS:
        try:
            print(f"fetching {url} ...")
            raw = download(url)
            spec = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"  ! failed: {type(exc).__name__}: {exc}")
            continue
        # Pretty-write so the vendored/live diff stays reviewable in git.
        LIVE_OUT.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  -> wrote {LIVE_OUT.relative_to(ROOT)} ({len(raw):,} bytes from source)")
        return spec
    return None


def report_drift(live: dict) -> None:
    if not VENDORED.exists():
        print("\nno vendored schema/openapi.json to diff against — skipping drift report.")
        return
    vendored = json.loads(VENDORED.read_text(encoding="utf-8"))
    live_ops = operations(live)
    vend_ops = operations(vendored)

    added = sorted(live_ops.keys() - vend_ops.keys())
    removed = sorted(vend_ops.keys() - live_ops.keys())

    print("\n=== drift report (live vs vendored openapi.json) ===")
    print(f"vendored operations: {len(vend_ops)}  |  live operations: {len(live_ops)}")
    print(f"added (in live, not vendored):   {len(added)}")
    for method, path in added:
        print(f"  + {live_ops[(method, path)]:40} {method} {path}")
    print(f"removed (in vendored, not live): {len(removed)}")
    for method, path in removed:
        print(f"  - {vend_ops[(method, path)]:40} {method} {path}")
    if not added and not removed:
        print("  no operation drift — vendored spec matches live.")


def main() -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    live = fetch_live()
    if live is None:
        print(
            "\nERROR: could not download the live spec from any known URL.\n"
            "Check connectivity; the vendored schema/openapi.json remains the oracle.",
            file=sys.stderr,
        )
        return 1
    report_drift(live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
