from __future__ import annotations

import time

import anthropic

from aki_code import config


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=config.API_KEY,
        base_url=config.BASE_URL,
        max_retries=0,  # We handle retries ourselves
    )


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
MAX_RETRIES = 3
BASE_DELAY = 1.0


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return False


def retry_delay(attempt: int, exc: Exception) -> float:
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = exc.response.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return BASE_DELAY * (2 ** attempt)


def call_with_retry(fn, *args, **kwargs):
    """Execute fn with exponential backoff on retryable errors."""
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not is_retryable(exc) or attempt == MAX_RETRIES:
                raise
            last_exc = exc
            delay = retry_delay(attempt, exc)
            time.sleep(delay)
    raise last_exc  # unreachable, but satisfies type checker
