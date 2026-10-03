#!/usr/bin/env python3
"""Fantasy console — FastAPI backend with two modes.

/draft is the live auction console (pre-season); /manage is the fftiers board
(in-season); / redirects to DEFAULT_MODE (env, default "manage"). Also serves
a thin LLM advisor (/api/advise) powered by
Claude Haiku 4.5 (fast, for live-draft latency). The advisor's system prompt is
a strategy briefing distilled from the league analysis, so it predicts opponent
behavior with full context; the frontend posts the live draft state each call.

Set CONSOLE_PASSWORD to put HTTP Basic in front of everything but /healthz — required
when deploying publicly, since /api/advise spends real Anthropic credit per call.
"""
import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DEFAULT_MODEL = "claude-haiku-4-5"  # fast, for live-draft latency
# models the advisor dropdown may select (allowlist — anything else falls back to default)
ALLOWED_MODELS = {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}


def _load_dotenv():
    """Populate os.environ from config/.env when present.

    The advisor reads ANTHROPIC_API_KEY from the environment and the README tells you to
    put it in config/.env — but nothing ever loaded that file, so the documented setup
    only worked if you separately sourced it into your shell (`set -a && . config/.env`).
    That step is POSIX-only, so on Windows the key sat in the file and the advisor stayed
    silently disabled. Parsed here instead: KEY=VALUE, `#` comments, optional surrounding
    quotes. Never overrides a variable already set for real — an explicit export, a
    Railway config var, or a systemd Environment= still wins.
    """
    path = os.environ.get("DOTENV_PATH",
                          os.path.join(HERE, os.pardir, "config", ".env"))
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) > 1 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    os.environ.setdefault(key, val)
    except OSError:
        pass


_load_dotenv()

def _load_briefing():
    """The advisor's system prompt. Kept in the local (gitignored) config/ directory so
    league-specific content (opponent names, your plan, your league's tendencies) stays
    out of the public repo. Override with STRATEGY_BRIEFING_PATH. Falls back to a generic,
    still-grounded briefing if none is present. See config/briefing.example.md."""
    path = os.environ.get(
        "STRATEGY_BRIEFING_PATH",
        os.path.join(HERE, os.pardir, "config", "briefing.md"),
    )
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    except OSError:
        pass
    return (
        "You are a draft-night strategist for an auction fantasy football league. Be concise, "
        "concrete, and decisive. Use ONLY the players, budgets, needs, and rosters in the provided "
        "live state — never invent players; TARGET must be a name in `best_available`. Size a bid "
        "off the player's `worth`/`est_price`, not the user's budget (budget is a ceiling, not a "
        "target). Check `teams[me].needs` before recommending a position. Copy "
        "config/briefing.example.md to config/briefing.md and customize it with your league's "
        "tendencies to make the advisor sharp."
    )


STRATEGY_BRIEFING = _load_briefing()


app = FastAPI(title="Live Auction Draft Console")

# ── Access gate ──────────────────────────────────────────────────────────────
# Two parallel mechanisms, either grants access:
#   1. Supabase email login (browsers): /login sends an email OTP; /api/session turns
#      the resulting access token into an httpOnly session cookie. The server checks
#      the token against Supabase Auth AND requires the email to be in ALLOWED_EMAILS
#      — signups are open (future multi-tenant), the allowlist is the launch gate.
#   2. HTTP Basic CONSOLE_PASSWORD (curl/scripts/fallback), as before.
# Neither configured => open, so local development stays frictionless.
CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "").strip()
CONSOLE_USER = os.environ.get("CONSOLE_USER", "draft").strip() or "draft"
ALLOWED_EMAILS = {e.strip().lower()
                  for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
SESSION_COOKIE = "fc_session"
OPEN_PATHS = {"/healthz", "/login", "/api/session", "/favicon.ico"}
_token_cache: dict = {}   # access_token -> (email, checked_at)


def _authorized(header: str) -> bool:
    """Constant-time check of an HTTP Basic header against the configured password."""
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:], validate=True).decode().partition(":")
    except Exception:
        return False
    # compare both halves in constant time so neither is a timing oracle
    return (secrets.compare_digest(user, CONSOLE_USER)
            and secrets.compare_digest(pw, CONSOLE_PASSWORD))


def _email_login_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_API_KEY and ALLOWED_EMAILS)


