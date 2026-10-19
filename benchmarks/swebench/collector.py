"""Trusted runner helper. Execute only inside its disposable inference container."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


def quiesce():
    # This process sweep is valid only in the runner's dedicated PID namespace.
    # Refuse accidental use as a local machine cleanup command.
    if not Path("/.dockerenv").exists() or not Path("/opt/akicode-runner/owner").is_file():
        raise RuntimeError("Collector requires an owned disposable Docker container.")
    keep = {1, os.getpid()}
    parent = os.getppid()
    while parent > 1 and parent not in keep:
        keep.add(parent)
        try:
            status = (Path("/proc") / str(parent) / "status").read_text()
            parent = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, StopIteration):
            break
    for _ in range(4):
        active = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) in keep:
                continue
            try:
                status = (entry / "status").read_text()
                state = next(line.split()[1] for line in status.splitlines() if line.startswith("State:"))
                if state != "Z":
                    active.append(int(entry.name))
            except (OSError, StopIteration):
                continue
        if not active:
            return
        for pid in active:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
    raise RuntimeError("Could not quiesce all Agent processes before patch collection.")


def collect(base_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise ValueError("Invalid benchmark base_commit.")
    quiesce()
    workspace = Path("/testbed")
    env = {key: value for key, value in os.environ.items()
           if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}}
    with tempfile.TemporaryDirectory(prefix="akicode-collector-", dir="/tmp") as temporary:
        env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        command = ["git", "-c", "safe.directory=/testbed", "-c", "core.fsmonitor=false",
                   "-c", "core.hooksPath=/dev/null", "-C", str(workspace)]

        def git(*args, output=subprocess.DEVNULL):
            subprocess.run([*command, *args], env=env, stdout=output, stderr=sys.stderr,
                           check=True, timeout=60)

        git("cat-file", "-e", base_commit + "^{commit}")
        git("read-tree", base_commit)
        git("add", "--all", "--", ".")
        git("diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
            "--no-textconv", base_commit, "--", ".", output=sys.stdout.buffer)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--base-commit")
    cli.add_argument("--quiesce-only", action="store_true")
    args = cli.parse_args()
    if args.quiesce_only:
        quiesce()
    elif args.base_commit:
        collect(args.base_commit)
    else:
        cli.error("--base-commit or --quiesce-only is required")


if __name__ == "__main__":
    main()
