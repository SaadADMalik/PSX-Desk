"""The workflow engine: claims runs and drives a workflow over them.

    claim run (lease + fencing token)
      ├─ heartbeat: renew the lease while working
      ├─ run the workflow (workflow.py); each step is checkpointed as it finishes
      ├─ Park       ─► give the run back, retry later with backoff
      ├─ StepFailed ─► park, or fail the run once the step is out of attempts
      └─ finish run + outbox event, in one transaction

The engine knows nothing about briefs: it runs any workflow function
`(ctx, desk, request, config) -> result`. That split (engine vs workflow
definition) is the same one Temporal makes.

Crash anywhere and the lease expires; the next claim resumes from the checkpoints.
A run is attempted at most `max_run_attempts` times (every claim counts, including
resumes after a crash), so a run that crashes its worker every time cannot loop
forever: it fails and announces `brief.failed`.
"""

import asyncio
import logging
import os

from contracts import AnalysisRequest, Lane
from contracts.events import BRIEFS, Event
from opentelemetry import propagate, trace

from orchestrator.desk import Desk
from orchestrator.store import SPECIALISTS, LeaseLost, Run, Store
from orchestrator.workflow import (
    Chaos,
    Park,
    RunContext,
    StepFailed,
    WorkflowConfig,
    brief_workflow,
)

log = logging.getLogger(__name__)
_tracer = trace.get_tracer("orchestrator.engine")



class Engine:
    def __init__(self, store: Store, desk: Desk, *, worker: str, lease_s: float = 30.0,
                 concurrency: int = 2, max_run_attempts: int = 5, max_step_attempts: int = 3,
                 specialist_tries: int = 2, base_backoff_s: float = 15.0, poll_s: float = 1.0,
                 chaos: Chaos | None = None, workflow=brief_workflow,
                 config: WorkflowConfig | None = None) -> None:
        self.store, self.desk, self.worker = store, desk, worker
        self.lease_s, self.concurrency = lease_s, concurrency
        self.max_run_attempts, self.max_step_attempts = max_run_attempts, max_step_attempts
        self.workflow = workflow
        self.config = config or WorkflowConfig(specialist_tries=specialist_tries)
        self.base_backoff_s, self.poll_s = base_backoff_s, poll_s
        self.chaos = chaos

    # ------------------------------------------------------------ loop

    async def run(self, stop: asyncio.Event) -> None:
        slots = asyncio.Semaphore(self.concurrency)
        tasks: set[asyncio.Task] = set()
        while not stop.is_set():
            await slots.acquire()
            try:
                run = await self.store.claim_run(self.worker, self.lease_s)
            except Exception:
                log.exception("claim failed")
                run = None
            if run is None:
                slots.release()
                await asyncio.sleep(self.poll_s)
                continue
            task = asyncio.create_task(self._guarded(run, slots))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        for t in tasks:
            t.cancel()

    async def _guarded(self, run: Run, slots: asyncio.Semaphore) -> None:
        try:
            await self.execute(run)
        finally:
            slots.release()

    # ------------------------------------------------------------ one run

    async def execute(self, run: Run) -> None:
        # Continue the trace of whoever created the run, even if it was days ago or
        # this is the third worker to pick it up.
        ctx = propagate.extract({"traceparent": run.trace_parent} if run.trace_parent else {})
        with _tracer.start_as_current_span("workflow.run", context=ctx) as span:
            span.set_attributes({"workflow.run_id": str(run.id), "workflow.attempt": run.attempts,
                                 "workflow.worker": self.worker, "psx.symbol": run.symbol})
            heartbeat = asyncio.create_task(self._heartbeat(run))
            try:
                await self._drive(run)
            except LeaseLost as e:
                log.warning("%s", e)  # someone else owns it now; stop quietly
            except Exception as e:
                log.exception("run %s crashed", run.id)
                span.record_exception(e)
                try:
                    await self.store.park(run, delay_s=self._backoff(run.attempts),
                                          error=f"{type(e).__name__}: {e}", reset=SPECIALISTS)
                except LeaseLost:
                    pass
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, run: Run) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            if not await self.store.renew_lease(run, self.lease_s):
                log.warning("run %s: lease lost; further writes will be rejected", run.id)
                return

    async def _drive(self, run: Run) -> None:
        if run.attempts > self.max_run_attempts:
            await self._fail(run, f"gave up after {run.attempts - 1} attempts")
            return

        await self.store.ensure_steps(run.id)  # runs created by an older workflow version
        ctx = RunContext(self.store, run, await self.store.steps(run.id), chaos=self.chaos)
        done = [s for s, v in ctx.steps.items() if v.status == "succeeded"]
        if done:
            log.info("run %s attempt %d: resuming, %s already checkpointed",
                     run.id, run.attempts, done)
        req = AnalysisRequest(symbol=run.symbol, request_id=str(run.id), lane=Lane(run.lane))
        try:
            result = await self.workflow(ctx, self.desk, req, self.config)
        except Park as p:
            await self.store.park(run, delay_s=self._backoff(run.attempts), error=p.reason,
                                  reset=p.reset)
            log.warning("run %s parked: %s", run.id, p.reason)
            return
        except StepFailed as e:
            if e.attempts >= self.max_step_attempts:
                await self._fail(run, f"{e.step} failed {e.attempts} times: {e.error}")
            else:
                await self.store.park(run, delay_s=self._backoff(e.attempts), error=str(e),
                                      reset=[e.step])
            return

        brief, risk = result["brief"], result["risk"]
        await self.store.finish_run(run, "succeeded", result=result, stream=BRIEFS, event=Event(
            type="brief.completed", source="orchestrator", payload={
                "run_id": str(run.id), "idempotency_key": run.idempotency_key,
                "symbol": run.symbol, "stance": brief["stance"],
                "confidence": brief["confidence"], "risk_verdict": risk["verdict"],
                "actionable": risk["actionable"], "tokens": result["tokens"]["total"],
                "proposal": result.get("proposal", {}).get("action")}))
        log.info("run %s succeeded on attempt %d", run.id, run.attempts)

    async def _fail(self, run: Run, error: str) -> None:
        await self.store.finish_run(run, "failed", error=error, stream=BRIEFS, event=Event(
            type="brief.failed", source="orchestrator", payload={
                "run_id": str(run.id), "idempotency_key": run.idempotency_key,
                "symbol": run.symbol, "error": error}))
        log.error("run %s failed: %s", run.id, error)

    def _backoff(self, attempt: int) -> float:
        return min(600.0, self.base_backoff_s * 2 ** max(0, attempt - 1))


def redis_chaos(redis) -> Chaos:
    """Drill hook. `SET drill:crash_after_step <step>` makes the next worker to checkpoint
    that step exit immediately, as if the container was killed. Only the worker whose
    DELETE succeeds crashes, so the drill fires exactly once."""

    async def maybe_crash(step: str) -> None:
        key = "drill:crash_after_step"
        if await redis.get(key) == step and await redis.delete(key) == 1:
            log.critical("DRILL: crashing after checkpointing step %s", step)
            os._exit(70)

    return maybe_crash