def _supabase_email(token: str) -> str | None:
    """The verified email behind a Supabase access token (5-min cache), else None."""
    if not token:
        return None
    hit = _token_cache.get(token)
    now = time.time()
    if hit and now - hit[1] < 300:
        return hit[0]
    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={"apikey": SUPABASE_API_KEY, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            email = (json.load(r).get("email") or "").strip().lower()
    except Exception:
        return None
    if email:
        if len(_token_cache) > 200:
            _token_cache.clear()
        _token_cache[token] = (email, now)
    return email or None


def _session_ok(request: Request) -> bool:
    if not _email_login_enabled():
        return False
    email = _supabase_email(request.cookies.get(SESSION_COOKIE, ""))
    return bool(email and email in ALLOWED_EMAILS)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    gated = CONSOLE_PASSWORD or _email_login_enabled()
    if gated and request.url.path not in OPEN_PATHS:
        basic_ok = CONSOLE_PASSWORD and _authorized(request.headers.get("authorization", ""))
        if not basic_ok and not _session_ok(request):
            wants_html = ("text/html" in request.headers.get("accept", "")
                          and request.method == "GET")
            if wants_html and _email_login_enabled():
                return RedirectResponse(f"/login?next={request.url.path}", status_code=307)
            return Response(
                "Authentication required.", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Draft console", charset="UTF-8"'}
                        if CONSOLE_PASSWORD else {},
            )
    return await call_next(request)


@app.get("/login")
async def login_page():
    if not _email_login_enabled():
        return Response("Email login is not configured on this server.\n",
                        status_code=404, media_type="text/plain")
    page = open(os.path.join(HERE, "login.html"), encoding="utf-8").read()
    page = page.replace("{{SUPABASE_URL}}", SUPABASE_URL)
    page = page.replace("{{SUPABASE_API_KEY}}", SUPABASE_API_KEY)
    return Response(page, media_type="text/html")


@app.post("/api/session")
async def create_session(req: Request):
    """Turn a Supabase access token into the httpOnly session cookie (allowlist-gated)."""
    if not _email_login_enabled():
        return JSONResponse({"error": "email login not configured"}, status_code=404)
    try:
        token = ((await req.json()).get("access_token") or "").strip()
    except Exception:
        token = ""
    email = _supabase_email(token)
    if not email:
        return JSONResponse({"error": "invalid or expired session"}, status_code=401)
    if email not in ALLOWED_EMAILS:
        return JSONResponse({"error": f"{email} isn't authorized for this console yet"},
                            status_code=403)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(SESSION_COOKIE, token, max_age=7 * 86400, httponly=True,
                    samesite="lax",
                    secure=req.headers.get("x-forwarded-proto", req.url.scheme) == "https")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=307)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/advise")
