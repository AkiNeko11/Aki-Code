"""Single-task CLI host, supervised in a separate process for a wall-clock limit."""
from __future__ import annotations

import argparse
import codecs
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
import io
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from rich.console import Console

from aki_code import config
from aki_code.artifacts import RunRecorder, export_patch, validate_workspace, write_json
from aki_code.execution import BudgetClient, RunControl, RunLimits, RunStopped
from aki_code.hooks.manager import HookEvent
from aki_code.llm.client import get_client
from aki_code.runtime import AgentRuntime
from aki_code.security.permission import PermissionMode


EXIT_CODES = {"completed": 0, "achieved": 0, "failed": 1, "error": 1,
              "goal_error": 1, "blocked": 2, "limit": 3, "timeout": 124,
              "interrupted": 130}


def _positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def parser():
    cli = argparse.ArgumentParser(prog="python -m aki_code.main")
    commands = cli.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Execute one task without terminal input")
    task = run.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Literal task text (slash commands are not parsed)")
    task.add_argument("--task-file", type=Path, help="UTF-8 task text file")
    run.add_argument("--workspace", type=Path, required=True, help="Clean Git repository root")
    run.add_argument("--output-dir", type=Path, required=True, help="New directory outside workspace")
    run.add_argument("--permission-mode", choices=[mode.value for mode in PermissionMode], default="default")
    run.add_argument("--goal", help="Optional verifiable completion condition")
    run.add_argument("--execution-profile", choices=["coding", "benchmark"], default="coding",
                     help="Benchmark permissions are only for disposable Linux containers")
    run.add_argument("--scratch-dir", type=Path, help="Dedicated writable temporary directory for benchmark mode")
    run.add_argument("--patch-export", choices=["local", "runner"], default="local",
                     help="Let an external runner collect the final patch instead of exporting locally")
    run.add_argument("--max-turns", type=_positive_int, default=30)
    run.add_argument("--max-calls", type=_positive_int, default=60)
    run.add_argument("--max-tokens", type=_positive_int, default=100000)
    run.add_argument("--timeout", type=_positive_float, default=600,
                     help="Worker wall-clock seconds, including model, tools and background waits")
    return cli


def execute_task(request, recorder, *, client=None):
    """Execute in the current worker; the parent owns the absolute deadline."""
    control = RunControl(RunLimits(**request["limits"]), recorder.emit)
    recorder.control = control
    runtime = None
    result = {"status": "error", "reason": "Runtime did not start.", "text": ""}
    try:
        if client is None and not config.API_KEY:
            raise ValueError("DEEPSEEK_API_KEY is not set.")
        client = BudgetClient(client if client is not None else get_client(), control)
        runtime = AgentRuntime(
            workspace=request["workspace"], state_dir=recorder.directory / "state",
            profile="coding", client=client,
            console=Console(file=sys.stderr, force_terminal=False, color_system=None),
            ask_permission=control.require_permission, event_sink=recorder.emit,
            permission_mode=PermissionMode(request["permission_mode"]),
            benchmark_scratch=request.get("scratch_dir"),
            permission_observer=control.observe_benchmark_permission,
        )
        recorder.runtime = runtime
        runtime.lead_hooks.register(HookEvent.PRE_LLM_CALL, control.before_turn)
        # These also apply to subagents through the base-hook fork. Their model
        # calls share the global request budget; only lead calls count as turns.
        for hooks in (runtime.hook_manager, runtime.lead_hooks):
            hooks.register(HookEvent.PRE_TOOL_USE, lambda ctx: control.check(), prepend=True)
        # Check before the Goal evaluator starts an additional model request.
        # A successful final judge may itself exceed the token soft threshold;
        # that in-flight overshoot must not undo an already achieved goal.
        runtime.lead_hooks.register(HookEvent.BEFORE_STOP, lambda ctx: control.check(), prepend=True)
        if request.get("goal"):
            runtime.goal.set(request["goal"])
        runtime.append_user(request["task"])
        recorder.emit({"type": "run_started", "workspace": request["workspace"]})
        while True:
            turn = runtime.run_turn(request["task"], interactive=False)
            if turn.status != "defer":
                result.update(status=turn.status, reason=turn.reason, text=turn.text)
                break
            recorder.emit({"type": "waiting_background"})
            while not runtime.background_manager.has_ready():
                if not runtime.background_manager.has_pending(runtime.goal.current_id):
                    raise RuntimeError("Turn deferred without pending background work.")
                time.sleep(0.05)
            # Feed notifications into this very conversation. Calling the UI's
            # background runner would hide its TurnResult and reset host limits.
            records = runtime.background_manager.collect_records()
            for record in records:
                runtime.append_user(record["notification"])
                recorder.emit({"type": "background_result", "record": record})
    except RunStopped as exc:
        result.update(status=exc.status, reason=exc.reason)
        if runtime is not None:
            runtime.lead_hooks.fire(HookEvent.TURN_STOPPED, status=exc.status, reason=exc.reason)
    except KeyboardInterrupt:
        result.update(status="interrupted", reason="Interrupted by user or supervisor.")
    except Exception as exc:
        result.update(status="error", reason=f"{type(exc).__name__}: {exc}")
    finally:
        if runtime is not None:
            runtime.close()
        recorder.checkpoint()
    if runtime is not None:
        result["goal"] = runtime.goal.status()
    result.update(control.snapshot())
    recorder.emit({"type": "run_stopped", "status": result["status"], "reason": result["reason"]})
    return result


