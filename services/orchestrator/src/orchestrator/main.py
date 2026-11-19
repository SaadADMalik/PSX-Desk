"""Orchestrator service: HTTP API, event consumer, workflow engine and outbox relay.

Two ways in, one way through:

    POST /v1/briefs ──────────────┐
                                  ├─► create_run (idempotent) ─► engine ─► outbox ─► desk.briefs
    desk.requests ─► consumer ────┘

The API returns 202 with a run id at once; the work happens in the engine, so a
slow brief never holds an HTTP connection, and a crash never loses the request.
"""

import asyncio
import logging
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager

import uvicorn
from agentkit.a2a import AgentDirectory
from agentkit.auth import guard_http, load_verifier
from agentkit.tracing import setup_tracing
from contracts import Lane
from contracts.events import REQUESTS, BriefRequested, Event
from eventbus import Consumer, EventBus, PoisonMessage
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from opentelemetry import propagate
from pydantic import BaseModel, ValidationError, field_validator
from redis.asyncio import Redis

from orchestrator.desk import Desk
from orchestrator.engine import Engine, redis_chaos
from orchestrator.store import TERMINAL, Store

log = logging.getLogger("orchestrator")
SYMBOL = re.compile(r"^[A-Z0-9]{1,12}$")
CONSUMER_GROUP = "orchestrator"


def _symbol(v: str) -> str:
    v = v.strip().upper()
    if not SYMBOL.match(v):
        raise ValueError("not a PSX ticker")
    return v


class BriefRequest(BaseModel):
    symbol: str
    lane: Lane = Lane.interactive

    @field_validator("symbol")
    @classmethod
    def _check(cls, v: str) -> str:
        return _symbol(v)


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def make_request_handler(store: Store):
    async def on_brief_requested(event: Event) -> None:
        if event.type != "brief.requested":
            raise PoisonMessage(f"unexpected event type {event.type!r} on {REQUESTS}")
        try:
            req = BriefRequested.model_validate(event.payload)
            symbol = _symbol(req.symbol)
        except (ValidationError, ValueError) as e:
            raise PoisonMessage(f"invalid brief.requested: {e}") from e
        created = await store.create_run(
            symbol=symbol, lane=req.lane.value, idempotency_key=req.idempotency_key,
            trace_parent=event.traceparent, consumer=CONSUMER_GROUP, event_id=event.event_id)
        if created.created:
            log.info("run %s created from event %s", created.run_id, event.event_id)
        else:
            log.info("duplicate (%s) for %s -> existing run %s",
                     created.duplicate, req.idempotency_key, created.run_id)

    return on_brief_requested


async def _relay_loop(store: Store, bus: EventBus, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await store.relay_outbox(bus.publish) == 0:
                await asyncio.sleep(1)
        except Exception:
            log.exception("outbox relay error")
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = f"{socket.gethostname()}-{os.getpid()}"
    urls = [u.strip() for u in os.getenv("AGENT_URLS", "").split(",") if u.strip()]
    directory = AgentDirectory(urls)
    await directory.refresh()
    log.info("agents: %s unreachable: %s", directory.listing(), directory.unreachable)

    store = await Store.connect(os.environ["DATABASE_URL"])
    # socket_timeout must exceed the consumer's XREADGROUP block (5 s); redis-py 8
    # defaults it to 5 s, so every idle read would time out.
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                           decode_responses=True, socket_timeout=30)
    bus = EventBus(redis)
    desk = Desk(directory,
                market_data_url=os.getenv("MARKET_DATA_MCP_URL", "http://127.0.0.1:8300/mcp"),
                memory_url=os.getenv("MEMORY_MCP_URL", "http://127.0.0.1:8500/mcp"))
    engine = Engine(store, desk, worker=worker,
                    lease_s=float(os.getenv("LEASE_SECONDS", "30")),
                    concurrency=int(os.getenv("ENGINE_CONCURRENCY", "2")),
                    chaos=redis_chaos(redis))
    consumer = Consumer(redis, REQUESTS, CONSUMER_GROUP, worker, make_request_handler(store))

    stop = asyncio.Event()
    tasks = [asyncio.create_task(engine.run(stop), name="engine"),
             asyncio.create_task(consumer.run(stop), name="consumer"),
             asyncio.create_task(_relay_loop(store, bus, stop), name="outbox-relay")]
    app.state.directory, app.state.store = directory, store
    log.info("worker %s started", worker)
    yield
    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await store.close()
    await redis.aclose()
    await directory.aclose()


app = FastAPI(title="orchestrator", lifespan=lifespan)
guard_http(app, load_verifier("orchestrator"), {
    "POST /v1/briefs": "briefs:write", "GET /v1/runs": "runs:read",
    "GET /v1/briefs": "runs:read",
    "GET /v1/agents": "runs:read"})


@app.get("/v1/agents")
async def agents(refresh: bool = False) -> dict:
    d: AgentDirectory = app.state.directory
    if refresh:
        await d.refresh()
    return {"skills": d.listing(), "unreachable": d.unreachable}


@app.post("/v1/briefs", status_code=202)
async def create_brief(req: BriefRequest,
                       idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    """Returns at once with a run id. Send an `Idempotency-Key` header to make retries
    safe: the same key always returns the same run."""
    store: Store = app.state.store
    key = f"api:{idempotency_key}" if idempotency_key else f"api:{uuid.uuid4()}"
    created = await store.create_run(symbol=req.symbol, lane=req.lane.value,
                                     idempotency_key=key, trace_parent=current_traceparent())
    return JSONResponse(status_code=202 if created.created else 200, content={
        "run_id": str(created.run_id), "created": created.created,
        "duplicate": created.duplicate})


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: uuid.UUID) -> dict:
    run = await app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    run["terminal"] = run["status"] in TERMINAL
    return run


@app.get("/v1/briefs/latest")
async def latest_briefs(symbols: str | None = None) -> dict:
    """Latest finished brief per symbol (comma-separated list, or all)."""
    wanted = None
    if symbols:
        try:
            wanted = [_symbol(s) for s in symbols.split(",") if s.strip()][:100]
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return {"briefs": await app.state.store.latest_briefs(wanted)}


@app.get("/v1/runs")
async def list_runs(limit: int = 20) -> dict:
    return {"runs": await app.state.store.list_runs(min(limit, 200))}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    setup_tracing("orchestrator", app=app)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
