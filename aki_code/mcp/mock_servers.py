from __future__ import annotations

from aki_code.mcp.client import MCPClient

# In-process mock servers stand in for real MCP transports (the course keeps real
# transport out of this chapter). Each factory returns an MCPClient already
# loaded with the server's tool defs and handlers, exactly as a tools/list result
# would populate it. Tool defs carry readOnlyHint/destructiveHint to show that a
# server's own hints are never trusted as authorization (see policy.py).

# ── docs server (read-only) ────────────────────────────────────────
_DOCS = {
    "deploy.md": "How to deploy the service to production.",
    "auth.md": "Authentication and token refresh flow.",
    "search.md": "Search API reference and rate limits.",
}


def _docs_search(query: str) -> str:
    needle = str(query).lower()
    hits = [
        name
        for name, body in _DOCS.items()
        if needle in name.lower() or needle in body.lower()
    ]
    if not hits:
        return f"No docs matched '{query}'."
    return "\n".join(f"{name}: {_DOCS[name]}" for name in sorted(hits))


def _docs_get_version() -> str:
    return "docs corpus v1.4.2"


def _docs_server() -> MCPClient:
    client = MCPClient()
    client.register(
        [
            {
                "name": "search",
                "description": "Search the documentation corpus by keyword.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "readOnlyHint": True,
            },
            {
                "name": "get_version",
                "description": "Return the documentation corpus version.",
                "inputSchema": {"type": "object", "properties": {}},
                "readOnlyHint": True,
            },
        ],
        {"search": _docs_search, "get_version": _docs_get_version},
    )
    return client


# ── deploy server (has one destructive action) ─────────────────────
def _deploy_status(service: str = "web") -> str:
    return f"service '{service}' is running (2 healthy instances)"


def _deploy_trigger(service: str, version: str) -> str:
    return f"Triggered deploy of '{service}' version '{version}'."


def _deploy_server() -> MCPClient:
    client = MCPClient()
    client.register(
        [
            {
                "name": "status",
                "description": "Report the running status of a service.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                },
                "readOnlyHint": True,
            },
            {
                "name": "trigger",
                "description": "Trigger a deployment of a service version.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "version": {"type": "string"},
                    },
                    "required": ["service", "version"],
                },
                "destructiveHint": True,
            },
        ],
        {"status": _deploy_status, "trigger": _deploy_trigger},
    )
    return client


MOCK_SERVERS = {
    "docs": _docs_server,
    "deploy": _deploy_server,
}
