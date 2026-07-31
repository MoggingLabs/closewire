"""Gates on the validation logs themselves.

`docs/validation/*.md` are the phase deliverable a council reads and phase 13 assembles into
the sign-off packet. They are also long, hand-edited documents, and one failure mode has now
been filed as BLOCKING in phase 07, 08 and 09 — repeatedly:

> the detailed section gets corrected, the summary that repeats it does not.

Phase 09 round 7 made the cause plain. It was not forgetfulness: string replacements had been
*inserting* corrected text without deleting the wrong version, so the document ended up
asserting both. Four critics independently found a census table printed twice with different
sums, a retracted claim still standing in the verdict register, and a call count contradicted
by the table beneath it.

Fixing those three instances would not stop a fourth. So the properties are asserted here:

* **Numbers that describe the test suite must match the suite.** A log claiming `N passed`
  is checked against a real collection.
* **A table's own arithmetic must hold.** A heading that says "N added" over a table of
  per-file counts must sum to N.
* **Nothing may be asserted and retracted in the same document.** A withdrawn claim may be
  *quoted* — the retraction itself has to quote it, and so does the verdict entry recording
  the block — but it may not be *asserted as bare prose*. Quoting is mention; unquoted is
  use. Matching is whitespace-normalised and inflection-tolerant, because both of those
  blind spots have already let a restatement through a full round.

These are cheap, mechanical, and they fail loudly on exactly the edit that keeps happening.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs" / "validation"
TESTS = Path(__file__).resolve().parent


def _logs() -> list[Path]:
    return sorted(DOCS.glob("[0-9][0-9]-*.md"))


def _collected_total() -> int:
    """How many tests pytest actually collects, right now."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS), "-q", "--collect-only"],
        capture_output=True, text=True,
    )
    # A collection error must not be reported as a documentation defect. Without this, an
    # import error anywhere in `tests/` drops the count and the assertion below says "the log
    # claims 245 but pytest collects 231" — blaming the log for a broken module.
    assert result.returncode == 0, (
        "pytest could not collect the suite, so the log's total cannot be checked. This is a "
        "test-suite failure, not a documentation one:\n" + result.stdout[-2000:]
    )
    return sum(1 for line in result.stdout.splitlines() if "::" in line)


