from __future__ import annotations

# Tools a teammate must never see. Graph editing and team control belong to the
# Lead alone; recursion, compaction, the shared todo list, and cron would either
# corrupt a teammate's isolated context or race the Lead. Teammates keep the
# file/shell tools plus list_tasks / get_task / claim_task / complete_task, and
# (from Phase D) submit_plan.
#
# Names for tools that land in later phases (spawn_teammate, create_worktree,
# request_plan, approve_plan) are listed now so the deny-list is complete; they
# simply do not match anything until those tools exist.
TEAMMATE_EXCLUDED_TOOLS = frozenset({
    "task",             # no nested subagent delegation from a teammate
    "compact",          # compaction is the Lead conversation's concern
    "todo_write",       # the todo list is shared, single-conversation state
    "create_task",      # task-graph editing is Lead-only
    "update_task",      # task-graph editing is Lead-only
    "spawn_teammate",   # team control is Lead-only
    "create_worktree",  # team control is Lead-only
    "request_plan",     # Lead-side plan protocol
    "approve_plan",     # Lead-side plan protocol
    "schedule_cron",    # cron belongs to the Lead/runtime
    "cancel_cron",
    "list_crons",
})


def teammate_schemas(registry) -> list[dict]:
    """Tool schemas visible to a teammate (Lead-only tools filtered out)."""
    return [
        schema
        for schema in registry.get_schemas()
        if schema["name"] not in TEAMMATE_EXCLUDED_TOOLS
    ]
