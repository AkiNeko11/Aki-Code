import anthropic
from aki_code import config


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=config.API_KEY,
        base_url=config.BASE_URL,
    )
