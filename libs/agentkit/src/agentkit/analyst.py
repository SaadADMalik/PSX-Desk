"""The shared shape of a specialist agent.

A specialist is configuration, not code: a prompt, the MCP tools it may use, and
a model tier. Everything else is common:

    open MCP toolset (allowlisted)  ->  agent loop  ->  parse draft (one repair)
    ->  enforce grounding locally   ->  AnalysisReport (evidence by id only)

Grounding is enforced *inside* the agent, before anything crosses the wire: a
point citing evidence this agent never received is dropped and recorded as a
data gap. Every agent is responsible for its own output; the orchestrator should
not have to trust it.
"""

import os
from dataclasses import dataclass, replace

from contracts import AnalysisReport, AnalysisRequest, Claim, Evidence, Stance, Tier
from mcp.server import MCPServer
from pydantic import BaseModel, Field, field_validator

from agentkit.a2a import A2AService, Skill, build_a2a_app
from agentkit.client import GatewayClient
from agentkit.loop import run_agent
from agentkit.mcp_tools import McpToolset
from agentkit.structured import cap_claims, parse_with_repair

# Every agent gets this rule; the verifier enforces it on every claim.
NUMBERS_RULE = """Numbers: copy them exactly as they appear in evidence values. Never
calculate a new number (no differences, ratios or percentages of your own). If the
number you want is not in the evidence, describe the relationship in words."""

DRAFT_FORMAT = """Reply in plain message text (do not call a tool to submit) with ONLY:
{"stance": "bullish"|"neutral"|"bearish", "confidence": 0..1, "summary": str,
 "points": [{"text": str, "evidence_ids": [str, ...]}, ...],   (1-6 items)
 "data_gaps": [str, ...]}"""


class _Draft(BaseModel):
    """What the model writes. The agent adds identity, evidence and bookkeeping."""

    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10)
    points: list[Claim] = Field(min_length=1, max_length=6)
    data_gaps: list[str] = Field(default_factory=list)

    _cap = field_validator("points", mode="before")(lambda v: cap_claims(v, 6))


class UngroundedOutput(Exception):
    """Every point the model wrote cited evidence it never received."""


class NoEvidence(Exception):
    """The agent's tools returned no evidence (e.g. PSX has no data for the symbol)."""


@dataclass(frozen=True)
class AnalystConfig:
    agent: str
    system: str
    tools: frozenset[str]
    tier: Tier = Tier.small
    max_steps: int = 4
    max_tokens: int = 1500
    prefer_model: str | None = None  # spread agents across separate model budgets


async def analyse(config: AnalystConfig, req: AnalysisRequest, *, gateway_url: str,
                  mcp_server: str | MCPServer,
                  gateway: GatewayClient | None = None) -> AnalysisReport:
    registry: dict[str, Evidence] = {}

    def record(_tool: str, data: dict) -> dict:
        ev = Evidence.model_validate(data)  # id arrives set; never recomputed downstream
        registry[ev.id] = ev
        return ev.prompt_view()

    tool_errors: list[str] = []

    def watched(tool):
        async def fn(**kw):
            try:
                return await tool.fn(**kw)
            except Exception as e:
                tool_errors.append(f"{tool.name}: {e}")
                raise  # the loop still hands the error to the model
        return replace(tool, fn=fn)

    own_client = gateway is None
    client = gateway or GatewayClient(gateway_url, caller=config.agent, lane=req.lane)
    try:
        async with McpToolset(mcp_server, allow=set(config.tools), on_result=record) as ts:
            result = await run_agent(
                client,
                system=f"{config.system}\n\n{NUMBERS_RULE}\n\n{DRAFT_FORMAT}",
                user=f"Analyse {req.symbol}.",
                tools=[watched(t) for t in ts.tools],
                tier=config.tier,
                max_steps=config.max_steps,
                max_tokens=config.max_tokens,
                prefer_model=config.prefer_model,
            )
        if not registry:
            # Nothing to cite, so nothing to analyse. Say why, and skip the parse call.
            raise NoEvidence(f"no evidence received; tool errors: {tool_errors or 'none'}")
        draft, _ = await parse_with_repair(client, result.text, _Draft, tier=Tier.small)
    finally:
        if own_client:
            await client.aclose()

    gaps = list(draft.data_gaps)
    points = []
    for p in draft.points:
        unknown = set(p.evidence_ids) - registry.keys()
        if unknown:
            gaps.append(f"dropped a point citing unknown evidence {sorted(unknown)}: {p.text}")
        else:
            points.append(p)
    if not points:
        raise UngroundedOutput("no point cited evidence the agent actually received")
    gaps += [f"{ev.kind} served from last-good snapshot (stale)"
             for ev in registry.values() if ev.stale]

    return AnalysisReport(
        request_id=req.request_id,
        agent=config.agent,
        symbol=req.symbol,
        stance=draft.stance,
        confidence=draft.confidence,
        summary=draft.summary,
        points=points,
        data_gaps=gaps,
        evidence_ids=sorted(registry),
        tokens_used=result.usage.total_tokens,
    )


def analyst_app(config: AnalystConfig, *, description: str, skill: Skill,
                version: str = "0.1.0"):
    """An analyst as a deployable A2A service. Wiring comes from the environment."""
    gateway_url = os.getenv("LLM_GATEWAY_URL", "http://127.0.0.1:8100")
    mcp_url = os.getenv("MARKET_DATA_MCP_URL", "http://127.0.0.1:8300/mcp")
    public_url = os.getenv("PUBLIC_URL", "http://127.0.0.1:8000/")

    async def handle(payload: dict) -> dict:
        req = AnalysisRequest.model_validate(payload)
        report = await analyse(config, req, gateway_url=gateway_url, mcp_server=mcp_url)
        return report.model_dump(mode="json")

    return build_a2a_app(A2AService(name=config.agent, description=description,
                                    version=version, url=public_url, skills=[skill],
                                    handler=handle))


def serve(app, service_name: str) -> None:
    import logging

    import uvicorn

    from agentkit.tracing import setup_tracing

    logging.basicConfig(level=logging.INFO)
    setup_tracing(service_name, app=app)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
