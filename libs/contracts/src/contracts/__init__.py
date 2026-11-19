"""Typed, versioned contracts shared by every service.

Rule: services never pass free-form dicts to each other. If a message crosses a
process boundary, its shape lives here and carries a schema version.
"""

from contracts.analysis import AnalysisReport, AnalysisRequest
from contracts.brief import Brief, Claim, Stance
from contracts.debate import Case, DebateRequest, Judgement, PriorView, Side
from contracts.evidence import Evidence
from contracts.llm import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Lane,
    Tier,
    ToolCall,
    Usage,
)
from contracts.review import ClaimCheck, RiskAssessment, RiskFlag, Verification
from contracts.trading import (
    Check,
    Decision,
    Fill,
    OrderSide,
    Portfolio,
    Position,
    Proposal,
    ProposalStatus,
    ProposeTradeRequest,
    ProposeTradeResult,
    TradeIntent,
)

__all__ = [
    "AnalysisReport",
    "AnalysisRequest",
    "Brief",
    "Case",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "Check",
    "Claim",
    "ClaimCheck",
    "DebateRequest",
    "Decision",
    "Evidence",
    "Fill",
    "Judgement",
    "Lane",
    "OrderSide",
    "Portfolio",
    "Position",
    "PriorView",
    "Proposal",
    "ProposalStatus",
    "ProposeTradeRequest",
    "ProposeTradeResult",
    "RiskAssessment",
    "RiskFlag",
    "Side",
    "Stance",
    "Tier",
    "ToolCall",
    "TradeIntent",
    "Usage",
    "Verification",
]
