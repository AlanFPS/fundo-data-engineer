"""The whole story, end to end: `make demo`."""
import subprocess
import sys
import time

from . import breakit, checks, db, dedupe, seed, simulate, stats, sync

STEPS = 9


def step(n, title):
    print(f"\n{'=' * 100}\n[{n}/{STEPS}] {title}\n{'=' * 100}")


def expect(ok, what):
    print(f"  -> {'OK' if ok else 'UNEXPECTED'}: {what}")
    if not ok:
        sys.exit(1)


def run():
    t0 = time.time()

    step(1, "Seed the source: SQL Server with customers, advances, cards, transactions, history, a scratch table")
    seed.run()

    step(2, "Initial load: no watermark yet, so every table is a full snapshot")
    full_rows, full_bytes = sync.run()

    step(3, "One business day happens at the source (~1% of rows: inserts, updates, deletes)")
    simulate.run()

    step(4, "Check BEFORE loading: the warehouse is behind, not wrong, so the check passes and reports lag")
    expect(not checks.run(), "check passes; source changes after the last load are reported as lag")

    step(5, "Incremental load: only what changed. Compare the counts with step 3")
    rows, size = sync.run()
    print(f"  -> moved {rows:,} rows / {sync.fmt_bytes(size)} instead of {full_rows:,} rows / "
          f"{sync.fmt_bytes(full_bytes)} for a full copy ({size / full_bytes:.2%} of the bytes)")
    expect(not checks.run(quiet=True), "check passes after the incremental load")

    step(6, "Idempotency: run again (nothing to do), then replay the last window on purpose (same result)")
    sync.run()
    before = sync.fingerprint()
    sync.run(replay=True)
    after = sync.fingerprint()
    print(f"  warehouse fingerprint before replay {before}\n  warehouse fingerprint after replay  {after}")
    expect(before == after, "replaying an applied window changes nothing")

    step(7, "Failure mid-run: another day, then the load is killed after applying 3 of 5 tables")
    simulate.run()
    subprocess.run([sys.executable, "-m", "fundo", "load", "--crash-after", "Cards"])
    print("  after the crash the warehouse still shows the previous, consistent state:")
    expect(not checks.run(), "nothing partial landed; the check passes and shows the unloaded changes as lag")
    print("  rerun the load: it starts from the last committed watermark")
    sync.run()
    expect(not checks.run(quiet=True), "recovered; check passes")

    step(8, "Duplicate customers: plan, apply (backup + one transaction + change log), then try to re-create one")
    dedupe.run(apply_changes=True)
    src = db.source()
    expect(dedupe.survivors_without_default_card(src.cursor()) == 0,
           "every survivor that received an active card has an active default card to charge")
    src.close()
    print("  the merge is just more source changes; the pipeline picks them up (Customers gained columns -> full refresh):")
    sync.run()
    print("  another day, in which two existing customers try to sign up again:")
    simulate.run()
    sync.run()
    expect(not checks.run(), "warehouse matches source after the merge")

    step(9, "Break the warehouse on purpose, watch the check fail, repair, watch it pass")
    breakit.run()
    failures = checks.run()
    expect(bool(failures), f"the check fails with {len(failures)} problems (`make check` exits non-zero)")
    failing = sorted({r.table for r in failures})
    print(f"  repair: full refresh of {', '.join(failing)} from the source")
    sync.run(full_refresh=failing)
    expect(not checks.run(), "repaired; check passes")

    print(f"\n{'=' * 100}\nMeasurements\n{'=' * 100}")
    stats.run()
    print(f"\ndemo finished in {time.time() - t0:.0f}s. Reports in reports/, warehouse in data/warehouse.duckdb")
