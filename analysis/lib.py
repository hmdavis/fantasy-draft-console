#!/usr/bin/env python3
"""Shared loaders for league analysis.

Handles the three things every analysis needs:
  - player id -> (name, position)   [built across all seasons w/ players.json]
  - per-season teamId -> manager identity (stable across seasons via member id)
  - draft picks with the $100 keeper hack normalized to true cost

League-specific identity lives in config/ (local, gitignored), NOT in this code:
  - MANAGER_CANON  <- config/manager_canon.json  (GUID -> canonical name; optional)
  - ME             <- config/league.json "me"    (which manager you are)
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, os.pardir, "scraping", "raw")
CONFIG = os.path.join(HERE, os.pardir, "config")


def _load_config(name):
    """Load a config/<name> JSON, or {} if it isn't there (keeps the code league-agnostic)."""
    p = os.path.join(CONFIG, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

AUCTION_SEASONS = list(range(2017, 2026))   # 2017..2025
ALL_SEASONS = list(range(2013, 2026))
KEEPER_INFLATION = 100

# ── Cross-season price normalization (READ THIS BEFORE COMPARING ANY TWO SEASONS) ──
#
# The nominal auctionBudget is NOT spending power. 2020-2024 show $300, but that extra
# $100 existed only to carry the keeper encoding (a keeper is recorded as a bid of
# cost+$100, so the cap had to rise to fit it). Proof: total league spend is ~$2,400 in
# EVERY season, $200-cap and $300-cap alike — ~$200 per team throughout.
#
# So the comparable denominator is the EFFECTIVE WALLET: non-keeper dollars actually spent
# per team. ~$199 in full-supply seasons, ~$170-188 in keeper seasons.
#
# There is a SECOND, separate effect, and conflating it with the first is the trap:
# keeper seasons removed 12 elite players from supply, which pushed top-of-board prices UP
# and mid-board prices DOWN (measured: share-of-wallet ratio full-supply/keeper is
# 0.77-0.86 at ranks 1-8 but 0.99-1.18 at ranks 11-15; whole-distribution 0.97). Therefore:
#
#   TOP-OF-BOARD quantities (max buy, the price curve, plan ceilings)
#       -> use FULL_SUPPLY seasons only. Keeper-era top prices reflect a scarcity that
#          will not exist in a full-supply season, so rescaling them is not enough.
#   WHOLE-DISTRIBUTION behavioural traits (positional multipliers, concentration)
#       -> may pool all seasons AFTER wallet-normalizing; the distortion is ~3%.
KEEPER_SEASONS = (2020, 2021, 2022, 2023, 2024)
FULL_SUPPLY_SEASONS = (2017, 2018, 2019, 2025)   # ~$200 wallet, no keepers; 2026 repeats


def regime(season):
    """'keeper' (12 elites kept, supply-constrained) or 'full-supply'."""
    return "keeper" if int(season) in KEEPER_SEASONS else "full-supply"


def effective_wallet(season):
    """Non-keeper dollars actually spent per team — the only comparable denominator.

    Prefer this over auction_budget() for anything that compares seasons. Falls back to
    the nominal cap if a season has no auction data.
    """
    picks = draft_picks(season)
    teams = len({p["teamId"] for p in picks})
    if not teams:
        return float(auction_budget(season) or 200)
    spend = sum(p["cost"] for p in picks if not p["is_keeper"])
    return (spend / teams) if spend else float(auction_budget(season) or 200)


def norm_cost(season, cost, target_wallet=200.0):
    """A season's raw dollars -> dollars at `target_wallet` of spending power.

    Wallet-normalization ONLY. It does not correct for keeper-era supply scarcity, so do
    not use it to project a keeper season's TOP prices onto a full-supply season — filter
    to FULL_SUPPLY_SEASONS for that instead (see the note above).
    """
    w = effective_wallet(season) or target_wallet
    return cost * (target_wallet / w)

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST",
       7: "OP", 9: "DL", 10: "LB", 11: "DB", 12: "DP", 13: "DT", 14: "DE"}


_LOAD_CACHE = {}


def has_season(season):
    return os.path.exists(os.path.join(RAW, str(season), "league_full.json"))


def load(season):
    """Return the league_full object (unwrapping the historical list form)."""
    if season in _LOAD_CACHE:
        return _LOAD_CACHE[season]
    d = json.load(open(os.path.join(RAW, str(season), "league_full.json")))
    if isinstance(d, list):
        d = d[0] if d else {}
    _LOAD_CACHE[season] = d
    return d


