from __future__ import annotations

from typing import Callable


class MCPClient:
    """One connected MCP server's discovered tools and call entry point.

    Mirrors the course MCPClient: register() stores the tool definitions and the
    raw-name -> handler map produced by discovery (the tools/list result), and
    call_tool() invokes a handler by the server's OWN tool name. Errors are
    returned as strings rather than raised, so a bad call surfaces to the model
    as a tool_result instead of ending the agent loop.
    """

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self._handlers: dict[str, Callable] = {}

    def register(self, tool_defs, handlers) -> None:
        """Record a discovery result: tool defs plus their callable handlers."""
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def tool_names(self) -> list[str]:
        return [str(t.get("name", "")) for t in self.tools]

    def call_tool(self, tool_name: str, args: dict | None = None) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**(args or {})))
        except Exception as error:  # noqa: BLE001 - errors become tool_results
            return f"MCP error: {type(error).__name__}: {error}"
