#!/usr/bin/env python3
"""Offline test for the manage board's `pull` and `sync` (ESPN and Sleeper).

Replaces the network calls with synthetic payloads (fake players, fake leagues)
and checks the files under dat/espn/ that the vbd, csg, and board stages read.

    .venv/bin/python tests/board_pull.py
"""
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fftiers import board, espn, fetch, sleeper  # noqa: E402

EXAMPLE_YAML = "leagues/example-standard-12.yaml"
SCORING = {"pass_yd": 0.04, "pass_td": 5.0, "rush_yd": 0.1, "rush_td": 6.0, "rec": 0.5,
           "rec_yd": 0.1, "rec_td": 6.0, "rush_fd": 0.5, "xpm": 1.0, "sack": 1.0}
LEAGUE = {"league_id": "100", "name": "Fixture Sleeper League", "season": "2026",
          "total_rosters": 2, "scoring_settings": SCORING,
          "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"],
          "settings": {"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 0,
                       "reserve_slots": 1, "start_week": 1}}
USERS = [{"user_id": "u1", "display_name": "fixture-me", "metadata": {"team_name": "Fixture Franchise"}},
         {"user_id": "u2", "display_name": "fixture-rival", "metadata": {}}]
ROSTERS = [{"roster_id": 1, "owner_id": "u1", "players": ["1", "2", "3", "4", "DAL"],
            "starters": ["1", "2", "0", "4", "0", "0", "DAL"], "reserve": ["3"]},
           {"roster_id": 2, "owner_id": "u2", "players": ["5"], "starters": ["5"], "reserve": None}]
STATE = {"week": 4, "season": "2026", "season_type": "regular", "season_start_date": "2026-09-09"}


def proj(pid, first, last, pos, stats, injury=None):
    return {"player_id": pid, "stats": stats,
            "player": {"first_name": first, "last_name": last, "position": pos,
                       "injury_status": injury}}


def projections(week):
    return [proj("1", "Alpha", "Quarterback", "QB", {"pass_yd": 250, "pass_td": 2}),
            proj("2", "Alpha", "Runner", "RB", {"rush_yd": 80, "rush_fd": 4, "rec": 2}),
            proj("3", "Bravo", "Runner", "RB", {}, injury="IR"),
            proj("4", "Alpha", "Tightend", "TE", {"rec": 4, "rec_yd": 40}, injury="Sus"),
            proj("5", "Bravo", "Wideout", "WR", {"rec": 6, "rec_yd": 70, "rec_td": 1}),
            proj("6", "Golf", "Wideout", "WR", {"rec": 3, "rec_yd": 30}),
            proj("DAL", "Dallas", "Cowboys", "DEF", {"sack": 3}),
            proj("9", "Punter", "Person", "P", {"xpm": 1})]


def fake_sleeper_fetch(url):
    if "/projections/nfl/2026/" in url:
        week = int(url.split("/projections/nfl/2026/")[1].split("?")[0])
        assert "position%5B%5D=DEF" in url and "season_type=regular" in url, url
        return projections(week)
    tail = url.removeprefix(sleeper.API + "/")
    return {"league/100": LEAGUE, "league/100/users": USERS, "league/100/rosters": ROSTERS,
            "state/nfl": STATE}[tail]


ESPN_CALLS = []


def install_fakes():
    sleeper.fetch = fake_sleeper_fetch
    sleeper.time.sleep = lambda s: None
    board.time.sleep = lambda s: None
    fetch.download = lambda data_dir, year, week, pos, scoring, key: ESPN_CALLS.append(
        ("fp", week, pos, scoring, key)) or fetch.cache_path(data_dir, year, week, pos, scoring)

    def cookie():
        ESPN_CALLS.append(("cookie",))
        return "fake"
    espn.auth_cookie = cookie
    espn.league_settings = lambda lid, season, c: {
        "name": "Fixture ESPN League", "current_week": 4, "final_week": 17,
        "my_teams": {1: "Fixture Franchise", 2: "Rival Robots"}}
    pool = [{"id": 11, "player": "Alpha Quarterback", "pos": "QB", "points": 20.5,
             "team_id": 1, "rostered": "yes", "injury": "ACTIVE"},
            {"id": 12, "player": "Golf Wideout", "pos": "WR", "points": 9.0,
             "team_id": 0, "rostered": "no", "injury": ""}]
    espn.player_pool = lambda lid, season, week, c: [dict(p) for p in pool]
    espn.ros_pool = lambda lid, season, wk, final, c: [dict(p, points=p["points"] * 14) for p in pool]
    espn.league_rosters = lambda lid, season, c: {
        1: [{"id": 11, "name": "Alpha Quarterback", "pos": "QB", "injury": "ACTIVE", "slot": "QB"}],
        2: []}


def write_boards(tmp, leagues):
    path = Path(tmp) / "boards.json"
    path.write_text(json.dumps({"season": 2026, "leagues": leagues}))
    return str(path)


