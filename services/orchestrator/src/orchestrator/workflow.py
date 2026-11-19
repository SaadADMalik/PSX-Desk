"""The brief workflow, written as straight-line code over durable steps.

    technical ─┐
    fundamental├─► verify reports ─► bull ─┐
    news ──────┘    (code)          bear ──┴─► verify cases ─► judgement ⇄ verification
                                                (code)                       (code)
                                    ─► risk_gate (code) ─► remember ─► proposal ─► done
                                                                      (only if actionable;
                                                                       a human decides)

Each `ctx.step(name, fn)` is a checkpoint: if the step already succeeded (in this
attempt or a crashed earlier one), its stored result is returned and `fn` is not
called. So this function can be re-run from the top after any crash and it
continues where it stopped. That is the idea behind Temporal's workflows; the
engine below it (engine.py) is the part Temporal would replace.

Rules this relies on:
  * steps must be safe to repeat (the in-flight one is redone after a crash);
  * anything not checkpointed must be deterministic or cheap to redo (evidence
    lookups, the verifier, the risk gate's inputs).

Grounding happens at every hand-off, not only at the end: specialists' points are
verified before the debaters see them, the debaters' arguments before the judge
sees them, and the judge's brief before anyone else does. If the brief is
unusable, the verifier's reasons go back to the judge for a rewrite.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from contracts import (
    AnalysisReport,
    AnalysisRequest,
    Brief,
    Case,
    DebateRequest,
    Evidence,
    Judgement,
    ProposeTradeRequest,
    RiskAssessment,
    Side,
    Verification,
)

from orchestrator.desk import Desk
from orchestrator.risk import RiskPolicy, assess
from orchestrator.store import DEBATE, SPECIALISTS, LeaseLost, Run, Step, Store
from orchestrator.verify import verify_brief, verify_case, verify_report


class Park(Exception):
    """Not now: give the run back and try later (e.g. every agent is down)."""

    def __init__(self, reason: str, reset: list[str]) -> None:
        super().__init__(reason)
        self.reason, self.reset = reason, reset


class StepFailed(Exception):
    def __init__(self, step: str, error: str, attempts: int) -> None:
        super().__init__(f"{step} failed (attempt {attempts}): {error}")
        self.step, self.error, self.attempts = step, error, attempts


Chaos = Callable[[str], Awaitable[None]]


def describe(e: BaseException) -> str:
    """Readable cause. anyio task groups (the MCP client uses them) wrap the real error
    in an ExceptionGroup whose own message says nothing useful."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


@dataclass
class RunContext:
    store: Store
    run: Run
    steps: dict[str, Step]
    chaos: Chaos | None = None
    resumed: list[str] = field(default_factory=list)

    async def step(self, name: str, fn: Callable[[], Awaitable[Any]], *,
                   optional: bool = False) -> Any:
        done = self.steps.get(name)
        if done is not None and done.status == "succeeded":
            self.resumed.append(name)
            return done.result
        attempts = await self.store.start_step(self.run, name)
        try:
            result = await fn()
        except LeaseLost:
            raise
        except Exception as e:
            error = describe(e)[:4000]
            checkpoint = getattr(e, "checkpoint", None)
            await self.store.finish_step(self.run, name, "failed", result=checkpoint,
                                         error=error)
            self.steps[name] = Step(name, "failed", attempts, checkpoint, error)
            if optional:
                return None
            raise StepFailed(name, error, attempts) from e
        await self.store.finish_step(self.run, name, "succeeded", result=result)
        self.steps[name] = Step(name, "succeeded", attempts, result, None)
        if self.chaos is not None:
            await self.chaos(name)
        return result

    async def reset(self, names: list[str]) -> None:
        await self.store.reset_steps(self.run, names)
        for n in names:
            s = self.steps[n]
            self.steps[n] = Step(n, "pending", s.attempts, s.result, s.error)

    def failed(self, names: list[str]) -> dict[str, str]:
        return {n: self.steps[n].error or "failed" for n in names
                if self.steps[n].status == "failed"}


@dataclass(frozen=True)
class WorkflowConfig:
    specialist_tries: int = 2
    max_judgements: int = 3
    retry_pause_s: float = 2.0
    policy: RiskPolicy = field(default_factory=RiskPolicy)


async def _retrying(fn: Callable[[], Awaitable[Any]], tries: int, pause_s: float) -> Any:
    for attempt in range(1, tries + 1):
        try:
            return await fn()
        except Exception:
            if attempt == tries:
                raise
            await asyncio.sleep(pause_s * attempt)


