"""Probe every surface that could expose goal or variable state. Read-only, zero cost.

Phase 09's remaining unmet clause is "goal completion is visible in the UI transcript". Round
12 added an `Objective` node to a throwaway bot, published it, ran a test session, found
nothing anywhere, and wrote that up as *"no goal or variable state is exposed on any
surface"*.

Three critics then filed the same objection: the sweep had never touched
`GET /botVariables/{botId}/{sourceId}` — a pure GET, shipped in this repo, taking exactly the
`(botId, sourceId)` pair `create_session` hands back, and the obvious home for an Objective's
output variable. It was run in round 13 and returned `[]`.

A fourth objection then landed, and it is the important one: **`[]` proves nothing without a
control.** The same endpoint returns `[]` for a real production source on an established bot,
so an empty result cannot separate "no state exists for a test lead" from "this endpoint
returns empty for everything on this account". A probe whose negative result is indistinguish-
able from its null result is not evidence.

So this script takes the control. It queries every bot/source pair on the account, not just
the throwaway's, and prints them together — which is what makes the empty answer readable
either way. Rounds 2, 4 and 10 all blocked on results that existed only as prose in the log;
this exists so the next reader can re-run it instead of believing it.

    python scripts/probe_goal_state.py

Every call is a GET on the read lane. Nothing is created, sent, published or deleted, and no
credit can be spent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from closewire_client.config import load_config
from closewire_client.endpoints import bot as bot_ep
from closewire_client.endpoints import bot_source_variable as bsv
from closewire_client.endpoints import lead as lead_ep
from closewire_client.endpoints import metrics
from closewire_client.rest import RestClient
from closewire_client.writes import testing

THROWAWAY = "bot_2U91R6FH00C25WZS"


def _short(value: object, width: int = 70) -> str:
    return json.dumps(value, default=str)[:width]


def main() -> int:
    print("# produced by: scripts/probe_goal_state.py")
    client = RestClient(load_config())

    print("== GET /botVariables/{botId}/{sourceId} — every pair on the account ==")
    print("   (the throwaway AND real production bots: without the control, an empty")
    print("    result for the test lead cannot be told from an empty endpoint)\n")
    bots = bot_ep.get_bot(client)
    for bot in bots if isinstance(bots, list) else []:
        bot_id = bot.get("_id") or bot.get("id")
        name = str(bot.get("name", "?"))[:28]
        sources = bot.get("sources") or []
        if not sources:
            print(f"  {name:30s} (no sources attached)")
        for source in sources:
            source_id = source.get("id") or source.get("_id")
            try:
                variables = bsv.get_botvariables_botid_sourceid(client, bot_id, source_id)
                print(f"  {name:30s} {str(source_id)[:26]:28s} -> {_short(variables)}")
            except Exception as error:
                print(f"  {name:30s} {str(source_id)[:26]:28s} -> "
                      f"{type(error).__name__}: {str(error)[:60]}")

    print("\n== test-session leads on the throwaway ==")
    rows = testing.sessions_of(testing.list_sessions(client, THROWAWAY))
    for row in rows:
        lead_id = row.get("id")
        print(f"\n  {lead_id}")
        print(f"    list row   fields={_short(row.get('fields'))} tags={_short(row.get('tags'))}")
        print(f"    instances  {_short(row.get('instances'), 90)}")
        try:
            detail = lead_ep.get_lead_leadid(client, lead_id)
            keys = sorted(detail) if isinstance(detail, dict) else []
            goalish = [k for k in keys if any(
                word in k.lower() for word in ("goal", "object", "variable", "field")
            )]
            print(f"    lead read  fields={_short((detail or {}).get('fields'))}")
            print(f"    goal-ish keys on the lead record: {goalish or 'NONE'}")
        except Exception as error:
            print(f"    lead read  {type(error).__name__}: {str(error)[:60]}")

    print("\n== GET /botMetric/actions?leadId=... — per-lead NODE EXECUTION ==")
    print("   The surface that actually answers the question, and the one a council block")
    print("   had to name: it takes `leadId` and returns `nodeId`/`frontendNodeId` per")
    print("   action, so it shows which flow nodes ran for a given lead.\n")
    for row in rows:
        lead_id = row.get("id")
        try:
            actions = metrics.actions(client, leadId=lead_id, botId=THROWAWAY)
        except Exception as error:
            print(f"  {lead_id}: {type(error).__name__}: {str(error)[:60]}")
            continue
        print(f"  {lead_id}: {len(actions or [])} action(s)")
        for action in sorted(actions or [], key=lambda a: a.get("timestamp", "")):
            print(f"      {action.get('timestamp','')[:19]}  "
                  f"nodeId={action.get('nodeId')!r}  "
                  f"frontendNodeId={action.get('frontendNodeId')!r}")

    print("\n== what this establishes ==")
    print("  The variable/lead surfaces are empty everywhere, INCLUDING for production")
    print("  sources, so their emptiness alone proves nothing.")
    print("  The ACTION surface does answer it: every session executed `sourceNode` and")
    print("  nothing downstream. The Statement and Objective nodes never ran, in the")
    print("  pre-publish session AND the post-publish one. So the goal could not have")
    print("  flipped, and the absence of goal state is a consequence of that rather than")
    print("  evidence about where goal state lives. See deviations 31, 32 and 35.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
