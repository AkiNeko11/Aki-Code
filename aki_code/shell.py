from __future__ import annotations

import platform


def get_execution_shell() -> str:
    """The shell that BashTool actually runs commands in.

    BashTool uses subprocess.run(..., shell=True), which resolves to COMSPEC
    (cmd.exe) on Windows and /bin/sh on POSIX — NOT the user's interactive
    terminal. This function is the single source of truth so the system prompt
    always describes the same shell the tool executes in. Keep it in sync with
    tools/bash.py.
    """
    if platform.system() == "Windows":
        return "cmd.exe"
    return "sh"
