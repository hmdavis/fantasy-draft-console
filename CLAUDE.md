# CLAUDE.md

Guidance for Claude working in this repo. See `README.md` for the user-facing overview.

## What this is

One app, two modes:

- **Draft (pre-season)** — a live fantasy-football **auction draft console** (`/draft`)
  that re-prices players from calibrated opponent tendencies, plus a thin LLM advisor
  (`/api/advise`). Fed by the draft pipeline (`scraping/`, `analysis/`, `draft_sheets/`).
- **Manage (in-season)** — the **fftiers board** (`/manage`): one page, three valuation
  methods per player (Boris Chen-style ECR tiers, elboberto VBD, CSG games-based VBD),
  two horizons (this week / rest of season), across every league in `config/boards.json`.
  Fed by the `fftiers/` package (FantasyPros consensus ranks + live ESPN or Sleeper
  projections).

`draft_app/server.py` serves both: `/` redirects to `DEFAULT_MODE` (env, default
`manage`), `/draft` → `static/index.html`, `/manage` → `static/board.html`.

## Layout

| Path | Role | Tracked? |
|------|------|----------|
| `draft_sheets/draft_tool_template.html` | **Draft frontend source** — edit this | yes |
| `draft_sheets/board_template.html` | **Manage frontend source** — edit this | yes |
| `draft_app/static/index.html` | Generated: draft template + injected data | no (generated) |
| `draft_app/static/board.html` | Generated: board template + injected viz-data | no (generated) |
| `draft_app/server.py` | FastAPI: two modes + `/api/advise` | yes |
| `pipeline.py` | **Single entry point** — draft: `scrape/calibrate/csg/simulate/build/inject/all`; manage: `pull/tiers/vbd-boards/csg-boards/board/week` | yes |
| `fftiers/` | The manage engine: league-configurable tier charts (`cli`), elboberto VBD port (`vbd_cli`), CSG VBD port (`csg_cli`), live ESPN client (`espn_cli`), live Sleeper client (`sleeper`, `sleeper_cli`), board builder (`board`) | yes |
| `leagues/*.yaml` | Per-league configs (teams, roster slots, scoring) — name real leagues | no (except `example-standard-12.yaml`) |
| `config/boards.json` | Manage league registry: key → league_id, team_id, label, yaml | no (local; `boards.example.json` tracked) |
| `dat/` | FantasyPros rank caches (`{year}/week-N-POS-SCORING.json`; week **90** = ROS) + ESPN pulls (`espn/{key}-*.csv`, `-roster.json`, `-meta.json`) | no (fetched) |
| `out/` | fftiers outputs: `{key}/week-N/{png,txt,csv,vbd,csg}`, `board/viz-data.json` | no (generated) |
| `pyproject.toml` | fftiers packaging (`pip install -e .` into `.venv`) | yes |
| `draft_sheets/build_tool_data.py` | Draft console builder — projections + scrape → `tool_data.json` | yes |
| `scraping/scrape_league.py` | Fresh-setup ESPN scraper (settings + managers, config-driven) | yes |
| `draft_sheets/*_elboberto.xlsm` | Universal projection baseline (checked in) | yes |
| `draft_sheets/CSG*auction.xlsm` | CSG sheet — **complementary market view** (third-party) | no (local) |
| `draft_sheets/extract_csg.py` | CSG `Overall` tab → `csg_consensus.json` | yes |
| `draft_sheets/check_csg_settings.py` | Diff CSG's league settings vs your scrape | yes |
| `draft_sheets/csg_consensus.json` | Generated CSG consensus data, by season | no (generated) |
| `config/league.json` | Draft league: id, season, `me`, projections path, `my_mult` | no (local) |
| `config/briefing.md` | Advisor system prompt (league-specific) | no (local) |
| `config/` | All local, league-specific config + secrets | no (except `*.example*`, `README.md`) |
| `draft_app/eval_advisor.py` | Advisor eval (mock draft → probe → check) | yes |
| `draft_sheets/tool_data.json` | Generated draft console data (players + profiles) | no (generated) |
| `scraping/scrape.py`, `scrape_playercards.py`, `extract_har.py` | Full historical scrapers (draft calibration) | yes |
| `analysis/calibrate.py` | Opponent history → `config/tendencies.json` (reuses `a18.build_agents`) | yes |
| `analysis/lib.py` | **Canonical** `effective_wallet` / `regime` / `norm_cost` helpers | yes |
| `analysis/price_curve.py` | Full-supply price + tier curve → `config/price_curve.json` | yes |
| `analysis/plan_tiers.py` | Grades a budget plan → the tiers it actually buys | yes |
| `analysis/a5`, `a18`, `a19`, `backtest_willgo.py` | Calibration + auction-sim engine | yes |
| `analysis/research/a1..a17` | Archived one-off research behind `reports/league_analysis.md` | yes |
| `config/tendencies.json`, `price_curve.json` | Calibration outputs | no (local) |
| `scraping/raw/`, `reports/`, `league/` | League data / analysis outputs | no (local) |
| `tests/draft_regression.py` | sha256-pins the draft payload (golden: `tests/draft_golden.json`) | yes |
| `tests/board_smoke.py` + `tests/fixtures_board/` | Offline manage-board build + inject check (synthetic data) | yes |
| `tests/board_pull.py` | Offline ESPN + Sleeper `pull`/`sync` check (network calls replaced, synthetic data) | yes |
| `docs/local/` | Local plans/notes (name real leagues/ids) | no (local) |

