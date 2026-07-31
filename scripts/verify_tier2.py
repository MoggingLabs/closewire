"""Verify the Tier-2 confirmation gates. Run: ``python scripts/verify_tier2.py``.

**Sends nothing, ever.** Every client here is built on a transport that raises on any
request, so even a fully-confirmed destructive call cannot reach the network from this
file. That matters more here than in phase 07: a mistake in this harness would delete a
bot or spend money rather than merely failing a test.

The central risk with a guard is not that it fails open — it is that it fails *closed* and
nobody notices. A `require_confirm` that refused unconditionally would sail through every
refusal check below while making the entire Tier-2 surface unusable. So group 5 is the
control: with a valid confirmation and dry-run off, each operation must actually reach the
transport, at the exact path it claims.

**How this file is built, and why.** An earlier version proved its properties by *watching
exceptions escape* — a check "passed" because the tripwire's `AssertionError` propagated out
of a bare `try`, and separately asserted `attempts == []` on a `Tripwire` that the risky call
had never used. Both are ways of making a check's meaning depend on control flow, and a
mutation that reroutes the control flow silently empties the check: a critic broke dry-run
and the harness died on the line *above* the assertion, which could therefore only ever print
PASS. Two rules follow, and everything below obeys them:

1. **A call's transport is bound to the call.** :class:`Harness` owns both, and
   :meth:`Harness.call` returns the delta of *that* transport across *that* call. There is no
   way to assert about a transport some other client used.
2. **No exception escapes into a check.** :meth:`Harness.call` returns an :class:`Outcome`
   holding what was raised; checks are pure predicates over recorded data. A group is a list
   of assertions on facts already gathered, so no assertion can be skipped by an earlier line
   in its own group, and no group can abort the run before the summary prints.

**Money.** `billing.refill` is never *armed* here — no call site gives it a valid token with
dry-run off. That is asserted, not asserted-about: every refill invocation in the whole run is
recorded (group 7), and the ledger's completeness is itself checked by parsing this file for
any Tier-2 call that bypasses :meth:`Harness.call`. Group 7 then proves, over that ledger,
that no refill was armed, that each was stopped by the gate or by dry-run, and that none
reached the transport its own call used — backed by a global tripwire seal that fires on a
refill escaping *any* client in *any* group.
"""

from __future__ import annotations

import ast
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closewire_client.console import configure_streams

configure_streams()

import httpx

from closewire_client.auth import ApiKeyAuth
from closewire_client.config import Config
from closewire_client.pacing import Pacer
from closewire_client.rest import RestClient
from closewire_client.session import Session
from closewire_client.tier2 import ConfirmationRequired
from closewire_client.tier2 import billing, bots, leads, personas, sources

SELF = Path(__file__).resolve()
REPO = SELF.parent.parent

FAKE_KEY = "cb_verify_tier2_not_a_real_key"
BOT = "bot_zzTESTONLY07"
PERSONA = "pers_zzTEST"
SOURCE = "src_zzTEST"
LEAD = "lead_zzTEST"
AMOUNT = 5

#: The one request path in this file that would spend real money.
MONEY_PATH = "/agency/billing/refill"

#: Every refill request that reached *any* transport built here, from *any* group. Written
#: by the Tripwire itself, below every layer under test, so it cannot be dodged by a mutation
#: that changes which client a call uses or which group it runs in.
MONEY_ESCAPES: list[str] = []

#: Every `billing.refill` invocation this run made — see :class:`RefillCall`.
REFILL_LOG: list["RefillCall"] = []

#: Every client this file builds, so the seal can sweep all of them at the end.
HARNESSES: list["Harness"] = []

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


class MissingDeliverable(Exception):
    """The function under test does not exist.

    Raised *by the harness*, never by the code under test, and deliberately not a subclass of
    anything :attr:`Outcome.refused` accepts — a deleted deliverable must fail every check,
    not read as a very thorough refusal.
    """


