"""Paper trading: proposals, decisions, fills, the portfolio.

Lifecycle of a proposal (the broker enforces every arrow):

    portfolio agent ──propose──► pending ──approve (human)──► filled
                                    │                      └► failed   (checks fail at fill time)
                                    ├──reject (human)──────► rejected
                                    └──older than expiry───► expired

Nothing becomes a trade without a human decision, and the broker re-checks its
own limits at the moment of the fill, whatever the proposer claimed.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class OrderSide(StrEnum):
    buy = "buy"
    sell = "sell"


class ProposalStatus(StrEnum):
    pending = "pending"
    rejected = "rejected"
    expired = "expired"
    filled = "filled"
    failed = "failed"


class Check(BaseModel):
    rule: str
    ok: bool
    detail: str


class Position(BaseModel):
    symbol: str
    quantity: int
    avg_cost: float
    mark: float | None = Field(description="latest quote; None if unavailable")
    market_value: float
    weight_pct: float


class Portfolio(BaseModel):
    schema_version: Literal["portfolio.v1"] = "portfolio.v1"
    cash: float
    equity: float
    positions: list[Position]
    marks_stale: list[str] = Field(default_factory=list,
                                   description="symbols valued at cost: no live quote")


class Proposal(BaseModel):
    schema_version: Literal["proposal.v1"] = "proposal.v1"
    id: str
    account: str  # whose demo account it is for
    client_order_id: str
    run_id: str | None
    proposed_by: str
    symbol: str
    side: OrderSide
    quantity: int
    ref_price: float
    rationale: str
    brief: dict
    evidence_ids: list[str]
    checks: list[Check]
    status: ProposalStatus
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    created_at: str
    expires_at: str


class Fill(BaseModel):
    proposal_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    fees: float
    filled_at: str


class Decision(BaseModel):
    proposal: Proposal
    fill: Fill | None = None
    reason: str | None = Field(default=None, description="why it failed or was refused")


class TradeIntent(BaseModel):
    """What the portfolio agent's model decides. Intent only: no quantities, no prices.
    Code turns a target weight into shares."""

    action: Literal["buy", "sell", "hold"]
    target_weight_pct: float = Field(ge=0, le=100,
                                     description="desired position size, % of equity")
    rationale: str = Field(min_length=10, max_length=600)


class ProposeTradeRequest(BaseModel):
    run_id: str
    symbol: str
    brief: dict
    risk: dict


class ProposeTradeResult(BaseModel):
    """The desk's call on a brief, and how many accounts got a proposal from it.
    Shared with everyone who sees the brief, so it names no account."""

    run_id: str
    symbol: str
    action: Literal["buy", "sell", "hold"]
    proposal_id: str | None = None
    status: str | None = None
    quantity: int = 0
    reason: str
    tokens_used: int = 0
    target_weight_pct: float = 0
    accounts: int = 0  # accounts that received a pending proposal


# ---------------------------------------------------------------- watchlists and alerts


class AlertKind(StrEnum):
    pct = "pct"  # the price falls this many percent below the reference
    pkr = "pkr"  # the position loses this many rupees against the reference


class AlertRule(BaseModel):
    """A user's price alert. It fires once when the drop crosses the threshold, and
    re-arms only after the price recovers past 80% of it (hysteresis)."""

    schema_version: Literal["alert_rule.v1"] = "alert_rule.v1"
    id: str
    account: str
    symbol: str
    kind: AlertKind
    threshold: float
    ref_price: float = Field(description="the price the drop is measured from")
    ref_basis: Literal["avg_cost", "price_when_set"]
    starts_at: str
    ends_at: str | None = Field(description="None: watches until cancelled")
    state: Literal["armed", "triggered"]
    status: Literal["active", "expired", "cancelled", "ended"]
    last_price: float | None = None
    last_checked_at: str | None = None
    times_triggered: int = 0
    created_by: str
    created_at: str


class AlertEvent(BaseModel):
    id: int
    rule_id: str
    account: str
    symbol: str
    kind: Literal["triggered", "rearmed"]
    price: float
    drop_pct: float
    loss_pkr: float | None = None
    at: str
