"""Messages between the orchestrator and specialist agents, carried over A2A.

A2A carries these as JSON data parts. Note that A2A data parts are protobuf
`Value`s, so every integer arrives as a float (2473 -> 2473.0). Typed fields
below coerce back; free-form `Evidence.values` keep the float. That is also why
evidence ids are assigned once at the source and never recomputed downstream.
"""

from typing import Literal

from pydantic import BaseModel, Field

from contracts.brief import Claim, Stance
from contracts.llm import Lane


class AnalysisRequest(BaseModel):
    schema_version: Literal["analysis_request.v1"] = "analysis_request.v1"
    symbol: str
    request_id: str = Field(description="Caller's id for this piece of work; echoed back")
    # Priority travels with the work: if a human is waiting on the orchestrator,
    # the specialists' model calls are interactive too.
    lane: Lane = Lane.pipeline


class AnalysisReport(BaseModel):
    # v2: evidence travels by reference. The MCP server that computed each record
    # stored it; anyone who needs the values fetches them from there (get_evidence),
    # so no agent can alter a fact on its way to the verifier.
    schema_version: Literal["analysis_report.v2"] = "analysis_report.v2"
    request_id: str
    agent: str
    symbol: str
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10)
    points: list[Claim] = Field(min_length=1, max_length=6)
    data_gaps: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    tokens_used: int = 0
