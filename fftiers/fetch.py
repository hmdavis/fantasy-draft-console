"""FantasyPros consensus-rankings download + cache (port of fp_api.py)."""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_URL = (
    "https://api.fantasypros.com/public/v2/json/nfl/{year}/consensus-rankings"
    "?position={position}&week={week}&scoring={scoring}"
)
# Rest-of-season ranks are a different ranking TYPE, not a week — and the API is
# doubly unusable for them: it clamps out-of-range weeks to the current week
# (week=90 returns weekly ranks), and `type=ros` truncates the players array to 10
# while reporting the full count. The public ranking PAGES carry the complete pool
# (verified: RB 105, WR 141, QB 61) as an embedded `var ecrData = {...}` blob with
# the same field names, so ROS is fetched from the page — no API key involved.
# Locally the ROS caches keep living under the week-90 sentinel filename.
#
# Since 2026-09-18 the API key is served as limited for WEEKLY ranks too:
# responses carry `"public_api_limited": true, "tier": "free"` and truncate
# `players` to 10 while still reporting the full `count`. The weekly ranking
# pages carry the same full-pool ecrData blob (verified 2026-09-22: RB-HALF 78
# players / QB 34, `ranking_type_name` "weekly", `week` matching the current
# week), so a truncated API response falls back to the page. The pages only
# serve the CURRENT week — a request for any other week fails rather than
# silently caching the wrong week, and a truncated pool is never written.
ROS_WEEK = 90
ROS_PAGE = "https://www.fantasypros.com/nfl/rankings/ros-{variant}{pos}.php"
WEEK_PAGE = "https://www.fantasypros.com/nfl/rankings/{variant}{pos}.php"
PAGE_VARIANT = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}
PAGE_POS = {"FLX": "flex"}              # page filenames that aren't just lowercase
SCORING_FREE_POS = {"QB", "K", "DST"}   # reception scoring doesn't move these pages
ECR_RE = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\});", re.S)
# An honest full pool is never this small; a truncation cap is (the API caps at 10).
TRUNCATION_FLOOR = 25
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


@dataclass
class PlayerRow:
    rank: int
    name: str
    detail: str        # position(s) pre-draft, opponent in-season
    best: float
    worst: float
    avg: float
    std: float


class MissingApiKeyError(RuntimeError):
    pass


def resolve_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("FANTASYPROS_API_KEY")
    if env:
        return env
    key_file = Path(__file__).resolve().parents[1] / "api_key.txt"
    if key_file.exists():
        return key_file.read_text().strip()
    raise MissingApiKeyError(
        "No FantasyPros API key. Pass --api-key, set FANTASYPROS_API_KEY, "
        "or put the key in api_key.txt at the repo root."
    )


def optional_api_key(explicit: str | None = None) -> str | None:
    try:
        return resolve_api_key(explicit)
    except MissingApiKeyError:
        return None


def cache_path(data_dir: Path, year: int, week: int, position: str, scoring: str) -> Path:
    return data_dir / str(year) / f"week-{week}-{position}-{scoring}.json"


def _truncated(data: dict) -> bool:
    """True when a rankings payload is a limited-tier stub, not a full pool."""
    if data.get("public_api_limited"):
        return True
    count = data.get("count")
    players = data.get("players") or []
    if isinstance(count, int):
        return len(players) < min(count, TRUNCATION_FLOOR)
    # No count reported: trust nothing small — an honest pool clears the floor.
    return len(players) < TRUNCATION_FLOOR


def download(data_dir: Path, year: int, week: int, position: str, scoring: str,
             api_key: str | None) -> Path:
    dest = cache_path(data_dir, year, week, position, scoring)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if week == ROS_WEEK:
        return _download_ros_page(dest, position, scoring)
    if not api_key:
        return _download_week_page(dest, week, position, scoring, "no FantasyPros API key")
    url = API_URL.format(year=year, position=position, week=week, scoring=scoring)
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        # Revoked key / hard block / timeout: the public page may still serve the week.
        code = getattr(e, "code", None)
        return _download_week_page(dest, week, position, scoring,
                                   f"API request failed ({code or e})")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and not _truncated(data):
        dest.write_bytes(raw)   # full API response — cache as-is, like always
        return dest
    if not isinstance(data, dict):
        why = "API response was not a JSON object"
    else:
        why = (f"API served {len(data.get('players') or [])}/{data.get('count')} "
               f"players (tier={data.get('tier')!r}, public_api_limited="
               f"{data.get('public_api_limited')!r})")
    return _download_week_page(dest, week, position, scoring, why)