async def advise(req: Request):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set on server"}, status_code=503)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    question = (body.get("question") or "").strip()
    state = body.get("state") or {}
    model = body.get("model") if body.get("model") in ALLOWED_MODELS else DEFAULT_MODEL
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    client = anthropic.Anthropic(api_key=key)
    user_msg = (
        "Live draft state (JSON):\n" + json.dumps(state, separators=(",", ":")) +
        "\n\nQuestion: " + question
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2048,  # ceiling only — model stops at natural end, so no latency cost for short answers
            system=STRATEGY_BRIEFING,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as e:
        return JSONResponse({"error": f"model error {e.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)

    answer = "".join(b.text for b in resp.content if b.type == "text").strip()
    return {"answer": answer, "model": resp.model, "truncated": resp.stop_reason == "max_tokens"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "advisor": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "gated": bool(CONSOLE_PASSWORD) or _email_login_enabled(),
            "email_login": _email_login_enabled(), "board_store": bool(SUPABASE_URL)}


# ── Board data: latest snapshot, Supabase first ──────────────────────────────
# The manage page boots from the data baked into board.html, then fetches this for
# anything newer. The pipeline's `board` stage pushes each build to the
# board_snapshots table, so a local `pipeline.py week` refreshes the deployed site
# without a redeploy. Unconfigured (local dev), it falls back to the build output
# on disk; the page silently keeps its baked data if the endpoint has nothing.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_API_KEY = os.environ.get("SUPABASE_API_KEY", "").strip()
BOARD_DB_SECRET = os.environ.get("BOARD_DB_SECRET", "").strip()
VIZ_LOCAL = os.path.join(HERE, os.pardir, "out", "board", "viz-data.json")
BOARD_CACHE_TTL = 60  # seconds; the page re-fetches per load, the store moves weekly
_board_cache = {"at": 0.0, "body": None}


def _latest_snapshot():
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/get_board_snapshot",
        data=json.dumps({"secret": BOARD_DB_SECRET}).encode(),
        headers={"apikey": SUPABASE_API_KEY,
                 "Authorization": f"Bearer {SUPABASE_API_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


@app.get("/api/board-data")
def board_data():  # sync on purpose: FastAPI runs it in the threadpool
    now = time.time()
    if _board_cache["body"] is not None and now - _board_cache["at"] < BOARD_CACHE_TTL:
        return JSONResponse(_board_cache["body"])
    if SUPABASE_URL and SUPABASE_API_KEY and BOARD_DB_SECRET:
        try:
            snap = _latest_snapshot()
            if snap and snap.get("data"):
                body = {"source": "supabase", "built_at": snap.get("built_at"),
                        "data": snap["data"]}
                _board_cache.update(at=now, body=body)
                return JSONResponse(body)
        except Exception:
            pass  # store down or empty -> fall back to the local build
    try:
        with open(VIZ_LOCAL, encoding="utf-8") as f:
            viz = json.load(f)
        body = {"source": "file", "built_at": (viz.get("asof") or {}).get("built"),
                "data": viz}
        _board_cache.update(at=now, body=body)
        return JSONResponse(body)
    except (OSError, ValueError):
        return JSONResponse(
            {"error": "no board data — run `python3 pipeline.py week`"}, status_code=404)


# ── Surface: two modes ───────────────────────────────────────────────────────
# Everything served here is GENERATED (pipeline.py `inject` for the draft console,
# `board` for the manage board). Nothing under static/ is hand-written, and this
# module never builds a payload.
#
#   /         redirect to the default mode (DEFAULT_MODE env: manage|draft)
#   /draft    auction console (pre-season)    static/index.html
#   /manage   fftiers board (in-season)       static/board.html
DEFAULT_MODE = os.environ.get("DEFAULT_MODE", "manage").strip().lower()
if DEFAULT_MODE not in ("manage", "draft"):
    DEFAULT_MODE = "manage"


def _page(path: str, what: str, hint: str):
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return Response(f"{what} not built yet. Generate it with:\n\n    {hint}\n",
                    status_code=404, media_type="text/plain")


@app.get("/")
async def root():
    return RedirectResponse(f"/{DEFAULT_MODE}", status_code=307)


@app.get("/draft")
async def draft_console():
    return _page(os.path.join(STATIC_DIR, "index.html"),
                 "Draft console", "python3 pipeline.py build inject")


@app.get("/manage")
async def manage_board():
    return _page(os.path.join(STATIC_DIR, "board.html"),
                 "Manage board", "python3 pipeline.py week")


# ── The Wire: curated reporter feed via Bluesky's public API ─────────────────
# Sources live in fftiers/data/bsky_reporters.json (tracked, user-editable).
# No key: public.api.bsky.app serves public author feeds anonymously. Fetched
# fan-out on demand, merged newest-first, cached in-process; /api/feed paginates
# the merged list by offset so the page's "Load more" is a slice, not a refetch.
REPORTERS_FILE = next(
    (p for p in (os.path.join(HERE, "fftiers", "data", "bsky_reporters.json"),
                 os.path.join(HERE, os.pardir, "fftiers", "data", "bsky_reporters.json"))
     if os.path.exists(p)), None)
BSKY_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
FEED_TTL = 300          # seconds; reporters don't out-post a 5-minute cache
FEED_PER_AUTHOR = 20
_feed_cache = {"at": 0.0, "posts": [], "sources": 0, "errors": 0}
_feed_lock = threading.Lock()


def _fetch_author_posts(account: dict) -> list[dict]:
    import urllib.parse
    url = BSKY_FEED + "?" + urllib.parse.urlencode(
        {"actor": account["handle"], "limit": FEED_PER_AUTHOR,
         "filter": "posts_no_replies"})
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-console/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        feed = json.load(r).get("feed") or []
    posts = []
    for item in feed:
        if item.get("reason"):
            continue   # repost of someone else — skip; keeps the feed original-voice
        post = item.get("post") or {}
        rec = post.get("record") or {}
        text = (rec.get("text") or "").strip()
        if not text:
            continue
        rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
        posts.append({"name": account["name"], "handle": account["handle"],
                      "text": text, "at": rec.get("createdAt") or "",
                      "url": f"https://bsky.app/profile/{account['handle']}/post/{rkey}"})
    return posts


def _load_wire():
    from concurrent.futures import ThreadPoolExecutor
    accounts = json.load(open(REPORTERS_FILE, encoding="utf-8"))["accounts"]
    posts, errors = [], 0

    def one(acct):
        try:
            return _fetch_author_posts(acct)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(one, accounts):
            if res is None:
                errors += 1
            else:
                posts.extend(res)
    posts.sort(key=lambda p: p["at"], reverse=True)
    return posts, len(accounts), errors


@app.get("/api/feed")
def wire_feed(offset: int = 0, limit: int = 25):  # sync: runs in the threadpool
    if REPORTERS_FILE is None:
        return JSONResponse({"error": "no reporter list in this deploy"}, status_code=404)
    offset, limit = max(0, offset), min(max(1, limit), 100)
    now = time.time()
    with _feed_lock:
        if not _feed_cache["posts"] or now - _feed_cache["at"] > FEED_TTL:
            try:
                posts, nsrc, errors = _load_wire()
                _feed_cache.update(at=now, posts=posts, sources=nsrc, errors=errors)
            except Exception as e:
                if not _feed_cache["posts"]:
                    return JSONResponse({"error": f"feed unavailable: {e}"}, status_code=502)
                # stale cache beats an error page
        posts = _feed_cache["posts"]
        return JSONResponse({
            "total": len(posts), "offset": offset,
            "sources": _feed_cache["sources"], "source_errors": _feed_cache["errors"],
            "fetched_at": _feed_cache["at"],
            "posts": posts[offset:offset + limit]})


# ── Refresh from the website ─────────────────────────────────────────────────
# POST /api/refresh runs the manage pipeline (pull → vbd/csg boards → build+push)
# in a background thread; the page polls /api/refresh/status and hot-swaps the new
# data when it lands. `tiers` (PNG charts) is deliberately excluded: it needs
# matplotlib, which the deployed image doesn't carry. Locally pipeline.py sits one
# level up from this file; in the Railway bundle it sits alongside it. Requires the
# same secrets the pipeline needs (ESPN_SWID/ESPN_S2, FANTASYPROS_API_KEY,
# SUPABASE_* — env vars on Railway). Behind the same Basic auth as everything else.
ROOT_DIR = os.path.abspath(os.path.join(HERE, os.pardir))
PIPELINE = next((p for p in (os.path.join(HERE, "pipeline.py"),
                             os.path.join(ROOT_DIR, "pipeline.py"))
                 if os.path.exists(p)), None)
# full = everything (ESPN pools + ROS sums + FantasyPros ranks), ~1-2 min.
# sync = fast ESPN state only (rosters/lineups/meta/weekly pts; the pipeline's
#        `sync` stage chains the offline board rebuild itself), ~15 s.
REFRESH_MODES = {"full": ("pull", "vbd-boards", "csg-boards", "board"),
                 "sync": ("sync",)}
_refresh = {"running": False, "mode": None, "started": None, "finished": None,
            "ok": None, "log": ""}
_refresh_lock = threading.Lock()


def _run_refresh(mode: str):
    try:
        p = subprocess.run([sys.executable, PIPELINE, *REFRESH_MODES[mode]],
                           cwd=os.path.dirname(PIPELINE),
                           capture_output=True, text=True, timeout=1800)
        ok, log = p.returncode == 0, (p.stdout + "\n" + p.stderr)[-4000:]
    except Exception as e:  # timeout, spawn failure — report, never crash the server
        ok, log = False, str(e)[-4000:]
    if ok:
        _board_cache.update(at=0.0, body=None)  # next /api/board-data is the new build
    _refresh.update(running=False, finished=time.time(), ok=ok, log=log)


@app.post("/api/refresh")
async def refresh_start(req: Request):
    if PIPELINE is None:
        return JSONResponse({"error": "refresh unavailable: pipeline not in this deploy"},
                            status_code=501)
    try:
        body = await req.json()
    except Exception:
        body = {}   # no/garbled body -> the default mode; a PRESENT bad mode is a 400
    mode = body.get("mode", "full") if isinstance(body, dict) else None
    if not isinstance(mode, str) or mode.strip().lower() not in REFRESH_MODES:
        return JSONResponse({"error": f"unknown mode {mode!r}"}, status_code=400)
    mode = mode.strip().lower()
    with _refresh_lock:
        if _refresh["running"]:
            return JSONResponse({"status": "already-running", "mode": _refresh["mode"],
                                 "started": _refresh["started"]}, status_code=409)
        _refresh.update(running=True, mode=mode, started=time.time(),
                        finished=None, ok=None, log="")
    threading.Thread(target=_run_refresh, args=(mode,), daemon=True).start()
    return {"status": "started", "mode": mode}


@app.get("/api/refresh/status")
async def refresh_status():
    return dict(_refresh)


# Static assets (mounted last so the routes above and /api/* take precedence)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
