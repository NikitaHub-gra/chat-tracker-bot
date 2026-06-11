"""MAX webhook handler — processes incoming MAX updates."""
import logging
import time

from src.database.db import db
from src.services.settings_service import (
    get_settings, get_phrases, is_team_member,
)
from src.services.text_utils import (
    is_positive, is_negative, is_pending_reply, is_no_reply,
)
from src.api.dashboard.sse import max_sse

logger = logging.getLogger(__name__)


def _now() -> int:
    return int(time.time())


async def handle_max_update(update: dict):
    """Process a single MAX webhook update."""
    if not update or update.get("update_type") != "message_created":
        return

    msg = update.get("message")
    if not msg:
        return

    sender = msg.get("sender") or {}
    from_id = sender.get("user_id")
    from_name = sender.get("name") or sender.get("username") or str(from_id or "")
    chat_type = (msg.get("recipient") or {}).get("chat_type")  # dialog | chat | channel
    chat_id = (msg.get("recipient") or {}).get("chat_id")
    text = (msg.get("body") or {}).get("text") or (msg.get("body") or {}).get("caption") or ""
    sent_at = msg.get("timestamp") or _now()
    if sent_at > 1e12:
        sent_at = sent_at // 1000  # ms -> sec

    if chat_id is None or from_id is None:
        return

    chat_id_str = str(chat_id)
    from_id_int = int(from_id)

    # ── Private dialog ───────────────────────────────────────
    if chat_type == "dialog":
        await _handle_max_private(chat_id_str, from_id_int, from_name, text, sent_at, msg)
        return

    # ── Group / Channel ──────────────────────────────────────
    if chat_type not in ("chat", "channel"):
        return

    await _handle_max_group(chat_id_str, from_id_int, from_name, text, sent_at, msg)


async def _handle_max_private(chat_id: str, from_id: int, from_name: str,
                               text: str, sent_at: int, msg: dict):
    """Handle private dialog message in MAX."""
    s = await get_settings()
    no_reply_phrases = await get_phrases("noReplyPhrases")
    pending_phrases = await get_phrases("pendingPhrases")
    negative_kw = await get_phrases("negativeKeywords")
    positive_kw = await get_phrases("positiveKeywords")

    sender = msg.get("sender") or {}
    client_name = sender.get("name") or sender.get("username") or str(from_id)
    client_username = sender.get("username")
    has_photo = bool((msg.get("body") or {}).get("attachments"))
    is_no_reply_msg = is_no_reply(text, no_reply_phrases)

    try:
        conv = await db.conversation.find_first(
            where={"messenger": "max", "chatId": chat_id}
        )

        if conv:
            update_data = {
                "clientName": client_name,
                "clientUsername": client_username,
                "lastClientMsgAt": sent_at,
                "lastClientMsgText": text[:300],
                "msgCount": conv.msgCount + 1,
                "isPending": False,
                "pendingAt": None,
            }
            if not is_no_reply_msg:
                update_data["status"] = "waiting"
            if not conv.isNegative and is_negative(text, negative_kw):
                update_data["isNegative"] = True
                update_data["isPositive"] = False
            elif not conv.isNegative and not conv.isPositive and is_positive(text, positive_kw):
                update_data["isPositive"] = True
            await db.conversation.update(where={"id": conv.id}, data=update_data)
        else:
            await db.conversation.create(data={
                "messenger": "max", "chatId": chat_id, "source": "private",
                "clientName": client_name,
                "clientUsername": client_username,
                "lastClientMsgAt": sent_at,
                "lastClientMsgText": text[:300],
                "status": "waiting" if not is_no_reply_msg else "answered",
                "isNegative": is_negative(text, negative_kw),
                "isPositive": is_positive(text, positive_kw),
                "createdAt": sent_at, "msgCount": 1,
            })

        await db.chatmessage.create(data={
            "messenger": "max",
            "msgId": str((msg.get("body") or {}).get("mid") or ""),
            "chatId": chat_id, "direction": "in",
            "text": text[:500], "sentAt": sent_at, "hasPhoto": has_photo,
        })
        await max_sse.broadcast("max-update", {"source": "private", "chatId": chat_id, "direction": "in"})

    except Exception as e:
        logger.error(f"MAX private handler error: {e}", exc_info=True)


async def _handle_max_group(chat_id: str, from_id: int, from_name: str,
                             text: str, sent_at: int, msg: dict):
    """Handle group/channel message in MAX."""
    is_team = await is_team_member("max", from_id)
    no_reply_phrases = await get_phrases("noReplyPhrases")
    pending_phrases = await get_phrases("pendingPhrases")

    try:
        # Register group
        gi = await db.groupinfo.find_first(
            where={"messenger": "max", "groupId": chat_id}
        )
        title = (msg.get("recipient") or {}).get("title") or chat_id
        if not gi:
            await db.groupinfo.create(data={
                "messenger": "max", "groupId": chat_id,
                "title": str(title), "createdAt": sent_at,
            })
        elif gi.title != str(title):
            await db.groupinfo.update(where={"id": gi.id}, data={"title": str(title)})

        mid = str((msg.get("body") or {}).get("mid") or "")

        if is_team:
            pending = is_pending_reply(text, pending_phrases)

            if not pending:
                # Mark unanswered client messages as answered
                unanswered = await db.groupmessage.find_many(
                    where={
                        "messenger": "max", "groupId": chat_id,
                        "answered": False, "isTeam": False,
                        "sentAt": {"lte": sent_at},
                    }
                )
                for m in unanswered:
                    await db.groupmessage.update(
                        where={"id": m.id},
                        data={"answered": True, "answeredAt": sent_at}
                    )

            await db.groupmessage.create(data={
                "messenger": "max", "groupId": chat_id,
                "msgId": mid, "fromId": from_id, "fromName": from_name,
                "text": text[:300], "sentAt": sent_at,
                "answered": True, "answeredAt": sent_at,
                "isTeam": True, "isPendingReply": pending,
            })
        else:
            no_reply = is_no_reply(text, no_reply_phrases)
            await db.groupmessage.create(data={
                "messenger": "max", "groupId": chat_id,
                "msgId": mid, "fromId": from_id, "fromName": from_name,
                "text": text[:300], "sentAt": sent_at,
                "answered": no_reply,
                "answeredAt": sent_at if no_reply else None,
                "isTeam": False,
            })

        await max_sse.broadcast("max-update", {"source": "group", "chatId": chat_id, "isTeam": is_team})

    except Exception as e:
        logger.error(f"MAX group handler error: {e}", exc_info=True)
