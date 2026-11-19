"""Redis Streams as an event bus.

Delivery is AT LEAST ONCE. That is the single most important property to design
around: every consumer must be safe to run twice on the same event.

How a message moves:

    XADD ─► stream ─► XREADGROUP (consumer group) ─► handler ─┬─ ok ─────► XACK
                                                             ├─ poison ─► DLQ + XACK
                                                             └─ error ──► stays pending
    pending + idle too long ─► XAUTOCLAIM by any consumer ─► retried (delivery count +1)
    delivered max times     ─► DLQ + XACK

  * consumer group: each event goes to ONE consumer in the group (work sharing);
    different groups each get every event (fan-out).
  * pending entries list: Redis remembers what was delivered but not acked. If a
    consumer crashes mid-handler, its messages are reclaimed by another consumer
    after `claim_idle_ms`. Nothing is lost, which is exactly why duplicates happen.
  * poison message: an event that can never succeed (malformed, invalid). Retrying
    it only blocks work behind it, so it goes straight to the dead-letter stream.
  * dead-letter stream `<stream>.dlq`: failed events kept with their error and
    delivery count, for a human to inspect and replay.

Why Redis Streams and not Kafka: same consumer-group model, one small container we
already run. We give up long retention, partitioned scale and replay-from-offset
for analytics, none of which this desk needs.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from contracts.events import Event
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

log = logging.getLogger(__name__)
_tracer = trace.get_tracer("eventbus")

Handler = Callable[[Event], Awaitable[None]]


class PoisonMessage(Exception):
    """Raise from a handler when an event can never succeed. It is dead-lettered at once."""


def dlq_stream(stream: str) -> str:
    return f"{stream}.dlq"


class EventBus:
    def __init__(self, redis: Redis, maxlen: int = 100_000) -> None:
        self.redis = redis
        self.maxlen = maxlen  # approximate cap so streams cannot grow forever

    async def publish(self, stream: str, event: Event) -> str:
        if event.traceparent is None:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            event = event.model_copy(update={"traceparent": carrier.get("traceparent")})
        return await self.redis.xadd(stream, {"event": event.model_dump_json()},
                                     maxlen=self.maxlen, approximate=True)


class Consumer:
    def __init__(self, redis: Redis, stream: str, group: str, name: str, handler: Handler, *,
                 max_deliveries: int = 3, claim_idle_ms: int = 30_000, block_ms: int = 5_000,
                 batch: int = 10) -> None:
        self.redis, self.stream, self.group, self.name = redis, stream, group, name
        self.handler = handler
        self.max_deliveries = max_deliveries
        self.claim_idle_ms = claim_idle_ms
        self.block_ms = block_ms
        self.batch = batch
        # A blocking read longer than the client's socket timeout fails every idle
        # poll with TimeoutError (redis-py 8 defaults socket_timeout to 5 s).
        kwargs = getattr(getattr(redis, "connection_pool", None), "connection_kwargs", {})
        timeout = kwargs.get("socket_timeout")
        if timeout is not None and block_ms / 1000 >= timeout:
            raise ValueError(f"block_ms={block_ms} must be below the Redis client's "
                             f"socket_timeout={timeout}s")

    async def ensure_group(self) -> None:
        # id="0": a new group also processes events published before it existed
        # (the scheduler may fire before the orchestrator first starts).
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def run_once(self) -> int:
        """Reclaim stale messages, then read new ones. Returns how many were handled."""
        handled = 0
        _, claimed, _ = await self.redis.xautoclaim(
            self.stream, self.group, self.name, self.claim_idle_ms, "0-0", count=self.batch)
        for msg_id, fields in claimed:
            await self._handle(msg_id, fields)
            handled += 1

        resp = await self.redis.xreadgroup(self.group, self.name, {self.stream: ">"},
                                           count=self.batch, block=self.block_ms)
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                await self._handle(msg_id, fields)
                handled += 1
        return handled

    async def run(self, stop: asyncio.Event) -> None:
        await self.ensure_group()
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("consumer %s/%s loop error", self.stream, self.group)
                await asyncio.sleep(1)

    async def _deliveries(self, msg_id: str) -> int:
        pending = await self.redis.xpending_range(self.stream, self.group, msg_id, msg_id, 1)
        return int(pending[0]["times_delivered"]) if pending else 1

    async def _handle(self, msg_id: str, fields: dict) -> None:
        try:
            event = Event.model_validate_json(fields.get("event", ""))
        except ValidationError as e:
            await self._dead_letter(msg_id, fields, f"unparseable event: {e.error_count()} errors")
            return

        ctx = propagate.extract({"traceparent": event.traceparent} if event.traceparent else {})
        with _tracer.start_as_current_span(f"consume {event.type}", context=ctx,
                                           kind=SpanKind.CONSUMER) as span:
            span.set_attributes({"messaging.system": "redis", "messaging.destination": self.stream,
                                 "messaging.consumer.group": self.group,
                                 "messaging.message.id": msg_id, "event.id": event.event_id})
            try:
                await self.handler(event)
            except PoisonMessage as e:
                await self._dead_letter(msg_id, fields, f"poison: {e}")
                return
            except Exception as e:  # noqa: BLE001
                deliveries = await self._deliveries(msg_id)
                span.record_exception(e)
                if deliveries >= self.max_deliveries:
                    await self._dead_letter(msg_id, fields,
                                            f"failed {deliveries} times; last: {e!r}")
                else:
                    log.warning("event %s failed (delivery %d/%d), will retry: %r",
                                event.event_id, deliveries, self.max_deliveries, e)
                return  # not acked: reclaimed after claim_idle_ms
        await self.redis.xack(self.stream, self.group, msg_id)

    async def _dead_letter(self, msg_id: str, fields: dict, reason: str) -> None:
        log.error("dead-lettering %s from %s: %s", msg_id, self.stream, reason)
        await self.redis.xadd(dlq_stream(self.stream), {
            "event": fields.get("event", ""), "original_id": msg_id, "group": self.group,
            "reason": reason[:2000], "deliveries": str(await self._deliveries(msg_id)),
        })
        await self.redis.xack(self.stream, self.group, msg_id)


def describe(fields: dict) -> str:
    """One-line summary of a stream entry, for humans."""
    try:
        e = json.loads(fields.get("event", "{}"))
    except ValueError:
        return f"<unparseable> {fields}"
    extra = f" reason={fields['reason']}" if "reason" in fields else ""
    return f"{e.get('type')} {json.dumps(e.get('payload'), separators=(',', ':'))}{extra}"