History note: the previous in-season stack (engine/, season console, research_agent/app,
claude_plugin, WS-8 backtests) was removed in favor of fftiers — it lives on the
`in-season-console` branch history if ever needed.

## The golden rule: edit the template, then re-inject

Both served pages are **generated**. Never hand-edit anything under `draft_app/static/`.

- Draft: edit `draft_sheets/draft_tool_template.html` (has a `/*DATA*/` marker), then
  `python3 pipeline.py inject` (template + `tool_data.json` → `static/index.html` + `static/data.json`).
- Manage: edit `draft_sheets/board_template.html` (same `/*DATA*/` convention), then
  `python3 pipeline.py board` (template + `out/board/viz-data.json` → `static/board.html`).

## Run / verify

```bash
# server (advisor needs the key; briefing.md is loaded if present)
cd draft_app && ANTHROPIC_API_KEY=sk-ant-... ../.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
curl -s localhost:8000/healthz        # {"ok":true,"advisor":true,...}
#   /        → 307 to /manage (override with DEFAULT_MODE=draft)
#   /draft   → auction console      (404 hint: pipeline.py build inject)
#   /manage  → fftiers board        (404 hint: pipeline.py week)
python3 tests/draft_regression.py     # draft payload byte-identical
.venv/bin/python tests/board_smoke.py # manage board builds + injects offline
.venv/bin/python tests/board_pull.py  # ESPN + Sleeper pull/sync write the board inputs
python3 draft_app/eval_advisor.py     # eval the advisor against a mock draft
```

The server has **no --reload**; restart it after editing `server.py` or `config/briefing.md`.

Python: use the repo venv (`.venv/bin/python pipeline.py …`) — it has openpyxl (draft
build) and the fftiers deps (scikit-learn, matplotlib, PyYAML; `pip install -e .`).

## Draft data pipeline (regenerate console data)

**One entry point — `pipeline.py`.** Stages run in the order you list them:

| Stage | Does | Wraps |
|-------|------|-------|
| `scrape` | ESPN settings + managers → `raw/{season}/league_full.json` (`--deep` also pulls history) | `scraping/scrape_league.py` (+ `scrape.py`) |
| `calibrate` | opponent auction history → `config/tendencies.json` | `extract_elboberto_master.py`, `analysis/calibrate.py` |
| `csg` | CSG sheet → `csg_consensus.json` (market view; self-skips if absent) | `draft_sheets/extract_csg.py` |
| `simulate` | agent-auction strategy test (stdout; `--stress` adds `a19`) | `analysis/a18_agent_auction.py` |
| `build` | projections × league → `tool_data.json` | `draft_sheets/build_tool_data.py` |
| `inject` | template + data → `static/index.html` + `data.json` | (the golden-rule step) |
| `all` | local refresh: `calibrate` (if history) → `csg` (if sheet) → `build` → `inject` | — |

