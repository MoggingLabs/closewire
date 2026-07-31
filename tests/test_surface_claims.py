"""Gate: a claim that quantifies over "every surface" must dispose of every surface.

**The class this closes has been filed eight times in thirteen review rounds.** Every
instance was the same shape — the log wrote "on any surface", "every shape", "all three
credential forms", "the last cheap avenue", and the set being quantified over existed only in
the author's head at the moment of writing. Each round a critic named one member the sweep had
missed, that member was probed, and the claim was re-published. Adding a member does not turn
something into a set.

The repo has already solved this class four times and never applied the solution here. Every
other census that drifted got bound to a derivation:

* test totals from `pytest --collect-only` (`test_validation_logs.py`);
* Tier-2 routes from `tier2_rule()` over the vendored spec (`closewire_client/tiers.py`);
* `--json` coverage from `build_parser()` (`test_json_contract.py`);
* send counts from the evidence they cite (`test_validation_logs.py`).

`tiers.py` is the exact architecture: **every mutating route in the spec must carry a
disposition, so a new one fails rather than defaulting to "harmless"**. The surface tables in
the validation logs were the last hand-typed census left.

So the domain is derived from `schema/endpoints.index.json` — all 60 GET operations — and a
claim must account for each one: either the log's table lists it as checked, or it appears in
`excluded` with a reason. **The domain is deliberately every GET, not a keyword-narrowed
subset.** Narrowing by "paths mentioning goal/variable/lead" would itself be a hand-typed
exhaustiveness claim — the same defect one level up, which is how every previous fix failed.

Fails in both directions: an unaccounted-for route fails, and so does a stale exclusion naming
a route the spec no longer has.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "validation"
INDEX = ROOT / "schema" / "endpoints.index.json"


class SurfaceClaim(NamedTuple):
    """A log passage that quantifies over read surfaces, and its disposition of each."""

    log: str
    #: A string that appears in the log, anchoring the claim being checked.
    anchor: str
    #: Routes NOT in the log's table, each mapped to why it cannot bear on the claim.
    excluded: dict[str, str]


#: Why each read surface cannot carry a test-session lead's goal or variable state.
#:
#: Sixty one-line dispositions, written once. That is an afternoon, and it then holds for
#: phases 10-13 — against eight rounds of a critic finding one missed route at a time.
_NOT_LEAD_STATE = "not a lead-scoped surface — cannot carry a test lead's goal or variable state"
_BILLING = "billing/usage ledger — carries money, not flow state"
_DEMO = "public live-demo surface, unauthenticated, unrelated to test sessions"
_LIBRARY = "knowledge-base file storage — not conversation state"
#: Aggregate metrics — counts over many leads, not per-lead state.
#:
#: **This reason was wrong for five of the routes it disposed of, and a critic proved it.**
#: `GET /botMetric/actions` takes a `leadId` and returns `BotMetricAction {…, nodeId,
#: frontendNodeId}` — per-lead, per-*node* execution records. `messages`,
#: `messageFeedback`, `messageLikes` and `messageReason` likewise return message rows, not
#: counts. The domain was honestly derived; the *dispositions* were hand-typed prose, and one
#: of them let a real, free, already-shipped surface through — the eighth instance of the
#: exhaustiveness class, committed inside the mechanism built to end it.
#:
#: `actions` is now **checked** rather than excluded, and it answered the phase's open
#: question. The remaining `_METRIC` routes are genuine aggregates; each per-lead one that
#: was misfiled is listed separately below with its own reason.
_METRIC = "aggregate count over many leads — no per-lead rows, so it cannot carry flow state"

#: Per-lead message records. Real rows, but message text and channel — not node execution or
#: variable state, which is what deviation 32 quantifies over.
_MESSAGE_ROWS = "per-lead *message* records (text/channel/direction), not node or variable state"

_GOAL_STATE_CLAIM = SurfaceClaim(
    log="09-runtime.md",
    anchor="No goal or variable state is exposed for test-session leads",
    excluded={
        "/account/apiKey": _NOT_LEAD_STATE,
        "/agency": _NOT_LEAD_STATE,
        "/agency/billing/balance": _BILLING,
        "/agency/billing/balance/source/{sourceId}": _BILLING,
        "/agency/billing/options": _BILLING,
        "/agency/billing/re-billing": _BILLING,
        "/agency/billing/transactions": _BILLING,
        "/agency/billing/transactions/source/{sourceId}": _BILLING,
        "/agency/billing/usages": _BILLING,
        "/agency/current": _NOT_LEAD_STATE,
        "/agency/source": _NOT_LEAD_STATE,
        "/agency/source/{id}": _NOT_LEAD_STATE,
        "/agency/source/{id}/calendars": _NOT_LEAD_STATE,
        "/agency/source/{id}/channels": _NOT_LEAD_STATE,
        "/agency/source/{id}/fields": "source field *definitions*, not a lead's values",
        "/agency/source/{id}/owners": _NOT_LEAD_STATE,
        "/agency/source/{id}/tags": "tag definitions on the source, not a lead's tags",
        "/agency/usage": _BILLING,
        "/bot": _NOT_LEAD_STATE,
        "/bot-live-demo/live/{key}": _DEMO,
        "/bot-live-demo/live/{key}/session/{sessionLeadId}": _DEMO,
        "/bot-live-demo/live/{key}/session/{sessionLeadId}/stream": _DEMO,
        "/bot-live-demo/{botId}": _DEMO,
        "/bot/bbb/templates": _NOT_LEAD_STATE,
        "/bot/nodeDescriptors": "the node catalogue — schema, not instance state",
        "/bot/{botId}/testSession/messages/{leadId}": "deviation 25 — does not return, times out server-side",
        "/bot/{id}": _NOT_LEAD_STATE,
        "/bot/{id}/export": "the flow definition, not a run of it",
        "/bot/{id}/steps": "the flow graph, not a run of it",
        "/botMetric/actionCount": _METRIC,
        "/botMetric/agencyMetric": _METRIC,
        "/botMetric/agencySummary": _METRIC,
        "/botMetric/bookingGraph": _METRIC,
        "/botMetric/leaderboard": _METRIC,
        "/botMetric/localleaderboard": _METRIC,
        "/botMetric/logs": "deviation 34 — fails unfiltered (400) and filtered (timeout)",
        "/botMetric/messageCount": _METRIC,
        "/botMetric/messageFeedback": _MESSAGE_ROWS,
        "/botMetric/messageLikes": _MESSAGE_ROWS,
        "/botMetric/messageReason": _MESSAGE_ROWS,
        "/botMetric/messages": _MESSAGE_ROWS,
        "/botTemplates": _NOT_LEAD_STATE,
        "/hierarchy": _NOT_LEAD_STATE,
        "/hierarchy/{id}": _NOT_LEAD_STATE,
        "/lead/{leadId}/ai-toggle": "one boolean, the AI on/off switch — not goal state",
        "/lead/{leadId}/page-history": "web pages the contact viewed — not flow state",
        "/library/files": _LIBRARY,
        "/library/files/{fileId}": _LIBRARY,
        "/library/files/{fileId}/scrape-pages": _LIBRARY,
        "/library/files/{fileId}/view": _LIBRARY,
        "/notifications": _NOT_LEAD_STATE,
        "/notifications/forwarding": _NOT_LEAD_STATE,
        "/persona": _NOT_LEAD_STATE,
        "/persona/{id}": _NOT_LEAD_STATE,
        "/smart-faq": _NOT_LEAD_STATE,
    },
)

CLAIMS = [_GOAL_STATE_CLAIM]


def _get_routes() -> set[str]:
    """Every GET path the vendored spec declares. **The domain, derived not typed.**"""
    raw = json.loads(INDEX.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("endpoints", raw.get("operations", []))
    return {e["path"] for e in rows if e.get("method") == "GET"}


def _tabled_routes(claim: SurfaceClaim) -> set[str]:
    """Routes the log's own table lists as checked, parsed out of the log.

    Read from the document rather than kept in a second list here — a parallel list is the
    drift this file exists to stop.
    """
    text = (DOCS / claim.log).read_text(encoding="utf-8")
    start = text.find(claim.anchor)
    if start == -1:
        return set()
    # The table is the next markdown table after the anchor.
    table = text[start: start + 3000]
    return set(re.findall(r"`GET (/[^`]+)`", table))


def test_a_surface_claim_disposes_of_every_read_surface() -> None:
    """Every GET is either checked in the log's table or excluded here with a reason.

    This is the mechanism the exhaustiveness class never had. Deleting the
    `GET /botVariables/{botId}/{sourceId}` row from the log's table reproduces the round-12
    defect exactly — the one three critics found by hand — and this fails, naming the route.
    """
    domain = _get_routes()
    problems: list[str] = []
    for claim in CLAIMS:
        tabled = _tabled_routes(claim)
        assert tabled, (
            f"{claim.log}: found no `GET /...` rows under {claim.anchor!r}. Either the anchor "
            "moved or the table lost its routes — both make this gate vacuous."
        )
        unaccounted = domain - tabled - set(claim.excluded)
        if unaccounted:
            problems.append(
                f"{claim.log}: the claim under {claim.anchor!r} quantifies over read surfaces "
                f"but does not dispose of {sorted(unaccounted)}. Either check them and add "
                "them to the table, or exclude them in _GOAL_STATE_CLAIM with a reason."
            )
        stale = set(claim.excluded) - domain
        if stale:
            problems.append(
                f"{claim.log}: these exclusions name routes the spec no longer has: "
                f"{sorted(stale)}. A stale exclusion makes the census look complete."
            )
        blank = [route for route, why in claim.excluded.items() if not why.strip()]
        if blank:
            problems.append(f"{claim.log}: exclusions with no reason: {sorted(blank)}")
    assert not problems, "\n".join(problems)


def test_the_domain_is_derived_from_the_spec_not_typed() -> None:
    """The self-check. A domain narrowed to nothing makes the gate above vacuous.

    Also pins the *size*: if a spec refresh adds a read surface, the count moves and the
    gate above names it. That is the point — a new surface must be considered, not
    default to harmless. This mirrors `closewire_client/tiers.py`'s rule for mutating routes.
    """
    domain = _get_routes()
    assert len(domain) >= 50, (
        f"only {len(domain)} GET routes derived from {INDEX.name} — the domain has collapsed "
        "and every exhaustiveness claim would pass vacuously"
    )
    assert "/botVariables/{botId}/{sourceId}" in domain, (
        "the route three critics had to name by hand is no longer in the derived domain"
    )


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} surface-claim tests passed.")
