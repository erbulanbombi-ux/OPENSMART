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

STATUS_EN = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "paused": "Paused",
    "completed": "Completed",
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
        "👋 Hello! I am OPENSMART — your personal AI assistant for goals, study, fitness, habits, and projects.\n\n"
        "What I can do:\n"
        "🎯 /tasks — Long-term goals with milestones, history, and progress\n"
        "📝 /notes — Personal notes & ideas with tags and dates\n"
        "☀️ /daily — Daily assistant (first priority, deadlines, goals)\n"
        "🗑 /forget — Safely remove specific items with YES confirmation\n"
        "⏱ /focus 25 — Deep work & concentration timer\n\n"
        "Send me your goal (e.g., \"I want to learn Spanish\") or a thought (\"Note: buy books #study\"), and let's get started!",
    )


@router.message(Command("new"))
async def handle_new(message: Message, agent: Agent) -> None:
    agent.reset_session(message.chat.id)
    await _safe_answer(message, "Conversation context has been cleared. What would you like to discuss?")


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
                "Usage: /tasks add Goal Title | Desired Outcome\n"
                "Example: /tasks add Learn English | Pass IELTS with 7.5 in 6 months",
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
                f"📋 Recommended milestone roadmap:\n{steps}\n\n"
                "💡 To confirm your goal, send:\n"
                f"/tasks add {title} | <your desired outcome>",
            )
        elif result.get("created"):
            goal = result["created"]
            milestones_text = "\n".join(
                f"  {'🔄' if s['status'] == 'in_progress' else '⏳'} {i}. {s['title']}"
                for i, s in enumerate(goal.get("milestones", []), 1)
            )
            await _safe_answer(
                message,
                f"🎯 Goal created!\n"
                f"📌 Title: {goal['title']} [ID: {goal['id']}]\n"
                f"🏁 Desired outcome: {goal['desired_result']}\n"
                f"📊 Status: {STATUS_EN.get(goal['status'], goal['status'])}\n\n"
                f"Milestones:\n{milestones_text}\n\n"
                f"👉 Next step: {goal.get('next_recommended_step', 'Start')}\n\n"
                f"Mark milestone completed: /tasks advance {goal['id']}",
            )
        else:
            await _safe_answer(message, "Failed to create goal. Please check your wording and try again.")
        return

    # 2. /tasks advance <id> or /tasks next <id>
    if subcommand in {"advance", "next", "step"}:
        goal_id = payload or None
        result = await execute_tool("manage_goal", {"action": "advance", "goal_id": goal_id}, scope=scope)
        if result.get("error"):
            await _safe_answer(message, f"Error: {result['error']}\nUsage: /tasks advance ID")
            return
        goal = result.get("updated", {})
        progress = goal.get("progress_percent", 0)
        bar = _render_progress_bar(progress)
        next_step = goal.get("next_recommended_step", "All milestones completed!")
        status = STATUS_EN.get(goal.get("status"), goal.get("status"))
        await _safe_answer(
            message,
            f"🎉 Great job! Milestone completed!\n\n"
            f"🎯 Goal: \"{goal.get('title')}\" [ID: {goal.get('id')}]\n"
            f"📊 Progress: [{bar}] {progress}% ({status})\n"
            f"👉 Next step: {next_step}\n\n"
            f"Details: /tasks view {goal.get('id')}",
        )
        return

    # 3. /tasks pause <id>
    if subcommand in {"pause", "stop"}:
        result = await execute_tool("manage_goal", {"action": "pause", "goal_id": payload}, scope=scope)
        if result.get("updated"):
            await _safe_answer(message, f"⏸ Goal \"{result['updated'].get('title')}\" paused.")
        else:
            await _safe_answer(message, "Could not pause goal. Specify ID: /tasks pause ID")
        return

    # 4. /tasks resume <id>
    if subcommand in {"resume", "continue", "start"}:
        result = await execute_tool("manage_goal", {"action": "resume", "goal_id": payload}, scope=scope)
        if result.get("updated"):
            await _safe_answer(message, f"▶️ Goal \"{result['updated'].get('title')}\" resumed!")
        else:
            await _safe_answer(message, "Could not resume goal. Specify ID: /tasks resume ID")
        return

    # 5. /tasks done <id> or /tasks complete <id>
    if subcommand in {"done", "complete"}:
        result = await execute_tool("manage_goal", {"action": "complete", "goal_id": payload}, scope=scope)
        if result.get("completed"):
            goal = result["completed"]
            await _safe_answer(
                message,
                f"🏆 Congratulations! Goal \"{goal.get('title')}\" successfully completed (100%)!\n"
                f"All completed milestones are recorded in history.",
            )
        else:
            await _safe_answer(message, "Could not complete goal. Specify ID: /tasks done ID")
        return

    # 6. /tasks view <id> or /tasks get <id>
    if subcommand in {"view", "get", "info"} or (subcommand and not payload and len(subcommand) >= 6):
        goal_id = payload if subcommand in {"view", "get", "info"} else subcommand
        result = await execute_tool("manage_goal", {"action": "get", "goal_id": goal_id}, scope=scope)
        if result.get("goal"):
            goal = result["goal"]
            progress = goal.get("progress_percent", 0)
            bar = _render_progress_bar(progress)
            status = STATUS_EN.get(goal.get("status"), goal.get("status"))
            milestones_lines = []
            for i, step in enumerate(goal.get("milestones", []), 1):
                icon = "✅" if step.get("status") == "completed" else ("🔄" if step.get("id") == goal.get("current_milestone") else "⏳")
                milestones_lines.append(f"{icon} {i}. {step.get('title')}")
            milestones_text = "\n".join(milestones_lines) or "No milestones set"

            history_lines = []
            for h in goal.get("history", []):
                time_str = h.get("completed_at", "")[:16].replace("T", " ")
                history_lines.append(f"• {h.get('title')} ({time_str})")
            history_text = "\n".join(history_lines) if history_lines else "No completed milestones yet"

            details = [
                f"🎯 Goal: {goal.get('title')} [ID: {goal.get('id')}]",
                f"🏁 Desired outcome: {goal.get('desired_result') or 'Not specified'}",
                f"📊 Progress: [{bar}] {progress}% ({status})",
                f"👉 Next step: {goal.get('next_recommended_step', 'None')}",
                f"\n📋 Milestones & subtasks:\n{milestones_text}",
                f"\n📜 Completed history:\n{history_text}",
            ]
            if goal.get("deadline"):
                details.append(f"⏰ Deadline: {goal.get('deadline')}")
            details.append(f"\nActions: /tasks advance {goal.get('id')} | /tasks pause {goal.get('id')} | /forget {goal.get('id')}")
            await _safe_answer(message, "\n".join(details))
            return
        elif subcommand in {"view", "get", "info"}:
            await _safe_answer(message, "Goal with this ID was not found.")
            return

    # 7. List active goals (default)
    result = await execute_tool("manage_goal", {"action": "list"}, scope=scope)
    if result.get("error"):
        await _safe_answer(message, "Could not fetch goals. Please try again later.")
        return
    all_goals = result.get("goals", [])
    active_goals = [g for g in all_goals if g.get("status") != "completed"]

    if not active_goals:
        await _safe_answer(
            message,
            "🎯 You don't have any active goals yet.\n\n"
            "Create a long-term goal:\n"
            "• Command: /tasks add Title | Desired outcome\n"
            "• Plain message: \"My goal is to learn Spanish\"",
        )
        return

    blocks = ["🎯 Your Active Goals & Tasks:\n"]
    for goal in active_goals:
        progress = goal.get("progress_percent", 0)
        bar = _render_progress_bar(progress)
        status = STATUS_EN.get(goal.get("status"), goal.get("status"))
        curr_step = goal.get("next_recommended_step") or "Define next step"

        milestones = goal.get("milestones", [])
        m_preview = []
        for i, s in enumerate(milestones[:4], 1):
            icon = "✅" if s.get("status") == "completed" else ("🔄" if s.get("id") == goal.get("current_milestone") else "⏳")
            m_preview.append(f"  {icon} {i}. {s.get('title')}")
        if len(milestones) > 4:
            m_preview.append(f"  ... {len(milestones) - 4} more milestones")

        block = (
            f"📌 {goal.get('title')} [ID: {goal.get('id')}]\n"
            f"🏁 Outcome: {goal.get('desired_result') or 'Not specified'}\n"
            f"📊 Progress: [{bar}] {progress}% ({status})\n"
            f"👉 Next step: {curr_step}\n"
            f"Milestones:\n" + "\n".join(m_preview) + "\n"
            f"Actions: /tasks advance {goal.get('id')} | /tasks view {goal.get('id')}"
        )
        blocks.append(block)

    blocks.append("💡 Commands: /tasks add Title | Result, /tasks advance ID, /tasks view ID")
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
                "Usage: /notes add Note text #tag\n"
                "Example: /notes add Idea for mobile app #startup #ideas",
            )
            return
        result = await manage_notes("create", text=text, tags=tags, scope=scope)
        if result.get("created"):
            note = result["created"]
            tags_str = f" (#{', #'.join(note.get('tags', []))})" if note.get("tags") else ""
            date_str = note.get("date") or note.get("created_at", "")[:10]
            await _safe_answer(
                message,
                f"📝 Note saved!\n"
                f"ID: {note['id']}\n"
                f"Text: {note['text']}{tags_str}\n"
                f"📅 Date: {date_str}\n\n"
                f"View: /notes view {note['id']} | Delete: /forget {note['id']}",
            )
        else:
            await _safe_answer(message, "Failed to save note.")
        return

    # 2. /notes view <id> or /notes <id>
    if action in {"view", "get"} or (action and not payload and len(action) == 8):
        note_id = payload if action in {"view", "get"} else action
        result = await manage_notes("get", note_id=note_id, scope=scope)
        if result.get("note"):
            note = result["note"]
            tags_str = ", ".join(f"#{t}" for t in note.get("tags", [])) or "no tags"
            date_str = note.get("date") or note.get("created_at", "")[:10]
            created_at = note.get("created_at", "")[:19].replace("T", " ")
            updated_at = note.get("updated_at", "")[:19].replace("T", " ") if note.get("updated_at") else None
            lines = [
                f"📝 Note [ID: {note['id']}]",
                f"📅 Date: {date_str} (created: {created_at})",
            ]
            if updated_at:
                lines.append(f"🔄 Updated: {updated_at}")
            lines.extend([
                f"🏷 Tags: {tags_str}",
                f"\n{note['text']}",
                f"\nEdit: /notes edit {note['id']} | new text",
                f"Delete: /forget {note['id']}",
            ])
            await _safe_answer(message, "\n".join(lines))
        else:
            await _safe_answer(message, "Note with this ID was not found.")
        return

    # 3. /notes edit <id> | <new text>
    if action == "edit":
        note_id, separator, text = payload.partition("|")
        note_id = note_id.strip()
        text = text.strip()
        if not separator or not text:
            await _safe_answer(
                message,
                "Usage: /notes edit ID | New note text #tag\n"
                "Example: /notes edit a1b2c3d4 | Updated text #new",
            )
            return
        tags = [w.lstrip("#") for w in text.split() if w.startswith("#") and len(w) > 1]
        clean_text = " ".join(w for w in text.split() if not w.startswith("#")).strip()
        result = await manage_notes("edit", note_id=note_id, text=clean_text or text, tags=tags if tags else None, scope=scope)
        if result.get("updated"):
            note = result["updated"]
            await _safe_answer(message, f"✅ Note [{note['id']}] successfully updated!")
        else:
            await _safe_answer(message, "Failed to update note. Please verify ID.")
        return

    # 4. /notes search <query>
    if action in {"search", "find"}:
        if not payload:
            await _safe_answer(message, "Specify search query: /notes search keyword")
            return
        result = await manage_notes("search", query=payload.lstrip("#"), scope=scope)
        notes = result.get("notes", [])
        if not notes:
            await _safe_answer(message, f"No notes found matching \"{payload}\".")
            return
        lines = []
        for n in notes:
            tags = f" (#{', #'.join(n.get('tags', []))})" if n.get("tags") else ""
            date = n.get("date") or n.get("created_at", "")[:10]
            lines.append(f"• [{n.get('id')}] {n.get('text', '')[:140]}{tags} ({date})")
        await _safe_answer(message, f"🔍 Found notes ({len(notes)}):\n" + "\n".join(lines))
        return

    # 5. List all notes (/notes)
    if not action or action == "list":
        result = await manage_notes("list", scope=scope)
        notes = result.get("notes", [])
        if not notes:
            await _safe_answer(
                message,
                "📝 Notes storage is empty.\n\n"
                "How to save a note:\n"
                "• Plain message: \"Note: buy groceries #shopping\"\n"
                "• Command: /notes add text #tag",
            )
            return
        lines = []
        for n in notes:
            tags = f" (#{', #'.join(n.get('tags', []))})" if n.get("tags") else ""
            date = n.get("date") or n.get("created_at", "")[:10]
            lines.append(f"• [{n.get('id')}] {n.get('text', '')[:160]}{tags} — 📅 {date}")
        lines.append("\n💡 View: /notes view ID | Edit: /notes edit ID | new text | Delete: /forget ID")
        await _safe_answer(message, f"📝 Your saved notes ({len(notes)}):\n" + "\n".join(lines))
        return

    await _safe_answer(
        message,
        "Notes commands:\n"
        "• /notes — list all notes\n"
        "• /notes add text #tag — add a note\n"
        "• /notes view ID — view specific note\n"
        "• /notes edit ID | new text — edit note\n"
        "• /notes search query — search notes\n"
        "• /forget ID — delete note with YES confirmation\n\n"
        "Or simply send: \"Note: note text #tag\"",
    )


