import logging
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import Settings
from bot.tools import list_active_goal_chats, list_daily_briefing_chats
from bot.utils import sanitize_latex

logger = logging.getLogger(__name__)


def _render_bar(percent: int, length: int = 6) -> str:
    filled = max(0, min(length, round(length * percent / 100)))
    return "█" * filled + "░" * (length - filled)


class ProactiveCheckins:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self._bot = bot
        timezone = ZoneInfo(settings.checkin_timezone)
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._scheduler.add_job(
            self._send_checkins,
            CronTrigger(hour=",".join(map(str, settings.checkin_hours)), minute=0, timezone=timezone),
            id="opensmart-daily-checkins",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._send_daily_briefings,
            CronTrigger(hour=8, minute=0, timezone=timezone),
            id="opensmart-daily-briefings",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def start(self) -> None:
        self._scheduler.start()
        logger.info("Daily check-ins scheduled at configured hours")

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _send_checkins(self) -> None:
        for chat_id, goals in await list_active_goal_chats():
            goal_lines = "\n".join(f"• {goal['title']}" for goal in goals)
            text = (
                "🎯 Чек-ин OPENSMART: как продвигаются твои цели?\n\n"
                f"{goal_lines}\n\n"
                "Расскажи, что удалось сделать или сколько времени уделил. "
                "Я зафиксирую прогресс и подскажу следующий шаг!"
            )
            try:
                await self._bot.send_message(chat_id, sanitize_latex(text))
            except TelegramAPIError:
                logger.exception("Could not send scheduled check-in to chat %s", chat_id)

    async def _send_daily_briefings(self) -> None:
        for chat_id, briefing in await list_daily_briefing_chats():
            sections = ["☀️ Доброе утро! OPENSMART Daily"]
            if briefing.get("first_action"):
                sections.append(f"🔥 Что лучше сделать первым:\n{briefing['first_action']}")
            if briefing.get("reminders"):
                sections.append("⏰ Напоминания и дедлайны:\n" + "\n".join(
                    f"• {item['text']}" + (f" (до {item['due_at']})" if item.get("due_at") else "")
                    for item in briefing["reminders"][:5]
                ))
            if briefing.get("goals"):
                goals_lines = []
                for goal in briefing["goals"][:5]:
                    bar = _render_bar(goal.get("progress_percent", 0))
                    next_step = goal.get("next_recommended_step", "выбрать следующий шаг")
                    goals_lines.append(
                        f"• {goal['title']} [{bar}] {goal.get('progress_percent', 0)}%\n"
                        f"  👉 Следующий шаг: {next_step}"
                    )
                sections.append("🎯 Цели в фокусе (/tasks):\n" + "\n".join(goals_lines))
            if briefing.get("notes"):
                notes_lines = [
                    f"• {note.get('text', '')[:120]}" + (f" (#{', #'.join(note.get('tags', []))})" if note.get("tags") else "")
                    for note in briefing["notes"][:3]
                ]
                sections.append("💡 Заметки и идеи:\n" + "\n".join(notes_lines))
            try:
                await self._bot.send_message(chat_id, sanitize_latex("\n\n".join(sections)))
            except TelegramAPIError:
                logger.exception("Could not send daily briefing to chat %s", chat_id)