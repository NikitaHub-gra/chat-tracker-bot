"""Merged Telegram message handlers — combines src/ bot + TG_Dashboard logic."""
import logging
import time
from datetime import datetime, timezone

from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle,
    InputTextMessageContent, ChosenInlineResult, MessageReactionUpdated
)

from src.database.db import db
from src.services.settings_service import (
    get_settings, get_phrases, is_team_member,
)
from src.services.text_utils import (
    is_positive, is_negative, is_pending_reply, is_no_reply, parse_agent_tag,
)
from src.api.dashboard.sse import tg_sse

logger = logging.getLogger(__name__)
router = Router(name="common")


def _now() -> int:
    return int(time.time())


async def _record_missed_if_needed(conv, answered_at: int | None = None):
    """Record a missed event if conversation waited too long."""
    if conv.status != "waiting" or not conv.lastClientMsgAt:
        return
    s = await get_settings()
    threshold = s.missedThreshold if s else 900
    waited = (answered_at or _now()) - conv.lastClientMsgAt
    if waited >= threshold:
        await db.missedevent.create(data={
            "messenger": "telegram",
            "chatId": str(conv.chatId),
            "clientName": conv.clientName or "",
            "clientUsername": conv.clientUsername,
            "lastMsg": (conv.lastClientMsgText or "")[:300],
            "waitedSeconds": waited,
            "missedAt": answered_at or _now(),
            "source": "timeout",
        })


# ── /start — Engineer Registration ───────────────────────────

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.chat.type != "private" or not message.from_user:
        return

    tg_id = str(message.from_user.id)
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    full_name = message.from_user.full_name

    try:
        await db.engineer.upsert(
            where={"telegramId": tg_id},
            data={
                "create": {"telegramId": tg_id, "username": username, "name": full_name},
                "update": {"username": username, "name": full_name}
            }
        )
        await message.reply(
            f"👋 <b>Привет, {full_name}!</b>\n"
            f"Ты успешно зарегистрирован в базе как инженер.\n\n"
            f"💼 <b>Для Telegram Business (Секретарь):</b>\n"
            f"Не забудь подключить этого бота в <i>Настройки -> Telegram Business -> Чат-боты</i>."
        )
    except Exception as e:
        logger.error(f"Ошибка регистрации инженера: {e}", exc_info=True)


# ── /active — Open Chats List ───────────────────────────────

@router.message(Command("active"))
async def cmd_get_opened_chats(message: types.Message):
    if not message.from_user:
        return

    user_id = str(message.from_user.id)
    is_engineer = await db.engineer.find_unique(where={"telegramId": user_id})

    from src.bot.handlers.admin import is_has_admin_rights
    is_admin = await is_has_admin_rights(user_id)

    if not is_engineer and not is_admin:
        await message.reply("У вас нет доступа к этой команде.")
        return

    # Get open tickets from both old ActiveChat and new Conversation tables
    opened_tickets = await db.activechat.find_many(
        where={"status": "opened"},
        order={"updatedAt": "asc"}
    )

    if not opened_tickets:
        await message.reply("🎉 <b>Идеально!</b> Нет ни одного чата, ожидающего ответа.")
        return

    text = f"⏳ <b>Список чатов, ожидающих ответа ({len(opened_tickets)}):</b>\n\n"
    for idx, ticket in enumerate(opened_tickets, 1):
        now = datetime.now(timezone.utc)
        waiting_time = now - ticket.updatedAt
        minutes_waiting = int(waiting_time.total_seconds() / 60)
        text += (
            f"{idx}. 👥 <b>{ticket.chatTitle}</b>\n"
            f"   👤 Клиент: <code>{ticket.clientName}</code>\n"
            f"   💬 Последнее: <i>\"{ticket.lastMessage}\"</i>\n"
            f"   ⏰ Ждет: <b>{minutes_waiting} мин.</b>\n"
            f"   🏃‍♂️ <a href='{ticket.externalChatUrl}'>Перейти к сообщению</a>\n\n"
        )
    await message.reply(text, disable_web_page_preview=True, parse_mode="HTML")


# =====================================================================
# 1. BUSINESS MESSAGES: Private chats via Telegram Business
# =====================================================================

