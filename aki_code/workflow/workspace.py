"""Fingerprint read-only inputs without changing the repository or reading secrets."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

from aki_code.workflow.model import WorkflowInputError


_EXCLUDED = {".git", ".venv", "venv", "__pycache__", ".workflows", ".tasks",
             ".memory", ".mailboxes", ".worktrees", ".task_outputs", ".transcripts",
             ".pytest_cache", ".mypy_cache", "node_modules"}


def is_input_path(relative) -> bool:
    relative = Path(relative)
    return (not relative.is_absolute() and ".." not in relative.parts
            and not any(p.casefold() in _EXCLUDED for p in relative.parts)
            and not any(p.casefold().startswith(".env") for p in relative.parts)
            and relative.name.casefold() != ".scheduled_tasks.json")


def capture_workspace(workspace, *, runtime_dir=None) -> dict:
    """A conservative content snapshot; generated workflow artifacts are excluded.

    Git supplies tracked and non-ignored paths (including deleted tracked files).
    A plain directory falls back to a source-tree walk. Symlinks are recorded as
    links, never followed outside the workspace. This is an invalidation check,
    not filesystem isolation: tools must still enforce their own path policy.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise WorkflowInputError(f"Workspace does not exist: {root}")
    excluded_root = Path(runtime_dir).resolve() if runtime_dir is not None else None
    head = None
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=15, check=False,
        )
        if listing.returncode == 0:
            names = {os.fsdecode(p) for p in listing.stdout.split(b"\0") if p}
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                      capture_output=True, timeout=15, check=False)
            if revision.returncode == 0:
                head = revision.stdout.decode("ascii").strip()
        else:
            names = None
    except (OSError, subprocess.TimeoutExpired):
        names = None
    if names is None:
        names = set()
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in _EXCLUDED
                       and not (Path(parent) / d).is_symlink()
                       and not getattr(Path(parent) / d, "is_junction", lambda: False)()]
            for name in files:
                names.add((Path(parent) / name).relative_to(root).as_posix())

    digest = hashlib.sha256()
    count = 0
    paths = []
    hashes = {}
    for name in sorted(names):
        relative = Path(name)
        if not is_input_path(relative):
            continue
        path = root / relative
        if excluded_root is not None and (path == excluded_root or excluded_root in path.parents):
            continue
        try:
            if path.is_symlink():
                content_hash = "link:" + os.readlink(path)
            elif not path.exists():
                content_hash = "missing"
            elif path.is_file():
                # A leaf can be regular while an ancestor is a junction/symlink.
                # Check resolution before reading, not just the leaf's type.
                resolved = path.resolve()
                if not resolved.is_relative_to(root) or not is_input_path(resolved.relative_to(root)):
                    raise WorkflowInputError(f"Workspace input resolves outside review inputs: {relative}")
                file_hash = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        file_hash.update(chunk)
                content_hash = file_hash.hexdigest()
                hashes[relative.as_posix()] = content_hash
            else:
                continue
        except OSError as exc:
            raise WorkflowInputError(f"Cannot snapshot workspace path {relative}: {exc}") from exc
        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(content_hash.encode("utf-8", "surrogateescape"))
        digest.update(b"\n")
        count += 1
        if content_hash != "missing":
            paths.append(relative.as_posix())
    return {"root": str(root), "head": head, "files": count, "sha256": digest.hexdigest(),
            "paths": paths, "hashes": hashes}
