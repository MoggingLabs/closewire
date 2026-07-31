"""Phase-04 pacing demo — shows the Pacer's behavior without touching the network.

Five scenes, each printing what the Pacer actually did:

1. ten fake reads      -> randomized gaps inside [min, max+jitter], never fixed
2. reads vs writes     -> writes are strictly slower
3. serial writes       -> only ever one write in flight; reads run bounded-parallel
4. budget exhaustion   -> a low hourly ceiling BLOCKS until the window frees
5. 429 storm + dry-run -> exponential backoff, breaker trip, suppressed write

Scenes 1-4 use a fake clock so a one-hour budget wait costs no real time. Run:

    python scripts/pacing_demo.py

No API key, no network, no secrets.
"""

from __future__ import annotations

import random
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closewire_client.config import Config
from closewire_client.pacing import WINDOW_S, Pacer, PacingHalt


class FakeClock:
    """Monotonic clock that advances only when something sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def build(**overrides: object) -> tuple[Pacer, FakeClock]:
    base: dict[str, object] = {
        "api_key": "not-a-real-key",
        "min_delay_s": 1.0,
        "max_delay_s": 4.0,
        "jitter_s": 0.35,
        "write_delay_mult": 2.0,
        # Scene 5 trips the breaker, which is persisted so a halt survives a restart.
        # Keep that out of the real state dir — a demo must not halt your actual client.
        "state_dir": tempfile.mkdtemp(prefix="closewire-demo-"),
    }
    base.update(overrides)
    clock = FakeClock()
    pacer = Pacer(
        Config(**base),  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        rng=random.Random(),
    )
    return pacer, clock


def head(n: int, title: str) -> None:
    print(f"\n{'=' * 68}\n{n}. {title}\n{'=' * 68}")


def bar(seconds: float, scale: float = 6.0) -> str:
    return "#" * max(1, int(seconds * scale))


# ── 1 ─────────────────────────────────────────────────────────────────────────
def scene_randomized_gaps() -> None:
    head(1, "Ten fake reads — randomized think-time")
    pacer, clock = build()
    for i in range(10):
        with pacer.acquire(write=False, description=f"GET /fake/{i}"):
            pass
    gaps = clock.slept
    for i, g in enumerate(gaps, 1):
        print(f"   call {i:>2}   {g:5.2f}s  {bar(g)}")
    print("\n   band      [1.00 .. 4.35]s (min..max+jitter)")
    print(f"   observed  [{min(gaps):.2f} .. {max(gaps):.2f}]s")
    print(f"   distinct  {len(set(gaps))}/10 values -> randomized, not a fixed interval")
    assert all(1.0 <= g <= 4.35 for g in gaps)


# ── 2 ─────────────────────────────────────────────────────────────────────────
def scene_writes_slower() -> None:
    head(2, "Writes are held stricter than reads")
    pacer, clock = build()
    for _ in range(15):
        with pacer.acquire(write=False):
            pass
    read_avg = sum(clock.slept) / len(clock.slept)

    pacer2, clock2 = build()
    for _ in range(15):
        with pacer2.acquire(write=True):
            pass
    write_avg = sum(clock2.slept) / len(clock2.slept)

    print(f"   mean read  delay  {read_avg:5.2f}s  {bar(read_avg)}")
    print(f"   mean write delay  {write_avg:5.2f}s  {bar(write_avg)}")
    print(f"\n   writes are {write_avg / read_avg:.1f}x slower (CLOSEWIRE_WRITE_DELAY_MULT)")
    assert write_avg > read_avg


# ── 3 ─────────────────────────────────────────────────────────────────────────
def scene_concurrency() -> None:
    head(3, "Concurrency — writes serial, reads bounded")
    for label, write, limit in (("writes", True, 1), ("reads", False, 3)):
        pacer, _ = build(max_read_concurrency=3)
        peak = current = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal peak, current
            with pacer.acquire(write=write):
                with guard:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.02)
                with guard:
                    current -= 1

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        verdict = "OK" if peak <= limit else "FAIL"
        print(f"   6 concurrent {label:<7} -> peak in-flight {peak} (limit {limit})  [{verdict}]")
        assert peak <= limit


# ── 4 ─────────────────────────────────────────────────────────────────────────
def scene_budget_block() -> None:
    head(4, "Hourly budget — a low ceiling BLOCKS until the window frees")
    pacer, clock = build(
        max_ops_per_hour=3, min_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0
    )
    print("   CLOSEWIRE_MAX_OPS_PER_HOUR=3, issuing 5 calls\n")
    for i in range(1, 6):
        before = clock.now
        with pacer.acquire():
            pass
        waited = clock.now - before
        note = f"BLOCKED {waited:8.0f}s for budget" if waited > 1 else "sent immediately"
        print(f"   call {i}  t={clock.now:8.0f}s  {note}")
    stats = pacer.stats()
    print(f"\n   budget waits: {stats.budget_waits}  |  total waited: {stats.total_budget_wait_s:.0f}s")
    print(f"   window is {WINDOW_S:.0f}s and slides — calls are delayed, never dropped")
    # One wait, not two: the window is a sliding hour, so when call 4 waits it out, all
    # three earlier ops age out together and call 5 finds the budget free again.
    assert stats.budget_waits == 1


# ── 5 ─────────────────────────────────────────────────────────────────────────
def scene_backoff_breaker_dryrun() -> None:
    head(5, "429 storm -> backoff -> breaker, and dry-run suppression")
    pacer, _ = build(
        backoff_base_s=2.0, backoff_cap_s=60.0, backoff_jitter_s=0.0,
        max_retries=32, breaker_429_threshold=5,
    )
    print("   simulated 429s:")
    for attempt in range(10):
        try:
            decision = pacer.note_response(429, attempt=attempt)
        except PacingHalt as exc:
            print(f"\n   BREAKER TRIPPED on 429 #{attempt + 1}")
            print(f"   -> {exc}")
            break
        print(f"     429 #{attempt + 1}  back off {decision.backoff_s:5.1f}s  {bar(decision.backoff_s, 1)}")
    else:
        raise AssertionError("breaker should have tripped")

    print("\n   with the breaker OPEN, further calls are refused:")
    try:
        with pacer.acquire():
            print("     ...call went through (WRONG)")
    except PacingHalt:
        print("     PacingHalt raised — no traffic leaves. Manual reset required.")

    pacer.reset_breaker()
    print("   reset_breaker() -> traffic resumed\n")

    dry, _ = build(dry_run=True)
    with dry.acquire(write=True, description="POST /bot") as slot:
        print(f"   dry-run write  POST /bot  -> blocked={slot.dry_run_blocked} (nothing sent)")
    with dry.acquire(write=False, description="GET /bot") as slot:
        print(f"   dry-run read   GET  /bot  -> blocked={slot.dry_run_blocked} (reads unaffected)")
    stats = dry.stats()
    print(f"\n   suppressed writes still counted: writes={stats.total_writes}, "
          f"dry_run_blocked={stats.dry_run_blocked}")
    print("\n   --- pacing_status ---")
    print(stats.render())
    assert stats.dry_run_blocked == 1 and stats.total_writes == 1


def main() -> int:
    print("Closewire phase-04 pacing demo (fake clock — no network, no key)")
    scene_randomized_gaps()
    scene_writes_slower()
    scene_concurrency()
    scene_budget_block()
    scene_backoff_breaker_dryrun()
    print(f"\n{'=' * 68}\nAll pacing scenes behaved as specified.\n{'=' * 68}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
