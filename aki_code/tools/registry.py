from __future__ import annotations

from aki_code.tools.base import BaseTool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def restrict(self, allowed: set[str]) -> None:
        self._tools = {name: tool for name, tool in self._tools.items() if name in allowed}

    def get_schemas(self) -> list[dict]:
        return [t.to_schema() for t in self._tools.values()]

    def execute(self, name, /, **kwargs) -> str:
        # `name` is positional-only so a tool whose own input schema has a "name"
        # argument (connect_mcp, create_worktree) cannot collide with it when the
        # loop calls registry.execute(tool_name, **tool_input).
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            return tool.execute(**kwargs)
        except Exception as e:
            return f"Error executing {name}: {e}"

    def setup_all(self) -> None:
        for tool in self._tools.values():
            tool.setup()

    def teardown_all(self) -> None:
        for tool in self._tools.values():
            tool.teardown()
