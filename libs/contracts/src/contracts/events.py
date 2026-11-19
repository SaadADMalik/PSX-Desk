"""Events: facts about something that already happened, published on the bus.

Commands ask; events tell. `brief.requested` is the one command-like event: it
records that someone asked, and the orchestrator decides what to do about it.

Every event has a unique `event_id` (for consumer dedupe) and carries the W3C
`traceparent` of the code that produced it, so a trace continues across the bus.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from contracts.llm import Lane

# Streams (topics). One stream per kind of fact keeps consumers independent.
REQUESTS = "desk.requests"  # brief.requested
BRIEFS = "desk.briefs"  # brief.completed, brief.failed
MARKET = "desk.market"  # market.closed


class Event(BaseModel):
    schema_version: Literal["event.v1"] = "event.v1"
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    source: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    traceparent: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BriefRequested(BaseModel):
    """Payload of `brief.requested`. `idempotency_key` is the business identity of the
    request: the scheduler uses `daily:<date>:<symbol>`, so firing twice is harmless."""

    symbol: str
    idempotency_key: str = Field(min_length=3, max_length=200)
    lane: Lane = Lane.pipeline
