"""Scheduler: after each PSX trading day, request briefs for the watchlist.

It publishes events and knows nothing about who handles them. That decoupling is
the point of the bus: the orchestrator can be down, slow, or replaced, and the
requests wait in the stream.

It has no database and needs none:
  * the idempotency key is deterministic (`daily:<date>:<symbol>`), so firing
    twice (a restart, a manual `--now`, two scheduler replicas) creates no extra runs;
  * the outbox pattern is for when a state change and its event must agree. The
    scheduler has no state, so it publishes directly.

Known gap: PSX holidays are not modelled; on a holiday the briefs simply describe
the last trading day again (and are mostly cache hits).
"""

import argparse
import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agentkit.tracing import setup_tracing, shutdown_tracing
from contracts import Lane
from contracts.events import MARKET, REQUESTS, BriefRequested, Event
from eventbus import EventBus
from opentelemetry import trace
from redis.asyncio import Redis

log = logging.getLogger("scheduler")
PKT = ZoneInfo("Asia/Karachi")
_tracer = trace.get_tracer("scheduler")


def watchlist() -> list[str]:
    raw = os.getenv("WATCHLIST", "LUCK,ENGRO,HBL,OGDC,PPL,MCB,FFC,SYS,HUBC,MEBL")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def next_fire(now: datetime, at: time) -> datetime:
    """Next weekday at `at` Pakistan time, strictly after `now`."""
    local = now.astimezone(PKT)
    candidate = datetime.combine(local.date(), at, tzinfo=PKT)
    while candidate <= local or candidate.weekday() >= 5:
        candidate = datetime.combine(candidate.date() + timedelta(days=1), at, tzinfo=PKT)
    return candidate


async def fire(bus: EventBus, day: date, symbols: list[str]) -> None:
    with _tracer.start_as_current_span("scheduler.fire") as span:
        span.set_attributes({"market.date": day.isoformat(), "watchlist.size": len(symbols)})
        await bus.publish(MARKET, Event(type="market.closed", source="scheduler",
                                        payload={"date": day.isoformat()}))
        for symbol in symbols:
            payload = BriefRequested(symbol=symbol, idempotency_key=f"daily:{day}:{symbol}",
                                     lane=Lane.pipeline)
            await bus.publish(REQUESTS, Event(type="brief.requested", source="scheduler",
                                              payload=payload.model_dump(mode="json")))
    log.info("fired %d brief requests for %s", len(symbols), day)


def _redis() -> Redis:
    return Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                          decode_responses=True, socket_timeout=30)


async def loop() -> None:
    at = time.fromisoformat(os.getenv("SCHEDULE_AT", "16:15"))  # PSX closes 15:30 PKT
    redis = _redis()
    bus = EventBus(redis)
    while True:
        when = next_fire(datetime.now(PKT), at)
        log.info("next fire at %s", when.isoformat())
        await asyncio.sleep(max(0.0, (when - datetime.now(PKT)).total_seconds()))
        try:
            await fire(bus, when.date(), watchlist())
        except Exception:
            log.exception("fire failed; retrying in 60s")
            await asyncio.sleep(60)
            await fire(bus, when.date(), watchlist())


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    setup_tracing("scheduler")
    asyncio.run(loop())


def cli() -> None:
    """`desk-schedule --now`: fire immediately (for drills and catching up)."""
    p = argparse.ArgumentParser(description="Fire the daily watchlist now")
    p.add_argument("--date", type=date.fromisoformat, default=datetime.now(PKT).date())
    p.add_argument("--symbols", help="comma-separated; default WATCHLIST")
    args = p.parse_args()
    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
               else watchlist())
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    setup_tracing("scheduler")

    async def go() -> None:
        redis = _redis()
        try:
            await fire(EventBus(redis), args.date, symbols)
        finally:
            await redis.aclose()

    asyncio.run(go())
    shutdown_tracing()