def _frontmatter(path: Path) -> dict[str, str]:
    """The declared header every validation log must carry.

    Deliberately a hand parser rather than a YAML dependency: the header is `key: value` and
    nothing else, and a gate must not be able to fail for reasons unrelated to what it asserts.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---" + "\n"):
        return {}
    end_marker = text.find("\n" + "---", 4)
    if end_marker == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[4:end_marker].split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_every_log_declares_its_phase_and_status() -> None:
    """The header is mandatory, so it cannot be opted out of by not writing it.

    The previous design inferred "the phase in progress" from filesystem ordering
    (`_logs()[-1]`) and read the total out of prose. Both inferences failed:

    * the newest *file* is not the phase in progress. Between phase 10's first test landing
      and phase 10's log existing, the newest file is `09-runtime.md` — a closed,
      council-reviewed document whose own docstring says it must not be rewritten — and the
      gate held it to a live number. The only green paths were to rewrite history, or to
      author the next log before its first test.
    * the prose scan required backticks, so `08-tier2.md`'s **132 passed** in bold was
      invisible. A phase-10 log written in the house style used one phase earlier would
      silently disable the check, and a critic demonstrated exactly that — passing a log
      claiming 9999 and one claiming nothing at all.

    Declaration replaces inference. A missing header is a failure, not a skip.
    """
    problems: list[str] = []
    for path in _logs():
        fields = _frontmatter(path)
        if not fields:
            problems.append(
                f"{path.name}: no frontmatter. Every validation log must open with a "
                "`---` block declaring `phase:` and `status:`."
            )
            continue
        if "phase" not in fields:
            problems.append(f"{path.name}: frontmatter has no `phase:`")
        status = fields.get("status")
        if status not in {"closed", "in-progress"}:
            problems.append(
                f"{path.name}: `status:` is {status!r}, expected 'closed' or 'in-progress'"
            )
    assert not problems, "; ".join(problems)


def test_at_most_one_phase_is_in_progress() -> None:
    """Two phases in progress means nothing is, and the suite total has no owner."""
    live = [p.name for p in _logs() if _frontmatter(p).get("status") == "in-progress"]
    assert len(live) <= 1, f"more than one log claims to be in progress: {live}"


def test_the_suite_total_matches_whichever_log_owns_it() -> None:
    """The phase in progress owns today's total; otherwise the last closed log froze it.

    The second half is the useful one. With no phase open, the suite must equal the number
    the last closed log recorded — so **a test cannot be added without opening the next
    phase's log**. That is a correct property, and it converts the old design's unavoidable
    red into a red whose remedy is one line and touches no closed prose.
    """
    total = _collected_total()
    logs = _logs()
    if not logs:
        return

    in_progress = [p for p in logs if _frontmatter(p).get("status") == "in-progress"]
    if in_progress:
        path = in_progress[0]
        declared = _frontmatter(path).get("suite_total")
        assert declared is not None, (
            f"{path.name} is the phase in progress but declares no `suite_total:`"
        )
        assert int(declared) == total, (
            f"{path.name} is the phase in progress: its frontmatter declares "
            f"suite_total: {declared}, but pytest collects {total}."
        )
        return

    frozen = [
        (p, _frontmatter(p)) for p in logs
        if _frontmatter(p).get("status") == "closed" and _frontmatter(p).get("suite_total")
    ]
    if not frozen:
        return
    path, fields = frozen[-1]
    assert int(fields["suite_total"]) == total, (
        f"no log is in-progress, so the suite is frozen at the total the last closed log "
        f"({path.name}) recorded: {fields['suite_total']}. pytest collects {total}. Open the "
        "next phase's log with `status: in-progress` before adding tests."
    )


def test_prose_totals_agree_with_the_declared_one() -> None:
    """A count written in prose must match the header, in **any** markup.

    Markup-blind on purpose: the old regex required backticks, so bold, italic or plain
    silently disabled it.

    Two exclusions, each for a legitimate way a log talks about counts that are not the
    current suite total:

    * **history** — "up from 132", "more than 75". A log narrates growth across rounds, and
      the figure it *leaves standing* is the one that has to be true.
    * **parentheticals** — "(7 passed with a tripped breaker.json in CWD)". A parenthesis
      qualifies a local statement; this one describes one file under one condition. Counting
      it as a claim about the whole suite would make the gate unusable, and a gate people
      route around is worse than none.

    The honest limit: a stale total written *outside* a parenthesis and *not* introduced as
    history is caught; one written inside one is not. That is narrower than "every number in
    the document", and it is stated here rather than left for a critic to find.
    """
    problems: list[str] = []
    for path in _logs():
        declared = _frontmatter(path).get("suite_total")
        if declared is None:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(\d+)\s+passed\b", text):
            before = text[max(0, match.start() - 24):match.start()]
            if re.search(r"(?:from|than|was)\s*\**\s*$", before):
                continue
            # Paragraph-scoped, not line-scoped: markdown wraps at ~95 columns, so the
            # closing parenthesis of an aside routinely lands on the next line. A
            # line-scoped balance check missed exactly that and reported a false positive.
            para_start = text.rfind("\n\n", 0, match.start()) + 2
            para_end = text.find("\n\n", match.end())
            para = text[para_start: para_end if para_end != -1 else len(text)]
            offset = match.start() - para_start
            if para[:offset].count("(") > para[:offset].count(")") and ")" in para[offset:]:
                continue  # a parenthetical aside, not a claim about the suite
            if match.group(1) != declared:
                problems.append(
                    f"{path.name}: prose says {match.group(1)} passed, frontmatter declares "
                    f"{declared}"
                )
    assert not problems, "; ".join(problems)


def test_the_send_accounting_matches_its_own_evidence() -> None:
    """The credits headline must equal the table under it, and the table must equal reality.

    This is the money number — the brief says "log the count you spent" — and it drifted in
    **three consecutive rounds**: 22 vs 26 in round 12, "roughly two sends" over-run when it
    was six in round 13, and a breakdown of "3 + 4" for a capture that actually holds 2 + 2.
    Every one was found by a critic reading prose. Three corrections in three rounds is a
    missing mechanism, not carelessness, and the other counts in this log (suite totals, the
    per-file census) have had gates for rounds while this one did not.

    Three properties, because the failures were different each time:

    * the bolded total equals the sum of the table below it — catches round 12's 22-vs-26;
    * a row citing an evidence file matches what that file actually contains — catches round
      13's 3+4 breakdown of a capture holding 2+2;
    * a row citing the 410 table matches its real row count — the runtime figure has drifted
      twice before (7 vs 10, and 13 vs 17).
    """
    log = DOCS / "09-runtime.md"
    if not log.exists():
        return
    text = log.read_text(encoding="utf-8")

    match = re.search(r"\*\*(\d+) sends by this phase\*\*:?\s*\n\n((?:\|.*\n)+)", text)
    assert match, (
        "09-runtime.md no longer carries a '**N sends by this phase**' heading over a "
        "per-source table. That table is what makes the credits figure checkable; if the "
        "accounting moved, move this gate with it rather than deleting it."
    )
    claimed = int(match.group(1))
    rows = [row for row in match.group(2).splitlines() if row.startswith("|")][2:]

    counts: list[int] = []
    problems: list[str] = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 3 or not cells[1].isdigit():
            continue
        count, source, derivation = int(cells[1]), cells[0], cells[2]
        counts.append(count)

        # Rows that name an artefact are checked against the artefact.
        artefact = re.search(r"`(evidence/[^`]+)`", derivation)
        if artefact:
            path = DOCS / artefact.group(1)
            if not path.exists():
                problems.append(f"{source}: cites {artefact.group(1)}, which does not exist")
                continue
            # A `$`-prefixed line is an invocation; the `next:` hint printed by `test start`
            # names the command without running it and must not be counted.
            actual = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.lstrip().startswith("$") and "test say" in line
            )
            if actual != count:
                problems.append(
                    f"{source}: the table says {count} but {artefact.group(1)} records "
                    f"{actual} `test say` invocations"
                )
        elif "410 evidence table" in derivation:
            table = re.search(r"\| Attempt \| Result \|\s*\n\|[-| ]+\n((?:\|.*\n)+)", text)
            actual = len([r for r in table.group(1).splitlines() if r.startswith("|")]) if table else 0
            if actual != count:
                problems.append(
                    f"{source}: the table says {count} but the 410 evidence table has "
                    f"{actual} rows"
                )

    assert counts, "the send-accounting table has no numeric rows"
    if sum(counts) != claimed:
        problems.append(
            f"the heading claims {claimed} sends but the table sums to {sum(counts)} "
            f"({counts})"
        )
    assert not problems, "\n".join(problems)


def _flat(text: str) -> str:
    """Text with every run of whitespace collapsed to one space.

    **This is the whole reason the gate works.** Markdown wraps prose at ~95 columns, so a
    claim like "that needs a bot, which the plan ceiling prevents" is split across two lines
    in the file. A round-7 fix searched for the unwrapped string, found nothing, and reported
    the claim removed — while it sat in the document for another full round until three
    critics found it. Any check that matches raw file text has that blind spot.
    """
    return " ".join(text.split())


#: (file, withdrawn wording). Every entry is asserted to match something by
#: `test_every_sentinel_matches_something` — a sentinel that matches nothing is a check that
#: cannot fail, which is how an early version shipped two dead rows a critic had to find.
RETRACTED: list[tuple[str, str]] = [
    ("09-runtime.md", "test-session messages are not metered"),
    ("09-runtime.md", "that needs a bot, which the plan ceiling prevents"),
    ("09-runtime.md", "no session was created."),
    # Round 9/10. Both were asserted as findings and are withdrawn: a probe carrying no
    # credential at all returns the same 410, so the runtime evidence does not discriminate
    # on credentials, and the avenues were not exhausted when that was written.
    ("09-runtime.md", "close the last cheap avenue"),
    (
        "09-runtime.md",
        "leans toward the credential not authenticating on the runtime host at all",
    ),
    # Round 11. Round 9 inferred this from the no-credential probe; it does not follow, and
    # the body was corrected in round 10 while the deviations register was not. Sentinelled
    # because the register is what later phases inherit.
    ("09-runtime.md", "is not a credential verdict"),
]

#: A word's last few characters may change without the claim changing. `close`/`closes`/
#: `closing`/`closed` are one claim; matching only the literal let `closes` through a full
#: round. Rather than model morphology, the last two characters of any word over four
#: letters are treated as free — over-matching is the safe direction here, because a false
#: positive costs one pair of quotation marks and a false negative costs a round.
_MIN_STEM = 4
_TAIL = 2

#: Everything that is not a letter or digit, and every homoglyph, is noise.
#:
#: This replaced a hyphen-only rule, and the replacement is the whole lesson. Round 13 fixed
#: `test session` vs `test-session` by normalising hyphens — an instance fix — and a critic
#: then measured **six more interior mutations escaping at 95%**: an apostrophe
#: (`message's`), an internal period (`mess.ages`), an em dash, a zero-width space, a doubled
#: letter (`messsages`), and a Cyrillic homoglyph (`mеssages`). None is a paraphrase; every
#: one is a literal a literal matcher should catch.
#:
#: The root cause was never hyphens. It is that the matcher compares *characters*, so any
#: character inserted, doubled or swapped inside a word defeats it, and patching one
#: character class at a time is an endless game. Both sides are therefore reduced to a
#: **skeleton**: Unicode-normalised, homoglyphs folded, non-alphanumerics dropped, repeated
#: letters collapsed. `test-session`, `test session`, `test—session` and `test/session` all
#: become `testsesion`, and the comparison stops depending on punctuation at all.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_REPEATS = re.compile(r"(.)\1+")

#: Letters that look identical but are different codepoints. NFKD does not fold these — a
#: Cyrillic `е` stays a Cyrillic `е` — so they are mapped explicitly.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",   # Cyrillic
    "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "ɡ": "g", "ν": "v", "ο": "o",   # Cyrillic/Greek
    "α": "a", "ι": "i", "κ": "k", "μ": "u", "τ": "t",                       # Greek
})

#: Markdown emphasis may sit *between* the words of a claim or *inside* one: both
#: `*closes* the last cheap avenue` and `clos**es** the last cheap avenue` are the same
#: assertion. Emphasis characters are therefore deleted outright before matching rather than
#: allowed for in the pattern, which only ever handled the between-words case. Backticks are
#: **not** stripped — they are quote delimiters and `_is_quoted` needs them.
_EMPHASIS = re.compile(r"[*_~]")

#: Paragraph boundary. Delimiters are paired **within one paragraph**, never across the file.
#:
#: Document-wide left-to-right pairing was measured escaping on 7.1% of insertion points: a
#: ``` fence contributes an odd backtick that poisons parity for everything after it, so a
#: bare-prose restatement anywhere downstream read as "quoted". Stripping fences fixed that
#: instance and left 18 more, because *any* unbalanced delimiter has the same effect.
#:
#: A proximity window was tried instead and is recorded here so it is not tried again: no
#: window is both correct and complete. At 12 characters it rejects the document's own
#: legitimate quotations; at 20 and above it lets 6-29% of restatements through, because
#: this log is dense with inline code and something backticked is nearly always nearby. That
#: is the round-8 lesson repeating — presuming innocence from proximity does not work.
#:
#: Paragraph scoping fixes the actual defect: unbalanced punctuation can only corrupt its own
#: paragraph, and a quotation does not span a blank line. Fenced blocks are excluded, because
#: captured command output is not prose making a claim.
#:
#: This comment used to end with "Measured over 3,645 insertion points across nine restatement
#: forms: zero escapes" — a figure that was already stale when a critic read it, disagreed with
#: the log's own copy of the same measurement, and stayed stale through the round that claimed
#: to have fixed it. **No count is quoted here now.** The property is asserted by
#: `test_the_gate_has_no_undeclared_escapes`, and `scripts/sweep_retraction_gate.py --check`
#: sweeps the real log; both report today's number rather than repeating yesterday's.
_PARAGRAPH = re.compile(r"\n\s*\n")


def _stem(word: str) -> str:
    """`word` with its inflectable tail removed, if it is long enough to have one."""
    return word[: -_TAIL] if len(word) > _MIN_STEM else word


def _skeleton(text: str) -> tuple[str, list[int]]:
    """`text` reduced to letters and digits, plus a map back to the original offsets.

    The map is what keeps `_is_quoted` honest: matching happens on the skeleton, but whether
    a claim sits inside quotation marks is a fact about the *original* text, where the
    quotation marks still exist. `positions[i]` is the index in `text` that skeleton
    character `i` came from.
    """
    folded = unicodedata.normalize("NFKD", text).lower().translate(_CONFUSABLES)
    characters: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(folded[: len(text)]):
        if character.isalnum() and character.isascii():
            # Collapse a run of the same letter: `messsages` and `messages` are one claim.
            if characters and characters[-1] == character:
                continue
            characters.append(character)
            positions.append(index)
    return "".join(characters), positions


def _pattern(wording: str) -> "re.Pattern[str]":
    """A regex matching `wording`'s skeleton, with each word's tail free.

    Four escapes have now been found by hand in prose this was supposed to catch, each one a
    narrower version of the same mistake — **describing the wording that happened to be
    withdrawn rather than the claim**:

    * `closes` against a sentinel of `close` — exact-literal matching (round 10);
    * `closing`, and `clos**es**` — a fixed suffix list and a between-words gap (round 11);
    * `test session` against `test-session` — hyphens (round 13);
    * apostrophes, internal periods, em dashes, zero-width spaces, doubled letters and
      homoglyphs — everything else that can sit inside a word (round 13, same critic, after
      the hyphen fix shipped).

    Matching on the skeleton ends that sequence: there is no punctuation left to normalise.
    Words are joined by `\\w{0,4}` rather than a space because the skeleton has no spaces —
    the bound keeps a claim from matching across an unrelated span of the document.
    """
    words = _flat(_EMPHASIS.sub("", wording)).lower().split()
    stems = [re.escape(_skeleton(_stem(word))[0]) for word in words if _skeleton(word)[0]]
    return re.compile(r"\w{0,4}".join(stems))


def _strip_fences(text: str) -> str:
    """Replace fenced code blocks with spaces, preserving every offset.

    **This is load-bearing.** `_is_quoted` pairs backticks left to right; a ``` fence
    contributes three, two of which pair off and one of which dangles. A critic measured the
    consequence: the dangling tick made every position from that fence onward read as
    "quoted", so **7.1% of insertion points in this document escaped the gate entirely** —
    including inside the very section carrying three of the sentinels. Removing fenced
    regions before pairing removes the corruption at its source rather than compensating for
    it, and nothing inside a code fence is prose making a claim anyway.
    """
    out = list(text)
    for match in re.finditer(r"```.*?```", text, re.DOTALL):
        for index in range(match.start(), match.end()):
            if out[index] != "\n":
                out[index] = " "
    return "".join(out)


def _paragraphs(text: str) -> list[str]:
    """The document as normalised paragraphs: fences removed, emphasis deleted, wrap undone.

    Matching and quote-detection both run per paragraph, so a claim and the delimiters
    around it are always read the same way and neither can be corrupted by punctuation
    elsewhere in the file. See `_PARAGRAPH` for why the scope is the paragraph.
    """
    stripped = _EMPHASIS.sub("", _strip_fences(text))
    return [_flat(part) for part in _PARAGRAPH.split(stripped) if part.strip()]


def _is_quoted(paragraph: str, position: int, length: int) -> bool:
    """Is the span at `position` inside a pair of delimiters **in this paragraph**?

    The discipline this enforces: a log may *quote* a claim it has withdrawn — the body
    retraction and the round entry that records the block both need to — but it may not
    *assert* it. Quoting is mention; bare prose is use, and delimiters are what separate the
    two mechanically.

    Pairing is strict and left to right, which is only sound because the scope is one
    paragraph.
    """
    end = position + length
    for opener, closer in (('"', '"'), ("`", "`"), ("“", "”")):
        cursor = 0
        while (start := paragraph.find(opener, cursor)) != -1:
            stop = paragraph.find(closer, start + 1)
            if stop == -1:
                break
            if start < position and end <= stop:
                return True
            cursor = stop + 1
    return False


def bare_prose_hits(text: str, wording: str) -> list[str]:
    """Every place `wording` is ASSERTED (not quoted) in `text`, as human-readable strings.

    **Public, and the single implementation.** The gate below calls it, and so does
    `scripts/sweep_retraction_gate.py`. The script previously kept its own copy of this loop,
    and a critic predicted exactly what happened next: the gate moved to skeleton matching,
    the copy did not, and the sweep reported every form escaping when none did. A measurement
    that re-implements what it measures is measuring the wrong thing.

    So there is one loop. If it is wrong, the gate and the sweep are wrong together and
    visibly, rather than disagreeing quietly.
    """
    pattern = _pattern(wording)
    hits: list[str] = []
    for index, paragraph in enumerate(_paragraphs(text)):
        # Match on the skeleton, judge quoting on the paragraph. `offsets` maps skeleton
        # positions back, because the delimiters this gate cares about are exactly the
        # characters the skeleton drops.
        skeleton, offsets = _skeleton(paragraph)
        for match in pattern.finditer(skeleton):
            if not offsets:
                continue
            position = offsets[match.start()]
            length = offsets[min(match.end(), len(offsets)) - 1] + 1 - position
            if not _is_quoted(paragraph, position, length):
                hits.append(
                    f"paragraph {index}: "
                    f"...{paragraph[max(0, position - 60):position + 90]}..."
                )
    return hits


def test_no_log_asserts_a_claim_it_also_retracts() -> None:
    """A withdrawn claim may be QUOTED as often as the narrative needs, never ASSERTED.

    Phase 09 retracted claims in its body and left them standing in the verdict register,
    twice; phase 08 was blocked on the same shape. The rule that makes this checkable is a
    writing discipline: **a retraction must quote the wording it withdraws**. Quoting is
    mention, bare prose is use, and `_is_quoted` separates the two mechanically.

    Three designs have now failed here, each in a way a critic had to find:

    1. *"a retraction marker within 1000 characters"* — worthless; 41% of positions in the
       file have such a marker nearby, so a restatement was presumed innocent. Two of its
       three sentinels also matched nothing, so those rows could never fire.
    2. *raw-text matching* — markdown wraps prose at ~95 columns, so a claim split across
       two lines was invisible. Fixed by `_flat`.
    3. *exact-literal matching* — the sentinel read `close the last cheap avenue` and the
       standing assertion read `clos**es** the last cheap avenue`. One inflection, and two
       critics had to find by hand the very defect this test exists to find. Fixed by
       `_pattern`, which lets any word carry a different verb or plural ending.

    The through-line: every version was narrower than the class it claimed to cover, and the
    narrowing was invisible because the test was green. Hence
    `test_every_sentinel_matches_something` below — a sentinel that matches nothing is a row
    that cannot fail, and it is asserted rather than assumed.
    """
    problems: list[str] = []
    for filename, wording in RETRACTED:
        path = DOCS / filename
        if not path.exists():
            continue
        for hit in bare_prose_hits(path.read_text(encoding="utf-8"), wording):
            problems.append(
                f"{filename}: {wording!r} appears as bare prose in {hit}\n"
                "A withdrawn claim may be QUOTED (in backticks or quotation marks) as often "
                "as the narrative needs — the body retraction and the verdict entry both "
                "legitimately do — but asserting it unquoted is the claim standing again."
            )
    assert not problems, "\n".join(problems)


def test_the_gate_has_no_undeclared_escapes() -> None:
    """The gate's own escape rate, asserted rather than measured-and-written-down.

    Three rounds running, this gate's coverage was described in prose and the prose was
    wrong: a figure that said 3,645 in one file and 10,917 in another; then one that said
    8,224 and was stale inside the round that wrote it, because it counts lines of the very
    document it lives in. A number that describes a document, transcribed into that document,
    is drift waiting to happen — and no gate could see it, because the log gates only check
    suite totals and table arithmetic.

    So no number is claimed. What is asserted is the **property**: every restatement form is
    caught, except the ones declared unfixable.

    The forms are exercised against a compact fixture rather than the real log, deliberately:
    the property belongs to the *matcher*, not to one document, and a fixture makes it a
    millisecond check that runs on every commit instead of a two-minute one that runs when
    someone remembers. `scripts/sweep_retraction_gate.py` sweeps the real log for when the
    matcher changes.
    """
    claim = "test-session messages are not metered"
    #: Each is a bare-prose assertion of a withdrawn claim, mutated the way a human or a
    #: copy-paste actually mutates text. Every one escaped some earlier version of this gate.
    mutations = {
        "verbatim": claim,
        "inflected": "test-session messages are not metering",
        "de-hyphenated": "test session messages are not metered",
        "emphasis-inner": "test-session mess**ages** are not metered",
        "emphasis-outer": "test-session *messages* are not metered",
        "apostrophe": "test-session message's are not metered",
        "internal-period": "test-session mess.ages are not metered",
        "em-dash": "test—session messages are not metered",
        "zero-width": "test-session mess​ages are not metered",
        "doubled-letter": "test-session messsages are not metered",
        "homoglyph": "test-session mеssages are not metered",
        "slash": "test/session messages are not metered",
        "camel-case": "testSession messages are not metered",
    }
    #: Contexts a restatement can hide in. The fenced block is the one place it legitimately
    #: may sit — captured output is not prose making a claim.
    def document(text: str) -> str:
        return (
            f'A paragraph quoting "{claim}" as a retraction.\n\n'
            "```\nsome captured output\n```\n\n"
            f"{text}\n\n"
            "A trailing paragraph with `inline code` in it.\n"
        )

    escaped = [name for name, text in mutations.items()
               if not bare_prose_hits(document(text), claim)]
    assert not escaped, (
        f"these restatement forms are NOT caught: {escaped}. Each is a bare-prose assertion "
        "of a withdrawn claim; the gate must flag every one."
    )

    # And the other direction: quoting must still be allowed, or the gate is unusable.
    quoted = f'The log withdraws "{claim}" here.\n'
    assert not bare_prose_hits(quoted, claim), (
        "a properly quoted retraction is being flagged — the gate would force the log to "
        "stop quoting the claims it withdraws, which is the opposite of the discipline"
    )
    fenced = f"```\n{claim}\n```\n"
    assert not bare_prose_hits(fenced, claim), (
        "text inside a fenced code block is captured output, not a claim"
    )


def test_every_sentinel_matches_something() -> None:
    """A sentinel that matches nothing is a row of the gate above that can never fire.

    Split out from the gate itself so the failure says which it is: a stale sentinel is a
    maintenance problem, a bare-prose restatement is a documentation defect, and reporting
    both through one assertion is how an early version's two dead rows went unnoticed.
    """
    problems: list[str] = []
    for filename, wording in RETRACTED:
        path = DOCS / filename
        if not path.exists():
            problems.append(f"{filename}: no such log, so {wording!r} guards nothing")
            continue
        paragraphs = _paragraphs(path.read_text(encoding="utf-8"))
        pattern = _pattern(wording)
        if not any(pattern.search(_skeleton(paragraph)[0]) for paragraph in paragraphs):
            problems.append(
                f"{filename}: the sentinel {wording!r} matches nothing, so that row of the "
                "retraction gate cannot fail. Either the retraction stopped quoting the "
                "wording it withdraws, or the sentinel is stale and should be removed."
            )
    assert not problems, "\n".join(problems)


def test_a_quantity_heading_sums_to_the_table_under_it() -> None:
    """A heading of "**N <noun> by this phase**" over a per-file table must add up.

    **This gate was deleted by accident in round 14 and the defect it guards reappeared in
    the same commit.** Splicing the frontmatter rewrite into this file removed
    `test_added_by_this_phase_tables_sum_to_their_heading` along with the two tests it sat
    between; the module docstring went on advertising the property; and the census then
    shipped a heading of 173 over a table summing to 168, missing the two test files round 14
    had itself added. A critic found it by adding up the rows.

    So it is back, and generalised — any `**N <noun> by this phase**` heading, not just
    "added" — because the round-14 log introduced a second such heading (`sends`) and a rule
    written for one noun would not have covered it.

    The arithmetic property is the one that mattered historically: rounds 6 and 7 shipped
    107/109 over a table of 124. `test_a_quantity_is_stated_once_per_document` catches
    *duplication*; this catches *contradiction*. They are different defects and both have
    occurred.
    """
    heading = re.compile(r"\*\*(\d+)\s+[a-z]+\s+by this phase\*\*:?\s*\n\n((?:\|.*\n)+)")
    problems: list[str] = []
    for path in _logs():
        for match in heading.finditer(path.read_text(encoding="utf-8")):
            claimed = int(match.group(1))
            rows = [row for row in match.group(2).splitlines() if row.startswith("|")]
            counts: list[int] = []
            for row in rows[2:]:  # skip header and separator
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                if len(cells) >= 2 and cells[1].isdigit():
                    counts.append(int(cells[1]))
            if counts and sum(counts) != claimed:
                problems.append(
                    f"{path.name}: heading says {claimed}, table sums to {sum(counts)} "
                    f"({counts})"
                )
    assert not problems, "; ".join(problems)


def test_a_quantity_is_stated_once_per_document() -> None:
    """A census printed twice is two censuses, whether or not the rows drifted.

    The duplicate-table check next to this one compares the header AND the first data row
    byte-for-byte, which misses the exact defect it was written for: phase 09 round 7 shipped
    a census twice with **different sums** (109 and 124), and a differing row is the whole
    point of an insert-that-never-deleted. A critic reconstructed that shape and both gates
    stayed green.

    Matching table bodies was the wrong property. A document may state "N added by this
    phase" **once**; two of them is a duplicate regardless of what the rows say, and it needs
    no exception list — the byte-identical rule needed one to dodge phase 05's two legitimate
    `| Round | Result |` tables under one heading.
    """
    heading = re.compile(r"\*\*(\d+)\s+([a-z]+)\s+by this phase\*\*")
    problems: list[str] = []
    for path in _logs():
        seen: dict[str, list[str]] = {}
        for match in heading.finditer(path.read_text(encoding="utf-8")):
            seen.setdefault(match.group(2), []).append(match.group(1))
        for noun, counts in seen.items():
            if len(counts) > 1:
                problems.append(
                    f"{path.name}: {len(counts)} '**N {noun} by this phase**' headings "
                    f"({counts}) — a census printed twice"
                )
    assert not problems, "; ".join(problems)


def test_no_table_is_printed_twice_under_one_heading() -> None:
    """The literal round-7 defect: an inserted correction that never deleted the original.

    The property is **not** "no table header repeats in the file" — a first attempt asserted
    that and false-positived on four logs, because `| Round | Result |` legitimately appears
    once per section and separator rows repeat by construction. The defect shape is narrower:
    the *same* table header twice **under one heading**, which is what an insert-without-
    delete produces.
    """
    problems: list[str] = []
    for path in _logs():
        section = ""
        seen: dict[str, str] = {}  # header -> its first data row, within one section
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("#"):
                section, seen = line, {}
                continue
            is_separator = line.strip().startswith("|") and set(line.strip()) <= set("|-: ")
            if not (is_separator and index >= 1 and lines[index - 1].startswith("|")):
                continue
            header = lines[index - 1]
            first_row = lines[index + 1] if index + 1 < len(lines) else ""
            # Same header AND same first row = one table printed twice, which is exactly what
            # an insert-that-never-deleted produces. Two tables sharing only a header are
            # normal: phase 05 has a detailed verdict table and a summary one, both
            # `| Round | Result |` with different content. Comparing the first row separates
            # the defect from the idiom without hand-listing exceptions.
            if seen.get(header) == first_row:
                problems.append(
                    f"{path.name}: the table {header!r} starting {first_row!r} is printed "
                    f"twice under {section!r} — an insert that never deleted the original"
                )
            seen[header] = first_row
    assert not problems, "\n".join(problems)



if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} validation-log tests passed.")