async def brief_workflow(ctx: RunContext, desk: Desk, req: AnalysisRequest,
                         cfg: WorkflowConfig = WorkflowConfig()) -> dict:
    # ---- 1. specialists, in parallel; partial failure is allowed
    def specialist(skill: str):
        async def call() -> dict:
            report = await _retrying(lambda: desk.analyse(skill, req), cfg.specialist_tries,
                                     cfg.retry_pause_s)
            return report.model_dump(mode="json")
        return ctx.step(skill, call, optional=True)

    raw = await asyncio.gather(*(specialist(s) for s in SPECIALISTS))
    reports = [AnalysisReport.model_validate(r) for r in raw if r]
    failures = ctx.failed(SPECIALISTS)
    if not reports:
        raise Park(f"no specialist analysis: {failures}", reset=SPECIALISTS)

    # Facts come from their source, not from the reports that cite them.
    evidence = await desk.evidence(sorted({i for r in reports for i in r.evidence_ids}))
    checks: dict[str, Verification] = {}
    verified_reports = []
    for r in reports:
        r, checks[r.agent] = verify_report(r, evidence)
        if r.points:
            verified_reports.append(r)
        else:
            failures[r.agent] = "every point failed verification"
    if not verified_reports:
        raise Park("no verifiable specialist analysis", reset=SPECIALISTS)

    # ---- 2. debate: bull and bear in parallel, blind to each other
    debate = DebateRequest(symbol=req.symbol, request_id=req.request_id, lane=req.lane,
                           reports=verified_reports, missing=failures)

    def argue(side: Side, step: str):
        async def call() -> dict:
            return (await desk.argue(side, debate)).model_dump(mode="json")
        return ctx.step(step, call, optional=True)

    raw_cases = await asyncio.gather(argue(Side.bull, "bull_case"), argue(Side.bear, "bear_case"))
    cases: list[Case] = []
    for c in raw_cases:
        if c:
            case, checks[f"{c['side']}_case"] = verify_case(Case.model_validate(c), evidence)
            if case.arguments:
                cases.append(case)
    cases_missing = [s.value for s in Side if s.value not in {c.side.value for c in cases}]
    if not cases:
        raise Park(f"no debate case: {ctx.failed(DEBATE)}", reset=DEBATE)

    # ---- 3. judgement, checked by the verifier; rejected briefs go back once more
    judge_tokens = 0
    while True:
        previous = ctx.steps["verification"]
        feedback = (previous.result or {}).get("feedback", []) if previous.status == "failed" \
            else []

        async def call_judge(feedback: list[str] = feedback) -> dict:
            j = await desk.judge(debate.model_copy(update={"cases": cases,
                                                           "feedback": feedback}))
            return j.model_dump(mode="json")

        judgement = Judgement.model_validate(await ctx.step("judgement", call_judge))
        judge_tokens += judgement.tokens_used  # rejected drafts cost tokens too

        async def check(judgement: Judgement = judgement) -> dict:
            brief, v = verify_brief(judgement.brief, evidence)
            return {"brief": brief.model_dump(mode="json"), "verification": v.model_dump()}

        try:
            verified = await ctx.step("verification", check)
            break
        except StepFailed as e:
            if ctx.steps["judgement"].attempts >= cfg.max_judgements:
                raise StepFailed("verification", e.error, cfg.max_judgements) from e
            await ctx.reset(["judgement"])
    brief = Brief.model_validate(verified["brief"])
    verification = Verification.model_validate(verified["verification"])

    # ---- 4. risk gate (code), then memory (best effort)
    async def gate() -> dict:
        b, risk = assess(brief, evidence, failures=failures, cases_missing=cases_missing,
                         verification=verification, policy=cfg.policy)
        return {"brief": b.model_dump(mode="json"), "risk": risk.model_dump(mode="json")}

    gated = await ctx.step("risk_gate", gate)
    final = Brief.model_validate(gated["brief"])
    risk = RiskAssessment.model_validate(gated["risk"])
    as_of = max((ev.as_of for ev in evidence.values()), default="")

    async def remember() -> dict:
        return {"stored": await desk.remember(f"brief:{ctx.run.id}", final, as_of)}

    await ctx.step("remember", remember, optional=True)

    # ---- 5. a paper-trade proposal, only for briefs the risk gate cleared. Optional:
    # a missing proposal never costs the brief. Idempotent in the broker (run:<id>).
    async def propose() -> dict:
        if not risk.actionable:
            return {"action": "none", "reason": "not actionable"}
        r = await desk.propose(ProposeTradeRequest(
            run_id=str(ctx.run.id), symbol=req.symbol, brief=final.model_dump(mode="json"),
            risk=risk.model_dump(mode="json")))
        return r.model_dump(mode="json")

    proposal = await ctx.step("proposal", propose, optional=True)

    out = _result(req, final, risk, verification, checks, reports, cases, failures,
                  cases_missing, judgement, evidence, judge_tokens,
                  memory_error=ctx.failed(["remember"]).get("remember"))
    out["proposal"] = proposal or {"action": "error", "reason": ctx.failed(["proposal"])
                                   .get("proposal", "failed")}
    out["tokens"]["portfolio-agent"] = (proposal or {}).get("tokens_used", 0)
    out["tokens"]["total"] += out["tokens"]["portfolio-agent"]
    return out


def _result(req, brief: Brief, risk: RiskAssessment, verification: Verification,
            checks: dict[str, Verification], reports: list[AnalysisReport], cases: list[Case],
            failures: dict[str, str], cases_missing: list[str], judgement: Judgement,
            evidence: dict[str, Evidence], judge_tokens: int, memory_error: str | None) -> dict:
    tokens = {r.agent: r.tokens_used for r in reports}
    tokens |= {f"{c.side.value}-agent": c.tokens_used for c in cases}
    tokens["judge-agent"] = judge_tokens
    tokens["total"] = sum(tokens.values())
    return {
        "request_id": req.request_id,
        "brief": brief.model_dump(mode="json"),
        "risk": risk.model_dump(mode="json"),
        "verification": verification.model_dump(mode="json"),
        "upstream_checks": {k: {"checked": v.checked, "verified": v.verified,
                                "rejected": [c.model_dump() for c in v.rejected]}
                            for k, v in checks.items()},
        "reports": [r.model_dump(mode="json") for r in reports],
        "cases": [c.model_dump(mode="json") for c in cases],
        "failures": failures,
        "cases_missing": cases_missing,
        "prior_views": [p.model_dump(mode="json") for p in judgement.prior_views],
        "lean": {"stance": judgement.lean, "reason": judgement.lean_reason,
                 "override_reason": judgement.override_reason},
        "models": {**{f"{c.side.value}-agent": c.model for c in cases},
                   "judge-agent": judgement.model},
        "memory_error": memory_error,
        "evidence": {k: v.model_dump(mode="json") for k, v in evidence.items()},
        "tokens": tokens,
    }
