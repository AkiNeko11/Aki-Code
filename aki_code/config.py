import os
from dotenv import load_dotenv

load_dotenv()

API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
MODEL: str = os.getenv("MODEL_NAME", "deepseek-flash")
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
SKILLS_DIR: str = os.getenv("SKILLS_DIR", "skills")

# ── Context compaction (Lesson 08) ───────────────────────────────
# Char-based (not token-based) so no tokenizer/API round-trip is needed.
CONTEXT_CHAR_LIMIT: int = int(os.getenv("CONTEXT_CHAR_LIMIT", "50000"))
TOOL_RESULT_BUDGET_CHARS: int = int(os.getenv("TOOL_RESULT_BUDGET_CHARS", "200000"))
LARGE_RESULT_CHAR_LIMIT: int = int(os.getenv("LARGE_RESULT_CHAR_LIMIT", "30000"))
LARGE_RESULT_PREVIEW_CHARS: int = int(os.getenv("LARGE_RESULT_PREVIEW_CHARS", "2000"))
MAX_MESSAGES: int = int(os.getenv("MAX_MESSAGES", "50"))
SNIP_HEAD_MESSAGES: int = int(os.getenv("SNIP_HEAD_MESSAGES", "3"))
KEEP_RECENT_MESSAGES: int = int(os.getenv("KEEP_RECENT_MESSAGES", "5"))
# Step 3 (micro_compact / fit_tool_results): shrink already-consumed results
# toward COMPACT_TARGET_RATIO of the limit before resorting to a summary.
COMPACT_TARGET_RATIO: float = float(os.getenv("COMPACT_TARGET_RATIO", "0.8"))
KEEP_RECENT_RESULTS: int = int(os.getenv("KEEP_RECENT_RESULTS", "3"))
MICRO_COMPACT_MIN_CHARS: int = int(os.getenv("MICRO_COMPACT_MIN_CHARS", "120"))
FIT_RESULT_PREVIEW_CHARS: int = int(os.getenv("FIT_RESULT_PREVIEW_CHARS", "1000"))
MAX_REACTIVE_RETRIES: int = int(os.getenv("MAX_REACTIVE_RETRIES", "1"))
SUMMARY_MAX_TOKENS: int = int(os.getenv("SUMMARY_MAX_TOKENS", "1024"))
TRANSCRIPTS_DIR: str = os.getenv("TRANSCRIPTS_DIR", ".transcripts")
TOOL_OUTPUTS_DIR: str = os.getenv("TOOL_OUTPUTS_DIR", ".task_outputs/tool-results")

# ── Memory system (Lesson 09) ────────────────────────────────────
# Durable, cross-session knowledge: one Markdown file per memory under
# MEMORY_DIR, with MEMORY.md as a rebuilt index. Recall is selective.
MEMORY_DIR: str = os.getenv("MEMORY_DIR", ".memory")
MEMORY_INDEX_NAME: str = os.getenv("MEMORY_INDEX_NAME", "MEMORY.md")
MEMORY_MAX_RECALL: int = int(os.getenv("MEMORY_MAX_RECALL", "5"))
MEMORY_RECALL_CHAR_CAP: int = int(os.getenv("MEMORY_RECALL_CHAR_CAP", "8000"))
MEMORY_SELECT_MAX_TOKENS: int = int(os.getenv("MEMORY_SELECT_MAX_TOKENS", "200"))
MEMORY_EXTRACT_MAX_TOKENS: int = int(os.getenv("MEMORY_EXTRACT_MAX_TOKENS", "1024"))
MEMORY_CONSOLIDATE_MAX_TOKENS: int = int(os.getenv("MEMORY_CONSOLIDATE_MAX_TOKENS", "2048"))
MEMORY_CONSOLIDATE_THRESHOLD: int = int(os.getenv("MEMORY_CONSOLIDATE_THRESHOLD", "10"))
MEMORY_DUP_THRESHOLD: float = float(os.getenv("MEMORY_DUP_THRESHOLD", "0.85"))

# ── Task system (Lesson 10) ──────────────────────────────────────
# Durable, cross-session task graph: one JSON file per task under TASKS_DIR,
# with blockedBy dependencies and owner/claim. Distinct from the in-session
# TodoWrite checklist (Lesson 05).
TASKS_DIR: str = os.getenv("TASKS_DIR", ".tasks")

# ── Background tasks (Lesson 11) ─────────────────────────────────
# Long-running bash commands execute in a daemon thread + own process group;
# results are collected and injected as notifications on later turns.
BACKGROUND_JOB_TIMEOUT: int = int(os.getenv("BACKGROUND_JOB_TIMEOUT", "600"))
BACKGROUND_MAX_OUTPUT_CHARS: int = int(os.getenv("BACKGROUND_MAX_OUTPUT_CHARS", "200000"))

# ── Cron scheduler (Lesson 12) ───────────────────────────────────
# Durable job definitions (durable=True) persist to a single JSON file, written
# atomically. In-memory jobs (durable=False) vanish on restart.
SCHEDULES_FILE: str = os.getenv("SCHEDULES_FILE", ".scheduled_tasks.json")
CRON_POLL_INTERVAL: float = float(os.getenv("CRON_POLL_INTERVAL", "1.0"))
CRON_PROCESSOR_INTERVAL: float = float(os.getenv("CRON_PROCESSOR_INTERVAL", "0.2"))

# ── Agent teams (Lesson 13) ──────────────────────────────────────
# Per-agent file mailboxes for Lead/teammate communication, kept out of the
# model context until the runtime delivers them.
MAILBOX_DIR: str = os.getenv("MAILBOX_DIR", ".mailboxes")
# Optional per-task git worktrees so parallel edits do not collide; a task with
# no worktree runs in the repository working directory.
WORKTREES_DIR: str = os.getenv("WORKTREES_DIR", ".worktrees")

# Saved, read-only workflows (Lesson 16). Limits apply per run/resume attempt.
WORKFLOWS_DIR: str = os.getenv("WORKFLOWS_DIR", ".workflows")
WORKFLOW_MAX_CONCURRENCY: int = int(os.getenv("WORKFLOW_MAX_CONCURRENCY", "3"))
WORKFLOW_MAX_CALLS: int = int(os.getenv("WORKFLOW_MAX_CALLS", "60"))
WORKFLOW_MAX_TURNS: int = int(os.getenv("WORKFLOW_MAX_TURNS", "12"))
WORKFLOW_MAX_TOKENS: int = int(os.getenv("WORKFLOW_MAX_TOKENS", "100000"))
WORKFLOW_REQUEST_TIMEOUT: float = float(os.getenv("WORKFLOW_REQUEST_TIMEOUT", "120"))
