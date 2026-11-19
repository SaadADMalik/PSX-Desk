"""The debate desk: a bull, a bear and a judge, as three skills of one A2A service.

    reports + evidence ──► bull_case  (prefers Qwen)        ─┐  in parallel, blind
                       └─► bear_case  (prefers gpt-oss-20b) ─┤  to each other
                                                             ▼
             cases + evidence + prior views ──► judgement (gpt-oss-120b) ──► Brief

Why a debate: one model asked "what do you think?" tends to agree with whatever
the material leans towards. Making one agent argue *for* and another *against*,
independently, surfaces the strongest points on both sides; the judge then has
to weigh them.

Why different models: two prompts on the same model share its blind spots.
Different model families make correlated mistakes less likely. They also draw
on separate per-model token budgets. `prefer_model` keeps failover: if Qwen is
down, the bull still runs on the next model in the tier.

Why one service with three skills, not three: an agent is a role (prompt, model,
contract), not a container. These three share code, data access and permissions
(read-only), and change together, so one deployable is enough. The orchestrator
routes by skill, so splitting them later changes configuration, not callers.

Data access is by reference: requests carry evidence *ids*; this service fetches
the records from the market-data MCP server (get_evidence) and the judge's prior
views from the memory MCP server (recall). Both calls are made by code, not by
the model: the model gets no tools at all.
"""

import json
import logging
import os

from agentkit import GatewayClient, parse_with_repair
from agentkit.a2a import A2AService, InvalidRequest, Skill, build_a2a_app
from agentkit.analyst import NUMBERS_RULE, serve
from agentkit.mcp_tools import McpToolset
from agentkit.structured import cap_claims
from contracts import (
    Brief,
    Case,
    ChatMessage,
    Claim,
    DebateRequest,
    Evidence,
    Judgement,
    PriorView,
    Side,
    Stance,
    Tier,
)
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("debate_agent")

GATEWAY = os.getenv("LLM_GATEWAY_URL", "http://127.0.0.1:8100")
MARKET_DATA = os.getenv("MARKET_DATA_MCP_URL", "http://127.0.0.1:8300/mcp")
MEMORY = os.getenv("MEMORY_MCP_URL", "http://127.0.0.1:8500/mcp")

PREFER = {Side.bull: "qwen/qwen3.8-27b", Side.bear: "openai/gpt-oss-20b"}
# The same allowance for both sides, and under Qwen's 1,000 output tokens/min on Groq.
# Measured cases use 290-590 output tokens.
CASE_MAX_TOKENS = 900

COMMON = """Rules:
- Use only the evidence and reports given. No outside knowledge; do not name the company.
- Every argument cites the evidence ids it relies on.
- {numbers}
- This is research, not investment advice."""

CASE = """You are the {side} analyst on a Pakistan Stock Exchange (PSX) research desk.
Make the strongest HONEST {side} case for {symbol} from the material below. Another
analyst is independently making the opposite case; a judge will weigh both.

- Argue from the evidence, not from optimism or pessimism.
- weight each argument by how much the evidence behind it really carries:
    1 = weak or indirect   2 = solid   3 = strong and direct
- against: the 1-3 strongest points in the evidence AGAINST your side, each with
  the same 1-3 weight. Be as honest about these as about your own arguments:
  the desk computes how strong your case is from both lists.
- concession: the single strongest point against your side, in one sentence.
  Judges trust analysts who name it.

{common}

Reply in plain text with ONLY this JSON object:
{{"thesis": str,
 "arguments": [{{"text": str, "evidence_ids": [str, ...], "weight": 1|2|3}}, ...],  (1-5 items)
 "against": [{{"text": str, "weight": 1|2|3}}, ...],  (1-3 items)
 "concession": str}}"""

