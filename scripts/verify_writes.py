"""Verify the Tier-1 write client: coverage, dry-run, write lane, tier boundary.

Run: ``python scripts/verify_writes.py``. Exits non-zero on the first failure. Sends no
network traffic — every check here runs against a tripwire transport.

The central claim of phase 07's dry-run deliverable is "logs the exact payloads and sends
nothing". Trusting ``DRY_RUN_RESULT`` to prove that would be circular: it is the very flag
under test. So the proof is made **below** the layer being tested — a
``httpx.MockTransport`` that raises on any request at all. If a single byte escapes, it
raises.

And because a check that cannot fail is worth nothing — phase 06 shipped one — check 6
runs the identical calls with dry-run **off** and asserts the tripwire *does* fire. Without
that control, checks 2-5 would pass just as happily against a transport that was never
wired up.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closewire_client.console import configure_streams

configure_streams()

import httpx

from closewire_client.auth import ApiKeyAuth
from closewire_client.config import Config
from closewire_client.pacing import Pacer
from closewire_client.rest import RestClient
from closewire_client.session import Session
from closewire_client.writes import bots, personas

#: A throwaway key. This file never touches the network, so it needs no real one.
FAKE_KEY = "cb_verify_writes_not_a_real_key"

#: Every function the phase-07 brief names, and where it must live.
REQUIRED: dict[str, tuple[Any, str]] = {
    "bots.create": (bots, "create"),
    "bots.create_with_ai": (bots, "create_with_ai"),
    "bots.update": (bots, "update"),
    "bots.duplicate": (bots, "duplicate"),
    "bots.save": (bots, "save"),
    "bots.set_steps": (bots, "set_steps"),
    "bots.validate": (bots, "validate"),
    "bots.save_tools": (bots, "save_tools"),
    "bots.attach_source": (bots, "attach_source"),
    "bots.detach_source": (bots, "detach_source"),
    "personas.create": (personas, "create"),
    "personas.update": (personas, "update"),
}

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


class Tripwire(httpx.BaseTransport):
    """A transport that refuses to carry anything, and counts attempts."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts.append(f"{request.method} {request.url.path}")
        raise AssertionError(
            f"NETWORK ESCAPE: {request.method} {request.url.path} reached the transport"
        )


