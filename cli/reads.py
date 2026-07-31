"""Tier-0 read commands for the ``closewire`` CLI.

One verb per operation over :mod:`closewire_client.endpoints`'s curated read modules.
Read-only: no command here mutates anything, and none reaches a generated write function.

Two output modes:

* default — a compact human table, sized to a terminal.
* ``--json`` — pretty JSON and **nothing else on stdout**. Progress, pacing, and warnings
  all go to stderr, so ``closewire bots list --json | jq`` is always safe to pipe.

Exit codes: ``0`` success · ``1`` config/API failure · ``2`` the circuit breaker is open.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any

from closewire_client.endpoints import bots, leads, metrics, personas, sources
from closewire_client.errors import ClosebotAPIError, ClosewireError
from closewire_client.pacing import PacingHalt

__all__ = ["add_read_parsers", "dispatch_read", "READ_COMMANDS"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_HALTED = 2

#: Every read command, for `--help` and for the validation log's command list.
READ_COMMANDS = (
    "bots list", "bots get", "bots steps", "bots descriptors", "bots templates",
    "personas list", "personas get",
    "sources list", "sources get", "sources calendars", "sources fields",
    "sources tags", "sources channels",
    "leads list", "leads get", "leads history", "leads ai-toggle", "leads search",
    "metrics booking", "metrics summary", "metrics messages", "metrics actions",
    "metrics logs",
)


# ── Rendering ─────────────────────────────────────────────────────────────────
def _emit(payload: Any, *, as_json: bool, table) -> int:
    """Print a payload as JSON or as a table. JSON mode writes nothing else to stdout."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        table()
    return EXIT_OK