JUDGE = """You are the head of research on a Pakistan Stock Exchange (PSX) desk. A bull
and a bear analyst argued opposite sides of {symbol}, independently, from the same
evidence. Write the desk's research brief.

- Weigh arguments by their evidence, not by eloquence.
- desk_lean is where the desk stands before you decide, computed mechanically
  from the specialists' stances and the two cases' strength. Start from it.
  You may reach a different stance, but only for a concrete reason in the
  evidence that the lean cannot see (name the point and its evidence id). If you
  do, explain it in override_reason. If you agree with the lean, leave
  override_reason empty.
- Neutral is right when the material is genuinely balanced or thin. It is not a
  safe default: a desk that never commits tells the reader nothing, and an
  unexplained neutral is as much an error as an unjustified call.
- Where the specialists or the debaters disagree, say so in the thesis.
- If an analysis or a case is missing, say so and lower your confidence.
- prior_views are the desk's own earlier conclusions. They are context, not
  evidence: never cite them. If today's stance differs from a recent prior view,
  say in the thesis what changed.
- Carry the specialists' data gaps into data_gaps.
{feedback}
{common}

Reply in plain text with ONLY this JSON object:
{{"symbol": str, "stance": "bullish"|"neutral"|"bearish", "confidence": 0..1,
 "thesis": str, "override_reason": str,
 "key_points": [{{"text": str, "evidence_ids": [str, ...]}}, ...],   (1-6 items)
 "risks":      [{{"text": str, "evidence_ids": [str, ...]}}, ...],   (1-6 items)
 "data_gaps": [str, ...]}}"""

FEEDBACK = """- IMPORTANT: your previous brief was checked mechanically and these claims were
  rejected. Remove them or restate them using numbers exactly as they appear in the
  evidence:
{items}"""


class _Weighted(Claim):
    weight: int = Field(default=2, ge=1, le=3)


class _Against(BaseModel):
    text: str = Field(min_length=5)
    weight: int = Field(default=2, ge=1, le=3)


class _CaseDraft(BaseModel):
    thesis: str = Field(min_length=10)
    arguments: list[_Weighted] = Field(min_length=1, max_length=5)
    against: list[_Against] = Field(default_factory=list, max_length=3)
    concession: str = Field(min_length=5)

    _cap = field_validator("arguments", mode="before")(lambda v: cap_claims(v, 5))
    _cap_against = field_validator("against", mode="before")(
        lambda v: v[:3] if isinstance(v, list) else v)


# Measured on the first replay with a 0.2-0.9 scale in the prompt: every bull scored
# 0.7 and every bear 0.4 (or 0.5), whatever the stock. A model asked for one number
# picks a familiar one. So it is asked for something it can judge (how much each
# point carries) and the number is computed: support / (support + opposition).

def case_strength(arguments: list[_Weighted], against: list[_Against]) -> float:
    """0..1 from weighted points. No opposing points named counts as one weak one:
    an analyst who sees nothing against their side is not believed outright."""
    support = sum(a.weight for a in arguments)
    opposition = sum(a.weight for a in against) or 1
    if not support:
        return 0.0
    return round(min(0.95, max(0.05, support / (support + opposition))), 2)


class _JudgeDraft(Brief):
    """The judge writes a Brief; mechanical faults are fixed here instead of by a repair call."""

    override_reason: str = ""

    _cap = field_validator("key_points", "risks", mode="before")(lambda v: cap_claims(v, 6))


# ---------------------------------------------------------------- the desk's lean
#
# Measured on 26 stored briefs (Nov 2026): the judge chose neutral 20 times, including
# when two of three specialists were bullish and the bull case out-scored the bear by
# 0.2 or more. Left alone, a model treats neutral as the safe answer. So the starting
# point is computed here, in code, where it is the same every time and testable; the
# judge may depart from it, but must say why.

CASE_GAP = 0.15  # how much stronger one case must be to count as a lean


def desk_lean(reports: list, cases: list) -> tuple[Stance, str]:
    """bullish / bearish when the specialists lean one way with nobody opposed and
    either two of them agree or the debate backs them; neutral otherwise."""
    stances = [str(getattr(r, "stance", "")) for r in reports]
    up, down = stances.count(Stance.bullish), stances.count(Stance.bearish)
    conf = {str(c.side): c.confidence for c in cases}
    gap = round(conf.get(Side.bull, 0.0) - conf.get(Side.bear, 0.0), 2) \
        if len(conf) == 2 else 0.0
    why = (f"specialists {up} bullish, {down} bearish, {len(stances) - up - down} neutral; "
           f"bull case {conf.get(Side.bull, '-')} vs bear case {conf.get(Side.bear, '-')}")
    if up and not down and (up >= 2 or gap >= CASE_GAP) and gap > -CASE_GAP:
        return Stance.bullish, why
    if down and not up and (down >= 2 or gap <= -CASE_GAP) and gap < CASE_GAP:
        return Stance.bearish, why
    return Stance.neutral, why