@router.message(Command("forget"))
async def handle_forget(message: Message) -> None:
    _, _, query = (message.text or "").partition(" ")
    query = query.strip()
    if not query:
        await _safe_answer(
            message,
            "🗑 /forget — Safely remove specific items\n\n"
            "Usage: /forget <text or ID>\n"
            "Example: /forget a1b2c3d4 or /forget project\n\n"
            "The bot will find matching entries and ask for confirmation with YES before deleting.\n"
            "Any other response cancels deletion.",
        )
        return

    matches = await search_forget_items(query, scope=str(message.chat.id))
    if not matches:
        await _safe_answer(message, f"No entries found matching \"{query}\".")
        return

    _PENDING_FORGET[message.chat.id] = [item["id"] for item in matches]

    if len(matches) == 1:
        item = matches[0]
        await _safe_answer(
            message,
            f"🔍 Found entry to delete:\n\n"
            f"• [{item.get('kind', 'Item')}] ID: {item['id']}\n"
            f"  {item['text']}\n\n"
            f"⚠️ Reply YES to confirm deletion.\n"
            f"Any other response will cancel.",
        )
    else:
        lines = "\n".join(f"• [{item.get('kind')}] {item['id']} — {item['text']}" for item in matches)
        await _safe_answer(
            message,
            f"🔍 Found {len(matches)} matches:\n\n{lines}\n\n"
            f"⚠️ Reply YES to confirm deleting ALL found entries.\n"
            f"To delete only one item, specify its exact ID: /forget ID\n"
            f"Any other response will cancel.",
        )


