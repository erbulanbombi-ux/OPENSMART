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


def load_settings() -> Settings:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path)

    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    llm_base_url = (
        os.getenv("LLM_BASE_URL") or "https://openrouter.ai/api/v1"
    ).strip()
    llm_api_key = (
        os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY") or ""
    ).strip()
    if not llm_api_key and "localhost" in llm_base_url or (
        not llm_api_key and "127.0.0.1" in llm_base_url
    ):
        llm_api_key = "ollama"
    llm_model = (os.getenv("LLM_MODEL") or "openai/gpt-4o-mini").strip()

    if not bot_token or bot_token.startswith("replace_"):
        raise ValueError("Set a valid BOT_TOKEN in the .env file.")
    if not llm_api_key or llm_api_key.startswith("replace_"):
        raise ValueError("Set LLM_API_KEY or OPENROUTER_API_KEY in the .env file.")

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
