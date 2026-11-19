"""The desk: how the orchestrator reaches every other participant.

    A2A  ─► specialists (technical, fundamental, news), bull, bear, judge, portfolio
    MCP  ─► market-data (get_evidence), memory (remember)

Since Slice 3 the orchestrator makes no model calls of its own. It coordinates
and checks; agents think. Every reply is validated against its contract here,
before the workflow trusts it.
"""

from agentkit.a2a import AgentDirectory
from agentkit.mcp_tools import McpToolset
from contracts import (
    AnalysisReport,
    AnalysisRequest,
    Brief,
    Case,
    DebateRequest,
    Evidence,
    Judgement,
    ProposeTradeRequest,
    ProposeTradeResult,
    Side,
)
from pydantic import BaseModel, ValidationError

from orchestrator.store import SPECIALISTS

__all__ = ["SPECIALISTS", "AgentFailed", "Desk", "SpecialistFailed"]

SIDES = {Side.bull: "bull_case", Side.bear: "bear_case"}


class AgentFailed(Exception):
    """An agent was unreachable, failed its task, or broke its contract."""


SpecialistFailed = AgentFailed  # Slice 2 name


class Desk:
    def __init__(self, directory: AgentDirectory, *, market_data_url: str,
                 memory_url: str) -> None:
        self.directory = directory
        self.market_data_url, self.memory_url = market_data_url, memory_url

    async def _call[T: BaseModel](self, skill: str, payload: dict, model: type[T]) -> T:
        try:
            result = await self.directory.call(skill, payload)
        except Exception as e:
            raise AgentFailed(f"{type(e).__name__}: {e}") from e
        try:
            return model.model_validate(result)
        except ValidationError as e:  # an agent broke the contract
            raise AgentFailed(f"invalid {model.__name__}: {e.error_count()} errors") from e

    async def analyse(self, skill: str, req: AnalysisRequest) -> AnalysisReport:
        report = await self._call(skill, req.model_dump(mode="json"), AnalysisReport)
        if report.request_id != req.request_id or report.symbol != req.symbol:
            raise AgentFailed("report does not match the request")
        return report

    call_specialist = analyse  # Slice 2 name

    async def argue(self, side: Side, req: DebateRequest) -> Case:
        case = await self._call(SIDES[side], req.model_copy(update={"side": side})
                                .model_dump(mode="json"), Case)
        if case.side != side or case.request_id != req.request_id:
            raise AgentFailed("case does not match the request")
        return case

    async def judge(self, req: DebateRequest) -> Judgement:
        j = await self._call("judgement", req.model_dump(mode="json"), Judgement)
        if j.request_id != req.request_id:
            raise AgentFailed("judgement does not match the request")
        return j

    async def propose(self, req: ProposeTradeRequest) -> ProposeTradeResult:
        r = await self._call("propose_trade", req.model_dump(mode="json"), ProposeTradeResult)
        if r.run_id != req.run_id or r.symbol != req.symbol:
            raise AgentFailed("proposal result does not match the request")
        return r

    async def evidence(self, ids: list[str]) -> dict[str, Evidence]:
        """From the service that computed it, never from what agents passed along."""
        out: dict[str, Evidence] = {}
        async with McpToolset(self.market_data_url, allow={"get_evidence"}) as ts:
            for i in range(0, len(ids), 20):
                for row in await ts.call("get_evidence", ids=ids[i:i + 20]):
                    ev = Evidence.model_validate(row)
                    out[ev.id] = ev
        return out

    async def remember(self, key: str, brief: Brief, as_of: str) -> bool:
        async with McpToolset(self.memory_url, allow={"remember"}) as ts:
            r = await ts.call("remember", key=key, symbol=brief.symbol, as_of=as_of,
                              stance=brief.stance.value, confidence=brief.confidence,
                              text=brief.thesis)
        return bool(r["stored"])
