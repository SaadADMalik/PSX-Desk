"""`desk-events`: show recent entries on every desk stream and its dead-letter stream.

Run inside the stack, where Redis is reachable:
    docker compose exec orchestrator desk-events
"""

import argparse
import asyncio
import os

from contracts.events import BRIEFS, MARKET, REQUESTS
from redis.asyncio import Redis

from eventbus.bus import describe, dlq_stream


async def amain(count: int) -> None:
    r = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
                       socket_timeout=30)
    try:
        for stream in [MARKET, REQUESTS, BRIEFS]:
            for name in (stream, dlq_stream(stream)):
                entries = await r.xrevrange(name, count=count)
                if not entries and name.endswith(".dlq"):
                    continue
                print(f"== {name} ({await r.xlen(name)} total)")
                for msg_id, fields in reversed(entries):
                    print(f"  {msg_id}  {describe(fields)}")
            try:
                for g in await r.xinfo_groups(stream):
                    print(f"   group {g['name']}: pending={g['pending']} lag={g.get('lag')}")
            except Exception:  # noqa: BLE001, S110  stream may not exist yet
                pass
    finally:
        await r.aclose()


def main() -> None:
    p = argparse.ArgumentParser(description="Show recent desk events")
    p.add_argument("-n", type=int, default=10)
    asyncio.run(amain(p.parse_args().n))
