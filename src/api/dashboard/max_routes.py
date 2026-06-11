"""MAX Dashboard API routes — ported from TG_Dashboard/server.js MAX module."""
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from src.database.db import db
from src.services.settings_service import (
    get_settings, get_max_team_ids,
    update_phrases, update_settings_field,
)
from src.api.dashboard.sse import max_sse

router = APIRouter()


def _now() -> int:
    return int(time.time())


def _day_ago() -> int:
    return _now() - 86400


# ── GET /chats ───────────────────────────────────────────────

@router.get("/chats")
async def get_chats():
    now = _now()

    # Private conversations
    private_convs = await db.conversation.find_many(
        where={"messenger": "max", "source": "private"}
    )
    private_chats = [{
        "chatId": c.chatId,
        "clientName": c.clientName,
        "clientUsername": c.clientUsername,
        "lastClientMsgAt": c.lastClientMsgAt,
        "lastClientMsgText": c.lastClientMsgText,
        "lastAgentMsgAt": c.lastAgentMsgAt,
        "lastAgentName": c.lastAgentName,
        "status": c.status,
        "isPending": c.isPending,
        "isNegative": c.isNegative,
        "isPositive": c.isPositive,
        "msgCount": c.msgCount,
        "createdAt": c.createdAt,
        "source": "private",
        "sourceName": "Личные сообщения",
        "messenger": "max",
        "waitSeconds": (now - c.lastClientMsgAt)
            if (c.status == "waiting" or c.isPending) and c.lastClientMsgAt else None,
    } for c in private_convs]

    # Group unanswered
    unanswered = await db.groupmessage.find_many(
        where={"messenger": "max", "answered": False, "isTeam": False}
    )
    group_map: dict[str, dict] = {}
    for m in unanswered:
        key = f"{m.groupId}__{m.fromId}"
        if key not in group_map:
            group_map[key] = {
                "groupChatId": m.groupId, "fromId": m.fromId,
                "fromName": m.fromName, "firstSentAt": m.sentAt,
                "latestText": m.text, "latestSentAt": m.sentAt,
                "msgCount": 1,
            }
        else:
            g = group_map[key]
            g["latestText"] = m.text
            g["latestSentAt"] = m.sentAt
            g["msgCount"] += 1

    group_ids = list({m.groupId for m in unanswered})
    gi_map = {}
    if group_ids:
        infos = await db.groupinfo.find_many(
            where={"messenger": "max", "groupId": {"in": group_ids}}
        )
        gi_map = {g.groupId: g.title for g in infos}

    group_chats = [{
        "chatId": f"max_{g['groupChatId']}_{g['fromId']}",
        "clientName": g["fromName"],
        "clientUsername": None,
        "lastClientMsgAt": g["latestSentAt"],
        "lastClientMsgText": g["latestText"],
        "lastAgentMsgAt": None, "lastAgentName": None,
        "status": "waiting", "isPending": False,
        "msgCount": g["msgCount"], "createdAt": g["firstSentAt"],
        "source": "group",
        "sourceName": gi_map.get(g["groupChatId"], f"Группа {g['groupChatId']}"),
        "messenger": "max",
        "waitSeconds": now - g["firstSentAt"],
    } for g in group_map.values()]

    chats = private_chats + group_chats
    chats.sort(key=lambda c: (
        0 if (c["status"] == "waiting" or c.get("isPending")) else 1,
        -(c.get("waitSeconds") or 0)
    ))
    return {"success": True, "chats": chats}


