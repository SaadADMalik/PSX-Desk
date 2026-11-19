"""At-least-once delivery, retry, dead-lettering and reclaim, against fakeredis."""

import pytest
from contracts.events import Event
from eventbus import Consumer, EventBus, PoisonMessage, dlq_stream
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

S = "test.stream"


def consumer(redis, handler, name="c1", **kw):
    return Consumer(redis, S, "g", name, handler, block_ms=1, claim_idle_ms=0, **kw)


async def test_handled_event_is_acked(redis):
    seen = []

    async def ok(e):
        seen.append(e.payload["n"])

    c = consumer(redis, ok)
    await c.ensure_group()
    await EventBus(redis).publish(S, Event(type="t", source="test", payload={"n": 1}))
    assert await c.run_once() == 1
    assert seen == [1]
    assert (await redis.xpending(S, "g"))["pending"] == 0


async def test_group_created_late_still_sees_earlier_events(redis):
    await EventBus(redis).publish(S, Event(type="t", source="test"))
    seen = []

    async def ok(e):
        seen.append(e.type)

    c = consumer(redis, ok)
    await c.ensure_group()
    await c.run_once()
    assert seen == ["t"]


async def test_failing_event_is_retried_then_dead_lettered(redis):
    calls = []

    async def flaky(e):
        calls.append(1)
        raise ConnectionError("db down")

    c = consumer(redis, flaky, max_deliveries=3)
    await c.ensure_group()
    await EventBus(redis).publish(S, Event(type="t", source="test"))
    for _ in range(4):
        await c.run_once()
    assert len(calls) == 3  # delivered, reclaimed, reclaimed, then dead-lettered
    dead = await redis.xrange(dlq_stream(S))
    assert len(dead) == 1 and "failed 3 times" in dead[0][1]["reason"]
    assert (await redis.xpending(S, "g"))["pending"] == 0


async def test_poison_message_goes_straight_to_dlq(redis):
    calls = []

    async def strict(e):
        calls.append(1)
        raise PoisonMessage("symbol missing")

    c = consumer(redis, strict)
    await c.ensure_group()
    await EventBus(redis).publish(S, Event(type="t", source="test"))
    await c.run_once()
    await c.run_once()
    assert len(calls) == 1
    assert "poison" in (await redis.xrange(dlq_stream(S)))[0][1]["reason"]


async def test_unparseable_entry_is_dead_lettered_without_calling_handler(redis):
    async def never(e):
        raise AssertionError("should not be called")

    c = consumer(redis, never)
    await c.ensure_group()
    await redis.xadd(S, {"event": "{not json"})
    await c.run_once()
    assert len(await redis.xrange(dlq_stream(S))) == 1


async def test_message_of_a_crashed_consumer_is_reclaimed_by_another(redis):
    await redis.xgroup_create(S, "g", id="0", mkstream=True)
    await EventBus(redis).publish(S, Event(type="t", source="test"))
    # c1 reads the message and "crashes" before acking.
    await redis.xreadgroup("g", "c1", {S: ">"}, count=1)
    seen = []

    async def ok(e):
        seen.append(e.type)

    await consumer(redis, ok, name="c2").run_once()
    assert seen == ["t"]
    assert (await redis.xpending(S, "g"))["pending"] == 0


async def test_trace_context_crosses_the_bus(redis):
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("t")
    got = []

    async def capture(e):
        got.append(trace.get_current_span().get_span_context().trace_id)

    c = consumer(redis, capture)
    await c.ensure_group()
    with tracer.start_as_current_span("producer") as span:
        await EventBus(redis).publish(S, Event(type="t", source="test"))
        producer_trace = span.get_span_context().trace_id
    await c.run_once()
    assert got == [producer_trace]


def test_consumer_rejects_block_longer_than_socket_timeout():
    from redis.asyncio import Redis

    r = Redis.from_url("redis://localhost:6379/0", socket_timeout=5)
    with pytest.raises(ValueError, match="socket_timeout"):
        Consumer(r, "s", "g", "c", lambda e: None, block_ms=5_000)
    Consumer(Redis.from_url("redis://localhost:6379/0", socket_timeout=30), "s", "g", "c",
             lambda e: None, block_ms=5_000)
