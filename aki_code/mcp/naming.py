from __future__ import annotations

import re

# Model tool names must match ^[a-zA-Z0-9_-]{1,64}$. Anything else in a server or
# tool name is replaced with '_' so the prefixed name stays a valid tool name.
_INVALID = re.compile(r"[^a-zA-Z0-9_-]")
MAX_TOOL_NAME_LEN = 64
MCP_PREFIX = "mcp"


def normalize_mcp_name(name: str) -> str:
    """Replace characters a model tool name cannot contain with underscores."""
    return _INVALID.sub("_", str(name))


def is_mcp_tool(name: str) -> bool:
    return str(name).startswith(f"{MCP_PREFIX}__")


def prefixed_name(server: str, tool: str) -> str:
    """Build mcp__{server}__{tool}, enforcing the 64-character model limit.

    Two different (server, tool) pairs can normalize to the same prefixed name
    (e.g. 'docs.one'/'get.version' and 'docs_one'/'get_version'); the caller that
    assembles the pool must reject that collision rather than alias silently.
    """
    prefixed = (
        f"{MCP_PREFIX}__{normalize_mcp_name(server)}__{normalize_mcp_name(tool)}"
    )
    if len(prefixed) > MAX_TOOL_NAME_LEN:
        raise ValueError(
            f"MCP tool name exceeds {MAX_TOOL_NAME_LEN} characters: {prefixed}"
        )
    return prefixed


def parse_prefixed_name(prefixed: str) -> tuple[str, str] | None:
    """Recover (server, tool) from a prefixed name, or None if malformed.

    Server names come from host configuration and contain no '__', so the first
    '__' after the prefix separates server from tool; a tool name may itself
    contain '__' and is rejoined.
    """
    if not is_mcp_tool(prefixed):
        return None
    parts = str(prefixed).split("__")
    if len(parts) < 3 or parts[0] != MCP_PREFIX:
        return None
    return parts[1], "__".join(parts[2:])
