from __future__ import annotations

import os

from aki_code.tools.base import WorkspaceTool


class ReadFileTool(WorkspaceTool):
    name = "read_file"
    description = "Read the contents of a file. Returns content with line numbers."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read",
            },
        },
        "required": ["path"],
    }

    def execute(self, *, path: str) -> str:
        path = self._resolve(path)
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