# ── GET /stats ───────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    now = _now()
    day_ago_ts = _day_ago()

    private_convs = await db.conversation.find_many(
        where={"messenger": "max", "source": "private"}
    )
    private_waiting = sum(1 for c in private_convs if c.status == "waiting")

    unanswered = await db.groupmessage.find_many(
        where={"messenger": "max", "answered": False, "isTeam": False}
    )
    group_waiting = len({(m.groupId, m.fromId) for m in unanswered})
    waiting = private_waiting + group_waiting

    today_private = sum(1 for c in private_convs if c.createdAt >= day_ago_ts)
    today_groups = len({(m.groupId, m.fromId) for m in unanswered if m.sentAt >= day_ago_ts})
    today_chats = today_private + today_groups

    by_agent: dict[str, int] = {}
    incoming_today = 0
    agent_msg_today = 0

    grp_msgs = await db.groupmessage.find_many(
        where={"messenger": "max", "sentAt": {"gte": day_ago_ts}}
    )
    for m in grp_msgs:
        if m.isTeam:
            agent_msg_today += 1
            if m.fromName:
                by_agent[m.fromName] = by_agent.get(m.fromName, 0) + 1
        else:
            incoming_today += 1

    msgs = await db.chatmessage.find_many(
        where={"messenger": "max", "sentAt": {"gte": day_ago_ts}}
    )
    for m in msgs:
        if m.direction == "in":
            incoming_today += 1
        else:
            agent_msg_today += 1
            if m.agentName:
                by_agent[m.agentName] = by_agent.get(m.agentName, 0) + 1

    # Response times
    response_times = []
    for m in grp_msgs:
        if not m.isTeam and m.answered and m.answeredAt and m.answeredAt >= day_ago_ts and m.answeredAt > m.sentAt:
            response_times.append(m.answeredAt - m.sentAt)
    for c in private_convs:
        if (c.status == "answered" and c.lastClientMsgAt and c.lastAgentMsgAt
                and c.lastAgentMsgAt > c.lastClientMsgAt and c.lastAgentMsgAt >= day_ago_ts):
            response_times.append(c.lastAgentMsgAt - c.lastClientMsgAt)

    avg_response_sec = round(sum(response_times) / len(response_times)) if response_times else None
    time_spent_sec = agent_msg_today * 30

    missed_today = await db.missedevent.count(
        where={"messenger": "max", "missedAt": {"gte": day_ago_ts}}
    )
    pending_missed = await db.missedevent.count(
        where={"messenger": "max", "missedAt": {"gte": day_ago_ts}, "source": "pending_expired"}
    )
    pending_now = sum(1 for c in private_convs if c.isPending)

    return {
        "success": True,
        "stats": {
            "waiting": waiting, "avgResponseSec": avg_response_sec,
            "todayChats": today_chats, "byAgent": by_agent,
            "incomingToday": incoming_today, "agentMsgToday": agent_msg_today,
            "timeSpentSec": time_spent_sec,
            "missedToday": missed_today - pending_missed,
            "pendingNow": pending_now, "pendingMissedToday": pending_missed,
        },
    }


# ── GET /chat-messages/{chatId} ──────────────────────────────

@router.get("/chat-messages/{chat_id}")
async def get_chat_messages(chat_id: str):
    cutoff = _now() - 2 * 86400

    if not chat_id.startswith("max_"):
        # Private dialog
        msgs = await db.chatmessage.find_many(
            where={"messenger": "max", "chatId": chat_id}, order={"sentAt": "asc"}
        )
        filtered = [m for m in msgs if m.sentAt >= cutoff][-400:]
        if len(filtered) < 60:
            filtered = msgs[-60:]

        messages = [{
            "direction": m.direction, "text": m.text or "",
            "sentAt": m.sentAt, "agentName": m.agentName, "hasPhoto": m.hasPhoto,
        } for m in filtered]

        conv = await db.conversation.find_first(where={"messenger": "max", "chatId": chat_id})
        return {
            "success": True, "messages": messages,
            "clientName": conv.clientName if conv else "",
            "clientUsername": conv.clientUsername if conv else None,
            "groupTitle": None, "messenger": "max",
        }

    # Group
    parts = chat_id[4:]
    last_under = parts.rfind("_")
    group_id = parts[:last_under]
    from_id = int(parts[last_under + 1:])

    client_msgs = await db.groupmessage.find_many(
        where={"messenger": "max", "groupId": group_id, "fromId": from_id, "isTeam": False}
    )
    team_msgs = await db.groupmessage.find_many(
        where={"messenger": "max", "groupId": group_id, "isTeam": True}
    )
    all_msgs = sorted(client_msgs + team_msgs, key=lambda m: m.sentAt)
    filtered = [m for m in all_msgs if m.sentAt >= cutoff][-400:]
    if len(filtered) < 60:
        filtered = all_msgs[-60:]

    messages = [{
        "direction": "out" if m.isTeam else "in",
        "text": m.text or "", "sentAt": m.sentAt,
        "agentName": m.fromName if m.isTeam else None, "hasPhoto": False,
    } for m in filtered]

    gi = await db.groupinfo.find_first(where={"messenger": "max", "groupId": group_id})
    from_name = client_msgs[0].fromName if client_msgs else ""
    return {
        "success": True, "messages": messages,
        "clientName": from_name, "clientUsername": None,
        "groupTitle": gi.title if gi else f"Группа {group_id}",
        "messenger": "max",
    }


