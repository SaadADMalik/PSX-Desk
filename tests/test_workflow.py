"""Durable workflow, outbox, idempotency and the Slice 3 brief workflow, against a real
Postgres (testcontainers, pgvector image).

Skipped automatically if Docker is not available.
"""

import asyncio
import uuid
from datetime import datetime, time

import pytest
from contracts import (
    AnalysisReport,
    Brief,
    Case,
    Claim,
    DebateRequest,
    Evidence,
    Judgement,
    ProposeTradeResult,
    Stance,
)
from contracts.events import BRIEFS, REQUESTS, Event
from eventbus import Consumer, EventBus, dlq_stream
from market_data_mcp.evidence_store import PgEvidenceStore
from memory_mcp.server import HashEmbedder, MemoryRecord, PgMemoryStore
from orchestrator.desk import AgentFailed
from orchestrator.engine import Engine
from orchestrator.main import make_request_handler
from orchestrator.store import SPECIALISTS, STEPS, LeaseLost, Store
from orchestrator.workflow import WorkflowConfig
from scheduler.main import PKT, fire, next_fire


@pytest.fixture
async def store(pg_url):
    s = await Store.connect(pg_url)
    await s.pool.execute("TRUNCATE workflow_steps, workflow_runs, outbox, processed_events, "
                         "evidence, memories")
    yield s
    await s.close()


# ---------------------------------------------------------------- doubles

EV = Evidence(id="ev_aaa", symbol="LUCK", kind="price_summary", values={"rsi_14": 65.0},
              source="t", as_of="2026-11-14", fetched_at="2026-11-14T00:00:00Z")
RSI = Claim(text="RSI is 65", evidence_ids=["ev_aaa"])


def brief(risk_text="RSI of 65 could cool off", stance=Stance.bullish):
    return Brief(symbol="LUCK", stance=stance, confidence=0.7, thesis="Momentum leads.",
                 key_points=[RSI], risks=[Claim(text=risk_text, evidence_ids=["ev_aaa"])])


class FakeDesk:
    """Every participant of the workflow, scripted."""

    def __init__(self, failing=(), briefs=None, memory_up=True):
        self.failing = set(failing)
        self.briefs = list(briefs or [])
        self.memory_up = memory_up
        self.calls: list[str] = []
        self.proposed: list = []
        self.judge_requests: list[DebateRequest] = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.failing:
            raise AgentFailed(f"{name} down")

    async def analyse(self, skill, req):
        self._maybe_fail(skill)
        return AnalysisReport(request_id=req.request_id, agent=skill, symbol=req.symbol,
                              stance=Stance.neutral, confidence=0.5, summary="Fine enough.",
                              points=[RSI], evidence_ids=["ev_aaa"], tokens_used=10)

    async def evidence(self, ids):
        return {i: EV for i in ids if i == EV.id}

    async def argue(self, side, req):
        self._maybe_fail(f"{side.value}_case")
        return Case(request_id=req.request_id, symbol=req.symbol, side=side,
                    thesis=f"The {side.value} view.", arguments=[RSI],
                    concession="The other side has a point.", confidence=0.6, tokens_used=20)

    async def judge(self, req):
        self._maybe_fail("judgement")
        self.judge_requests.append(req)
        b = self.briefs.pop(0) if self.briefs else brief()
        return Judgement(request_id=req.request_id, brief=b, tokens_used=30)

    async def remember(self, key, b, as_of):
        self.calls.append("remember")
        if not self.memory_up:
            raise ConnectionError("memory down")
        return True

    async def propose(self, req):
        self._maybe_fail("propose_trade")
        self.proposed.append(req)
        return ProposeTradeResult(run_id=req.run_id, symbol=req.symbol, action="buy",
                                  proposal_id="p1", status="pending", quantity=10,
                                  reason="bullish", tokens_used=40)


def engine(store, desk, **kw):
    return Engine(store, desk, worker="w1", lease_s=30,
                  config=WorkflowConfig(specialist_tries=1, retry_pause_s=0), **kw)


