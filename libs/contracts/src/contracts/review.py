"""Outputs of the two deterministic checks: the claim verifier and the risk gate.

Neither is an agent. Checking that a number matches its source, or that a stock
trades enough to be bought, needs rules, not judgement, so it is plain code:
cheap, instant, and the same answer every time.
"""

from typing import Literal

from pydantic import BaseModel, Field

ClaimStatus = Literal["verified", "unverified", "uncited"]


class ClaimCheck(BaseModel):
    section: str  # key_points | risks | arguments
    text: str
    status: ClaimStatus
    evidence_ids: list[str]
    numbers: list[str] = Field(default_factory=list, description="numbers found in the text")
    unmatched: list[str] = Field(default_factory=list, description="numbers not in the evidence")
    unknown_ids: list[str] = Field(default_factory=list)

    def reason(self) -> str:
        if self.status == "uncited":
            return f"cites unknown evidence {self.unknown_ids}"
        return f"numbers {self.unmatched} are not in evidence {self.evidence_ids}"


class Verification(BaseModel):
    schema_version: Literal["verification.v1"] = "verification.v1"
    checked: int
    verified: int
    rejected: list[ClaimCheck] = Field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.verified / self.checked if self.checked else 1.0


Severity = Literal["info", "caution", "veto"]


class RiskFlag(BaseModel):
    rule: str
    severity: Severity
    detail: str


class RiskAssessment(BaseModel):
    schema_version: Literal["risk.v1"] = "risk.v1"
    verdict: Literal["pass", "caution", "veto"]
    # Slice 4: only actionable briefs may become paper-trade proposals.
    actionable: bool
    flags: list[RiskFlag] = Field(default_factory=list)
    confidence_before: float
    confidence_after: float
