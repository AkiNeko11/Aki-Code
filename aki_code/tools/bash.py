from __future__ import annotations

import subprocess

from aki_code.tools.base import WorkspaceTool
from aki_code.shell import decode_shell_output
from aki_code.background.manager import _popen_kwargs, _kill_group


class BashTool(WorkspaceTool):
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

    def __init__(self, cwd_provider=None, *, event_sink=None):
        super().__init__(cwd_provider)
        self._event_sink = event_sink

    def execute(self, *, command: str, run_in_background: bool = False) -> str:
        # run_in_background is routed by the agent loop before execution reaches
        # here; on the synchronous path the flag is accepted and ignored.
        # shell=True resolves to cmd.exe on Windows and /bin/sh on POSIX.
        # This must match aki_code.shell.get_execution_shell(), which is what
        # the system prompt reports to the model.
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self._cwd(), **_popen_kwargs())
        try:
            if self._event_sink is not None:
                self._event_sink({"type": "shell_process", "pid": proc.pid})
            try:
                stdout, stderr = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.communicate()
                return "Error: command timed out after 120s"
        finally:
            _kill_group(proc)
            proc.wait()
            if self._event_sink is not None:
                self._event_sink({"type": "shell_process_finished", "pid": proc.pid})

        output = ""
        if stdout:
            output += decode_shell_output(stdout)
        if stderr:
            output += f"\n[stderr]\n{decode_shell_output(stderr)}"
        output = output.strip() or "(no output)"
        return f"{output}\n[exit code: {proc.returncode}]"
