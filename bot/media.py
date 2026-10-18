from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import AsyncIterator

from aiogram import Bot


@asynccontextmanager
async def downloaded_telegram_file(
    bot: Bot,
    file_id: str,
    suffix: str,
) -> AsyncIterator[Path]:
    """Download a Telegram file and keep it available until the context exits."""
    with TemporaryDirectory(prefix="opensmart-") as temp_dir:
        destination = Path(temp_dir) / f"upload{suffix}"
        await bot.download(file_id, destination=destination)
        yield destination
