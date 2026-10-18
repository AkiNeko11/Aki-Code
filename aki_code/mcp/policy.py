from __future__ import annotations

# Host-side authorization for MCP tools, keyed by the NORMALIZED (server, tool).
# A server's own readOnlyHint/destructiveHint is never trusted as authorization;
# only this host policy decides. Anything unconfigured defaults to confirm, so an
# unknown external tool always asks the user even if it claims to be read-only.
DEFAULT_MCP_POLICY = "confirm"

MCP_HOST_POLICY: dict[tuple[str, str], str] = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


def mcp_policy(server: str, tool: str) -> str:
    """The host policy for a normalized (server, tool): 'allow' or 'confirm'."""
    return MCP_HOST_POLICY.get((server, tool), DEFAULT_MCP_POLICY)
