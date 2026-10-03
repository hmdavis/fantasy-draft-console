#!/usr/bin/env python3
"""Scrape ONE season of a Sleeper fantasy league into the shape the console reads.

Sleeper equivalent of scrape_league.py. Rather than teaching build_tool_data.py a
second platform, this ADAPTS Sleeper's API into the same ESPN-shaped league_full.json
that read_scraped_league() already parses — so the console, and later the analysis
pipeline, stay platform-agnostic and the ESPN path is untouched.

    Sleeper /league /users /rosters /drafts
        └─ translate ─► scraping/raw/{season}/league_full.json  (ESPN shape)
                            └─► build_tool_data.py (unchanged)

Auth: NONE. Sleeper's read API is public — no cookies, no tokens. You only need the
league id, which is the number in your league's URL:
    https://sleeper.com/leagues/<THIS>/team

Config (config/league.json):
    {"sleeper_league_id": "1234567890", "season": 2026, "me": "Your Display Name"}
Set "sleeper_username" instead of "me" to auto-detect which manager is you.

SCOPE: current-season settings + managers, which is everything the console needs.
Historical auction calibration (analysis/) is NOT wired up — Sleeper does not document
its auction bid fields, so that half needs to be verified against a real auction draft
before it can be trusted. See the note printed at the end of a run.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(HERE, "raw")
CONFIG_PATH = os.path.join(ROOT, "config", "league.json")

API = "https://api.sleeper.app/v1"

# Sleeper roster_positions -> ESPN lineupSlotCounts keys that ESPN_SLOT/FLEX/BENCH read.
# (build_tool_data.py: ESPN_SLOT {"0":QB,"2":RB,"4":WR,"6":TE}, flex "23", bench "20")
SLEEPER_SLOT = {"QB": "0", "RB": "2", "WR": "4", "TE": "6", "BN": "20"}
# Every Sleeper flex flavour collapses to ESPN's RB/WR/TE flex slot. SUPER_FLEX allows a
# QB too, which the console's flex model doesn't represent — counted as flex, and warned.
SLEEPER_FLEX = {"FLEX", "WRRB_FLEX", "REC_FLEX", "WRRB_WRT", "SUPER_FLEX", "IDP_FLEX"}
# Slots the console ignores entirely (it drafts skill positions only).
SLEEPER_IGNORED = {"K", "DEF", "DL", "LB", "DB", "IDP", "TAXI"}

# Sleeper scoring keys -> ESPN statId, for the 8 stats build_tool_data.py actually reads
# (its ESPN_STAT map). Anything else Sleeper scores is dropped: the projection workbook
# has no raw column for it, so it could not affect FPTS either way.
SLEEPER_STAT = {"pass_yd": 3, "pass_td": 4, "pass_int": 20, "rush_yd": 24,
                "rush_td": 25, "rec_yd": 42, "rec_td": 43, "rec": 53, "fum_lost": 72}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("Missing config/league.json. Copy config/league.example.json and set "
                 "sleeper_league_id.")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def fetch(path):
    url = f"{API}/{path}"
    req = urllib.request.Request(url, headers={
        "accept": "application/json",
        "user-agent": "fantasy-draft-console/1.0 (+https://github.com/hmdavis/fantasy-draft-console)",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if e.code == 404:
                raise RuntimeError(f"HTTP 404 for {url}\n  -> no such league/draft. Check "
                                   "sleeper_league_id (the number in your sleeper.com URL).")
            raise RuntimeError(f"HTTP {e.code} for {url}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise


# ───────────────────────────── translation to ESPN shape ─────────────────────────────
def lineup_slot_counts(roster_positions):
    """Sleeper's positional ARRAY -> ESPN's slot->count MAP. Returns (counts, warnings)."""
    counts, warn = {}, []
    for slot in roster_positions or []:
        if slot in SLEEPER_IGNORED:
            continue
        if slot in SLEEPER_FLEX:
            key = "23"
            if slot == "SUPER_FLEX":
                warn.append("SUPER_FLEX counted as a standard RB/WR/TE flex — the console's "
                            "replacement-level model has no QB-eligible flex.")
        else:
            key = SLEEPER_SLOT.get(slot)
            if key is None:
                warn.append(f"unmapped roster slot {slot!r} — ignored.")
                continue
        counts[key] = counts.get(key, 0) + 1
    return counts, warn


def scoring_items(scoring_settings):
    """Sleeper scoring_settings -> ESPN scoringItems, for the stats the console scores."""
    return [{"statId": sid, "points": scoring_settings[key]}
            for key, sid in SLEEPER_STAT.items()
            if scoring_settings and key in scoring_settings]


def auction_budget(drafts, league_settings):
    """Sleeper does not document where the auction budget lives, so probe both the draft
    settings and the league settings. Returns (budget, source) — source is None when we
    fell back to the ESPN-ish default, which the caller warns about."""
    for d in drafts or []:
        s = d.get("settings") or {}
        for key in ("budget", "auction_budget"):
            if s.get(key):
                return int(s[key]), f"draft.settings.{key}"
    for key in ("budget", "auction_budget"):
        if (league_settings or {}).get(key):
            return int(league_settings[key]), f"league.settings.{key}"
    return 200, None


def manager_names(users, rosters):
    """Team-ordered manager names + ESPN-shaped members/teams.

    read_scraped_league() walks teams[] and looks each primaryOwner up in members[],
    preferring firstName/lastName then displayName. Sleeper has no real names, so
    display_name becomes displayName and the team name is the fallback.
    """
    by_id = {u.get("user_id"): u for u in (users or [])}
    members, teams, names = [], [], []
    # roster_id order is the league's team order; fall back to users when rosters are
    # absent. Sorted so members[] and teams[] stay index-aligned run to run — calibrate.py
    # zips managers against teams[].primaryOwner positionally.
    ordered = sorted(rosters, key=lambda r: r.get("roster_id") or 0) if rosters else [
        {"roster_id": i + 1, "owner_id": u.get("user_id")}
        for i, u in enumerate(users or [])]
    for r in ordered:
        uid = r.get("owner_id")
        u = by_id.get(uid) or {}
        team_name = ((u.get("metadata") or {}).get("team_name") or "").strip()
        display = (u.get("display_name") or "").strip()
        name = display or team_name or f"Team {r.get('roster_id')}"
        members.append({"id": uid, "displayName": display or name})
        teams.append({"id": r.get("roster_id"), "primaryOwner": uid, "name": team_name or name})
        names.append(name)
    return members, teams, names


def to_espn_shape(league, users, rosters, drafts):
    counts, warn = lineup_slot_counts(league.get("roster_positions"))
    budget, budget_src = auction_budget(drafts, league.get("settings"))
    if budget_src is None:
        warn.append("no auction budget found in the Sleeper payload — defaulted to $200. "
                    "Set it explicitly if your league differs (see README).")
    members, teams, names = manager_names(users, rosters)
    data = {
        "id": league.get("league_id"),
        "seasonId": int(league.get("season") or 0),
        "members": members,
        "teams": teams,
        "settings": {
            "name": league.get("name"),
            "rosterSettings": {"lineupSlotCounts": counts},
            "draftSettings": {"auctionBudget": budget},
            "scoringSettings": {"scoringItems": scoring_items(league.get("scoring_settings"))},
        },
        # provenance: this file did not come from ESPN. Ignored by every reader; kept so a
        # stray league_full.json is self-describing.
        "_source": {"platform": "sleeper", "league_id": league.get("league_id"),
                    "previous_league_id": league.get("previous_league_id")},
    }
    return data, names, warn, budget


def detect_me(cfg, users, names):
    """Match config 'sleeper_username' to a manager and persist as 'me'."""
    me = (cfg.get("me") or "").strip()
    if me and me.lower() not in ("your name", "me"):
        return me
    want = (cfg.get("sleeper_username") or "").strip().lower()
    if not want:
        print("  Set 'me' in config/league.json to one of the managers above "
              "(or 'sleeper_username' to auto-detect).")
        return me
    match = next((n for n in names if n.lower() == want), None)
    if not match:
        print(f"  sleeper_username {want!r} matched no manager — set 'me' manually.")
        return me
    cfg["me"] = match
    save_config(cfg)
    print(f"  Detected your team: me = {match!r} — written to config/league.json.")
    return match


def main():
    cfg = load_config()
    league_id = str(cfg.get("sleeper_league_id") or "").strip()
    if not league_id:
        sys.exit("Set sleeper_league_id in config/league.json (the number in your "
                 "https://sleeper.com/leagues/<ID>/team URL).")

    print(f"Fetching Sleeper league {league_id}...")
    league = fetch(f"league/{league_id}")
    if not league:
        sys.exit(f"  League {league_id} returned nothing.")
    users = fetch(f"league/{league_id}/users")
    rosters = fetch(f"league/{league_id}/rosters")
    try:
        drafts = fetch(f"league/{league_id}/drafts")
    except RuntimeError:
        drafts = []

    data, names, warn, budget = to_espn_shape(league, users, rosters, drafts)
    season = int(cfg.get("season") or data.get("seasonId") or 0)
    if not season:
        sys.exit("  Could not determine the season — set 'season' in config/league.json.")

    d = os.path.join(RAW, str(season))
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "league_full.json")
    with open(out, "w") as f:
        json.dump(data, f)

    slots = data["settings"]["rosterSettings"]["lineupSlotCounts"]
    print(f"  saved {out} ({os.path.getsize(out):,} bytes)")
    print(f"  league    {league.get('name')!r}  season {season}")
    print(f"  managers  {len(names)}: {', '.join(names)}")
    print(f"  slots     {slots}  (ESPN ids: 0=QB 2=RB 4=WR 6=TE 23=FLEX 20=BN)")
    print(f"  budget    ${budget}")
    print(f"  scoring   {len(data['settings']['scoringSettings']['scoringItems'])} "
          f"of {len(SLEEPER_STAT)} scored stats mapped")
    for w in warn:
        print(f"  ! {w}")
    detect_me(cfg, users, names)

    prev = league.get("previous_league_id")
    if prev:
        print(f"\n  Prior season chains from previous_league_id={prev}. Opponent calibration "
              "from Sleeper auction history is not implemented yet (Sleeper's auction bid "
              "fields are undocumented) — opponents stay neutral.")
    print("\nNext: python3 pipeline.py build inject")


if __name__ == "__main__":
    main()