def _worker(request_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    recorder = RunRecorder(Path(request["output_dir"]))
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)
    # Built-in hooks/tools may use print; no worker output reaches parent stdout.
    with redirect_stdout(sys.stderr):
        result = execute_task(request, recorder)
        write_json(recorder.directory / "worker-result.json", result)
    return 0


def _background_pids(directory):
    try:
        lines = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    pids = set()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # A hard stop may leave the very last line incomplete.
        if event.get("type") in {"background_process", "shell_process"}:
            pids.add(event["pid"])
        elif event.get("type") in {"background_process_finished", "shell_process_finished"}:
            pids.discard(event["pid"])
    return pids


def _terminate_worker(process, directory):
    # Background commands have their own groups. Kill them before the worker so
    # Windows can still walk the complete descendant tree.
    for pid in _background_pids(directory):
        try:
            if os.name == "posix":
                os.killpg(pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=10)
            if process.poll() is None:
                process.kill()
    except (OSError, subprocess.SubprocessError):
        if process.poll() is None:
            process.kill()
    process.wait(timeout=10)


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def supervise(request):
    directory = Path(request["output_dir"])
    started = time.monotonic()
    result = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "workspace": request["workspace"], "base_commit": request["base_commit"],
              "output_dir": str(directory), "model": config.MODEL,
              "max_output_tokens": config.MAX_TOKENS,
              "goal_evaluator_model": config.GOAL_EVALUATOR_MODEL if request.get("goal") else None,
              "max_stop_blocks": config.MAX_STOP_BLOCKS,
              "permission_mode": request["permission_mode"], "profile": "coding",
              "memory_enabled": False, "timeout_seconds": request["timeout"],
              "limits": request["limits"], "goal_condition": request.get("goal"),
              "status": "error", "reason": "Worker did not start."}
    result.update(execution_profile=request.get("execution_profile", "coding"),
                  patch_export=request.get("patch_export", "local"))
    process = None
    log_reader = None
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), True)
    def drain_log(*, final=False):
        chunk = log_reader.read() if log_reader is not None else b""
        decoded = decoder.decode(chunk, final=final)
        if decoded:
            sys.stderr.write(decoded)
            sys.stderr.flush()
    forced_status = None
    try:
        package_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        env["PYTHONPATH"] = str(package_root) + os.pathsep + env.get("PYTHONPATH", "")
        group = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        with (directory / "run.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "aki_code.headless", "--worker", str(directory / "request.json")],
                cwd=package_root, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, **group,
            )
        log_reader = (directory / "run.log").open("rb")
        while process.poll() is None:
            drain_log()
            remaining = request["timeout"] - (time.monotonic() - started)
            if remaining <= 0:
                forced_status = "timeout"
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        forced_status = "interrupted"
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None:
            if forced_status and process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM if os.name == "posix" else signal.CTRL_BREAK_EVENT)
                    process.wait(timeout=1)
                except (OSError, subprocess.SubprocessError):
                    pass
            _terminate_worker(process, directory)
        drain_log(final=True)
        if log_reader is not None:
            log_reader.close()
    result.update(_read_json(directory / "progress.json", {}))
    result.update(_read_json(directory / "worker-result.json", {}))
    if forced_status:
        result.update(status=forced_status, reason=(
            f"Task exceeded {request['timeout']} seconds." if forced_status == "timeout"
            else "Interrupted by user."))
        if result.get("goal", {}).get("active"):
            result["goal"].update(automatic=False, status=forced_status, reason=result["reason"])
    elif process is not None and process.returncode:
        result.update(status="error", reason=f"Worker exited with code {process.returncode}; see run.log.")
    result["execution_seconds"] = round(time.monotonic() - started, 3)
    result["patch_exported"] = False
    if request.get("patch_export", "local") == "local":
        try:
            export_patch(Path(request["workspace"]), request["base_commit"], directory)
            result["patch_exported"] = True
        except Exception as exc:
            result["patch_error"] = str(exc)
            if result["status"] in {"completed", "achieved"}:
                result.update(status="error", reason="Task ended but patch export failed.")
    result["exit_code"] = EXIT_CODES.get(result["status"], 1)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(directory / "result.json", result)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    directory = None
    previous_sigterm = None
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    try:
        previous_sigterm = signal.signal(signal.SIGTERM, interrupted)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        task = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8-sig")
        if not task.strip():
            raise ValueError("Task must not be empty.")
        if args.goal is not None and not args.goal.strip():
            raise ValueError("Goal must not be empty.")
        workspace = args.workspace.expanduser().resolve()
        output = args.output_dir.expanduser().resolve()
        if output.is_relative_to(workspace):
            raise ValueError("Output directory must be outside the workspace.")
        scratch = args.scratch_dir.expanduser().resolve() if args.scratch_dir else None
        if args.execution_profile == "benchmark":
            if os.name != "posix" or scratch is None or not scratch.is_dir():
                raise ValueError("Benchmark mode needs POSIX and an existing dedicated --scratch-dir.")
            if args.permission_mode != "accept-edits" or args.patch_export != "runner":
                raise ValueError("Benchmark mode requires --permission-mode accept-edits --patch-export runner.")
            if (scratch.is_relative_to(workspace) or workspace.is_relative_to(scratch)
                    or scratch.is_relative_to(output) or output.is_relative_to(scratch)):
                raise ValueError("Benchmark scratch, workspace and output directories must be separate.")
            os.environ["TMPDIR"] = str(scratch)
        elif scratch is not None:
            raise ValueError("--scratch-dir requires --execution-profile benchmark.")
        base = validate_workspace(workspace)
        output.mkdir(parents=True, exist_ok=False)
        directory = output
        for name in ("events.jsonl", "run.log") + (("patch.diff",) if args.patch_export == "local" else ()):
            (output / name).touch()
        write_json(output / "messages.json", [])
        request = {"task": task, "workspace": str(workspace), "output_dir": str(output),
                   "base_commit": base, "permission_mode": args.permission_mode,
                   "goal": args.goal, "timeout": args.timeout,
                   "execution_profile": args.execution_profile, "patch_export": args.patch_export,
                   "scratch_dir": str(scratch) if scratch else None,
                   "limits": {"max_turns": args.max_turns, "max_calls": args.max_calls,
                              "max_tokens": args.max_tokens}}
        write_json(output / "request.json", request)
        result = supervise(request)
    except KeyboardInterrupt:
        result = {"schema_version": 1, "status": "interrupted", "reason": "Interrupted by user.", "exit_code": 130}
    except Exception as exc:
        result = {"schema_version": 1, "status": "error", "reason": f"{type(exc).__name__}: {exc}", "exit_code": 1}
        if directory is not None:
            write_json(directory / "result.json", result)
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    print(json.dumps(result, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker(sys.argv[2]))
    raise SystemExit(main())
