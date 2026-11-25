"""Durable workflow state in Postgres.

The ideas, each implemented in one place below:

* Idempotent creation (`create_run`). Two layers catch two kinds of duplicate:
    - inbox: the same *event* delivered twice (at-least-once bus) -> processed_events;
    - business key: two *different* requests for the same work (the scheduler fired
      twice, a client retried a POST) -> UNIQUE idempotency_key.
* Leases (`claim_run`). A worker owns a run until `lease_until`, renewing it while it
  works. If the worker dies, the lease expires and any worker may claim the run.
  `FOR UPDATE SKIP LOCKED` lets many workers claim concurrently without blocking or
  double-claiming.
* Fencing tokens. Every claim issues a fresh `lease_token`. Every write checks it
  in the same statement. A worker that stalled past its lease (GC pause, network
  partition) and wakes up holding an old token has its writes rejected (`LeaseLost`).
* Checkpoints (`workflow_steps`). A succeeded step keeps its result and is never run
  again, so a resumed run continues where it stopped.
* Transactional outbox (`finish_run`). The run's final state and the event announcing
  it commit together or not at all. A relay publishes the event afterwards.

Temporal or a similar engine is the production answer to all of this. This is the
smallest version that makes each mechanism visible.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
from contracts.events import Event

# The brief workflow's steps, in order. Steps are rows, so a run created by an older
# version of the workflow gets any new steps added when it is next claimed
# (`ensure_steps`). Changing what an *existing* step means is the hard part of
# workflow versioning; Temporal has a whole API for it. We only ever add steps.
SPECIALISTS = ["technical_analysis", "fundamental_analysis", "news_analysis"]
DEBATE = ["bull_case", "bear_case"]
STEPS = [*SPECIALISTS, *DEBATE, "judgement", "verification", "announcements", "risk_gate",
         "remember", "proposal"]
TERMINAL = {"succeeded", "failed"}


class LeaseLost(Exception):
    """This worker no longer owns the run; another worker has claimed it."""


@dataclass(frozen=True)
class Run:
    id: uuid.UUID
    idempotency_key: str
    symbol: str
    lane: str
    status: str
    attempts: int
    lease_token: uuid.UUID | None
    trace_parent: str | None


@dataclass(frozen=True)
class Step:
    step: str
    status: str
    attempts: int
    result: Any
    error: str | None


@dataclass(frozen=True)
class Created:
    run_id: uuid.UUID
    created: bool
    duplicate: str | None = None  # "event" | "idempotency_key" | None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                              schema="pg_catalog")


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=8, init=_init_conn))

    async def close(self) -> None:
        await self.pool.close()

    # ------------------------------------------------------------ creation

    async def create_run(self, *, symbol: str, lane: str, idempotency_key: str,
                         trace_parent: str | None = None, consumer: str | None = None,
                         event_id: str | None = None) -> Created:
        async with self.pool.acquire() as c, c.transaction():
            if consumer and event_id:
                fresh = await c.fetchval(
                    "INSERT INTO processed_events (consumer, event_id) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING RETURNING 1", consumer, uuid.UUID(event_id))
                if not fresh:
                    existing = await c.fetchval(
                        "SELECT id FROM workflow_runs WHERE idempotency_key = $1", idempotency_key)
                    return Created(existing, False, "event")
            run_id = await c.fetchval(
                "INSERT INTO workflow_runs (kind, idempotency_key, symbol, lane, trace_parent) "
                "VALUES ('brief', $1, $2, $3, $4) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                idempotency_key, symbol, lane, trace_parent)
            if run_id is None:
                existing = await c.fetchval(
                    "SELECT id FROM workflow_runs WHERE idempotency_key = $1", idempotency_key)
                return Created(existing, False, "idempotency_key")
            await c.executemany("INSERT INTO workflow_steps (run_id, step) VALUES ($1, $2)",
                                [(run_id, s) for s in STEPS])
            return Created(run_id, True)

    # ------------------------------------------------------------ leases

    async def claim_run(self, worker: str, lease_s: float) -> Run | None:
        row = await self.pool.fetchrow(
            """
            UPDATE workflow_runs r
               SET status = 'running', lease_owner = $1, lease_token = gen_random_uuid(),
                   lease_until = now() + make_interval(secs => $2),
                   attempts = attempts + 1, updated_at = now()
             WHERE r.id = (
                   SELECT id FROM workflow_runs
                    WHERE status IN ('pending', 'running')
                      AND next_attempt_at <= now()
                      AND (lease_until IS NULL OR lease_until < now())
                    ORDER BY (lane = 'interactive') DESC, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
            RETURNING id, idempotency_key, symbol, lane, status, attempts, lease_token,
                      trace_parent
            """, worker, float(lease_s))
        return Run(**dict(row)) if row else None

    async def renew_lease(self, run: Run, lease_s: float) -> bool:
        return bool(await self.pool.fetchval(
            "UPDATE workflow_runs SET lease_until = now() + make_interval(secs => $3), "
            "updated_at = now() WHERE id = $1 AND lease_token = $2 AND status = 'running' "
            "RETURNING 1", run.id, run.lease_token, float(lease_s)))

    # ------------------------------------------------------------ steps (fenced)

    async def steps(self, run_id: uuid.UUID) -> dict[str, Step]:
        rows = await self.pool.fetch(
            "SELECT step, status, attempts, result, error FROM workflow_steps WHERE run_id = $1",
            run_id)
        return {r["step"]: Step(**dict(r)) for r in rows}

    async def ensure_steps(self, run_id: uuid.UUID) -> None:
        await self.pool.executemany(
            "INSERT INTO workflow_steps (run_id, step) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            [(run_id, s) for s in STEPS])

    async def reset_steps(self, run: Run, steps: list[str]) -> None:
        """Make steps runnable again within this attempt (e.g. the judge, after the
        verifier rejected its brief). Fenced like every other write."""
        ok = await self.pool.fetchval(
            """
            WITH owner AS (SELECT 1 FROM workflow_runs WHERE id = $1 AND lease_token = $2)
            UPDATE workflow_steps SET status = 'pending'
             WHERE run_id = $1 AND step = ANY($3) AND EXISTS (SELECT 1 FROM owner)
            RETURNING 1
            """, run.id, run.lease_token, steps)
        if not ok:
            raise LeaseLost(f"run {run.id}: lost lease before resetting {steps}")

    async def start_step(self, run: Run, step: str) -> int:
        attempts = await self.pool.fetchval(
            """
            UPDATE workflow_steps s
               SET status = 'running', attempts = s.attempts + 1, started_at = now(), error = NULL
              FROM workflow_runs r
             WHERE s.run_id = $1 AND s.step = $3 AND r.id = s.run_id AND r.lease_token = $2
            RETURNING s.attempts
            """, run.id, run.lease_token, step)
        if attempts is None:
            raise LeaseLost(f"run {run.id}: lost lease before step {step}")
        return attempts

    async def finish_step(self, run: Run, step: str, status: str, *, result: Any = None,
                          error: str | None = None) -> None:
        ok = await self.pool.fetchval(
            """
            UPDATE workflow_steps s
               SET status = $4, result = $5, error = $6, finished_at = now()
              FROM workflow_runs r
             WHERE s.run_id = $1 AND s.step = $3 AND r.id = s.run_id AND r.lease_token = $2
            RETURNING 1
            """, run.id, run.lease_token, step, status, result, error)
        if not ok:
            raise LeaseLost(f"run {run.id}: lost lease before finishing step {step}")

    # ------------------------------------------------------------ run outcomes (fenced)

    async def park(self, run: Run, *, delay_s: float, error: str, reset: list[str]) -> None:
        """Give the run back for a later attempt, e.g. when every agent is down."""
        async with self.pool.acquire() as c, c.transaction():
            ok = await c.fetchval(
                "UPDATE workflow_runs SET status = 'pending', lease_owner = NULL, "
                "lease_token = NULL, lease_until = NULL, error = $3, updated_at = now(), "
                "next_attempt_at = now() + make_interval(secs => $4) "
                "WHERE id = $1 AND lease_token = $2 RETURNING 1",
                run.id, run.lease_token, error, float(delay_s))
            if not ok:
                raise LeaseLost(f"run {run.id}: lost lease before parking")
            await c.execute("UPDATE workflow_steps SET status = 'pending' "
                            "WHERE run_id = $1 AND step = ANY($2) AND status <> 'succeeded'",
                            run.id, reset)

    async def finish_run(self, run: Run, status: str, *, stream: str, event: Event,
                         result: Any = None, error: str | None = None) -> None:
        """Terminal state and its announcement commit atomically (transactional outbox)."""
        async with self.pool.acquire() as c, c.transaction():
            ok = await c.fetchval(
                "UPDATE workflow_runs SET status = $3, result = $4, error = $5, "
                "finished_at = now(), updated_at = now(), lease_owner = NULL, "
                "lease_until = NULL WHERE id = $1 AND lease_token = $2 RETURNING 1",
                run.id, run.lease_token, status, result, error)
            if not ok:
                raise LeaseLost(f"run {run.id}: lost lease before finishing")
            await c.execute("INSERT INTO outbox (event_id, stream, event) VALUES ($1, $2, $3)",
                            uuid.UUID(event.event_id), stream, event.model_dump(mode="json"))

    # ------------------------------------------------------------ outbox relay

    async def relay_outbox(self, publish: Callable[[str, Event], Awaitable[Any]],
                           limit: int = 50) -> int:
        """Publish unpublished events, oldest first. A crash after publishing but before
        marking them republishes them: consumers dedupe on event_id."""
        async with self.pool.acquire() as c, c.transaction():
            rows = await c.fetch(
                "SELECT id, stream, event FROM outbox WHERE published_at IS NULL "
                "ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED", limit)
            for r in rows:
                await publish(r["stream"], Event.model_validate(r["event"]))
            if rows:
                await c.execute("UPDATE outbox SET published_at = now() WHERE id = ANY($1)",
                                [r["id"] for r in rows])
            return len(rows)

    # ------------------------------------------------------------ reads

    async def get_run(self, run_id: uuid.UUID) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT id, idempotency_key, symbol, lane, status, attempts, error, result, "
            "lease_owner, created_at, finished_at FROM workflow_runs WHERE id = $1", run_id)
        if row is None:
            return None
        steps = await self.steps(run_id)
        out = {k: (str(v) if k in {"id", "created_at", "finished_at"} and v else v)
               for k, v in dict(row).items()}
        out["steps"] = {s.step: {"status": s.status, "attempts": s.attempts, "error": s.error}
                        for s in steps.values()}
        return out

    async def latest_briefs(self, symbols: list[str] | None) -> list[dict]:
        """The desk's most recent finished view per symbol: brief, risk, debate, proposal."""
        rows = await self.pool.fetch(
            "SELECT DISTINCT ON (symbol) symbol, id, finished_at, "
            "       result->'brief' AS brief, result->'risk' AS risk, "
            "       result->'proposal' AS proposal, result->'cases' AS cases, "
            "       result->'reports' AS reports, result->'prior_views' AS prior_views, "
            "       result->'verification' AS verification, result->'failures' AS failures, "
            "       result->'lean' AS lean "
            "FROM workflow_runs WHERE status = 'succeeded' "
            "  AND ($1::text[] IS NULL OR symbol = ANY($1)) "
            "ORDER BY symbol, finished_at DESC", symbols)
        out = []
        for r in rows:
            d = dict(r)  # jsonb columns arrive decoded (see the codec in connect)
            d["id"], d["finished_at"] = str(r["id"]), r["finished_at"].isoformat()
            for rep in d.get("reports") or []:
                rep.pop("points", None)
                rep.pop("evidence_ids", None)
            out.append(d)
        return out

    async def list_runs(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, idempotency_key, symbol, status, attempts, created_at, finished_at, "
            "       result->'brief'->>'stance' AS stance, "
            "       (result->'brief'->>'confidence')::float AS confidence, "
            "       result->'risk'->>'verdict' AS risk, "
            "       (result->'risk'->>'actionable')::bool AS actionable, "
            "       result->'proposal'->>'action' AS proposal "
            "FROM workflow_runs ORDER BY created_at DESC LIMIT $1", limit)
        return [{k: (str(v) if v is not None and k in {"id", "created_at", "finished_at"}
                     else v) for k, v in dict(r).items()} for r in rows]
