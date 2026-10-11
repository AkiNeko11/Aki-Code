from __future__ import annotations

import os

from aki_code.tools.base import BaseTool


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write content to a file. Creates file and parent directories if needed. Overwrites existing content."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write",
            },
            "content": {
                "type": "string",
                "description": "Content to write",
            },
        },
        "required": ["path", "content"],
    }

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