def read_csv(path):
    with open(path) as f:
        return {r["player"]: r for r in csv.DictReader(f)}


def ok(msg):
    print(f"  OK  {msg}")


def check_sleeper(espn_dir):
    meta = json.loads((espn_dir / "sl-meta.json").read_text())
    assert meta == {**meta, "week": 4, "final_week": 17, "team": "Fixture Franchise",
                    "league_name": "Fixture Sleeper League", "platform": "sleeper"}, meta
    week = read_csv(espn_dir / "sl-week-4.csv")
    assert float(week["Alpha Quarterback"]["points"]) == 20.0, week["Alpha Quarterback"]
    assert float(week["Alpha Runner"]["points"]) == 11.0, week["Alpha Runner"]
    assert week["Dallas Cowboys"]["pos"] == "DST" and "Punter Person" not in week
    assert week["Golf Wideout"]["rostered"] == "no" and week["Bravo Runner"]["rostered"] == "yes"
    ros = read_csv(espn_dir / "sl-ros-from-4.csv")
    assert float(ros["Alpha Quarterback"]["points"]) == 280.0, ros["Alpha Quarterback"]
    ok("Sleeper week + ROS csv: league scoring (5-pt pass TD, first downs), DEF -> DST, "
       "ownership, ROS = weeks 4-17")

    roster = {r["name"]: r for r in json.loads((espn_dir / "sl-roster.json").read_text())}
    assert set(roster) == {"Alpha Quarterback", "Alpha Runner", "Bravo Runner",
                           "Alpha Tightend", "Dallas Cowboys"}, roster
    assert roster["Alpha Quarterback"]["slot"] == "QB" and roster["Dallas Cowboys"]["slot"] == "DST"
    assert roster["Bravo Runner"]["slot"] == "IR" and roster["Bravo Runner"]["injury"] == "IR"
    assert roster["Alpha Tightend"]["slot"] == "TE" and roster["Alpha Tightend"]["injury"] == "SUSP"
    teams = json.loads((espn_dir / "sl-teams.json").read_text())
    assert [(t["id"], t["name"], t["mine"]) for t in teams] == [
        (1, "Fixture Franchise", True), (2, "fixture-rival", False)], teams
    ok("Sleeper roster/teams json: set slots, IR, injury pills, team names, my team by user")


def main():
    install_fakes()
    tmp = tempfile.mkdtemp(prefix="board_pull_")
    data_dir = Path(tmp) / "dat"
    espn_dir = data_dir / "espn"
    sl = {"platform": "sleeper", "league_id": "100", "user": "fixture-me",
          "label": "Sleeper", "yaml": EXAMPLE_YAML}
    es = {"league_id": 1, "team_id": 1, "label": "ESPN", "yaml": EXAMPLE_YAML}
    os.environ.pop("FANTASYPROS_API_KEY", None)

    cfg = write_boards(tmp, {"sl": sl})
    assert board.main(["pull", "--config", cfg, "--data-dir", str(data_dir)]) == 0
    assert ("cookie",) not in ESPN_CALLS, "a Sleeper-only pull must not need ESPN cookies"
    fp = [c for c in ESPN_CALLS if c[0] == "fp"]
    assert {(w, p) for _, w, p, _, _ in fp} == {(w, p) for w in (4, 90)
                                               for p in ("QB", "RB", "WR", "TE")}, fp
    if not (ROOT / "api_key.txt").exists():
        assert all(c[4] is None for c in fp), "keyless pull must reach FantasyPros with no key"
    ok("Sleeper-only pull: no ESPN cookie, FantasyPros refreshed for week 4 + ROS without a key")
    check_sleeper(espn_dir)

    (espn_dir / "sl-roster.json").unlink()
    assert board.main(["sync", "--config", cfg, "--data-dir", str(data_dir)]) == 0
    roster = {r["name"]: r for r in json.loads((espn_dir / "sl-roster.json").read_text())}
    assert roster["Alpha Quarterback"]["ros"] == 280.0, roster["Alpha Quarterback"]
    ok("Sleeper sync: roster rebuilt, ROS points re-read from the cached csv")

    ESPN_CALLS.clear()
    cfg = write_boards(tmp, {"es": es})
    assert board.main(["pull", "--config", cfg, "--data-dir", str(data_dir)]) == 0
    assert ESPN_CALLS.count(("cookie",)) == 1, ESPN_CALLS
    meta = json.loads((espn_dir / "es-meta.json").read_text())
    assert "platform" not in meta and meta["team"] == "Fixture Franchise", meta
    roster = json.loads((espn_dir / "es-roster.json").read_text())
    assert roster == [{"name": "Alpha Quarterback", "pos": "QB", "wk": 20.5, "ros": 287.0,
                       "injury": "", "slot": "QB"}], roster
    assert float(read_csv(espn_dir / "es-ros-from-4.csv")["Golf Wideout"]["points"]) == 126.0
    ok("ESPN pull: cookie read once, same roster/meta/csv shapes as before")

    print("\nboard pull: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
