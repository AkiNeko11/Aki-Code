import os
from dotenv import load_dotenv

load_dotenv()

API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
MODEL: str = os.getenv("MODEL_NAME", "deepseek-flash")
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
SKILLS_DIR: str = os.getenv("SKILLS_DIR", "skills")