class _FakeClock:
    """Coupled clock + sleeper, so budget waits terminate instead of spinning."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def build_client(*, dry_run: bool) -> tuple[RestClient, Tripwire]:
    """A real client on a fake clock and a tripwire transport.

    The clock is fake so 11 paced writes do not take 11 think-times; the Pacer itself is
    real, so the write lane and budgets are genuinely exercised.
    """
    config = Config(api_key=FAKE_KEY, dry_run=dry_run)
    clock = _FakeClock()
    pacer = Pacer(config, monotonic=clock.monotonic, sleeper=clock.sleep)
    tripwire = Tripwire()
    session = Session(config, ApiKeyAuth.from_config(config), pacer, transport=tripwire)
    return RestClient(config, session, pacer=pacer), tripwire


#: One unique, mutually non-substring marker per exercised write.
#:
#: Check 4 used to look for a bot name and a path. Both were vacuous: the bot name
#: ``zz-closewire-test-07`` is a strict *substring* of the persona name
#: ``zz-closewire-test-07-persona``, so the persona payload alone satisfied it, and the path
#: is emitted by ``pacing.py``'s own suppression line whether or not a body is logged at all.
#: A critic proved it by logging only persona bodies and still passing.
#:
#: Markers make the check discriminating by construction: each marker rides in exactly one
#: call's payload, so a write whose body is not logged loses its own marker and nothing else
#: can cover for it. Every marker is checked to be a substring of no other (see main()).
MARKERS: dict[str, str] = {
    "bots.create": "MK-create-a1",
    "bots.create_with_ai": "MK-ai-b2",
    "bots.update": "MK-update-c3",
    "bots.duplicate": "MK-duplicate-d4",       # no body; marked via its path instead
    "bots.save": "MK-save-e5",
    "bots.set_steps": "MK-steps-f6",
    "bots.save_tools": "MK-tools-g7",
    "bots.attach_source": "MK-attach-h8",
    "bots.detach_source": "MK-detach-i9",      # no body; marked via its path instead
    "personas.create": "MK-percreate-j10",
    "personas.update": "MK-perupdate-k11",
}

#: The two writes that carry no body at all, so their marker rides in the path, not the
#: payload. Called out rather than quietly excluded — the reader should know check 4 proves
#: something weaker for these two.
BODYLESS: frozenset[str] = frozenset({"bots.duplicate", "bots.detach_source"})


def _graph(marker: str) -> dict[str, Any]:
    return {
        "nodes": [{"id": "n1", "type": "Source", "position": {"x": 0, "y": 0},
                   "data": {"type": "Source", "name": marker}}],
        "edges": [],
    }


def exercise(client: RestClient) -> list[tuple[str, Any]]:
    """Call every mutating deliverable once. ``bots.validate`` is offline and excluded."""
    m = MARKERS
    return [
        ("bots.create", bots.create(client, m["bots.create"])),
        ("bots.create_with_ai",
         # `name` is required in practice — see writes/_required.py. Positional so the
         # harness cannot drift back to the shape the API rejects.
         bots.create_with_ai(client, m["bots.create_with_ai"], "zz-harness-ai")),
        ("bots.update", bots.update(client, "BOT", name=m["bots.update"])),
        ("bots.duplicate", bots.duplicate(client, m["bots.duplicate"])),
        ("bots.save", bots.save(client, "BOT", _graph(m["bots.save"]))),
        ("bots.set_steps", bots.set_steps(client, "BOT", _graph(m["bots.set_steps"]))),
        ("bots.save_tools",
         bots.save_tools(client, "BOT", [bots.tool(m["bots.save_tools"])])),
        # `AttachSourceInput.tags` is `ContactTag[]` = {name, approveDeny, id}, not a list of
        # bare strings. Dry-run would suppress the difference, which is exactly how the
        # off-spec `save_tools` payload survived round 1 — so the harness sends the real shape.
        ("bots.attach_source",
         # `channels` is required and positional; an empty list is valid (verified live).
         bots.attach_source(client, "BOT", "SRC", [],
                            tags=[{"name": m["bots.attach_source"]}])),
        ("bots.detach_source", bots.detach_source(client, "BOT", m["bots.detach_source"])),
        ("personas.create", personas.create(client, m["personas.create"], typoPercent=3)),
        ("personas.update",
         personas.update(client, "PERSONA", description=m["personas.update"])),
    ]


def main() -> int:
    print("\n1. Deliverable coverage")
    for name, (module, attr) in REQUIRED.items():
        check(name, callable(getattr(module, attr, None)))

    print("\n2. Dry run: every write returns the dry-run sentinel")
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
    # Scoped to `closewire.rest`, NOT `closewire`. The parent logger also carries
    # `pacing.py`'s suppression line, which already contains the method and path — capturing
    # it would let a path-shaped assertion pass without any payload ever being logged.
    logger = logging.getLogger("closewire.rest")
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    client, tripwire = build_client(dry_run=True)
    try:
        results = exercise(client)
    except AssertionError as exc:
        check("no write escaped to the network", False, str(exc))
        return 1
    finally:
        logger.removeHandler(handler)

    for name, result in results:
        check(
            name,
            isinstance(result, dict) and result.get("dry_run") is True
            and result.get("sent") is False,
            repr(result)[:60],
        )

    print("\n3. Dry run: nothing reached the transport")
    check("zero transport attempts", not tripwire.attempts, f"attempts={tripwire.attempts}")

    print("\n4. Dry run: EVERY payload was logged, individually")
    blob = "\n".join(records)
    # Guard the guard: if two markers overlapped, one payload could cover for another and
    # this group would go quietly vacuous again.
    overlaps = [
        (a, b)
        for a, x in MARKERS.items()
        for b, y in MARKERS.items()
        if a != b and x in y
    ]
    check("markers are mutually non-substring", not overlaps, f"overlaps={overlaps}")
    for name, marker in MARKERS.items():
        label = f"{name} payload logged" + (" (path only — bodyless)" if name in BODYLESS else "")
        check(label, marker in blob, "" if marker in blob else f"marker {marker} absent")
    check(
        "one payload log per write",
        blob.count("DRY RUN would send") == len(results),
        f"logged={blob.count('DRY RUN would send')}, writes={len(results)}",
    )

    print("\n5. Every call was charged to the WRITE lane")
    stats = client.pacer.stats()
    check(
        "every write counted",
        stats.writes_last_hour == len(results),
        f"writes_last_hour={stats.writes_last_hour}, expected {len(results)}",
    )
    # There is no reads counter; reads are ops minus writes. Equality therefore proves the
    # read lane carried nothing — a write that slipped into the cheap lane would show here.
    check(
        "no read-lane leakage",
        stats.ops_last_hour == stats.writes_last_hour,
        f"ops={stats.ops_last_hour}, writes={stats.writes_last_hour}",
    )
    check(
        "suppression counted",
        stats.dry_run_blocked == len(results),
        f"dry_run_blocked={stats.dry_run_blocked}",
    )

    print("\n6. CONTROL — with dry run OFF the tripwire must fire")
    live, live_trip = build_client(dry_run=False)
    fired = False
    try:
        bots.create(live, "zz-closewire-test-07")
    except AssertionError:
        fired = True
    except Exception as exc:
        fired = "NETWORK ESCAPE" in str(exc) or bool(live_trip.attempts)
    check(
        "tripwire detects a real send",
        fired and bool(live_trip.attempts),
        f"attempts={live_trip.attempts}",
    )

    print("\n7. Tier-1/Tier-2 boundary")
    guarded, _ = build_client(dry_run=True)
    for label, call in [
        ("bots.update(trash=True) refused", lambda: bots.update(guarded, "B", trash=True)),
        ("personas.update(trash=True) refused",
         lambda: personas.update(guarded, "P", trash=True)),
    ]:
        try:
            call()
            check(label, False, "it was allowed")
        except ValueError:
            check(label, True)
    try:
        bots.update(guarded, "B", trash=False)
        check("bots.update(trash=False) allowed (restore)", True)
    except ValueError as exc:
        check("bots.update(trash=False) allowed (restore)", False, str(exc))

    print("\n8. save() refuses a graph with errors")
    try:
        bots.save(guarded, "B", {"nodes": [{"id": "a", "type": "NoSuchClass"}], "edges": []})
        check("invalid graph refused", False, "it was sent")
    except ValueError as exc:
        check("invalid graph refused", "invalid graph" in str(exc))

    print("\n9. The dry-run payload log redacts secrets")
    # The new payload log is a fresh place a credential can reach a terminal. A body can
    # legitimately carry one (updating a source's OAuth token), and the API key is in the
    # config, so both must be masked before they are printed.
    leak_client, _ = build_client(dry_run=True)
    leaked: list[str] = []

    class LeakCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            leaked.append(record.getMessage())

    leak_handler = LeakCapture()
    logging.getLogger("closewire.rest").addHandler(leak_handler)
    try:
        bots.update(
            leak_client, "BOT", name=f"leak-probe {FAKE_KEY}",
            followUpExtraPrompt="x",
        )
        personas.update(leak_client, "P", description="probe",
                        aiProviderPreferences=[{"apiKey": "sk_live_SHOULD_NOT_PRINT"}])
    finally:
        logging.getLogger("closewire.rest").removeHandler(leak_handler)

    dump = "\n".join(leaked)
    check("nested apiKey masked", "sk_live_SHOULD_NOT_PRINT" not in dump,
          "probe credential absent from the log")
    check("config api key scrubbed", FAKE_KEY not in dump, "config key absent from the log")
    check("payload still legible", "leak-probe" in dump and "probe" in dump,
          "the payload survived redaction")

    print("\n10. The write-lane override guard")
    # A critic replaced this guard with `if False:` and every other check stayed green —
    # the prerequisite the phase landed before shipping mutations had no regression guard
    # anywhere. It does now.
    guard_client, guard_trip = build_client(dry_run=True)
    for path in ("/bot", "/bot/abc/publish", "/agency/billing/refill"):
        try:
            guard_client.request("POST", path, json={}, write=False)
            check(f"write=False refused for {path}", False, "it was allowed")
        except ValueError:
            check(f"write=False refused for {path}", True)
        except AssertionError:
            # The tripwire fired: the guard let a mutation through to the wire. That is the
            # failure this check exists for, so report it as one rather than letting it
            # abort the run — the remaining sub-checks still need to execute.
            check(f"write=False refused for {path}", False, "ESCAPED to the transport")
    # ...and the one path it is legitimately allowed for still works.
    try:
        guard_client.request("POST", "/lead/search", json={}, write=False)
        check("write=False still allowed for /lead/search", False, "unexpectedly suppressed")
    except AssertionError:
        # The tripwire fired, which is correct: a read is NOT dry-run suppressed, so it
        # genuinely attempts to send. That is the proof the override still grants the read lane.
        check("write=False still allowed for /lead/search", True, "reached transport as a read")
    except ValueError as exc:
        check("write=False still allowed for /lead/search", False, str(exc)[:70])

    # Non-bool flags must be rejected, not silently coerced: `write=0` is falsy but not
    # False, so before this it slipped past the guard and was treated as a read everywhere.
    for bad in (0, "", [], 1, "yes"):
        try:
            guard_client.request("POST", "/bot", json={}, write=bad)
            check(f"write={bad!r} rejected", False, "it was accepted")
        except TypeError:
            check(f"write={bad!r} rejected", True)
        except Exception as exc:
            check(f"write={bad!r} rejected", False, f"{type(exc).__name__}: {str(exc)[:50]}")

    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
