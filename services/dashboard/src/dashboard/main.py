"""The app's backend: a JSON API and a live event stream for the web UI (web/).

    browser ──► web (Next.js, :8700) ──/api/*──► this service ──► broker, market-data,
                                                    │              orchestrator, advisor
                                                    └─ /api/events (SSE): quotes, briefs,
                                                       portfolio changes, pushed live

Two identities meet here. The *service* proves itself to every backend with its key
(identity "dashboard": the only holder of broker:decide and broker:trade); the
*human* proves themself to this service by signing in (auth.py: users in Postgres,
a 15-minute JWT, a rotating refresh token). The broker records both
("dashboard:<account>"), so the audit log says which person acted.

Many users, one desk. Each user has their own demo account, watchlist and alerts; every
call to the broker names the signed-in user's account, taken from their token, never
from the request. Research is shared: a brief on a stock is the same for everyone.

Every state-changing request must also carry the header `X-Desk-Request: 1`, which a
cross-site form cannot send: CSRF protection without tokens in forms.

Demo account only: test money, real PSX prices. Nothing here reaches a brokerage.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from agentkit.a2a import AgentDirectory
from agentkit.auth import MAX_TTL_S, bearer_for, default_identity
from agentkit.mcp_tools import McpToolError, McpToolset
from agentkit.tracing import setup_tracing
from contracts.events import BRIEFS
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from dashboard.auth import (
    ACCESS_S,
    EMAIL_RE,
    MIN_PASSWORD,
    REFRESH_S,
    EmailTaken,
    RefreshRaced,
    User,
    UserStore,
    issue_access,
    read_access,
)

log = logging.getLogger("dashboard")

BROKER = os.getenv("BROKER_MCP_URL", "http://127.0.0.1:8600/mcp")
MARKET_DATA = os.getenv("MARKET_DATA_MCP_URL", "http://127.0.0.1:8300/mcp")
ORCHESTRATOR = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8200")
GATEWAY = os.getenv("LLM_GATEWAY_URL", "http://127.0.0.1:8100")
ADVISOR = os.getenv("ADVISOR_URL", "http://127.0.0.1:8406")
# A new user's starting watchlist. After that, each user's list lives in the broker.
DEFAULT_WATCHLIST = [s.strip().upper() for s in os.getenv("WATCHLIST", "LUCK,ENGRO,HBL").split(",")
                     if s.strip()]
OPERATOR_EMAIL = os.getenv("OPERATOR_EMAIL", "operator@psx-desk.local").lower()
OPERATOR = "operator"  # the account that existed before sign-up, and its demo money
SIGNUP_ENABLED = os.getenv("SIGNUP_ENABLED", "1") == "1"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
ACCESS_COOKIE, REFRESH_COOKIE = "desk_access", "desk_refresh"
PKT = ZoneInfo("Asia/Karachi")
INITIAL_CAPITAL = 1_000_000.0
BROKER_TOOLS = {"get_portfolio", "list_proposals", "get_proposal", "list_fills", "decide",
                "place_order", "audit_log", "verify_audit", "get_watchlist", "init_watchlist",
                "watch", "unwatch", "list_alerts", "list_alert_events", "create_alert",
                "cancel_alert", "open_account", "activity"}


# ---------------------------------------------------------------- sessions


def _secret() -> str:
    s = os.getenv("DASHBOARD_SESSION_SECRET", "")
    if len(s) < 32:
        raise RuntimeError("DASHBOARD_SESSION_SECRET must be set (32+ characters)")
    return s


def current_user(request: Request) -> User:
    """The signed-in user, from their access token. 401 when it is missing or expired:
    the browser then refreshes it (POST /api/auth/refresh) and retries."""
    user = read_access(_secret(), request.cookies.get(ACCESS_COOKIE))
    if user is None:
        raise HTTPException(401, "not signed in")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-desk-request") != "1":
        raise HTTPException(403, "missing X-Desk-Request header")
    return user


Me = Annotated[User, Depends(current_user)]  # the signed-in user, on any endpoint


class Limiter:
    """Fixed-window counters in Redis (in memory when there is none, as in tests)."""

    def __init__(self) -> None:
        self.redis: Redis | None = None
        self.mem: dict[str, tuple[float, int]] = {}

    async def hit(self, key: str, limit: int, window_s: int) -> bool:
        """Count one attempt; False once `limit` is passed within the window."""
        if self.redis is not None:
            n = await self.redis.incr(f"rl:{key}")
            if n == 1:
                await self.redis.expire(f"rl:{key}", window_s)
            return n <= limit
        start, n = self.mem.get(key, (time.time(), 0))
        if time.time() - start > window_s:
            start, n = time.time(), 0
        self.mem[key] = (start, n + 1)
        return n + 1 <= limit


limiter = Limiter()


# ---------------------------------------------------------------- backends


async def _call(url: str, tool: str, args: dict):
    # A tool error raised inside the client's context comes out wrapped in an
    # ExceptionGroup by its task group; keep it and raise it plainly after closing.
    failed: McpToolError | None = None
    async with McpToolset(url, allow={tool}) as ts:
        try:
            return await ts.call(tool, **args)
        except McpToolError as e:
            # show the server's reason, not the protocol's "Error executing tool x:"
            failed = McpToolError(str(e).removeprefix(f"Error executing tool {tool}: "))
    raise failed


def user_grant(account: str, party: str = "dashboard", ttl_s: int = 60) -> str | None:
    """The signed-in user's grant (agentkit.auth): proof for the broker that this
    account's owner is signed in, usable only by `party`. The dashboard is the one
    service that checks passwords, so it is the one that issues these. None when this
    process has no signing key (tests, local runs without auth)."""
    me = default_identity()
    return me.mint_grant(account, "broker", party, ttl_s) if me else None


async def broker(tool: str, **args):
    # Every per-account call carries the user's grant; the broker refuses it otherwise.
    if "account" in args and (g := user_grant(args["account"])):
        args["grant"] = g
    return await _call(BROKER, tool, args)


async def market(tool: str, **args):
    return await _call(MARKET_DATA, tool, args)


async def orch(method: str, path: str, **kw) -> dict:
    async with httpx.AsyncClient(base_url=ORCHESTRATOR, timeout=20, auth=bearer_for()) as c:
        r = await c.request(method, path, **kw)
    if r.status_code >= 400:
        raise HTTPException(502, f"orchestrator HTTP {r.status_code}")
    return r.json()


_history_cache: dict[tuple[str, int], tuple[float, dict]] = {}


def _loggable(text: str) -> str:
    """Strip line breaks, so a value cannot forge extra log lines."""
    return text.replace("\r", "").replace("\n", "")[:40]


async def history(symbol: str, days: int) -> dict | None:
    """Daily closes; cached 10 minutes (they change once a day)."""
    key = (symbol, days)
    hit = _history_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        h = await market("get_price_history", symbol=symbol, days=days)
    except Exception as e:  # noqa: BLE001
        log.warning("no history for %s: %s", _loggable(symbol), e)
        return None
    _history_cache[key] = (time.time(), h)
    return h


async def watchlist_of(account: str) -> list[str]:
    return await broker("get_watchlist", account=account)


async def provision(account: str) -> None:
    """A user's demo account and starting watchlist. Idempotent, so it runs at every
    sign-in and heals a sign-up whose broker call failed."""
    await broker("open_account", account=account)
    await broker("init_watchlist", account=account, symbols=DEFAULT_WATCHLIST)


def market_open(now: datetime | None = None) -> bool:
    t = (now or datetime.now(UTC)).astimezone(PKT)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (15, 30)


# ---------------------------------------------------------------- live hub


class Hub:
    """Fan-out of live events to every open browser tab (Server-Sent Events)."""

    def __init__(self) -> None:
        self.clients: dict[asyncio.Queue, str] = {}  # queue -> the signed-in account
        self.quotes: dict = {}
        self.board: dict = {}

    def publish(self, kind: str, data: dict, account: str | None = None) -> None:
        """To every open tab, or only to `account`'s tabs (an alert is one user's)."""
        msg = {"type": kind, "data": data, "at": datetime.now(UTC).isoformat()}
        for q, owner in list(self.clients.items()):
            if account is not None and owner != account:
                continue
            if q.qsize() < 100:
                q.put_nowait(msg)

    async def quotes_loop(self) -> None:
        """The whole PSX board (every traded stock) in one request, every 30 s while the
        market is open and every 5 minutes otherwise. Kept in memory, so search and the
        all-stocks list are served without touching PSX; prices go out to every tab."""
        while True:
            await self.refresh_board()
            await asyncio.sleep(30 if market_open() else 300)

    async def refresh_board(self) -> None:
        try:
            board = await market("get_board")
        except Exception as e:  # noqa: BLE001
            log.warning("board refresh failed: %s", e)
            return
        self.board = board
        self.quotes = {"fetched_at": board["fetched_at"], "market_open": market_open(),
                       "quotes": {s["symbol"]: {"price": s["price"],
                                                "change_pct": s["change_pct"],
                                                "volume_avg_30d": s["volume_avg_30d"]}
                                  for s in board["stocks"]}}
        self.publish("quotes", self.quotes)

    async def briefs_loop(self, redis: Redis) -> None:
        last = "$"
        while True:
            try:
                for _, entries in await redis.xread({BRIEFS: last}, block=10_000, count=20) or []:
                    for entry_id, fields in entries:
                        last = entry_id
                        ev = json.loads(fields["event"])
                        self.publish("brief", {"type": ev["type"], **ev["payload"]})
            except Exception as e:  # noqa: BLE001
                log.warning("briefs stream error: %s", e)
                await asyncio.sleep(5)

    async def broker_loop(self) -> None:
        """The broker publishes no events, so follow its audit log: one cheap read every
        4 s for all users, whatever their number. Each change is tagged with its account
        and goes only to that account's tabs: "portfolio" (proposals, fills) tells the
        screens to refresh, "alert" is a price-alert notification."""
        cursor: int | None = None
        while True:
            try:
                feed = await broker("activity", after_id=cursor)
                if cursor is not None:
                    self.route(feed["items"])
                cursor = feed["cursor"]
            except Exception as e:  # noqa: BLE001
                log.warning("broker watch failed: %s", e)
            await asyncio.sleep(4)

    def route(self, items: list[dict]) -> None:
        touched: set[str] = set()
        for it in items:
            acct, action, d = it.get("account"), it["action"], it["details"]
            if not acct:
                continue
            if action.startswith("alert."):
                self.publish("alert", {"kind": action.removeprefix("alert."),
                                       "id": d.get("event_id"), "account": acct,
                                       **{k: d.get(k) for k in ("symbol", "price", "drop_pct",
                                                                "loss_pkr")}}, account=acct)
            elif action.startswith(("proposal.", "order.", "account.")):
                touched.add(acct)
        for acct in touched:
            self.publish("portfolio", {}, account=acct)


hub = Hub()


async def warm_sparklines() -> None:
    """Keep the 30-day closes of the stocks people are looking at cached (the default
    list, plus every watchlist with a tab open), so Today never waits on a cold PSX fetch
    (about 13 s per symbol through the rate-limited data server)."""
    while True:
        symbols = dict.fromkeys(DEFAULT_WATCHLIST)
        for acct in sorted(set(hub.clients.values())):
            try:
                symbols |= dict.fromkeys(await watchlist_of(acct))
            except Exception as e:  # noqa: BLE001
                log.warning("watchlist unavailable: %s", e)
        for sym in symbols:
            _history_cache.pop((sym, 30), None)
            await history(sym, 30)
        await asyncio.sleep(540)  # just inside the 10-minute cache window


async def bootstrap(users: UserStore) -> None:
    """The operator user (password from .env) and their demo account, once the broker
    is up. Retries while it starts."""
    pw = os.getenv("DASHBOARD_PASSWORD", "")
    if pw:
        await users.ensure_operator(OPERATOR_EMAIL, pw, account=OPERATOR)
    for _ in range(30):
        try:
            await provision(OPERATOR)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("operator account setup failed, retrying: %s", e)
            await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _secret()
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                           decode_responses=True, socket_timeout=30)
    limiter.redis = redis
    app.state.users = await UserStore.connect(os.environ["DATABASE_URL"])
    app.state.advisor = AgentDirectory([ADVISOR], timeout_s=180)
    tasks = [asyncio.create_task(bootstrap(app.state.users)),
             asyncio.create_task(hub.quotes_loop()), asyncio.create_task(hub.briefs_loop(redis)),
             asyncio.create_task(hub.broker_loop()), asyncio.create_task(warm_sparklines())]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis.aclose()
    await app.state.advisor.aclose()
    await app.state.users.close()


app = FastAPI(title="desk-app-api", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------------------------------- auth endpoints


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class SignUp(Credentials):
    name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


def _csrf(request: Request) -> None:
    if request.headers.get("x-desk-request") != "1":
        raise HTTPException(403, "missing X-Desk-Request header")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _signed_in(user: User, refresh: str) -> Response:
    r = JSONResponse(_profile(user))
    r.set_cookie(ACCESS_COOKIE, issue_access(_secret(), user), max_age=ACCESS_S,
                 httponly=True, samesite="strict", secure=COOKIE_SECURE, path="/")
    r.set_cookie(REFRESH_COOKIE, refresh, max_age=REFRESH_S, httponly=True,
                 samesite="strict", secure=COOKIE_SECURE, path="/api/auth")
    return r


def _profile(user: User) -> dict:
    return {"user": user.name, "name": user.name, "email": user.email,
            "account": user.account}


@app.post("/api/auth/signup")
async def signup(body: SignUp, request: Request) -> Response:
    _csrf(request)
    if not SIGNUP_ENABLED:
        raise HTTPException(403, "Sign-up is closed on this server")
    email, name = body.email.strip().lower(), " ".join(body.name.split())
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "That doesn't look like an email address")
    if not name:
        raise HTTPException(422, "Enter your name")
    if not await limiter.hit(f"signup:{_ip(request)}", 10, 3600):
        raise HTTPException(429, "Too many sign-ups from here. Try again in an hour.")
    try:
        user = await request.app.state.users.create_user(email, name, body.password)
    except EmailTaken:
        raise HTTPException(409, "An account with that email already exists. "
                                 "Sign in instead.") from None
    try:
        await provision(user.account)
    except Exception as e:  # noqa: BLE001  healed at the next sign-in
        log.warning("account setup for a new user failed: %s", e)
    log.info("new user signed up (account %s)", user.account)
    return _signed_in(user, await request.app.state.users.start_session(
        user, request.headers.get("user-agent", "")))


@app.post("/api/auth/login")
async def login(body: Credentials, request: Request) -> Response:
    _csrf(request)
    email = body.email.strip().lower()
    key = hashlib.sha256(email.encode()).hexdigest()[:16]
    if not (await limiter.hit(f"login:ip:{_ip(request)}", 30, 900)
            and await limiter.hit(f"login:email:{key}", 10, 900)):
        raise HTTPException(429, "Too many attempts. Wait 15 minutes and try again.")
    user = await request.app.state.users.authenticate(email, body.password)
    if user is None:
        # The typed email is not logged: it is attacker-controlled text.
        log.warning("failed login from %s", _ip(request))
        await asyncio.sleep(1.0)  # slows guessing
        raise HTTPException(401, "Wrong email or password")
    try:
        await provision(user.account)
    except Exception as e:  # noqa: BLE001
        log.warning("account check at sign-in failed: %s", e)
    return _signed_in(user, await request.app.state.users.start_session(
        user, request.headers.get("user-agent", "")))


@app.post("/api/auth/refresh")
async def refresh(request: Request) -> Response:
    _csrf(request)
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(401, "not signed in")
    try:
        got = await request.app.state.users.rotate(raw, request.headers.get("user-agent", ""))
    except RefreshRaced:
        raise HTTPException(409, "refreshed by another tab; retry") from None
    if got is None:
        r = JSONResponse({"detail": "session ended; sign in again"}, status_code=401)
        r.delete_cookie(REFRESH_COOKIE, path="/api/auth")
        return r
    return _signed_in(*got)


@app.post("/api/auth/logout")
async def logout(request: Request) -> Response:
    _csrf(request)
    if raw := request.cookies.get(REFRESH_COOKIE):
        await request.app.state.users.end_session(raw)
    r = JSONResponse({"ok": True})
    r.delete_cookie(ACCESS_COOKIE, path="/")
    r.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return r


@app.get("/api/me")
async def me(user: Me) -> dict:
    return {**_profile(user), "watchlist": await watchlist_of(user.account),
            "market_open": market_open()}


# ---------------------------------------------------------------- reads


def _desk_summary(b: dict | None) -> dict | None:
    if not b:
        return None
    brief, risk = b.get("brief") or {}, b.get("risk") or {}
    return {"stance": brief.get("stance"), "confidence": brief.get("confidence"),
            "thesis": brief.get("thesis"), "risk": risk.get("verdict"),
            "actionable": risk.get("actionable"), "as_of": b.get("finished_at"),
            "run_id": b.get("id")}


@app.get("/api/overview")
async def overview(user: Me) -> dict:
    watch = await watchlist_of(user.account)

    async def latest() -> dict:
        if not watch:
            return {"briefs": []}
        return await orch("GET", "/v1/briefs/latest", params={"symbols": ",".join(watch)})

    briefs, pf, pending, alerts, fired = await asyncio.gather(
        latest(), broker("get_portfolio", account=user.account), broker("list_proposals", account=user.account, status="pending", limit=20),
        broker("list_alerts", account=user.account, active_only=True),
        broker("list_alert_events", account=user.account, limit=5))
    by_symbol = {b["symbol"]: b for b in briefs["briefs"]}
    quotes = (hub.quotes or {}).get("quotes", {})
    sparks = await asyncio.gather(*(history(s, 30) for s in watch))
    rows = []
    for sym, h in zip(watch, sparks, strict=True):
        q = quotes.get(sym) or {}
        rows.append({"symbol": sym, "price": q.get("price"), "change_pct": q.get("change_pct"),
                     "spark": [p["close"] for p in (h or {}).get("points", [])],
                     "desk": _desk_summary(by_symbol.get(sym))})
    return {"user": user.name, "market_open": market_open(), "quotes_at": hub.quotes.get("fetched_at"),
            "watchlist": rows, "portfolio": pf, "pending": pending, "alerts": alerts,
            "alert_events": fired, "initial_capital": INITIAL_CAPITAL}


@app.get("/api/market")
async def market_board(user: Me) -> dict:
    """Every stock on PSX with name, sector and today's numbers, for the all-stocks list
    and search. Served from memory (refreshed by the live loop), so it is instant."""
    if not hub.board:
        await hub.refresh_board()
    if not hub.board:
        raise HTTPException(503, "PSX market data is unavailable right now")
    return {**hub.board, "market_open": market_open(), "watchlist": await watchlist_of(user.account)}


@app.get("/api/stocks/{symbol}")
async def stock(symbol: str, user: Me) -> dict:
    symbol = symbol.upper()
    if not symbol.isalnum() or len(symbol) > 12:
        raise HTTPException(400, "not a PSX ticker")
    briefs, h, pf, watch, alerts = await asyncio.gather(
        orch("GET", "/v1/briefs/latest", params={"symbols": symbol}),
        history(symbol, 365), broker("get_portfolio", account=user.account), watchlist_of(user.account),
        broker("list_alerts", account=user.account))
    news = None
    with contextlib.suppress(Exception):
        news = (await market("get_quotes", symbols=[symbol]))["quotes"].get(symbol)
    b = briefs["briefs"][0] if briefs["briefs"] else None
    info = next((s for s in hub.board.get("stocks", []) if s["symbol"] == symbol), None)
    position = next((p for p in pf["positions"] if p["symbol"] == symbol), None)
    if hub.board and info is None and position is None and b is None:
        # A typo'd ticker. Something you hold or the desk researched still opens, even
        # if it has left the board (delisted, suspended), so a position never vanishes.
        raise HTTPException(404, f"{symbol} is not traded on PSX")
    return {"symbol": symbol, "info": info, "history": h, "quote": news,
            "position": position,
            "cash": pf["cash"], "equity": pf["equity"], "brief": b,
            "watched": symbol in watch,
            "alerts": [a for a in alerts if a["symbol"] == symbol][:20]}


@app.get("/api/portfolio")
async def portfolio(user: Me) -> dict:
    pf, fills, props = await asyncio.gather(broker("get_portfolio", account=user.account),
                                            broker("list_fills", account=user.account, limit=200),
                                            broker("list_proposals", account=user.account, limit=100))
    return {"portfolio": pf, "fills": fills, "proposals": props,
            "curve": await equity_curve(fills), "initial_capital": INITIAL_CAPITAL}


async def equity_curve(fills: list[dict]) -> list[dict]:
    """Demo account value per day since the first trade, next to the KSE-100 rebased to
    the same starting capital: 'what if I had just bought the index?'"""
    if not fills:
        return []
    fills = sorted(fills, key=lambda f: f["filled_at"])
    start = date.fromisoformat(fills[0]["filled_at"][:10])
    days = (date.today() - start).days + 10
    symbols = sorted({f["symbol"] for f in fills})
    hists = await asyncio.gather(*(history(s, max(days, 30)) for s in [*symbols, "KSE100"]))
    closes = {s: {p["date"]: p["close"] for p in (h or {}).get("points", [])}
              for s, h in zip([*symbols, "KSE100"], hists, strict=True)}
    cal = sorted({d for s in symbols for d in closes[s] if d >= start.isoformat()}
                 | {date.today().isoformat()})
    out, cash, qty, last = [], INITIAL_CAPITAL, dict.fromkeys(symbols, 0), {}
    fi = 0
    bench0 = None
    for d in cal:
        while fi < len(fills) and fills[fi]["filled_at"][:10] <= d:
            f = fills[fi]
            value = f["quantity"] * f["price"]
            if f["side"] == "buy":
                cash -= value + f["fees"]
                qty[f["symbol"]] += f["quantity"]
            else:
                cash += value - f["fees"]
                qty[f["symbol"]] -= f["quantity"]
            last.setdefault(f["symbol"], f["price"])
            fi += 1
        for s in symbols:
            if d in closes[s]:
                last[s] = closes[s][d]
        equity = cash + sum(qty[s] * last.get(s, 0) for s in symbols)
        k = closes["KSE100"].get(d)
        if k and bench0 is None:
            bench0 = k
        out.append({"date": d, "equity": round(equity, 2),
                    "benchmark": round(INITIAL_CAPITAL * k / bench0, 2) if k and bench0
                    else None})
    return out


@app.get("/api/proposals")
async def proposals(user: Me, status: str | None = None) -> list:
    return await broker("list_proposals", account=user.account, status=status, limit=100)


@app.get("/api/runs/{run_id}")
async def run_status(run_id: uuid.UUID, user: Me) -> dict:
    r = await orch("GET", f"/v1/runs/{run_id}")
    return {"id": r["id"], "symbol": r["symbol"], "status": r["status"],
            "steps": {k: v["status"] for k, v in r.get("steps", {}).items()},
            "error": r.get("error")}


@app.get("/api/system")
async def system(request: Request, user: Me) -> dict:
    async def safe(coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}

    async def budget():
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.get(f"{GATEWAY}/v1/budget")).json()

    agents, b, audit_ok, audit = await asyncio.gather(
        safe(orch("GET", "/v1/agents")), safe(budget()), safe(broker("verify_audit")),
        safe(broker("audit_log", limit=60, account=user.account)))
    # The advisor answers the app directly, not through the orchestrator: add it here.
    advisor = request.app.state.advisor
    try:
        await advisor.refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("advisor card: %s", e)
    if isinstance(agents, dict) and "error" not in agents:
        known = {s["skill"] for s in agents.get("skills", [])}
        agents["skills"] = [*agents.get("skills", []),
                            *(s for s in advisor.listing() if s["skill"] not in known)]
        agents["unreachable"] = {**(agents.get("unreachable") or {}), **advisor.unreachable}
    return {"agents": agents, "budget": b, "audit_check": audit_ok, "audit": audit,
            "market_open": market_open()}


# ---------------------------------------------------------------- actions


class DecideBody(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=500)


@app.post("/api/proposals/{proposal_id}/decide")
async def decide(proposal_id: uuid.UUID, body: DecideBody,
                 user: Me) -> dict:
    try:
        return await broker("decide", account=user.account, proposal_id=str(proposal_id), approve=body.approve,
                            decided_by=user.account, note=body.note)
    except McpToolError as e:
        raise HTTPException(409, str(e)) from e


class TradeBody(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: int = Field(ge=1, le=1_000_000)
    note: str = Field(default="", max_length=500)
    client_order_id: str = Field(pattern=r"^[A-Za-z0-9\-]{8,64}$")


@app.post("/api/trades")
async def trade(body: TradeBody, user: Me) -> dict:
    try:
        return await broker("place_order", account=user.account, client_order_id=body.client_order_id,
                            symbol=body.symbol.upper(), side=body.side,
                            quantity=body.quantity, placed_by=user.account, note=body.note)
    except McpToolError as e:
        raise HTTPException(409, str(e)) from e


def _ticker(symbol: str) -> str:
    symbol = symbol.upper()
    if not symbol.isalnum() or len(symbol) > 12:
        raise HTTPException(400, "not a PSX ticker")
    return symbol


def _listed(symbol: str) -> str:
    """A ticker that trades on PSX today (checked against the live board)."""
    symbol = _ticker(symbol)
    if hub.board and symbol not in {s["symbol"] for s in hub.board.get("stocks", [])}:
        raise HTTPException(404, f"{symbol} is not traded on PSX")
    return symbol


@app.get("/api/watchlist")
async def get_watchlist(user: Me) -> dict:
    return {"watchlist": await watchlist_of(user.account)}


@app.put("/api/watchlist/{symbol}")
async def add_to_watchlist(symbol: str, user: Me) -> dict:
    try:
        symbols = await broker("watch", account=user.account, symbol=_listed(symbol))
    except McpToolError as e:
        raise HTTPException(409, str(e)) from e
    return {"watchlist": symbols}


@app.delete("/api/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str, user: Me) -> dict:
    return {"watchlist": await broker("unwatch", account=user.account, symbol=_ticker(symbol))}


class AlertBody(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")
    kind: str = Field(pattern=r"^(pct|pkr)$")  # % below the reference, or Rs lost
    threshold: float = Field(gt=0, le=1_000_000_000)
    days: int | None = Field(default=None, ge=1, le=3650)  # None: until cancelled


@app.get("/api/alerts")
async def list_alerts(user: Me) -> dict:
    rules, events = await asyncio.gather(
        broker("list_alerts", account=user.account),
        broker("list_alert_events", account=user.account, limit=50))
    return {"alerts": rules, "events": events}


@app.post("/api/alerts")
async def create_alert(body: AlertBody, user: Me) -> dict:
    try:
        return await broker("create_alert", account=user.account, symbol=_listed(body.symbol),
                            kind=body.kind, threshold=body.threshold, days=body.days,
                            created_by=user.account)
    except McpToolError as e:
        raise HTTPException(409, str(e)) from e


@app.delete("/api/alerts/{alert_id}")
async def cancel_alert(alert_id: uuid.UUID, user: Me) -> dict:
    try:
        return await broker("cancel_alert", account=user.account, alert_id=str(alert_id),
                            cancelled_by=user.account)
    except McpToolError as e:
        raise HTTPException(404, str(e)) from e


class ResearchBody(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")


@app.post("/api/research")
async def research(body: ResearchBody, user: Me) -> dict:
    sym = _listed(body.symbol)  # a typo would start a run that cannot find any data
    return await orch("POST", "/v1/briefs", json={"symbol": sym, "lane": "interactive"},
                      headers={"Idempotency-Key": f"app:{user.account}:{sym}:"
                                                  f"{datetime.now(UTC):%Y%m%d%H%M}"})


class ChatMessageIn(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatBody(BaseModel):
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=40)


def _advisor_payload(user: User, body: "ChatBody") -> dict:
    """What the advisor gets: the conversation, the account, and a grant that lets it
    act for this one user at the broker, for one chat (a few minutes), and that only
    the advisor can present."""
    return {"user": user.account, "messages": [m.model_dump() for m in body.messages[-12:]],
            "grant": user_grant(user.account, party="advisor-agent", ttl_s=MAX_TTL_S)}


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request, user: Me) -> dict:
    try:
        return await request.app.state.advisor.call("chat", _advisor_payload(user, body))
    except Exception as e:
        log.warning("advisor failed: %s", e)
        raise HTTPException(502, "The advisor is unavailable right now. Try again shortly.") \
            from e


@app.post("/api/chat/stream")
async def chat_stream(body: ChatBody, request: Request,
                      user: Me) -> StreamingResponse:
    """The advisor's reply as server-sent events, relayed from its A2A stream:
    tool steps and answer text as they happen, then {"type": "done", ...the reply}."""
    payload = _advisor_payload(user, body)

    async def events():
        try:
            async for ev in request.app.state.advisor.stream("chat", payload):
                if ev.get("type") == "result":
                    ev = {"type": "done", **ev["data"]}
                elif ev.get("type") not in ("delta", "tool", "discard"):
                    continue
                yield f"data: {json.dumps(ev, default=str)}\n\n"
                if await request.is_disconnected():
                    return
        except Exception as e:  # noqa: BLE001  the browser gets a readable error, not a cut
            log.warning("advisor stream failed: %s", e)
            msg = "The advisor is unavailable right now. Try again shortly."
            yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- live stream


@app.get("/api/events")
async def events(request: Request, user: Me) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    hub.clients[q] = user.account

    async def stream():
        try:
            yield "retry: 3000\n\n"
            if hub.quotes:
                yield f"data: {json.dumps({'type': 'quotes', 'data': hub.quotes})}\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(msg, default=str)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.clients.pop(q, None)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    _secret()  # fail at startup, not at first login
    setup_tracing("dashboard", app=app)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