def _trunc(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _short(value: Any, width: int) -> str:
    """Trim a fixed-format value (a date, a timestamp) tolerating a null.

    ``payload.get(key, "")[:n]`` returns ``None`` when the key is *present* with a null
    value, and ``None[:n]`` raises. Dates are null on some rows.
    """
    return str(value or "")[:width]


def _table(rows: list[list[Any]], headers: list[str], widths: list[int]) -> None:
    line = "  ".join(h.ljust(w)[:w] for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(_trunc(cell, w).ljust(w) for cell, w in zip(row, widths)))


def _booking_period(point: dict[str, Any]) -> str:
    """Label one bookingGraph point.

    The endpoint returns a **different shape per resolution**, verified against a live
    account rather than assumed:

    * ``monthly`` → ``{year, month, count}``
    * ``daily``   → ``{date, count}``
    * ``hourly``  → ``{date, hour, count}``

    So there is no single date field to read, and guessing one produced a blank column.
    """
    if point.get("year") is not None and point.get("month") is not None:
        return f"{point['year']}-{int(point['month']):02d}"
    date = str(point.get("date") or "")[:10]
    hour = point.get("hour")
    if date and hour is not None:
        return f"{date} {int(hour):02d}:00"
    return date or "?"


def _count(label: str, n: int, total: Any = None) -> None:
    suffix = f" of {total}" if total is not None and total != n else ""
    print(f"\n{n}{suffix} {label}")


# ── Parser ────────────────────────────────────────────────────────────────────
def add_read_parsers(
    sub: "argparse._SubParsersAction", json_opt
) -> dict[str, "argparse._SubParsersAction"]:
    """Register every read command group and **return its action subparsers by group name**.

    ``json_opt`` is a parent parser carrying ``--json`` with ``default=SUPPRESS``, so the
    flag works either side of the subcommand without a child default clobbering a global.

    Returning the map is what lets a later tier add actions to a group this module already
    created. The CLI is organised by **noun** (``bots``, ``leads``) while the code is
    organised by **tier**, and the two axes meet at the noun: ``closewire bots list`` is
    Tier-0 and ``closewire bots delete`` is Tier-2, but ``bots`` can only be registered on
    the top-level subparser once — a second ``add_parser("bots")`` raises. So ownership of
    the group parser lives here, and other tiers extend it rather than re-declaring it.
    """
    groups: dict[str, "argparse._SubParsersAction"] = {}

    # bots
    p = sub.add_parser("bots", help="Inspect bots (called 'Agents' in the Closebot UI).")
    s = p.add_subparsers(dest="action", required=True)
    groups["bots"] = s
    s.add_parser("list", parents=[json_opt], help="Every bot, with versions and which are published.")
    g = s.add_parser("get", parents=[json_opt], help="One bot by id.")
    g.add_argument("id")
    st = s.add_parser("steps", parents=[json_opt], help="The Job-Flow graph for a bot version.")
    st.add_argument("id")
    st.add_argument(
        "--version",
        help="Bot version. REQUIRED by the API despite the spec marking it optional; "
        "defaults to the published version, else the newest.",
    )
    st.add_argument("--published", action="store_true",
                    help="Force the published version (what the UI shows).")
    s.add_parser("descriptors", parents=[json_opt], help="The Job-Flow node catalogue.")
    s.add_parser("templates", parents=[json_opt], help="Builder template names.")

    # personas
    p = sub.add_parser("personas", help="Inspect personas.")
    s = p.add_subparsers(dest="action", required=True)
    groups["personas"] = s
    s.add_parser("list", parents=[json_opt], help="Every persona.")
    g = s.add_parser("get", parents=[json_opt], help="One persona by id.")
    g.add_argument("id")

    # sources
    p = sub.add_parser("sources", help="Inspect connected CRM sub-accounts.")
    s = p.add_subparsers(dest="action", required=True)
    groups["sources"] = s
    ls = s.add_parser("list", parents=[json_opt], help="Connected sources (credentials always masked).")
    ls.add_argument("--all", action="store_true", help="Sweep every page, not just the first.")
    ls.add_argument("--query")
    ls.add_argument("--category")
    for name, helptext in (
        ("get", "One source by id."),
        ("calendars", "Booking calendars on the sub-account."),
        ("fields", "Custom fields, grouped by object type."),
        ("tags", "Contact tags on the sub-account."),
        ("channels", "Messaging channels (SMS, WhatsApp, …)."),
    ):
        sp = s.add_parser(name, parents=[json_opt], help=helptext)
        sp.add_argument("source_id", metavar="SOURCE_ID")

    # leads
    p = sub.add_parser("leads", help="Inspect leads and conversations (UI: Chats).")
    s = p.add_subparsers(dest="action", required=True)
    groups["leads"] = s
    ll = s.add_parser("list", parents=[json_opt], help="Leads, newest activity first.")
    ll.add_argument("--source", help="Filter to one connected sub-account.")
    ll.add_argument("--page", type=int, default=0, help="0-indexed page number.")
    ll.add_argument("--page-size", type=int, default=20)
    ll.add_argument("--all", action="store_true", help="Sweep every page (slow: one paced call each).")
    for name, helptext in (
        ("get", "One lead, with its source and tags."),
        ("history", "Page-visit history for a lead."),
        ("ai-toggle", "Whether the AI may reply to this lead."),
    ):
        sp = s.add_parser(name, parents=[json_opt], help=helptext)
        sp.add_argument("lead_id", metavar="LEAD_ID")
    sr = s.add_parser("search", parents=[json_opt], help="Search leads (a POST that only reads).")
    sr.add_argument("--query", default=None)
    sr.add_argument("--source", action="append", default=None, help="Repeatable.")
    sr.add_argument("--count", type=int, default=20)
    sr.add_argument("--offset", type=int, default=0)

    # metrics
    p = sub.add_parser("metrics", help="Booking, message, and action metrics.")
    s = p.add_subparsers(dest="action", required=True)
    groups["metrics"] = s
    bg = s.add_parser("booking", parents=[json_opt], help="Bookings over time.")
    bg.add_argument("--start", help="ISO date. Defaults to 30 days ago.")
    bg.add_argument("--end", help="ISO date. Defaults to today.")
    bg.add_argument(
        "--resolution",
        default="daily",
        help="hourly | daily | monthly. NOTE: 'day' is documented in the vendor toolkit "
        "and the phase prompt, but the API rejects it with HTTP 400.",
    )
    bg.add_argument("--source", help="Filter to one connected sub-account.")
    s.add_parser("summary", parents=[json_opt], help="Agency-wide usage summary (the dashboard's numbers).")
    ms = s.add_parser("messages", parents=[json_opt], help="Recent messages. CONTAINS CONSUMER PII.")
    ms.add_argument("--limit", type=int, default=20, help="Rows to display (default 20).")
    s.add_parser("actions", parents=[json_opt], help="Bot actions. Known to time out server-side.")
    s.add_parser("logs", parents=[json_opt], help="Bot execution logs. Known to time out server-side.")

    return groups


# ── Dispatch ──────────────────────────────────────────────────────────────────
class NotFound(ClosewireError):
    """A read returned no resource — a bad id, rather than a transport failure."""


def _require(payload: Any, kind: str, ident: str) -> Any:
    """Turn "the API returned nothing useful" into a readable error, once, centrally.

    Closebot answers some bad ids with 200 and a null body rather than a 404, so every
    renderer doing ``payload.get(...)`` would raise `AttributeError` and surface as a
    traceback. Checking here means one guard instead of one per renderer.
    """
    if payload is None or (isinstance(payload, dict) and not payload):
        raise NotFound(f"no {kind} found with id {ident!r}")
    if not isinstance(payload, (dict, list)):
        raise NotFound(f"unexpected response for {kind} {ident!r}: {type(payload).__name__}")
    return payload

def dispatch_read(args: argparse.Namespace, rest, as_json: bool) -> int:
    """Run one read command. Raises nothing: failures become exit codes."""
    try:
        return _run(args, rest, as_json)
    except PacingHalt as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_HALTED
    except ClosebotAPIError as exc:
        print(f"\nHTTP {exc.status_code}: {exc.method} {exc.path}", file=sys.stderr)
        detail = str(exc.body)
        print(f"  {detail[:600]}", file=sys.stderr)
        if exc.status_code == 404:
            print("  (check the id — ids are case-sensitive and prefixed, "
                  "e.g. bot_… / src_… / lead_…)", file=sys.stderr)
        return EXIT_FAILURE
    except NotFound as exc:
        print(f"\n{exc}", file=sys.stderr)
        print("  (ids are case-sensitive and prefixed: bot_… / pers_… / src_… / lead_…)",
              file=sys.stderr)
        return EXIT_FAILURE
    except ClosewireError as exc:
        print(f"\nrequest failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except ValueError as exc:  # local validation, e.g. a bad --resolution
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_FAILURE
    except (AttributeError, TypeError, KeyError) as exc:
        # A renderer met a shape it did not expect. Report it rather than letting a
        # traceback reach the user — a CLI should never die on an unfamiliar payload.
        print(f"\nunexpected response shape ({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_FAILURE




def _run(args: argparse.Namespace, rest, as_json: bool) -> int:
    group, action = args.command, args.action

    if group == "bots":
        return _bots(args, rest, as_json, action)
    if group == "personas":
        return _personas(args, rest, as_json, action)
    if group == "sources":
        return _sources(args, rest, as_json, action)
    if group == "leads":
        return _leads(args, rest, as_json, action)
    if group == "metrics":
        return _metrics(args, rest, as_json, action)
    print(f"unknown command group {group!r}", file=sys.stderr)
    return EXIT_FAILURE


def _bots(args, rest, as_json, action) -> int:
    if action == "list":
        rows = bots.list_bots(rest)

        def table() -> None:
            _table(
                [[b.get("id"), b.get("name"),
                  ",".join(bots.published_versions(b)) or "-",
                  bots.latest_version(b) or "-",
                  len(b.get("sources") or []),
                  _short(b.get("modifiedAt"), 10)] for b in rows],
                ["ID", "NAME", "PUBLISHED", "LATEST", "SRC", "MODIFIED"],
                [22, 30, 18, 8, 3, 10],
            )
            _count("bots", len(rows))
        return _emit(rows, as_json=as_json, table=table)

    if action == "get":
        bot = _require(bots.get(rest, args.id), "bot", args.id)

        def table() -> None:
            for key in ("id", "name", "category", "locked", "folderId", "modifiedAt",
                        "modifiedBy", "personaIds", "followUpActive"):
                print(f"  {key:16s} {bot.get(key)}")
            print(f"  {'versions':16s} "
                  f"{[v.get('version') for v in bots.versions_of(bot)]}")
            print(f"  {'published':16s} {bots.published_versions(bot) or '(none)'}")
            print(f"  {'sources':16s} "
                  f"{[s.get('name') for s in (bot.get('sources') or [])]}")
        return _emit(bot, as_json=as_json, table=table)

    if action == "steps":
        bot = _require(bots.get(rest, args.id), "bot", args.id)
        version = args.version or (
            bots.published_version(bot) if args.published else
            bots.published_version(bot) or bots.latest_version(bot)
        )
        if not version:
            if args.published and bots.versions_of(bot):
                raise ValueError(
                    f"{args.id} has never been published; its versions are "
                    f"{[v.get('version') for v in bots.versions_of(bot)]}. "
                    "Drop --published, or pass --version explicitly."
                )
            raise ValueError(f"{args.id} has no versions; nothing to fetch")
        graph = bots.get_steps(rest, args.id, version)
        catalogue = bots.node_descriptors(rest)
        described = bots.describe_flow(catalogue, graph)

        def table() -> None:
            print(f"  bot {args.id}  version {version}"
                  f"{'  (published)' if version in bots.published_versions(bot) else ''}\n")
            _table(
                [[r["id"], r["type"], r["displayName"] or "?", r["group"] or "",
                  "" if r["known"] else "UNKNOWN",
                  ",".join(e["target"] for e in r["next"])] for r in described],
                ["NODE", "TYPE", "DISPLAY NAME", "GROUP", "?", "NEXT"],
                [14, 16, 22, 16, 7, 28],
            )
            _count(f"nodes ({len(graph.get('edges') or [])} edges)", len(described))
        return _emit({"version": version, "graph": graph, "described": described},
                     as_json=as_json, table=table)

    if action == "descriptors":
        catalogue = bots.node_descriptors(rest)
        index = bots.descriptors_by_class(catalogue)

        def table() -> None:
            _table(
                [[k, v.get("displayName"), v.get("group"), v.get("description")]
                 for k, v in sorted(index.items())],
                ["CLASS NAME", "DISPLAY NAME", "GROUP", "DESCRIPTION"],
                [24, 24, 20, 40],
            )
            _count(f"node types across {sorted(catalogue)}", len(index))
        return _emit(catalogue, as_json=as_json, table=table)

    if action == "templates":
        rows = bots.templates(rest)

        def table() -> None:
            for t in rows:
                print(f"  {t}")
            _count("templates", len(rows))
        return _emit(rows, as_json=as_json, table=table)
    return EXIT_FAILURE


def _personas(args, rest, as_json, action) -> int:
    if action == "list":
        rows = personas.list_personas(rest)

        def table() -> None:
            _table(
                [[p.get("id"), p.get("personaName"), p.get("description"),
                  p.get("responseTime"), _short(p.get("modifiedAt"), 10)] for p in rows],
                ["ID", "NAME", "DESCRIPTION", "RESPONSE", "MODIFIED"],
                [24, 18, 34, 10, 10],
            )
            _count("personas", len(rows))
        return _emit(rows, as_json=as_json, table=table)

    persona = _require(personas.get(rest, args.id), "persona", args.id)

    def table() -> None:
        for key in ("id", "personaName", "description", "voiceStyles", "typoPercent",
                    "responseTime", "responseDelay", "modifiedAt", "modifiedBy"):
            print(f"  {key:18s} {_trunc(persona.get(key), 90)}")
    return _emit(persona, as_json=as_json, table=table)


def _sources(args, rest, as_json, action) -> int:
    if action == "list":
        if args.all:
            rows = sources.iter_sources(rest, query=args.query, category=args.category)
            total = len(rows)
        else:
            page = sources.list_sources(rest, query=args.query, category=args.category)
            rows, total = page["results"], page["total"]

        def table() -> None:
            _table(
                [[sources.source_id_of(s), s.get("name"), s.get("category"),
                  "yes" if s.get("connected") else "NO", _trunc(s.get("address"), 40)]
                 for s in rows],
                ["SOURCE ID", "NAME", "CRM", "CONN", "ADDRESS"],
                [24, 32, 6, 5, 40],
            )
            _count("sources", len(rows), total)
            print("  (OAuth credentials are masked; --json shows them as <redacted>)")
        return _emit({"total": total, "results": rows}, as_json=as_json, table=table)

    sid = args.source_id
    if action == "get":
        src = _require(sources.get(rest, sid), "source", sid)

        def table() -> None:
            for key in ("sourceId", "name", "category", "connected", "address",
                        "autoShutoff", "gracefulGoodbye", "key", "accessToken"):
                print(f"  {key:18s} {_trunc(src.get(key), 80)}")
        return _emit(src, as_json=as_json, table=table)

    if action == "fields":
        grouped = sources.list_fields(rest, sid)

        def table() -> None:
            for obj, fields in sorted(grouped.items()):
                print(f"\n  {obj}  ({len(fields)})")
                _table([[f.get("name"), f.get("fieldKey"), f.get("dataType")]
                        for f in fields],
                       ["NAME", "FIELD KEY", "TYPE"], [30, 40, 12])
            _count(f"fields across {sorted(grouped)}",
                   sum(len(v) for v in grouped.values()))
        return _emit(grouped, as_json=as_json, table=table)

    fn, label = {
        "calendars": (sources.list_calendars, "calendars"),
        "tags": (sources.list_tags, "tags"),
        "channels": (sources.list_channels, "channels"),
    }[action]
    rows = fn(rest, sid)

    def table() -> None:
        _table([[r.get("name"), r.get("id"), r.get("source", "")] for r in rows],
               ["NAME", "ID", "ORIGIN"], [44, 34, 12])
        _count(label, len(rows))
        if label == "calendars" and any(r.get("id") == "not_in_db" for r in rows):
            print("  ('not_in_db' = exists in the CRM but not imported into Closebot)")
    return _emit(rows, as_json=as_json, table=table)


def _leads(args, rest, as_json, action) -> int:
    if action == "list":
        if args.all:
            rows = leads.iter_leads(rest, source_id=args.source)
            payload, total = {"total": len(rows), "results": rows}, len(rows)
        else:
            payload = leads.list_leads(rest, page=args.page, page_size=args.page_size,
                                       source_id=args.source)
            rows, total = payload["results"], payload["total"]

        def table() -> None:
            _table(
                [[l.get("id"), l.get("name"),
                  (l.get("source") or {}).get("name"),
                  l.get("lastMessageDirection"),
                  _short(l.get("lastMessageTime"), 16).replace("T", " "),
                  _trunc(l.get("lastMessage"), 34)] for l in rows],
                ["LEAD ID", "NAME", "SOURCE", "DIR", "LAST MESSAGE AT", "LAST MESSAGE"],
                [22, 18, 22, 4, 16, 34],
            )
            _count("leads", len(rows), total)
        return _emit(payload, as_json=as_json, table=table)

    if action == "search":
        found = leads.search(rest, search=args.query, count=args.count,
                             offset=args.offset, source_ids=args.source)
        rows = found["results"]

        def table() -> None:
            _table([[l.get("id"), l.get("name"),
                     (l.get("source") or {}).get("name"),
                     _trunc(l.get("lastMessage"), 40)] for l in rows],
                   ["LEAD ID", "NAME", "SOURCE", "LAST MESSAGE"], [22, 18, 22, 40])
            _count("matching leads", len(rows), found["total"])
        return _emit(found, as_json=as_json, table=table)

    lid = args.lead_id
    if action == "get":
        lead = _require(leads.get(rest, lid), "lead", lid)

        def table() -> None:
            for key in ("id", "name", "contactId", "lastMessageDirection",
                        "lastMessageTime", "mostRecentFailureReason", "starred",
                        "unread", "tags"):
                print(f"  {key:24s} {_trunc(lead.get(key), 70)}")
            print(f"  {'source':24s} {(lead.get('source') or {}).get('name')}")
            print(f"\n  last message:\n    {_trunc(lead.get('lastMessage'), 300)}")
        return _emit(lead, as_json=as_json, table=table)

    if action == "history":
        rows = leads.history(rest, lid)

        def table() -> None:
            if not rows:
                print("  (no tracked page history for this lead)")
            else:
                _table([[_trunc(r, 100)] for r in rows], ["PAGE VISIT"], [100])
            _count("page visits", len(rows))
        return _emit(rows, as_json=as_json, table=table)

    toggle = leads.get_ai_toggle(rest, lid)

    def table() -> None:
        print(f"  enabled     {toggle.get('enabled')}")
        print(f"  applicable  {toggle.get('applicable')}")
        print(f"  reason      {toggle.get('reason')}")
        print("\n  (a lead's own `mostRecentFailureReason` — from `leads get` — explains "
              "the UI banner;\n   this call's `reason` is a different field and is often null)")
    return _emit(toggle, as_json=as_json, table=table)


def _metrics(args, rest, as_json, action) -> int:
    if action == "booking":
        today = dt.date.today()
        start = args.start or (today - dt.timedelta(days=30)).isoformat()
        end = args.end or today.isoformat()
        series = metrics.booking_graph(rest, start=start, end=end,
                                       resolution=args.resolution, source_id=args.source)

        def table() -> None:
            print(f"  {start} .. {end}  resolution={args.resolution}\n")
            if not series:
                print("  (no bookings in this range — widen it before concluding "
                      "the call is broken)")
            else:
                rows = [[_booking_period(p), p.get("count")] for p in series]
                _table(rows, ["PERIOD", "BOOKINGS"], [28, 10])
                print(f"  {'':28}  {'-' * 10}")
                print(f"  {'total':28}  {sum(p.get('count') or 0 for p in series)}")
            _count("points", len(series))
        return _emit(series, as_json=as_json, table=table)

    if action == "summary":
        data = metrics.summary(rest)

        def table() -> None:
            for key, value in data.items():
                print(f"  {key:36s} {value}")
        return _emit(data, as_json=as_json, table=table)

    if action == "messages":
        rows = metrics.messages(rest)
        shown = rows[: args.limit]

        def table() -> None:
            _table([[_short(m.get("timestamp"), 16).replace("T", " "),
                     m.get("channel"), m.get("direction"),
                     m.get("leadId"), _trunc(m.get("message"), 46)] for m in shown],
                   ["WHEN", "CHANNEL", "DIRECTION", "LEAD", "MESSAGE"],
                   [16, 9, 10, 22, 46])
            _count("messages shown", len(shown), len(rows))
        # Self-describing rather than a bare truncated array: a scripted consumer must be
        # able to tell a capped result from a complete one.
        return _emit({"total": len(rows), "returned": len(shown), "results": shown},
                     as_json=as_json, table=table)

    fn = metrics.actions if action == "actions" else metrics.logs
    rows = fn(rest)

    def table() -> None:
        for row in rows[:40]:
            print(f"  {_trunc(row, 110)}")
        _count(action, len(rows))
    return _emit(rows, as_json=as_json, table=table)