def _fetch_ecr_blob(url: str) -> dict:
    """A public ranking page's embedded `var ecrData = {...}` blob."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = ECR_RE.search(html)
    if not m:
        raise RuntimeError(f"no `var ecrData` blob in {url} — page layout changed")
    try:
        blob = json.loads(m.group(1))
    except ValueError as e:
        raise RuntimeError(f"unparseable ecrData blob in {url}: {e}") from e
    if not isinstance(blob, dict):
        raise RuntimeError(f"ecrData in {url} is not an object")
    return blob


def _page_url(template: str, position: str, scoring: str) -> str:
    variant = "" if position in SCORING_FREE_POS else PAGE_VARIANT.get(scoring, "")
    return template.format(variant=variant,
                           pos=PAGE_POS.get(position, position.lower()))


def _download_ros_page(dest: Path, position: str, scoring: str) -> Path:
    """Full-pool ROS ranks from the public ranking page's ecrData blob."""
    src = _fetch_ecr_blob(_page_url(ROS_PAGE, position, scoring))
    players = [{
        "rank_ecr": p.get("rank_ecr"),
        "player_name": p.get("player_name"),
        "player_positions": position,
        "rank_min": p.get("rank_min", p.get("rank_ave")),
        "rank_max": p.get("rank_max", p.get("rank_ave")),
        "rank_ave": p.get("rank_ave"),
        "rank_std": p.get("rank_std"),
    } for p in src.get("players", [])]
    dest.write_text(json.dumps({
        "type": src.get("type") or f"ROS {scoring}", "week": ROS_WEEK,
        "ranking_type_name": "ros", "position": position, "scoring": scoring,
        "count": len(players), "players": players}))
    return dest


def _download_week_page(dest: Path, week: int, position: str, scoring: str,
                        api_why: str) -> Path:
    """Full-pool WEEKLY ranks from the public ranking page, replacing a
    truncated API response. The pages serve only the current week, so the
    blob's week must match the request — otherwise the prior cache is kept."""
    url = _page_url(WEEK_PAGE, position, scoring)
    combo = f"{position}-{scoring} week {week}"
    try:
        src = _fetch_ecr_blob(url)
    except Exception as e:
        raise RuntimeError(
            f"{combo}: {api_why}; weekly-page fallback {url} failed ({e}) — "
            f"prior cache kept") from e
    try:
        page_week = int(src.get("week"))
    except (TypeError, ValueError):
        page_week = None
    if page_week != week:
        raise RuntimeError(
            f"{combo}: {api_why}; the page {url} serves week {src.get('week')!r}, "
            f"not week {week} (pages only carry the current week) — prior cache kept")
    players = [{
        "rank_ecr": p.get("rank_ecr"),
        "player_name": p.get("player_name"),
        "player_positions": position,
        "player_opponent": p.get("player_opponent"),   # load_rows' in-season detail
        "rank_min": p.get("rank_min", p.get("rank_ave")),
        "rank_max": p.get("rank_max", p.get("rank_ave")),
        "rank_ave": p.get("rank_ave"),
        "rank_std": p.get("rank_std"),
    } for p in src.get("players", [])]
    count = src.get("count")
    if len(players) < min(count if isinstance(count, int) else TRUNCATION_FLOOR,
                          TRUNCATION_FLOOR):
        raise RuntimeError(
            f"{combo}: {api_why}; the page {url} also served a truncated pool "
            f"({len(players)}/{count}) — prior cache kept")
    dest.write_text(json.dumps({
        "type": src.get("type") or f"Weekly {scoring}", "week": week,
        "ranking_type_name": src.get("ranking_type_name") or "weekly",
        "position": position, "scoring": scoring,
        "count": len(players), "players": players}))
    return dest


def load_rows(json_path: Path) -> list[PlayerRow]:
    data = json.loads(json_path.read_text())
    rows = []
    for p in data.get("players", []):
        try:
            rows.append(PlayerRow(
                rank=int(p["rank_ecr"]),
                name=p["player_name"],
                detail=str(p.get("player_opponent") or p.get("player_positions") or ""),
                best=float(p["rank_min"]),
                worst=float(p["rank_max"]),
                avg=float(p["rank_ave"]),
                std=float(p["rank_std"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # upstream skipped malformed player entries the same way
    rows.sort(key=lambda r: r.rank)
    return rows


def get_rankings(data_dir: Path, year: int, week: int, position: str, scoring: str,
                 refresh: bool, api_key: str | None) -> list[PlayerRow]:
    path = cache_path(data_dir, year, week, position, scoring)
    if refresh or not path.exists():
        download(data_dir, year, week, position, scoring, optional_api_key(api_key))
    return load_rows(path)
