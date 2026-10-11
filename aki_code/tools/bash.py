from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict

    def execute(self, **kwargs) -> str:
        raise NotImplementedError


class BashTool(Tool):
    def __init__(self):
        super().__init__(
            name="bash",
            description="Execute a bash command in the shell. Returns stdout and stderr.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute",
                    }
                },
                "required": ["command"],
            },
        )

    def execute(self, *, command: str) -> str:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
