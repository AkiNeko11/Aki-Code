from __future__ import annotations

import os

from aki_code.tools.base import BaseTool


class EditFileTool(BaseTool):
    name = "edit_file"
    description = "Replace a unique string in a file. old_string must appear exactly once."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to edit",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find (must be unique in file)",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def execute(self, *, path: str, old_string: str, new_string: str) -> str:
        if not os.path.exists(path):
            return f"Error: file not found: {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return f"Error reading file: {e}"

        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}"
        if count > 1:
            return f"Error: old_string appears {count} times in {path}, must be unique"

        new_content = content.replace(old_string, new_string, 1)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return f"Successfully edited {path}"
        except Exception as e:
            return f"Error writing file: {e}"