@router.business_message()
async def handle_business_messages(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    if not message.from_user:
        return

    user_id = str(message.from_user.id)
    is_ignored = await db.ignoreduser.find_unique(where={"id": user_id})
    if is_ignored:
        return

    chat_id = str(message.chat.id)
    client_name = message.from_user.full_name
    text = message.text or message.caption or "[Медиафайл]"
    sent_at = int(message.date.timestamp())  # aiogram gives datetime → store as unix seconds
    chat_url = f"tg://user?id={chat_id}"
    is_client = message.from_user.id == message.chat.id
    has_photo = bool(message.photo or message.sticker or message.document or message.video)

    # Load settings for analysis
    s = await get_settings()
    no_reply_phrases = await get_phrases("noReplyPhrases")
    pending_phrases = await get_phrases("pendingPhrases")
    negative_kw = await get_phrases("negativeKeywords")
    positive_kw = await get_phrases("positiveKeywords")
    agent_tag = parse_agent_tag(text)

    is_team = await is_team_member("telegram", message.from_user.id)

    try:
        if is_team:
            # ── TEAM MEMBER writes (engineer replying to client) ──
            if not is_client:
                conv = await db.conversation.find_first(
                    where={"messenger": "telegram", "chatId": chat_id}
                )
                if conv:
                    await _record_missed_if_needed(conv, sent_at)
                    update_data = {"lastAgentMsgAt": sent_at}
                    if agent_tag:
                        update_data["lastAgentName"] = agent_tag
                    if is_pending_reply(text, pending_phrases):
                        update_data["isPending"] = True
                        update_data["pendingAt"] = sent_at
                    else:
                        update_data["status"] = "answered"
                        update_data["isPending"] = False
                        update_data["pendingAt"] = None
                    await db.conversation.update(where={"id": conv.id}, data=update_data)
                else:
                    await db.conversation.create(data={
                        "messenger": "telegram", "chatId": chat_id, "source": "private",
                        "clientName": f"ЛС: {client_name}",
                        "lastAgentMsgAt": sent_at, "lastAgentName": agent_tag,
                        "status": "answered", "createdAt": sent_at,
                        "msgCount": 1,
                    })

                # Also update legacy ActiveChat
                existing_ac = await db.activechat.find_unique(
                    where={"chatId_userId": {"chatId": chat_id, "userId": chat_id}}
                )
                engineer = await db.engineer.find_unique(where={"telegramId": user_id})
                if existing_ac:
                    await db.activechat.update(
                        where={"chatId_userId": {"chatId": chat_id, "userId": chat_id}},
                        data={
                            "status": "answered",
                            "engineerId": engineer.id if engineer else None,
                            "isAlerted": False,
                            "lastMessage": text,
                            "updatedAt": datetime.now(timezone.utc),
                        }
                    )
                elif engineer:
                    await db.activechat.create(data={
                        "chatId": chat_id, "userId": chat_id,
                        "chatTitle": "Личные сообщения",
                        "clientName": f"ЛС: {client_name}",
                        "externalChatUrl": chat_url,
                        "lastMessage": text,
                        "status": "answered",
                        "engineerId": engineer.id,
                        "isAlerted": False,
                        "updatedAt": datetime.now(timezone.utc),
                    })

            # Store message
            await db.chatmessage.create(data={
                "messenger": "telegram", "msgId": str(message.message_id),
                "chatId": chat_id, "direction": "out",
                "text": text[:500], "agentName": agent_tag,
                "sentAt": sent_at, "hasPhoto": has_photo,
            })
            await tg_sse.broadcast("update", {"source": "business", "chatId": chat_id, "direction": "out"})
            return

        # ── CLIENT writes to engineer ──
        # Ignore if another engineer
        if await db.engineer.find_unique(where={"telegramId": chat_id}):
            return

        conv = await db.conversation.find_first(
            where={"messenger": "telegram", "chatId": chat_id}
        )
        is_no_reply_msg = is_no_reply(text, no_reply_phrases)

        if conv:
            update_data = {
                "clientName": client_name,
                "clientUsername": message.from_user.username,
                "lastClientMsgAt": sent_at,
                "lastClientMsgText": text[:300],
                "msgCount": conv.msgCount + 1,
                "isPending": False,
                "pendingAt": None,
            }
            if not is_no_reply_msg:
                update_data["status"] = "waiting"
            # Sentiment
            if not conv.isNegative and is_negative(text, negative_kw):
                update_data["isNegative"] = True
                update_data["isPositive"] = False
            elif not conv.isNegative and not conv.isPositive and is_positive(text, positive_kw):
                update_data["isPositive"] = True
            await db.conversation.update(where={"id": conv.id}, data=update_data)
        else:
            await db.conversation.create(data={
                "messenger": "telegram", "chatId": chat_id, "source": "private",
                "clientName": client_name,
                "clientUsername": message.from_user.username,
                "lastClientMsgAt": sent_at,
                "lastClientMsgText": text[:300],
                "status": "waiting" if not is_no_reply_msg else "answered",
                "isNegative": is_negative(text, negative_kw),
                "isPositive": is_positive(text, positive_kw),
                "createdAt": sent_at, "msgCount": 1,
            })

        # Legacy ActiveChat
        existing_ac = await db.activechat.find_unique(
            where={"chatId_userId": {"chatId": chat_id, "userId": chat_id}}
        )
        if existing_ac:
            is_already_opened = existing_ac.status == "opened"
            await db.activechat.update(
                where={"chatId_userId": {"chatId": chat_id, "userId": chat_id}},
                data={
                    "status": "opened", "clientName": f"ЛС: {client_name}",
                    "chatTitle": "Личные сообщения", "lastMessage": text,
                    "externalChatUrl": chat_url, "isAlerted": False,
                    "updatedAt": existing_ac.updatedAt if is_already_opened else datetime.now(timezone.utc),
                }
            )
        else:
            await db.activechat.create(data={
                "chatId": chat_id, "userId": chat_id,
                "chatTitle": "Личные сообщения",
                "clientName": f"ЛС: {client_name}",
                "externalChatUrl": chat_url, "lastMessage": text,
                "status": "opened", "isAlerted": False,
                "updatedAt": datetime.now(timezone.utc),
            })

        # Store message
        await db.chatmessage.create(data={
            "messenger": "telegram", "msgId": str(message.message_id),
            "chatId": chat_id, "direction": "in",
            "text": text[:500], "sentAt": sent_at, "hasPhoto": has_photo,
        })
        await tg_sse.broadcast("update", {"source": "business", "chatId": chat_id, "direction": "in"})

    except Exception as e:
        logger.error(f"Ошибка в бизнес-чате: {e}", exc_info=True)


# =====================================================================
# 2. GROUP MESSAGES
# =====================================================================

@router.message()
async def handle_group_messages(message: types.Message):
    if message.chat.type == "private" or not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        return

    user_id = str(message.from_user.id)
    is_ignored = await db.ignoreduser.find_unique(where={"id": user_id})
    if is_ignored:
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title or "Группа поддержки"
    text = message.text or message.caption or "[Медиафайл]"
    sent_at = int(message.date.timestamp())  # aiogram gives datetime → store as unix seconds
    chat_url = f"https://t.me/c/{chat_id.replace('-100', '')}/{message.message_id}"
    from_name = message.from_user.full_name
    from_id = message.from_user.id

    # Check if this is the alert chat
    s = await get_settings()
    if s and s.alertChatId == chat_id:
        return

    is_team = await is_team_member("telegram", from_id)
    no_reply_phrases = await get_phrases("noReplyPhrases")
    pending_phrases = await get_phrases("pendingPhrases")

    try:
        # Register group if not exists
        gi = await db.groupinfo.find_first(
            where={"messenger": "telegram", "groupId": chat_id}
        )
        if not gi:
            await db.groupinfo.create(data={
                "messenger": "telegram", "groupId": chat_id,
                "title": chat_title, "createdAt": sent_at,
            })
        elif gi.title != chat_title:
            await db.groupinfo.update(where={"id": gi.id}, data={"title": chat_title})

        if is_team:
            # ── TEAM member in group ──
            pending = is_pending_reply(text, pending_phrases)

            if not pending:
                # Real answer — mark unanswered client messages as answered
                unanswered = await db.groupmessage.find_many(
                    where={
                        "messenger": "telegram", "groupId": chat_id,
                        "answered": False, "isTeam": False,
                        "sentAt": {"lte": sent_at},
                    }
                )
                for m in unanswered:
                    await db.groupmessage.update(
                        where={"id": m.id},
                        data={"answered": True, "answeredAt": sent_at}
                    )

            # Store team message
            await db.groupmessage.create(data={
                "messenger": "telegram", "groupId": chat_id,
                "msgId": str(message.message_id),
                "fromId": from_id, "fromName": from_name,
                "text": text[:300], "sentAt": sent_at,
                "answered": True, "answeredAt": sent_at,
                "isTeam": True, "isPendingReply": pending,
            })

            # Also handle legacy ActiveChat (reply-based)
            if message.reply_to_message and message.reply_to_message.from_user:
                replied_user_id = str(message.reply_to_message.from_user.id)
                is_reply_to_engineer = await is_team_member("telegram", message.reply_to_message.from_user.id)
                if not is_reply_to_engineer:
                    target_ticket = await db.activechat.find_unique(
                        where={"chatId_userId": {"chatId": chat_id, "userId": replied_user_id}}
                    )
                    engineer = await db.engineer.find_unique(where={"telegramId": user_id})
                    if target_ticket:
                        await db.activechat.update(
                            where={"chatId_userId": {"chatId": chat_id, "userId": replied_user_id}},
                            data={
                                "status": "answered",
                                "engineerId": engineer.id if engineer else None,
                                "isAlerted": False,
                                "lastMessage": f"Ответ для {message.reply_to_message.from_user.full_name}: {text}",
                                "updatedAt": datetime.now(timezone.utc),
                            }
                        )
        else:
            # ── CLIENT in group ──
            no_reply = is_no_reply(text, no_reply_phrases)

            await db.groupmessage.create(data={
                "messenger": "telegram", "groupId": chat_id,
                "msgId": str(message.message_id),
                "fromId": from_id, "fromName": from_name,
                "text": text[:300], "sentAt": sent_at,
                "answered": no_reply,
                "answeredAt": sent_at if no_reply else None,
                "isTeam": False,
            })

            # Legacy ActiveChat
            existing_ticket = await db.activechat.find_unique(
                where={"chatId_userId": {"chatId": chat_id, "userId": user_id}}
            )
            if existing_ticket:
                is_already_opened = existing_ticket.status == "opened"
                await db.activechat.update(
                    where={"chatId_userId": {"chatId": chat_id, "userId": user_id}},
                    data={
                        "status": "opened", "clientName": from_name,
                        "chatTitle": chat_title, "lastMessage": text,
                        "externalChatUrl": chat_url, "isAlerted": False,
                        "updatedAt": existing_ticket.updatedAt if is_already_opened else datetime.now(timezone.utc),
                    }
                )
            else:
                await db.activechat.create(data={
                    "chatId": chat_id, "userId": user_id,
                    "clientName": from_name, "chatTitle": chat_title,
                    "externalChatUrl": chat_url, "lastMessage": text,
                    "status": "opened", "isAlerted": False,
                    "updatedAt": datetime.now(timezone.utc),
                })

        await tg_sse.broadcast("update", {"source": "group", "chatId": chat_id, "isTeam": is_team})

    except Exception as e:
        logger.error(f"Ошибка в групповом обработчике: {e}", exc_info=True)


# =====================================================================
# 3. REACTIONS
# =====================================================================

@router.message_reaction()
async def handle_message_reaction(reaction_update: MessageReactionUpdated):
    try:
        if not reaction_update.user:
            return

        user_id = str(reaction_update.user.id)
        is_team = await is_team_member("telegram", reaction_update.user.id)
        if not is_team:
            return

        ALLOWED_EMOJIS = ["✅", "👌", "👍"]
        current_emoji = None
        for reaction in reaction_update.new_reaction:
            if reaction.type == "emoji" and reaction.emoji in ALLOWED_EMOJIS:
                current_emoji = reaction.emoji
                break
        if not current_emoji:
            return

        chat_id = str(reaction_update.chat.id)
        chat_type = reaction_update.chat.type

        # Find open ticket in legacy ActiveChat
        active_chat = await db.activechat.find_first(
            where={"chatId": chat_id, "status": "opened"}
        )
        if not active_chat and chat_type == "private":
            active_chat = await db.activechat.find_unique(
                where={"chatId_userId": {"chatId": chat_id, "userId": chat_id}}
            )
            if active_chat and active_chat.status != "opened":
                active_chat = None

        if active_chat:
            engineer = await db.engineer.find_unique(where={"telegramId": user_id})
            await db.activechat.update(
                where={"chatId_userId": {"chatId": active_chat.chatId, "userId": active_chat.userId}},
                data={
                    "status": "answered",
                    "engineerId": engineer.id if engineer else None,
                    "isAlerted": False,
                    "lastMessage": f"Закрыто реакцией {current_emoji}",
                    "updatedAt": datetime.now(timezone.utc),
                }
            )

        # Also update Conversation table
        conv = await db.conversation.find_first(
            where={"messenger": "telegram", "chatId": chat_id, "status": "waiting"}
        )
        if conv:
            await db.conversation.update(
                where={"id": conv.id},
                data={"status": "answered", "lastAgentMsgAt": _now()}
            )

    except Exception as e:
        logger.error(f"Ошибка в обработчике реакций: {e}", exc_info=True)


# =====================================================================
# 4. BUSINESS CONNECTION (capture owner ID)
# =====================================================================

@router.business_connection()
async def handle_business_connection(connection: types.BusinessConnection):
    """Capture business owner ID from Telegram Business connection."""
    if connection.user:
        owner_id = connection.user.id
        from src.services.settings_service import update_settings_field
        await update_settings_field("tgOwnerId", owner_id)
        logger.info(f"Business owner ID captured: {owner_id}")


# =====================================================================
# 5. INLINE QUERY
# =====================================================================

@router.inline_query()
async def inline_close_handler(inline_query: InlineQuery):
    user_id = str(inline_query.from_user.id)
    engineer = await db.engineer.find_unique(where={"telegramId": user_id})
    if not engineer:
        await inline_query.answer(
            results=[],
            switch_pm_text="Только для инженеров",
            switch_pm_parameter="auth",
            cache_time=1
        )
        return

    opened_tickets = await db.activechat.find_many(
        where={"status": "opened"}, order={"updatedAt": "asc"}, take=10
    )
    if not opened_tickets:
        await inline_query.answer(
            results=[],
            switch_pm_text="Нет открытых тикетов",
            switch_pm_parameter="empty",
            cache_time=1
        )
        return

    results = []
    for t in opened_tickets:
        result_id = f"close|{t.chatId}|{t.userId}"
        client = t.clientName[:25] if t.clientName else "Неизвестно"
        last_msg = t.lastMessage[:40] if t.lastMessage else "—"
        chat_title = t.chatTitle[:20] if t.chatTitle else "ЛС"
        is_mine = t.engineerId == engineer.id
        prefix = "✅" if is_mine else "⚡"
        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=f"{prefix} {client}",
                description=f"💬 {last_msg} | 📍 {chat_title}",
                input_message_content=InputTextMessageContent(
                    message_text=f"Тикет закрыт: {client}",
                    parse_mode="HTML"
                )
            )
        )
    await inline_query.answer(results=results, cache_time=1, is_personal=True)


@router.chosen_inline_result()
async def chosen_close_handler(chosen_result: ChosenInlineResult):
    result_id = chosen_result.result_id
    if not result_id.startswith("close|"):
        return

    _, chat_id, user_id = result_id.split("|")
    engineer_id = str(chosen_result.from_user.id)
    engineer = await db.engineer.find_unique(where={"telegramId": engineer_id})
    if not engineer:
        return

    try:
        await db.activechat.update(
            where={"chatId_userId": {"chatId": chat_id, "userId": user_id}},
            data={
                "status": "answered",
                "engineerId": engineer.id,
                "isAlerted": False,
                "lastMessage": f"Закрыто через inline ({engineer.name})",
                "updatedAt": datetime.now(timezone.utc),
            }
        )
    except Exception as e:
        logger.error(f"Ошибка inline закрытия: {e}", exc_info=True)
