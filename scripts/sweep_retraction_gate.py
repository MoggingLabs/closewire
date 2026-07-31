"""Measure the retraction gate's escape rate, reproducibly.

`tests/test_validation_logs.py` gates the property that a withdrawn claim may be quoted but
never asserted as bare prose. That gate has now been rebuilt four times, each time after a
critic found it narrower than it claimed:

1. a "retraction marker within 1000 characters" heuristic that fired on 41% of the document;
2. raw-text matching, blind to a claim wrapped across two lines;
3. exact-literal matching, blind to `closes` against a sentinel of `close`;
4. document-wide quote pairing, which inherited a ``` fence's odd backtick and let **7.1%**
   of insertion points through.

Each rebuild was accompanied by a measured escape rate — and the measurements existed only as
prose. Round 12 found the same figure written as 3,645 in one place and 10,917 in another,
with no script anywhere to say which was right. A number that gates nothing and reproduces
nowhere is exactly the kind of claim this project keeps blocking on.

So the measurement is a script. It inserts each restatement form at every line boundary of a
log, runs the real gate predicates, and reports where the gate stays green. Insertions that
land **inside a fenced code block** are counted separately and are not escapes: fenced content
is captured command output, not prose making a claim, and `_strip_fences` removes it by design.

    python scripts/sweep_retraction_gate.py            # every sentinel form, every line
    python scripts/sweep_retraction_gate.py --stride 5 # faster, samples every 5th line

Not wired into `scripts/ci.py`: it takes minutes, and the gate it measures is already enforced
on every run by `tests/test_validation_logs.py`. This answers "how well does it hold", which
is a question you ask when you change it.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tests" / "test_validation_logs.py"
LOG = ROOT / "docs" / "validation" / "09-runtime.md"


def _gate():
    """The real gate module, loaded by path so this measures what ships."""
    spec = importlib.util.spec_from_file_location("_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


#: Restatement forms. Each must be a *bare-prose assertion* of something the log withdrew —
#: if the gate stays green on any of these, a real defect could ship the same way.
FORMS: list[tuple[str, str]] = [
    ("plain", "The body-auth rows close the last cheap avenue and nothing remains."),
    ("inflected-s", "The body-auth rows closes the last cheap avenue."),
    ("inflected-ed", "The body-auth rows closed the last cheap avenue."),
    ("inflected-ing", "Round 6 was right in closing the last cheap avenue."),
    ("emphasis-inner", "The probe clos**es** the last cheap avenue."),
    ("emphasis-outer", "This *closes* the last cheap avenue for good."),
    ("credential-verdict", "In short, 410 is not a credential verdict."),
    ("leans", "Evidence leans toward the credential not authenticating on the runtime host at all."),
    ("metered", "Confirmed: test-session messages are not metered."),
    ("session", "It turns out no session was created."),
    ("table-cell", "| note | 410 is not a credential verdict |"),
    ("heading", "### It turns out no session was created."),
    ("blockquote", "> Confirmed: test-session messages are not metered."),
    ("list-item", "- In short, 410 is not a credential verdict."),
    # Known limits, measured rather than hidden. `_stem` frees a word's tail, not its
    # interior, and `_pattern` joins words on whitespace — so changing punctuation *inside*
    # a word defeats it. De-hyphenation is the one a human might write by accident.
    ("de-hyphenated", "Confirmed: test session messages are not metered."),
    # The semantic class. Five forms, not one — see KNOWN_LIMITS for why that matters.
    ("paraphrase-synonym", "Confirmed: test-session messages are not billed."),
    ("paraphrase-nocharge", "Confirmed: test-session messages incur no charge."),
    ("paraphrase-recast", "No credit is consumed by a test-session message."),
    ("paraphrase-verdict", "The 410 does not tell us anything about the credential."),
    ("paraphrase-passive", "Sessions are not created by the probe."),
]


#: Forms that are *expected* to escape, with the reason.
#:
#: **The limit is semantic, not lexical, and saying "synonym" understated it.** A previous
#: version listed exactly one form, which implied a one-form hole; the hole is the entire
#: paraphrase class and is infinite. A critic measured five distinct paraphrases escaping,
#: two of them near-verbatim recasts of real sentinels ("The 410 does not tell us anything
#: about the credential", "Sessions are not created by the probe"). Declaring a narrower
#: limit than the design has is the same *summary understates the detail* defect this whole
#: file polices, one level up — so the declaration now names the class and carries enough
#: members to show its breadth.
#:
#: What justifies keeping the matcher anyway: **every escape this gate has actually suffered
#: was a mutated copy** — `closes`, `clos**es**`, `test session`, a homoglyph — not a novel
#: paraphrase. The realised threat is stale duplication, and against that the matcher is
#: strong. No amount of hardening touches the theoretical one; a different representation
#: does (see the generated-register recommendation in `docs/validation/09-runtime.md`).
#:
#: `--check` fails in **both** directions: a form that escapes and is not listed is a
#: regression, and a listed form that stops escaping is a stale exemption hiding the next one.
KNOWN_LIMITS = {
    "paraphrase-synonym", "paraphrase-nocharge", "paraphrase-recast",
    "paraphrase-verdict", "paraphrase-passive",
}


def fenced_lines(text: str) -> set[int]:
    inside: set[int] = set()
    open_fence = False
    for index, line in enumerate(text.split("\n")):
        if line.strip().startswith("```"):
            open_fence = not open_fence
            inside.add(index)
            continue
        if open_fence:
            inside.add(index)
    return inside


def caught(gate, text: str) -> bool:
    """True when the gate would flag `text`.

    Calls `bare_prose_hits` — **the gate's own function**, not a re-implementation of it.
    This script used to keep a copy of the matching loop, and when the gate moved to skeleton
    matching the copy stayed on lowercased text: the sweep then reported all sixteen forms
    escaping when none did. A critic had predicted precisely that failure one round earlier.
    A measurement that re-implements what it measures cannot be trusted in either direction.
    """
    return any(gate.bare_prose_hits(text, wording) for _filename, wording in gate.RETRACTED)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stride", type=int, default=1,
                        help="sample every Nth line boundary (default: every one)")
    parser.add_argument("--log", type=Path, default=LOG)
    parser.add_argument(
        "--check", action="store_true",
        help="assert the escape count is zero for every non-limit form; exit 1 otherwise. "
             "This is the mode a gate calls — it asserts the property, not the arithmetic.",
    )
    args = parser.parse_args()

    gate = _gate()
    raw = args.log.read_text(encoding="utf-8")
    lines = raw.split("\n")
    fenced = fenced_lines(raw)

    if caught(gate, raw):
        print("FAIL: the unmodified log already trips the gate — fix that first.")
        return 1

    total = escapes = in_fence = 0
    rows: list[tuple[str, int, int]] = []
    for label, form in FORMS:
        form_escapes = form_fenced = 0
        for index in range(0, len(lines), args.stride):
            total += 1
            mutated = "\n".join(lines[:index] + ["", form, ""] + lines[index:])
            if caught(gate, mutated):
                continue
            if index in fenced:
                form_fenced += 1
                in_fence += 1
            else:
                form_escapes += 1
                escapes += 1
        rows.append((label, form_escapes, form_fenced))

    width = max(len(label) for label, _, _ in rows)
    print(f"\n{'form'.ljust(width)}  escapes  in-fence")
    for label, form_escapes, form_fenced in rows:
        marker = "  <-- ESCAPES" if form_escapes else ""
        print(f"{label.ljust(width)}  {form_escapes:7d}  {form_fenced:8d}{marker}")

    print(f"\nlog:              {args.log.relative_to(ROOT)}")
    print(f"insertion points: {total}  (stride {args.stride}, {len(FORMS)} forms)")
    print(f"real escapes:     {escapes}")
    print(f"inside fences:    {in_fence}  (not escapes — fenced text is captured output)")

    if args.check:
        # Assert the PROPERTY, never a transcribed count. Round 13 wrote "8,224 insertion
        # points" into the log; the log then grew and the number was wrong inside the round
        # that introduced it — which is the same drift the script was added to stop, one
        # level up. An insertion-point total is a function of the document's length and has
        # no business being a claim. What matters is that nothing escapes.
        leaked = [(label, count) for label, count, _ in rows
                  if count and label not in KNOWN_LIMITS]
        if leaked:
            print("\nFAIL: forms escaping that are not declared limits:")
            for label, count in leaked:
                print(f"  {label}: {count}")
            return 1
        undeclared = [label for label in KNOWN_LIMITS
                      if not any(label == row_label and count for row_label, count, _ in rows)]
        if undeclared:
            # A "limit" that no longer leaks is a limit that has been fixed, or a form that
            # has gone stale. Either way the declaration is now false and should be removed.
            print(f"\nFAIL: declared limits that no longer escape: {undeclared}")
            print("  Remove them from KNOWN_LIMITS — a stale exemption hides a real regression.")
            return 1
        print(f"\nOK: zero escapes outside the declared limits {sorted(KNOWN_LIMITS)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
