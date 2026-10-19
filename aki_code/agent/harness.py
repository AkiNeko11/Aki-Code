from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aki_code.compact import is_prompt_too_long
from aki_code.hooks.manager import HookEvent
from aki_code.agent.lifecycle import StopDecision, TurnLimitReached


@dataclass
class TurnResult:
    """Outcome of one harness turn.

    ok describes execution, not goal achievement. A goal can defer or reach its
    stop-block limit with ok=True; status/reason explain the handoff. Model
    errors, interruptions and model-turn exhaustion return ok=False. text is the
    final assistant text when requested, empty for the streaming lead.
    """

    ok: bool
    text: str = ""
    status: str = "completed"
    reason: str = ""
    turns: int = 0


def permission_hook_factory(permission_manager):
    """A PreToolUse hook that turns a permission decision into a block.

    Permission lives on the hook point (course s15) rather than inline in the
    loop, so permission, logging and audit all attach to one PreToolUse chain.
    The per-turn `ask` callback arrives in ctx.data: an interactive turn passes a
    real prompter, a scheduled turn passes one that always denies, and a
    non-interactive agent (teammate) passes None plus its own ask_block_message.
    Setting ctx.data['block'] tells the harness to skip execution and return the
    message as the tool_result.
    """

    def permission_hook(ctx):
        data = ctx.data
        if data.get("block"):
            # An earlier PreToolUse hook (e.g. a subagent's tool-exclusion) already
            # blocked this call; do not re-evaluate or prompt for a doomed tool.
            return ctx
        name = data.get("tool_name")
        tool_input = data.get("tool_input") or {}
        decision, reason = permission_manager.check(name, tool_input)
        if decision == "deny":
            data["block"] = f"Denied: {reason}"
            return ctx
        if decision == "ask":
            ask = data.get("ask")
            if ask is None:
                template = data.get("ask_block_message")
                if template:
                    data["block"] = (
                        template.format(reason=reason)
                        if "{reason}" in template
                        else template
                    )
                else:
                    data["block"] = (
                        f"Permission needed; cannot prompt the user: {reason}"
                    )
            elif not ask(name, tool_input, reason):
                data["block"] = "User denied this action."
        return ctx

    return permission_hook


