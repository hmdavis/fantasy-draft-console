"""Manage board: live ESPN inputs -> viz-data.json -> draft_app/static/board.html.

    python -m fftiers.board pull  [--league KEY]   # network: ESPN + FantasyPros
    python -m fftiers.board build [--league KEY]   # offline: assemble + inject

`pull` writes, per league in config/boards.json:
    dat/espn/<key>-week-<N>.csv       weekly league-scored projections (engine CSV)
    dat/espn/<key>-ros-from-<N>.csv   rest-of-season totals (engine CSV)
    dat/espn/<key>-roster.json        my roster [{name,pos,wk,ros,injury}]
    dat/espn/<key>-meta.json          {week, final_week, team, league_name, pulled}
and refreshes the FantasyPros consensus caches the tier boards read (week N +
the ROS sentinel week 90, per position/scoring the leagues need).

`build` is pure offline: Boris tiers from the FP caches (fftiers.cluster),
elboberto/CSG rows from out/<key>/week-<N>/{vbd,csg}/*.csv, roster/meta from
dat/espn/, league shape from leagues/<key>.yaml — merged per player per
horizon into viz-data.json, then injected at the template's /*DATA*/ marker.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import repo_root
from .cluster import assign_tiers
from .config import LeagueConfig, load_league
from .depth import plan_positions
from .fetch import cache_path, load_rows
from .names import norm_name

CORE = ("QB", "RB", "WR", "TE")
ROS_CACHE_WEEK = 90   # FantasyPros sentinel week for rest-of-season ranks
SLOT_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "OP", "WRTE", "WRRB", "K", "DST")
SCORING_LABEL = {"STD": "standard", "HALF": "half-PPR", "PPR": "PPR"}


def load_boards(path: Path, only: str | None) -> tuple[int, dict]:
    raw = json.loads(path.read_text())
    leagues = raw["leagues"]
    if only:
        if only not in leagues:
            raise SystemExit(f"league {only!r} not in {path} (have: {', '.join(leagues)})")
        leagues = {only: leagues[only]}
    return int(raw["season"]), leagues


def boris_tiers(cfg: LeagueConfig, data_dir: Path, cache_week: int) -> dict[tuple[str, str], dict]:
    """(name, pos) -> {posrank, tier, ecr, std} via GMM over the cached ranks."""
    out = {}
    plans = {p.position: p for p in plan_positions(cfg, week=1)}
    for pos in CORE:
        plan = plans.get(pos)
        if plan is None:
            continue
        scoring = cfg.scoring_source if pos in ("RB", "WR", "TE") else "STD"
        path = cache_path(data_dir, cfg.year, cache_week, pos, scoring)
        if not path.exists():
            continue
        rows = load_rows(path)
        window = rows[: min(plan.high, len(rows))]
        tiers = assign_tiers([r.avg for r in window], plan.tiers)
        for r, t in zip(window, tiers):
            out[(r.name, pos)] = {"posrank": r.rank, "tier": t, "ecr": r.avg, "std": r.std}
    return out


def read_board(path: Path, cols: dict[str, str]) -> dict[tuple[str, str], dict]:
    out = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            if row["Pos"] not in CORE:
                continue
            out[(row["Player"], row["Pos"])] = {
                k: (float(row[c]) if k != "tier" else row[c]) for k, c in cols.items()}
    return out


def write_pool_csv(dest: Path, pool: list[dict]) -> None:
    """Same shape + filtering as `fftiers-espn pull`: player,pos,points,rostered."""
    rows = sorted((p for p in pool if p["points"] > 0 or p["rostered"] == "yes"),
                  key=lambda p: -p["points"])
    with dest.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["player", "pos", "points", "rostered"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")


# ESPN's injuryStatus for a healthy player is the string "ACTIVE"; on the board a
# status pill means "look at this", so healthy must be empty. Long designations get
# the short pill the board renders elsewhere.
INJURY_PILLS = {"ACTIVE": "", "NORMAL": "", "QUESTIONABLE": "Q", "DOUBTFUL": "D",
                "OUT": "OUT", "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP"}


def norm_injury(status: str) -> str:
    s = (status or "").strip()
    return INJURY_PILLS.get(s.upper(), s)


# ── pull (network) ───────────────────────────────────────────────────────────
def cmd_pull(args) -> int:
    from . import espn
    from .fetch import download, optional_api_key
    root = repo_root()
    season, leagues = load_boards(Path(args.config), args.league)
    data_dir = Path(args.data_dir)
    espn_dir = data_dir / "espn"
    espn_dir.mkdir(parents=True, exist_ok=True)
    cookie = espn.auth_cookie()
    combos: set[tuple[int, str, str]] = set()

    for key, lg in leagues.items():
        cfg = load_league(root / lg["yaml"])
        info = espn.league_settings(lg["league_id"], season, cookie)
        week = args.week or info["current_week"]
        final_week = int(info.get("final_week")
                         or cfg.vbd_options.get("final_week") or 17)
        wk_pool = espn.player_pool(lg["league_id"], season, week, cookie)
        print(f"[{key}] week {week}: {len(wk_pool)} players; "
              f"summing ROS weeks {week}-{final_week} ...")
        ros_list = espn.ros_pool(lg["league_id"], season, week, final_week, cookie)
        write_pool_csv(espn_dir / f"{key}-week-{week}.csv", wk_pool)
        write_pool_csv(espn_dir / f"{key}-ros-from-{week}.csv", ros_list)

        wk_by = {p["id"]: p["points"] for p in wk_pool}
        ros_by = {p["id"]: p["points"] for p in ros_list}
        all_rosters = espn.league_rosters(lg["league_id"], season, cookie)

        def enrich(entries):
            rows = [{"name": r["name"], "pos": r["pos"],
                     "wk": wk_by.get(r["id"], 0.0), "ros": ros_by.get(r["id"], 0.0),
                     "injury": norm_injury(r["injury"]), "slot": r.get("slot", "")}
                    for r in entries]
            rows.sort(key=lambda r: -r["ros"])
            return rows

        roster = enrich(all_rosters.get(int(lg["team_id"]), []))
        (espn_dir / f"{key}-roster.json").write_text(json.dumps(roster, indent=1) + "\n")
        write_teams_json(espn_dir / f"{key}-teams.json", all_rosters, enrich,
                         info["my_teams"], int(lg["team_id"]))
        meta = {"week": week, "final_week": final_week,
                "team": info["my_teams"].get(int(lg["team_id"]), ""),
                "league_name": info["name"], "pulled": _now()}
        (espn_dir / f"{key}-meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        print(f"[{key}] {meta['team'] or '?'}: {len(roster)} rostered "
              f"-> {espn_dir}/{key}-{{week-{week},ros-from-{week}}}.csv + roster/meta json")
        for pos in CORE:
            scoring = cfg.scoring_source if pos in ("RB", "WR", "TE") else "STD"
            combos.add((week, pos, scoring))
            combos.add((ROS_CACHE_WEEK, pos, scoring))

    api_key = optional_api_key(None)
    if not api_key:
        print("no FantasyPros API key - reading the public ranking pages (current week only).")
    for i, (week, pos, scoring) in enumerate(sorted(combos)):
        if i:
            time.sleep(1.5)   # the public API throttles bursts (429 after ~8 rapid calls)
        try:
            path = _download_throttled(data_dir, season, week, pos, scoring, api_key)
        except urllib.error.HTTPError as e:
            # Keep whatever cache exists — including text-pasted ones from
            # `python3 -m fftiers.text_to_cache` — rather than dying mid-refresh.
            print(f"  FP {pos}-{scoring} week {week}: HTTP {e.code} - kept existing cache"
                  f" (paste fresh ranks via fftiers.text_to_cache if needed)")
        except urllib.error.URLError as e:
            print(f"  FP {pos}-{scoring} week {week}: {e.reason} - kept existing cache")
        except RuntimeError as e:
            # fetch refuses to write a truncated or wrong-week pool; its message
            # already says the prior cache was kept — carry on with the next combo.
            print(f"  FP: {e}")
        except (OSError, ValueError) as e:
            # read timeouts, connection resets, unparseable payloads — one bad
            # combo must never abort the rest of the refresh.
            print(f"  FP {pos}-{scoring} week {week}: {type(e).__name__}: {e} "
                  f"- kept existing cache")
        else:
            print(f"  FP {pos}-{scoring} week {week} -> {path}")
    return 0


def _download_throttled(data_dir, season, week, pos, scoring, api_key):
    from .fetch import download
    try:
        return download(data_dir, season, week, pos, scoring, api_key)
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        time.sleep(15)   # rate-limited: one patient retry, then give up on this combo
        return download(data_dir, season, week, pos, scoring, api_key)


# ── build (offline) ──────────────────────────────────────────────────────────
def build_league(key: str, lg: dict, cfg: LeagueConfig, meta: dict, roster: list[dict],
                 data_dir: Path, out_dir: Path) -> dict:
    week = int(meta["week"])
    base = out_dir / key / f"week-{week}"
    horizons_spec = {"week": (week, f"week-{week}"),
                     "ros": (ROS_CACHE_WEEK, f"ros-from-{week}")}
    mine = {norm_name(r["name"]) for r in roster}
    # Sources spell players differently (ESPN "Patrick Mahomes" vs FantasyPros
    # "Patrick Mahomes II"), so every dict is keyed by (norm_name, pos) and a
    # display name is kept per key — the ESPN engines' spelling wins, since the
    # roster and free-agent state join on it.
    display: dict = {}

    def rekey(d, authoritative):
        out = {}
        for (name, pos), v in d.items():
            k = (norm_name(name), pos)
            if authoritative:
                display[k] = name
            else:
                display.setdefault(k, name)
            out[k] = v
        return out

    horizons, keys_ = {}, set()
    for hz, (cache_week, blabel) in horizons_spec.items():
        boris = rekey(boris_tiers(cfg, data_dir, cache_week), False)
        elb = rekey(read_board(base / "vbd" / f"vbd-{blabel}.csv",
                    {"posrank": "PosRank", "pts": "FPTS", "vbd": "AvgVBD", "tier": "Tier"}), True)
        csg_path = base / "csg" / f"csg-{blabel}.csv"
        csg = rekey(read_board(csg_path, {"posrank": "PosRank", "pts": "FPTS", "vbd": "AvgVBD",
                                          "adj": "VBDAdj", "tier": "PosTier"}), True)
        # Roster status is only OBSERVED via the CSG board's Rostered column (the
        # engines share one ESPN pool, so vbd keys ⊆ csg keys in practice — a player
        # in vbd but not csg would land fa:null "unknown", never a wrong claim).
        rostered = {}
        with csg_path.open() as f:
            for row in csv.DictReader(f):
                rostered[(norm_name(row["Player"]), row["Pos"])] = row["Rostered"] == "yes"
        horizons[hz] = {"boris": boris, "elb": elb, "csg": csg, "rostered": rostered}
        keys_ |= set(boris) | set(elb) | set(csg)

    players = []
    for key_ in sorted(keys_, key=lambda k: display[k]):
        norm, pos = key_
        entry = {"name": display[key_], "pos": pos, "mine": norm in mine}
        fa = False
        observed = False   # did any horizon's CSG board actually report roster status?
        for hz, h in horizons.items():
            entry[hz] = {"boris": h["boris"].get(key_),
                         "elb": h["elb"].get(key_),
                         "csg": h["csg"].get(key_)}
            if key_ in h["rostered"]:
                observed = True
                if not h["rostered"][key_]:
                    fa = True
        # A player seen only in FantasyPros consensus (never in any ESPN engine
        # CSV) has UNKNOWN roster status — emit null, not a false "rostered".
        # My own roster is an observation too: mine players keep fa=False.
        entry["fa"] = fa if (observed or entry["mine"]) else None
        players.append(entry)

    slots = [[s, cfg.roster[s]] for s in SLOT_ORDER if cfg.roster.get(s, 0) > 0]
    # every team's roster, when the pull captured them (trade finder's raw material)
    teams_p = data_dir / "espn" / f"{key}-teams.json"
    teams = []
    if teams_p.exists():
        row = lambda r: [r["name"], r["pos"], r["wk"], r["ros"], r["injury"], r.get("slot", "")]
        teams = [{"id": t["id"], "name": t["name"], "mine": t["mine"],
                  "roster": [row(r) for r in t["roster"]]}
                 for t in json.loads(teams_p.read_text())]
    return {
        "label": lg.get("label", key),
        "size": cfg.teams,
        "scoring": SCORING_LABEL[cfg.scoring_source],
        "lineup": "-".join(("" if n == 1 else str(n)) + s for s, n in slots),
        "team": meta.get("team", ""),
        "slots": slots,
        "roster": [[r["name"], r["pos"], r["wk"], r["ros"], r["injury"], r.get("slot", "")]
                   for r in roster],
        "teams": teams,
        "players": players,
    }


def cmd_build(args) -> int:
    root = repo_root()
    season, leagues = load_boards(Path(args.config), args.league)
    data_dir, out_dir = Path(args.data_dir), Path(args.out_dir)
    built: dict[str, dict] = {}
    weeks, finals = [], []
    mtimes: dict[str, list[float]] = {"fp": [], "espn": [], "boards": []}

    for key, lg in leagues.items():
        meta_p = data_dir / "espn" / f"{key}-meta.json"
        roster_p = data_dir / "espn" / f"{key}-roster.json"
        missing = [p.name for p in (meta_p, roster_p) if not p.exists()]
        if missing:
            print(f"[{key}] SKIP - missing {', '.join(missing)} in {data_dir / 'espn'}; "
                  f"run `python3 pipeline.py pull` first")
            continue
        meta = json.loads(meta_p.read_text())
        roster = json.loads(roster_p.read_text())
        week = int(meta["week"])
        base = out_dir / key / f"week-{week}"
        csvs = [base / "vbd" / f"vbd-{b}.csv" for b in (f"week-{week}", f"ros-from-{week}")] \
             + [base / "csg" / f"csg-{b}.csv" for b in (f"week-{week}", f"ros-from-{week}")]
        missing = [str(p) for p in csvs if not p.exists()]
        if missing:
            print(f"[{key}] SKIP - missing {', '.join(missing)}; "
                  f"run `python3 pipeline.py vbd-boards csg-boards` first (after `pull`)")
            continue
        cfg = load_league(root / lg["yaml"])
        built[key] = build_league(key, lg, cfg, meta, roster, data_dir, out_dir)
        weeks.append(week)
        finals.append(int(meta.get("final_week") or 17))
        mtimes["espn"] += [meta_p.stat().st_mtime, roster_p.stat().st_mtime]
        mtimes["boards"] += [p.stat().st_mtime for p in csvs]
        for pos in CORE:
            scoring = cfg.scoring_source if pos in ("RB", "WR", "TE") else "STD"
            for cw in (week, ROS_CACHE_WEEK):
                p = cache_path(data_dir, cfg.year, cw, pos, scoring)
                if p.exists():
                    mtimes["fp"].append(p.stat().st_mtime)

    if not built:
        print("no league built - nothing to write")
        return 1

    week, final_week = max(weeks), max(finals)
    if len(set(weeks)) > 1:
        print(f"note: leagues disagree on the current week ({sorted(set(weeks))}); "
              f"asof uses week {week}")
    day = lambda ts: datetime.date.fromtimestamp(ts).isoformat()
    sources = {}
    if mtimes["fp"]:
        sources["FantasyPros consensus ranks"] = day(max(mtimes["fp"]))
    if mtimes["espn"]:
        sources["ESPN rosters & projections"] = day(max(mtimes["espn"]))
    if mtimes["boards"]:
        sources["VBD / CSG boards"] = day(max(mtimes["boards"]))
    data = {"asof": {"week": week, "built": _now(),
                     "weeks_left": final_week - week + 1, "final_week": final_week,
                     "sources": sources},
            "leagues": built}

    # Week-over-week ledger: per-league {player: mean ROS VBD}, one entry per week.
    # Each league embeds the latest PRIOR week's map so the trade finder can chip
    # rising/falling values without a network hop. Local, gitignored, append-only.
    ledger_p = (Path(args.viz_out).parent if args.viz_out else out_dir / "board") / "history.json"
    try:
        ledger = json.loads(ledger_p.read_text()) if ledger_p.exists() else {}
    except ValueError:
        ledger = {}
    weeks_led = ledger.setdefault("weeks", {})
    for key, lg in built.items():
        cur = {}
        for p in lg["players"]:
            vbds = [b["vbd"] for b in (p["ros"].get("elb"), p["ros"].get("csg")) if b]
            if vbds:
                cur[p["name"]] = round(sum(vbds) / len(vbds), 1)
        prior_weeks = sorted((int(w) for w, m in weeks_led.items()
                              if key in m and int(w) < week), reverse=True)
        lg["prev_vbd"] = weeks_led[str(prior_weeks[0])][key] if prior_weeks else {}
        lg["prev_week"] = prior_weeks[0] if prior_weeks else None
        weeks_led.setdefault(str(week), {})[key] = cur
    ledger_p.parent.mkdir(parents=True, exist_ok=True)
    ledger_p.write_text(json.dumps(ledger))

    viz_out = Path(args.viz_out) if args.viz_out else out_dir / "board" / "viz-data.json"
    viz_out.parent.mkdir(parents=True, exist_ok=True)
    viz_out.write_text(json.dumps(data))
    counts = {k: len(v["players"]) for k, v in built.items()}
    print(f"wrote {viz_out} - players per league: {counts}")

    if not args.no_inject:
        tpl = Path(args.template).read_text()
        if "/*DATA*/" not in tpl:
            raise SystemExit(f"{args.template}: no /*DATA*/ marker to inject at")
        dest = Path(args.dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(tpl.replace("/*DATA*/", json.dumps(data)))
        print(f"injected -> {dest}")

    if not args.no_push:
        push_snapshot(data, season)
    return 0


def write_teams_json(dest: Path, all_rosters: dict, enrich, team_names: dict,
                     my_id: int) -> None:
    """Every team's enriched roster — the trade finder's raw material."""
    teams = [{"id": tid, "name": team_names.get(tid, f"Team {tid}"),
              "mine": tid == my_id, "roster": enrich(entries)}
             for tid, entries in sorted(all_rosters.items())]
    dest.write_text(json.dumps(teams, indent=1) + "\n")


# ── sync (network, fast) ─────────────────────────────────────────────────────
def cmd_sync(args) -> int:
    """Fast ESPN state sync: rosters, lineup slots, meta, this-week projections.

    The fast-moving state (who's rostered, what's set in the lineup, weekly
    numbers) refreshes in a few calls per league; the slow-moving forecasts —
    rest-of-season totals and FantasyPros ranks — are reused from cache and only
    move on a full `pull`. Roster ROS points are re-matched against the cached
    ROS csv by normalized name (fallback: the previous snapshot's value).
    """
    from . import espn
    root = repo_root()
    season, leagues = load_boards(Path(args.config), args.league)
    data_dir = Path(args.data_dir)
    espn_dir = data_dir / "espn"
    espn_dir.mkdir(parents=True, exist_ok=True)
    cookie = espn.auth_cookie()
    synced = 0

    for key, lg in leagues.items():
        cfg = load_league(root / lg["yaml"])
        # ALL network first, ALL writes last: an ESPN failure mid-league must leave
        # that league's dat/espn files exactly as they were, and move on.
        try:
            info = espn.league_settings(lg["league_id"], season, cookie)
            week = args.week or info["current_week"]
            ros_csv = espn_dir / f"{key}-ros-from-{week}.csv"
            if not ros_csv.exists():
                # Week rolled over (or never pulled): the cached ROS totals belong
                # to another week. A half-synced league would break the board.
                print(f"[{key}] SKIP - no cached {ros_csv.name} for ESPN week {week}; "
                      f"run the full refresh (`python3 pipeline.py week`) first")
                continue
            wk_pool = espn.player_pool(lg["league_id"], season, week, cookie)
            all_rosters = espn.league_rosters(lg["league_id"], season, cookie)
            team = all_rosters.get(int(lg["team_id"]), [])
        except (espn.EspnError, OSError) as e:
            print(f"[{key}] SKIP - ESPN error, nothing written for this league: {e}")
            continue
        final_week = int(info.get("final_week")
                         or cfg.vbd_options.get("final_week") or 17)
        write_pool_csv(espn_dir / f"{key}-week-{week}.csv", wk_pool)
        wk_by = {p["id"]: p["points"] for p in wk_pool}

        ros_by = {}
        with ros_csv.open() as f:
            for row in csv.DictReader(f):
                k2 = (norm_name(row["player"]), row["pos"])
                if k2 in ros_by:
                    print(f"[{key}] note: duplicate normalized name in {ros_csv.name}: "
                          f"{row['player']} — last row wins")
                ros_by[k2] = float(row["points"])
        prev = {}
        roster_p = espn_dir / f"{key}-roster.json"
        if roster_p.exists():
            for r0 in json.loads(roster_p.read_text()):
                prev[(norm_name(r0["name"]), r0["pos"])] = r0.get("ros", 0.0)

        def enrich(entries):
            rows = []
            for r in entries:
                k2 = (norm_name(r["name"]), r["pos"])
                rows.append({"name": r["name"], "pos": r["pos"],
                             "wk": wk_by.get(r["id"], 0.0),
                             "ros": ros_by.get(k2, prev.get(k2, 0.0)),
                             "injury": norm_injury(r["injury"]),
                             "slot": r.get("slot", "")})
            rows.sort(key=lambda r: -r["ros"])
            return rows

        roster = enrich(team)
        roster_p.write_text(json.dumps(roster, indent=1) + "\n")
        write_teams_json(espn_dir / f"{key}-teams.json", all_rosters, enrich,
                         info["my_teams"], int(lg["team_id"]))
        meta = {"week": week, "final_week": final_week,
                "team": info["my_teams"].get(int(lg["team_id"]), ""),
                "league_name": info["name"], "pulled": _now()}
        (espn_dir / f"{key}-meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        set_now = sum(1 for r in roster if r["slot"] not in ("", "BE", "IR"))
        print(f"[{key}] synced: {len(roster)} rostered, {set_now} set in lineup, "
              f"week {week} projections refreshed (ROS totals + ranks from cache)")
        synced += 1
    return 0 if synced or not leagues else 1


# ── snapshot store (Supabase) ────────────────────────────────────────────────
SUPABASE_KEYS = ("SUPABASE_URL", "SUPABASE_API_KEY", "BOARD_DB_SECRET")


def supabase_env() -> dict | None:
    """The three snapshot-store settings, from the environment else config/.env."""
    vals = {k: os.environ.get(k, "").strip() for k in SUPABASE_KEYS}
    envf = repo_root() / "config" / ".env"
    if not all(vals.values()) and envf.exists():
        for line in envf.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k in SUPABASE_KEYS and not vals[k]:
                vals[k] = v.strip()
    return vals if all(vals.values()) else None


def push_snapshot(data: dict, season: int) -> None:
    """Persist the built viz-data to the board_snapshots table (one row per build).

    The deployed server reads the latest row, so a local `pipeline.py week` updates
    the live site without a redeploy. Best-effort: the local build stands either way.
    """
    cfg = supabase_env()
    if cfg is None:
        print("snapshot push skipped - set SUPABASE_URL / SUPABASE_API_KEY / "
              "BOARD_DB_SECRET (env or config/.env) to persist builds")
        return
    body = json.dumps({"secret": cfg["BOARD_DB_SECRET"], "p_season": season,
                       "p_week": data["asof"]["week"], "p_data": data}).encode()
    req = urllib.request.Request(
        cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/rpc/put_board_snapshot",
        data=body, headers={"apikey": cfg["SUPABASE_API_KEY"],
                            "Authorization": "Bearer " + cfg["SUPABASE_API_KEY"],
                            "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"snapshot -> supabase board_snapshots id {r.read().decode().strip()}")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        detail = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e.reason)
        print(f"WARNING: snapshot push failed ({detail}); the local board is still built")


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    ap = argparse.ArgumentParser(prog="fftiers-board",
                                 description="Manage-board data builder + injector.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", default=str(root / "config" / "boards.json"))
        p.add_argument("--league", default=None, help="Only this league key.")
        p.add_argument("--data-dir", default=str(root / "dat"))

    pp = sub.add_parser("pull", help="ESPN projections/roster/meta + FP cache refresh.")
    common(pp)
    pp.add_argument("--week", type=int, default=None, help="Default: current ESPN week.")

    sp = sub.add_parser("sync", help="Fast ESPN sync: rosters/lineups/meta/weekly "
                                     "projections; ROS + ranks stay cached.")
    common(sp)
    sp.add_argument("--week", type=int, default=None, help="Default: current ESPN week.")

    bp = sub.add_parser("build", help="Assemble viz-data.json and inject board.html.")
    common(bp)
    bp.add_argument("--out-dir", default=str(root / "out"))
    bp.add_argument("--viz-out", default=None,
                    help="Default: <out-dir>/board/viz-data.json")
    bp.add_argument("--template", default=str(root / "draft_sheets" / "board_template.html"))
    bp.add_argument("--dest", default=str(root / "draft_app" / "static" / "board.html"))
    bp.add_argument("--no-inject", action="store_true")
    bp.add_argument("--no-push", action="store_true",
                    help="Skip persisting the snapshot to Supabase.")

    args = ap.parse_args(argv)
    return {"pull": cmd_pull, "sync": cmd_sync, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    import sys
    sys.exit(main())
