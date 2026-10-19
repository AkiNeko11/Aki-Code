"""Run artifacts and Git patches without touching the user's real index."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading


def json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    default=json_default) + "\n", encoding="utf-8")
    temporary.replace(path)


class RunRecorder:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.RLock()
        self.runtime = None
        self.control = None

    def emit(self, event):
        with self._lock:
            event = {"time": datetime.now(timezone.utc).isoformat(), **event}
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, default=json_default) + "\n")
            self.checkpoint()

    def checkpoint(self):
        with self._lock:
            if self.control is not None:
                progress = self.control.snapshot()
                if self.runtime is not None:
                    progress["goal"] = self.runtime.goal.status()
                write_json(self.directory / "progress.json", progress)
            if self.runtime is not None:
                write_json(self.directory / "messages.json", self.runtime.messages)


def git(workspace: Path, *args, env=None, timeout=30):
    if env is None:
        env = {key: value for key, value in os.environ.items()
               if key not in {"GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}}
    result = subprocess.run(["git", "-C", str(workspace), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=timeout)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()
                         or f"git {args[0]} failed")
    return result.stdout


def validate_workspace(workspace: Path) -> str:
    if not workspace.is_dir():
        raise ValueError("Workspace must be an existing Git repository directory.")
    root = Path(os.fsdecode(git(workspace, "rev-parse", "--show-toplevel")).strip()).resolve()
    if root != workspace:
        raise ValueError("Workspace must be the Git repository root.")
    if git(workspace, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Workspace must be clean, including non-ignored untracked files.")
    if any(entry.startswith(b"160000 ")
           for entry in git(workspace, "ls-files", "--stage", "-z").split(b"\0")):
        raise ValueError("Submodule workspaces are not supported by this runner yet.")
    return git(workspace, "rev-parse", "HEAD").decode("ascii").strip()


def export_patch(workspace: Path, base_commit: str, directory: Path):
    index = directory / ".patch-index"
    # Never inherit an external index or user-supplied Git path overrides.
    env = {key: value for key, value in os.environ.items()
           if key not in {"GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}}
    env["GIT_INDEX_FILE"] = str(index)
    try:
        git(workspace, "read-tree", base_commit, env=env)
        git(workspace, "add", "--all", "--", ".", env=env)
        patch = git(workspace, "diff", "--cached", "--binary", "--full-index",
                    "--no-ext-diff", "--no-textconv", base_commit, "--", ".", env=env)
        (directory / "patch.diff").write_bytes(patch)
    finally:
        index.unlink(missing_ok=True)
        index.with_name(index.name + ".lock").unlink(missing_ok=True)