class AgentHarness:
    """The one agent loop, configured per agent type (Lesson 15).

    The lead REPL, a one-shot subagent and a persistent teammate all run the same
    skeleton — call the model, check for tool_use blocks, dispatch each tool, feed
    the results back, repeat — and differ only in the pieces injected here: which
    tools are offered, how the model is called, whether context is compacted, how
    permission is resolved, and what is displayed. Permission is a PreToolUse hook
    (registered here when a permission_manager is given) so it can block a call;
    a turn ends by firing STOP, where the lead hangs memory extraction.

    The caller owns `messages`; the harness mutates that list in place and rolls
    back to the pre-call snapshot on interruption or error, so a shared
    conversation (the lead's) or an owned one (a subagent's) both work.
    """

    def __init__(
        self,
        *,
        hook_manager,
        schema_provider: Callable[[], list],
        dispatch: Callable[[object], str],
        llm_caller: Callable[[list, list, str], object],
        permission_manager=None,
        system_prompt: str = "",
        system_prompt_provider: Callable[[str], str] | None = None,
        compactor=None,
        pre_model: Callable[[list], None] | None = None,
        abort_check: Callable[[], bool] | None = None,
        on_tool_start: Callable[[object], None] | None = None,
        on_tool_result: Callable[[str], None] | None = None,
        on_interrupt: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        todo_nudge: bool = False,
        text_extractor: Callable[[object], str] | None = None,
        max_turns: int | None = None,
        reraise_interrupt: bool = False,
    ):
        self.hook_manager = hook_manager
        self.schema_provider = schema_provider
        self.dispatch = dispatch
        self.llm_caller = llm_caller
        self.system_prompt = system_prompt
        self.system_prompt_provider = system_prompt_provider
        self.compactor = compactor
        self.pre_model = pre_model
        self.abort_check = abort_check
        self.on_tool_start = on_tool_start
        self.on_tool_result = on_tool_result
        self.on_interrupt = on_interrupt
        self.on_error = on_error
        self.todo_nudge = todo_nudge
        self.text_extractor = text_extractor
        self.max_turns = max_turns
        # A one-shot subagent runs on the caller's thread, so an interrupt there
        # must still bubble up and abort the parent turn (the lead swallows its
        # own interrupt instead); reraise_interrupt preserves that.
        self.reraise_interrupt = reraise_interrupt
        # Persists across turns on this instance, matching the lead's prior
        # nonlocal counter: three tool rounds without a todo_write nudge the model.
        self._rounds_since_todo = 0

        if permission_manager is not None:
            hook_manager.register(
                HookEvent.PRE_TOOL_USE, permission_hook_factory(permission_manager)
            )

    def run_turn(
        self,
        messages: list,
        active_request: str = "",
        *,
        ask: Callable[[str, dict, str], bool] | None = None,
        ask_block_message: str | None = None,
    ) -> TurnResult:
        system = (
            self.system_prompt_provider(active_request)
            if self.system_prompt_provider is not None
            else self.system_prompt
        )
        reactive_retries = 0
        iterations = 0

        def append_message(message):
            messages.append(message)
            self.hook_manager.fire(HookEvent.MESSAGE_APPENDED, message=message)

        def stopped(status, reason):
            self.hook_manager.fire(HookEvent.TURN_STOPPED, status=status, reason=reason)
            return TurnResult(False, reason, status, reason, iterations)

        while True:
            if self.max_turns is not None and iterations >= self.max_turns:
                return stopped("limit", f"Stopped after {self.max_turns} turns without a final answer.")
            iterations += 1

            if self.pre_model is not None:
                self.pre_model(messages)
            # An agent that drains its inbox in pre_model (a teammate) can be
            # asked to shut down mid-turn; stop before spending a model call.
            if self.abort_check is not None and self.abort_check():
                self.hook_manager.fire(HookEvent.TURN_STOPPED, status="interrupted", reason="Agent stopped")
                return TurnResult(False, status="interrupted", reason="Agent stopped", turns=iterations)
            if self.compactor is not None:
                messages[:] = self.compactor.prepare(messages, active_request)
            # Snapshot the clean pre-call state so a mid-round failure rolls back
            # without leaving a dangling tool_use.
            snapshot = len(messages)

            try:
                self.hook_manager.fire(HookEvent.PRE_LLM_CALL, messages=messages)
                response = self.llm_caller(messages, self.schema_provider(), system)
                reactive_retries = 0
                self.hook_manager.fire(HookEvent.POST_LLM_CALL, response=response)

                append_message({"role": "assistant", "content": response.content})
                tool_uses = [b for b in response.content if b.type == "tool_use"]

                if not tool_uses:
                    text = (
                        self.text_extractor(response.content)
                        if self.text_extractor is not None
                        else ""
                    )
                    gate = self.hook_manager.fire(
                        HookEvent.BEFORE_STOP, messages=messages, text=text,
                        decision=StopDecision(),
                    )
                    decision = gate.data["decision"]
                    if not isinstance(decision, StopDecision) or decision.action not in {
                        "allow", "block", "defer", "achieved", "failed", "limit", "goal_error", "restored"
                    }:
                        raise ValueError("Invalid stop-hook decision")
                    if decision.action == "block":
                        append_message({"role": "user", "content":
                                        "[Completion check: continue working]\n" + decision.reason})
                        continue
                    if decision.action in {"allow", "achieved", "failed"}:
                        self.hook_manager.fire(HookEvent.STOP, messages=messages,
                                               status=decision.action)
                    status = "completed" if decision.action == "allow" else decision.action
                    return TurnResult(True, text, status, decision.reason, iterations)

                tool_results = []
                compact_requested = False
                for tool_use in tool_uses:
                    ctx = self.hook_manager.fire(
                        HookEvent.PRE_TOOL_USE,
                        tool_name=tool_use.name,
                        tool_input=tool_use.input,
                        tool_use_id=tool_use.id,
                        ask=ask,
                        ask_block_message=ask_block_message,
                    )
                    block = ctx.data.get("block")
                    if block:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": block,
                        })
                        continue

                    if self.on_tool_start is not None:
                        self.on_tool_start(tool_use)
                    result = self.dispatch(tool_use)
                    self.hook_manager.fire(
                        HookEvent.POST_TOOL_USE,
                        tool_name=tool_use.name,
                        tool_input=tool_use.input,
                        tool_use_id=tool_use.id,
                        result=result,
                    )
                    if self.on_tool_result is not None:
                        self.on_tool_result(result)
                    # The compact tool only signals intent; the real compaction
                    # runs once the whole batch is appended, below.
                    if tool_use.name == "compact":
                        compact_requested = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })

                if self.todo_nudge:
                    used_todo = any(t.name == "todo_write" for t in tool_uses)
                    self._rounds_since_todo = 0 if used_todo else self._rounds_since_todo + 1
                    if self._rounds_since_todo >= 3:
                        tool_results.append({
                            "type": "text",
                            "text": "<reminder>Update your todos.</reminder>",
                        })
                        self._rounds_since_todo = 0

                append_message({"role": "user", "content": tool_results})

                # Batch is closed: collapse history into a summary now.
                if self.compactor is not None and compact_requested:
                    messages[:] = self.compactor.compact_history(messages, active_request)

            except TurnLimitReached as exc:
                return stopped("limit", str(exc))
            except KeyboardInterrupt:
                del messages[snapshot:]
                self.hook_manager.fire(HookEvent.TURN_STOPPED, status="interrupted", reason="Interrupted by user")
                if self.on_interrupt is not None:
                    self.on_interrupt()
                if self.reraise_interrupt:
                    raise
                return TurnResult(False, status="interrupted", reason="Interrupted by user", turns=iterations)
            except Exception as e:
                if (
                    self.compactor is not None
                    and is_prompt_too_long(e)
                    and reactive_retries < self.compactor.max_reactive_retries
                ):
                    messages[:] = self.compactor.reactive_compact(messages, active_request)
                    reactive_retries += 1
                    continue
                del messages[snapshot:]
                self.hook_manager.fire(HookEvent.ON_ERROR, error=str(e))
                self.hook_manager.fire(HookEvent.TURN_STOPPED, status="error", reason=str(e))
                if self.on_error is not None:
                    self.on_error(e)
                return TurnResult(False, status="error", reason=str(e), turns=iterations)