@router.message(Command("daily"))
async def handle_daily(message: Message) -> None:
    briefing = await get_daily_briefing(str(message.chat.id))
    if briefing.get("error"):
        await _safe_answer(message, "Could not compile daily briefing. Please try again shortly.")
        return

    sections = ["☀️ OPENSMART Daily — Your Assistant for Today"]

    if briefing.get("first_action"):
        sections.append(f"🔥 Recommended First Priority:\n{briefing['first_action']}")

    if briefing.get("reminders"):
        reminders_text = "\n".join(
            f"• {item['text']}" + (f" (by {item['due_at']})" if item.get("due_at") else "")
            for item in briefing["reminders"][:5]
        )
        sections.append(f"⏰ Upcoming Deadlines & Reminders:\n{reminders_text}")

    if briefing.get("goals"):
        goals_lines = []
        for goal in briefing["goals"][:5]:
            bar = _render_progress_bar(goal.get("progress_percent", 0), length=6)
            next_step = goal.get("next_recommended_step", "choose next step")
            goals_lines.append(
                f"• {goal['title']} [{bar}] {goal.get('progress_percent', 0)}%\n"
                f"  👉 Next step: {next_step}"
            )
        sections.append("🎯 Active Goals in Focus (/tasks):\n" + "\n".join(goals_lines))

    if briefing.get("notes"):
        notes_lines = [
            f"• {note.get('text', '')[:120]}" + (f" (#{', #'.join(note.get('tags', []))})" if note.get("tags") else "")
            for note in briefing["notes"][:3]
        ]
        sections.append("💡 Useful Notes & Ideas:\n" + "\n".join(notes_lines))

    if len(sections) == 1:
        sections.append(
            "No active goals, tasks, or notes yet.\n\n"
            "Add your first goal: /tasks add Title | Outcome\n"
            "Or record a note: \"Note: your idea #tag\""
        )

    sections.append("💡 Commands: /tasks — goals | /notes — notes | /focus 25 — focus session")
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
                f"Focus session of {minutes} minute(s) completed. What did you accomplish, and what is your next step?"
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
        await _safe_answer(message, "Focus session stopped. What did you accomplish?")
        return
    try:
        minutes = int(argument)
    except ValueError:
        await _safe_answer(message, "Specify duration in minutes: /focus 25 (from 1 to 180), or /focus stop.")
        return
    if not 1 <= minutes <= 180:
        await _safe_answer(message, "Duration must be between 1 and 180 minutes.")
        return
    previous = _FOCUS_TASKS.pop(chat_id, None)
    if previous:
        previous.cancel()
    agent.start_focus(chat_id, minutes)
    _FOCUS_TASKS[chat_id] = asyncio.create_task(_finish_focus_timer(message, agent, minutes))
    await _safe_answer(
        message,
        f"Focus session started for {minutes} minute(s). Tell me what you're working on; "
        "stop anytime with /focus stop.",
    )