# ── POST /dismiss/{chatId} ──────────────────────────────────

@router.post("/dismiss/{chat_id}")
async def dismiss_chat(chat_id: str):
    now = _now()

    if not chat_id.startswith("max_"):
        conv = await db.conversation.find_first(where={"messenger": "max", "chatId": chat_id})
        if conv:
            await db.conversation.update(
                where={"id": conv.id},
                data={"status": "answered", "lastAgentMsgAt": now}
            )
            await max_sse.broadcast("max-update", {"source": "dismiss", "chatId": chat_id})
        return {"success": True}

    parts = chat_id[4:]
    last_under = parts.rfind("_")
    group_id = parts[:last_under]
    from_id = int(parts[last_under + 1:])

    msgs = await db.groupmessage.find_many(
        where={"messenger": "max", "groupId": group_id, "fromId": from_id, "answered": False}
    )
    for m in msgs:
        await db.groupmessage.update(where={"id": m.id}, data={"answered": True, "answeredAt": now})
    if msgs:
        await max_sse.broadcast("max-update", {"source": "dismiss", "chatId": chat_id})
    return {"success": True}


# ── POST /reply/{chatId} (send reply via MAX bot) ───────────

@router.post("/reply/{chat_id}")
async def reply_chat(chat_id: str, body: dict):
    text = (body.get("text") or "").strip()
    agent_name = (body.get("agentName") or "").strip() or None
    if not text:
        return {"success": False, "error": "text required"}

    from src.services.max_api import max_api_call
    r = await max_api_call("POST", f"/messages?chat_id={chat_id}", {"text": text})
    if not r.get("ok"):
        return {"success": False, "error": r.get("message") or r.get("error") or "send failed"}

    now = _now()
    conv = await db.conversation.find_first(where={"messenger": "max", "chatId": chat_id})
    if conv:
        from src.services.text_utils import is_pending_reply
        from src.services.settings_service import get_phrases
        pending_phrases = await get_phrases("pendingPhrases")

        update_data = {"lastAgentMsgAt": now, "lastAgentName": agent_name}
        if is_pending_reply(text, pending_phrases):
            update_data["isPending"] = True
            update_data["pendingAt"] = now
        else:
            update_data["status"] = "answered"
            update_data["isPending"] = False
            update_data["pendingAt"] = None
        await db.conversation.update(where={"id": conv.id}, data=update_data)

    await db.chatmessage.create(data={
        "messenger": "max", "chatId": chat_id, "direction": "out",
        "text": text[:500], "agentName": agent_name, "sentAt": now,
    })
    await max_sse.broadcast("max-update", {"source": "private", "chatId": chat_id, "direction": "out"})
    return {"success": True}


# ── Missed ───────────────────────────────────────────────────

@router.get("/missed")
async def get_missed():
    day_ago_ts = _day_ago()
    events = await db.missedevent.find_many(
        where={"messenger": "max", "hidden": False}, order={"missedAt": "desc"}, take=200
    )
    today_missed = sum(1 for e in events if e.missedAt >= day_ago_ts)
    total = await db.missedevent.count(where={"messenger": "max", "hidden": False})
    return {
        "success": True,
        "events": [{"id": e.id, "chatId": e.chatId, "clientName": e.clientName,
                     "lastMsg": e.lastMsg, "waitedSeconds": e.waitedSeconds,
                     "missedAt": e.missedAt, "source": e.source} for e in events],
        "todayMissed": today_missed, "total": total,
    }


@router.post("/missed/{event_id}/hide")
async def hide_missed(event_id: int):
    try:
        ev = await db.missedevent.find_unique(where={"id": event_id})
    except Exception:
        ev = None
    if not ev:
        return {"success": False, "error": "not_found"}
    await db.missedevent.update(where={"id": event_id}, data={"hidden": True})
    await max_sse.broadcast("max-update", {"source": "missed-hide"})
    return {"success": True}


