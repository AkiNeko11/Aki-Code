from __future__ import annotations

from rich.markup import escape

from aki_code import config
from aki_code.llm.client import call_with_retry
from aki_code.hooks.manager import HookEvent

MAX_TURNS = 30
# The subagent shares the registry but must not recurse, touch shared state
# (the todo list, the durable task graph), or request compaction (its loop does
# not run the prepare() pipeline). Task claiming is the parent's job until
# multi-agent coordination lands. connect_mcp is excluded so a subagent cannot
# attach new servers, but the mcp__ tools the lead already connected ARE offered
# to it (Lesson 15), gated by the same host MCP policy as everywhere else.
EXCLUDED_TOOLS = (
    "task",
    "todo_write",
    "compact",
    "create_task",
    "get_task",
    "list_tasks",
    "update_task",
    "claim_task",
    "complete_task",
    "schedule_cron",
    "list_crons",
    "cancel_cron",
    "connect_mcp",
    "workflow",
    "spawn_teammate",
    "list_teammates",
    "send_message",
    "request_plan",
    "review_plan",
    "request_shutdown",
    "submit_plan",
    "create_worktree",
)


def _subagent_visible(schema_name: str) -> bool:
    return schema_name not in EXCLUDED_TOOLS


def extract_text(content) -> str:
    """Concatenate the text blocks of a response's content."""
    return "".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    ).strip()


def run_subagent(
    *,
    prompt: str,
    client,
    registry,
    permission_manager,
    hook_manager,
    ask_permission,
    system: str,
    console=None,
    max_turns: int = MAX_TURNS,
) -> str:
    """Run a nested agent loop with a fresh message list; return its final text.

    Isolation is at the message level only: the subagent shares the working
    directory, permission rules, and lifecycle hooks with the parent. Its tool
    calls never enter the parent's conversation — only the final text does.

    The loop is the shared AgentHarness (Lesson 15), configured for a one-shot
    subagent: filtered schemas, interactive permission, no compaction / memory /
    background routing, and a forked HookManager so its exclusion + permission
    PreToolUse hooks never leak onto the parent or a teammate.
    """
    # Imported lazily: harness -> compact -> agent -> subagent would otherwise be
    # a circular import at module load (the same reason team.teammate defers its
    # registry import).
    from aki_code.agent.harness import AgentHarness

    sub_hooks = hook_manager.fork()

    # Defense in depth: even though excluded tools are filtered out of the
    # schemas, refuse one the model still asks for. Registered before the
    # harness's permission hook so a doomed tool is blocked without prompting.
    def _exclusion_hook(ctx):
        name = ctx.data.get("tool_name")
        if not _subagent_visible(name):
            ctx.data["block"] = f"Tool '{name}' is not available inside a subagent."
        return ctx

    sub_hooks.register(HookEvent.PRE_TOOL_USE, _exclusion_hook)

    def _dispatch(tool_use):
        return registry.execute(tool_use.name, **tool_use.input)

    def _llm(messages, tools, system_prompt):
        return call_with_retry(
            lambda: client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=system_prompt,
                messages=messages,
                tools=tools,
            )
        )

    def _tool_start(tool_use):
        if console is not None:
            console.print(
                f"  \\[sub] [cyan]→ {tool_use.name}[/cyan]"
                f"{escape(f'({tool_use.input})')}"
            )

    def _tool_result(result):
        if console is not None:
            preview = result if len(result) <= 500 else result[:500] + "..."
            console.print(f"  \\[sub] [dim]← {escape(preview)}[/dim]")

    harness = AgentHarness(
        hook_manager=sub_hooks,
        schema_provider=lambda: [
            s for s in registry.get_schemas() if _subagent_visible(s["name"])
        ],
        dispatch=_dispatch,
        llm_caller=_llm,
        permission_manager=permission_manager,
        system_prompt=system,
        on_tool_start=_tool_start,
        on_tool_result=_tool_result,
        text_extractor=extract_text,
        max_turns=max_turns,
        reraise_interrupt=True,
    )

    messages: list[dict] = [{"role": "user", "content": prompt}]
    result = harness.run_turn(messages, ask=ask_permission)
    if result.ok:
        return result.text or "(no summary)"
    # max_turns carries its own explanation; an API error leaves the text empty.
    return result.text or "(subagent stopped without a result)"
