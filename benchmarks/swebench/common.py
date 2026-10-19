"""Run snapshots and result aggregation; importing this needs only Python."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
HARNESS_VERSION = "4.1.0"
DEFAULT_INSTANCE = "sympy__sympy-20590"
TERMINAL = {"resolved", "unresolved", "empty_patch", "validated", "error", "interrupted"}

# Freeze all model/context settings used by the coding profile. Secrets are
# supplied only to docker exec at execution time, never stored in the manifest.
ENV_DEFAULTS = {
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/anthropic",
    "MODEL_NAME": "deepseek-flash", "MAX_TOKENS": "4096",
    "CONTEXT_CHAR_LIMIT": "50000", "TOOL_RESULT_BUDGET_CHARS": "200000",
    "LARGE_RESULT_CHAR_LIMIT": "30000", "LARGE_RESULT_PREVIEW_CHARS": "2000",
    "MAX_MESSAGES": "50", "SNIP_HEAD_MESSAGES": "3", "KEEP_RECENT_MESSAGES": "5",
    "COMPACT_TARGET_RATIO": "0.8", "KEEP_RECENT_RESULTS": "3",
    "MICRO_COMPACT_MIN_CHARS": "120", "FIT_RESULT_PREVIEW_CHARS": "1000",
    "MAX_REACTIVE_RETRIES": "1", "SUMMARY_MAX_TOKENS": "1024",
    "BACKGROUND_JOB_TIMEOUT": "600", "BACKGROUND_MAX_OUTPUT_CHARS": "200000",
    "MAX_STOP_BLOCKS": "10", "GOAL_EVALUATOR_MAX_CHARS": "24000",
    "GOAL_EVALUATOR_TIMEOUT": "60",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bytes_digest(data):
    return hashlib.sha256(data).hexdigest()


def source_archive():
    """Allowlist runtime inputs; .env, benchmark answers and host state stay out."""
    files = sorted((ROOT / "aki_code").rglob("*.py"))
    files += sorted(path for path in (ROOT / "skills").rglob("*") if path.is_file())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in files:
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError(f"Source snapshot does not accept symlinks: {path}")
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(ROOT).as_posix())
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def git_metadata():
    def git(*args):
        return subprocess.check_output(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *args],
                                       stderr=subprocess.DEVNULL).decode("utf-8").strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def driver_digest():
    files = sorted(Path(__file__).parent.glob("*.py"))
    files += sorted(Path(__file__).parent.glob("*requirements.txt"))
    return bytes_digest(b"".join(path.name.encode() + b"\0" + path.read_bytes() for path in files))


def check_row(row):
    for key in ("instance_id", "repo", "base_commit", "version", "problem_statement",
                "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
        if key not in row:
            raise ValueError(f"Dataset row is missing {key}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", row["instance_id"]):
        raise ValueError("Invalid instance_id")
    if not re.fullmatch(r"[0-9a-f]{40}", row["base_commit"]):
        raise ValueError("Invalid base_commit")


def task_text(row):
    # Deliberately do not include patch, test_patch, hints, or grading test IDs.
    return ("Fix the following issue in the repository at /testbed. Inspect the code, "
            "implement a minimal general fix, and run relevant verification using the "
            "repository's existing test environment. Do not change existing tests to "
            "make them pass. The exported Git patch is your submission.\n\n"
            "Issue description:\n" + row["problem_statement"] + "\n")


def latest_attempt(directory, instance_id):
    paths = sorted((directory / "instances" / instance_id).glob("attempt-*/state.json"))
    return paths[-1].parent if paths else None


def summarize(directory, *, save=True):
    manifest = read_json(directory / "manifest.json")
    counts, agent_counts = Counter(), Counter()
    rows, predictions = [], []
    calls = tokens = references = 0
    elapsed = 0.0
    for instance_id in manifest["instance_ids"]:
        attempt = latest_attempt(directory, instance_id)
        state = read_json(attempt / "state.json") if attempt else {"status": "pending"}
        counts[state["status"]] += 1
        references += int(state.get("gold", {}).get("resolved", False))
        result = state.get("agent", {})
        if result:
            agent_counts[result.get("status", "unknown")] += 1
        row_calls = row_tokens = 0
        # Keep costs from interrupted/error attempts instead of hiding retries.
        for old in (directory / "instances" / instance_id).glob("attempt-*/agent/run"):
            result_path = old / "result.json"
            if not result_path.exists():
                result_path = old / "progress.json"
            if not result_path.exists():
                continue
            old_result = read_json(result_path)
            usage = old_result.get("usage", {})
            row_calls += old_result.get("request_attempts", usage.get("calls", 0))
            row_tokens += usage.get("total_tokens", 0)
            elapsed += old_result.get("elapsed_seconds", 0)
        calls += row_calls
        tokens += row_tokens
        rows.append({"instance_id": instance_id, "status": state["status"],
                     "phase": state.get("phase"),
                     "agent_status": result.get("status"), "reason": state.get("reason", ""),
                     "attempt": str(attempt.relative_to(directory)) if attempt else None,
                     "agent_elapsed_seconds": result.get("elapsed_seconds"),
                     "request_attempts": row_calls, "total_tokens": row_tokens})
        if attempt and (attempt / "prediction.json").exists():
            predictions.append(read_json(attempt / "prediction.json"))
    total = len(manifest["instance_ids"])
    summary = {"run_id": manifest["run_id"], "mode": manifest["mode"],
               "updated_at": now(), "selected": total, "counts": dict(counts),
               "agent_statuses": dict(agent_counts), "resolved": counts["resolved"],
               "resolved_rate": counts["resolved"] / total if manifest["mode"] == "run" else None,
               "reference_validated": references, "agent_elapsed_seconds": round(elapsed, 3),
               "request_attempts": calls, "total_tokens": tokens, "instances": rows}
    if not save:
        return summary
    write_json(directory / "summary.json", summary)
    # Rebuild atomically so a resumed run cannot duplicate JSONL submissions.
    target = directory / "predictions.jsonl"
    temporary = target.with_suffix(".jsonl.tmp")
    temporary.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in predictions),
                         encoding="utf-8")
    temporary.replace(target)
    return summary
