"""Saved, trusted workflows: synchronous tool boundary, bounded async steps."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import copy
import hashlib
import inspect
import json
import threading
from typing import Any

from aki_code.workflow.budget import RunBudget
from aki_code.workflow.model import (
    RunLimits, WorkflowBudgetExceeded, WorkflowCancelled, WorkflowError,
    WorkflowInputChanged, WorkflowInputError,
)
from aki_code.workflow.schema import check_schema, parse_output, validate_value
from aki_code.workflow.workspace import capture_workspace


_SCOPE: ContextVar[tuple[str, ...]] = ContextVar("workflow_step_scope", default=())


def _json(value) -> str:
    try:
        validate_value(value, True)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise WorkflowInputError(f"Expected JSON-serializable data: {exc}") from exc


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sum_usage(old: dict, current: dict) -> dict:
    return {key: old.get(key, 0) + current.get(key, 0)
            for key in old.keys() | current.keys()
            if isinstance(old.get(key, 0), (int, float))
            and isinstance(current.get(key, 0), (int, float))}


class _StepFailure(WorkflowError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status if status in {"failed", "blocked", "cancelled", "limit"} else "failed"


class _Execution:
    def __init__(self, runtime, journal, budget, cancel_event, previous_usage, runner):
        self.runtime = runtime
        self.runner = runner
        self.journal = journal
        self.budget = budget
        self.cancel_event = cancel_event
        self.previous_usage = previous_usage
        self.semaphore = asyncio.Semaphore(runtime.limits.max_concurrency)
        self.claimed: set[str] = set()
        self.steps: dict[str, dict] = {}
        self.workers: set[asyncio.Task] = set()
        self.cached = 0

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise WorkflowCancelled("Workflow cancelled; no new steps will be started")

    def event(self, kind, **data):
        self.runtime._emit({"type": kind, "run_id": self.journal.run_id, **data})

    def checkpoint(self):
        usage = self.budget.snapshot()
        self.journal.save({"status": "running", "usage": usage,
                           "total_usage": _sum_usage(self.previous_usage, usage),
                           "steps": copy.deepcopy(self.steps), "cache_hits": self.cached})

    async def worker(self, fn, *args, **kwargs):
        """Shield threads from asyncio cancellation, then drain before unlocking."""
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        self.workers.add(task)
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    self.cancel_event.set()
                    cancelled = True
                except Exception:
                    break
            if cancelled:
                # Retrieve any worker exception to avoid an unobserved-task warning.
                if not task.cancelled():
                    task.exception()
                raise asyncio.CancelledError
            return task.result()
        finally:
            if task.done():
                self.workers.discard(task)

    async def drain(self):
        if self.workers:
            await self.wait_cleanup(asyncio.gather(*self.workers, return_exceptions=True))
            self.workers.clear()

    async def wait_cleanup(self, future):
        # Repeated Task.cancel() must not cancel the task wrapping a live thread.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                self.cancel_event.set()
        return future.result()


class ExecutionState:
    """Capabilities passed to host-registered Python scripts, not a Python sandbox."""

    def __init__(self, execution: _Execution, definition, *, path=(), depth=0):
        self._execution = execution
        self._definition = definition
        self._path = path or (definition.name,)
        self._depth = depth

    def phase(self, title: str):
        self._execution.check_cancel()
        if not isinstance(title, str) or not title.strip():
            raise WorkflowInputError("Phase title must be a nonempty string")
        if self._definition.phases and title not in self._definition.phases:
            raise WorkflowInputError(f"Undeclared workflow phase: {title}")
        self._execution.event("task_progress", progress_type="phase", phase=title)

    def log(self, message: str):
        self._execution.check_cancel()
        if not isinstance(message, str):
            raise WorkflowInputError("Workflow log message must be a string")
        self._execution.event("task_progress", progress_type="log", message=message)

    async def agent(self, prompt: str, *, schema=None, label: str = "", phase: str | None = None):
        execution = self._execution
        execution.check_cancel()
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowInputError("Agent prompt must be a nonempty string")
        if not isinstance(label, str) or len(label) > 200:
            raise WorkflowInputError("Agent label must be a string of at most 200 characters")
        if schema is not None:
            check_schema(schema)
        if phase is not None:
            self.phase(phase)
        fingerprint = _hash({"prompt": prompt, "schema": schema})
        label = label or "agent:" + fingerprint
        step_id = _hash({"scope": list(self._path + _SCOPE.get()), "label": label})
        if step_id in execution.claimed:
            raise WorkflowInputError(f"Duplicate step label '{label}'; give independent calls unique labels")
        execution.claimed.add(step_id)
        hit, cached = execution.journal.cached(step_id, fingerprint)
        if hit:
            if schema is not None:
                validate_value(cached, schema)
            execution.cached += 1
            execution.steps[step_id] = {"label": label, "status": "cached"}
            execution.event("task_progress", progress_type="agent_cached", label=label)
            execution.checkpoint()
            return copy.deepcopy(cached)

        async with execution.semaphore:
            execution.check_cancel()
            execution.steps[step_id] = {"label": label, "status": "running"}
            execution.checkpoint()
            execution.event("task_progress", progress_type="agent_started", label=label)
            try:
                result = await execution.worker(
                    execution.runner.run, prompt, schema=schema, label=label,
                    budget=execution.budget, cancel_event=execution.cancel_event,
                )
                self._check_result(result)
                if schema is None:
                    value = result.value if result.value is not None else result.text
                else:
                    try:
                        value = self._parse_result(result, schema)
                    except WorkflowInputError as error:
                        execution.check_cancel()
                        execution.event("task_progress", progress_type="format_repair", label=label)
                        repaired = await execution.worker(
                            execution.runner.repair, result.text, schema, str(error),
                            label=label, budget=execution.budget, cancel_event=execution.cancel_event,
                        )
                        self._check_result(repaired)
                        value = self._parse_result(repaired, schema)
                # Disk JSON is canonicalized too; initial and cached values must
                # produce identical prompts when downstream scripts serialize them.
                value = json.loads(_json(value))
                execution.journal.record(step_id, fingerprint, value)
                execution.steps[step_id]["status"] = "completed"
                execution.event("task_progress", progress_type="agent_completed", label=label)
                execution.checkpoint()
                return copy.deepcopy(value)
            except BaseException as exc:
                status = ("cancelled" if isinstance(exc, (asyncio.CancelledError, WorkflowCancelled))
                          else "limit" if isinstance(exc, WorkflowBudgetExceeded)
                          else getattr(exc, "status", "failed"))
                execution.steps[step_id]["status"] = status
                execution.event("task_progress", progress_type="agent_failed", label=label,
                                status=status, error=str(exc))
                execution.checkpoint()
                raise

    @staticmethod
    def _parse_result(result, schema):
        if result.value is not None:
            validate_value(result.value, schema)
            return result.value
        return parse_output(result.text, schema)

    @staticmethod
    def _check_result(result):
        if getattr(result, "input_changed", False):
            raise WorkflowInputChanged(result.error or "Workflow input changed during review")
        if result.status != "completed":
            raise _StepFailure(result.status, result.error or result.text or "Agent step failed")

    async def _gather(self, coroutines):
        tasks = [asyncio.create_task(coro) for coro in coroutines]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            self._execution.cancel_event.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await self._execution.wait_cleanup(asyncio.gather(*tasks, return_exceptions=True))
            raise

    async def parallel(self, thunks, *, label="parallel"):
        self._execution.check_cancel()
        thunks = list(thunks)
        if not isinstance(label, str) or not label:
            raise WorkflowInputError("Parallel label must be a nonempty string")
        if not all(callable(thunk) for thunk in thunks):
            raise WorkflowInputError("parallel expects callables, not already running tasks")
        parent_scope = _SCOPE.get()

        async def invoke(thunk, index):
            token = _SCOPE.set(parent_scope + (f"parallel:{label}", str(index)))
            try:
                self._execution.check_cancel()
                value = thunk()
                return await value if inspect.isawaitable(value) else value
            finally:
                _SCOPE.reset(token)

        return await self._gather(invoke(thunk, i) for i, thunk in enumerate(thunks))

    async def pipeline(self, items, *stages, label="pipeline"):
        self._execution.check_cancel()
        items = list(items)
        if not stages or not all(callable(stage) for stage in stages):
            raise WorkflowInputError("pipeline needs at least one callable stage")
        if not isinstance(label, str) or not label:
            raise WorkflowInputError("Pipeline label must be a nonempty string")
        parent_scope = _SCOPE.get()

        async def invoke(item, index):
            value = item
            for stage_index, stage in enumerate(stages):
                token = _SCOPE.set(parent_scope + (f"pipeline:{label}", str(index), str(stage_index)))
                try:
                    self._execution.check_cancel()
                    result = stage(value, item, index)
                    value = await result if inspect.isawaitable(result) else result
                finally:
                    _SCOPE.reset(token)
            return value

        return await self._gather(invoke(item, i) for i, item in enumerate(items))

    async def workflow(self, name: str, args=None):
        self._execution.check_cancel()
        if self._depth >= 1:
            raise WorkflowInputError("Only one level of nested workflows is supported")
        definition, arguments = self._execution.runtime._prepare(name, args)
        child = ExecutionState(self._execution, definition,
                               path=self._path + (definition.name,), depth=self._depth + 1)
        return await definition.script(child, arguments)


class WorkflowRuntime:
    def __init__(self, registry, runner, store, limits=None, event_sink=None):
        self.registry = registry
        self.runner = runner
        self.store = store
        self.limits = limits or RunLimits()
        self.event_sink = event_sink
        self._active: set[threading.Event] = set()
        self._active_lock = threading.Lock()

    def _emit(self, event):
        if self.event_sink is not None:
            try:
                self.event_sink(copy.deepcopy(event))
            except Exception:
                # Display failures cannot invalidate completed tool work.
                pass

    def cancel(self):
        with self._active_lock:
            for event in self._active:
                event.set()

    def _prepare(self, name, args):
        definition = self.registry.get(name)
        if not definition.read_only:
            raise WorkflowInputError("This runtime currently supports read-only workflows")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise WorkflowInputError("Workflow args must be an object")
        args = json.loads(_json(args))
        if definition.args_schema is not None:
            validate_value(args, definition.args_schema)
        return definition, args

    def run(self, name, args=None, resume_from_run_id=None) -> dict:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise WorkflowInputError("Use await runtime.arun() inside an existing event loop")
        return asyncio.run(self.arun(name, args, resume_from_run_id))

    async def arun(self, name, args=None, resume_from_run_id=None) -> dict:
        definition, arguments = self._prepare(name, args)
        if resume_from_run_id is not None:
            saved = self.store.get_run(resume_from_run_id)
            if saved.get("resumable") is False:
                raise WorkflowInputError("This run observed changed inputs and cannot be resumed; start a new run")
        runner_identity = self.runner.fingerprint()
        workspace_snapshot = None
        if runner_identity.get("allow_reads"):
            workspace_snapshot = capture_workspace(
                runner_identity["workspace"], runtime_dir=getattr(self.store, "root", None))
        runner = self.runner
        if workspace_snapshot is not None and hasattr(runner, "for_snapshot"):
            runner = runner.for_snapshot(workspace_snapshot)
        definitions = [{**meta, "args_schema": self.registry.get(meta["name"]).args_schema}
                       for meta in self.registry.list()]
        identity = {"workflow": {**definition.metadata(), "args_schema": definition.args_schema}, "args": arguments,
                    "runner": runner_identity, "workspace_snapshot": workspace_snapshot,
                    "registry": definitions}
        # Store validates the identity and old journal before changing any files.
        with self.store.open_run(identity, resume_from_run_id) as journal:
            if journal.snapshot.get("resumable") is False:
                raise WorkflowInputError("This run observed changed inputs and cannot be resumed; start a new run")
            cancel_event = threading.Event()
            budget = RunBudget(self.limits, cancel_event)
            previous_usage = copy.deepcopy(journal.snapshot.get("total_usage", {}))
            execution = _Execution(self, journal, budget, cancel_event, previous_usage, runner)
            with self._active_lock:
                self._active.add(cancel_event)
            result, error, status, propagate_cancel = None, "", "running", False
            resumable = True
            try:
                execution.checkpoint()
                execution.event("task_started", name=name, resumed=resume_from_run_id is not None)
                ctx = ExecutionState(execution, definition)
                result = await definition.script(ctx, copy.deepcopy(arguments))
                execution.check_cancel()
                _json(result)
                status = "completed"
            except asyncio.CancelledError:
                cancel_event.set()
                status, error, propagate_cancel = "cancelled", "Workflow interrupted", True
            except (WorkflowCancelled, KeyboardInterrupt):
                cancel_event.set()
                status, error = "cancelled", "Workflow cancelled"
            except WorkflowBudgetExceeded as exc:
                cancel_event.set()
                status, error = "limit", str(exc)
            except WorkflowInputChanged as exc:
                cancel_event.set()
                status, error, resumable = "failed", str(exc), False
            except Exception as exc:
                cancel_event.set()
                status, error = getattr(exc, "status", "failed"), str(exc)
            finally:
                try:
                    await execution.drain()
                    # Even failed/cancelled attempts may have cached earlier
                    # steps. Once changed inputs were observed, those results
                    # must not become reusable merely by restoring old files.
                    if workspace_snapshot is not None:
                        try:
                            current = capture_workspace(runner_identity["workspace"],
                                                        runtime_dir=getattr(self.store, "root", None))
                            unchanged = current == workspace_snapshot
                        except Exception:
                            unchanged = False
                        if not unchanged:
                            resumable = False
                            if status == "completed":
                                status = "failed"
                                error = "Workspace changed during review; start a new run with fresh inputs"
                    if status == "completed":
                        try:
                            journal.write_output(result)
                        except Exception as exc:
                            status, error = "failed", str(exc)
                    usage = budget.snapshot()
                    total_usage = _sum_usage(previous_usage, usage)
                    task = {"run_id": journal.run_id, "name": name, "status": status,
                            "steps": copy.deepcopy(execution.steps), "cache_hits": execution.cached,
                            "resumable": resumable}
                    journal.save({"status": status, "error": error, "usage": usage,
                                  "total_usage": total_usage, "steps": task["steps"],
                                  "cache_hits": execution.cached, "resumable": resumable})
                    artifacts = {"snapshot": str(journal.snapshot_path),
                                 "journal": str(journal.journal_path)}
                    if status == "completed":
                        artifacts["output"] = str(journal.output_path)
                    execution.event("task_notification", status=status, error=error,
                                    usage=usage, cache_hits=execution.cached, artifacts=artifacts)
                finally:
                    with self._active_lock:
                        self._active.discard(cancel_event)
            if propagate_cancel:
                raise asyncio.CancelledError
            return {"run_id": journal.run_id, "status": status,
                    "result": result if status == "completed" else None,
                    "error": error, "usage": usage, "total_usage": total_usage,
                    "artifacts": artifacts, "task": task, "resumable": resumable}
