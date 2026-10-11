from __future__ import annotations

import time

from aki_code.hooks.manager import HookContext, HookEvent, HookManager


def logging_hook(ctx: HookContext) -> None:
    event = ctx.event.value
    data = ctx.data

    if ctx.event == HookEvent.PRE_TOOL_USE:
        print(f"  [hook] executing {data.get('tool_name')}...")
    elif ctx.event == HookEvent.POST_TOOL_USE:
        print(f"  [hook] {data.get('tool_name')} done")
    elif ctx.event == HookEvent.ON_ERROR:
        print(f"  [hook] error: {data.get('error')}")


def timing_hook_factory():
    """Returns a pair of pre/post hooks that measure tool execution time."""
    start_times: dict[str, float] = {}

    def pre_hook(ctx: HookContext) -> None:
        tool_id = ctx.data.get("tool_use_id", "")
        start_times[tool_id] = time.time()

    def post_hook(ctx: HookContext) -> None:
        tool_id = ctx.data.get("tool_use_id", "")
        start = start_times.pop(tool_id, None)
        if start is not None:
            elapsed = time.time() - start
            print(f"  [hook] {ctx.data.get('tool_name')} took {elapsed:.2f}s")

    return pre_hook, post_hook


def register_builtin_hooks(manager: HookManager) -> None:
    manager.register(HookEvent.PRE_TOOL_USE, logging_hook)
    manager.register(HookEvent.POST_TOOL_USE, logging_hook)
    manager.register(HookEvent.ON_ERROR, logging_hook)

    pre, post = timing_hook_factory()
    manager.register(HookEvent.PRE_TOOL_USE, pre)
    manager.register(HookEvent.POST_TOOL_USE, post)
