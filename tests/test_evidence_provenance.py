"""Gate: every committed capture says where it came from, and the producer exists.

"Results exist only as prose" has been a BLOCKING finding in rounds 2, 4, 10 — and again in
13, when the *fix* for it was itself delivered as prose. The root cause is that prose is the
default medium and evidence is opt-in: nothing distinguishes a sentence reporting an
observation from a sentence asserting a belief, so the cheapest way to record a result is to
type it, and every fix has produced the one artefact a critic named.

Two properties, both cheap:

* **Every capture declares a producer.** A first line of `# produced by: <thing>`. A
  hand-typed file with no header is prose wearing a filename, and this is what makes that
  visible. Where the producer genuinely is not a script — `09-goal-flip-cli.txt` interleaves
  `test start`, `test say` and a `bots publish`, so no one command emits it — the header says
  *that*, which is an attested claim rather than an unattributed one. A review agent found the
  log describing it as "verbatim output of `test start` and each `test say`", which its own
  composition does not support.
* **A declared script exists and is CI-classified.** A capture pointing at a deleted script is
  evidence nobody can reproduce; `tests/test_ci_wiring.py` already requires every
  `scripts/*.py` to be wired or explicitly excluded, so naming one here inherits that.

What this deliberately does **not** do is claim every observation in the logs cites a capture.
That would be the right property and it is a large migration; asserting it now would either
fail on hundreds of legitimate sentences or need an exclusion list long enough to be its own
defect. Recorded as the honest limit, in the spirit of `KNOWN_LIMITS`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "validation" / "evidence"

#: `# produced by: <script path or an explicit statement that it is not a script>`
_PROVENANCE = re.compile(r"^#\s*produced by:\s*(.+?)\s*$")

#: Extensions that carry their own provenance conventions and are checked elsewhere.
#: Images cannot hold a comment line; their provenance lives in the log that cites them.
_NOT_TEXT = {".png", ".jpg", ".jpeg", ".gif"}


def _captures() -> list[Path]:
    if not EVIDENCE.is_dir():
        return []
    return sorted(p for p in EVIDENCE.iterdir()
                  if p.is_file() and p.suffix.lower() not in _NOT_TEXT)


def test_every_capture_declares_a_producer() -> None:
    """A capture with no stated origin is prose that happens to live in a file."""
    problems: list[str] = []
    for path in _captures():
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        match = _PROVENANCE.match(first[0]) if first else None
        if match is None:
            problems.append(
                f"{path.name}: no `# produced by:` first line. If a script produced it, name "
                "the script; if it was assembled by hand, say so — an unattributed capture is "
                "indistinguishable from prose."
            )
    assert not problems, "\n".join(problems)


def test_a_declared_producer_script_exists() -> None:
    """A capture citing a deleted script is a result nobody can reproduce."""
    problems: list[str] = []
    for path in _captures():
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        match = _PROVENANCE.match(first[0]) if first else None
        if match is None:
            continue  # reported by the test above
        declared = match.group(1)
        script = re.match(r"(scripts/[\w./-]+\.py)", declared)
        if script is None:
            continue  # an explicit "not a script" attestation
        if not (ROOT / script.group(1)).exists():
            problems.append(f"{path.name}: names producer {script.group(1)}, which does not exist")
    assert not problems, "\n".join(problems)


def test_there_are_captures_to_check() -> None:
    """The self-check. An empty evidence directory makes both gates above vacuous."""
    captures = _captures()
    assert captures, (
        "docs/validation/evidence/ holds no text captures, so the provenance gates assert "
        "nothing. If evidence moved, move this gate with it."
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} evidence-provenance tests passed.")
