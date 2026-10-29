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

    nebius_key = (os.getenv("NEBIUS_API_KEY") or "").strip().strip('"').strip("'")
    openrouter_key = (os.getenv("OPENROUTER_API_KEY") or "").strip().strip('"').strip("'")
    generic_key = (os.getenv("LLM_API_KEY") or "").strip().strip('"').strip("'")
    custom_url = (os.getenv("LLM_BASE_URL") or "").strip().strip('"').strip("'")
    custom_model = (os.getenv("LLM_MODEL") or "").strip().strip('"').strip("'")

    # Smart provider resolution:
    # 1. If OpenRouter key is provided and valid (e.g. starts with sk-or-) and Nebius is not explicitly set or placeholder:
    if _is_valid_key(openrouter_key) and not _is_valid_key(nebius_key):
        llm_api_key = openrouter_key
        default_base_url = "https://openrouter.ai/api/v1"
        default_model = "openai/gpt-4o-mini"
    # 2. If Nebius key is provided and valid:
    elif _is_valid_key(nebius_key):
        llm_api_key = nebius_key
        default_base_url = "https://api.studio.nebius.ai/v1/"
        default_model = "meta-llama/Meta-Llama-3.1-70B-Instruct"
    # 3. If OpenRouter key is provided:
    elif _is_valid_key(openrouter_key):
        llm_api_key = openrouter_key
        default_base_url = "https://openrouter.ai/api/v1"
        default_model = "openai/gpt-4o-mini"
    # 4. Fallback to any generic key:
    else:
        llm_api_key = generic_key or nebius_key or openrouter_key
        default_base_url = "https://api.studio.nebius.ai/v1/"
        default_model = "meta-llama/Meta-Llama-3.1-70B-Instruct"

    llm_base_url = custom_url or default_base_url
    llm_model = custom_model or default_model

    if not bot_token or not _is_valid_key(bot_token):
        raise ValueError("Укажите корректный BOT_TOKEN в переменных окружения.")
    if not llm_api_key or not _is_valid_key(llm_api_key):
        raise ValueError("Укажите действующий NEBIUS_API_KEY или OPENROUTER_API_KEY в переменных окружения.")

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