def load_players_raw(season):
    p = os.path.join(RAW, str(season), "players.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p))
    return d.get("players", []) if isinstance(d, dict) else d


def load_transactions(season):
    p = os.path.join(RAW, str(season), "transactions.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p))


def load_playercards(season):
    p = os.path.join(RAW, str(season), "playercards.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p))


_TRADE_CACHE = {}


def executed_trades(season):
    """Unique executed trades for a season, parsed from playercards (the only
    complete source of trade item detail). Each trade dict:
        {sp, date, sides: {teamId: [playerIds]}, managers: {teamId: name},
         pids: [all playerIds]}
    Deduped across the redundant per-player card copies.
    """
    if season in _TRADE_CACHE:
        return _TRADE_CACHE[season]
    seen = {}
    for card in load_playercards(season):
        for t in card.get("transactions", []):
            if t.get("type") != "TRADE_ACCEPT" or not t.get("items"):
                continue
            moves = [(i["playerId"], i["fromTeamId"], i["toTeamId"])
                     for i in t["items"] if i.get("type") == "TRADE"]
            if not moves:
                continue
            key = (t.get("acceptedDate") or t.get("proposedDate"), tuple(sorted(moves)))
            if key in seen:
                continue
            sides = {}
            for pid, ft, tt in moves:
                sides.setdefault(tt, []).append(pid)
            seen[key] = {
                "sp": t.get("scoringPeriodId"),
                "date": t.get("acceptedDate") or t.get("proposedDate"),
                "sides": sides,
                "managers": {tm: manager(season, tm) for tm in sides},
                "pids": [m[0] for m in moves],
            }
    out = list(seen.values())
    _TRADE_CACHE[season] = out
    return out


_PLAYER_MAP = None


def player_map():
    """Global {playerId: {'name':..., 'pos':...}} across every season available."""
    global _PLAYER_MAP
    if _PLAYER_MAP is not None:
        return _PLAYER_MAP
    m = {}
    for yr in ALL_SEASONS:
        for entry in load_players_raw(yr):
            p = entry.get("player", {})
            pid = p.get("id")
            if pid is None:
                continue
            name = p.get("fullName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
            pos = POS.get(p.get("defaultPositionId"), str(p.get("defaultPositionId")))
            # prefer the most recent non-empty name
            m[pid] = {"name": name or m.get(pid, {}).get("name", f"#{pid}"), "pos": pos}
    _PLAYER_MAP = m
    return m


def pname(pid):
    return player_map().get(pid, {}).get("name", f"#{pid}")


def ppos(pid):
    return player_map().get(pid, {}).get("pos", "?")


_MEMBER_CACHE = {}
_TEAMOWNER_CACHE = {}


def member_name(mem):
    full = f"{mem.get('firstName','').strip()} {mem.get('lastName','').strip()}".strip()
    return full or (mem.get("displayName") or "").strip()


def member_names(season=None):
    """{memberId: member_name} — merged across all seasons if season is None."""
    key = season or "ALL"
    if key in _MEMBER_CACHE:
        return _MEMBER_CACHE[key]
    out = {}
    seasons = [season] if season else ALL_SEASONS
    for yr in seasons:
        if not has_season(yr):
            continue
        d = load(yr)
        for mem in d.get("members", []):
            mid = mem.get("id")
            nm = member_name(mem)
            if mid and nm:
                out[mid] = nm
    _MEMBER_CACHE[key] = out
    return out


# Canonical manager identity ({primaryOwnerGUID: name}) and which manager is you.
# Both are LOCAL config (real names / your identity), so the code ships clean.
# MANAGER_CANON is optional: when a GUID isn't in it, manager() falls back to the
# team's scraped owner name — so a fresh league works with no canon file at all.
# (Copy config/manager_canon.example.json to config/manager_canon.json to set one up.)
MANAGER_CANON = _load_config("manager_canon.json")
ME = _load_config("league.json").get("me", "")


def manager(season, team_id):
    """Canonical manager name for a team in a season (via primary owner)."""
    to = team_owner(season)
    prim = to.get(team_id, {}).get("owner")
    return MANAGER_CANON.get(prim, to.get(team_id, {}).get("ownerName", "?"))


def team_owner(season):
    """Per-season {teamId: {'name': teamName, 'owner': memberId, 'ownerName': str,
    'owners': [memberIds]}} using the primary (first) owner as the manager."""
    if season in _TEAMOWNER_CACHE:
        return _TEAMOWNER_CACHE[season]
    d = load(season)
    names = member_names()  # merged, so co-owners resolve even if named oddly
    out = {}
    for t in d.get("teams", []):
        owners = t.get("owners") or []
        primary = owners[0] if owners else None
        tname = t.get("name") or f"{t.get('location','')} {t.get('nickname','')}".strip()
        out[t["id"]] = {
            "name": tname,
            "owner": primary,
            "ownerName": names.get(primary, primary or "?"),
            "owners": owners,
        }
    _TEAMOWNER_CACHE[season] = out
    return out


def auction_budget(season):
    d = load(season)
    ds = d.get("settings", {}).get("draftSettings", {})
    return ds.get("auctionBudget") or 200


def draft_picks(season):
    """List of normalized picks for a season. Each pick:
       {teamId, owner, ownerName, playerId, name, pos, bid_raw, is_keeper, cost}
       cost = true dollars spent (keeper bids have 100 subtracted).
    """
    d = load(season)
    to = team_owner(season)
    picks = d.get("draftDetail", {}).get("picks", []) or []
    out = []
    for p in picks:
        raw = p.get("bidAmount", 0) or 0
        # Keeper is the ESPN `keeper` flag ONLY (exactly 1/team in 2020-24).
        # In a $300 budget, non-keeper studs legitimately exceed $100, so a
        # bid>=100 heuristic would corrupt real spend. Real keeper cost is the
        # displayed bid minus the flat $100 inflation.
        is_keeper = bool(p.get("keeper"))
        cost = raw - KEEPER_INFLATION if is_keeper else raw
        if cost < 0:
            cost = 0
        pid = p.get("playerId")
        tid = p.get("teamId")
        prim = to.get(tid, {}).get("owner")
        out.append({
            "teamId": tid,
            "owner": prim,
            "manager": MANAGER_CANON.get(prim, to.get(tid, {}).get("ownerName", "?")),
            "ownerName": to.get(tid, {}).get("ownerName", "?"),
            "teamName": to.get(tid, {}).get("name", "?"),
            "playerId": pid,
            "name": pname(pid),
            "pos": ppos(pid),
            "bid_raw": raw,
            "is_keeper": is_keeper,
            "cost": cost,
            "overall": p.get("overallPickNumber"),
            "nominatingTeamId": p.get("nominatingTeamId"),
        })
    return out


def draft_type(season):
    return load(season).get("settings", {}).get("draftSettings", {}).get("type")
