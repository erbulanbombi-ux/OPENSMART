import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    checkin_timezone: str = "UTC"
    checkin_hours: tuple[int, ...] = (9, 14, 20)


def _is_valid_key(value: str) -> bool:
    val = value.strip().strip('"').strip("'")
    if not val:
        return False
    low = val.casefold()
    if low.startswith("replace_") or low.startswith("ваш_") or "your_" in low or len(val) < 8:
        return False
    return True


def load_settings() -> Settings:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path)

    bot_token = (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")

    openrouter_key = (os.getenv("OPENROUTER_API_KEY") or "").strip().strip('"').strip("'")
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
    nebius_key = (os.getenv("NEBIUS_API_KEY") or "").strip().strip('"').strip("'")
    generic_key = (os.getenv("LLM_API_KEY") or "").strip().strip('"').strip("'")
    custom_url = (os.getenv("LLM_BASE_URL") or "").strip().strip('"').strip("'")
    custom_model = (os.getenv("LLM_MODEL") or "").strip().strip('"').strip("'")

    # Smart provider resolution (OpenRouter is default/recommended for stability and tools):
    if _is_valid_key(openrouter_key):
        llm_api_key = openrouter_key
        default_base_url = "https://openrouter.ai/api/v1"
        default_model = "openai/gpt-4o-mini"
    elif _is_valid_key(openai_key):
        llm_api_key = openai_key
        default_base_url = "https://api.openai.com/v1"
        default_model = "gpt-4o-mini"
    elif _is_valid_key(nebius_key):
        llm_api_key = nebius_key
        default_base_url = "https://api.studio.nebius.ai/v1/"
        default_model = "meta-llama/Meta-Llama-3.1-70B-Instruct-fast"
    else:
        llm_api_key = generic_key or openrouter_key or openai_key or nebius_key
        default_base_url = "https://openrouter.ai/api/v1"
        default_model = "openai/gpt-4o-mini"

    llm_base_url = custom_url or default_base_url
    llm_model = custom_model or default_model

    # Model slug normalization to avoid 404s:
    # 1. OpenRouter model slugs are case-sensitive and lowercase
    if "openrouter.ai" in llm_base_url.lower():
        if "meta-llama/meta-llama" in llm_model.lower():
            llm_model = "openai/gpt-4o-mini"
        elif llm_model.startswith("meta-llama/Meta-Llama"):
            llm_model = llm_model.lower()
    # 2. Nebius serverless uses -fast suffix
    elif "nebius" in llm_base_url.lower():
        if llm_model == "meta-llama/Meta-Llama-3.1-70B-Instruct":
            llm_model = "meta-llama/Meta-Llama-3.1-70B-Instruct-fast"

    if not bot_token or not _is_valid_key(bot_token):
        raise ValueError("Укажите корректный BOT_TOKEN в переменных окружения.")
    if not llm_api_key or not _is_valid_key(llm_api_key):
        raise ValueError("Укажите действующий OPENROUTER_API_KEY, OPENAI_API_KEY или NEBIUS_API_KEY в переменных окружения.")

    checkin_timezone = (os.getenv("CHECKIN_TIMEZONE") or "UTC").strip()
    try:
        ZoneInfo(checkin_timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown CHECKIN_TIMEZONE: {checkin_timezone}") from error

    try:
        checkin_hours = tuple(
            sorted(
                {
                    int(hour.strip())
                    for hour in (os.getenv("CHECKIN_HOURS") or "9,14,20").split(",")
                }
            )
        )
    except ValueError as error:
        raise ValueError("CHECKIN_HOURS must be comma-separated hours from 0 to 23.") from error
    if not checkin_hours or any(hour < 0 or hour > 23 for hour in checkin_hours):
        raise ValueError("CHECKIN_HOURS must contain hours from 0 to 23.")

    return Settings(
        bot_token=bot_token,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        checkin_timezone=checkin_timezone,
        checkin_hours=checkin_hours,
    )
