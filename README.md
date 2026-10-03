# Fantasy Console — draft + manage

One app, two modes:

- **Draft (pre-season)** — a dynamic command console for a **fantasy football auction
  draft**, with a thin LLM advisor. It re-prices every remaining player in real time from
  the calibrated bidding tendencies of each opponent, tracks your build against a target
  roster, surfaces value/scarcity as the board moves, and (optionally) calls Claude for a
  live read of the room after every pick.
- **Manage (in-season)** — the **fftiers board**: one page, every league you play in,
  each player valued three independent ways — Boris Chen-style expert-consensus tiers
  (GMM clustering), an elboberto-workbook VBD port, and a CSG games-based VBD port — at
  two horizons (this week / rest of season), with your roster and free agents flagged.
  Rebuilt from live ESPN + FantasyPros data with one command every week.

Built for specific leagues, but the framework is **bring-your-own-league**: the code and
the universal projection baseline are tracked here; your league configs, data, secrets,
and advisor briefing stay local (see [What's ignored](#whats-ignored)).

## What it does

**Draft mode** (`/draft`):

- **On the block** — type the nominated player → **Worth**, live **Will go ≈** (predicted
  sale price from opponents' calibrated bids × market inflation), and a **dynamic Max bid**
  that adjusts all draft long for value, scarcity, market trend, your budget and roster.
- **Value board / Projections** — best-available ranked by worth, or raw projections
  (FPTS / VORP / tiers) filterable by position **and tier**, with live scarcity/cliffs.
- **Your build vs target** — roster (starters + bench) tracked against a target split.
- **Advisor (LLM)** — ask anything, or let it auto-post a read after each pick.

**Manage mode** (`/manage`):

- **Three methods, side by side** — consensus-rank tiers vs. two VBD engines run on your
  league's exact scoring and roster shape; when they disagree about a player, that's the
  signal.
- **Two horizons** — start/sit reads (this week) and trade/waiver reads (rest of season).
- **Multi-league tabs** — every league in `config/boards.json`, each with your roster,
  lineup shape, and free-agent flags.
- **Weekly one-liner** — `python3 pipeline.py week` pulls ESPN projections + rosters and
  FantasyPros ranks, rebuilds every board, and re-renders the page.

## Architecture

```
DRAFT                                              MANAGE
projections .xlsm ─┐                               ESPN (fftiers-espn) ─┐
ESPN league scrape ┼► build_tool_data.py           FantasyPros ranks ───┼► pipeline.py week
tendencies.json ───┘        │                      leagues/*.yaml ──────┘   (pull→tiers→vbd→csg→board)
                            ▼                                                    │
draft_tool_template.html + tool_data.json          board_template.html + viz-data.json
        └────► static/index.html                           └────► static/board.html
                     │                                                │
                 draft_app/server.py (FastAPI): /draft · /manage · /api/advise
```

`pipeline.py` drives both — draft stages (`scrape calibrate csg simulate build inject`,
or `all`) and manage stages (`pull tiers vbd-boards csg-boards board`, or `week`).

- **League-accurate valuation, both modes:** the draft builder **recomputes** FPTS/VBD/
  auction-$ from your league's ESPN scoring and roster; the manage boards run the ported
  elboberto and CSG engines on the same league-exact settings (verified against their
  source workbooks).
- **fftiers** (`fftiers/`) is also usable standalone: `fftiers --league leagues/my.yaml`
  (tier charts), `fftiers-vbd`, `fftiers-csg`, `fftiers-espn discover|sync-league|pull|roster`.

### Opponent tendencies (draft)
The per-manager bid model (`mult`/`conc`/`maxbuy`) is calibrated from auction history via
`python3 pipeline.py calibrate` (writes `config/tendencies.json`); with no history every
opponent stays neutral, or hand-write the file from what you know about your leaguemates.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r draft_app/requirements.txt   # server + draft build
pip install -e .                            # fftiers (manage mode)

# 1) Configure
cp config/league.example.json config/league.json    # draft: league_id, season, your team ("me")
cp config/boards.example.json config/boards.json    # manage: leagues, team ids
cp config/env.example config/.env                   # ESPN_SWID + ESPN_S2 (+ ANTHROPIC_API_KEY,
set -a && . config/.env && set +a                   #  FANTASYPROS_API_KEY)

# 2) Draft mode
python3 pipeline.py scrape build inject             # or `all` when you have auction history

# 3) Manage mode
python3 -m fftiers.espn_cli sync-league <league_id> # writes leagues/<key>.yaml from ESPN
python3 pipeline.py week                            # pull + boards + render

# 4) Run
cd draft_app && uvicorn server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000  (/ → manage; /draft for the auction console)
```

Run `pipeline.py` with the venv active (or `.venv/bin/python pipeline.py …`) — the draft
build needs `openpyxl`, the manage stages need `scikit-learn`/`matplotlib`.

Without `ANTHROPIC_API_KEY` everything still works; only the Advisor panel is disabled.
`FANTASYPROS_API_KEY` is optional. Without it, the board reads the public FantasyPros
ranking pages. These pages give only the current week. With a key, the API gives all weeks.

## Configuration

**All of your custom, league-specific setup lives in one un-pushed directory: `config/`**
(plus per-league `leagues/*.yaml`). `.gitignore` keeps everything local except the
`*.example` templates and README.

```bash
cp config/league.example.json  config/league.json   # draft league: id, season, "me"
cp config/boards.example.json  config/boards.json   # manage leagues: ids, team ids, yamls
cp config/briefing.example.md  config/briefing.md   # advisor prompt: your opponents + plan
cp config/env.example          config/.env          # secrets
set -a && . config/.env && set +a
```

See [`config/README.md`](config/README.md) for the full table. Generated data
(`tool_data.json`, `static/*.html`, `dat/`, `out/`, `reports/`, scrapes) is built by the
pipeline, not committed.

## The advisor

`POST /api/advise` sends `{question, state, model}` to Claude. The **system prompt** is
loaded from `config/briefing.md` (gitignored; start from `config/briefing.example.md`).
The **live state** — every team's budget, needs and roster, best-available, inflation,
and the player on the block — is posted on every call. Model is chosen from the dropdown
(allow-listed in `server.py`).

**Eval:** `python3 draft_app/eval_advisor.py` runs a tendency-driven mock auction and
probes the advisor at checkpoints, checking it stays grounded and on-strategy.

## Deploy on Railway

`./deploy_railway.sh` stages only the runtime surface (server, both generated pages, the
briefing) into a temp bundle and runs `railway up` — nothing league-private touches git.
Set `ANTHROPIC_API_KEY`, `CONSOLE_PASSWORD` (HTTP Basic on everything but `/healthz`),
and optionally `DEFAULT_MODE=draft|manage`. See `draft_app/README.md`.

## Refresh data

- **Weekly (manage):** `python3 pipeline.py week` — new ESPN projections + rosters, fresh
  FantasyPros ranks, all boards, re-rendered page.
- **New season (draft):** drop the new Elboberto `.xlsm` into `draft_sheets/`, point
  `config/league.json` at it, then `python3 pipeline.py scrape build inject` (or `all` to
  also refresh opponent calibration).

## What's ignored

`.gitignore` keeps league-specific and private files **local** (never pushed): scraped
data (`scraping/raw/`), fetched caches (`dat/`), generated boards and payloads (`out/`,
`tool_data.json`, `static/index.html`, `static/data.json`, `static/board.html`), real
league configs (`leagues/*.yaml` except the example, `config/boards.json`,
`config/league.json`), analysis outputs (`reports/`), private notes (`league/`,
`docs/local/`), the advisor `briefing.md`, and every secret (`config/.env`,
`scraping/.espn_auth.json`, `api_key.txt`). The reusable app, scrapers, templates,
pipeline, fftiers package, and the projection baseline are tracked.
