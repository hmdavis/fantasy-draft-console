#!/usr/bin/env python3
"""Backfill past Sleeper seasons into the ESPN-shaped raw/ layout the analysis pipeline reads.

`scrape_sleeper.py` covers the CURRENT season's settings and managers, which is all the
console needs. Calibration needs more: priced draft picks, per-season manager identity, and a
player map. This walks `previous_league_id` back through the chain and writes, per season:

    scraping/raw/<season>/league_full.json   ESPN shape + draftDetail.picks
    scraping/raw/<season>/players.json       ESPN shape, built from the picks' metadata

Auth: none. Sleeper's read API is public.

Auction bids live at `pick.metadata.amount`. Sleeper does not document that field.

    python3 scraping/scrape_sleeper_history.py            # every prior season in the chain
    python3 scraping/scrape_sleeper_history.py 2023 2024  # just these
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(HERE, "raw")
API = "https://api.sleeper.app/v1"

POS_ID = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "DEF": 16}
SLOT = {"QB": "0", "RB": "2", "WR": "4", "TE": "6", "K": "17", "DEF": "16", "BN": "20"}
FLEX = {"FLEX", "WRRB_FLEX", "REC_FLEX", "WRRB_WRT", "SUPER_FLEX", "IDP_FLEX"}


def get(path):
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.load(r)


def lineup_counts(positions):
    counts = {}
    for p in positions or []:
        key = "23" if p in FLEX else SLOT.get(p)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def build_season(league):
    """One season -> (league_full dict, players.json list)."""
    lid = league["league_id"]
    users = get("/league/%s/users" % lid) or []
    rosters = get("/league/%s/rosters" % lid) or []
    drafts = [d for d in (get("/league/%s/drafts" % lid) or []) if d.get("type") == "auction"]

    name_of = {u["user_id"]: (u.get("display_name") or "").strip() for u in users}
    members, teams = [], []
    for r in sorted(rosters, key=lambda x: x.get("roster_id") or 0):
        uid = r.get("owner_id")
        nm = name_of.get(uid) or ("Team %s" % r.get("roster_id"))
        members.append({"id": uid, "displayName": nm})
        teams.append({"id": r.get("roster_id"), "primaryOwner": uid, "owners": [uid], "name": nm,
                      "record": {"overall": {
                          "wins": (r.get("settings") or {}).get("wins"),
                          "losses": (r.get("settings") or {}).get("losses"),
                          "pointsFor": (r.get("settings") or {}).get("fpts"),
                      }}})

    picks_out, players_out, seen = [], [], set()
    used_draft = None
    for d in drafts:
        did = d["draft_id"]
        picks = get("/draft/%s/picks" % did) or []
        if not picks:
            continue
        used_draft = d
        for pk in picks:
            md = pk.get("metadata") or {}
            pos = (md.get("position") or "").upper()
            pid = pk.get("player_id")
            if pos == "DEF":
                nm = str(md.get("last_name") or md.get("first_name") or "").split(" ")[-1] + " D/ST"
            else:
                nm = ((md.get("first_name") or "") + " " + (md.get("last_name") or "")).strip()
            if pid and pid not in seen:
                seen.add(pid)
                players_out.append({"player": {"id": pid, "fullName": nm,
                                               "defaultPositionId": POS_ID.get(pos, 0)}})
            picks_out.append({
                "playerId": pid,
                "teamId": pk.get("roster_id"),
                "bidAmount": int(md.get("amount") or 0),
                "overallPickNumber": pk.get("pick_no"),
            })
        break

    budget = ((used_draft or {}).get("settings") or {}).get("budget") or 200
    data = {
        "id": lid,
        "seasonId": int(league.get("season") or 0),
        "members": members,
        "teams": teams,
        "settings": {
            "name": league.get("name"),
            "rosterSettings": {"lineupSlotCounts": lineup_counts(league.get("roster_positions"))},
            "draftSettings": {"auctionBudget": budget, "type": "AUCTION"},
            "scoringSettings": {"scoringItems": []},
        },
        "draftDetail": {"picks": picks_out, "drafted": True},
        "_source": {"platform": "sleeper", "league_id": lid,
                    "previous_league_id": league.get("previous_league_id")},
    }
    return data, players_out


def main():
    want = {int(a) for a in sys.argv[1:]} or None
    cfg = json.load(open(os.path.join(ROOT, "config", "league.json"), encoding="utf-8"))
    lid = str(cfg.get("sleeper_league_id") or "").strip()
    if not lid:
        sys.exit("Set sleeper_league_id in config/league.json.")

    seen = set()
    while lid and lid not in seen:
        seen.add(lid)
        league = get("/league/%s" % lid)
        if not league:
            break
        season = int(league.get("season") or 0)
        nxt = league.get("previous_league_id")
        if (want is None or season in want) and league.get("status") == "complete":
            data, players = build_season(league)
            n = len(data["draftDetail"]["picks"])
            if not n:
                print("%d: no priced draft found — skipped" % season)
            else:
                out = os.path.join(RAW, str(season))
                os.makedirs(out, exist_ok=True)
                for fn in ("league_full.json", "players.json"):
                    p = os.path.join(out, fn)
                    if os.path.exists(p) and not os.path.exists(p + ".pre-sleeper"):
                        os.rename(p, p + ".pre-sleeper")
                        print("  kept previous %s as %s.pre-sleeper" % (fn, fn))
                json.dump(data, open(os.path.join(out, "league_full.json"), "w"))
                json.dump(players, open(os.path.join(out, "players.json"), "w"))
                priced = sum(1 for x in data["draftDetail"]["picks"] if x["bidAmount"])
                print("%d: %d picks (%d priced), %d players" % (season, n, priced, len(players)))
        lid = nxt if nxt and str(nxt) != "0" else None


if __name__ == "__main__":
    main()
