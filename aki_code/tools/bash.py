from __future__ import annotations

import subprocess

from aki_code.tools.base import BaseTool


class BashTool(BaseTool):
    name = "bash"
    description = "Run a shell command. Returns stdout, stderr and exit code."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Set true to run a long-running command (install, build, "
                    "full test suite) in the background and continue without "
                    "waiting. The call returns a background task id immediately "
                    "and you are notified with the result on a later turn. Only "
                    "use it when the next steps do not depend on this command's "
                    "output."
                ),
            },
        },
        "required": ["command"],
    }

    def execute(self, *, command: str, run_in_background: bool = False) -> str:
        # run_in_background is routed by the agent loop before execution reaches
        # here; on the synchronous path the flag is accepted and ignored.
        # shell=True resolves to cmd.exe on Windows and /bin/sh on POSIX.
        # This must match aki_code.shell.get_execution_shell(), which is what
        # the system prompt reports to the model.
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 120s"

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
