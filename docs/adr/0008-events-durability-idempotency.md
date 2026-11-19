# ADR 0008: Events, durable workflows, and idempotency

**Status:** accepted

## Context
The first orchestrator ran a brief inside one HTTP request, in memory. A crash lost the work, a
retry repeated it, and nothing could start work except a person.

## Decisions

### Event bus: Redis Streams with consumer groups (`libs/eventbus`)
- Streams: `desk.requests` (brief.requested), `desk.briefs` (brief.completed /
  brief.failed), `desk.market` (market.closed). Each has a `.dlq` dead-letter stream.
- Delivery is **at least once**. Unacked messages stay pending and are reclaimed
  (`XAUTOCLAIM`) by any consumer after 30 s, so a crashed consumer loses nothing.
- **Poison messages** (invalid payloads) go straight to the DLQ; other failures are
  retried and dead-lettered after 3 deliveries.
- Trace context rides inside the event (`traceparent`), so a trace continues from
  the scheduler through the bus into the workflow.
- Why not Kafka: same consumer-group model, and we already run Redis. We give up
  long retention and partitioned throughput, which this desk does not need.

### Orchestration stays central; events are used at the edges
The scheduler publishes requests; the orchestrator announces outcomes. Inside a
brief, the orchestrator still calls specialists directly over A2A (ADR 0007).
Choreography, where agents react to each other's events, would hide the flow of
a brief across many consumers.

### Durable workflows in Postgres (`services/orchestrator/store.py`, `engine.py`)
- A brief is a **run** with **steps**; each step's result is checkpointed, so a
  resumed run skips what already succeeded.
- A worker holds a **lease** (30 s, renewed every 10 s). If the worker dies, the lease
  expires and any worker can claim the run with `FOR UPDATE SKIP LOCKED`.
- Every claim issues a new **fencing token**; every write checks it in the same
  statement. A worker that stalled past its lease cannot overwrite its successor.
- Every claim counts as an attempt, including resumes. After 5, the run fails, so a
  run that crashes its worker every time cannot loop forever.
- If every specialist is down, the run is **parked** with exponential backoff
  rather than failed.

### Idempotency, at two layers
| Duplicate | Caught by |
|---|---|
| The same event delivered twice | inbox: `processed_events (consumer, event_id)` |
| Two different requests for the same work (scheduler fired twice, client retried) | `UNIQUE idempotency_key`: `daily:<date>:<symbol>`, or `api:<Idempotency-Key header>` |

### Transactional outbox
A run's final state and its `brief.completed` event are written in **one
transaction**; a relay publishes the outbox afterwards. Without it, a crash between
"commit" and "publish" either loses the event or announces a result that was rolled
back. The relay can still publish an event twice (crash after publish, before
marking), which is fine because consumers dedupe on `event_id`.

### Migrations instead of init scripts
`db/init` ran only when the volume was created, so new tables would never reach an
existing database. `db/migrate.sh` applies versioned files once each, each in one
transaction with its `schema_migrations` row, and re-applies role passwords every run.

## Measured (live drills, 2026-11-16)
| Drill | Result |
|---|---|
| Scheduler fired twice for LUCK, ENGRO | 4 events consumed, 2 runs; both succeeded on attempt 1 (15 s, 18 s), 2 `brief.completed` events published once each |
| Orchestrator killed right after checkpointing `technical_analysis` (HBL) | Restarted by Docker in ~2 s; run reclaimed ~27 s after the crash when its lease expired; attempt 2 skipped technical, re-ran the in-flight fundamentals step (served from cache, 0 tokens); brief done 33 s after the request |
| Invalid symbol and non-JSON event on `desk.requests` | Both in `desk.requests.dlq` on first delivery with the reason; 0 pending; no run created |
| Same `Idempotency-Key` POSTed twice | Same `run_id`, `duplicate: idempotency_key`, HTTP 200 |
| Trace of a scheduled run | One trace, root `scheduler.fire`, 353 spans across 6 services |

Found by the drills, not the tests: redis-py 8 defaults `socket_timeout` to 5 s,
equal to the consumer's 5 s blocking read, so every idle poll raised `TimeoutError`.
Clients that block now set `socket_timeout=30`, and `Consumer` rejects a block time
that isn't below the client's socket timeout.

## Consequences
- The API returns `202 + run_id` immediately; clients poll `GET /v1/runs/{id}`.
- Our engine is about 200 lines. Temporal provides the same guarantees and much more
  (timers, signals, versioning, visibility). Ours exists to make each mechanism
  visible; in production, adopt Temporal.
- Holding a transaction open while the relay publishes to Redis is fine at this
  volume; at scale, mark rows "claimed" first and publish outside the transaction.
- PSX holidays are not modelled by the scheduler.