```bash
.venv/bin/python pipeline.py all       # refresh console from already-scraped history
.venv/bin/python pipeline.py scrape calibrate build inject   # full refresh from ESPN
.venv/bin/python pipeline.py build inject   # rebuild after editing the template only
```

**Valuation is league-accurate (do not regress this):** `build_tool_data.py` recomputes FPTS
from the scraped ESPN scoring (statId→points), then VBD (replacement = teams×starters + FLEX
pooled over RB/WR/TE) and auction-$ (VBD share of the discretionary pool). It does NOT trust
the workbook's baked-in CheatSheet values. Falls back to CheatSheet if raw sheets are absent,
and to workbook roster defaults + generic opponents when no scrape exists, so the console
always runs. Schema is in build_tool_data's docstring.

**Opponent calibration:** `calibrate.py` reuses `a18_agent_auction.build_agents()` —
per-manager positional aggression ($-weighted paid/proj), stars-and-scrubs concentration,
and max-buy ceiling from 2017–2025 auction history — and writes `config/tendencies.json`
(`{name: {mult, conc, maxbuy}}` plus a reserved `_league_default` entry). A manager with
**no** auction history gets `_league_default` — the **league average**, not a flat `1.0`
(1.0 would model a newcomer paying ~2.4× the league norm at QB with no ceiling).
`config/league.json` `manager_labels` optionally renames a manager on the board; tendencies
merge on the **relabelled** name, and `calibrate.py` bridges ESPN member GUIDs so a returning
manager whose display name drifted still matches. `elboberto_projections.json` (regenerated
from the tracked `*_elboberto.xlsm` by `extract_elboberto_master.py`) feeds
calibration/research **only**, never the console valuation.

For a brand-new league with no history, `all` skips `calibrate` and builds a
neutral-opponent console. The archived `analysis/research/a1..a17` run with
`PYTHONPATH=analysis python3 analysis/research/<script>.py`.

## Manage pipeline (the weekly board refresh)

| Stage | Does | Network |
|-------|------|---------|
| `pull` | ESPN or Sleeper projections (week + ROS CSVs), my roster, league meta → `dat/espn/`; refreshes FantasyPros rank caches (current week + ROS sentinel week 90) → `dat/{year}/` | ESPN/Sleeper + FantasyPros |
| `tiers` | GMM tier charts per league → `out/{key}/week-N/{png,txt,csv}` | none with `--no-download` |
| `vbd-boards` | elboberto VBD, both horizons, league-scored ESPN projections → `out/{key}/week-N/vbd/` | none |
| `csg-boards` | CSG games-based VBD, both horizons → `out/{key}/week-N/csg/` | none |
| `board` | viz-data + template → `static/board.html` (golden rule); pushes the build to the Supabase snapshot store when configured (`--no-push` to skip) | Supabase (optional) |
| `week` | `pull → tiers → vbd-boards → csg-boards → board` — the Tuesday one-liner | as `pull` |

```bash
.venv/bin/python pipeline.py week                  # full weekly refresh, all leagues
.venv/bin/python pipeline.py week --league 2kdome  # one league
.venv/bin/python pipeline.py board                 # re-render after editing the template
```

Requirements: `config/boards.json` (see `boards.example.json`), `leagues/{key}.yaml` per
league (generate with `.venv/bin/python -m fftiers.espn_cli sync-league <id>`), ESPN cookies
in `scraping/.espn_auth.json` (or `ESPN_SWID`/`ESPN_S2` env), and `FANTASYPROS_API_KEY`
(or `api_key.txt`, gitignored) — without the FP key, `pull` warns and reuses cached ranks.

