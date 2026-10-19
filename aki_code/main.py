import signal
import threading
import queue
import time
import sys

from rich.console import Console
from rich.markup import escape
from aki_code import config
from aki_code.runtime import AgentRuntime
from aki_code.commands.base import CommandAction, CommandContext
from aki_code.hooks.manager import HookEvent

console = Console()
_NO_INPUT = object()


def _request_permission(tool_name: str, tool_input: dict, reason: str) -> bool:
    # ASCII marker: rich's legacy Windows console renders via the GBK codec,
    # which cannot encode symbols like U+26A0 and would raise mid-prompt.
    console.print(f"  [yellow]! {reason}[/yellow]")
    console.print(f"  [dim]{escape(f'{tool_name}({tool_input})')}[/dim]")
    answer = console.input("  [yellow]Allow? (y/n): [/yellow]").strip().lower()
    return answer.startswith("y")


def _read_input() -> str | None:
    """Read one line of input. Returns None on Ctrl+C / EOF (signals exit)."""
    try:
        return console.input("[bold blue]> [/bold blue]").strip()
    except (KeyboardInterrupt, EOFError):
        return None


def run() -> None:
    if not config.API_KEY:
        console.print("[bold red]Error:[/bold red] DEEPSEEK_API_KEY not set.")
        console.print("Copy .env.example to .env and fill in your API key.")
        raise SystemExit(1)

    runtime = AgentRuntime(console=console, ask_permission=_request_permission)
    permission_manager = runtime.permission_manager
    settings = runtime.settings
    hook_manager = runtime.hook_manager
    token_tracker = runtime.token_tracker
    todo_manager = runtime.todo_manager
    skill_loader = runtime.skill_loader
    memory_store = runtime.memory_store
    task_store = runtime.task_store
    background_manager = runtime.background_manager
    scheduler = runtime.scheduler
    team_manager = runtime.team_manager
    mcp_manager = runtime.mcp_manager
    goal = runtime.goal
    workflow_runtime = runtime.workflow_runtime
    command_registry = runtime.command_registry
    compactor = runtime.compactor
    messages = runtime.messages
    agent_lock = runtime.agent_lock
    stop_event = runtime.stop_event
    _append_user = runtime.append_user
    run_agent_turn = runtime.run_turn
    run_scheduled_turn = runtime.run_scheduled_turn
    run_team_turn = runtime.run_team_turn
    run_background_turn = runtime.run_background_turn

    def _terminate(signum, frame):
        stop_event.set()
        raise SystemExit(0)
    try:
        previous_sigterm = signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):
        previous_sigterm = None
    console.print("[bold green]Aki-Code[/bold green] — coding assistant")
    console.print("[dim]/help for commands, /exit to quit[/dim]\n")
    try:
        # Start the cron runtime only for the live CLI; importing this module must
        # not spawn threads.
        scheduler.start(agent_lock, run_scheduled_turn)

        # Terminal input runs on its own thread so the loop can also wake on team
        # events without the user pressing enter. A gate keeps exactly one prompt
        # outstanding, so the reader never reads ahead or stacks prompts.
        input_queue: "queue.Queue" = queue.Queue()
        want_input = threading.Event()

        def _input_reader() -> None:
            while not stop_event.is_set():
                want_input.wait()
                if stop_event.is_set():
                    break
                want_input.clear()
                input_queue.put(_read_input())  # None signals EOF / exit

        reader = threading.Thread(target=_input_reader, name="input-reader", daemon=True)
        reader.start()
        want_input.set()  # allow the first prompt

        POLL_SECONDS = 0.1
        while True:
            # 1) User input has priority over team events.
            try:
                user_input = input_queue.get_nowait()
            except queue.Empty:
                user_input = _NO_INPUT

            if user_input is not _NO_INPUT:
                if user_input is None:
                    console.print("\n[dim]Bye![/dim]")
                    break

                exit_requested = False
                with agent_lock:
                    # Slash commands are intercepted before anything touches the convo.
                    if user_input.startswith("/"):
                        ctx = CommandContext(
                            console=console,
                            messages=messages,
                            token_tracker=token_tracker,
                            commands=command_registry,
                            settings=settings,
                            permission_manager=permission_manager,
                            todo_manager=todo_manager,
                            skill_loader=skill_loader,
                            compactor=compactor,
                            memory_store=memory_store,
                            task_store=task_store,
                            mcp_manager=mcp_manager,
                            workflow_runtime=workflow_runtime,
                            goal_controller=goal,
                        )
                        command = command_registry.dispatch(user_input, ctx)
                        if command.action == CommandAction.EXIT:
                            exit_requested = True
                        elif command.action == CommandAction.RUN_AGENT:
                            hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=command.prompt)
                            _append_user(command.prompt)
                            run_agent_turn(command.prompt, interactive=True)
                    else:
                        retrying = bool(messages) and messages[-1]["role"] == "user"
                        if user_input or goal.active:
                            goal.resume_for_user()
                            prompt = user_input or "Continue working toward the active goal using the available results."
                            hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=user_input)
                            _append_user(prompt)
                            run_agent_turn(prompt, interactive=True)
                        elif retrying:
                            previous = messages[-1]["content"]
                            prompt = previous if isinstance(previous, str) else "Retry the previous request using the recorded results."
                            run_agent_turn(prompt, interactive=True)
                if exit_requested:
                    break

                # Re-arm the reader: show the retry hint (if any) then the prompt.
                if bool(messages) and messages[-1]["role"] == "user" and not goal.active:
                    console.print(
                        "[dim]Last request failed. Press Enter to retry, "
                        "or type a new message.[/dim]"
                    )
                want_input.set()
                continue

            # 2) No input pending: deliver team events, waking a Lead turn without
            #    the user having to type anything.
            if team_manager.has_lead_mail():
                with agent_lock:
                    ran = run_team_turn()
                if ran:
                    continue

            if background_manager.has_ready():
                with agent_lock:
                    ran = run_background_turn()
                if ran:
                    continue

            # 3) Idle: briefly wait, then re-check both sources.
            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    finally:
        stop_event.set()
        if "want_input" in locals():
            want_input.set()
        runtime.close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def main() -> None:
    if len(sys.argv) > 1:
        from aki_code.headless import main as headless_main
        raise SystemExit(headless_main(sys.argv[1:]))
    run()


if __name__ == "__main__":
    main()
