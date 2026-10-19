"""Task permissions for disposable benchmark containers, not a shell sandbox."""
from __future__ import annotations

from pathlib import Path
import re
import shlex

from aki_code.security.permission import PermissionManager, PermissionMode


class BenchmarkPermissionManager(PermissionManager):
    """Reuse tool denial feedback while keeping host-owned records off limits."""

    _SCOPED_RULES = {rf"\b{name}\s+" for name in ("mkdir", "touch", "tee", "truncate", "rm")}
    _GIT_MUTATION = re.compile(
        r"\bgit\s+(?:-[Cc]\s+\S+\s+)*(?:add|commit|reset|clean|checkout|switch|restore|"
        r"update-index|read-tree|rm|mv|config|branch|tag)\b"
    )

    def __init__(self, *, workspace, scratch_dir, output_dir, readable_dirs=(), on_decision=None):
        self.workspace = Path(workspace).resolve()
        self.scratch_dir = Path(scratch_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.on_decision = on_decision
        super().__init__(allowed_dirs=[str(self.workspace), str(self.scratch_dir)],
                         base_dir=self.workspace, mode=PermissionMode.ACCEPT_EDITS,
                         readable_dirs=list(readable_dirs))

    def prompt_section(self):
        return (
            "\n\n# Isolated benchmark task\n"
            f"Modify source in {self.workspace}. Use {self.scratch_dir} for reproduction "
            "scripts, downloaded inspection material and test logs; TMPDIR points there. "
            "Do not change existing tests to make them pass. Do not look for reference "
            "patches, hidden grading tests or answers in other directories. "
            "The external runner collects the Git changes after execution stops. "
            "Do not generate, read or write the runner's patch.diff or result records. "
            "Use git diff to inspect your current changes. Do not stage, commit, "
            "reset or change repository metadata. A rejected tool call has not run; "
            "read its feedback and choose an allowed alternative. Repeated denials "
            "can end the run; never treat denial as permission to bypass the policy."
        )

    def _extract_bash_paths(self, command):
        paths = super()._extract_bash_paths(command)
        # Supplement the existing heuristic for common filesystem commands and
        # cd. This is deliberately not presented as complete shell parsing.
        for match in re.finditer(
                r"(?:^|[;&|\n])\s*(cd|mkdir|touch|tee|truncate|rm)\s+([^;&|\n]+)", command):
            try:
                targets = shlex.split(match.group(2), comments=True)
            except ValueError:
                raise ValueError("Use explicit paths and separate simple filesystem commands.")
            for target in targets:
                if target.startswith("-") or target in {"2", "1", "0"}:
                    continue
                paths.append(target)
        # Expand only the runner-supplied scratch variable for shell paths.
        return [path.replace("${TMPDIR}", str(self.scratch_dir)).replace("$TMPDIR", str(self.scratch_dir))
                for path in paths]

    def _decision(self, name, args):
        paths = self._extract_paths_from_args(name, args)
        if name == "bash":
            paths += self._extract_bash_paths(args.get("command", ""))
        for raw in paths:
            path = self._resolve_path(raw)
            if path.is_relative_to(self.workspace / ".git"):
                return "deny", "Repository metadata is runner-owned. Use git status or git diff."
            if path.is_relative_to(self.output_dir) and not (
                    name in {"read_file", "glob"} and self._is_readable_artifact(raw)):
                return "deny", ("Run records and the final patch are runner-owned. "
                                "The runner collects the patch after execution; use git diff now.")
        if name == "bash" and self._GIT_MUTATION.search(args.get("command", "")):
            return "deny", "Leave changes in the working tree; Git metadata is managed by the runner."
        if name == "bash" and re.search(r"\brm\s+", args.get("command", "")):
            if any(self._resolve_path(path) in {self.workspace, self.scratch_dir} for path in paths):
                return "deny", "Do not remove the task workspace or scratch directory itself."
        decision, reason = super().check(name, args)
        if decision == "ask":
            # A mkdir/touch/etc target has already passed both directory checks.
            if reason in {f"Command matches approval rule: {rule}" for rule in self._SCOPED_RULES}:
                return "allow", "Scoped filesystem operation in the benchmark workspace/scratch area."
            return "deny", reason
        return decision, reason

    def check(self, tool_name, args):
        try:
            decision, reason = self._decision(tool_name, args)
        except (OSError, ValueError) as exc:
            decision, reason = "deny", str(exc)
        if decision != "allow":
            reason += (f" Allowed task directories: {self.workspace} and {self.scratch_dir}. "
                       "This operation was not executed; choose an allowed alternative.")
        if self.on_decision is not None:
            self.on_decision(tool_name, args, decision, reason)
        return decision, reason