**Sleeper leagues** (`"platform": "sleeper"` in `config/boards.json`, with `league_id` and
`user` or `team_id`): `fftiers/sleeper.py` reads the public Sleeper API and writes the
same `dat/espn/<key>-*` files, so vbd/csg/board do not know the platform. It needs no
ESPN cookies and no API key. Points are the Sleeper projection stats (undocumented
`api.sleeper.app/projections/nfl/<season>/<week>`) times the league's `scoring_settings`
(same keys). ROS sums each week to the last playoff week. Lineup slots use ESPN names
(DEF → `DST`, BN → `BE`, reserve → `IR`). The YAML comes from
`python -m fftiers.sleeper_cli sync-league <id> --dest leagues/<key>.yaml`.
`meta.platform` drives the platform name on the page; ESPN meta has no `platform`.

**Snapshot store (Supabase, project `fantasy-console`):** every `board` build is also
persisted as one row in `board_snapshots` (whole viz-data as jsonb) via the
`put_board_snapshot` RPC; the server's `/api/board-data` reads the latest via
`get_board_snapshot`, falling back to `out/board/viz-data.json`. The board page boots
from its baked-in data, then fetches `/api/board-data` and hot-swaps if a newer build
exists — so a local `pipeline.py week` refreshes the DEPLOYED site without a redeploy.
The table has RLS on with zero policies; both RPCs require `BOARD_DB_SECRET` (stored in
`private.app_config`), so the publishable API key alone reads nothing. All three
settings (`SUPABASE_URL`, `SUPABASE_API_KEY`, `BOARD_DB_SECRET`) live in `config/.env`
locally and must be set as Railway env vars for the deployed reader. Endpoint responses
are cached in-process for 60s. History accumulates one row per build — week-over-week
trend data for free.

**Relational fact tables (Supabase), fed automatically:** an AFTER INSERT trigger on
`board_snapshots` (`shred_board_snapshot`) shreds every build into three layers —
`consensus_ranks` (global: one row per season/fp_week/scoring-flavor/player; fp_week 90
= ROS), `player_valuations` (per league_id × horizon × method: points/vbd/tier — raw
data is global, league settings map it to per-league value), and `rosters` (per
league/week). Upserts, so rebuilding a week overwrites it. No client code involved —
any snapshot push populates them. RLS is on with no policies (server-side access only)
until app features read them directly.

Key conventions (do not regress):
- **Week 90 is the ROS sentinel** in `dat/{year}/` FantasyPros caches — `board.py` reads
  both `week-N` and `week-90` caches per position/scoring.
- **The board's valuation math lives in fftiers**, ported and verified against its source
  workbooks (elboberto FPTS to ~rounding over 492 players; CSG StartVBD/AvgVBD ≤0.07 over
  484). The elboberto and CSG boards are separate methods on purpose — never merge them.
