from __future__ import annotations

import os

from aki_code.tools.bash import Tool


class FileWriteTool(Tool):
    def __init__(self):
        super().__init__(
            name="file_write",
            description="Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        )

    def execute(self, *, path: str, content: str) -> str:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote to {path}"
        except Exception as e:
            return f"Error writing file: {e}"
