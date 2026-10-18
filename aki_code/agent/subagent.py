from __future__ import annotations

from rich.markup import escape

from aki_code import config
from aki_code.llm.client import call_with_retry
from aki_code.hooks.manager import HookEvent
from aki_code.mcp.naming import is_mcp_tool

MAX_TURNS = 30
# The subagent shares the registry but must not recurse, touch shared state
# (the todo list, the durable task graph), or request compaction (its loop does
# not run the prepare() pipeline). Task claiming is the parent's job until
# multi-agent coordination lands. connect_mcp is excluded so a subagent cannot
# attach new servers, and discovered mcp__* tools are filtered out by prefix
# below (Lesson 14 keeps MCP on the lead only).
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
)


def _subagent_visible(schema_name: str) -> bool:
    return schema_name not in EXCLUDED_TOOLS and not is_mcp_tool(schema_name)


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
    """
    messages: list[dict] = [{"role": "user", "content": prompt}]
    sub_schemas = [
        s for s in registry.get_schemas() if _subagent_visible(s["name"])
    ]

    for _ in range(max_turns):
        hook_manager.fire(HookEvent.PRE_LLM_CALL, messages=messages)
        response = call_with_retry(
            lambda: client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=system,
                messages=messages,
                tools=sub_schemas,
            )
        )
        hook_manager.fire(HookEvent.POST_LLM_CALL, response=response)
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return extract_text(response.content) or "(no summary)"

        results = []
        for tool_use in tool_uses:
            # Defense in depth: refuse excluded tools even if the model asks.
            if not _subagent_visible(tool_use.name):
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": f"Tool '{tool_use.name}' is not available inside a subagent.",
                })
                continue

            decision, reason = permission_manager.check(tool_use.name, tool_use.input)
            if decision == "deny":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": f"Denied: {reason}",
                })
                continue
            if decision == "ask" and not ask_permission(
                tool_use.name, tool_use.input, reason
            ):
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "User denied this action.",
                })
                continue

            if console is not None:
                console.print(
                    f"  \\[sub] [cyan]→ {tool_use.name}[/cyan]"
                    f"{escape(f'({tool_use.input})')}"
                )
            hook_manager.fire(
                HookEvent.PRE_TOOL_USE,
                tool_name=tool_use.name,
                tool_input=tool_use.input,
                tool_use_id=tool_use.id,
            )
            output = registry.execute(tool_use.name, **tool_use.input)
            hook_manager.fire(
                HookEvent.POST_TOOL_USE,
                tool_name=tool_use.name,
                tool_input=tool_use.input,
                tool_use_id=tool_use.id,
                result=output,
            )
            if console is not None:
                preview = output if len(output) <= 500 else output[:500] + "..."
                console.print(f"  \\[sub] [dim]← {escape(preview)}[/dim]")
            results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})

    return f"Subagent stopped after {max_turns} turns without a final answer."
