"""Research brief: the output both the baseline and the multi-agent system produce.

Keeping one output contract for both is what makes the Slice 5 comparison fair.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Stance(StrEnum):
    bullish = "bullish"
    neutral = "neutral"
    bearish = "bearish"


class Claim(BaseModel):
    text: str = Field(min_length=5)
    evidence_ids: list[str] = Field(min_length=1, description="Every claim must cite evidence")


class Brief(BaseModel):
    schema_version: Literal["brief.v1"] = "brief.v1"
    symbol: str
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=10)
    key_points: list[Claim] = Field(min_length=1, max_length=6)
    risks: list[Claim] = Field(min_length=1, max_length=6)
    data_gaps: list[str] = Field(default_factory=list)

    def cited_ids(self) -> set[str]:
        return {eid for c in [*self.key_points, *self.risks] for eid in c.evidence_ids}
