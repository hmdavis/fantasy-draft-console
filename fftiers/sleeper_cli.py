"""CLI: fftiers-sleeper - live Sleeper data into this repo's formats.

    fftiers-sleeper sync-league 1234567890 --dest leagues/my-league.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .paths import repo_root
from .sleeper import SleeperLeague, write_league_yaml


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fftiers-sleeper",
                                 description="Pull live Sleeper league data.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sync-league", help="Fetch settings -> leagues/<key>.yaml")
    sp.add_argument("league_id")
    sp.add_argument("--dest", default=None)
    args = ap.parse_args(argv)

    lg = SleeperLeague.load(args.league_id)
    dest = Path(args.dest) if args.dest else (
        repo_root() / "leagues" / f"sleeper-{args.league_id}.yaml")
    warnings = write_league_yaml(lg, dest)
    slots, _ = lg.roster_slots()
    print(f"{lg.name}: {lg.league.get('total_rosters')} teams, roster {slots}, "
          f"week {lg.current_week} of {lg.final_week}")
    for w in warnings:
        print(f"  ! {w}")
    print(f"  -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
