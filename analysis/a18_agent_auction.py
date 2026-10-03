#!/usr/bin/env python3
"""Analysis 18: agent-based 2026 auction. Each manager is a bidding agent
calibrated to their real history (positional aggression vs projection §11,
stars-vs-scrubs concentration §2, max-buy ceiling). They bid independently; the
auction dynamics emerge. Harry is run BOTH as his validated-optimal self and his
historical self vs the same 11-opponent field.
"""
import json
import os
import re
import random
import statistics
from collections import defaultdict
import lib

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir,
              "draft_sheets", "elboberto_projections.json")))
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
LEAGUE_MULT = {"QB": 0.41, "RB": 1.31, "WR": 1.47, "TE": 0.74}
POS = ("QB", "RB", "WR", "TE")
# roster: 1QB 2RB 2WR 1TE 2FLEX(RB/WR/TE) + 6 bench = 14 spendable; DST=$1 sep
START = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
NFLEX, NBENCH, BUDGET = 2, 6, 199


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def proj_lookup(year):
    return {norm(p["name"]): p for p in PROJ[str(year)] if p.get("proj_value") is not None}


def build_agents():
    """Per-manager: pos_mult (DOLLAR-WEIGHTED paid/proj by pos — stud-weighted, so
    cheap darts don't drag it down and keeper-suppressed years don't distort it),
    concentration (top3%), max_buy. Keeper picks excluded (their price isn't a bid)."""
    paid_sum = defaultdict(lambda: defaultdict(float))
    proj_sum = defaultdict(lambda: defaultdict(float))
    ncount = defaultdict(lambda: defaultdict(int))
    for yr in [y for y in [2022, 2023, 2024, 2025] if lib.has_season(y)]:
        pl = proj_lookup(yr)
        for p in lib.draft_picks(yr):
            if p["is_keeper"] or p["cost"] < 1:
                continue
            e = pl.get(norm(p["name"]))
            if e and (e["proj_value"] or 0) >= 3:
                paid_sum[p["manager"]][p["pos"]] += p["cost"]
                proj_sum[p["manager"]][p["pos"]] += e["proj_value"]
                ncount[p["manager"]][p["pos"]] += 1
    top3 = defaultdict(list); maxbuy = defaultdict(list)
    for yr in [y for y in range(2017, 2026) if lib.has_season(y)]:
        byteam = defaultdict(list)
        for p in lib.draft_picks(yr):
            byteam[p["teamId"]].append(p)
        for tid, ps in byteam.items():
            m = lib.manager(yr, tid)
            costs = sorted((x["cost"] for x in ps), reverse=True)
            sp = sum(costs) or 1
            top3[m].append(100*sum(costs[:3])/sp); maxbuy[m].append(costs[0] if costs else 0)
    agents = {}
    for m in top3:
        mult = {}
        for pos in POS:
            # dollar-weighted ratio (sum paid / sum proj); needs >=3 picks & real proj,
            # else fall back to the league positional multiplier
            if ncount[m].get(pos, 0) >= 3 and proj_sum[m].get(pos, 0) > 0:
                mult[pos] = paid_sum[m][pos] / proj_sum[m][pos]
            else:
                mult[pos] = LEAGUE_MULT[pos]
        agents[m] = {"mult": mult,
                     "conc": statistics.mean(top3[m]),
                     "maxbuy": max(maxbuy[m]) * 1.15 if maxbuy[m] else 100}
    return agents


VALIDATED_HARRY = {"mult": {"QB": 0.9, "RB": 1.4, "WR": 1.4, "TE": 1.05},
                   "conc": 72, "maxbuy": 100, "elite_qb_steal": True,
                   # positional spend discipline (the actual recommended plan):
                   # 1 elite RB + mid RB, 1 elite WR, value TE, value/elite QB
                   "env": {"RB": 112, "WR": 58, "TE": 15, "QB": 14}}


