#!/usr/bin/env python3
"""One entry point for both halves of the app: the DRAFT console (pre-season) and the
MANAGE board (in-season, fftiers). Runs the stage scripts in order; stops on the first
failure.

    draft:   scrape - calibrate - csg - build - tool_data.json - inject - /draft
    manage:  pull (ESPN + FantasyPros) - tiers - vbd-boards - csg-boards - board
                 (dat/espn/*, dat/{year}/*)  (out/{league}/week-N/{vbd,csg})
             board = viz-data + inject - static/board.html - /manage

Usage:
    python3 pipeline.py all                 # draft refresh: calibrate -> csg -> build -> inject
    python3 pipeline.py build inject        # rebuild the draft console from current data only
    python3 pipeline.py scrape calibrate build inject   # full draft refresh from ESPN
    python3 pipeline.py week                # weekly manage refresh: pull -> ... -> board
    python3 pipeline.py sync                # fast refresh: rosters/lineups/weekly pts only
    python3 pipeline.py board               # re-render the manage board from cached data
    python3 pipeline.py week --league 2kdome            # one league only

Stages run in the order you list them. Flags:
    --deep         with `scrape`: also pull full multi-season history (scraping/scrape.py)
    --stress       with `simulate`: also run the strategy stress test (a19)
    --league KEY   manage stages: limit to one league (default: all in config/boards.json)
    --no-download  tiers/vbd-boards/csg-boards: render from cached rankings only

The draft `all` runs opponent calibration only when the local analysis pipeline AND
scraped history are present; otherwise opponents stay neutral and the console still
builds. Manage stages need config/boards.json (see config/boards.example.json) and use
the repo venv (fftiers + scikit-learn/matplotlib) — they self-skip with a hint otherwise.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

# Windows consoles default to a legacy codepage (cp1252) that cannot encode the status
# glyphs this pipeline and its stage scripts print (✓ ✗ • × →). Force UTF-8 on our own
# streams, and via the environment on every child stage, so output is not locale-dependent.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DRAFT_STAGES = ("scrape", "calibrate", "csg", "simulate", "build", "inject", "all")
# Manage (in-season) stages — the fftiers board. Each fans out over every league in
# config/boards.json unless --league narrows it.
BOARD_STAGES = ("pull", "sync", "tiers", "vbd-boards", "csg-boards", "board", "week")
STAGES = DRAFT_STAGES + BOARD_STAGES

TEMPLATE = os.path.join(ROOT, "draft_sheets", "draft_tool_template.html")
TOOL_DATA = os.path.join(ROOT, "draft_sheets", "tool_data.json")
STATIC = os.path.join(ROOT, "draft_app", "static")
PROJECTIONS = os.path.join(ROOT, "draft_sheets", "elboberto_projections.json")


def run(*cmd):
    """Run a stage script from the repo root; abort the pipeline if it fails."""
    print(f"\n\033[1m$ {' '.join(os.path.relpath(c, ROOT) if os.path.isabs(c) else c for c in cmd)}\033[0m",
          flush=True)  # flush so the banner prints before the child's own output
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
        sys.exit(f"\n✗ stage failed: {' '.join(cmd)}")


def have_calibration():
    """True when the (local) calibration pipeline can run: its script + scraped auction
    history — meaning at least one scraped season that actually contains draft picks.

    The mere existence of a league_full.json is NOT enough: the fresh-setup scraper
    (scrape_league.py) writes settings + managers for the CURRENT
    season with no draftDetail, while build_agents() reads PRIOR seasons' picks. Checking
    only for the file made `all` and `calibrate` die with FileNotFoundError on a
    brand-new league, instead of skipping calibration the way the README promises.
    """
    if not os.path.exists(os.path.join(ROOT, "analysis", "calibrate.py")):
        return False
    for path in glob.glob(os.path.join(ROOT, "scraping", "raw", "*", "league_full.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, list):
            d = d[0] if d else {}
        if (d.get("draftDetail") or {}).get("picks"):
            return True
    return False


# ───────────────────────────────── stages ─────────────────────────────────
def scrape(args):
    run(PY, os.path.join(ROOT, "scraping", "scrape_league.py"))
    if args.deep:
        run(PY, os.path.join(ROOT, "scraping", "scrape.py"))


def calibrate(args):
    if not have_calibration():
        print("• calibrate: skipped — no local analysis/ pipeline, or no scraped season "
              "with draft picks (scraping/raw/*/league_full.json → draftDetail.picks). "
              "Opponents stay neutral.")
        return
    # projections cache the calibration reads (regenerated from the tracked workbooks)
    run(PY, os.path.join(ROOT, "draft_sheets", "extract_elboberto_master.py"))
    run(PY, os.path.join(ROOT, "analysis", "calibrate.py"))


def csg(args):
    """Complementary market view: the CSG sheet's consensus columns -> csg_consensus.json.

    Self-skips when no CSG workbook is present, so `all` stays valid for anyone who
    doesn't use the sheet. Advisory only — it never feeds the console's worth/vbd.
    """
    if not glob.glob(os.path.join(ROOT, "draft_sheets", "CSG*auction.xlsm")):
        print("• csg: skipped — no draft_sheets/CSG*auction.xlsm (market view is optional).")
        return
    run(PY, os.path.join(ROOT, "draft_sheets", "extract_csg.py"))


def simulate(args):
    if not have_calibration():
        sys.exit("simulate needs the local analysis/ pipeline and scraped history.")
    run(PY, os.path.join(ROOT, "analysis", "a18_agent_auction.py"))
    if args.stress:
        run(PY, os.path.join(ROOT, "analysis", "a19_stress_test.py"))


def build(args):
    run(PY, os.path.join(ROOT, "draft_sheets", "build_tool_data.py"))


def inject(args):
    """Golden rule: the served console is generated — template + injected data, never hand-edited."""
    if not os.path.exists(TOOL_DATA):
        sys.exit(f"inject: {os.path.relpath(TOOL_DATA, ROOT)} missing — run `build` first.")
    # encoding is explicit: the template carries non-ASCII glyphs (◎ ☾ ⊘, ·) and Python
    # defaults to the locale codec on Windows (cp1252), which cannot read or write them.
    tpl = open(TEMPLATE, encoding="utf-8").read()
    data = open(TOOL_DATA, encoding="utf-8").read()
    if "/*DATA*/" not in tpl:
        sys.exit("inject: template is missing the /*DATA*/ marker.")
    os.makedirs(STATIC, exist_ok=True)
    with open(os.path.join(STATIC, "index.html"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*DATA*/", data))
    with open(os.path.join(STATIC, "data.json"), "w", encoding="utf-8") as f:
        f.write(data)
    print(f"• inject: wrote {os.path.relpath(os.path.join(STATIC, 'index.html'), ROOT)} "
          f"and static/data.json ({len(data):,} bytes of data)")


def do_all(args):
    calibrate(args)   # self-skips for a fresh league
    csg(args)         # self-skips when the CSG sheet isn't present
    build(args)
    inject(args)


# ───────────────────────────── manage (board) stages ─────────────────────────────
# The manage board is multi-league: stages fan out over every league registered in
# config/boards.json. --league narrows them. They run in the repo venv because the
# fftiers package (scikit-learn, matplotlib, PyYAML) is installed there.

BOARDS_CONFIG = os.path.join(ROOT, "config", "boards.json")


def venv_py():
    """The interpreter that has fftiers installed (falls back to whoever runs us)."""
    cand = os.path.join(ROOT, ".venv", "bin", "python")
    return cand if os.path.exists(cand) else PY


def boards_leagues(args):
    """config/boards.json -> {key: entry}, honoring --league. None = stage must skip."""
    if not os.path.exists(BOARDS_CONFIG):
        print("• skipped — no config/boards.json (see config/boards.example.json).")
        return None
    import json
    leagues = json.load(open(BOARDS_CONFIG))["leagues"]
    if getattr(args, "league", None):
        if args.league not in leagues:
            sys.exit(f"--league {args.league}: not in config/boards.json "
                     f"(have: {', '.join(sorted(leagues))})")
        leagues = {args.league: leagues[args.league]}
    return leagues


def _league_args(args):
    return ["--league", args.league] if getattr(args, "league", None) else []


def _meta_week(key):
    """Current week for a league, from the pull snapshot. None until `pull` has run."""
    import json
    p = os.path.join(ROOT, "dat", "espn", f"{key}-meta.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p)).get("week")


def pull(args):
    """ESPN (projections, roster, meta) + FantasyPros rankings -> dat/."""
    if boards_leagues(args) is None:
        return
    run(venv_py(), "-m", "fftiers.board", "pull", *_league_args(args))


def tiers(args):
    """Boris Chen-style tier charts per league -> out/{key}/week-N/{png,txt,csv}."""
    leagues = boards_leagues(args)
    if leagues is None:
        return
    extra = ["--no-download"] if getattr(args, "no_download", False) else []
    for key, entry in leagues.items():
        run(venv_py(), "-m", "fftiers.cli", "--league", entry["yaml"], *extra)


def _value_boards(args, module):
    """Shared driver for the elboberto (vbd) and CSG value boards, both horizons."""
    leagues = boards_leagues(args)
    if leagues is None:
        return
    for key, entry in leagues.items():
        week_n = _meta_week(key)
        if week_n is None:
            print(f"• {key}: skipped — no dat/espn/{key}-meta.json (run `pull` first).")
            continue
        for horizon, csv_name in (("week", f"{key}-week-{week_n}.csv"),
                                  ("ros", f"{key}-ros-from-{week_n}.csv")):
            csv_path = os.path.join(ROOT, "dat", "espn", csv_name)
            if not os.path.exists(csv_path):
                print(f"• {key}/{horizon}: skipped — missing {os.path.relpath(csv_path, ROOT)} "
                      "(run `pull` first).")
                continue
            run(venv_py(), "-m", module, "--league", entry["yaml"], "--week", str(week_n),
                "--horizon", horizon, "--projections-csv", csv_path)


def vbd_boards(args):
    """elboberto VBD boards (league-scored) -> out/{key}/week-N/vbd/."""
    _value_boards(args, "fftiers.vbd_cli")


def csg_boards(args):
    """CSG games-based VBD boards -> out/{key}/week-N/csg/."""
    _value_boards(args, "fftiers.csg_cli")


def board(args):
    """viz-data.json + template -> static/board.html (the manage half of the golden rule)."""
    if boards_leagues(args) is None:
        return
    run(venv_py(), "-m", "fftiers.board", "build", *_league_args(args))


def sync(args):
    """The fast refresh: live rosters/lineups/weekly projections, cached forecasts.

    Seconds instead of minutes — ROS totals and FantasyPros ranks stay as-is
    (run `week` for those). This is the check-my-lineup-against-ESPN loop.
    """
    if boards_leagues(args) is None:
        return
    run(venv_py(), "-m", "fftiers.board", "sync", *_league_args(args))
    vbd_boards(args)
    csg_boards(args)
    league, args.league = getattr(args, "league", None), None
    board(args)
    args.league = league


def week(args):
    """The weekly manage refresh: everything from live data to a served board."""
    pull(args)
    args.no_download = True   # pull just refreshed the FantasyPros caches
    tiers(args)
    vbd_boards(args)
    csg_boards(args)
    # Render EVERY league with data, even when --league narrowed the fetch stages —
    # a one-league refresh must not clobber the other leagues' tabs on the board.
    league, args.league = getattr(args, "league", None), None
    board(args)
    args.league = league


DISPATCH = {"scrape": scrape, "calibrate": calibrate, "csg": csg, "simulate": simulate,
            "build": build, "inject": inject, "all": do_all,
            "pull": pull, "sync": sync, "tiers": tiers, "vbd-boards": vbd_boards,
            "csg-boards": csg_boards, "board": board, "week": week}


def main():
    ap = argparse.ArgumentParser(
        description="Run the draft console or manage-board pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="stages: " + " ".join(STAGES))
    ap.add_argument("stages", nargs="+", choices=STAGES, metavar="STAGE",
                    help="one or more of: " + ", ".join(STAGES))
    ap.add_argument("--deep", action="store_true", help="with scrape: also pull full history")
    ap.add_argument("--stress", action="store_true", help="with simulate: also run a19 stress test")
    ap.add_argument("--league", metavar="KEY",
                    help="manage stages: limit to one league (default: all in config/boards.json)")
    ap.add_argument("--no-download", action="store_true",
                    help="tiers/vbd-boards/csg-boards: use cached rankings, no FantasyPros fetch")
    args = ap.parse_args()

    print(f"pipeline: {' -> '.join(args.stages)}")
    for stage in args.stages:
        DISPATCH[stage](args)
    print("\n✓ done.")


if __name__ == "__main__":
    main()