async def new_run(store, key="k1", symbol="LUCK"):
    return (await store.create_run(symbol=symbol, lane="pipeline", idempotency_key=key)).run_id


async def run_once(store, desk, **kw):
    run_id = await new_run(store)
    await engine(store, desk, **kw).execute(await store.claim_run("w1", 30))
    return await store.get_run(run_id)


# ---------------------------------------------------------------- idempotency


async def test_same_business_key_maps_to_one_run(store):
    a = await store.create_run(symbol="LUCK", lane="pipeline", idempotency_key="daily:x:LUCK")
    b = await store.create_run(symbol="LUCK", lane="pipeline", idempotency_key="daily:x:LUCK")
    assert a.created and not b.created and b.duplicate == "idempotency_key"
    assert a.run_id == b.run_id


async def test_redelivered_event_is_recognised_by_its_id(store):
    eid = str(uuid.uuid4())
    kw = {"symbol": "LUCK", "lane": "pipeline", "idempotency_key": "k", "consumer": "o",
          "event_id": eid}
    first, again = await store.create_run(**kw), await store.create_run(**kw)
    assert first.created and again.duplicate == "event" and again.run_id == first.run_id


# ---------------------------------------------------------------- leases and fencing


async def test_live_lease_cannot_be_claimed_twice(store):
    await new_run(store)
    assert await store.claim_run("w1", 30) is not None
    assert await store.claim_run("w2", 30) is None


async def test_expired_lease_is_reclaimed_and_the_old_owner_is_fenced_out(store):
    await new_run(store)
    old = await store.claim_run("w1", 0.2)
    await asyncio.sleep(0.4)  # w1 "stalls" past its lease
    new = await store.claim_run("w2", 30)
    assert new is not None and new.id == old.id and new.lease_token != old.lease_token
    with pytest.raises(LeaseLost):
        await store.start_step(old, "technical_analysis")
    with pytest.raises(LeaseLost):
        await store.reset_steps(old, ["judgement"])
    await store.start_step(new, "technical_analysis")  # the new owner is fine


# ---------------------------------------------------------------- the brief workflow


async def test_happy_path_runs_every_step_and_writes_one_outbox_event(store, redis):
    run = await run_once(store, FakeDesk())
    assert run["status"] == "succeeded"
    assert set(run["steps"]) == set(STEPS)
    assert {s["status"] for s in run["steps"].values()} == {"succeeded"}
    result = run["result"]
    assert result["risk"]["verdict"] == "pass" and result["risk"]["actionable"]
    assert result["tokens"]["total"] == 3 * 10 + 2 * 20 + 30 + 40  # + portfolio agent
    assert result["proposal"]["status"] == "pending"  # proposed; a human decides

    bus = EventBus(redis)
    assert await store.relay_outbox(bus.publish) == 1
    assert await store.relay_outbox(bus.publish) == 0  # published exactly once
    (_, fields), = await redis.xrange(BRIEFS)
    event = Event.model_validate_json(fields["event"])
    assert event.type == "brief.completed" and event.payload["risk_verdict"] == "pass"


async def test_unactionable_brief_never_reaches_the_portfolio_agent(store):
    desk = FakeDesk(briefs=[brief(stance=Stance.neutral)])
    run = await run_once(store, desk)
    assert run["status"] == "succeeded" and not run["result"]["risk"]["actionable"]
    assert run["result"]["proposal"] == {"action": "none", "reason": "not actionable"}
    assert desk.proposed == []


async def test_portfolio_agent_down_costs_the_proposal_not_the_brief(store):
    run = await run_once(store, FakeDesk(failing={"propose_trade"}))
    assert run["status"] == "succeeded"
    assert run["steps"]["proposal"]["status"] == "failed"
    assert run["result"]["proposal"]["action"] == "error"