def max_bid(agent, p, budget, roster, rng):
    base = max(0.5, p["proj_value"] or 0)
    pos = p["pos"]
    # slot availability
    need = roster["need"]; flex = roster["flex"]; bench = roster["bench"]
    if need.get(pos, 0) > 0:
        nf = 1.0
    elif pos in ("RB", "WR", "TE") and flex > 0:
        nf = 1.0
    elif bench > 0:
        nf = 0.4
    else:
        return 0
    mb = base * agent["mult"].get(pos, 1.0) * nf
    # stars-and-scrubs tilt
    if agent["conc"] > 72:
        if base >= 25:
            mb *= 1.12
        elif 8 <= base < 25:
            mb *= 0.82
    # validated Harry steals elite QB cheap (won't overpay but happily wins value)
    if agent.get("elite_qb_steal") and pos == "QB" and base >= 25:
        mb = max(mb, base * 0.9)
    mb *= rng.lognormvariate(0, 0.11)
    slots_open = sum(need.values()) + flex + bench
    mb = min(mb, budget - (slots_open - 1), agent["maxbuy"])
    # positional spend envelope (disciplined agents only)
    if "env" in agent and pos in agent["env"]:
        mb = min(mb, agent["env"][pos] - roster["spent_pos"].get(pos, 0))
    return max(0, mb)


def new_roster():
    return {"need": dict(START), "flex": NFLEX, "bench": NBENCH, "players": [],
            "spent_pos": {}}


def assign(roster, p, price):
    pos = p["pos"]
    if roster["need"].get(pos, 0) > 0:
        roster["need"][pos] -= 1; slot = "start"
    elif pos in ("RB", "WR", "TE") and roster["flex"] > 0:
        roster["flex"] -= 1; slot = "flex"
    else:
        roster["bench"] -= 1; slot = "bench"
    roster["spent_pos"][pos] = roster["spent_pos"].get(pos, 0) + price
    roster["players"].append({**p, "pay": price, "slot": slot})


def run_auction(agents, seed):
    rng = random.Random(seed)
    teams = {m: {"budget": BUDGET, "roster": new_roster()} for m in agents}
    pl = proj_lookup(2026)
    pool = [p for p in PROJ["2026"] if p["pos"] in POS and (p["proj_value"] or 0) > -3]
    remaining = sorted(pool, key=lambda p: -(p["proj_value"] or 0))
    order = list(agents.keys()); rng.shuffle(order); ni = 0
    while remaining:
        active = [m for m in agents
                  if sum(teams[m]["roster"]["need"].values()) + teams[m]["roster"]["flex"] + teams[m]["roster"]["bench"] > 0]
        if not active:
            break
        # nominator nominates the best remaining player (studs first -> front-loaded)
        player = remaining.pop(0)
        bids = []
        for m in agents:
            t = teams[m]
            r = t["roster"]
            if sum(r["need"].values()) + r["flex"] + r["bench"] <= 0:
                continue
            mb = max_bid(agents[m], player, t["budget"], r, rng)
            if mb >= 1:
                bids.append((mb, m))
        if not bids:
            continue
        bids.sort(reverse=True)
        winner_mb, winner = bids[0]
        second = bids[1][0] if len(bids) > 1 else 1
        price = max(1, min(int(round(second)) + 1, int(winner_mb)))
        price = min(price, teams[winner]["budget"] -
                    (sum(teams[winner]["roster"]["need"].values()) +
                     teams[winner]["roster"]["flex"] + teams[winner]["roster"]["bench"] - 1))
        price = max(1, price)
        teams[winner]["budget"] -= price
        assign(teams[winner]["roster"], player, price)
        ni += 1
    return teams


def starter_vbd(roster):
    ps = sorted(roster["players"], key=lambda r: -(r.get("start_vbd") or 0))
    need = dict(START); flex = NFLEX; tot = 0; slots = []
    for r in ps:
        if need.get(r["pos"], 0) > 0:
            need[r["pos"]] -= 1; tot += (r.get("start_vbd") or 0); slots.append(r)
        elif r["pos"] in ("RB", "WR", "TE") and flex > 0:
            flex -= 1; tot += (r.get("start_vbd") or 0); slots.append(r)
    return tot, slots


