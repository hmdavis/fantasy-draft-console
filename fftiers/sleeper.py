"""Live Sleeper fantasy data for the manage board.

The Sleeper twin of fftiers/espn.py. It gives the same shapes as espn.py, so
the vbd, csg, and board stages do not know the platform. The Sleeper read API
is public: no auth. README "Sleeper leagues on the manage board" lists the
endpoints.
"""
from __future__ import annotations

import datetime
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.sleeper.app/v1"
PROJECTIONS = "https://api.sleeper.app/projections/nfl/{season}/{week}"
PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
HEADERS = {"accept": "application/json",
           "user-agent": "fantasy-draft-console/1.0 (+https://github.com/hmdavis/fantasy-draft-console)"}
RETRY_CODES = (429, 500, 502, 503, 504)

POSITION_NAMES = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DEF": "DST"}

LINEUP_SLOT_NAMES = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K",
                     "DEF": "DST", "FLEX": "FLEX", "SUPER_FLEX": "OP",
                     "WRRB_FLEX": "RB/WR", "REC_FLEX": "WR/TE", "BN": "BE"}

YAML_SLOT_NAMES = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K",
                   "DEF": "DST", "FLEX": "FLEX", "SUPER_FLEX": "OP",
                   "WRRB_FLEX": "WRRB", "REC_FLEX": "WRTE", "BN": "BENCH"}

YAML_SCORING_KEYS = {
    "pass_att": "passing_attempts", "pass_cmp": "completions",
    "pass_yd": "passing_yards", "pass_td": "passing_td", "pass_int": "interceptions",
    "pass_2pt": "two_point_conversions", "rush_att": "rushing_attempts",
    "rush_yd": "rushing_yards", "rush_td": "rushing_td", "rec_yd": "receiving_yards",
    "rec_td": "receiving_td", "rec": "receptions", "fum_lost": "fumbles_lost",
    "bonus_rec_te": "te_reception_bonus",
}


OFFENSE_PREFIXES = ("pass_", "rush_", "rec", "bonus_")


class SleeperError(RuntimeError):
    pass