- **ESPN pulls are the source of truth for non-standard scoring** (~400 players in the
  league's exact points); FantasyPros consensus covers STD/HALF/PPR ranks only, and
  off-formula scoring is surfaced in `SCORING-NOTES.txt`, never silently ignored.
- `fftiers/espn.py` and `scraping/scrape_league.py` are two separate ESPN clients (manage
  vs draft) with separate auth loaders reading the same cookie file. Known seam, accepted.
- **The Trades tab** (third board section) is an evidence-based trade finder: FA-pooled
  team value (`teamValue = rosLineup(roster ∪ top FAs)`, both sides, so deltas are
  trade-only edge and pure waiver churn scores 0), win-win gate (myΔ>0.15, theirΔ≥0.3),
  injured/consensus-unranked players and K/DST are never trade currency, forced drops of
  boris top-12 players reject the candidate, dominance dedupe with negotiation ladders,
  plus a custom offer evaluator (no filters, warnings instead). Inputs: every team's
  roster (`pull`/`sync` write `dat/espn/{key}-teams.json`) and the week-over-week value
  ledger (`out/board/history.json` → `prev_vbd`). Validated by adversarial QA, an
  editorial judge (verdict: SOUND), and an empirical backtest whose reusable harness
  lives in `league/backtest_trades/` (local) — rerun it as weeks accrue before trusting
  threshold changes.

## Cross-season price normalization (read before touching any historical $)

**The nominal `auctionBudget` is not spending power.** 2020–2024 report $300, but that extra
$100 existed only to carry the keeper encoding (a keeper is recorded as a bid of `cost+$100`,
so the cap had to rise to fit it). Proof: total league spend is **~$2,400 in every season**,
$200-cap and $300-cap alike. `lib.effective_wallet(season)` — non-keeper dollars actually
spent per team — is the comparable denominator (~$199 full-supply, ~$170–188 keeper era).

A **second, independent** effect must not be conflated with it: keeper seasons removed 12
elite players from supply, pushing top-of-board prices **up** and mid-board **down**
(measured full-supply/keeper share ratio: 0.77–0.86 at ranks 1–8, 0.99–1.18 at ranks 11–15,
0.97 whole-distribution). So:

| quantity | treatment |
|---|---|
| top-of-board (max-buy, price curve, plan ceilings) | **`lib.FULL_SUPPLY_SEASONS` only** — rescaling keeper-era top prices is not enough, they encode absent scarcity |
| whole-distribution traits (positional `mult`, `conc`) | may pool all seasons **after** `lib.norm_cost` (~3% distortion) |

Canonical helpers live in `analysis/lib.py`: `regime`, `effective_wallet`, `norm_cost`,
`FULL_SUPPLY_SEASONS`, `KEEPER_SEASONS`. **Use them; do not re-derive.** Normalized:
`a18.build_agents` (mult wallet-normalized, `conc` keeper-excluded, `maxbuy` full-supply-only
and no longer +15%), `a5_draft_value`, `research/backtest_budget`, `research/strategy_search_v2`,
`price_curve`, `plan_tiers`.

**Two bugs this audit fixed, worth not reintroducing:** `build_agents`' `conc`/`maxbuy` loop
counted keeper picks (its docstring claimed otherwise), and every shape-replay `lineup()`
hardcoded a $200 wallet while shopping at raw prices.

## What the league actually pays (`analysis/price_curve.py` → `config/price_curve.json`)

Derived from `FULL_SUPPLY_SEASONS` (2017/18/19/25 — the regime 2026 repeats, `keeperCount=0`).
Board-rank curve: **#1 $77 · #2 $73 · #3 $71 · #5 $69 · #10 $61**. Tier ladder: RB1 $72 /
RB2 $60 / RB3 $59 ‖ RB4 $17 / RB5 $24 ‖ RB6 $6; WR1 $68 / WR2 $57 / WR3 $50 ‖ WR4 $26 /
WR5 $10; TE1 $33; **QB1 $32** (QB has re-priced — top QB went $19 in 2019 → $39 in 2025, so
the "elite QB is a steal" thesis is retired). Tiers 4–5 are the worst points-per-dollar on
the board: spend up or down, never in between. `analysis/plan_tiers.py` grades a plan against
this and flags trough money.

**Still honest about the limit:** *which* budget shape wins is **not** validated
out-of-sample — `research/strategy_search_v2.py` (regime-corrected) lands at **41%**, i.e.
worse than the coin-flip gate and below v1's 45%. An earlier 56% "validated" reading was an
artifact of normalizing by the nominal $300. No static shape is promoted; `config/plan.json`
remains a disciplined default.

## The CSG sheet: a complementary market view (never the draft valuation)

`extract_csg.py` reads the CSG workbook's `Overall` tab (header row 11, players from row
12; layout stable across v11–v14) into `csg_consensus.json`, keyed by season.
`build_tool_data.py` merges the current season onto each player as `p["mkt"]`
(`price`, `espn`, `ecr`, `boris`, `gold`, `advbd`, `bs`, `status`, plus a derived `edge` =
our `worth` − market price). Joined on a punctuation/suffix-insensitive name key
(`extract_csg.norm_name` — the single definition, imported by the builder; don't fork it).

**It is advisory and must stay that way.** `worth`/`vbd` remain recomputed from the scraped
ESPN scoring — CSG's own VBD/price columns are computed for *its* settings, so treating them
as valuation would silently regress league accuracy. The value is the *disagreement*: the
console shows `market $X (±N us)` under Worth and a ▲/▼ on the board when divergence is
material (≥$5 **and** ≥25%), and the advisor state carries `market_price`/`ecr`.
(The manage board's CSG method is different: `fftiers/csg.py` re-runs the CSG *engine* on
the league's own settings — that one IS a valuation, scoped to the manage mode.)

**Coverage is uneven per year — check `pipeline.py csg` output, never assume a column.**
**Do not write these workbooks with openpyxl:** it cannot recalculate formulas and drops
conditional-formatting/data-validation extensions. Edit in Excel, save, re-run
`check_csg_settings.py`, then `python3 pipeline.py csg build inject`.

## Deploying (Railway)

`./deploy_railway.sh` (`--dry-run` to inspect first). The repo's GitHub remote is **public**
and the served payloads (`draft_app/static/*`) + `config/briefing.md` are gitignored, so a
git-based deploy would ship an empty app. The script instead stages just the runtime
surface — `server.py`, `requirements.txt`, `Procfile`, `railway.json`, `static/index.html`,
`static/data.json`, `static/board.html`, `config/briefing.md` — into a temp dir and runs
`railway up` from there, so ESPN cookies, raw scrapes, the `.xlsm`, `dat/`, `leagues/` and
`analysis/` never leave the box. It refuses to deploy if anything secret-shaped is staged.

**Auth is two parallel mechanisms** (either grants access; neither configured = open,
so local dev is frictionless): Supabase email OTP login for browsers (`/login` →
`/api/session` sets an httpOnly cookie; server validates the token against
`/auth/v1/user` and requires the email ∈ `ALLOWED_EMAILS` — signups are open by design
for future multi-tenant, the allowlist is the gate) and HTTP Basic `CONSOLE_PASSWORD`
for curl/scripts. Multi-tenant seeds already in Supabase: `user_leagues` (per-user
league registry, RLS owner-only; pre-seeded rows claimed by email via the
`on_auth_user_created` trigger) and `board_snapshots.user_id`.

Service env vars: `ANTHROPIC_API_KEY`, `CONSOLE_PASSWORD` (HTTP Basic on everything but
`/healthz`; **unset = the URL is public and `/api/advise` spends your credit**),
`ALLOWED_EMAILS` (comma-separated; the email-login gate),
`STRATEGY_BRIEFING_PATH=/app/config/briefing.md`, optional `DEFAULT_MODE=draft|manage`,
and — for live board data without redeploys — `SUPABASE_URL`, `SUPABASE_API_KEY`,
`BOARD_DB_SECRET` (see the snapshot-store section; without them `/manage` serves its
baked data).

## Conventions & guardrails

- **Secrets stay out of git.** `ANTHROPIC_API_KEY` via env (the user keeps it in 1Password:
  `op read 'op://HMD LOCAL/Claude - API Key/credential'`). ESPN cookies live in
  `scraping/.espn_auth.json` (gitignored); the FantasyPros key in env or gitignored
  `api_key.txt`. Never print or commit these.
- **League-specific content stays local** (see `.gitignore`): scraped data, generated
  payloads, `reports/`, `league/`, `docs/local/`, `config/league.json`, `config/boards.json`,
  `config/briefing.md`, `leagues/*.yaml` (except the example), `dat/`, `out/`. Adding a file
  to `docs/` (rather than `docs/local/`) is a decision to PUBLISH it — the remote is public,
  so keep league ids, manager real names and team names out of anything tracked, including
  `draft_sheets/board_template.html` and `tests/fixtures_board/`. Use the league KEY
  (`2kdome`), which is already public in the code, not the ESPN display name or a manager's
  name. The universal `*_elboberto.xlsm` projection baseline **is** tracked; live-edited
  `.xlsx`/`.csv` copies are not.
- **Models:** default `claude-haiku-4-5` for live latency; the dropdown also allows
  `claude-sonnet-5` and `claude-opus-4-8` (allow-listed in `server.py`).
- **The advisor's grounding** is the live `state` posted each call (every team's budget,
  needs, roster; best-available; inflation; on-the-block). Keep `draftStateForAdvisor()`
  in the template and the eval's state-builder in sync when changing the shape.
- **Nothing ships untested**: `tests/draft_regression.py` guards the draft payload;
  `tests/board_smoke.py` guards the manage build; `tests/board_pull.py` guards the ESPN
  and Sleeper pulls. Run them before calling work done.
- Don't commit or push unless the user asks.