def main():
    opp = build_agents()
    field = [m for m in {lib.manager(2025, t) for t in lib.team_owner(2025)} if m != lib.ME]
    field = [m for m in field if m in opp][:11]

    NSIM = 100
    results = {}
    for variant, hp in [("validated", VALIDATED_HARRY), ("historical", opp[lib.ME])]:
        agents = {m: opp[m] for m in field}
        agents["Harry"] = hp
        strengths = []; rosters = []
        for s in range(200, 200 + NSIM):
            teams = run_auction(agents, s)
            vbd, slots = starter_vbd(teams["Harry"]["roster"])
            strengths.append(vbd); rosters.append(slots)
        results[variant] = (strengths, rosters, agents)

    print("=" * 84)
    print(f"ANALYSIS 18 — AGENT-BASED 2026 AUCTION ({NSIM} sims, calibrated opponents)")
    print("=" * 84)
    print("Each opponent bids per their real history; Harry run as validated vs historical self.")
    print("\n### Calibrated agent multipliers ($-weighted paid/proj by position)")
    print(f"   {'manager':18}{'RB':>6}{'WR':>6}{'TE':>6}{'QB':>6}{'conc%':>7}{'maxbuy':>8}")
    for m in sorted(opp, key=lambda m: -opp[m]["mult"]["RB"]):
        a = opp[m]
        print(f"   {m:18}{a['mult']['RB']:>6.2f}{a['mult']['WR']:>6.2f}{a['mult']['TE']:>6.2f}"
              f"{a['mult']['QB']:>6.2f}{a['conc']:>7.0f}{a['maxbuy']:>8.0f}")
    print()
    for variant in ("validated", "historical"):
        strengths, rosters, _ = results[variant]
        print(f"### Harry — {variant} strategy")
        print(f"   projected STARTER VBD: median {statistics.median(strengths):.0f}  "
              f"(range {min(strengths):.0f}-{max(strengths):.0f})")
        # median roster template by pos-rank
        slot_p = defaultdict(list); slot_t = defaultdict(list); posspend = defaultdict(list)
        for slots in rosters:
            byp = defaultdict(list)
            for r in slots:
                byp[r["pos"]].append(r)
            for pos, rs in byp.items():
                rs.sort(key=lambda r: -r["pay"])
                for i, r in enumerate(rs, 1):
                    slot_p[(pos, i)].append(r["pay"]); slot_t[(pos, i)].append(r["tier"])
                posspend[pos].append(sum(r["pay"] for r in rs))
        for pos in POS:
            i = 1
            while (pos, i) in slot_p:
                pr = slot_p[(pos, i)]
                tt = statistics.mode(slot_t[(pos, i)])
                print(f"      {pos} #{i}: ${int(statistics.median(pr)):>3} ({min(pr)}-{max(pr)})  ~{tt}")
                i += 1
        print("      budget: " + "  ".join(
            f"{pos} ${int(statistics.median(posspend[pos]))}" for pos in POS if posspend.get(pos)))
        print()
    dv = statistics.median(results["validated"][0])
    dh = statistics.median(results["historical"][0])
    print(f"### Edge of validated strategy: {dv-dh:+.0f} projected starter VBD "
          f"({100*(dv-dh)/abs(dh) if dh else 0:+.0f}%) vs historical Harry")
    # head-to-head on identical seeds (same field, same player-price draws)
    sv, sh = results["validated"][0], results["historical"][0]
    wins = sum(1 for a, b in zip(sv, sh) if a > b)
    diffs = sorted(a - b for a, b in zip(sv, sh))
    print(f"   head-to-head (same draft, 100 seeds): validated beat historical "
          f"{wins}/100  (median margin {statistics.median(diffs):+.0f} VBD, "
          f"5th–95th pct {diffs[4]:+.0f} to {diffs[94]:+.0f})")


if __name__ == "__main__":
    main()
