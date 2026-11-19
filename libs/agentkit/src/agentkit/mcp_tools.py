"""Turn an MCP server's tools into agent-loop Tools, with an allowlist.

This is where Composio-style "tool directory" behaviour actually lives: the
client asks the server what it offers (`tools/list`), gets names, descriptions
and JSON schemas, and hands them to the model. Calls go out as `tools/call`.

Two things we add on top:
  * an allowlist, so an agent only ever sees the tools its job needs (least
    privilege at the agent level, before the server's own checks in Slice 4);
  * fail-fast on contract drift: if an allowed tool is missing from the server,
    the agent refuses to start instead of failing mid-task.

Tracing needs no work here: the MCP SDK puts W3C trace context into each
request's `_meta`, a protocol field, so it survives any transport (HTTP or stdio).
"""

from collections.abc import Callable
from typing import Any, Self

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer

from agentkit.auth import default_identity, mcp_http_client
from agentkit.tools import Tool


class McpToolError(Exception):
    """The server reported the call as failed. The message is safe to show the model."""


class McpContractError(Exception):
    """The server does not offer a tool this agent depends on."""


ResultHook = Callable[[str, dict[str, Any]], Any]


class McpToolset:
    def __init__(self, server: str | MCPServer, *, allow: set[str] | None = None,
                 on_result: ResultHook | None = None, timeout_s: float = 120.0) -> None:
        self._server = server
        self._allow = allow
        self._on_result = on_result
        self._timeout_s = timeout_s
        self._client: Client | None = None
        self._http = None
        self.tools: list[Tool] = []

    async def __aenter__(self) -> Self:
        server = self._server
        if isinstance(server, str) and default_identity() is not None:
            # Every HTTP request to the server carries a signed token (aud = server).
            self._http = mcp_http_client(server, self._timeout_s)
            server = streamable_http_client(server, http_client=self._http)
        self._client = Client(server, read_timeout_seconds=self._timeout_s)
        await self._client.__aenter__()
        listed = (await self._client.list_tools()).tools
        offered = {t.name: t for t in listed}
        wanted = self._allow if self._allow is not None else set(offered)
        missing = wanted - offered.keys()
        if missing:
            await self.__aexit__(None, None, None)
            raise McpContractError(f"server does not offer required tools: {sorted(missing)}")
        self.tools = [self._wrap(offered[n].name, offered[n].description or "",
                                 offered[n].input_schema) for n in sorted(wanted)]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def call(self, name: str, **arguments: Any) -> Any:
        """Call an allowed tool from code, not from a model. Same allowlist, same errors.
        MCP wraps a non-object result (e.g. a list) as {"result": ...}; unwrap it."""
        tool = next((t for t in self.tools if t.name == name), None)
        if tool is None:
            raise McpContractError(f"tool {name!r} is not on this toolset's allowlist")
        data = await tool.fn(**arguments)
        if isinstance(data, dict) and data.keys() == {"result"}:
            return data["result"]
        return data

    def _wrap(self, name: str, description: str, schema: dict[str, Any]) -> Tool:
        async def call(**arguments: Any) -> Any:
            assert self._client is not None, "toolset used outside its context"
            result = await self._client.call_tool(name, arguments)
            if result.is_error:
                text = " ".join(getattr(c, "text", "") for c in result.content)
                raise McpToolError(text or f"{name} failed")
            data = result.structured_content
            if data is None:  # server returned unstructured content only
                data = {"text": " ".join(getattr(c, "text", "") for c in result.content)}
            return self._on_result(name, data) if self._on_result else data

        return Tool(name=name, description=description, parameters=schema, fn=call)
