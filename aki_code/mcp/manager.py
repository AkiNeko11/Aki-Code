from __future__ import annotations

from aki_code.mcp.client import MCPClient
from aki_code.mcp.mock_servers import MOCK_SERVERS
from aki_code.mcp.naming import prefixed_name
from aki_code.mcp.tools import McpToolAdapter


class MCPManager:
    """Host-side registry of connected MCP servers (Lesson 14).

    Holds the connected MCPClients and, on connect, adapts each discovered tool
    into the live ToolRegistry under its mcp__<server>__<tool> name. The agent
    loop re-reads registry.get_schemas() every iteration, so a server connected
    mid-turn surfaces its tools on the next model call — the course's dynamic
    assemble_tool_pool(), realized by the registry we already have.
    """

    def __init__(self, *, servers=None):
        self.servers = dict(servers if servers is not None else MOCK_SERVERS)
        self.clients: dict[str, MCPClient] = {}

    def connect(self, name: str, registry) -> str:
        """Connect a server and register its tools; return a status message.

        Prefixed names are all resolved first so a normalization collision (two
        distinct tools mapping to one name) aborts the whole connect rather than
        half-registering a server.
        """
        if name in self.clients:
            return f"MCP server '{name}' is already connected"
        factory = self.servers.get(name)
        if factory is None:
            available = ", ".join(sorted(self.servers)) or "none"
            return f"Unknown MCP server '{name}'. Available: {available}"

        client = factory()
        existing = {schema["name"] for schema in registry.get_schemas()}
        planned: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for tool_def in client.tools:
            prefixed = prefixed_name(name, str(tool_def.get("name", "")))
            if prefixed in seen or prefixed in existing:
                raise ValueError(
                    f"MCP tool name collision after normalization: {prefixed}"
                )
            seen.add(prefixed)
            planned.append((prefixed, tool_def))

        for prefixed, tool_def in planned:
            registry.register(
                McpToolAdapter(
                    name=prefixed,
                    description=str(tool_def.get("description", "")),
                    input_schema=tool_def.get("inputSchema")
                    or {"type": "object", "properties": {}},
                    client=client,
                    raw_name=str(tool_def.get("name", "")),
                )
            )
        self.clients[name] = client
        names = ", ".join(prefixed for prefixed, _ in planned)
        return f"Connected MCP server '{name}' with tools: {names}"

    def connected(self) -> list[str]:
        return sorted(self.clients)

    def describe(self) -> str:
        """One line per connected server: its name and discovered tool names."""
        if not self.clients:
            return "(no MCP servers connected)"
        return "\n".join(
            f"{name}: {', '.join(self.clients[name].tool_names())}"
            for name in self.connected()
        )

    def prompt_state(self) -> str:
        """Compact MCP state for the system prompt: available + connected servers.

        Rebuilt each turn so the model sees servers connected earlier in the
        session; the tools themselves surface immediately via get_schemas().
        """
        available = ", ".join(sorted(self.servers)) or "none"
        if not self.clients:
            return f"Available (connect with connect_mcp): {available}. None connected yet."
        connected = "; ".join(
            f"{name} -> {', '.join('mcp__' + name + '__' + t for t in self.clients[name].tool_names())}"
            for name in self.connected()
        )
        return f"Available (connect with connect_mcp): {available}. Connected: {connected}."
