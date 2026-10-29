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
        print(f"\n[OPENSMART CONFIG ERROR]: {error}", file=sys.stderr)
        print("Check environment variables on your server or your .env file. See .env.example\n", file=sys.stderr)
        return

    logger.info(
        "Loaded settings: LLM Base URL=%s, Model=%s",
        settings.llm_base_url,
        settings.llm_model,
    )
    print(f"\n[OPENSMART] Connecting to LLM: {settings.llm_base_url}")
    print(f"[OPENSMART] Model: {settings.llm_model}\n")

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
            print(f">>> OPENSMART successfully started as @{me.username} <<<")
            print("Bot is ready. Press Ctrl+C to stop.\n")
        except TelegramAPIError:
            logger.exception("Failed to get bot info")

        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start the bot"),
                    BotCommand(command="tasks", description="Long-term goals & milestone tracking"),
                    BotCommand(command="notes", description="Personal notes, ideas & tags"),
                    BotCommand(command="forget", description="Safely remove specific items"),
                    BotCommand(command="daily", description="Daily briefing, deadlines & focus"),
                    BotCommand(command="focus", description="Focus session timer"),
                    BotCommand(command="new", description="Reset conversation context"),
                    BotCommand(command="help", description="Help & command list"),
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