def fetch(url: str, *, timeout: int = 45, retries: int = 4):
    req = urllib.request.Request(url, headers=HEADERS)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise SleeperError(f"HTTP {e.code} for {url}")
        except (OSError, json.JSONDecodeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise SleeperError(f"{type(e).__name__} for {url}: {e}")
    raise SleeperError(f"exhausted retries for {url}: {last}")


def projections_url(season: int, week: int) -> str:
    query = [("season_type", "regular")] + [("position[]", p) for p in PROJECTION_POSITIONS]
    return PROJECTIONS.format(season=season, week=week) + "?" + urllib.parse.urlencode(query)


def score(stats: dict, scoring_settings: dict) -> float:
    return round(sum(float(stats.get(k) or 0) * float(v)
                     for k, v in scoring_settings.items()), 2)


def player_name(player: dict) -> str:
    return f"{player.get('first_name') or ''} {player.get('last_name') or ''}".strip()


def playoff_weeks(settings: dict) -> int:
    rounds = max(1, math.ceil(math.log2(max(2, int(settings.get("playoff_teams") or 6)))))
    round_type = int(settings.get("playoff_round_type") or 0)
    return {0: rounds, 1: rounds + 1, 2: 2 * rounds}.get(round_type, rounds)


def week_one_tuesday(season_start: str) -> datetime.date:
    start = datetime.date.fromisoformat(season_start)
    return start - datetime.timedelta(days=(start.weekday() - 1) % 7)


class SleeperLeague:
    """One Sleeper league: settings, users, rosters, and projections."""

    def __init__(self, league: dict, users: list, rosters: list, state: dict):
        self.league = league
        self.users = users or []
        self.rosters = rosters or []
        self.state = state or {}
        self.settings = league.get("settings") or {}
        self.scoring = {k: float(v) for k, v in (league.get("scoring_settings") or {}).items()}
        self.season = int(league.get("season") or self.state.get("season") or 0)
        self._projections: dict[int, list] = {}
        self._players: dict[str, dict] = {}

    @classmethod
    def load(cls, league_id) -> "SleeperLeague":
        lid = str(league_id).strip()
        league = fetch(f"{API}/league/{lid}")
        if not league:
            raise SleeperError(f"Sleeper league {lid} returned nothing - check league_id")
        return cls(league, fetch(f"{API}/league/{lid}/users"),
                   fetch(f"{API}/league/{lid}/rosters"), fetch(f"{API}/state/nfl"))

    @property
    def name(self) -> str:
        return self.league.get("name") or f"League {self.league.get('league_id')}"

    @property
    def current_week(self) -> int:
        season_type = self.state.get("season_type")
        if season_type == "pre" or str(self.state.get("season")) != str(self.season):
            return int(self.settings.get("start_week") or 1)
        if season_type == "post":
            return self.final_week
        return max(1, int(self.state.get("week") or 1))

    @property
    def final_week(self) -> int:
        start = int(self.settings.get("playoff_week_start") or 15)
        return min(18, start + playoff_weeks(self.settings) - 1)

    def team_names(self) -> dict[int, str]:
        users = {u.get("user_id"): u for u in self.users}
        names = {}
        for r in self.rosters:
            u = users.get(r.get("owner_id")) or {}
            names[int(r["roster_id"])] = (((u.get("metadata") or {}).get("team_name") or "").strip()
                                          or u.get("display_name")
                                          or f"Team {r['roster_id']}")
        return names

    def team_id_for(self, entry: dict) -> int:
        """The roster_id of my team: `team_id` in boards.json, else `user`."""
        if entry.get("team_id"):
            return int(entry["team_id"])
        want = str(entry.get("user") or "").strip().lower()
        if not want:
            raise SleeperError("a Sleeper board needs `user` (your Sleeper username) "
                               "or `team_id` (your roster_id) in config/boards.json")
        user_id = next((u.get("user_id") for u in self.users
                        if (u.get("display_name") or "").lower() == want), None)
        if user_id is None:
            user_id = (fetch(f"{API}/user/{urllib.parse.quote(want)}") or {}).get("user_id")
        for r in self.rosters:
            if r.get("owner_id") == user_id or user_id in (r.get("co_owners") or []):
                return int(r["roster_id"])
        raise SleeperError(f"Sleeper user {want!r} owns no roster in this league")

    def owners(self) -> dict[str, int]:
        return {pid: int(r["roster_id"]) for r in self.rosters for pid in (r.get("players") or [])}

    def projections(self, week: int) -> list[dict]:
        if week not in self._projections:
            rows = fetch(projections_url(self.season, week)) or []
            self._projections[week] = rows
            for row in rows:
                self._players.setdefault(str(row.get("player_id")), row.get("player") or {})
        return self._projections[week]

    def player_pool(self, week: int) -> list[dict]:
        """League-scored week-N projections, in the shape of espn.player_pool."""
        owners = self.owners()
        out = []
        for row in self.projections(week):
            pl = row.get("player") or {}
            pos = POSITION_NAMES.get(pl.get("position"))
            if pos is None:
                continue
            pid = str(row.get("player_id"))
            out.append({"id": pid, "player": player_name(pl), "pos": pos,
                        "points": score(row.get("stats") or {}, self.scoring),
                        "team_id": owners.get(pid, 0),
                        "rostered": "yes" if pid in owners else "no",
                        "injury": pl.get("injury_status") or ""})
        return out

    def ros_pool(self, from_week: int, final_week: int, pause: float = 0.4) -> list[dict]:
        """Rest of season = the sum of each remaining week's projections."""
        totals: dict[str, dict] = {}
        for wk in range(from_week, final_week + 1):
            for p in self.player_pool(wk):
                agg = totals.setdefault(p["id"], dict(p, points=0.0))
                agg["points"] = round(agg["points"] + p["points"], 2)
            time.sleep(pause)
        return list(totals.values())

    def player_info(self, pid: str) -> dict:
        if pid not in self._players:
            self._players.update(fetch(f"{API}/players/nfl") or {})
        return self._players.get(pid) or {}

    def league_rosters(self, week: int) -> dict[int, list[dict]]:
        """Every team's roster: {roster_id: [{id,name,pos,injury,slot}]}.

        `slot` is the slot set on Sleeper now, with ESPN slot names."""
        self.projections(week)
        positions = [p for p in (self.league.get("roster_positions") or []) if p != "BN"]
        out = {}
        for r in self.rosters:
            slots = {pid: LINEUP_SLOT_NAMES.get(positions[i], positions[i])
                     for i, pid in enumerate(r.get("starters") or [])
                     if i < len(positions) and pid and pid != "0"}
            slots.update({pid: "IR" for pid in r.get("reserve") or []})
            entries = []
            for pid in r.get("players") or []:
                pl = self.player_info(str(pid))
                entries.append({"id": str(pid), "name": player_name(pl) or str(pid),
                                "pos": POSITION_NAMES.get(pl.get("position"), "?"),
                                "injury": pl.get("injury_status") or "",
                                "slot": slots.get(pid, "BE")})
            out[int(r["roster_id"])] = entries
        return out

    def roster_slots(self) -> tuple[dict[str, int], list[str]]:
        slots: dict[str, int] = {}
        skipped = []
        for p in self.league.get("roster_positions") or []:
            key = YAML_SLOT_NAMES.get(p)
            if key is None:
                skipped.append(p)
                continue
            slots[key] = slots.get(key, 0) + 1
        if self.settings.get("reserve_slots"):
            slots["IR"] = int(self.settings["reserve_slots"])
        return slots, skipped

    def yaml_scoring(self) -> dict[str, float]:
        return {YAML_SCORING_KEYS[k]: v for k, v in self.scoring.items() if k in YAML_SCORING_KEYS}

    def week_one_tuesday(self) -> datetime.date:
        start = self.state.get("season_start_date")
        if start and str(self.state.get("season")) == str(self.season):
            return week_one_tuesday(start)
        return datetime.date(self.season, 9, 8)


def write_league_yaml(lg: SleeperLeague, dest: Path) -> list[str]:
    """Write a leagues/<key>.yaml from the Sleeper settings. Returns warnings."""
    slots, skipped = lg.roster_slots()
    unmapped = sorted(k for k, v in lg.scoring.items()
                      if v and k not in YAML_SCORING_KEYS and k.startswith(OFFENSE_PREFIXES))
    lines = [f"# {lg.name} - generated from Sleeper league {lg.league.get('league_id')} "
             f"(season {lg.season}) by `python -m fftiers.sleeper_cli sync-league`.",
             "# The board scores Sleeper projections with the full Sleeper scoring_settings;",
             "# this file keeps only the keys that the tiers and notes read.",
             f"name: {json.dumps(lg.name)}", f"teams: {int(lg.league.get('total_rosters') or 0)}",
             "roster:"]
    lines += [f'  "{slot}": {n}' for slot, n in slots.items()]
    lines.append("scoring:")
    lines += [f"  {k}: {v}" for k, v in sorted(lg.yaml_scoring().items())]
    lines += ["season:", f"  year: {lg.season}",
              f"  week_one_tuesday: {lg.week_one_tuesday().isoformat()}",
              f"vbd: {{final_week: {lg.final_week}}}"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n")
    warnings = [f"roster slot {s!r} has no board equivalent - left out" for s in skipped]
    if unmapped:
        warnings.append("offense scoring that consensus ranks do not reflect: " + ", ".join(unmapped))
    return warnings
