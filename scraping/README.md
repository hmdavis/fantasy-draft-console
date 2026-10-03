# Fantasy Football league scraping

Pulls your league's data off your platform's API so the console can wire in your real
roster settings, auction budget, and manager list. **ESPN** and **Sleeper** are supported.

Both write the same file — `raw/{season}/league_full.json` in ESPN's shape — so everything
downstream (`build_tool_data.py`, the console, `analysis/`) is platform-agnostic.

## Which script do I run?

| Script | Use it for | Scope |
|--------|-----------|-------|
| `scrape_sleeper.py` | **Sleeper leagues (start here).** Current-season settings + managers. No auth required. | 1 season |
| `scrape_sleeper_history.py` | Past Sleeper seasons (priced draft picks, managers) for calibrating opponent tendencies. No auth required. | all prior seasons in the chain |
| `scrape_league.py` | **ESPN fresh setup (start here).** Current-season settings + teams/owners, so `build_tool_data.py` can configure the console for your league. | 1 season, 2 views |
| `scrape.py` | Deep history for calibrating opponent tendencies (many seasons of drafts, transactions, matchups). Only needed for the optional `analysis/` pipeline. | all seasons, all views |
| `scrape_playercards.py` | Executed-trade detail (playercards), for the deep analysis only. | all seasons |
| `extract_har.py` | Recover API JSON bodies from a browser HAR capture (fallback when the API is unreachable). | — |

For a normal bring-your-own-league setup you only need one of the two fresh-setup
scrapers — `scrape_sleeper.py` (Sleeper) or `scrape_league.py` (ESPN).

## Sleeper

No auth. Sleeper's read API is public, so you only need the league id — the number in
`https://sleeper.com/leagues/<ID>/team`:

```json
{ "sleeper_league_id": "1234567890", "season": 2026, "sleeper_username": "yourname" }
```

```bash
python3 scraping/scrape_sleeper.py    # → scraping/raw/{season}/league_full.json
python3 pipeline.py build inject
```

It translates Sleeper's payload into ESPN's shape: `roster_positions` (an array) becomes
ESPN `lineupSlotCounts` (a map), every flex flavour collapses to ESPN's RB/WR/TE flex
slot, K/DEF/IDP/taxi slots are dropped (the console drafts skill positions only), and
`scoring_settings` maps onto the nine ESPN `statId`s `build_tool_data.py` scores. The
auction budget is read from the draft object, falling back to the league object, then to
$200 — the run prints which source it used, and warns on anything it had to guess.

**Opponent calibration.** Sleeper opponents can be calibrated from past auctions. Run the
history scraper after `scrape_sleeper.py`, then calibrate:

```bash
python3 scraping/scrape_sleeper_history.py    # → scraping/raw/{season}/league_full.json, players.json
python3 pipeline.py calibrate build inject
```

It follows `previous_league_id` back through the seasons. It reads each bid from
`pick.metadata.amount`, which Sleeper does not document. Calibration skips a season that
has no scrape.

**Limit.** Sleeper leagues have one gap:

- **SUPER_FLEX is approximated** as a standard RB/WR/TE flex — the console's
  replacement-level model has no QB-eligible flex slot. The run warns when it sees one.

## Configure your ESPN league

Set your league in `config/league.json` (copy `config/league.example.json`):

```json
{ "league_id": 123456, "season": 2026, "me": "Your Name" }
```

`league_id` is the number in your league's `fantasy.espn.com/.../leagues/<ID>` URL.
`me` must match the owner display name ESPN returns for your team.

## Authenticate

The API needs two cookies from a logged-in `fantasy.espn.com` session. Get them from
DevTools → Application → Cookies:

- `SWID` — looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`
- `espn_s2` — a long URL-encoded string

Provide them either way:

- **env vars:** `export ESPN_SWID=...` and `export ESPN_S2=...` (e.g. via `config/.env`), or
- **file:** `scraping/.espn_auth.json` → `{"SWID": "{...}", "espn_s2": "..."}` (gitignored).

These cookies expire every so often; re-grab them if you start getting `401`s.

## Run

```bash
set -a && . config/.env && set +a        # if you put cookies in config/.env
python3 scraping/scrape_league.py         # → scraping/raw/{season}/league_full.json
python3 draft_sheets/build_tool_data.py   # → draft_sheets/tool_data.json
```

`build_tool_data.py` reads `league_full.json` if present; otherwise it falls back to the
projection workbook's default roster/budget and generic opponents, so the console still
runs before you've scraped.

## ESPN API reference

- Modern read base (season ≥ 2018):
  `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league_id}`
- Historical (≤ 2017):
  `.../apis/v3/games/ffl/leagueHistory/{league_id}?seasonId={year}` (returns a single-element list)
- Useful `view=` params: `mSettings mTeam mRoster mDraftDetail mMatchup mStandings mStatus mTransactions2`
- `scrape_league.py` only needs `mSettings` (roster/scoring/budget) + `mTeam` (teams + owners).
- Transactions are returned **per `scoringPeriodId`** — iterate weeks 1–18 and dedupe by
  transaction `id`. Executed trades are not fully detailed in `mTransactions2`; the complete
  source is each player's `kona_playercard` `transactions` array (see `scrape_playercards.py`).

## Directory layout

```
scraping/
  scrape_sleeper.py     # Sleeper fresh-setup scraper (settings + managers, one season)
  scrape_sleeper_history.py # Sleeper past seasons (priced picks) for calibration
  scrape_league.py      # ESPN fresh-setup scraper (settings + teams, one season)
  scrape.py             # full historical scraper (all seasons, all views)
  scrape_playercards.py # executed-trade detail for the deep analysis
  extract_har.py        # recover JSON bodies from a browser HAR
  .espn_auth.json       # SWID + espn_s2 cookies (GITIGNORED — never commit)
  raw/{season}/
    league_full.json    # settings, teams, members (+ draft/rosters/etc. from scrape.py)
    players.json         # kona_player_info player universe (scrape.py, 2018+)
    transactions.json    # adds/drops/waivers/trade offers (scrape.py, 2018+)
```
