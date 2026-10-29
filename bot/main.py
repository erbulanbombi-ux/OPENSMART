import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from bot.agent import Agent
from bot.checkins import ProactiveCheckins
from bot.config import load_settings
from bot.handlers import router

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
    except ValueError as error:
        logger.error("Invalid bot configuration: %s", error)
        print(f"\n[OPENSMART ОШИБКА КОНФИГУРАЦИИ]: {error}", file=sys.stderr)
        print("Проверьте переменные окружения на сервере или файл .env. См. .env.example\n", file=sys.stderr)
        return

    logger.info(
        "Loaded settings: LLM Base URL=%s, Model=%s",
        settings.llm_base_url,
        settings.llm_model,
    )
    print(f"\n[OPENSMART] Подключение к LLM: {settings.llm_base_url}")
    print(f"[OPENSMART] Модель: {settings.llm_model}\n")

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    agent = Agent(settings)
    checkins = ProactiveCheckins(bot, settings)
    dispatcher["agent"] = agent
    dispatcher.include_router(router)

    try:
        # Delete any hanging webhooks to allow long polling
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except TelegramAPIError as error:
            logger.warning("Could not delete webhook: %s", error)

        try:
            me = await bot.get_me()
            logger.info("Bot connected as @%s (id=%s)", me.username, me.id)
            print(f">>> OPENSMART успешно запущен как @{me.username} <<<")
            print("Бот готов к работе. Нажмите Ctrl+C для остановки.\n")
        except TelegramAPIError:
            logger.exception("Failed to get bot info")

        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Запустить бота"),
                    BotCommand(command="tasks", description="Долгосрочные цели и задачи"),
                    BotCommand(command="notes", description="Заметки, идеи и теги"),
                    BotCommand(command="forget", description="Безопасное удаление информации"),
                    BotCommand(command="daily", description="План на день и дедлайны"),
                    BotCommand(command="focus", description="Таймер фокус-сессии"),
                    BotCommand(command="new", description="Сбросить контекст диалога"),
                    BotCommand(command="help", description="Справка по всем командам"),
                ]
            )
        except TelegramAPIError:
            logger.exception("Failed to register Telegram bot commands")

        checkins.start()
        await dispatcher.start_polling(bot)
    except TelegramAPIError:
        logger.exception("Telegram API error while polling")
    except Exception:
        logger.exception("Unexpected error while running the bot")
        raise
    finally:
        checkins.stop()
        await agent.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
