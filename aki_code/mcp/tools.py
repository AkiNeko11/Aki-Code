from __future__ import annotations

from aki_code.tools.base import BaseTool


class McpToolAdapter(BaseTool):
    """Expose one discovered MCP server tool as a native BaseTool.

    The model sees the prefixed name and the server's inputSchema (mapped to
    input_schema); execute forwards to the server's OWN tool name through the
    MCPClient, which already turns errors into tool_result strings. Holding the
    client and raw name on the instance is the course's default-argument lambda
    capture, without the late-binding hazard.
    """

    def __init__(self, *, name, description, input_schema, client, raw_name):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._client = client
        self._raw_name = raw_name

    def execute(self, **kwargs) -> str:
        return self._client.call_tool(self._raw_name, kwargs)


class ConnectMcpTool(BaseTool):
    """Connect an MCP server and add its tools to the live pool (Lesson 14).

    The model calls this to discover a server's tools at runtime. Because the
    agent loop re-reads registry.get_schemas() on every iteration, the adapters
    registered by a successful connect surface on the next model call. Connecting
    is discovery only and auto-allowed; the discovered tools are gated separately
    by the host MCP policy.
    """

    name = "connect_mcp"
    description = (
        "Connect to an MCP server by name and add its tools to the available "
        "tool set. After connecting, the server's tools appear as "
        "mcp__<server>__<tool> on the next turn."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Server name to connect, e.g. 'docs'.",
            },
        },
        "required": ["name"],
    }

    def __init__(self, *, registry, manager):
        self._registry = registry
        self._manager = manager
        available = ", ".join(sorted(manager.servers)) or "none"
        self.description = (
            "Connect to an MCP server by name and add its tools to the available "
            f"tool set. Available servers: {available}. After connecting, the "
            "server's tools appear as mcp__<server>__<tool> on the next turn."
        )

    def execute(self, *, name: str) -> str:
        try:
            return self._manager.connect(name, self._registry)
        except ValueError as exc:
            return f"Error: {exc}"