class Tripwire(httpx.BaseTransport):
    """Refuses to carry anything; records what tried.

    Records in two places on purpose. `self.attempts` is per-client, so a check can assert on
    the transport *its own* call used. :data:`MONEY_ESCAPES` is global, so a refill that
    escapes anywhere in the run is visible to group 7 no matter which client made it, which
    group it ran in, or whether that group finished.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        line = f"{request.method} {request.url.path}"
        self.attempts.append(line)
        if request.url.path == MONEY_PATH:
            MONEY_ESCAPES.append(line)
        raise AssertionError(f"REACHED TRANSPORT: {line}")


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class Outcome:
    """What one Tier-2 call did, as data rather than as control flow.

    `sent` is what the transport belonging to the calling :class:`Harness` recorded during
    this call and nothing else, so it answers "did *this* call send?" and cannot be confused
    with another client's history. `raised` is stored rather than propagated so a check can
    never be skipped by an exception thrown from an earlier line in its own group.
    """

    label: str
    returned: Any = None
    raised: BaseException | None = None
    sent: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        """Stopped before the wire: a gate/type/range rejection that sent nothing.

        The `not self.sent` half matters. The tripwire's own `AssertionError` is not in the
        accepted set, but a future guard that raised `ValueError` *after* sending would be —
        so the send is checked directly rather than inferred from the exception type.
        """
        return not self.sent and isinstance(
            self.raised, (ConfirmationRequired, ValueError, TypeError)
        )

    @property
    def suppressed(self) -> bool:
        """Dry-run's contract: returned the sentinel, raised nothing, sent nothing."""
        return (
            self.raised is None
            and not self.sent
            and isinstance(self.returned, dict)
            and self.returned.get("sent") is False
        )

    @property
    def why(self) -> str:
        """One short line for the check detail, whichever way the call went."""
        if self.raised is not None:
            first = str(self.raised).splitlines()[0] if str(self.raised) else ""
            return f"{type(self.raised).__name__}: {first[:64]}"
        return f"returned {self.returned!r}"[:76]


@dataclass(frozen=True)
class RefillCall:
    """One `billing.refill` invocation, with everything group 7 needs to judge it."""

    dry_run: bool
    amount: Any
    confirm: Any
    outcome: Outcome

    @property
    def token_valid(self) -> bool:
        """Would the gate have let this through? Positive int amount, token equal to it.

        Computed here from the *arguments*, independently of what the gate decided, so that a
        broken gate cannot also decide which calls count as dangerous.
        """
        return (
            isinstance(self.amount, int)
            and not isinstance(self.amount, bool)
            and self.amount > 0
            and str(self.confirm).strip() == str(self.amount).strip()
        )

    @property
    def armed(self) -> bool:
        """Both independent guards disengaged: a valid token with dry-run off.

        This is the property the money claim rests on. It is a fact about the *call site*,
        not about the outcome — so it stays true (and red) even if some third thing happened
        to stop the request.
        """
        return self.token_valid and not self.dry_run

    @property
    def stopped_by(self) -> str:
        """Which guard stopped it. Empty string means **nothing did**."""
        if self.outcome.refused:
            return "gate"
        if self.dry_run and self.outcome.suppressed:
            return "dry-run"
        return ""

    def __str__(self) -> str:
        return (
            f"refill(amount={self.amount!r}, confirm={self.confirm!r}, "
            f"dry_run={self.dry_run}) -> {self.outcome.why} sent={list(self.outcome.sent)}"
        )


