"""HTTP surface of the gateway. Every model call in the system comes through here."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import asyncpg
import httpx
import uvicorn
from agentkit.auth import guard_http, load_verifier
from agentkit.tracing import setup_tracing
from contracts import ChatRequest, ChatResponse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace
from redis.asyncio import Redis

from llm_gateway.breaker import CircuitBreaker
from llm_gateway.cache import ResponseCache
from llm_gateway.config import DEFAULT_LIMITS, DEFAULT_TIERS, Settings
from llm_gateway.ledger import Ledger
from llm_gateway.limits import RateLimiter
from llm_gateway.provider import GroqProvider, ModelOutputError, PermanentError
from llm_gateway.router import AllModelsFailed, ContextTooLarge, Gateway

log = logging.getLogger("llm_gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    pool = None
    if settings.database_url:
        try:
            pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
        except Exception:
            log.exception("ledger database unreachable; continuing without ledger")
    http = httpx.AsyncClient(timeout=settings.request_timeout_s)
    app.state.gateway = Gateway(
        provider=GroqProvider(http, settings.groq_api_key, settings.groq_base_url),
        limiter=RateLimiter(redis, DEFAULT_LIMITS),
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_s),
        cache=ResponseCache(redis, settings.cache_ttl_s),
        ledger=Ledger(pool),
        settings=settings,
        limits=DEFAULT_LIMITS,
        tiers=DEFAULT_TIERS,
    )
    yield
    await http.aclose()
    await redis.aclose()
    if pool:
        await pool.close()


app = FastAPI(title="llm-gateway", lifespan=lifespan)
guard_http(app, load_verifier("llm-gateway"), {"POST /v1/chat": "llm:chat"})


def proven_caller(declared: str, request: Request) -> str:
    """The ledger records who *proved* they called. A service may still name a role
    (the debate agent calls as bull, bear and judge); that is kept after a slash."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return declared
    return principal.name if declared == principal.name else f"{principal.name}/{declared}"


def error_reply(e: Exception) -> tuple[int, dict, dict]:
    """(status, body, headers) for a failed completion; shared by both endpoints."""
    if isinstance(e, ContextTooLarge):
        return 413, {"detail": str(e)}, {}
    if isinstance(e, ModelOutputError):
        return 422, {"detail": e.message, "code": e.code,
                     "failed_generation": e.failed_generation}, {}
    if isinstance(e, PermanentError):
        return 502, {"detail": str(e)}, {}
    if isinstance(e, AllModelsFailed):
        return 503, {"detail": "all models failed", "reasons": e.reasons}, {"Retry-After": "30"}
    if isinstance(e, ValueError):
        return 400, {"detail": str(e)}, {}
    raise e


def _prepare(req: ChatRequest, request: Request) -> tuple[ChatRequest, str | None]:
    req = req.model_copy(update={"caller": proven_caller(req.caller, request)})
    ctx = trace.get_current_span().get_span_context()
    return req, (format(ctx.trace_id, "032x") if ctx.is_valid else None)


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    req, trace_id = _prepare(req, request)
    gw: Gateway = app.state.gateway
    try:
        return await gw.complete(req, trace_id=trace_id)
    except (ContextTooLarge, ModelOutputError, PermanentError, AllModelsFailed,
            ValueError) as e:
        status, body, headers = error_reply(e)
        return JSONResponse(status_code=status, content=body, headers=headers)


@app.post("/v1/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """Server-sent events: {"type": "delta", "text"} as the answer is written, then
    {"type": "done", "response": ChatResponse} or {"type": "error", "status", "body"}
    (the same status and body /v1/chat would have returned)."""
    req, trace_id = _prepare(req, request)
    gw: Gateway = app.state.gateway
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_delta(text: str) -> None:
        await queue.put({"type": "delta", "text": text})

    async def work() -> None:
        try:
            resp = await gw.complete(req, trace_id=trace_id, on_delta=on_delta)
            await queue.put({"type": "done", "response": resp.model_dump(mode="json")})
        except (ContextTooLarge, ModelOutputError, PermanentError, AllModelsFailed,
                ValueError) as e:
            status, body, _ = error_reply(e)
            await queue.put({"type": "error", "status": status, "body": body})
        except Exception as e:
            log.exception("stream failed")
            await queue.put({"type": "error", "status": 500, "body": {"detail": str(e)}})
        finally:
            await queue.put(None)

    task = asyncio.create_task(work())

    async def events():
        try:
            while (item := await queue.get()) is not None:
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            if not task.done():  # the caller went away: stop spending tokens
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@app.get("/v1/budget")
async def budget() -> dict:
    gw: Gateway = app.state.gateway
    models = [await gw.limiter.usage(m) for m in gw.limits]
    for m in models:
        m["circuit"] = gw.breaker.state(m["model"]).value
    return {"models": models}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    setup_tracing("llm-gateway", app=app)
    uvicorn.run(app, host="0.0.0.0", port=8000)
