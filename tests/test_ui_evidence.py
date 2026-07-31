"""Gate: committed UI evidence carries a sidecar, and cited images exist.

`docs/validation/screens/` is gitignored — correctly, because some raw captures carry client
lead names and a phone number. But `docs/validation/README.md`'s protocol step 3 pointed
*every* phase's UI evidence there, so thirteen cited images across phases 04-09 are local-only
and phase 13's sign-off packet cannot carry any of them. The ignore rule was right; the
protocol aiming all captures at it was the defect.

So there are two tiers now — raw in `screens/`, a redacted crop plus a sidecar in
`evidence/ui/` — and this asserts what is mechanically assertable about the second:

* every committed UI image has a sidecar;
* every sidecar carries the required keys, non-empty;
* every sidecar's `raw:` points into `screens/`;
* every `screens/...` or `evidence/ui/...` path a log cites **exists**.

That last one is not hypothetical: deviation 33 cited "`screens/09-goal-flip-transcript.png`'s
sibling capture of the flow", and no flow-canvas capture was ever taken. The deviation's
substance was true; its evidence pointer was void, and nothing could see that.

**The honest limit, stated rather than discovered:** no test can read pixels for PII. The
sidecar's `redacted:` line is an attestation by whoever cropped the image. What is mechanical
is that the attestation exists, names its source, and is reviewable in the diff.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "validation"
UI = DOCS / "evidence" / "ui"

#: Keys a sidecar must carry. `limits:` is optional — most captures have none.
_REQUIRED_KEYS = ("raw", "captured", "shows", "redacted")

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}


def _sidecar_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    key = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-z]+):\s*(.*)$", line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip()
        elif key and line.startswith((" ", "\t")):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def test_every_committed_ui_image_has_a_complete_sidecar() -> None:
    """An image with no sidecar is a picture nobody can say anything about."""
    if not UI.is_dir():
        return
    problems: list[str] = []
    for image in sorted(p for p in UI.iterdir() if p.suffix.lower() in _IMAGE_SUFFIXES):
        sidecar = image.with_suffix(".md")
        if not sidecar.exists():
            problems.append(f"{image.name}: no {sidecar.name} sidecar")
            continue
        fields = _sidecar_fields(sidecar)
        for key in _REQUIRED_KEYS:
            if not fields.get(key):
                problems.append(f"{sidecar.name}: `{key}:` is missing or empty")
        raw = fields.get("raw", "")
        if raw and not raw.startswith("screens/"):
            problems.append(
                f"{sidecar.name}: `raw:` is {raw!r}; it must name the uncropped capture "
                "under screens/ so the redaction can be re-derived"
            )
    assert not problems, "\n".join(problems)


def test_every_image_a_log_cites_actually_exists() -> None:
    """A citation pointing at nothing is worse than no citation.

    Deviation 33 cited a flow-canvas capture that was never taken. The deviation was true and
    its evidence was void, and no check could tell.
    """
    problems: list[str] = []
    cited = re.compile(r"`((?:screens|evidence/ui)/[\w./-]+\.(?:png|jpg|jpeg|gif))`")
    for log in sorted(DOCS.glob("[0-9][0-9]-*.md")):
        for match in cited.finditer(log.read_text(encoding="utf-8")):
            target = DOCS / match.group(1)
            if not target.exists():
                problems.append(f"{log.name} cites {match.group(1)}, which does not exist")
    assert not problems, "\n".join(problems)


def test_the_ui_evidence_tier_is_not_empty() -> None:
    """Self-check. An empty directory makes the sidecar gate vacuous.

    Also the standing reminder that phases 04-08 have not been backfilled: nine raw captures
    are still local-only, and until they are cropped and committed, phase 13 cannot carry
    their UI evidence.
    """
    assert UI.is_dir() and any(p.suffix.lower() in _IMAGE_SUFFIXES for p in UI.iterdir()), (
        "docs/validation/evidence/ui/ holds no committed images, so the sidecar gate asserts "
        "nothing"
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} UI-evidence tests passed.")