@router.post("/missed/{event_id}/unhide")
async def unhide_missed(event_id: int):
    try:
        ev = await db.missedevent.find_unique(where={"id": event_id})
    except Exception:
        ev = None
    if not ev:
        return {"success": False, "error": "not_found"}
    await db.missedevent.update(where={"id": event_id}, data={"hidden": False})
    await max_sse.broadcast("max-update", {"source": "missed-unhide"})
    return {"success": True}


# ── Bot Config ───────────────────────────────────────────────

@router.get("/bot-config")
async def get_bot_config():
    s = await get_settings()
    token = s.maxBotToken if s else ""
    if not token:
        return {"success": True, "configured": False}

    from src.services.max_api import max_api_call
    me = await max_api_call("GET", "/me", None)
    return {
        "success": True, "configured": True,
        "botToken": token[:6] + "..." + token[-4:] if len(token) > 10 else "***",
        "bot": {"name": me.get("name"), "username": me.get("username")} if me.get("user_id") else None,
        "valid": bool(me.get("user_id")),
    }

@router.post("/bot-config")
async def set_bot_config(body: dict):
    token = (body.get("botToken") or "").strip()
    if not token:
        return {"success": False, "error": "botToken required"}

    from src.services.max_api import max_api_call
    me = await max_api_call("GET", "/me", None)
    if not me.get("user_id"):
        return {"success": False, "error": me.get("message") or "Неверный токен"}

    await update_settings_field("maxBotToken", token)
    return {"success": True, "bot": {"name": me.get("name"), "username": me.get("username")}}


# ── Team IDs ─────────────────────────────────────────────────

@router.get("/team-ids")
async def get_team_ids():
    ids = await get_max_team_ids()
    return {"success": True, "teamIds": ids}

@router.post("/team-ids")
async def set_team_ids(body: dict):
    ids = body.get("teamIds", [])
    clean = [int(x) for x in ids if isinstance(x, (int, float)) and x]
    await update_settings_field("maxTeamIds", json.dumps(clean))
    return {"success": True, "teamIds": clean}


# ── Phrase lists (mirror TG) ────────────────────────────────

def _phrase_routes(key: str):
    @router.get(f"/{key}")
    async def get_phrases():
        s = await get_settings()
        raw = getattr(s, key, "[]") if s else "[]"
        return {"success": True, "phrases": json.loads(raw)}

    @router.post(f"/{key}")
    async def set_phrases(body: dict):
        phrases = body.get("phrases", [])
        await update_phrases(key, [p.strip() for p in phrases if isinstance(p, str) and p.strip()])
        s = await get_settings()
        raw = getattr(s, key, "[]") if s else "[]"
        return {"success": True, "phrases": json.loads(raw)}

_phrase_routes("noReplyPhrases")
_phrase_routes("pendingPhrases")
_phrase_routes("negativeKeywords")
_phrase_routes("positiveKeywords")


# ── Setup Webhook ────────────────────────────────────────────

@router.get("/setup-webhook")
async def setup_webhook(url: str = Query(...)):
    if not url:
        return {"success": False, "error": "url parameter required"}
    from src.services.max_api import setup_max_webhook
    result = await setup_max_webhook(url)
    return {"success": result.get("ok", False), "result": result}

@router.get("/webhook-info")
async def get_webhook_info():
    from src.services.max_api import max_api_call
    result = await max_api_call("GET", "/subscriptions", None)
    return result


# ── Reset ────────────────────────────────────────────────────

@router.post("/reset")
async def reset_data():
    await db.conversation.delete_many(where={"messenger": "max"})
    await db.chatmessage.delete_many(where={"messenger": "max"})
    await db.groupmessage.delete_many(where={"messenger": "max"})
    await db.groupinfo.delete_many(where={"messenger": "max"})
    await db.missedevent.delete_many(where={"messenger": "max"})
    await max_sse.broadcast("max-update", {"source": "reset"})
    return {"success": True}


# ── SSE Stream ───────────────────────────────────────────────

@router.get("/events")
async def sse_events():
    q = max_sse.connect()
    return StreamingResponse(
        max_sse.stream(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no", "Access-Control-Allow-Origin": "*",
        },
    )


# ── Ping ─────────────────────────────────────────────────────

@router.get("/ping")
async def ping():
    return {"ok": True}
