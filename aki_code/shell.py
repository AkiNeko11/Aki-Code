from __future__ import annotations

import platform


def decode_shell_output(data: bytes | str | None) -> str:
    """Decode Python's UTF-8 output and Windows native command output safely."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # mbcs follows the Windows ANSI code page even under Python's UTF-8
        # mode. Decode bytes ourselves so subprocess reader threads cannot die.
        encoding = "mbcs" if platform.system() == "Windows" else "utf-8"
        return data.decode(encoding, errors="replace")


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
