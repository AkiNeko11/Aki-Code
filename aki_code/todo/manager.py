from __future__ import annotations

import ast
import json
from dataclasses import dataclass

MAX_ITEMS = 20
VALID_STATUSES = ("pending", "in_progress", "completed")
STATUS_ICONS = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
}


@dataclass
class Todo:
    content: str
    status: str


class TodoManager:
    """In-memory task list. Holds state, validates updates, renders for display.

    Adding/updating todos does not execute any work — it is a planning aid.
    """

    def __init__(self) -> None:
        self.items: list[Todo] = []

    def update(self, todos: list | str) -> str:
        """Validate and replace the whole list, then return the rendering.

        On validation failure the current list is left untouched.
        """
        parsed = self._parse(todos)
        self.items = self._validate(parsed)
        return self.render()

    def _parse(self, todos: list | str) -> list:
        if isinstance(todos, str):
            text = todos.strip()
            try:
                todos = json.loads(text)
            except json.JSONDecodeError:
                # Safe literal parsing (JSON-alternative Python repr); never eval.
                todos = ast.literal_eval(text)
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        return todos

    def _validate(self, raw: list) -> list[Todo]:
        if len(raw) > MAX_ITEMS:
            raise ValueError(f"too many todos (max {MAX_ITEMS}, got {len(raw)})")

        validated: list[Todo] = []
        in_progress = 0
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ValueError(f"todo #{i} must be an object")
            content = entry.get("content")
            status = entry.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"todo #{i} has empty content")
            if status not in VALID_STATUSES:
                raise ValueError(f"todo #{i} has invalid status '{status}'")
            if status == "in_progress":
                in_progress += 1
            validated.append(Todo(content=content.strip(), status=status))

        if in_progress > 1:
            raise ValueError("only one todo can be in_progress at a time")
        return validated

    def render(self) -> str:
        if not self.items:
            return "(no todos)"
        return "\n".join(f"{STATUS_ICONS[t.status]} {t.content}" for t in self.items)
