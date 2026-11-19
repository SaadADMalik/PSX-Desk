"""Contract between any agent and the LLM gateway.

Agents ask for a *tier* (how capable a model they need), not a vendor model id.
The gateway owns the mapping from tier to concrete models and the fallback order,
so swapping providers never touches agent code.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Lane(StrEnum):
    """Priority lane. Lower lanes may only use part of the daily budget."""

    interactive = "interactive"  # a human is waiting
    pipeline = "pipeline"  # the scheduled daily run
    backtest = "backtest"  # bulk replay; first to be starved


class Tier(StrEnum):
    small = "small"  # extraction, summarising, routing
    large = "large"  # reasoning, debate, judging


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: dict[str, Any]  # {"name": str, "arguments": json-string}


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    schema_version: Literal["chat.v1"] = "chat.v1"
    messages: list[ChatMessage]
    tools: list[dict[str, Any]] | None = None
    tier: Tier = Tier.small
    model: str | None = Field(default=None, description="Pin a model; disables fallback")
    prefer_model: str | None = Field(
        default=None, description="Try this model first, then the tier's fallback chain. "
        "Used to give debating agents different model families without losing failover")
    lane: Lane = Lane.pipeline
    caller: str = Field(default="unknown", description="Which agent is calling, for the ledger")
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    response_format: dict[str, Any] | None = None
    cache: bool = True


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatResponse(BaseModel):
    schema_version: Literal["chat.v1"] = "chat.v1"
    message: ChatMessage
    model: str
    usage: Usage
    cached: bool = False
    attempts: int = 1
    finish_reason: str | None = None
