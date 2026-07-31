"""Phase-05 verification: exercise EVERY Tier-0 read against a live account.

Read-only. Prints the numbers needed to cross-check each call against the Closebot UI.

Three properties this script is built to have, each of which it lacked in round 1:

* **Complete.** It calls every public function in the five curated modules, and asserts
  that it did — a function added later without a probe here fails the run.
* **Fails.** Exits non-zero when anything fails or leaks. A leaked client credential
  exiting 0 is worse than no check at all.
* **Detects leaks past row 0.** The secret assertion walks the whole structure instead of
  looking at the first occurrence of each field name.

    python scripts/verify_reads.py

Takes several minutes: every call goes through the Pacer's read lane.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closewire_client.console import configure_streams

configure_streams()

from closewire_client.config import load_config
from closewire_client.endpoints import bots, leads, metrics, personas, sources
from closewire_client.redaction import find_unredacted
from closewire_client.errors import ClosewireError
from closewire_client.rest import RestClient

FAILURES: list[str] = []
EXERCISED: set[str] = set()

#: Server-side failures already documented in docs/validation/05-read-client.md, keyed by
#: label AND the exact failure kind. A label alone is not enough: `metrics.logs` timing out
#: is known, `metrics.logs` returning 401 is a new defect, and keying on the label would
#: have called both "documented". Anything here still prints; only the exit code changes.
KNOWN_SERVER_FAILURES = {
    # "400" added in phase 09 round 13. The endpoint's failure *changed shape* between phase
    # 05 and now — it used to hang, it now rejects an unfiltered call — and this allowlist
    # keying on the failure kind is exactly why that was noticed instead of absorbed. Both
    # shapes are documented: see `metrics.logs`' docstring and deviation 34.
    "metrics.logs": ("ReadTimeout", "504", "400"),
    "metrics.actions": ("ReadTimeout", "504"),
    "metrics.action_count": ("ReadTimeout", "500"),
}

#: Functions that are helpers over already-fetched data, not API calls.
PURE_HELPERS = {
    "bots.versions_of", "bots.latest_version", "bots.published_version",
    "bots.published_versions", "bots.descriptors_by_class", "bots.describe_flow",
    "sources.source_id_of", "metrics.RESOLUTIONS", "metrics.AGENCY_METRICS",
}


def head(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def check(label: str, fn, *args, **kwargs):
    """Call a read, record that it was exercised, and record any failure."""
    EXERCISED.add(label)
    try:
        return fn(*args, **kwargs)
    except ClosewireError as exc:
        FAILURES.append(f"{label}: {type(exc).__name__}: {str(exc)[:130]}")
        print(f"  !! {label}: {str(exc)[:130]}")
        return None


#: Re-exported so this script checks with the same function the client ships, rather than
#: a private copy that could drift from it.
find_secrets = find_unredacted


def assert_no_secrets(label: str, payload) -> None:
    leaks = find_secrets(payload)
    if leaks:
        FAILURES.append(f"{label}: UNREDACTED SECRETS at {leaks[:6]}")
        print(f"  !! {label}: UNREDACTED SECRETS at {leaks[:6]}")
    else:
        print(f"  no unredacted credentials anywhere in {label}: OK")


def main() -> int:
    cfg = load_config()
    today = dt.date.today()

    with RestClient(cfg) as rest:
        head("BOTS  (UI: Agents, /bots)")
        all_bots = check("bots.list_bots", bots.list_bots, rest) or []
        print(f"  count: {len(all_bots)}   <-- compare with the UI Agents list")
        assert_no_secrets("bots.list_bots", all_bots)
        for b in all_bots:
            print(f"    {b['id']}  versions={[v.get('version') for v in bots.versions_of(b)]} "
                  f"published={bots.published_versions(b)} "
                  f"embedded_sources={len(b.get('sources') or [])}")

        nd = check("bots.node_descriptors", bots.node_descriptors, rest) or {}
        index = bots.descriptors_by_class(nd)
        print(f"  descriptor index: {len(index)} classes (atomicNodes + tools)")

        # Exercise get_steps on EVERY bot and EVERY version, not just bots[0] v[-1].
        for b in all_bots:
            detail = check("bots.get", bots.get, rest, b["id"])
            if detail:
                assert_no_secrets(f"bots.get[{b['id']}]", detail)
            pub = bots.published_version(b)
            for ver in [v.get("version") for v in bots.versions_of(b)]:
                graph = check("bots.get_steps", bots.get_steps, rest, b["id"], str(ver))
                if not isinstance(graph, dict):
                    continue
                described = bots.describe_flow(nd, graph)
                unknown = sorted({r["type"] for r in described if not r["known"]})
                edges = len(graph.get("edges") or [])
                mark = "  <-- PUBLISHED (what the UI shows)" if str(ver) == str(pub) else ""
                print(f"    {b['id']} v{ver}: {len(described)} nodes, {edges} edges, "
                      f"unknown types={unknown or 'none'}{mark}")

        for b in all_bots:
            latest, published = bots.latest_version(b), bots.published_version(b)
            if published and latest != published:
                print(f"    {b['id']}: latest={latest} is NOT the published {published} "
                      f"-- callers wanting what the UI shows must use published_version")
        tpl = check("bots.templates", bots.templates, rest)
        print(f"  templates: {len(tpl or [])} -> {[type(t).__name__ for t in (tpl or [])[:3]]}")

        head("PERSONAS")
        ps = check("personas.list_personas", personas.list_personas, rest) or []
        print(f"  count: {len(ps)}")
        if ps:
            one = check("personas.get", personas.get, rest, ps[0]["id"])
            print(f"  personas.get -> {'OK' if one else 'FAILED'}")

        head("SOURCES  (UI: Sources, /settings/sources)")
        page = check("sources.list_sources", sources.list_sources, rest) or {}
        rows = page.get("results", [])
        print(f"  total: {page.get('total')}  page {page.get('page')} -> {len(rows)} rows")
        assert_no_secrets("sources.list_sources", rows)

        every = check("sources.iter_sources", sources.iter_sources, rest) or []
        print(f"  iter_sources: {len(every)} rows across all pages")
        assert_no_secrets("sources.iter_sources", every)

        src_id = sources.source_id_of(rows[0]) if rows else None
        if src_id:
            one_src = check("sources.get", sources.get, rest, src_id)
            if one_src:
                assert_no_secrets("sources.get", one_src)
            cals = check("sources.list_calendars", sources.list_calendars, rest, src_id) or []
            flds = check("sources.list_fields", sources.list_fields, rest, src_id) or {}
            tags = check("sources.list_tags", sources.list_tags, rest, src_id) or []
            chans = check("sources.list_channels", sources.list_channels, rest, src_id) or []
            owners = check("sources.list_hubspot_owners", sources.list_hubspot_owners, rest, src_id)
            print(f"    calendars {len(cals)} | fields {sum(len(v) for v in flds.values())} "
                  f"across {sorted(flds)} | tags {len(tags)} | channels {len(chans)} "
                  f"| hubspot_owners {len(owners or [])} (GHL source -> expected 0)")

        head("LEADS  (UI: Chats, /conversations)")
        lead_page = check("leads.list_leads", leads.list_leads, rest) or {}
        lrows = lead_page.get("results", [])
        print(f"  total: {lead_page.get('total')} -> {len(lrows)} rows")

        found = check("leads.search", leads.search, rest) or {}
        print(f"  search: total={found.get('total')} rows={len(found.get('results', []))} "
              f"(same envelope as list_leads: {sorted(found) == sorted(lead_page) or sorted(found)})")
        # `search` is normalized now, so a `dry_run` stub can never appear — the old
        # guard was dead. A dropped `write=False` instead yields a silently EMPTY result,
        # so assert on substance: this account has 343 leads.
        if found.get("dry_run"):
            FAILURES.append("leads.search returned a dry-run stub")
        if not found.get("results"):
            FAILURES.append(
                "leads.search returned no rows — the likeliest cause is a lost write=False, "
                "which makes it a dry-run-suppressed write instead of a read"
            )

        if lrows:
            lead_id = lrows[0]["id"]
            check("leads.get", leads.get, rest, lead_id)
            toggle = check("leads.get_ai_toggle", leads.get_ai_toggle, rest, lead_id) or {}
            print(f"  ai toggle: {toggle}  (note: `reason` is from THIS call; "
                  f"`mostRecentFailureReason` is from leads.get)")
            hist = check("leads.history", leads.history, rest, lead_id)
            print(f"  page history rows: {len(hist) if hist is not None else 'FAILED'}")

        # Bounded: 343 leads at pageSize 20 would be 18 paced calls.
        some = check("leads.iter_leads", leads.iter_leads, rest, max_pages=2) or []
        print(f"  iter_leads (max_pages=2): {len(some)} rows, distinct={len({r['id'] for r in some})}")

        head("METRICS")
        # A 30-day window found nothing and made the round-1 evidence vacuous. Sweep a
        # range wide enough to contain this account's real bookings.
        wide_start = (today - dt.timedelta(days=730)).isoformat()
        for res in metrics.RESOLUTIONS:
            series = check(f"metrics.booking_graph[{res}]", metrics.booking_graph, rest,
                           start=wide_start, end=today.isoformat(), resolution=res)
            if series is None:
                continue
            booked = sum(
                (p.get("count") or p.get("bookings") or p.get("value") or 0)
                for p in series if isinstance(p, dict)
            )
            print(f"  booking_graph {res:8s}: {len(series):3d} points, {booked} bookings "
                  f"over 730d  <-- must be NON-ZERO to prove the call works")
            if res == "monthly" and not series:
                FAILURES.append("booking_graph[monthly] returned no points over 730 days")

        for bad, why in (("day", "invalid resolution"),):
            try:
                metrics.booking_graph(rest, start=wide_start, end=today.isoformat(), resolution=bad)
                FAILURES.append(f"booking_graph accepted {bad!r} ({why})")
            except ValueError:
                print(f"  rejects {bad!r} locally, no wasted call: OK")
        try:
            metrics.booking_graph(rest, start=today.isoformat(), end=wide_start, resolution="daily")
            FAILURES.append("booking_graph accepted an inverted range")
        except ValueError:
            print("  rejects an inverted start/end range locally: OK")

        summary = check("metrics.summary", metrics.summary, rest) or {}
        print(f"  summary: messages(month)={summary.get('currentMonthMessageCount')} "
              f"bookings(month)={summary.get('currentMonthSuccessfulBookings')} "
              f"users={summary.get('currentUsers')} storage={summary.get('totalStorage')}")
        msgs = check("metrics.messages", metrics.messages, rest) or []
        print(f"  messages: {len(msgs)} rows (bare list, no total -- may be server-capped)")
        check("metrics.message_count", metrics.message_count, rest)
        check("metrics.actions", metrics.actions, rest)
        check("metrics.action_count", metrics.action_count, rest)
        # Called unfiltered on purpose. The endpoint fails *both* ways and the unfiltered
        # failure is the better one: no filter gives a fast, self-describing 400 ("Must
        # specify at least one filter (botId, messageId, sourceId, leadId, actionId)"), while
        # supplying `botId` — exactly what that 400 asks for — goes back to hanging until the
        # read timeout. Probed both in phase 09 round 13. A 120-second hang teaches nothing
        # and costs a paced slot; a 400 names the contract.
        check("metrics.logs", metrics.logs, rest)
        check("metrics.agency_metric", metrics.agency_metric, rest,
              metric="responses", resolution="daily")
        try:
            metrics.agency_metric(rest, metric="bogus", resolution="daily")
            FAILURES.append("agency_metric accepted an unsupported metric")
        except ValueError:
            print("  agency_metric rejects an unsupported metric locally: OK")

        head("PACING")
        stats = rest.pacer.stats()
        print(stats.render())
        if stats.writes_last_hour:
            FAILURES.append(f"a READ-ONLY sweep used the write lane {stats.writes_last_hour}x")

    head("COVERAGE")
    missing = []
    for module in (bots, personas, sources, leads, metrics):
        name = module.__name__.rsplit(".", 1)[-1]
        for fn in module.__all__:
            label = f"{name}.{fn}"
            if label in PURE_HELPERS:
                continue
            if not any(e == label or e.startswith(label + "[") for e in EXERCISED):
                missing.append(label)
    if missing:
        FAILURES.append(f"never exercised: {missing}")
        print(f"  !! {len(missing)} public function(s) never called: {missing}")
    else:
        print(f"  every public API function exercised ({len(EXERCISED)} distinct calls)")

    head("RESULT")
    # A label alone must never downgrade a failure: adding a read's label to the allowlist
    # would otherwise bucket its UNREDACTED SECRETS as "known server-side" and report a
    # clean run over printed credential leaks. Only transport/HTTP failures can be known.
    NEVER_DOWNGRADE = ("UNREDACTED SECRETS", "never exercised", "dry-run stub", "no rows")
    known, unexpected = [], []
    for failure in FAILURES:
        label = failure.split(":", 1)[0]
        # Both the label and the failure kind must match. A leak or a coverage gap can
        # never be downgraded, whatever label it is reported under.
        expected = KNOWN_SERVER_FAILURES.get(label, ())
        downgradable = (
            any(marker in failure for marker in expected)
            and not any(marker in failure for marker in NEVER_DOWNGRADE)
        )
        (known if downgradable else unexpected).append(failure)

    if known:
        print(f"{len(known)} known server-side failure(s) — documented, not a regression:")
        for f in known:
            print(f"  . {f}")
    if unexpected:
        print()
        print(f"{len(unexpected)} UNEXPECTED failure(s):")
        for f in unexpected:
            print(f"  - {f}")
        return 1
    print()
    print("no unexpected failures; no unredacted credentials; full coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
