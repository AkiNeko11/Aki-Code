"""Host-wide limits shared by lead, child agents and auxiliary model calls."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import threading

from aki_code.llm.usage import TokenTracker


class RunStopped(BaseException):
    """Cancellation signal; best-effort model/tool fallbacks must not swallow it.

    Like KeyboardInterrupt, this crosses the harness and is handled by its host.
    Ordinary tool failures remain Exceptions and can still be repaired by agents.
    """

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class RunLimits:
    max_turns: int = 30
    max_calls: int = 60
    max_tokens: int = 100000


class RunControl:
    def __init__(self, limits: RunLimits, emit):
        self.limits = limits
        self.emit = emit
        self.turns = 0
        self.attempts = 0
        self.tracker = TokenTracker()
        self._lock = threading.RLock()

    def snapshot(self):
        with self._lock:
            usage = asdict(self.tracker.usage)
            usage["total_tokens"] = self.tracker.usage.total_tokens
            return {"lead_turns": self.turns, "request_attempts": self.attempts,
                    "usage": usage, "limits": asdict(self.limits)}

    def check(self):
        if self.tracker.usage.total_tokens >= self.limits.max_tokens:
            raise RunStopped("limit", "Total token limit reached (measured from responses).")

    def before_turn(self, ctx):
        with self._lock:
            self.check()
            if self.turns >= self.limits.max_turns:
                raise RunStopped("limit", "Lead model-turn limit reached.")
            self.turns += 1
        self.emit({"type": "lead_turn", **self.snapshot()})

    def before_request(self):
        with self._lock:
            self.check()
            if self.attempts >= self.limits.max_calls:
                raise RunStopped("limit", "Model request-attempt limit reached.")
            self.attempts += 1
        self.emit({"type": "model_request", **self.snapshot()})

    def after_response(self, response):
        with self._lock:
            self.tracker.add_from_response(response)
        self.emit({"type": "model_response", **self.snapshot()})

    def require_permission(self, name, tool_input, reason):
        self.emit({"type": "permission_required", "tool_name": name,
                   "tool_input": tool_input, "reason": reason})
        raise RunStopped("blocked", f"Approval required for {name}: {reason}")


class BudgetClient:
    """Wrap the SDK call boundary, including retries and with_options clones."""

    def __init__(self, client, control: RunControl):
        self._client = client
        self.control = control
        self.messages = _Messages(client.messages, control)

    def with_options(self, **kwargs):
        return BudgetClient(self._client.with_options(**kwargs), self.control)

    def __getattr__(self, name):
        return getattr(self._client, name)


class _Messages:
    def __init__(self, messages, control):
        self._messages = messages
        self._control = control

    def create(self, **kwargs):
        self._control.before_request()
        response = self._messages.create(**kwargs)
        self._control.after_response(response)
        return response

    @contextmanager
    def stream(self, **kwargs):
        self._control.before_request()
        with self._messages.stream(**kwargs) as stream:
            yield _Stream(stream, self._control)


class _Stream:
    def __init__(self, stream, control):
        self._stream = stream
        self._control = control
        self._recorded = False

    def get_final_message(self):
        response = self._stream.get_final_message()
        if not self._recorded:
            self._control.after_response(response)
            self._recorded = True
        return response

    def __getattr__(self, name):
        return getattr(self._stream, name)
