"""Background tasks: alert loop, pending timeout checker, daily KPI archiver."""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from src.database.db import db
from src.bot import dispatcher  # use dispatcher.bot (reassigned at startup) — never bind the None directly
from src.services.settings_service import get_settings

logger = logging.getLogger(__name__)


# ── Helper: date/time utilities ──────────────────────────────────────────────

def date_str_local(ts_sec: int) -> str:
    """Convert unix timestamp to YYYY-MM-DD in local time."""
    d = datetime.fromtimestamp(ts_sec)
    return d.strftime("%Y-%m-%d")


def day_bounds_local(date_string: str) -> tuple[int, int]:
    """Return (start_unix, end_unix) for a given YYYY-MM-DD local day."""
    y, m, d = map(int, date_string.split("-"))
    start_dt = datetime(y, m, d, 0, 0, 0)
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def add_days_str(date_string: str, days: int) -> str:
    """Add N days to a YYYY-MM-DD string."""
    y, m, d = map(int, date_string.split("-"))
    new_dt = datetime(y, m, d, 12, 0, 0) + timedelta(days=days)
    return new_dt.strftime("%Y-%m-%d")


# ── Task 1: Forgotten chats alert loop (Telegram) ────────────────────────────

async def check_forgotten_chats_loop():
    """Check for unanswered conversations that exceeded waitTimeoutMin, send alerts."""
    while True:
        try:
            s = await get_settings()
            if not s:
                await asyncio.sleep(30)
                continue

            alert_chat_id = s.alertChatId
            alert_users = await db.alertuser.find_many()

            if not alert_chat_id and not alert_users:
                await asyncio.sleep(30)
                continue

            if dispatcher.bot is None:
                # Bot token not configured yet — nothing to send through.
                await asyncio.sleep(30)
                continue

            timeout_min = s.waitTimeoutMin or 15
            deadline_unix = int(time.time()) - timeout_min * 60

            # Check new Conversation table (status=waiting, not alerted)
            forgotten = await db.conversation.find_many(
                where={
                    "messenger": "telegram",
                    "status": "waiting",
                    "source": "private",
                    "lastClientMsgAt": {"lt": deadline_unix},
                }
            )

            # Also check legacy ActiveChat for backward compat
            deadline_dt = datetime.utcnow() - timedelta(minutes=timeout_min)
            legacy_forgotten = await db.activechat.find_many(
                where={
                    "status": "opened",
                    "isAlerted": False,
                    "updatedAt": {"lt": deadline_dt}
                },
                include={"engineer": True}
            )

            # Process new Conversation table entries
            for conv in forgotten:
                try:
                    alert_text = (
                        f"ПРОСРОЧЕНО ОБРАЩЕНИЕ!\n\n"
                        f"Клиент: {conv.clientName}\n"
                        f"Последнее сообщение:\n\"{conv.lastClientMsgText or ''}\""
                    )

                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="Перейти к сообщению",
                            url=f"https://t.me/c/{conv.chatId.removeprefix('-100')}"
                        )]
                    ])

                    if alert_chat_id:
                        try:
                            await dispatcher.bot.send_message(
                                chat_id=int(alert_chat_id),
                                text=alert_text,
                                reply_markup=keyboard,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Alert send to chat error: {e}")

                    for au in alert_users:
                        try:
                            await dispatcher.bot.send_message(
                                chat_id=int(au.telegramId),
                                text=alert_text,
                                reply_markup=keyboard,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.warning(f"Alert send to user {au.telegramId} error: {e}")

                    # Mark as alerted by updating status
                    await db.conversation.update(
                        where={"id": conv.id},
                        data={"status": "answered"}
                    )
                    logger.info(f"Alert sent for client {conv.clientName}")

                except Exception as e:
                    logger.error(f"Error processing alert for conv {conv.chatId}: {e}", exc_info=True)

            # Process legacy ActiveChat entries
            for chat in legacy_forgotten:
                try:
                    engineer_mention = "Не назначен"
                    if chat.engineerId and chat.engineer:
                        engineer_mention = f"@{chat.engineer.username}" if chat.engineer.username else chat.engineer.name
                    elif chat.engineerId:
                        eng = await db.engineer.find_unique(where={"id": chat.engineerId})
                        if eng:
                            engineer_mention = f"@{eng.username}" if eng.username else eng.name

                    alert_text = (
                        f"ПРОСРОЧЕНО ОБРАЩЕНИЕ!\n\n"
                        f"Клиент: {chat.clientName}\n"
                        f"Чат: {chat.chatTitle}\n"
                        f"Инженер: {engineer_mention}\n"
                        f"Последнее сообщение:\n\"{chat.lastMessage}\""
                    )

                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Перейти к сообщению", url=chat.externalChatUrl)]
                    ])

                    if alert_chat_id:
                        try:
                            await dispatcher.bot.send_message(
                                chat_id=int(alert_chat_id),
                                text=alert_text,
                                reply_markup=keyboard,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Legacy alert send error: {e}")

                    for au in alert_users:
                        try:
                            await dispatcher.bot.send_message(
                                chat_id=int(au.telegramId),
                                text=alert_text,
                                reply_markup=keyboard,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.warning(f"Legacy alert to user {au.telegramId} error: {e}")

                    await db.activechat.update(
                        where={"chatId_userId": {"chatId": chat.chatId, "userId": chat.userId}},
                        data={"isAlerted": True}
                    )
                    logger.info(f"Legacy alert sent for client {chat.clientName}")

                except Exception as e:
                    logger.error(f"Legacy alert error for chatId={chat.chatId}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in forgotten chats loop: {e}", exc_info=True)

        await asyncio.sleep(60)


# ── Task 2: Pending timeout checker (TG + MAX) ──────────────────────────────

async def pending_timeout_checker():
    """Check conversations with isPending=true past pendingTimeout, create MissedEvent records."""
    while True:
        try:
            s = await get_settings()
            if not s:
                await asyncio.sleep(60)
                continue

            pending_timeout = s.pendingTimeout or 1800  # 30 min default
            now = int(time.time())

            # Check private conversations with pending state expired
            for messenger in ("telegram", "max"):
                try:
                    pending_convs = await db.conversation.find_many(
                        where={
                            "messenger": messenger,
                            "isPending": True,
                            "pendingAt": {"not": None},
                        }
                    )

                    for conv in pending_convs:
                        if conv.pendingAt and (now - conv.pendingAt) >= pending_timeout:
                            # Create MissedEvent
                            await db.missedevent.create(data={
                                "messenger": messenger,
                                "chatId": conv.chatId,
                                "clientName": conv.clientName or "",
                                "clientUsername": conv.clientUsername,
                                "lastMsg": conv.lastClientMsgText or "",
                                "waitedSeconds": now - conv.pendingAt,
                                "missedAt": now,
                                "source": "pending_expired",
                            })
                            # Clear pending state
                            await db.conversation.update(
                                where={"id": conv.id},
                                data={"isPending": False, "pendingAt": None}
                            )

                    # Check group messages with pending reply state expired
                    pending_group_msgs = await db.groupmessage.find_many(
                        where={
                            "messenger": messenger,
                            "isPendingReply": True,
                            "answered": False,
                            "isTeam": False,
                        }
                    )

                    for gm in pending_group_msgs:
                        if (now - gm.sentAt) >= pending_timeout:
                            await db.missedevent.create(data={
                                "messenger": messenger,
                                "chatId": f"grp_{gm.groupId}_{gm.fromId}",
                                "clientName": gm.fromName or "",
                                "clientUsername": None,
                                "lastMsg": gm.text or "",
                                "waitedSeconds": now - gm.sentAt,
                                "missedAt": now,
                                "source": "pending_expired",
                            })
                            await db.groupmessage.update(
                                where={"id": gm.id},
                                data={"isPendingReply": False}
                            )

                except Exception as e:
                    logger.error(f"Pending timeout error ({messenger}): {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in pending timeout checker: {e}", exc_info=True)

        await asyncio.sleep(60)  # Check every minute


# ── Task 3: Daily KPI archive catch-up ────────────────────────────────────────

async def _compute_day_stats(messenger: str, day_start: int, day_end: int) -> dict:
    """Compute KPI stats for a single day from DB data."""

    # Count conversations created today
    convs_today = await db.conversation.count(
        where={
            "messenger": messenger,
            "createdAt": {"gte": day_start, "lt": day_end},
        }
    )

    # Count unique group senders (non-team) today
    group_msgs_today = await db.groupmessage.find_many(
        where={
            "messenger": messenger,
            "sentAt": {"gte": day_start, "lt": day_end},
        },
        distinct=["fromId"]
    )
    non_team_group_senders = len([m for m in group_msgs_today if not m.isTeam])
    today_chats = convs_today + non_team_group_senders

    # Messages today
    chat_msgs = await db.chatmessage.find_many(
        where={
            "messenger": messenger,
            "sentAt": {"gte": day_start, "lt": day_end},
        }
    )
    group_msgs = await db.groupmessage.find_many(
        where={
            "messenger": messenger,
            "sentAt": {"gte": day_start, "lt": day_end},
        }
    )

    incoming_today = 0
    agent_msg_today = 0
    by_agent: dict[str, int] = {}

    for m in chat_msgs:
        if m.direction == "in":
            incoming_today += 1
        else:
            agent_msg_today += 1
            if m.agentName:
                by_agent[m.agentName] = by_agent.get(m.agentName, 0) + 1

    for m in group_msgs:
        if m.isTeam:
            agent_msg_today += 1
            if m.fromName:
                by_agent[m.fromName] = by_agent.get(m.fromName, 0) + 1
        else:
            incoming_today += 1

    time_spent_sec = agent_msg_today * 30

    # Response times for answered conversations
    answered_convs = await db.conversation.find_many(
        where={
            "messenger": messenger,
            "status": "answered",
            "lastAgentMsgAt": {"gte": day_start, "lt": day_end},
        }
    )
    response_times = []
    for c in answered_convs:
        if (c.lastClientMsgAt and c.lastAgentMsgAt
                and c.lastAgentMsgAt > c.lastClientMsgAt):
            response_times.append(c.lastAgentMsgAt - c.lastClientMsgAt)

    # Also from group messages
    answered_gm = await db.groupmessage.find_many(
        where={
            "messenger": messenger,
            "answered": True,
            "isTeam": False,
            "answeredAt": {"gte": day_start, "lt": day_end},
        }
    )
    for m in answered_gm:
        if m.answeredAt and m.answeredAt > m.sentAt:
            response_times.append(m.answeredAt - m.sentAt)

    avg_response_sec = (
        round(sum(response_times) / len(response_times))
        if response_times else None
    )

    # Missed events today
    missed_today = await db.missedevent.count(
        where={
            "messenger": messenger,
            "missedAt": {"gte": day_start, "lt": day_end},
            "source": {"not": "pending_expired"},
        }
    )

    return {
        "waiting": None,
        "pendingNow": None,
        "avgResponseSec": avg_response_sec,
        "todayChats": today_chats,
        "byAgent": by_agent,
        "incomingToday": incoming_today,
        "agentMsgToday": agent_msg_today,
        "timeSpentSec": time_spent_sec,
        "missedToday": missed_today,
    }


async def daily_archive_catchup():
    """Compute and store daily KPI stats for past days that haven't been archived yet."""
    await asyncio.sleep(15)  # Wait 15s after startup

    while True:
        try:
            today_str = date_str_local(int(time.time()))

            for messenger in ("telegram", "max"):
                try:
                    # Find the last archived date for this messenger
                    last_record = await db.dailystats.find_first(
                        where={"messenger": messenger},
                        order={"date": "desc"},
                    )

                    if last_record:
                        cursor = add_days_str(last_record.date, 1)
                    else:
                        cursor = add_days_str(today_str, -13)  # Go back 13 days

                    count = 0
                    while cursor <= today_str:  # include today (partial snapshot)
                        start, end = day_bounds_local(cursor)
                        # For today use "now" as the end so we count partial-day data
                        if cursor == today_str:
                            end = int(time.time()) + 1
                        stats = await _compute_day_stats(messenger, start, end)

                        # Upsert daily stats
                        await db.dailystats.upsert(
                            where={
                                "messenger_date": {
                                    "messenger": messenger,
                                    "date": cursor,
                                }
                            },
                            data={
                                "create": {
                                    "messenger": messenger,
                                    "date": cursor,
                                    "waiting": stats["waiting"],
                                    "avgResponseSec": stats["avgResponseSec"],
                                    "todayChats": stats["todayChats"],
                                    "incomingToday": stats["incomingToday"],
                                    "agentMsgToday": stats["agentMsgToday"],
                                    "timeSpentSec": stats["timeSpentSec"],
                                    "missedToday": stats["missedToday"],
                                    "pendingNow": stats["pendingNow"],
                                    "byAgent": json.dumps(stats["byAgent"], ensure_ascii=False),
                                    "recordedAt": int(time.time()),
                                },
                                "update": {
                                    "avgResponseSec": stats["avgResponseSec"],
                                    "todayChats": stats["todayChats"],
                                    "incomingToday": stats["incomingToday"],
                                    "agentMsgToday": stats["agentMsgToday"],
                                    "timeSpentSec": stats["timeSpentSec"],
                                    "missedToday": stats["missedToday"],
                                    "byAgent": json.dumps(stats["byAgent"], ensure_ascii=False),
                                    "recordedAt": int(time.time()),
                                }
                            }
                        )
                        cursor = add_days_str(cursor, 1)
                        count += 1

                    if count > 0:
                        logger.info(f"History ({messenger}): archived {count} days")

                except Exception as e:
                    logger.error(f"Archive error ({messenger}): {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in daily archive catchup: {e}", exc_info=True)

        await asyncio.sleep(900)  # Run every 15 min (keeps today's snapshot fresh)