class Harness:
    """A client bound to the tripwire it would send through.

    The pairing is the whole point: :meth:`call` reports what *this* transport recorded for
    *this* call, so a check cannot end up asserting about a transport the risky call never
    touched. The Pacer is real (so the write lane, budgets and dry-run gate are genuinely
    exercised) on a fake clock (so five paced writes do not cost five think-times).
    """

    def __init__(self, *, dry_run: bool) -> None:
        config = Config(api_key=FAKE_KEY, dry_run=dry_run)
        clock = _FakeClock()
        pacer = Pacer(config, monotonic=clock.monotonic, sleeper=clock.sleep)
        self.dry_run = dry_run
        self.trip = Tripwire()
        self.client = RestClient(
            config,
            Session(config, ApiKeyAuth.from_config(config), pacer, transport=self.trip),
            pacer=pacer,
        )
        HARNESSES.append(self)

    def call(self, label: str, fn: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Outcome:
        """Run one Tier-2 call and return what it did. **Never raises.**

        Every Tier-2 invocation in this file goes through here — group 7 checks that
        mechanically — which is what makes :data:`REFILL_LOG` complete rather than merely
        well-intentioned.
        """
        if fn is None:
            return Outcome(label=label, raised=MissingDeliverable(f"{label} does not exist"))
        before = len(self.trip.attempts)
        returned: Any = None
        raised: BaseException | None = None
        try:
            returned = fn(self.client, *args, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raised = exc
        outcome = Outcome(
            label=label,
            returned=returned,
            raised=raised,
            sent=tuple(self.trip.attempts[before:]),
        )
        if fn is getattr(billing, "refill", None):
            REFILL_LOG.append(
                RefillCall(
                    dry_run=self.dry_run,
                    amount=args[0] if args else None,
                    confirm=kwargs.get("confirm"),
                    outcome=outcome,
                )
            )
        return outcome


# ── The Tier-2 surface, as data ───────────────────────────────────────────────
#: Every function phase 08 names, and where it must live. Resolved with `getattr(..., None)`
#: at *use*, never at import: building this table by dereferencing the attributes would make
#: a missing deliverable an import-time `AttributeError` instead of a FAIL — which is exactly
#: how check group 1 used to become incapable of failing.
DELIVERABLES: list[tuple[str, Any, str]] = [
    ("bots.publish", bots, "publish"),
    ("bots.delete", bots, "delete"),
    ("bots.export", bots, "export"),
    ("personas.delete", personas, "delete"),
    ("sources.delete", sources, "delete"),
    ("leads.delete", leads, "delete"),
    ("billing.balance", billing, "balance"),
    ("billing.options", billing, "options"),
    ("billing.refill", billing, "refill"),
    ("billing.transactions", billing, "transactions"),
]


@dataclass(frozen=True)
class Op:
    """A token-gated operation and everything needed to test it in both directions."""

    label: str
    module: Any
    name: str
    target: Any
    valid: Any  #: a confirmation that must be ACCEPTED
    wrong: Any  #: a confirmation that must be REFUSED
    path: str  #: the request a confirmed, non-dry-run call must produce
    token: bool = True  #: a typed token is demanded on top of `confirm=True`
    money: bool = False  #: moves money — never given a valid token with dry-run off

    @property
    def fn(self) -> Any:
        return getattr(self.module, self.name, None)


OPS: list[Op] = [
    Op("bots.delete", bots, "delete", BOT, BOT, "bot_SOMETHING_ELSE", f"DELETE /bot/{BOT}"),
    Op("personas.delete", personas, "delete", PERSONA, PERSONA, "bot_SOMETHING_ELSE",
       f"DELETE /persona/{PERSONA}"),
    Op("sources.delete", sources, "delete", SOURCE, SOURCE, "bot_SOMETHING_ELSE",
       f"DELETE /agency/source/{SOURCE}"),
    Op("leads.delete", leads, "delete", LEAD, LEAD, "bot_SOMETHING_ELSE",
       f"DELETE /lead/{LEAD}"),
    Op("billing.refill", billing, "refill", AMOUNT, AMOUNT, 999, f"POST {MONEY_PATH}",
       money=True),
    Op("bots.publish", bots, "publish", BOT, True, "yes", f"POST /bot/{BOT}/publish",
       token=False),
]

def money_op() -> Op | None:
    """The one money-moving operation, found by its flag rather than by its position.

    Returns None if there is not exactly one, which group 7 fails on. Indexing `OPS` by
    position would silently test whichever op moved into the slot — a mutation that deleted
    the refill entry made group 7 exercise `bots.publish` while still printing "refill"
    labels, which is the same class of lie this file exists to remove.
    """
    found = [op for op in OPS if op.money]
    return found[0] if len(found) == 1 else None


#: Module names that, called directly, would sidestep `Harness.call`. See :func:`_bypasses`.
TIER2_MODULE_NAMES = {"bots", "personas", "sources", "leads", "billing"}


def _bypasses() -> list[str]:
    """Lines in **this file** that invoke a Tier-2 function without going through the Harness.

    The refill ledger is only as complete as this rule. A direct `billing.refill(client, 5,
    confirm=5)` anywhere below would be a refill that group 7 never saw and never judged — so
    completeness is a checked property of the source, not a promise in a comment. Naming
    `billing.refill` as a *value* (the tables above) is fine; calling it is not.
    """
    tree = ast.parse(SELF.read_text(encoding="utf-8"), str(SELF))
    return [
        f"line {node.lineno}: {node.func.value.id}.{node.func.attr}(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in TIER2_MODULE_NAMES
    ]


# ── Check groups ──────────────────────────────────────────────────────────────
def group_1_deliverables() -> None:
    for label, module, name in DELIVERABLES:
        fn = getattr(module, name, None)
        check(label, callable(fn), "" if callable(fn) else f"MISSING from {module.__name__}")


def group_2_no_confirmation() -> None:
    h = Harness(dry_run=False)
    for op in OPS:
        o = h.call(op.label, op.fn, op.target)
        check(f"{op.label} (no confirm)", o.refused, o.why)
    check("nothing reached the transport", not h.trip.attempts, f"attempts={h.trip.attempts}")


def group_3_confirm_true() -> None:
    # The dangerous near-miss: it reads as confirmed but names no target.
    h = Harness(dry_run=False)
    for op in OPS:
        if not op.token:
            continue  # publish accepts a bare True by design — group 4 covers its near-miss
        o = h.call(op.label, op.fn, op.target, confirm=True)
        check(f"{op.label} (confirm=True)", o.refused, o.why)
    check("still nothing sent", not h.trip.attempts, f"attempts={h.trip.attempts}")


def group_4_wrong_token() -> None:
    h = Harness(dry_run=False)
    for op in OPS:
        o = h.call(op.label, op.fn, op.target, confirm=op.wrong)
        label = f"{op.label} ({'wrong token' if op.token else 'truthy non-True'})"
        check(label, o.refused, o.why)
    check("still nothing sent", not h.trip.attempts, f"attempts={h.trip.attempts}")


def group_5_control() -> None:
    # Without this, a gate that refused unconditionally would pass groups 2-4 and be
    # completely broken. `billing.refill` is deliberately excluded — see group 7.
    #
    # Asserted as `sent == (path,)`, positively, on the transport this very call used. The
    # old form — "an AssertionError escaped" — was true of any AssertionError from anywhere,
    # and left the bound tripwire unexamined.
    h = Harness(dry_run=False)
    for op in OPS:
        if op.money:
            continue
        o = h.call(op.label, op.fn, op.target, confirm=op.valid)
        check(
            f"{op.label} (valid confirm) reaches the transport at {op.path}",
            o.sent == (op.path,),
            f"sent={list(o.sent)} — {o.why}",
        )


def group_6_dry_run() -> None:
    h = Harness(dry_run=True)
    for op in OPS:
        if op.money:
            continue
        o = h.call(op.label, op.fn, op.target, confirm=op.valid)
        check(f"{op.label} suppressed", o.suppressed, o.why)
        check(f"{op.label} sent nothing", not o.sent, f"sent={list(o.sent)}")
    check("dry run sent nothing", not h.trip.attempts, f"attempts={h.trip.attempts}")


def group_7_money() -> None:
    # The claim is "no refill in this validation could have reached the network", and it is
    # made over the WHOLE run: every refill any group performed is in REFILL_LOG, judged by
    # its own call site and its own transport delta.
    op = money_op()
    check("exactly one money-moving operation is declared", op is not None,
          f"money ops: {[o.label for o in OPS if o.money]}")
    refill = op.fn if op is not None else None

    h = Harness(dry_run=True)
    o = h.call("billing.refill", refill, AMOUNT, confirm=AMOUNT)
    check("valid refill under dry run is suppressed", o.suppressed, o.why)
    check("valid refill under dry run sent nothing", not o.sent, f"sent={list(o.sent)}")
    for bad, why in [(0, "zero"), (-5, "negative"), (5.0, "float"), (True, "bool")]:
        bad_o = h.call("billing.refill", refill, bad, confirm=bad)
        check(f"refill rejects {why} amount", bad_o.refused, bad_o.why)

    # ── the verdict, over every refill in the run ─────────────────────────────
    bypasses = _bypasses()
    check("no Tier-2 call in this file bypasses Harness.call (so the ledger is complete)",
          not bypasses, "; ".join(bypasses))

    # Anti-vacuity first: every "all refills were X" below is trivially true of no refills.
    check("refill was actually exercised", len(REFILL_LOG) >= 8,
          f"{len(REFILL_LOG)} invocation(s) across {len({id(h) for h in HARNESSES})} client(s)")
    check("a fully-valid refill token was exercised",
          any(r.token_valid for r in REFILL_LOG),
          f"{sum(r.token_valid for r in REFILL_LOG)} with a token the gate must accept")
    check("a refill ran against a NON-dry-run client",
          any(not r.dry_run for r in REFILL_LOG),
          f"{sum(not r.dry_run for r in REFILL_LOG)} invocation(s) with dry-run off")

    armed = [str(r) for r in REFILL_LOG if r.armed]
    check("NO refill was ever ARMED — no call site pairs a valid token with dry-run off",
          not armed, "; ".join(armed))
    unstopped = [str(r) for r in REFILL_LOG if not r.stopped_by]
    check("every refill was stopped by the gate or by dry-run", not unstopped,
          "; ".join(unstopped) or "stopped_by="
          + str(sorted({r.stopped_by for r in REFILL_LOG})))
    escaped = [str(r) for r in REFILL_LOG if r.outcome.sent]
    check("no refill reached the transport its own call used", not escaped, "; ".join(escaped))


def group_8_tier_boundary() -> None:
    # `cli/main.py` is the composition root: it imports every tier in order to wire them
    # together, and routes on (group, action). Excluding it is not a loophole — the property
    # worth proving is that a *lower-tier* module cannot reach a Tier-2 function, so that
    # `cli/reads.py` and the read/write client packages stay incapable of deleting anything.
    # A router that knows about all three tiers is what makes that separation enforceable.
    router = {"cli/main.py"}
    bad: list[str] = []
    scanned = 0
    for directory in ("closewire_client/endpoints", "closewire_client/writes", "cli"):
        for path in sorted((REPO / directory).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if path.name == "tier2.py" or rel in router:
                continue
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), rel)
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    # The imported NAMES matter as much as the module: `from
                    # closewire_client import tier2` names the package in `node.names`, not
                    # in `node.module`, so checking only the module missed the idiomatic
                    # spelling entirely and the boundary could be breached with this check
                    # still green.
                    base = ("." * node.level) + (node.module or "")
                    mods = [base] + [f"{base}.{a.name}" for a in node.names]
                # Match a *path segment*, not a substring. `tier2` appears inside
                # `writes._tier.reject_tier2_fields` — a function name, not the package —
                # and a substring test flagged both `writes/` modules as boundary breaches.
                # Over-reporting is not the safe direction here: a check that cries wolf on
                # correct code gets read as noise, and the real breach next to it gets
                # skimmed past.
                if any("tier2" in m.split(".") for m in mods):
                    bad.append(f"{rel}:{node.lineno}")
    # A scan that found no files would pass this vacuously, so the file count is checked too.
    check("the tier-boundary scan actually read the lower tiers", scanned >= 10,
          f"{scanned} module(s) scanned")
    check("no endpoints/, writes/ or read-CLI module imports tier2", not bad, f"{bad}")


GROUPS: list[tuple[str, Callable[[], None]]] = [
    ("Every Tier-2 deliverable exists", group_1_deliverables),
    ("No confirmation → refuses, sends nothing", group_2_no_confirmation),
    ("confirm=True is NOT enough for a destructive or financial op", group_3_confirm_true),
    ("A mismatched token refuses", group_4_wrong_token),
    ("CONTROL — a VALID confirmation must actually reach the transport", group_5_control),
    ("Dry run suppresses even a VALID confirmation", group_6_dry_run),
    ("MONEY — no refill was armed, and none reached a transport", group_7_money),
    ("Tier boundary — lower tiers cannot reach Tier-2", group_8_tier_boundary),
]


def main() -> int:
    for number, (title, fn) in enumerate(GROUPS, start=1):
        print(f"\n{number}. {title}")
        try:
            fn()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            # A group that blew up must not take the remaining groups, the money seal, or
            # the failure summary down with it — that is how a mutation once hid three
            # whole groups. It is recorded as a failure of that group and the run goes on.
            traceback.print_exc()
            check(f"group {number} ran to completion", False,
                  f"the harness raised {type(exc).__name__}: {str(exc).splitlines()[0][:70]}")

    # Re-read below everything, after every group: the tripwires themselves, not the ledger.
    # If any refill request had escaped in any group — including one added later, or one that
    # ran inside a group that then crashed — it is here.
    print("\nSEAL — re-checked after every group above, at the transport")
    check("no refill reached ANY transport built in this run", not MONEY_ESCAPES,
          f"escapes={MONEY_ESCAPES}")
    check("every client in this run was built on a tripwire",
          bool(HARNESSES) and all(isinstance(h.trip, Tripwire) for h in HARNESSES),
          f"{len(HARNESSES)} client(s)")

    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
