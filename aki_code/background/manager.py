from __future__ import annotations

import os
import signal
import subprocess
import threading

from aki_code import config


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """True only when bash is explicitly asked to run in the background.

    The decision is the model's, via the run_in_background flag — never guessed
    from keywords like install/build/test.
    """
    return tool_name == "bash" and bool(tool_input) and tool_input.get("run_in_background") is True


def _popen_kwargs() -> dict:
    """Start the child in its own process group so the whole tree can be killed."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def _kill_group(proc: subprocess.Popen) -> None:
    """Best-effort kill of a process and its group/tree."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            # Windows has no killpg; taskkill /T takes down the whole tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _combine(stdout: bytes | str | None, stderr: bytes | str | None) -> str:
    from aki_code.shell import decode_shell_output

    output = decode_shell_output(stdout)
    if stderr:
        output += f"\n[stderr]\n{decode_shell_output(stderr)}"
    return output


def _format_bash_result(output: str, exit_code: int, max_output: int) -> str:
    text = (output or "").strip() or "(no output)"
    if len(text) > max_output:
        text = text[:max_output] + f"\n...[truncated to {max_output} chars]"
    return text + f"\n[exit code: {exit_code}]"


def _format_notification(task_id: str, command: str, status: str, result: str) -> str:
    return (
        "<task_notification>\n"
        f"Background task {task_id} {status}: {command}\n"
        f"{result}\n"
        "</task_notification>"
    )


class BackgroundManager:
    """Run slow bash commands off the main thread (Lesson 11).

    start() registers a task, launches it in a daemon thread inside its own
    process group, and returns a bg id immediately so the agent loop can keep
    going. When a job finishes (or fails/times out) its formatted result is
    queued; collect() drains that queue into <task_notification> strings that
    the loop injects on a later turn. teardown_all() kills any process groups
    still running when the agent exits — lifecycle cleanup, not a sandbox.
    """

    def __init__(
        self,
        *,
        timeout: int = config.BACKGROUND_JOB_TIMEOUT,
        max_output: int = config.BACKGROUND_MAX_OUTPUT_CHARS,
    ):
        self.timeout = timeout
        self.max_output = max_output
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self._ready: list[str] = []
        self._procs: dict[str, subprocess.Popen] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def start(self, command: str, *, goal_id: str | None = None, cwd: str | None = None) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {
                "id": task_id,
                "command": command,
                "status": "running",
                "goal_id": goal_id,
                "cwd": cwd or os.getcwd(),
            }
        thread = threading.Thread(
            target=self._run, args=(task_id, command), daemon=True
        )
        thread.start()
        return task_id

    def _run(self, task_id: str, command: str) -> None:
        output = ""
        exit_code = -1
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.tasks[task_id]["cwd"],
                **_popen_kwargs(),
            )
            with self._lock:
                self._procs[task_id] = proc
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
                exit_code = proc.returncode if proc.returncode is not None else -1
                output = _combine(stdout, stderr)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                stdout, stderr = proc.communicate()
                output = _combine(stdout, stderr) + f"\n[timed out after {self.timeout}s]"
                exit_code = proc.returncode if proc.returncode is not None else -1
        except Exception as exc:  # a worker failure still yields a result
            output = f"Error: {exc}"
            exit_code = -1
        finally:
            with self._lock:
                self._procs.pop(task_id, None)

        status = "completed" if exit_code == 0 else "failed"
        with self._lock:
            self.tasks[task_id]["status"] = status
            self.results[task_id] = _format_bash_result(output, exit_code, self.max_output)
            self._ready.append(task_id)

    def has_ready(self) -> bool:
        with self._lock:
            return bool(self._ready)

    def has_pending(self, goal_id: str) -> bool:
        """Results queued but not delivered still count as pending evidence."""
        with self._lock:
            return any(task.get("goal_id") == goal_id
                       and (task["status"] == "running" or task_id in self._ready)
                       for task_id, task in self.tasks.items())

    def collect_records(self) -> list[dict]:
        """Atomically drain results with host-owned goal identity metadata."""
        with self._lock:
            ready = self._ready[:]
            self._ready.clear()
            records = []
            for task_id in ready:
                task = dict(self.tasks[task_id])
                task["result"] = self.results[task_id]
                task["notification"] = _format_notification(
                    task_id, task["command"], task["status"], task["result"])
                records.append(task)
            return records

    def collect(self) -> list[str]:
        return [record["notification"] for record in self.collect_records()]

    def teardown_all(self) -> None:
        """Kill every process group still running (called on agent exit)."""
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            _kill_group(proc)


def inject_background_results(messages: list, manager: BackgroundManager, *, on_result=None) -> None:
    """Merge any completed background notifications into the conversation.

    The original bash tool_use already got its placeholder tool_result, so a
    notification is added as an independent text event — never reusing that
    tool_use_id. It is merged into the trailing user message (converting string
    content to a block list if needed) to keep user/assistant alternation valid.
    """
    records = manager.collect_records()
    if not records:
        return
    notes = [record["notification"] for record in records]
    block = {"type": "text", "text": "\n\n".join(notes)}
    if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, list):
            content.append(block)
        else:
            messages[-1]["content"] = [{"type": "text", "text": content}, block]
    else:
        messages.append({"role": "user", "content": [block]})
    if on_result is not None:
        for record in records:
            on_result(record)
