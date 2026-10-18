import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from openai import OpenAIError

from bot.agent import Agent
from bot.media import downloaded_telegram_file
from bot.tools import (
    describe_tools,
    execute_tool,
    forget_item,
    get_daily_briefing,
    manage_notes,
    search_forget_items,
)
from bot.utils import format_telegram_messages, sanitize_latex

logger = logging.getLogger(__name__)
router = Router()
_PENDING_FORGET: dict[int, list[str]] = {}
_FOCUS_TASKS: dict[int, asyncio.Task[None]] = {}

STATUS_RU = {
    "not_started": "Не начата",
    "in_progress": "В процессе",
    "paused": "Приостановлена",
    "completed": "Завершена",
}


def _strip_markdown_v2_escapes(text: str) -> str:
    return re.sub(r"\\([_\*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)


def _render_progress_bar(percent: int, length: int = 8) -> str:
    filled = max(0, min(length, round(length * percent / 100)))
    return "█" * filled + "░" * (length - filled)


async def _safe_answer(message: Message, text: str, parse_mode: str | None = None) -> None:
    try:
        if parse_mode != "MarkdownV2":
            text = sanitize_latex(text)
        await message.answer(text, parse_mode=parse_mode)
    except TelegramAPIError as error:
        logger.warning("Failed to send message with parse_mode=%s: %s", parse_mode, error)
        if parse_mode is not None:
            try:
                fallback_text = _strip_markdown_v2_escapes(text)
                await message.answer(fallback_text)
                return
            except TelegramAPIError:
                pass
        logger.exception("Failed to send a reply to chat %s", message.chat.id)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await _safe_answer(
        message,
        "👋 Привет! Я OPENSMART — твой персональный AI-ассистент для учёбы, спорта, проектов и личных целей.\n\n"
        "Что я умею:\n"
        "🎯 /tasks — долгосрочные цели с этапами, историей и прогрессом\n"
        "📝 /notes — хранилище личных заметок, идей с тегами и датами\n"
        "☀️ /daily — ежедневный помощник (что сделать первым, дедлайны, цели)\n"
        "🗑 /forget — безопасное удаление конкретных записей с подтверждением YES\n"
        "⏱ /focus 25 — таймер глубокой концентрации\n\n"
        "Напиши свою цель (например, «Хочу учить английский») или мысль («Заметка: купить книги #учёба»), и начнём!",
    )


@router.message(Command("new"))
async def handle_new(message: Message, agent: Agent) -> None:
    agent.reset_session(message.chat.id)
    await _safe_answer(message, "Контекст диалога очищен. О чём поговорим?")


@router.message(Command("memory"))
async def handle_memory(message: Message, agent: Agent) -> None:
    await _safe_answer(message, agent.get_memory_state(message.chat.id))


@router.message(Command("goals", "tasks"))
async def handle_tasks(message: Message) -> None:
    command_text = message.text or ""
    _, _, arguments = command_text.partition(" ")
    arguments = arguments.strip()
    subcommand, _, payload = arguments.partition(" ")
    subcommand = subcommand.casefold()
    payload = payload.strip()
    scope = str(message.chat.id)

    # 1. /tasks add <title> [| <desired_result>]
    if subcommand in {"add", "new", "create"}:
        title, separator, desired_result = payload.partition("|")
        title = title.strip()
        desired_result = desired_result.strip() if separator else ""
        if not title:
            await _safe_answer(
                message,
                "Использование: /tasks add Название цели | Желаемый результат\n"
                "Пример: /tasks add Учить английский | Сдать IELTS на 7.0 через 6 месяцев",
            )
            return

        result = await execute_tool(
            "manage_goal",
            {
                "action": "create",
                "title": title,
                "desired_result": desired_result,
            },
            scope=scope,
        )
        if result.get("clarification_required"):
            steps = "\n".join(
                f"{index}. {step.lstrip('0123456789. ')}"
                for index, step in enumerate(result.get("suggested_milestones", []), 1)
            )
            await _safe_answer(
                message,
                f"🎯 {result['question']}\n\n"
                f"📋 Рекомендуемый план этапов:\n{steps}\n\n"
                "💡 Чтобы утвердить цель, отправьте команду с желаемым результатом:\n"
                f"/tasks add {title} | <твой желаемый результат>",
            )
        elif result.get("created"):
            goal = result["created"]
            milestones_text = "\n".join(
                f"  {'🔄' if s['status'] == 'in_progress' else '⏳'} {i}. {s['title']}"
                for i, s in enumerate(goal.get("milestones", []), 1)
            )
            await _safe_answer(
                message,
                f"🎯 Цель создана!\n"
                f"📌 Название: {goal['title']} [ID: {goal['id']}]\n"
                f"🏁 Желаемый результат: {goal['desired_result']}\n"
                f"📊 Статус: {STATUS_RU.get(goal['status'], goal['status'])}\n\n"
                f"Этапы:\n{milestones_text}\n\n"
                f"👉 Следующий шаг: {goal.get('next_recommended_step', 'Начать')}\n\n"
                f"Отметить этап выполненным: /tasks advance {goal['id']}",
            )
        else:
            await _safe_answer(message, "Не удалось создать цель. Проверьте формулировку и попробуйте снова.")
        return

    # 2. /tasks advance <id> or /tasks next <id>
    if subcommand in {"advance", "next", "step"}:
        goal_id = payload or None
        result = await execute_tool("manage_goal", {"action": "advance", "goal_id": goal_id}, scope=scope)
        if result.get("error"):
            await _safe_answer(message, f"Ошибка: {result['error']}\nИспользуйте: /tasks advance ID")
            return
        goal = result.get("updated", {})
        progress = goal.get("progress_percent", 0)
        bar = _render_progress_bar(progress)
        next_step = goal.get("next_recommended_step", "Все этапы завершены!")
        status = STATUS_RU.get(goal.get("status"), goal.get("status"))
        await _safe_answer(
            message,
            f"🎉 Отлично! Этап выполнен!\n\n"
            f"🎯 Цель: «{goal.get('title')}» [ID: {goal.get('id')}]\n"
            f"📊 Прогресс: [{bar}] {progress}% ({status})\n"
            f"👉 Следующий шаг: {next_step}\n\n"
            f"Подробнее: /tasks view {goal.get('id')}",
        )
        return

    # 3. /tasks pause <id>
    if subcommand in {"pause", "stop"}:
        result = await execute_tool("manage_goal", {"action": "pause", "goal_id": payload}, scope=scope)
        if result.get("updated"):
            await _safe_answer(message, f"⏸ Цель «{result['updated'].get('title')}» приостановлена.")
        else:
            await _safe_answer(message, "Не удалось поставить цель на паузу. Укажите ID: /tasks pause ID")
        return

    # 4. /tasks resume <id>
    if subcommand in {"resume", "continue", "start"}:
        result = await execute_tool("manage_goal", {"action": "resume", "goal_id": payload}, scope=scope)
        if result.get("updated"):
            await _safe_answer(message, f"▶️ Цель «{result['updated'].get('title')}» возобновлена!")
        else:
            await _safe_answer(message, "Не удалось возобновить цель. Укажите ID: /tasks resume ID")
        return

    # 5. /tasks done <id> or /tasks complete <id>
    if subcommand in {"done", "complete"}:
        result = await execute_tool("manage_goal", {"action": "complete", "goal_id": payload}, scope=scope)
        if result.get("completed"):
            goal = result["completed"]
            await _safe_answer(
                message,
                f"🏆 Поздравляем! Цель «{goal.get('title')}» успешно завершена (100%)!\n"
                f"Все выполненные этапы сохранены в истории.",
            )
        else:
            await _safe_answer(message, "Не удалось завершить цель. Укажите ID: /tasks done ID")
        return

    # 6. /tasks view <id> or /tasks get <id>
    if subcommand in {"view", "get", "info"} or (subcommand and not payload and len(subcommand) >= 6):
        goal_id = payload if subcommand in {"view", "get", "info"} else subcommand
        result = await execute_tool("manage_goal", {"action": "get", "goal_id": goal_id}, scope=scope)
        if result.get("goal"):
            goal = result["goal"]
            progress = goal.get("progress_percent", 0)
            bar = _render_progress_bar(progress)
            status = STATUS_RU.get(goal.get("status"), goal.get("status"))
            milestones_lines = []
            for i, step in enumerate(goal.get("milestones", []), 1):
                icon = "✅" if step.get("status") == "completed" else ("🔄" if step.get("id") == goal.get("current_milestone") else "⏳")
                milestones_lines.append(f"{icon} {i}. {step.get('title')}")
            milestones_text = "\n".join(milestones_lines) or "Этапы не заданы"

            history_lines = []
            for h in goal.get("history", []):
                time_str = h.get("completed_at", "")[:16].replace("T", " ")
                history_lines.append(f"• {h.get('title')} ({time_str})")
            history_text = "\n".join(history_lines) if history_lines else "Пока нет завершённых этапов"

            details = [
                f"🎯 Цель: {goal.get('title')} [ID: {goal.get('id')}]",
                f"🏁 Желаемый результат: {goal.get('desired_result') or 'Не указан'}",
                f"📊 Прогресс: [{bar}] {progress}% ({status})",
                f"👉 Следующий шаг: {goal.get('next_recommended_step', 'Нет шага')}",
                f"\n📋 Этапы и подзадачи:\n{milestones_text}",
                f"\n📜 История выполненных этапов:\n{history_text}",
            ]
            if goal.get("deadline"):
                details.append(f"⏰ Дедлайн: {goal.get('deadline')}")
            details.append(f"\nДействия: /tasks advance {goal.get('id')} | /tasks pause {goal.get('id')} | /forget {goal.get('id')}")
            await _safe_answer(message, "\n".join(details))
            return
        elif subcommand in {"view", "get", "info"}:
            await _safe_answer(message, "Цель с таким ID не найдена.")
            return

    # 7. List active goals (default)
    result = await execute_tool("manage_goal", {"action": "list"}, scope=scope)
    if result.get("error"):
        await _safe_answer(message, "Не удалось получить список целей. Попробуйте позже.")
        return
    all_goals = result.get("goals", [])
    active_goals = [g for g in all_goals if g.get("status") != "completed"]

    if not active_goals:
        await _safe_answer(
            message,
            "🎯 У вас пока нет активных целей.\n\n"
            "Создать долгосрочную цель:\n"
            "• Командой: /tasks add Название | Желаемый результат\n"
            "• Или обычным сообщением: «Моя цель — выучить английский»",
        )
        return

    blocks = ["🎯 Ваши активные цели и задачи:\n"]
    for goal in active_goals:
        progress = goal.get("progress_percent", 0)
        bar = _render_progress_bar(progress)
        status = STATUS_RU.get(goal.get("status"), goal.get("status"))
        curr_step = goal.get("next_recommended_step") or "Определить следующий шаг"

        milestones = goal.get("milestones", [])
        m_preview = []
        for i, s in enumerate(milestones[:4], 1):
            icon = "✅" if s.get("status") == "completed" else ("🔄" if s.get("id") == goal.get("current_milestone") else "⏳")
            m_preview.append(f"  {icon} {i}. {s.get('title')}")
        if len(milestones) > 4:
            m_preview.append(f"  ... ещё {len(milestones) - 4} этапов")

        block = (
            f"📌 {goal.get('title')} [ID: {goal.get('id')}]\n"
            f"🏁 Результат: {goal.get('desired_result') or 'Не указан'}\n"
            f"📊 Прогресс: [{bar}] {progress}% ({status})\n"
            f"👉 Текущий шаг: {curr_step}\n"
            f"Этапы:\n" + "\n".join(m_preview) + "\n"
            f"Действия: /tasks advance {goal.get('id')} | /tasks view {goal.get('id')}"
        )
        blocks.append(block)

    blocks.append("💡 Команды: /tasks add Название | Результат, /tasks advance ID, /tasks view ID")
    await _safe_answer(message, "\n\n".join(blocks))


@router.message(Command("notes"))
async def handle_notes(message: Message) -> None:
    _, _, arguments = (message.text or "").partition(" ")
    arguments = arguments.strip()
    action, _, payload = arguments.partition(" ")
    action = action.casefold()
    payload = payload.strip()
    scope = str(message.chat.id)

    # 1. /notes add <text> [#tag]
    if action in {"add", "new", "create"}:
        tags = [w.lstrip("#") for w in payload.split() if w.startswith("#") and len(w) > 1]
        text = " ".join(w for w in payload.split() if not w.startswith("#")).strip()
        if not text:
            await _safe_answer(
                message,
                "Использование: /notes add Текст заметки #тег\n"
                "Пример: /notes add Идея для приложения #стартап #идеи",
            )
            return
        result = await manage_notes("create", text=text, tags=tags, scope=scope)
        if result.get("created"):
            note = result["created"]
            tags_str = f" (#{', #'.join(note.get('tags', []))})" if note.get("tags") else ""
            date_str = note.get("date") or note.get("created_at", "")[:10]
            await _safe_answer(
                message,
                f"📝 Заметка сохранена!\n"
                f"ID: {note['id']}\n"
                f"Текст: {note['text']}{tags_str}\n"
                f"📅 Дата: {date_str}\n\n"
                f"Просмотр: /notes view {note['id']} | Удаление: /forget {note['id']}",
            )
        else:
            await _safe_answer(message, "Не удалось сохранить заметку.")
        return

    # 2. /notes view <id> or /notes <id>
    if action in {"view", "get"} or (action and not payload and len(action) == 8):
        note_id = payload if action in {"view", "get"} else action
        result = await manage_notes("get", note_id=note_id, scope=scope)
        if result.get("note"):
            note = result["note"]
            tags_str = ", ".join(f"#{t}" for t in note.get("tags", [])) or "нет тегов"
            date_str = note.get("date") or note.get("created_at", "")[:10]
            created_at = note.get("created_at", "")[:19].replace("T", " ")
            updated_at = note.get("updated_at", "")[:19].replace("T", " ") if note.get("updated_at") else None
            lines = [
                f"📝 Заметка [ID: {note['id']}]",
                f"📅 Дата: {date_str} (создана: {created_at})",
            ]
            if updated_at:
                lines.append(f"🔄 Изменена: {updated_at}")
            lines.extend([
                f"🏷 Теги: {tags_str}",
                f"\n{note['text']}",
                f"\nРедактировать: /notes edit {note['id']} | новый текст",
                f"Удалить: /forget {note['id']}",
            ])
            await _safe_answer(message, "\n".join(lines))
        else:
            await _safe_answer(message, "Заметка с таким ID не найдена.")
        return

    # 3. /notes edit <id> | <new text>
    if action == "edit":
        note_id, separator, text = payload.partition("|")
        note_id = note_id.strip()
        text = text.strip()
        if not separator or not text:
            await _safe_answer(
                message,
                "Использование: /notes edit ID | Новый текст заметки #тег\n"
                "Пример: /notes edit a1b2c3d4 | Обновлённый текст #новое",
            )
            return
        tags = [w.lstrip("#") for w in text.split() if w.startswith("#") and len(w) > 1]
        clean_text = " ".join(w for w in text.split() if not w.startswith("#")).strip()
        result = await manage_notes("edit", note_id=note_id, text=clean_text or text, tags=tags if tags else None, scope=scope)
        if result.get("updated"):
            note = result["updated"]
            await _safe_answer(message, f"✅ Заметка [{note['id']}] успешно обновлена!")
        else:
            await _safe_answer(message, "Не удалось обновить заметку. Проверьте ID.")
        return

    # 4. /notes search <query>
    if action in {"search", "find"}:
        if not payload:
            await _safe_answer(message, "Укажите поисковый запрос: /notes search ключевое слово")
            return
        result = await manage_notes("search", query=payload.lstrip("#"), scope=scope)
        notes = result.get("notes", [])
        if not notes:
            await _safe_answer(message, f"По запросу «{payload}» ничего не найдено.")
            return
        lines = []
        for n in notes:
            tags = f" (#{', #'.join(n.get('tags', []))})" if n.get("tags") else ""
            date = n.get("date") or n.get("created_at", "")[:10]
            lines.append(f"• [{n.get('id')}] {n.get('text', '')[:140]}{tags} ({date})")
        await _safe_answer(message, f"🔍 Найдено заметок ({len(notes)}):\n" + "\n".join(lines))
        return

    # 5. List all notes (/notes)
    if not action or action == "list":
        result = await manage_notes("list", scope=scope)
        notes = result.get("notes", [])
        if not notes:
            await _safe_answer(
                message,
                "📝 Хранилище заметок пусто.\n\n"
                "Как сохранить заметку:\n"
                "• Обычным сообщением: «Заметка: купить молоко #покупки»\n"
                "• Командой: /notes add текст #тег",
            )
            return
        lines = []
        for n in notes:
            tags = f" (#{', #'.join(n.get('tags', []))})" if n.get("tags") else ""
            date = n.get("date") or n.get("created_at", "")[:10]
            lines.append(f"• [{n.get('id')}] {n.get('text', '')[:160]}{tags} — 📅 {date}")
        lines.append("\n💡 Просмотр: /notes view ID | Редактирование: /notes edit ID | новый текст | Удаление: /forget ID")
        await _safe_answer(message, f"📝 Ваши сохранённые заметки ({len(notes)}):\n" + "\n".join(lines))
        return

    await _safe_answer(
        message,
        "Команды для заметок:\n"
        "• /notes — список всех заметок\n"
        "• /notes add текст #тег — добавить заметку\n"
        "• /notes view ID — просмотр конкретной записи\n"
        "• /notes edit ID | новый текст — редактировать\n"
        "• /notes search запрос — поиск по заметкам\n"
        "• /forget ID — удалить запись с подтверждением YES\n\n"
        "Также можно просто написать боту: «Заметка: текст заметки #тег»",
    )


@router.message(Command("forget"))
async def handle_forget(message: Message) -> None:
    _, _, query = (message.text or "").partition(" ")
    query = query.strip()
    if not query:
        await _safe_answer(
            message,
            "🗑 Команда /forget — безопасное удаление информации\n\n"
            "Использование: /forget <текст или ID>\n"
            "Пример: /forget a1b2c3d4 или /forget проект\n\n"
            "Бот найдёт запись и запросит подтверждение словом YES перед удалением.\n"
            "Если ответить иначе — запись не будет удалена.",
        )
        return

    matches = await search_forget_items(query, scope=str(message.chat.id))
    if not matches:
        await _safe_answer(message, f"По запросу «{query}» ничего не найдено.")
        return

    _PENDING_FORGET[message.chat.id] = [item["id"] for item in matches]

    if len(matches) == 1:
        item = matches[0]
        await _safe_answer(
            message,
            f"🔍 Найдена запись для удаления:\n\n"
            f"• [{item.get('kind', 'Запись')}] ID: {item['id']}\n"
            f"  {item['text']}\n\n"
            f"⚠️ Для подтверждения отправьте: YES\n"
            f"Любой другой ответ отменит удаление.",
        )
    else:
        lines = "\n".join(f"• [{item.get('kind')}] {item['id']} — {item['text']}" for item in matches)
        await _safe_answer(
            message,
            f"🔍 Найдено совпадений: {len(matches)}\n\n{lines}\n\n"
            f"⚠️ Отправьте YES, чтобы подтвердить удаление ВСЕХ найденных записей.\n"
            f"Чтобы удалить только одну, укажите её точный ID: /forget ID\n"
            f"Любой другой ответ отменит удаление.",
        )


@router.message(Command("daily"))
async def handle_daily(message: Message) -> None:
    briefing = await get_daily_briefing(str(message.chat.id))
    if briefing.get("error"):
        await _safe_answer(message, "Не удалось собрать сводку на день. Попробуйте чуть позже.")
        return

    sections = ["☀️ OPENSMART Daily — ваш помощник на сегодня"]

    if briefing.get("first_action"):
        sections.append(f"🔥 Что лучше сделать первым:\n{briefing['first_action']}")

    if briefing.get("reminders"):
        reminders_text = "\n".join(
            f"• {item['text']}" + (f" (до {item['due_at']})" if item.get("due_at") else "")
            for item in briefing["reminders"][:5]
        )
        sections.append(f"⏰ Ближайшие дедлайны и напоминания:\n{reminders_text}")

    if briefing.get("goals"):
        goals_lines = []
        for goal in briefing["goals"][:5]:
            bar = _render_progress_bar(goal.get("progress_percent", 0), length=6)
            next_step = goal.get("next_recommended_step", "выбрать следующий шаг")
            goals_lines.append(
                f"• {goal['title']} [{bar}] {goal.get('progress_percent', 0)}%\n"
                f"  👉 Следующий шаг: {next_step}"
            )
        sections.append("🎯 Активные цели (/tasks):\n" + "\n".join(goals_lines))

    if briefing.get("notes"):
        notes_lines = [
            f"• {note.get('text', '')[:120]}" + (f" (#{', #'.join(note.get('tags', []))})" if note.get("tags") else "")
            for note in briefing["notes"][:3]
        ]
        sections.append("💡 Полезные заметки и идеи:\n" + "\n".join(notes_lines))

    if len(sections) == 1:
        sections.append(
            "Пока нет активных целей, задач или заметок.\n\n"
            "Добавьте первую цель: /tasks add Название | Результат\n"
            "Или запишите заметку: «Заметка: ваша идея #тег»"
        )

    sections.append("💡 Команды: /tasks — цели | /notes — заметки | /focus 25 — фокус-сессия")
    await _safe_answer(message, "\n\n".join(sections))


async def _finish_focus_timer(message: Message, agent: Agent, minutes: int) -> None:
    chat_id = message.chat.id
    try:
        await asyncio.sleep(minutes * 60)
        agent.end_focus(chat_id)
        _FOCUS_TASKS.pop(chat_id, None)
        await message.bot.send_message(
            chat_id,
            sanitize_latex(
                f"Focus-сессия на {minutes} минут завершена. Что удалось продвинуть и какой следующий шаг?"
            ),
        )
    except asyncio.CancelledError:
        return
    except TelegramAPIError:
        logger.exception("Could not send focus completion to chat %s", chat_id)


@router.message(Command("focus"))
async def handle_focus(message: Message, agent: Agent) -> None:
    _, _, argument = (message.text or "").partition(" ")
    argument = argument.strip().casefold()
    chat_id = message.chat.id
    if argument in {"stop", "cancel"}:
        task = _FOCUS_TASKS.pop(chat_id, None)
        if task:
            task.cancel()
        agent.end_focus(chat_id)
        await _safe_answer(message, "Focus-сессия остановлена. Что успел сделать?")
        return
    try:
        minutes = int(argument)
    except ValueError:
        await _safe_answer(message, "Укажи длительность в минутах: /focus 25 (от 1 до 180), или /focus stop.")
        return
    if not 1 <= minutes <= 180:
        await _safe_answer(message, "Длительность должна быть от 1 до 180 минут.")
        return
    previous = _FOCUS_TASKS.pop(chat_id, None)
    if previous:
        previous.cancel()
    agent.start_focus(chat_id, minutes)
    _FOCUS_TASKS[chat_id] = asyncio.create_task(_finish_focus_timer(message, agent, minutes))
    await _safe_answer(
        message,
        f"Focus-сессия началась на {minutes} минут. Напиши, над чем работаешь; "
        "остановить можно командой /focus stop.",
    )


@router.message(Command("tools"))
async def handle_tools(message: Message) -> None:
    await _safe_answer(message, f"Available tools:\n{describe_tools()}")


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await _safe_answer(
        message,
        "🤖 OPENSMART — твой персональный AI-помощник\n\n"
        "📌 Управление целями и задачами:\n"
        "• /tasks — список активных целей и прогресс\n"
        "• /tasks add Название | Результат — создать цель\n"
        "• /tasks advance ID — завершить текущий этап\n"
        "• /tasks view ID — подробности, этапы и история\n"
        "• /tasks pause ID / /tasks resume ID — пауза/возобновить\n\n"
        "📝 Заметки и идеи:\n"
        "• /notes — просмотр всех заметок\n"
        "• /notes add Текст #тег — добавить заметку\n"
        "• /notes view ID — просмотр конкретной записи\n"
        "• /notes edit ID | новый текст — редактировать\n"
        "• /notes search запрос — поиск по заметкам\n"
        "• Обычным сообщением: «Заметка: текст #тег»\n\n"
        "🗑 Удаление данных:\n"
        "• /forget запрос — безопасное удаление конкретной записи с подтверждением YES\n\n"
        "☀️ Организация дня:\n"
        "• /daily — план на день, дедлайны и что сделать первым\n"
        "• /focus 25 — таймер продуктивной сессии (1-180 минут)\n"
        "• /focus stop — остановить фокус-сессию\n\n"
        "⚙️ Сервисные команды:\n"
        "• /new — очистить контекст диалога\n"
        "• /memory — состояние контекста\n"
        "• /tools — список доступных инструментов\n\n"
        "Просто напиши мне, и я помогу спланировать цель, сохранить идею или решить задачу!",
    )


@router.message(F.text)
async def handle_text(message: Message, agent: Agent) -> None:
    if message.text is None:
        return

    # 1. Check pending delete confirmation
    pending = _PENDING_FORGET.pop(message.chat.id, None)
    if pending is not None:
        if message.text.strip().casefold() in {"yes", "да"}:
            removed = 0
            for identifier in pending:
                removed += await forget_item(identifier, scope=str(message.chat.id))
            await _safe_answer(message, f"✅ Успешно удалено записей: {removed}.")
        else:
            await _safe_answer(message, "❌ Удаление отменено. Все записи сохранены.")
        return

    # 2. Check direct note creation from regular message
    note_match = re.match(
        r"^(?:заметка|запиши|запиши\s+заметку|сохрани\s+заметку|сохрани|note|#заметка)[:\s]+(.*)",
        message.text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if note_match and note_match.group(1).strip():
        payload = note_match.group(1).strip()
        tags = [w.lstrip("#") for w in payload.split() if w.startswith("#") and len(w) > 1]
        clean_text = " ".join(w for w in payload.split() if not w.startswith("#")).strip()
        res = await manage_notes("create", text=clean_text or payload, tags=tags, scope=str(message.chat.id))
        if res.get("created"):
            note = res["created"]
            tags_str = f" (#{', #'.join(note.get('tags', []))})" if note.get("tags") else ""
            date_str = note.get("date") or note.get("created_at", "")[:10]
            await _safe_answer(
                message,
                f"📝 Заметка сохранена!\n"
                f"ID: {note['id']}\n"
                f"Текст: {note['text']}{tags_str}\n"
                f"📅 Дата: {date_str}\n\n"
                f"Просмотр: /notes view {note['id']} | Удаление: /forget {note['id']}",
            )
            return

    # 3. LLM agent processing
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            reply = await agent.generate_reply(message.chat.id, message.text)
    except (OpenAIError, TimeoutError):
        logger.exception("LLM request failed or timed out")
        reply = "Сервис временно не отвечает. Попробуйте ещё раз чуть позже."
    except Exception:
        logger.exception("Unexpected agent failure for chat %s", message.chat.id)
        reply = "Не удалось обработать запрос. Попробуйте ещё раз чуть позже."

    for chunk in format_telegram_messages(reply):
        await _safe_answer(message, chunk, parse_mode="MarkdownV2")


@router.message(F.voice)
async def handle_voice(message: Message, bot: Bot) -> None:
    voice = message.voice
    if voice is None:
        return
    try:
        async with downloaded_telegram_file(bot, voice.file_id, ".ogg") as file_path:
            logger.info("Downloaded voice message to %s", file_path)
            await _safe_answer(message, "Голосовое сообщение скачано и готово к распознаванию.")
    except (TelegramAPIError, OSError):
        logger.exception("Failed to download voice message from chat %s", message.chat.id)
        await _safe_answer(message, "Не удалось скачать голосовое сообщение. Попробуйте позже.")


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    if not message.photo:
        return
    photo = message.photo[-1]
    try:
        async with downloaded_telegram_file(bot, photo.file_id, ".jpg") as file_path:
            logger.info("Downloaded photo to %s", file_path)
            await _safe_answer(message, "Фото скачано и готово к анализу.")
    except (TelegramAPIError, OSError):
        logger.exception("Failed to download photo from chat %s", message.chat.id)
        await _safe_answer(message, "Не удалось скачать фото. Попробуйте позже.")
