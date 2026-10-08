import argparse
import sys

from . import breakit, checks, dedupe, demo, seed, simulate, stats, sync
from .tables import TABLES


def main(argv=None):
    p = argparse.ArgumentParser(prog="fundo")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="(re)create the source database with synthetic data")
    load = sub.add_parser("load", help="incremental load source -> warehouse")
    names = [t.name for t in TABLES]
    load.add_argument("--full-refresh", nargs="*", default=[], choices=names, metavar="TABLE")
    load.add_argument("--crash-after", choices=names, metavar="TABLE", help="kill the process after applying TABLE (demo)")
    load.add_argument("--replay", action="store_true", help="re-apply the last incremental window (demo)")
    sub.add_parser("simulate", help="apply one simulated business day of changes to the source")
    sub.add_parser("check", help="is the warehouse complete and correct right now? (exit 1 if not)")
    dd = sub.add_parser("dedupe", help="resolve duplicate customers (dry run unless --apply)")
    mode = dd.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="execute the plan (backup + one transaction)")
    mode.add_argument("--undo", action="store_true", help="reverse the last applied run")
    sub.add_parser("break", help="damage the warehouse on purpose (for the failing-check demo)")
    sub.add_parser("repair", help="full-refresh every table the check fails on, then check again")
    sub.add_parser("stats", help="source sizes, change tracking overhead, bytes moved per load")
    sub.add_parser("demo", help="the whole story end to end")
    args = p.parse_args(argv)
    if args.cmd == "seed":
        seed.run()
    elif args.cmd == "simulate":
        simulate.run()
    elif args.cmd == "check":
        return 1 if checks.run() else 0
    elif args.cmd == "dedupe":
        dedupe.undo() if args.undo else dedupe.run(apply_changes=args.apply)
    elif args.cmd == "break":
        breakit.run()
    elif args.cmd == "repair":
        failing = sorted({r.table for r in checks.run(quiet=True)} & {t.name for t in TABLES})
        if not failing:
            print("check passes; nothing to repair")
            return 0
        print(f"repairing {', '.join(failing)} with a full refresh from the source")
        sync.run(full_refresh=failing)
        return 1 if checks.run() else 0
    elif args.cmd == "stats":
        stats.run()
    elif args.cmd == "demo":
        demo.run()
    elif args.cmd == "load":
        sync.run(full_refresh=args.full_refresh, crash_after=args.crash_after, replay=args.replay)


if __name__ == "__main__":
    sys.exit(main())