async def test_resume_skips_checkpointed_steps(store):
    run_id = await new_run(store)
    # Worker 1 finishes the technical step, then dies (lease expires).
    w1 = await store.claim_run("w1", 0.2)
    req = type("R", (), {"request_id": str(run_id), "symbol": "LUCK"})()
    report = await FakeDesk().analyse("technical_analysis", req)
    await store.start_step(w1, "technical_analysis")
    await store.finish_step(w1, "technical_analysis", "succeeded",
                            result=report.model_dump(mode="json"))
    await store.start_step(w1, "fundamental_analysis")  # in flight when it died
    await asyncio.sleep(0.4)

    desk = FakeDesk()
    w2 = await store.claim_run("w2", 30)
    assert w2.attempts == 2
    await engine(store, desk).execute(w2)
    assert "technical_analysis" not in desk.calls  # not redone
    run = await store.get_run(run_id)
    assert run["status"] == "succeeded"
    assert run["steps"]["fundamental_analysis"]["attempts"] == 2


async def test_one_agent_down_still_succeeds_with_the_failure_recorded(store):
    run = await run_once(store, FakeDesk(failing={"news_analysis"}))
    assert run["status"] == "succeeded"
    assert run["steps"]["news_analysis"]["status"] == "failed"
    risk = run["result"]["risk"]
    assert "missing_analysis" in {f["rule"] for f in risk["flags"]}
    assert risk["verdict"] == "caution"


async def test_every_specialist_down_parks_the_run_for_later(store):
    desk = FakeDesk(failing=set(SPECIALISTS))
    run = await run_once(store, desk)
    assert run["status"] == "pending" and "no specialist analysis" in run["error"]
    assert await store.claim_run("w1", 30) is None  # backoff: not runnable yet
    assert "bull_case" not in desk.calls


async def test_one_sided_debate_is_allowed_but_capped(store):
    run = await run_once(store, FakeDesk(failing={"bear_case"}))
    assert run["status"] == "succeeded"
    risk = run["result"]["risk"]
    assert "one_sided_debate" in {f["rule"] for f in risk["flags"]}
    assert risk["confidence_after"] == 0.6 and run["result"]["cases_missing"] == ["bear"]


async def test_rejected_brief_goes_back_to_the_judge_with_the_reasons(store):
    desk = FakeDesk(briefs=[brief(risk_text="Drawdown of 50% is a worry"), brief()])
    run = await run_once(store, desk)
    assert run["status"] == "succeeded"
    assert run["steps"]["judgement"]["attempts"] == 2
    first, second = desk.judge_requests
    assert first.feedback == [] and "50%" in second.feedback[0]
    assert run["result"]["tokens"]["judge-agent"] == 60  # both drafts cost tokens


async def test_judge_that_never_verifies_fails_the_run(store):
    bad = brief(risk_text="Drawdown of 50% is a worry")
    run = await run_once(store, FakeDesk(briefs=[bad, bad, bad]))
    assert run["status"] == "failed" and "verification failed 3 times" in run["error"]
    assert await store.pool.fetchval("SELECT event->>'type' FROM outbox") == "brief.failed"


async def test_memory_outage_does_not_fail_the_brief(store):
    run = await run_once(store, FakeDesk(memory_up=False))
    assert run["status"] == "succeeded" and run["steps"]["remember"]["status"] == "failed"
    assert "memory down" in run["result"]["memory_error"]


async def test_run_from_an_older_workflow_version_gets_the_new_steps(store):
    run_id = await new_run(store)
    await store.pool.execute("DELETE FROM workflow_steps WHERE run_id = $1 AND step <> ALL($2)",
                             run_id, ["technical_analysis", "fundamental_analysis"])
    await engine(store, FakeDesk()).execute(await store.claim_run("w1", 30))
    run = await store.get_run(run_id)
    assert run["status"] == "succeeded" and set(run["steps"]) == set(STEPS)


