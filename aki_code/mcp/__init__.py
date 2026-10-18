# Re-export only the leaf modules here. MCPManager and the tools (ConnectMcpTool
# / McpToolAdapter) are deliberately NOT re-exported: mcp.tools imports
# aki_code.tools.base, and aki_code.tools.__init__ imports mcp.tools, so eagerly
# loading manager/tools from this package init could run that cycle while
# aki_code.tools is only half-initialized. Import those two from their submodules
# (aki_code.mcp.manager / aki_code.mcp.tools) directly.
from aki_code.mcp.client import MCPClient
from aki_code.mcp.mock_servers import MOCK_SERVERS
from aki_code.mcp.naming import (
    MAX_TOOL_NAME_LEN,
    is_mcp_tool,
    normalize_mcp_name,
    parse_prefixed_name,
    prefixed_name,
)
from aki_code.mcp.policy import DEFAULT_MCP_POLICY, MCP_HOST_POLICY, mcp_policy

__all__ = [
    "MCPClient",
    "MOCK_SERVERS",
    "MAX_TOOL_NAME_LEN",
    "is_mcp_tool",
    "normalize_mcp_name",
    "parse_prefixed_name",
    "prefixed_name",
    "DEFAULT_MCP_POLICY",
    "MCP_HOST_POLICY",
    "mcp_policy",
]
