"""Local tools for the agent loop.

In Slice 1 these become MCP tools served by a separate process. The agent loop
does not care: it sees a name, a JSON schema, and an async function.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the arguments
    fn: Callable[..., Awaitable[Any]]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def to_tool_content(result: Any) -> str:
    """Tool results go back to the model as compact JSON, never pretty-printed: tokens cost quota."""
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    return json.dumps(result, separators=(",", ":"), default=str)
