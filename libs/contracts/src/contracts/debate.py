"""Messages for the debate: bull and bear argue, a judge writes the brief.

Bull and bear are asked *in parallel* and never see each other's case, so
neither anchors on the other. The judge sees both. Prior views from memory go
to the judge only, and are context, never evidence: they cannot be cited.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from contracts.analysis import AnalysisReport
from contracts.brief import Brief, Claim, Stance
from contracts.llm import Lane


class Side(StrEnum):
    bull = "bull"
    bear = "bear"


class Case(BaseModel):
    schema_version: Literal["case.v1"] = "case.v1"
    request_id: str
    symbol: str
    side: Side
    thesis: str = Field(min_length=10)
    arguments: list[Claim] = Field(min_length=1, max_length=5)
    concession: str = Field(min_length=5, description="The strongest point against this side")
    confidence: float = Field(ge=0.0, le=1.0)
    dropped: list[str] = Field(default_factory=list)
    model: str | None = None
    tokens_used: int = 0


class DebateRequest(BaseModel):
    """To a debater (side set) or to the judge (cases set)."""

    schema_version: Literal["debate_request.v1"] = "debate_request.v1"
    symbol: str
    request_id: str
    lane: Lane = Lane.pipeline
    side: Side | None = None
    reports: list[AnalysisReport]
    missing: dict[str, str] = Field(default_factory=dict, description="analysis -> why missing")
    cases: list[Case] = Field(default_factory=list)
    # From the verifier, when the judge's previous brief failed verification.
    feedback: list[str] = Field(default_factory=list)

    def evidence_ids(self) -> list[str]:
        return sorted({i for r in self.reports for i in r.evidence_ids})


class PriorView(BaseModel):
    as_of: str
    stance: Stance
    confidence: float
    thesis: str
    similarity: float | None = None


class Judgement(BaseModel):
    schema_version: Literal["judgement.v1"] = "judgement.v1"
    request_id: str
    brief: Brief
    prior_views: list[PriorView] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    model: str | None = None
    tokens_used: int = 0
    # The starting point code computed from the specialists and the cases, and the
    # judge's reason when its stance differs from it (empty when they agree).
    lean: Stance | None = None
    lean_reason: str = ""
    override_reason: str = ""
