"""The app's backend: a JSON API and a live event stream for the web UI (web/).

    browser ──► web (Next.js, :8700) ──/api/*──► this service ──► broker, market-data,
                                                    │              orchestrator, advisor
                                                    └─ /api/events (SSE): quotes, briefs,
                                                       portfolio changes, pushed live

Two identities meet here. The *service* proves itself to every backend with its key
(identity "dashboard": the only holder of broker:decide and broker:trade); the
*human* proves themself to this service with a password. The broker records both
("dashboard:<user>"), so the audit log says which person acted.

Session: an HMAC-signed cookie (HttpOnly, SameSite=Strict, 8 h). Every state-changing
request must also carry the header `X-Desk-Request: 1`, which a cross-site form
cannot send: CSRF protection without tokens in forms.

Demo account only: test money, real PSX prices. Nothing here reaches a brokerage.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from agentkit.a2a import AgentDirectory
from agentkit.auth import bearer_for
from agentkit.mcp_tools import McpToolError, McpToolset
from agentkit.tracing import setup_tracing
from contracts.events import BRIEFS
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

log = logging.getLogger("dashboard")

BROKER = os.getenv("BROKER_MCP_URL", "http://127.0.0.1:8600/mcp")
MARKET_DATA = os.getenv("MARKET_DATA_MCP_URL", "http://127.0.0.1:8300/mcp")
ORCHESTRATOR = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8200")
GATEWAY = os.getenv("LLM_GATEWAY_URL", "http://127.0.0.1:8100")
ADVISOR = os.getenv("ADVISOR_URL", "http://127.0.0.1:8406")
WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "LUCK,ENGRO,HBL").split(",") if s.strip()]
USER = os.getenv("DASHBOARD_USER", "operator")
SESSION_S = 8 * 3600
COOKIE = "desk_session"
PKT = ZoneInfo("Asia/Karachi")
INITIAL_CAPITAL = 1_000_000.0
BROKER_TOOLS = {"get_portfolio", "list_proposals", "get_proposal", "list_fills", "decide",
                "place_order", "audit_log", "verify_audit"}


# ---------------------------------------------------------------- sessions


def _secret() -> bytes:
    s = os.getenv("DASHBOARD_SESSION_SECRET", "")
    if len(s) < 16:
        raise RuntimeError("DASHBOARD_SESSION_SECRET must be set (16+ characters)")
    return s.encode()


def _sign(*parts: str) -> str:
    return hmac.new(_secret(), "|".join(parts).encode(), hashlib.sha256).hexdigest()


def make_session(user: str, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + SESSION_S))
    return f"{user}|{exp}|{_sign('session', user, exp)}"


def read_session(cookie: str | None, now: float | None = None) -> str | None:
    try:
        user, exp, sig = (cookie or "").split("|")
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign("session", user, exp)):
        return None
    if int(exp) < (now or time.time()):
        return None
    return user


def check_password(user: str, password: str) -> bool:
    expected = os.getenv("DASHBOARD_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(user, USER) and \
        hmac.compare_digest(password.encode(), expected.encode())


def current_user(request: Request) -> str:
    user = read_session(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "not signed in")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-desk-request") != "1":
        raise HTTPException(403, "missing X-Desk-Request header")
    return user


# ---------------------------------------------------------------- backends


async def broker(tool: str, **args):
    async with McpToolset(BROKER, allow={tool}) as ts:
        return await ts.call(tool, **args)


async def market(tool: str, **args):
    async with McpToolset(MARKET_DATA, allow={tool}) as ts:
        return await ts.call(tool, **args)


async def orch(method: str, path: str, **kw) -> dict:
    async with httpx.AsyncClient(base_url=ORCHESTRATOR, timeout=20, auth=bearer_for()) as c:
        r = await c.request(method, path, **kw)
    if r.status_code >= 400:
        raise HTTPException(502, f"orchestrator HTTP {r.status_code}")
    return r.json()


_history_cache: dict[tuple[str, int], tuple[float, dict]] = {}


async def history(symbol: str, days: int) -> dict | None:
    """Daily closes; cached 10 minutes (they change once a day)."""
    key = (symbol, days)
    hit = _history_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        h = await market("get_price_history", symbol=symbol, days=days)
    except Exception as e:  # noqa: BLE001
        log.warning("no history for %r: %s", symbol, e)
        return None
    _history_cache[key] = (time.time(), h)
    return h


def market_open(now: datetime | None = None) -> bool:
    t = (now or datetime.now(UTC)).astimezone(PKT)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (15, 30)


# ---------------------------------------------------------------- live hub


class Hub:
    """Fan-out of live events to every open browser tab (Server-Sent Events)."""

    def __init__(self) -> None:
        self.clients: set[asyncio.Queue] = set()
        self.quotes: dict = {}
        self.board: dict = {}

    def publish(self, kind: str, data: dict) -> None:
        msg = {"type": kind, "data": data, "at": datetime.now(UTC).isoformat()}
        for q in list(self.clients):
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
        """The broker publishes no events, so watch it: cheap reads, every 4 s."""
        seen = None
        while True:
            try:
                props = await broker("list_proposals", limit=20)
                fills = await broker("list_fills", limit=1)
                digest = json.dumps([[p["id"], p["status"]] for p in props] + fills,
                                    sort_keys=True)
                if seen is not None and digest != seen:
                    self.publish("portfolio", {"pending": sum(p["status"] == "pending"
                                                              for p in props)})
                seen = digest
            except Exception as e:  # noqa: BLE001
                log.warning("broker watch failed: %s", e)
            await asyncio.sleep(4)


hub = Hub()


async def warm_sparklines() -> None:
    """Keep the watchlist's 30-day closes cached, so Today never waits on a cold PSX fetch
    (about 13 s per symbol through the rate-limited data server)."""
    while True:
        for sym in WATCHLIST:
            _history_cache.pop((sym, 30), None)
            await history(sym, 30)
        await asyncio.sleep(540)  # just inside the 10-minute cache window


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _secret()
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                           decode_responses=True, socket_timeout=30)
    app.state.advisor = AgentDirectory([ADVISOR], timeout_s=180)
    tasks = [asyncio.create_task(hub.quotes_loop()), asyncio.create_task(hub.briefs_loop(redis)),
             asyncio.create_task(hub.broker_loop()), asyncio.create_task(warm_sparklines())]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis.aclose()
    await app.state.advisor.aclose()


app = FastAPI(title="desk-app-api", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------------------------------- auth endpoints


class Login(BaseModel):
    # A strict format: the name is written to logs and into the session cookie.
    user: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(body: Login, request: Request) -> Response:
    if request.headers.get("x-desk-request") != "1":
        raise HTTPException(403, "missing X-Desk-Request header")
    if not check_password(body.user, body.password):
        log.warning("failed login for %r", body.user[:40])
        await asyncio.sleep(1.0)  # slows guessing
        raise HTTPException(401, "Wrong user or password")
    r = JSONResponse({"user": body.user})
    r.set_cookie(COOKIE, make_session(body.user), max_age=SESSION_S, httponly=True,
                 samesite="strict", path="/")
    return r


@app.post("/api/logout")
async def logout() -> Response:
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, path="/")
    return r


@app.get("/api/me")
async def me(user: str = Depends(current_user)) -> dict:
    return {"user": user, "watchlist": WATCHLIST, "market_open": market_open()}


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
async def overview(user: str = Depends(current_user)) -> dict:
    briefs, pf, pending = await asyncio.gather(
        orch("GET", "/v1/briefs/latest", params={"symbols": ",".join(WATCHLIST)}),
        broker("get_portfolio"), broker("list_proposals", status="pending", limit=20))
    by_symbol = {b["symbol"]: b for b in briefs["briefs"]}
    quotes = (hub.quotes or {}).get("quotes", {})
    sparks = await asyncio.gather(*(history(s, 30) for s in WATCHLIST))
    rows = []
    for sym, h in zip(WATCHLIST, sparks, strict=True):
        q = quotes.get(sym) or {}
        rows.append({"symbol": sym, "price": q.get("price"), "change_pct": q.get("change_pct"),
                     "spark": [p["close"] for p in (h or {}).get("points", [])],
                     "desk": _desk_summary(by_symbol.get(sym))})
    return {"user": user, "market_open": market_open(), "quotes_at": hub.quotes.get("fetched_at"),
            "watchlist": rows, "portfolio": pf, "pending": pending,
            "initial_capital": INITIAL_CAPITAL}


@app.get("/api/market")
async def market_board(user: str = Depends(current_user)) -> dict:
    """Every stock on PSX with name, sector and today's numbers, for the all-stocks list
    and search. Served from memory (refreshed by the live loop), so it is instant."""
    if not hub.board:
        await hub.refresh_board()
    if not hub.board:
        raise HTTPException(503, "PSX market data is unavailable right now")
    return {**hub.board, "market_open": market_open(), "watchlist": WATCHLIST}


@app.get("/api/stocks/{symbol}")
async def stock(symbol: str, user: str = Depends(current_user)) -> dict:
    symbol = symbol.upper()
    if not symbol.isalnum() or len(symbol) > 12:
        raise HTTPException(400, "not a PSX ticker")
    briefs, h, pf = await asyncio.gather(
        orch("GET", "/v1/briefs/latest", params={"symbols": symbol}),
        history(symbol, 365), broker("get_portfolio"))
    news = None
    with contextlib.suppress(Exception):
        news = (await market("get_quotes", symbols=[symbol]))["quotes"].get(symbol)
    b = briefs["briefs"][0] if briefs["briefs"] else None
    info = next((s for s in hub.board.get("stocks", []) if s["symbol"] == symbol), None)
    return {"symbol": symbol, "info": info, "history": h, "quote": news,
            "position": next((p for p in pf["positions"] if p["symbol"] == symbol), None),
            "cash": pf["cash"], "equity": pf["equity"], "brief": b}


@app.get("/api/portfolio")
async def portfolio(user: str = Depends(current_user)) -> dict:
    pf, fills, props = await asyncio.gather(broker("get_portfolio"),
                                            broker("list_fills", limit=200),
                                            broker("list_proposals", limit=100))
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
async def proposals(status: str | None = None, user: str = Depends(current_user)) -> list:
    return await broker("list_proposals", status=status, limit=100)


@app.get("/api/runs/{run_id}")
async def run_status(run_id: uuid.UUID, user: str = Depends(current_user)) -> dict:
    r = await orch("GET", f"/v1/runs/{run_id}")
    return {"id": r["id"], "symbol": r["symbol"], "status": r["status"],
            "steps": {k: v["status"] for k, v in r.get("steps", {}).items()},
            "error": r.get("error")}


@app.get("/api/system")
async def system(request: Request, user: str = Depends(current_user)) -> dict:
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
        safe(broker("audit_log", limit=60)))
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
                 user: str = Depends(current_user)) -> dict:
    try:
        return await broker("decide", proposal_id=str(proposal_id), approve=body.approve,
                            decided_by=user, note=body.note)
    except McpToolError as e:
        raise HTTPException(409, str(e)) from e


class TradeBody(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: int = Field(ge=1, le=1_000_000)
    note: str = Field(default="", max_length=500)
    client_order_id: str = Field(pattern=r"^[A-Za-z0-9\-]{8,64}$")


@app.post("/api/trades")
async def trade(body: TradeBody, user: str = Depends(current_user)) -> dict:
    try:
        return await broker("place_order", client_order_id=body.client_order_id,
                            symbol=body.symbol.upper(), side=body.side,
                            quantity=body.quantity, placed_by=user, note=body.note)
    except McpToolError as e:
        raise HTTPException(409, str(e)) from e


class ResearchBody(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")


@app.post("/api/research")
async def research(body: ResearchBody, user: str = Depends(current_user)) -> dict:
    sym = body.symbol.upper()
    return await orch("POST", "/v1/briefs", json={"symbol": sym, "lane": "interactive"},
                      headers={"Idempotency-Key": f"app:{user}:{sym}:"
                                                  f"{datetime.now(UTC):%Y%m%d%H%M}"})


class ChatMessageIn(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatBody(BaseModel):
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=40)


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request, user: str = Depends(current_user)) -> dict:
    try:
        return await request.app.state.advisor.call(
            "chat", {"user": user, "messages": [m.model_dump() for m in body.messages[-12:]]})
    except Exception as e:
        log.warning("advisor failed: %s", e)
        raise HTTPException(502, "The advisor is unavailable right now. Try again shortly.") \
            from e


@app.post("/api/chat/stream")
async def chat_stream(body: ChatBody, request: Request,
                      user: str = Depends(current_user)) -> StreamingResponse:
    """The advisor's reply as server-sent events, relayed from its A2A stream:
    tool steps and answer text as they happen, then {"type": "done", ...the reply}."""
    payload = {"user": user, "messages": [m.model_dump() for m in body.messages[-12:]]}

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
async def events(request: Request, user: str = Depends(current_user)) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    hub.clients.add(q)

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
            hub.clients.discard(q)

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