@router.message(Command("tools"))
async def handle_tools(message: Message) -> None:
    await _safe_answer(message, f"Available tools:\n{describe_tools()}")


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await _safe_answer(
        message,
        "🤖 OPENSMART — Your Personal AI Assistant\n\n"
        "📌 Goal & Task Management:\n"
        "• /tasks — List active goals and progress\n"
        "• /tasks add Title | Outcome — Create goal\n"
        "• /tasks advance ID — Complete current milestone\n"
        "• /tasks view ID — Details, milestones & history\n"
        "• /tasks pause ID / /tasks resume ID — Pause/resume\n\n"
        "📝 Notes & Ideas:\n"
        "• /notes — View all notes\n"
        "• /notes add Text #tag — Add a note\n"
        "• /notes view ID — View specific note\n"
        "• /notes edit ID | new text — Edit note\n"
        "• /notes search query — Search notes\n"
        "• Plain message: \"Note: text #tag\"\n\n"
        "🗑 Safe Deletion:\n"
        "• /forget query — Safely remove entry with YES confirmation\n\n"
        "☀️ Daily Organization:\n"
        "• /daily — Daily plan, deadlines and first priority\n"
        "• /focus 25 — Focus timer (1-180 minutes)\n"
        "• /focus stop — Stop focus timer\n\n"
        "⚙️ Service Commands:\n"
        "• /new — Clear conversation context\n"
        "• /memory — Context state\n"
        "• /tools — List available tools\n\n"
        "Simply send me a message anytime to plan a goal, save an idea, or solve a problem!",
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
            await _safe_answer(message, f"✅ Successfully removed {removed} item(s).")
        else:
            await _safe_answer(message, "❌ Deletion canceled. All items preserved.")
        return

    # 2. Check direct note creation from regular message (supports English & Russian prefixes)
    note_match = re.match(
        r"^(?:note|save\s+note|save|заметка|запиши|сохрани|#note|#заметка)[:\s]+(.*)",
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
                f"📝 Note saved!\n"
                f"ID: {note['id']}\n"
                f"Text: {note['text']}{tags_str}\n"
                f"📅 Date: {date_str}\n\n"
                f"View: /notes view {note['id']} | Delete: /forget {note['id']}",
            )
            return

    # 3. LLM agent processing
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            reply = await agent.generate_reply(message.chat.id, message.text)
    except (OpenAIError, TimeoutError):
        logger.exception("LLM request failed or timed out")
        reply = "The service is temporarily unavailable. Please try again shortly."
    except Exception:
        logger.exception("Unexpected agent failure for chat %s", message.chat.id)
        reply = "Failed to process the request. Please try again shortly."

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
            await _safe_answer(message, "Voice message downloaded and ready for analysis.")
    except (TelegramAPIError, OSError):
        logger.exception("Failed to download voice message from chat %s", message.chat.id)
        await _safe_answer(message, "Failed to download voice message. Please try again later.")


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    if not message.photo:
        return
    photo = message.photo[-1]
    try:
        async with downloaded_telegram_file(bot, photo.file_id, ".jpg") as file_path:
            logger.info("Downloaded photo to %s", file_path)
            await _safe_answer(message, "Photo downloaded and ready for analysis.")
    except (TelegramAPIError, OSError):
        logger.exception("Failed to download photo from chat %s", message.chat.id)
        await _safe_answer(message, "Failed to download photo. Please try again later.")
