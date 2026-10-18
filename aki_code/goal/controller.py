"""Goal gate policy, separate from host input queues and execution tools."""
from __future__ import annotations

from contextlib import contextmanager
import json
import time

from aki_code.agent.lifecycle import StopDecision, TurnLimitReached
from aki_code.goal.evaluator import clip, plain, validate_verdict
from aki_code.goal.model import GoalState


def _add_usage(target, response):
    target["calls"] += 1
    usage = getattr(response, "usage", None)
    for name in target:
        if name == "calls":
            continue
        value = getattr(usage, name, 0) or 0
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[name] += value


class GoalController:
    def __init__(self, evaluator, *, max_stop_blocks=10, max_turns=None,
                 pending_work=None, event_sink=None, history_chars=48000):
        if isinstance(max_stop_blocks, bool) or not isinstance(max_stop_blocks, int) or max_stop_blocks < 1:
            raise ValueError("max_stop_blocks must be a positive integer")
        if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
            raise ValueError("max_turns must be a positive integer or None")
        if history_chars < 2000:
            raise ValueError("history_chars must be >= 2000")
        self.evaluator = evaluator
        self.max_stop_blocks = max_stop_blocks
        self.max_turns = max_turns
        self.pending_work = pending_work or (lambda: False)
        self.event_sink = event_sink
        self.history_chars = history_chars
        self.state: GoalState | None = None
        self._suppressed = 0

    @property
    def active(self):
        return bool(self.state and self.state.active)

    @property
    def current_id(self):
        return self.state.goal_id if self.active and not self._suppressed else None

    @property
    def automatic_allowed(self):
        return bool(self.current_id and self.state.automatic)

    @property
    def suppressed(self):
        return bool(self._suppressed)

    @contextmanager
    def suspend(self):
        """Unrelated host deliveries must not start a goal continuation chain."""
        self._suppressed += 1
        try:
            yield
        finally:
            self._suppressed -= 1

    def set(self, condition):
        if not isinstance(condition, str) or not condition.strip() or len(condition) > 8000:
            raise ValueError("Goal condition must contain 1-8000 characters")
        self.state = GoalState(condition=condition.strip())
        self._emit()
        return self.initial_prompt()

    def initial_prompt(self):
        return (f"[Goal {self.state.goal_id}]\nComplete this task and satisfy its completion condition:\n"
                f"{self.state.condition}\nUse tools to perform and verify the work. "
                "Record verification commands and actual results, including exit codes. "
                "An independent no-tool reviewer will check the conversation before completion.")

    def clear(self):
        if self.state is not None:
            self.state.active = self.state.automatic = False
            self.state.finished_at = time.time()
            self.state.status, self.state.reason = "cleared", "Goal cleared by the user"
            self._emit()

    def resume_for_user(self):
        if self.active:
            self.state.automatic = True
            self.state.blocks = self.state.chain_turns = 0
            self.state.status = "active"

    def status(self):
        if self.state is None:
            return {"active": False, "status": "none"}
        state = self.state
        def usage_summary(usage):
            return {**usage, "total_tokens": sum(value for key, value in usage.items() if key != "calls")}

        return {"goal_id": state.goal_id, "condition": state.condition,
                "active": state.active, "automatic": state.automatic, "status": state.status,
                "checks": state.checks, "consecutive_blocks": state.blocks,
                "chain_turns": state.chain_turns,
                "elapsed_seconds": max(0, (state.finished_at or time.time()) - state.started_at),
                "reason": state.reason, "agent_usage": usage_summary(state.agent_usage),
                "evaluator_usage": usage_summary(state.evaluator_usage)}

    def restore(self, event):
        saved = event.get("goal", event) if isinstance(event, dict) else {}
        if (not isinstance(saved, dict) or saved.get("active") is not True
                or saved.get("status") in {"achieved", "failed", "cleared"}
                or not isinstance(saved.get("condition"), str) or not saved["condition"].strip()):
            return False
        if len(saved["condition"]) > 8000:
            return False
        self.state = GoalState(
            condition=saved["condition"].strip(), automatic=False, status="restored",
            reason="Goal condition restored without conversation or running jobs; user input is required",
        )
        self._emit()
        return True

    def _emit(self):
        if self.event_sink is not None:
            try:
                self.event_sink({"type": "goal_status", "goal": self.status()})
            except Exception:
                pass

    def _decision(self, action, reason):
        state = self.state
        state.status, state.reason = action, reason
        if action in {"achieved", "failed"}:
            state.active = state.automatic = False
            state.finished_at = time.time()
        elif action in {"limit", "goal_error", "error", "interrupted"}:
            state.automatic = False
        self._emit()
        return StopDecision(action, reason)

    def before_model(self, ctx):
        if not self.current_id:
            return ctx
        if not self.state.automatic:
            raise TurnLimitReached("Goal automatic continuation is stopped; enter a new message to continue")
        if self.max_turns is not None and self.state.chain_turns >= self.max_turns:
            raise TurnLimitReached(f"Stopped after {self.max_turns} model turns in this continuation chain")
        self.state.chain_turns += 1
        return ctx

    def on_model_response(self, ctx):
        if self.current_id:
            _add_usage(self.state.agent_usage, ctx.data["response"])

    def on_evaluator_response(self, response):
        if self.current_id:
            _add_usage(self.state.evaluator_usage, response)

    def record_message(self, ctx):
        if self.current_id:
            self.observe_message(ctx.data["message"])

    def observe_message(self, message):
        if not self.current_id:
            return
        # A bounded projection of actual messages before compaction, not a new
        # executor or a source of synthesized verification claims.
        value = plain(message)
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(text) > min(12000, self.history_chars):
            content = value.get("content", value)
            content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            value = {"role": value.get("role", "unknown"),
                     "content": clip(content, min(11000, self.history_chars - 100))}
        self.state.history.append(value)
        size = sum(len(json.dumps(item, ensure_ascii=False)) for item in self.state.history)
        while len(self.state.history) > 1 and size > self.history_chars:
            size -= len(json.dumps(self.state.history.pop(0), ensure_ascii=False))

    def on_turn_stopped(self, ctx):
        if self.current_id and ctx.data.get("status") in {"limit", "error", "interrupted"}:
            self._decision(ctx.data["status"], ctx.data.get("reason", "Agent stopped"))

    def stop_hook(self, ctx):
        ctx.data["decision"] = self.evaluate_after_turn(ctx.data["messages"])
        return ctx

    def evaluate_after_turn(self, messages):
        if not self.current_id:
            return StopDecision()
        if not self.state.automatic:
            return StopDecision(self.state.status, self.state.reason)
        try:
            if self.pending_work():
                return self._decision("defer", "Waiting for related background or teammate results")
            if self.state.blocks >= self.max_stop_blocks:
                return self._decision("limit", "Stop-hook continuation limit reached; goal remains active")
            self.state.checks += 1
            # Keep raw observations available even after the main context has
            # replaced older tool results with summaries or archive pointers.
            records = self.state.history if self.state.history else messages
            verdict = validate_verdict(self.evaluator.evaluate(self.state.condition, records))
        except KeyboardInterrupt:
            self._decision("interrupted", "Goal judgment interrupted; goal remains active")
            raise
        except Exception as exc:
            return self._decision("goal_error", f"Goal judgment failed: {exc}")
        if verdict["ok"]:
            return self._decision("achieved", verdict["reason"])
        if verdict["impossible"]:
            return self._decision("failed", verdict["reason"])
        self.state.blocks += 1
        if self.state.blocks >= self.max_stop_blocks:
            return self._decision("limit", f"Stop-hook continuation limit reached: {verdict['reason']}")
        return self._decision("block", verdict["reason"])

    def prompt_section(self):
        if not self.current_id:
            return ""
        return ("\n\n# Active session goal\n" + self.state.condition
                + "\nThe goal is checked by an independent, no-tool reviewer. Use tools to "
                "verify it, include commands and actual exit codes in the record, and "
                "revalidate after changing relevant code. Do not treat launched background "
                "jobs, summaries, or a completed review workflow as passed tests.")
