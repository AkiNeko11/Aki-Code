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
MAX_REACTIVE_RETRIES: int = int(os.getenv("MAX_REACTIVE_RETRIES", "1"))
SUMMARY_MAX_TOKENS: int = int(os.getenv("SUMMARY_MAX_TOKENS", "1024"))
TRANSCRIPTS_DIR: str = os.getenv("TRANSCRIPTS_DIR", ".transcripts")
TOOL_OUTPUTS_DIR: str = os.getenv("TOOL_OUTPUTS_DIR", ".task_outputs/tool-results")
