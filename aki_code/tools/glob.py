from __future__ import annotations

import glob as glob_mod
import os

from aki_code.tools.base import WorkspaceTool


class GlobTool(WorkspaceTool):
    name = "glob"
    description = "Find files by glob pattern. Supports recursive ** matching."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: current directory)",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, *, pattern: str, path: str = ".") -> str:
        try:
            full_pattern = os.path.join(self._resolve(path), pattern)
            matches = sorted(glob_mod.glob(full_pattern, recursive=True))
            if not matches:
                return f"No files matching: {full_pattern}"
            if len(matches) > 200:
                return "\n".join(matches[:200]) + f"\n... and {len(matches) - 200} more"
            return "\n".join(matches)
        except Exception as e:
            return f"Error: {e}"
