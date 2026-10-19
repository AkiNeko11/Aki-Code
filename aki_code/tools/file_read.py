from __future__ import annotations

import os
from itertools import islice

from aki_code.tools.base import WorkspaceTool


class ReadFileTool(WorkspaceTool):
    name = "read_file"
    description = "Read a UTF-8 file with line numbers. Use offset and limit to read a portion of a large file."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read",
            },
            "offset": {
                "type": "integer", "minimum": 1,
                "description": "First line to read (1-based). Defaults to 1.",
            },
            "limit": {
                "type": "integer", "minimum": 1,
                "description": "Maximum lines to return. Omit to read all remaining lines.",
            },
        },
        "required": ["path"],
    }

    def execute(self, *, path: str, offset: int = 1, limit: int | None = None) -> str:
        if type(offset) is not int or offset < 1:
            return "Error: offset must be a positive integer (1-based)."
        if limit is not None and (type(limit) is not int or limit < 1):
            return "Error: limit must be a positive integer."
        path = self._resolve(path)
        if not os.path.exists(path):
            return f"Error: file not found: {path}"
        if not os.path.isfile(path):
            return f"Error: not a file: {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                stop = offset - 1 + limit if limit is not None else None
                lines = list(islice(f, offset - 1, stop))
            numbered = [f"{i:>4}\t{line}" for i, line in enumerate(lines, start=offset)]
            return "".join(numbered)
        except UnicodeDecodeError:
            return f"Error: file is not valid UTF-8: {path}"
        except Exception as e:
            return f"Error reading file: {e}"