async def test_run_gives_up_after_max_attempts_and_announces_failure(store):
    run_id = await new_run(store)
    await store.pool.execute("UPDATE workflow_runs SET attempts = 5 WHERE id = $1", run_id)
    await engine(store, FakeDesk(), max_run_attempts=5).execute(await store.claim_run("w1", 30))
    run = await store.get_run(run_id)
    assert run["status"] == "failed" and "gave up" in run["error"]
    event = await store.pool.fetchval("SELECT event FROM outbox")
    assert event["type"] == "brief.failed"


# ---------------------------------------------------------------- stores owned by MCP servers


async def test_evidence_store_round_trips_and_is_idempotent(store, pg_url):
    s = await PgEvidenceStore.connect(pg_url)
    ev = Evidence(symbol="LUCK", kind="valuation", values={"pe_ratio": 6.77, "n": 3},
                  source="t", as_of="2026-11-16", fetched_at="2026-11-16T00:00:00Z")
    await s.put(ev)
    await s.put(ev)
    (back,) = await s.get([ev.id, "ev_000000000000"])
    assert back.id == ev.id and back.values == {"pe_ratio": 6.77, "n": 3}
    await s.close()


async def test_pgvector_memory_ranks_by_similarity(store, pg_url):
    s = await PgMemoryStore.connect(pg_url)
    e = HashEmbedder()
    for key, text in [("m1", "cheap valuation high dividend"), ("m2", "illiquid and volatile")]:
        await s.add(MemoryRecord(key=key, symbol="LUCK", kind="brief", as_of="2026-11-16",
                                 stance="neutral", confidence=0.5, text=text),
                    e.embed([text])[0])
    hits = await s.search(e.embed(["dividend and valuation"])[0], "LUCK", 2)
    assert [h.key for h in hits] == ["m1", "m2"] and hits[0].similarity > hits[1].similarity
    await s.close()


# ---------------------------------------------------------------- consumer + scheduler


async def test_consumer_turns_events_into_runs_and_dead_letters_poison(store, redis):
    bus = EventBus(redis)
    c = Consumer(redis, REQUESTS, "orchestrator", "c1", make_request_handler(store),
                 block_ms=1, claim_idle_ms=0)
    await c.ensure_group()
    good = Event(type="brief.requested", source="t",
                 payload={"symbol": "luck", "idempotency_key": "daily:d:LUCK"})
    await bus.publish(REQUESTS, good)
    await bus.publish(REQUESTS, good)  # the same event delivered twice
    await bus.publish(REQUESTS, Event(type="brief.requested", source="t",
                                      payload={"symbol": "../etc", "idempotency_key": "bad"}))
    await c.run_once()
    assert await store.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    assert len(await redis.xrange(dlq_stream(REQUESTS))) == 1


async def test_scheduler_firing_twice_creates_each_run_once(store, redis):
    bus = EventBus(redis)
    day = datetime(2026, 11, 13).date()
    await fire(bus, day, ["LUCK", "ENGRO"])
    await fire(bus, day, ["LUCK", "ENGRO"])  # restart, manual re-fire, second replica...
    c = Consumer(redis, REQUESTS, "orchestrator", "c1", make_request_handler(store),
                 block_ms=1, claim_idle_ms=0)
    await c.ensure_group()
    await c.run_once()
    keys = await store.pool.fetch("SELECT idempotency_key FROM workflow_runs ORDER BY 1")
    assert [k[0] for k in keys] == ["daily:2026-11-13:ENGRO", "daily:2026-11-13:LUCK"]


def test_next_fire_skips_weekends_and_past_times():
    at = time(16, 15)
    friday_evening = datetime(2026, 11, 13, 17, 0, tzinfo=PKT)
    assert next_fire(friday_evening, at) == datetime(2026, 11, 16, 16, 15, tzinfo=PKT)
    monday_morning = datetime(2026, 11, 16, 9, 0, tzinfo=PKT)
    assert next_fire(monday_morning, at).date() == monday_morning.date()
