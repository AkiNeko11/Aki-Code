from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.todo.manager import TodoManager


class TodoWriteTool(BaseTool):
    name = "todo_write"
    description = (
        "Create and manage a task list to plan multi-step work. Pass the FULL "
        "list each time — it replaces the current list. Each item is "
        "{content, status} where status is one of pending | in_progress | "
        "completed. Keep exactly one item in_progress at a time. This tool "
        "only tracks the plan; it does not perform any work itself."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def __init__(self, manager: TodoManager):
        self.manager = manager

    def execute(self, *, todos) -> str:
        return self.manager.update(todos)
