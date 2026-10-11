from __future__ import annotations

import os

from aki_code.tools.bash import Tool


class FileReadTool(Tool):
    def __init__(self):
        super().__init__(
            name="file_read",
            description="Read the contents of a file. Returns the file content with line numbers.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to read (absolute or relative to current directory)",
                    },
                },
                "required": ["path"],
            },
        )

    def execute(self, *, path: str) -> str:
        if not os.path.exists(path):
            return f"Error: file not found: {path}"
        if not os.path.isfile(path):
            return f"Error: not a file: {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            numbered = [f"{i+1:>4}\t{line}" for i, line in enumerate(lines)]
            return "".join(numbered)
        except UnicodeDecodeError:
            return f"Error: file is not valid UTF-8: {path}"
        except Exception as e:
            return f"Error reading file: {e}"