async def fetch_evidence(ids: list[str]) -> dict[str, Evidence]:
    out: dict[str, Evidence] = {}
    async with McpToolset(MARKET_DATA, allow={"get_evidence"}) as ts:
        for i in range(0, len(ids), 20):
            for row in await ts.call("get_evidence", ids=ids[i:i + 20]):
                ev = Evidence.model_validate(row)
                out[ev.id] = ev
    return out


async def recall(symbol: str, query: str) -> list[PriorView]:
    """Best effort: a desk with amnesia still works, so a memory outage is not fatal."""
    try:
        async with McpToolset(MEMORY, allow={"recall"}) as ts:
            rows = await ts.call("recall", query=query, symbol=symbol, k=3)
    except Exception as e:  # noqa: BLE001
        log.warning("memory recall failed: %s", e)
        return []
    return [PriorView(as_of=r["as_of"], stance=r["stance"], confidence=r["confidence"],
                      thesis=r["text"], similarity=r.get("similarity")) for r in rows]


def material(req: DebateRequest, evidence: dict[str, Evidence]) -> dict:
    return {
        "symbol": req.symbol,
        "reports": [r.model_dump(mode="json", include={
            "agent", "stance", "confidence", "summary", "points", "data_gaps"})
            for r in req.reports],
        "missing_analyses": req.missing,
        "evidence": [ev.prompt_view() for ev in evidence.values()],
    }


def _grounded(claims: list[Claim], known: set[str], dropped: list[str]) -> list[Claim]:
    kept = []
    for c in claims:
        unknown = sorted(set(c.evidence_ids) - known)
        if unknown:
            dropped.append(f"{c.text} (unknown evidence {unknown})")
        else:
            kept.append(c)
    return kept


async def _ask(system: str, user: dict, *, caller: str, req: DebateRequest, prefer: str | None,
               max_tokens: int, effort: str = "low"):
    client = GatewayClient(GATEWAY, caller=caller, lane=req.lane)
    try:
        resp = await client.chat(
            [ChatMessage(role="system", content=system),
             ChatMessage(role="user", content=json.dumps(user, separators=(",", ":")))],
            tier=Tier.large, prefer_model=prefer, max_tokens=max_tokens,
            reasoning_effort=effort)
        return resp, client
    except Exception:
        await client.aclose()
        raise


async def argue(payload: dict, side: Side) -> dict:
    req = DebateRequest.model_validate(payload)
    if not req.reports:
        raise InvalidRequest("no reports to argue from")
    evidence = await fetch_evidence(req.evidence_ids())
    system = CASE.format(side=side.value, symbol=req.symbol,
                         common=COMMON.format(numbers=NUMBERS_RULE.replace("\n", " ")))
    resp, client = await _ask(system, material(req, evidence), caller=f"{side.value}-agent",
                              req=req, prefer=PREFER[side], max_tokens=CASE_MAX_TOKENS)
    try:
        draft, _ = await parse_with_repair(client, resp.message.content or "", _CaseDraft)
    finally:
        await client.aclose()
    dropped: list[str] = []
    kept = _grounded(draft.arguments, set(evidence), dropped)  # dropped points do not count
    confidence = case_strength(kept, draft.against)
    arguments = [Claim(text=a.text, evidence_ids=a.evidence_ids) for a in kept]
    if not arguments:
        raise RuntimeError(f"every argument cited unknown evidence: {dropped}")
    return Case(request_id=req.request_id, symbol=req.symbol, side=side, thesis=draft.thesis,
                arguments=arguments, concession=draft.concession, confidence=confidence,
                dropped=dropped, model=resp.model,
                tokens_used=0 if resp.cached else resp.usage.total_tokens).model_dump(mode="json")


