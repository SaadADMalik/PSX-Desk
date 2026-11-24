"""Scheduler: the desk's clock.

  * After each PSX trading day it requests a brief for every stock on anyone's
    watchlist, once per stock: ten users watching LUCK means one LUCK brief.
  * While the market is open (and once after it closes) it asks the broker to check
    every price alert. Each alert that fires gets a fresh brief on that stock.

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

from agentkit.mcp_tools import McpToolset
from agentkit.tracing import setup_tracing, shutdown_tracing
from contracts import Lane
from contracts.events import MARKET, REQUESTS, BriefRequested, Event
from eventbus import EventBus
from opentelemetry import trace
from redis.asyncio import Redis

log = logging.getLogger("scheduler")
PKT = ZoneInfo("Asia/Karachi")
_tracer = trace.get_tracer("scheduler")


BROKER = os.getenv("BROKER_MCP_URL", "http://127.0.0.1:8600/mcp")
ALERT_CHECK_S = float(os.getenv("ALERT_CHECK_S", "900"))


def default_watchlist() -> list[str]:
    raw = os.getenv("WATCHLIST", "LUCK,ENGRO,HBL,OGDC,PPL,MCB,FFC,SYS,HUBC,MEBL")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


async def watchlist() -> list[str]:
    """Every stock on anyone's watchlist (the broker keeps them). If the broker cannot
    be reached, fall back to the default list rather than skip the day's research."""
    try:
        async with McpToolset(BROKER, allow={"watched_symbols"}) as ts:
            symbols = await ts.call("watched_symbols")
    except Exception as e:  # noqa: BLE001
        log.warning("watchlists unavailable (%s); using the default list", e)
        return default_watchlist()
    return symbols


def market_hours(now: datetime) -> bool:
    """Weekdays from the 09:30 open until 16:00, half an hour after the 15:30 close,
    so the closing prices get one last check."""
    t = now.astimezone(PKT)
    return t.weekday() < 5 and time(9, 30) <= t.time() < time(16, 0)


async def check_alerts(bus: EventBus) -> int:
    """One alert pass. Each alert that fires requests a fresh brief on its stock,
    keyed by the alert event, so a retry never asks twice."""
    async with McpToolset(BROKER, allow={"check_alerts"}) as ts:
        events = await ts.call("check_alerts")
    fired = [e for e in events if e["kind"] == "triggered"]
    for e in fired:
        payload = BriefRequested(symbol=e["symbol"], idempotency_key=f"alert:{e['id']}",
                                 lane=Lane.interactive)
        await bus.publish(REQUESTS, Event(type="brief.requested", source="scheduler",
                                          payload=payload.model_dump(mode="json")))
    if events:
        log.info("alerts: %d fired, %d re-armed", len(fired), len(events) - len(fired))
    return len(fired)


async def alert_loop(bus: EventBus) -> None:
    while True:
        if market_hours(datetime.now(PKT)):
            try:
                await check_alerts(bus)
            except Exception as e:  # noqa: BLE001  the next pass retries
                log.warning("alert check failed: %s", e)
        await asyncio.sleep(ALERT_CHECK_S)


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
    redis = _redis()
    bus = EventBus(redis)
    await asyncio.gather(daily_loop(bus), alert_loop(bus))


async def daily_loop(bus: EventBus) -> None:
    at = time.fromisoformat(os.getenv("SCHEDULE_AT", "16:15"))  # PSX closes 15:30 PKT
    while True:
        when = next_fire(datetime.now(PKT), at)
        log.info("next fire at %s", when.isoformat())
        await asyncio.sleep(max(0.0, (when - datetime.now(PKT)).total_seconds()))
        try:
            await fire(bus, when.date(), await watchlist())
        except Exception:
            log.exception("fire failed; retrying in 60s")
            await asyncio.sleep(60)
            await fire(bus, when.date(), await watchlist())


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
               else asyncio.run(watchlist()))
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
