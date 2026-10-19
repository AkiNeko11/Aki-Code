"""Bounded per-instance processes; only the parent writes run-wide summaries."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import time
import traceback

from .common import TERMINAL, now, read_json, summarize, write_json


def _instance_worker(row, manifest, directory, attempt, api_key):
    # Isolate the worker and its official grading subprocesses from the
    # parent's terminal group. The parent owns interruption of all workers.
    if os.name == "posix":
        os.setsid()

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
    from .containers import docker_client
    from .runner import process_instance

    client = None
    try:
        client = docker_client()
        process_instance(client, row, manifest, Path(directory), api_key,
                         attempt=Path(attempt), update_summary=False)
    except Exception:
        (Path(attempt) / "worker-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if client is not None:
            client.close()


def _kill_tree(process):
    if os.name == "posix":
        # Workers create a dedicated session before starting any subprocess.
        # The group may outlive its leader if a worker crashed during grading.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.is_alive():
                process.kill()
    elif process.is_alive():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if process.is_alive():
            process.kill()


def _stop_workers(active):
    # Give workers one shared grace window to collect partial patches and
    # terminate graders. Then stop only their own process trees.
    for process, _, _ in active:
        if process.is_alive():
            if os.name == "posix":
                process.terminate()
            else:
                _kill_tree(process)
    deadline = time.monotonic() + 20
    while any(process.is_alive() for process, _, _ in active) and time.monotonic() < deadline:
        time.sleep(0.1)
    for process, _, _ in active:
        _kill_tree(process)
        process.join(timeout=5)


def _finish_worker(attempt, process, row, *, interrupted=False):
    path = attempt / "state.json"
    state = read_json(path)
    if state["status"] in TERMINAL:
        return
    state.update(status="interrupted" if interrupted else "error",
                 reason="Interrupted by user." if interrupted else
                 f"Instance worker exited with code {process.exitcode} before saving a final result; "
                 "see worker-error.log and the terminal output.")
    state["updated_at"] = now()
    try:
        patch_record = attempt / "patch.json"
        if patch_record.exists():
            state["patch_status"] = read_json(patch_record)["status"]
        if (attempt / "execution.json").exists():
            from .runner import agent_result
            state["agent"] = agent_result(attempt, row)
    except (OSError, ValueError, KeyError) as exc:
        state["artifact_error"] = f"Could not recover worker diagnostics: {exc}"
    if state.get("evaluation_status") == "running":
        state["evaluation_status"] = state["status"]
    write_json(path, state)


def _cleanup_crashed_worker(manifest, row, attempt):
    # Docker exec processes outlive the host worker. Reclaim this instance's
    # containers before replacing its slot, leaving other instances running.
    from .containers import cleanup_run_containers, docker_client
    client = None
    try:
        client = docker_client()
        cleanup_run_containers(client, manifest["run_id"], instance_id=row["instance_id"])
    except Exception as exc:
        state = read_json(attempt / "state.json")
        state["cleanup_warning"] = f"Crashed worker container cleanup failed: {type(exc).__name__}: {exc}"
        write_json(attempt / "state.json", state)
    finally:
        if client is not None:
            client.close()


def run_parallel(pending, manifest, directory, api_key):
    from .runner import next_attempt, say, set_state

    context = multiprocessing.get_context("spawn")
    queue = iter(pending)
    active = []
    stopped = False
    try:
        exhausted = False
        while active or not exhausted:
            dispatched = False
            while not exhausted and len(active) < manifest["workers"]:
                try:
                    index, row = next(queue)
                except StopIteration:
                    exhausted = True
                    break
                attempt = next_attempt(directory, row["instance_id"])
                set_state(attempt, {}, instance_id=row["instance_id"], started_at=now(),
                          status="running", phase="worker_start", patch_status="not_collected",
                          evaluation_status="not_run")
                process = context.Process(target=_instance_worker,
                    args=(row, manifest, str(directory), str(attempt), api_key),
                    name=f"swebench-{row['instance_id']}")
                say(f"[{index}/{len(manifest['instance_ids'])}] {row['instance_id']}: starting")
                entry = (process, attempt, row)
                active.append(entry)
                try:
                    process.start()
                except BaseException as exc:
                    if process.pid is None:
                        active.remove(entry)
                        process.close()
                    set_state(attempt, read_json(attempt / "state.json"),
                              status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                              reason=f"Could not start instance worker: {type(exc).__name__}: {exc}")
                    raise
                dispatched = True
            finished = [entry for entry in active if not entry[0].is_alive()]
            for process, attempt, row in finished:
                process.join()
                if process.exitcode:
                    _kill_tree(process)
                _finish_worker(attempt, process, row)
                if process.exitcode:
                    _cleanup_crashed_worker(manifest, row, attempt)
                active.remove((process, attempt, row))
                process.close()
            if finished or dispatched:
                summarize(directory)
            elif active:
                time.sleep(0.1)
    except KeyboardInterrupt:
        stopped = True
        raise
    finally:
        # A scheduler error also stops running tasks, leaving them resumable.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            _stop_workers(active)
            for process, attempt, row in active:
                _finish_worker(attempt, process, row, interrupted=stopped)
                process.close()
            summarize(directory)
        finally:
            signal.signal(signal.SIGINT, previous)