async def judge(payload: dict) -> dict:
    req = DebateRequest.model_validate(payload)
    if not req.cases:
        raise InvalidRequest("the judge needs at least one case")
    evidence = await fetch_evidence(req.evidence_ids())
    priors = await recall(req.symbol, " ".join(c.thesis for c in req.cases))
    feedback = (FEEDBACK.format(items="\n".join(f"  - {f}" for f in req.feedback))
                if req.feedback else "")
    system = JUDGE.format(symbol=req.symbol, feedback=feedback,
                          common=COMMON.format(numbers=NUMBERS_RULE.replace("\n", " ")))
    lean, lean_why = desk_lean(req.reports, req.cases)
    user = {**material(req, evidence),
            "cases": [c.model_dump(mode="json", include={
                "side", "thesis", "arguments", "concession", "confidence"}) for c in req.cases],
            "desk_lean": {"stance": lean.value, "basis": lean_why},
            "prior_views": [p.model_dump(mode="json", exclude={"similarity"}) for p in priors]}
    resp, client = await _ask(system, user, caller="judge-agent", req=req, prefer=None,
                              max_tokens=2500, effort="medium")
    tokens = 0 if resp.cached else resp.usage.total_tokens
    try:
        draft, _ = await parse_with_repair(client, resp.message.content or "", _JudgeDraft)
        if draft.stance != lean and not draft.override_reason.strip():
            # Departed from the lean without saying why: ask once, in the same thread.
            again = await client.chat(
                [ChatMessage(role="system", content=system),
                 ChatMessage(role="user", content=json.dumps(user, separators=(",", ":"))),
                 resp.message,
                 ChatMessage(role="user", content=(
                     f"Your stance ({draft.stance}) differs from desk_lean ({lean.value}) but "
                     "override_reason is empty. Either give the concrete evidence-backed "
                     "reason in override_reason, or adopt the lean. Reply with the full "
                     "JSON object again."))],
                tier=Tier.large, max_tokens=2500, reasoning_effort="medium")
            tokens += 0 if again.cached else again.usage.total_tokens
            draft, _ = await parse_with_repair(client, again.message.content or "", _JudgeDraft)
            resp = again
        brief = Brief.model_validate(draft.model_dump(exclude={"override_reason"}))
    finally:
        await client.aclose()
    override = draft.override_reason.strip() if brief.stance != lean else ""
    dropped: list[str] = []
    brief = brief.model_copy(update={
        "symbol": req.symbol,
        "key_points": _grounded(brief.key_points, set(evidence), dropped),
        "risks": _grounded(brief.risks, set(evidence), dropped)})
    if brief.stance != lean and not override:
        dropped.append(f"stance {brief.stance} departs from lean {lean.value} with no reason")
    return Judgement(request_id=req.request_id, brief=brief, prior_views=priors, dropped=dropped,
                     model=resp.model, tokens_used=tokens, lean=lean, lean_reason=lean_why,
                     override_reason=override).model_dump(mode="json")


async def bull(payload: dict) -> dict:
    return await argue(payload, Side.bull)


async def bear(payload: dict) -> dict:
    return await argue(payload, Side.bear)


app = build_a2a_app(A2AService(
    name="debate-agent",
    description="Bull and bear cases argued independently, and a judge who writes the brief.",
    version="0.1.0",
    url=os.getenv("PUBLIC_URL", "http://127.0.0.1:8000/"),
    skills=[
        Skill(id="bull_case", name="Bull case", tags=["psx", "debate"],
              description="The strongest honest case for a PSX stock, from given evidence."),
        Skill(id="bear_case", name="Bear case", tags=["psx", "debate"],
              description="The strongest honest case against a PSX stock, from given evidence."),
        Skill(id="judgement", name="Judgement", tags=["psx", "debate", "brief"],
              description="Weighs bull and bear cases and writes the research brief."),
    ],
    handler={"bull_case": bull, "bear_case": bear, "judgement": judge},
))


def run() -> None:
    serve(app, "debate-agent")
