from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from aki_code import config

# Message types. Free-form collaboration uses MSG_MESSAGE; the control protocols
# (Phase D) use the typed request/response events so a reply can be matched to
# its request and mismatched or duplicate replies rejected.
MSG_MESSAGE = "message"
MSG_RESULT = "result"
MSG_IDLE = "idle_notification"
MSG_SHUTDOWN_REQUEST = "shutdown_request"
MSG_SHUTDOWN_RESPONSE = "shutdown_response"
MSG_PLAN_REQUEST = "plan_request"
MSG_PLAN_APPROVAL_REQUEST = "plan_approval_request"
MSG_PLAN_APPROVAL_RESPONSE = "plan_approval_response"

# Agent names become mailbox filenames, so sanitize them against traversal.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def _safe_name(agent: str) -> str:
    safe = _UNSAFE_NAME.sub("_", str(agent)).strip("._")
    return safe or "agent"


class MessageBus:
    """File-mailbox message passing between Lead and teammates (Lesson 13).

    Each agent owns .mailboxes/<name>.jsonl. send() appends a JSON line and
    wakes waiters; read_inbox() drains and deletes the file (one consumer per
    agent); wait_for_messages() blocks on a Condition until mail arrives or the
    timeout elapses, which is how an IDLE teammate polls for work.

    Keeping messages on disk (not in a shared messages array) means one
    teammate's tool results never leak into another's reasoning context. The
    Condition is in-process: the whole team runs as threads of one CLI, so
    cross-process signalling is not needed.
    """

    def __init__(self, mailbox_dir: str | Path = config.MAILBOX_DIR):
        self.mailbox_dir = Path(mailbox_dir)
        self._changed = threading.Condition()

    def _path(self, agent: str) -> Path:
        return self.mailbox_dir / f"{_safe_name(agent)}.jsonl"

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = MSG_MESSAGE,
        metadata: dict | None = None,
    ) -> dict:
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "metadata": metadata or {},
        }
        line = json.dumps(msg, ensure_ascii=True)
        with self._changed:
            self.mailbox_dir.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._changed.notify_all()
        return msg

    def peek(self, agent: str) -> bool:
        """True if the agent has undelivered mail (does not consume)."""
        path = self._path(agent)
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def read_inbox(self, agent: str) -> list[dict]:
        """Drain and delete the agent's mailbox; returns messages in order."""
        with self._changed:
            return self._read_unlocked(agent)

    def _read_unlocked(self, agent: str) -> list[dict]:
        path = self._path(agent)
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        messages: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip a torn/partial line rather than lose the rest
            if isinstance(parsed, dict):
                messages.append(parsed)
        try:
            path.unlink()
        except OSError:
            pass
        return messages

    def wait_for_messages(self, agent: str, timeout: float | None = None) -> list[dict]:
        """Block until the agent has mail, or the timeout elapses (then [])."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)
