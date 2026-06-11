"""Telegram Dashboard API routes — ported from TG_Dashboard/server.js."""
import json
import math
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from src.database.db import db
from src.services.settings_service import (
    get_settings, update_phrases, update_settings_field,
    get_tg_team_ids, invalidate_cache,
)
from src.api.dashboard.sse import tg_sse

router = APIRouter()

MISSED_THRESHOLD = 15 * 60  # 15 min
HISTORY_SLICE_DAYS = 2
HISTORY_SLICE_MIN = 60
HISTORY_SLICE_MAX = 400


# ── Helpers ──────────────────────────────────────────────────

def _now() -> int:
    return int(time.time())


def _day_ago() -> int:
    return _now() - 86400


def _week_ago() -> int:
    return _now() - 7 * 86400


async def _record_missed_if_needed(conv, answered_at: int | None = None):
    if conv.status != "waiting" or not conv.lastClientMsgAt:
        return
    waited = (answered_at or _now()) - conv.lastClientMsgAt
    if waited >= MISSED_THRESHOLD:
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


# ── GET /chats — queue + all conversations ───────────────────

@router.get("/chats")
async def get_chats():
    now = _now()
    settings_row = await get_settings()

    # Snooze lookup
    snoozes = await db.notifsnooze.find_many()
    snooze_map = {s.chatId: s.snoozedUntil for s in snoozes}

    # Private conversations
    private_convs = await db.conversation.find_many(
        where={"messenger": "telegram", "source": "private"}
    )
    private_chats = []
    for c in private_convs:
        waiting = (c.status == "waiting" or c.isPending) and c.lastClientMsgAt
        private_chats.append({
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
            "hasControl": c.hasControl,
            "hasViolation": c.hasViolation,
            "msgCount": c.msgCount,
            "createdAt": c.createdAt,
            "source": "private",
            "sourceName": "Личные сообщения",
            "waitSeconds": (now - c.lastClientMsgAt) if waiting and c.lastClientMsgAt else None,
            "snoozedUntil": snooze_map.get(c.chatId),
        })

    # Group: group unanswered messages by (groupId, fromId)
    unanswered = await db.groupmessage.find_many(
        where={"messenger": "telegram", "answered": False, "isTeam": False}
    )
    group_map: dict[str, dict] = {}
    for m in unanswered:
        key = f"{m.groupId}__{m.fromId}"
        if key not in group_map:
            group_map[key] = {
                "groupChatId": m.groupId,
                "fromId": m.fromId,
                "fromName": m.fromName,
                "firstSentAt": m.sentAt,
                "latestText": m.text,
                "latestSentAt": m.sentAt,
                "msgCount": 1,
            }
        else:
            g = group_map[key]
            g["latestText"] = m.text
            g["latestSentAt"] = m.sentAt
            g["msgCount"] += 1

    # Get group titles
    group_ids = list({m.groupId for m in unanswered})
    group_infos = await db.groupinfo.find_many(
        where={"messenger": "telegram", "groupId": {"in": group_ids}} if group_ids else {"messenger": "telegram", "groupId": ""}
    ) if group_ids else []
    gi_map = {g.groupId: g.title for g in group_infos}

    # Check pending state for groups (from BotSettings — stored as JSON in a separate field or in-memory)
    # For simplicity, we check isPending from group conversations
    group_chats = []
    for g in group_map.values():
        chat_id = f"grp_{g['groupChatId']}_{g['fromId']}"
        group_chats.append({
            "chatId": chat_id,
            "clientName": g["fromName"],
            "clientUsername": None,
            "lastClientMsgAt": g["latestSentAt"],
            "lastClientMsgText": g["latestText"],
            "lastAgentMsgAt": None,
            "lastAgentName": None,
            "status": "waiting",
            "isPending": False,
            "msgCount": g["msgCount"],
            "createdAt": g["firstSentAt"],
            "source": "group",
            "sourceName": gi_map.get(g["groupChatId"], f"Группа {g['groupChatId']}"),
            "waitSeconds": now - g["firstSentAt"],
            "snoozedUntil": snooze_map.get(chat_id),
        })

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

    # Private conversations
    private_convs = await db.conversation.find_many(
        where={"messenger": "telegram", "source": "private"}
    )
    private_waiting = sum(1 for c in private_convs if c.status == "waiting")

    # Group waiting: unique senders with unanswered messages
    unanswered = await db.groupmessage.find_many(
        where={"messenger": "telegram", "answered": False, "isTeam": False}
    )
    group_waiting = len({(m.groupId, m.fromId) for m in unanswered})
    waiting = private_waiting + group_waiting

    # Today chats
    day_ago_convs = sum(1 for c in private_convs if c.createdAt >= day_ago_ts)
    day_ago_groups = len({
        (m.groupId, m.fromId) for m in unanswered if m.sentAt >= day_ago_ts
    })
    # Also count answered group messages from today
    answered_today = await db.groupmessage.find_many(
        where={"messenger": "telegram", "isTeam": False, "sentAt": {"gte": day_ago_ts}}
    )
    today_chats = day_ago_convs + len({(m.groupId, m.fromId) for m in answered_today})

    # Messages today
    msgs_today = await db.chatmessage.find_many(
        where={"messenger": "telegram", "sentAt": {"gte": day_ago_ts}}
    )
    incoming_today = sum(1 for m in msgs_today if m.direction == "in")
    agent_msg_today = sum(1 for m in msgs_today if m.direction == "out")

    # Group messages today
    grp_msgs_today = await db.groupmessage.find_many(
        where={"messenger": "telegram", "sentAt": {"gte": day_ago_ts}}
    )
    for m in grp_msgs_today:
        if m.isTeam:
            agent_msg_today += 1
        else:
            incoming_today += 1

    by_agent: dict[str, int] = {}
    for m in msgs_today:
        if m.direction == "out" and m.agentName:
            by_agent[m.agentName] = by_agent.get(m.agentName, 0) + 1
    for m in grp_msgs_today:
        if m.isTeam and m.fromName:
            by_agent[m.fromName] = by_agent.get(m.fromName, 0) + 1

    time_spent_sec = agent_msg_today * 30

    # Avg response time: answered convs where agent replied after client, today
    response_times = []
    for c in private_convs:
        if (c.status == "answered" and c.lastClientMsgAt and c.lastAgentMsgAt
                and c.lastAgentMsgAt > c.lastClientMsgAt
                and c.lastAgentMsgAt >= day_ago_ts):
            response_times.append(c.lastAgentMsgAt - c.lastClientMsgAt)

    # Group: unanswered->answered transitions today
    answered_grp = await db.groupmessage.find_many(
        where={
            "messenger": "telegram",
            "answered": True,
            "isTeam": False,
            "answeredAt": {"gte": day_ago_ts},
        }
    )
    for m in answered_grp:
        if m.answeredAt and m.answeredAt > m.sentAt:
            response_times.append(m.answeredAt - m.sentAt)

    avg_response_sec = (
        round(sum(response_times) / len(response_times))
        if response_times else None
    )

    missed_today = await db.missedevent.count(
        where={"messenger": "telegram", "missedAt": {"gte": day_ago_ts}}
    )
    pending_missed = await db.missedevent.count(
        where={"messenger": "telegram", "missedAt": {"gte": day_ago_ts}, "source": "pending_expired"}
    )
    pending_now = sum(1 for c in private_convs if c.isPending)

    return {
        "success": True,
        "stats": {
            "waiting": waiting,
            "answered": len(private_convs) - private_waiting,
            "avgResponseSec": avg_response_sec,
            "todayChats": today_chats,
            "byAgent": by_agent,
            "incomingToday": incoming_today,
            "agentMsgToday": agent_msg_today,
            "timeSpentSec": time_spent_sec,
            "missedToday": missed_today - pending_missed,
            "pendingNow": pending_now,
            "pendingMissedToday": pending_missed,
        },
    }


# ── GET /chat-messages/{chatId} ─────────────────────────────

@router.get("/chat-messages/{chat_id}")
async def get_chat_messages(chat_id: str):
    cutoff = _now() - HISTORY_SLICE_DAYS * 86400

    if chat_id.startswith("grp_"):
        parts = chat_id[4:]
        last_under = parts.rfind("_")
        group_id = parts[:last_under]
        from_id = int(parts[last_under + 1:])

        client_msgs = await db.groupmessage.find_many(
            where={"messenger": "telegram", "groupId": group_id, "fromId": from_id, "isTeam": False}
        )
        team_msgs = await db.groupmessage.find_many(
            where={"messenger": "telegram", "groupId": group_id, "isTeam": True}
        )
        all_msgs = sorted(client_msgs + team_msgs, key=lambda m: m.sentAt)
        # Filter by cutoff
        filtered = [m for m in all_msgs if m.sentAt >= cutoff][-HISTORY_SLICE_MAX:]
        if len(filtered) < HISTORY_SLICE_MIN:
            filtered = all_msgs[-HISTORY_SLICE_MIN:]

        messages = [{
            "direction": "out" if m.isTeam else "in",
            "text": m.text or "",
            "sentAt": m.sentAt,
            "agentName": m.fromName if m.isTeam else None,
            "hasPhoto": False,
        } for m in filtered]

        gi = await db.groupinfo.find_first(
            where={"messenger": "telegram", "groupId": group_id}
        )
        group_title = gi.title if gi else f"Группа {group_id}"
        from_name = client_msgs[0].fromName if client_msgs else ""

        return {
            "success": True, "messages": messages,
            "clientName": from_name, "clientUsername": None,
            "groupTitle": group_title,
        }

    # Private chat
    msgs = await db.chatmessage.find_many(
        where={"messenger": "telegram", "chatId": chat_id},
        order={"sentAt": "asc"}
    )
    filtered = [m for m in msgs if m.sentAt >= cutoff][-HISTORY_SLICE_MAX:]
    if len(filtered) < HISTORY_SLICE_MIN:
        filtered = msgs[-HISTORY_SLICE_MIN:]

    messages = [{
        "direction": m.direction,
        "text": m.text or "",
        "sentAt": m.sentAt,
        "agentName": m.agentName,
        "hasPhoto": m.hasPhoto,
    } for m in filtered]

    conv = await db.conversation.find_first(
        where={"messenger": "telegram", "chatId": chat_id}
    )

    return {
        "success": True, "messages": messages,
        "clientName": conv.clientName if conv else "",
        "clientUsername": conv.clientUsername if conv else None,
    }


# ── POST /dismiss/{chatId} ──────────────────────────────────

@router.post("/dismiss/{chat_id}")
async def dismiss_chat(chat_id: str):
    now = _now()

    if chat_id.startswith("grp_"):
        parts = chat_id[4:]
        last_under = parts.rfind("_")
        group_id = parts[:last_under]
        from_id = int(parts[last_under + 1:])

        msgs = await db.groupmessage.find_many(
            where={"messenger": "telegram", "groupId": group_id, "fromId": from_id, "answered": False}
        )
        for m in msgs:
            await db.groupmessage.update(
                where={"id": m.id},
                data={"answered": True, "answeredAt": now}
            )
        if msgs:
            await tg_sse.broadcast("update", {"source": "dismiss", "chatId": chat_id})
    else:
        conv = await db.conversation.find_first(
            where={"messenger": "telegram", "chatId": chat_id}
        )
        if conv:
            await _record_missed_if_needed(conv, now)
            await db.conversation.update(
                where={"id": conv.id},
                data={"status": "answered", "lastAgentMsgAt": now}
            )
            await tg_sse.broadcast("update", {"source": "dismiss", "chatId": chat_id})

    return {"success": True}


# ── POST /snooze/{chatId} ───────────────────────────────────

@router.post("/snooze/{chat_id}")
async def snooze_chat(chat_id: str, body: dict = None):
    """Mute notifications for this chat for 1 hour (saved to DB)."""
    hours = 1
    if body and body.get("hours"):
        try:
            hours = max(1, int(body["hours"]))
        except (ValueError, TypeError):
            pass
    snooze_until = _now() + hours * 3600
    await db.notifsnooze.upsert(
        where={"chatId": chat_id},
        data={
            "create": {"chatId": chat_id, "snoozedUntil": snooze_until},
            "update": {"snoozedUntil": snooze_until},
        },
    )
    return {"success": True, "snoozedUntil": snooze_until}


@router.delete("/snooze/{chat_id}")
async def unsnooze_chat(chat_id: str):
    """Remove snooze for this chat."""
    try:
        await db.notifsnooze.delete(where={"chatId": chat_id})
    except Exception:
        pass
    return {"success": True}


# ── POST /resolve ────────────────────────────────────────────

@router.post("/resolve")
async def resolve_task(body: dict):
    chat_id = body.get("chatId")
    if not chat_id:
        return {"success": False, "error": "chatId required"}

    now = _now()
    conv = await db.conversation.find_first(
        where={"messenger": "telegram", "chatId": str(chat_id)}
    )
    started_at = conv.createdAt if conv else now
    time_spent = body.get("timeSpentSec") or (now - started_at)

    task = await db.resolvedtask.create(data={
        "messenger": "telegram",
        "chatId": str(chat_id),
        "clientName": conv.clientName if conv else "",
        "taskType": body.get("taskType", "Другое"),
        "description": (body.get("description") or "")[:500],
        "objectId": body.get("objectId"),
        "planfixTaskId": body.get("planfixTaskId"),
        "planfixTaskUrl": body.get("planfixTaskUrl"),
        "timeSpentSec": time_spent,
        "resolvedAt": now,
        "isNegative": conv.isNegative if conv else False,
    })

    if conv:
        await _record_missed_if_needed(conv, now)
        update_data = {"status": "answered", "lastAgentMsgAt": now}
        await db.conversation.update(where={"id": conv.id}, data=update_data)

    await tg_sse.broadcast("update", {"source": "resolve", "chatId": str(chat_id)})
    return {"success": True, "task": {"id": task.id}}


# ── GET /resolved-tasks ──────────────────────────────────────

@router.get("/resolved-tasks")
async def get_resolved_tasks():
    week_ago_ts = _week_ago()
    tasks = await db.resolvedtask.find_many(
        where={"messenger": "telegram", "resolvedAt": {"gte": week_ago_ts}}
    )
    total = await db.resolvedtask.count(where={"messenger": "telegram"})
    return {
        "success": True,
        "tasks": [{
            "id": t.id, "chatId": t.chatId, "clientName": t.clientName,
            "taskType": t.taskType, "description": t.description,
            "timeSpentSec": t.timeSpentSec, "resolvedAt": t.resolvedAt,
            "isNegative": t.isNegative, "planfixTaskId": t.planfixTaskId,
            "planfixTaskUrl": t.planfixTaskUrl,
        } for t in tasks],
        "total": total,
    }


# ── Keyword / Phrase CRUD ────────────────────────────────────

@router.get("/positive-keywords")
async def get_positive_keywords():
    s = await get_settings()
    kw = json.loads(s.positiveKeywords) if s else []
    return {"success": True, "keywords": kw}

@router.post("/positive-keywords")
async def set_positive_keywords(body: dict):
    kw = body.get("keywords", [])
    await update_phrases("positiveKeywords", [k.strip() for k in kw if isinstance(k, str) and k.strip()])
    s = await get_settings()
    return {"success": True, "keywords": json.loads(s.positiveKeywords) if s else []}

@router.get("/negative-keywords")
async def get_negative_keywords():
    s = await get_settings()
    kw = json.loads(s.negativeKeywords) if s else []
    return {"success": True, "keywords": kw}

@router.post("/negative-keywords")
async def set_negative_keywords(body: dict):
    kw = body.get("keywords", [])
    await update_phrases("negativeKeywords", [k.strip() for k in kw if isinstance(k, str) and k.strip()])
    s = await get_settings()
    return {"success": True, "keywords": json.loads(s.negativeKeywords) if s else []}

@router.get("/no-reply-phrases")
async def get_no_reply_phrases():
    s = await get_settings()
    p = json.loads(s.noReplyPhrases) if s else []
    return {"success": True, "phrases": p}

@router.post("/no-reply-phrases")
async def set_no_reply_phrases(body: dict):
    p = body.get("phrases", [])
    await update_phrases("noReplyPhrases", [x.strip() for x in p if isinstance(x, str) and x.strip()])
    s = await get_settings()
    return {"success": True, "phrases": json.loads(s.noReplyPhrases) if s else []}

@router.get("/pending-phrases")
async def get_pending_phrases():
    s = await get_settings()
    p = json.loads(s.pendingPhrases) if s else []
    return {"success": True, "phrases": p}

@router.post("/pending-phrases")
async def set_pending_phrases(body: dict):
    p = body.get("phrases", [])
    await update_phrases("pendingPhrases", [x.strip() for x in p if isinstance(x, str) and x.strip()])
    s = await get_settings()
    return {"success": True, "phrases": json.loads(s.pendingPhrases) if s else []}


# ── Clear sentiment flags ────────────────────────────────────

@router.post("/clear-positive/{chat_id}")
async def clear_positive(chat_id: str):
    conv = await db.conversation.find_first(where={"messenger": "telegram", "chatId": chat_id})
    if conv:
        await db.conversation.update(where={"id": conv.id}, data={"isPositive": False})
        await tg_sse.broadcast("update", {"source": "clear-positive"})
    return {"success": True}

@router.post("/clear-negative/{chat_id}")
async def clear_negative(chat_id: str):
    conv = await db.conversation.find_first(where={"messenger": "telegram", "chatId": chat_id})
    if conv:
        await db.conversation.update(where={"id": conv.id}, data={"isNegative": False})
        await tg_sse.broadcast("update", {"source": "clear-negative"})
    return {"success": True}


# ── Controls ─────────────────────────────────────────────────

@router.post("/control")
async def create_control(body: dict):
    chat_id = body.get("chatId")
    action = body.get("action")
    if not chat_id or not action:
        return {"success": False, "error": "chatId и action обязательны"}

    now = _now()
    ctrl_id = f"ctrl_{chat_id}_{now}"
    item = await db.control.create(data={
        "id": ctrl_id,
        "messenger": "telegram",
        "chatId": str(chat_id),
        "clientName": body.get("clientName", ""),
        "action": action.strip(),
        "responsible": (body.get("responsible") or "").strip(),
        "deadline": body.get("deadline"),
        "messageText": (body.get("messageText") or "")[:500],
        "createdAt": now,
    })
    # Mark conversation
    conv = await db.conversation.find_first(where={"messenger": "telegram", "chatId": str(chat_id)})
    if conv:
        await db.conversation.update(where={"id": conv.id}, data={"hasControl": True})

    await tg_sse.broadcast("update", {"source": "control", "chatId": str(chat_id)})
    return {"success": True, "control": {"id": item.id}}

@router.get("/controls")
async def get_controls():
    controls = await db.control.find_many(
        where={"messenger": "telegram"},
        order={"createdAt": "desc"}
    )
    active = sum(1 for c in controls if not c.done)
    return {
        "success": True,
        "controls": [{
            "id": c.id, "chatId": c.chatId, "clientName": c.clientName,
            "action": c.action, "responsible": c.responsible,
            "deadline": c.deadline, "messageText": c.messageText,
            "done": c.done, "doneAt": c.doneAt, "createdAt": c.createdAt,
        } for c in controls],
        "activeCount": active,
    }

@router.patch("/control/{ctrl_id}/done")
async def toggle_control_done(ctrl_id: str):
    ctrl = await db.control.find_unique(where={"id": ctrl_id})
    if not ctrl:
        return {"success": False}
    new_done = not ctrl.done
    await db.control.update(
        where={"id": ctrl_id},
        data={"done": new_done, "doneAt": _now() if new_done else None}
    )
    return {"success": True, "done": new_done}

@router.delete("/control/{ctrl_id}")
async def delete_control(ctrl_id: str):
    try:
        await db.control.delete(where={"id": ctrl_id})
    except Exception:
        pass
    return {"success": True}


# ── Violations ───────────────────────────────────────────────

@router.post("/violation")
async def create_violation(body: dict):
    chat_id = body.get("chatId")
    employee = body.get("employeeName")
    if not chat_id or not employee:
        return {"success": False, "error": "chatId и employeeName обязательны"}

    now = _now()
    vid = f"{chat_id}_{now}"
    item = await db.violation.create(data={
        "id": vid,
        "messenger": "telegram",
        "chatId": str(chat_id),
        "clientName": body.get("clientName", ""),
        "employeeName": employee.strip(),
        "comment": (body.get("comment") or "").strip(),
        "messageText": (body.get("messageText") or "")[:500],
        "recordedAt": now,
    })
    conv = await db.conversation.find_first(where={"messenger": "telegram", "chatId": str(chat_id)})
    if conv:
        await db.conversation.update(where={"id": conv.id}, data={"hasViolation": True})

    await tg_sse.broadcast("update", {"source": "violation", "chatId": str(chat_id)})
    return {"success": True, "violation": {"id": item.id}}

@router.get("/violations")
async def get_violations():
    violations = await db.violation.find_many(
        where={"messenger": "telegram"},
        order={"recordedAt": "desc"}
    )
    return {
        "success": True,
        "violations": [{
            "id": v.id, "chatId": v.chatId, "clientName": v.clientName,
            "employeeName": v.employeeName, "comment": v.comment,
            "messageText": v.messageText, "recordedAt": v.recordedAt,
        } for v in violations],
    }

@router.delete("/violation/{vid}")
async def delete_violation(vid: str):
    try:
        await db.violation.delete(where={"id": vid})
    except Exception:
        pass
    return {"success": True}


# ── Missed Events ────────────────────────────────────────────

@router.get("/missed")
async def get_missed():
    day_ago_ts = _day_ago()
    events = await db.missedevent.find_many(
        where={"messenger": "telegram", "hidden": False},
        order={"missedAt": "desc"},
        take=200,
    )
    today_missed = sum(1 for e in events if e.missedAt >= day_ago_ts)
    total = await db.missedevent.count(where={"messenger": "telegram", "hidden": False})
    return {
        "success": True,
        "events": [{
            "id": e.id, "chatId": e.chatId, "clientName": e.clientName,
            "lastMsg": e.lastMsg, "waitedSeconds": e.waitedSeconds,
            "missedAt": e.missedAt, "source": e.source,
        } for e in events],
        "todayMissed": today_missed,
        "total": total,
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
    await tg_sse.broadcast("update", {"source": "missed-hide"})
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
    await tg_sse.broadcast("update", {"source": "missed-unhide"})
    return {"success": True}


@router.post("/group-message/{msg_id}/hide")
async def hide_group_message(msg_id: int):
    try:
        m = await db.groupmessage.find_unique(where={"id": msg_id})
    except Exception:
        m = None
    if not m:
        return {"success": False, "error": "not_found"}
    await db.groupmessage.update(
        where={"id": msg_id}, data={"hidden": True, "answered": True, "answeredAt": _now()}
    )
    await tg_sse.broadcast("update", {"source": "group-message-hide"})
    return {"success": True}


@router.get("/resolve-user")
async def resolve_user(id: int = Query(...)):
    s = await get_settings()
    token = s.tgBotToken if s else ""
    if not token:
        return {"success": False, "error": "no token"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getChat?chat_id={id}")
            chat = r.json()
        if not chat.get("ok"):
            return {"success": False, "error": chat.get("description", "not found")}
        result = chat["result"]
        name = " ".join(filter(None, [result.get("first_name"), result.get("last_name")]))
        return {"success": True, "name": name or None, "username": result.get("username") or None}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Weekly Report ────────────────────────────────────────────

@router.get("/report-week")
async def get_report_week():
    now = _now()
    week_ago_ts = _week_ago()

    # Day buckets
    days = {}
    for i in range(7):
        d = datetime.fromtimestamp(now - i * 86400, tz=timezone.utc)
        key = d.strftime("%Y-%m-%d")
        days[key] = {"date": key, "messages": 0, "agentMessages": 0, "newChats": 0, "missedChats": 0}

    # Messages
    msgs = await db.chatmessage.find_many(
        where={"messenger": "telegram", "sentAt": {"gte": week_ago_ts}}
    )
    for m in msgs:
        key = datetime.fromtimestamp(m.sentAt, tz=timezone.utc).strftime("%Y-%m-%d")
        if key in days:
            days[key]["messages"] += 1
            if m.direction == "out":
                days[key]["agentMessages"] += 1

    # New chats
    convs = await db.conversation.find_many(
        where={"messenger": "telegram", "source": "private", "createdAt": {"gte": week_ago_ts}}
    )
    for c in convs:
        key = datetime.fromtimestamp(c.createdAt, tz=timezone.utc).strftime("%Y-%m-%d")
        if key in days:
            days[key]["newChats"] += 1

    # Missed
    missed = await db.missedevent.find_many(
        where={"messenger": "telegram", "missedAt": {"gte": week_ago_ts}}
    )
    for e in missed:
        key = datetime.fromtimestamp(e.missedAt, tz=timezone.utc).strftime("%Y-%m-%d")
        if key in days:
            days[key]["missedChats"] += 1

    missed_week = [{
        "chatId": e.chatId, "clientName": e.clientName,
        "lastMsg": e.lastMsg, "waitedSeconds": e.waitedSeconds,
        "date": datetime.fromtimestamp(e.missedAt, tz=timezone.utc).strftime("%d.%m.%Y %H:%M"),
        "waited": f"{e.waitedSeconds // 60} мин {e.waitedSeconds % 60} сек",
    } for e in missed]

    resolved = await db.resolvedtask.find_many(
        where={"messenger": "telegram", "resolvedAt": {"gte": week_ago_ts}}
    )
    resolved_week = [{
        "id": t.id, "chatId": t.chatId, "clientName": t.clientName,
        "taskType": t.taskType,
        "date": datetime.fromtimestamp(t.resolvedAt, tz=timezone.utc).strftime("%d.%m.%Y %H:%M"),
        "timeSpent": f"{t.timeSpentSec // 60} мин",
    } for t in resolved]

    controls = await db.control.find_many(
        where={"messenger": "telegram", "createdAt": {"gte": week_ago_ts}}
    )
    controls_week = [{
        "id": c.id, "chatId": c.chatId, "action": c.action,
        "responsible": c.responsible,
        "date": datetime.fromtimestamp(c.createdAt, tz=timezone.utc).strftime("%d.%m.%Y %H:%M"),
        "doneStr": f"Done" if c.done else "Не выполнено",
    } for c in controls]

    violations = await db.violation.find_many(
        where={"messenger": "telegram", "recordedAt": {"gte": week_ago_ts}}
    )
    violations_week = [{
        "id": v.id, "chatId": v.chatId, "employeeName": v.employeeName,
        "comment": v.comment,
        "date": datetime.fromtimestamp(v.recordedAt, tz=timezone.utc).strftime("%d.%m.%Y %H:%M"),
    } for v in violations]

    return {
        "success": True,
        "days": list(days.values()),
        "missed": missed_week,
        "resolved": resolved_week,
        "violations": violations_week,
        "controls": controls_week,
    }


# ── Bot Config ───────────────────────────────────────────────

@router.get("/bot-config")
async def get_bot_config():
    s = await get_settings()
    token = s.tgBotToken if s else ""
    masked = token.split(":")[0] + ":***" if ":" in token else "***"
    username = None
    if token:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                info = r.json()
                username = info.get("result", {}).get("username")
        except Exception:
            pass
    return {"success": True, "masked": masked, "username": username, "ok": bool(username)}

@router.post("/bot-config")
async def set_bot_config(body: dict):
    token = body.get("token", "").strip()
    if not token or ":" not in token:
        return {"success": False, "error": "Неверный формат токена"}

    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        info = r.json()
    if not info.get("ok"):
        return {"success": False, "error": "Telegram отклонил токен"}

    s = await get_settings()
    old_token = s.tgBotToken if s else ""
    # Delete old webhook
    if old_token and old_token != token:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"https://api.telegram.org/bot{old_token}/deleteWebhook")
        except Exception:
            pass

    await update_settings_field("tgBotToken", token)

    # Reinitialize bot
    from src.bot.dispatcher import setup_bot
    await setup_bot(token)

    masked = token.split(":")[0] + ":***"
    return {
        "success": True, "masked": masked,
        "username": info.get("result", {}).get("username"),
    }


# ── Team IDs ─────────────────────────────────────────────────

@router.get("/team-ids")
async def get_team_ids():
    ids = await get_tg_team_ids()
    return {"success": True, "teamIds": ids}

@router.post("/team-ids")
async def set_team_ids(body: dict):
    ids = body.get("teamIds", [])
    clean = [int(x) for x in ids if isinstance(x, (int, float)) and x]
    await update_settings_field("tgTeamIds", json.dumps(clean))
    return {"success": True, "teamIds": clean}


# ── Set Owner ────────────────────────────────────────────────

@router.get("/set-owner")
async def set_owner(id: int = Query(...)):
    await update_settings_field("tgOwnerId", id)
    return {"success": True, "ownerId": id}


# ── Webhook Info ─────────────────────────────────────────────

@router.get("/webhook-info")
async def get_webhook_info():
    s = await get_settings()
    token = s.tgBotToken if s else ""
    if not token:
        return {"ok": False, "error": "no token"}
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
        return r.json()

@router.get("/setup-webhook")
async def setup_webhook(url: str = Query(...)):
    s = await get_settings()
    token = s.tgBotToken if s else ""
    if not token:
        return {"success": False, "error": "no token configured"}
    if not url:
        return {"success": False, "error": "url parameter required"}

    webhook_url = f"{url.rstrip('/')}/webhook/tg"
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={
                "url": webhook_url,
                "allowed_updates": [
                    "message", "business_message", "business_connection",
                    "message_reaction", "inline_query", "chosen_inline_result",
                ],
            }
        )
        result = r.json()
    return {"success": result.get("ok", False), "webhookUrl": webhook_url, "result": result}


# ── Rescan ───────────────────────────────────────────────────

@router.post("/rescan")
async def rescan():
    from src.services.text_utils import is_no_reply
    s = await get_settings()
    no_reply = json.loads(s.noReplyPhrases) if s else []

    waiting = await db.conversation.find_many(
        where={"messenger": "telegram", "status": "waiting"}
    )
    fixed = 0
    now = _now()
    for conv in waiting:
        if conv.lastClientMsgText and is_no_reply(conv.lastClientMsgText, no_reply):
            await db.conversation.update(
                where={"id": conv.id},
                data={"status": "answered"}
            )
            fixed += 1

    # Group messages
    unanswered_grp = await db.groupmessage.find_many(
        where={"messenger": "telegram", "answered": False, "isTeam": False}
    )
    for m in unanswered_grp:
        if is_no_reply(m.text, no_reply):
            await db.groupmessage.update(
                where={"id": m.id},
                data={"answered": True, "answeredAt": now}
            )
            fixed += 1

    if fixed > 0:
        await tg_sse.broadcast("update", {"source": "rescan"})
    return {"success": True, "fixed": fixed}


# ── Reset ────────────────────────────────────────────────────

@router.post("/reset")
async def reset_data():
    # Wipe conversations, messages, group messages, missed events, resolved tasks
    await db.conversation.delete_many(where={"messenger": "telegram"})
    await db.chatmessage.delete_many(where={"messenger": "telegram"})
    await db.groupmessage.delete_many(where={"messenger": "telegram"})
    await db.groupinfo.delete_many(where={"messenger": "telegram"})
    await db.missedevent.delete_many(where={"messenger": "telegram"})
    await db.resolvedtask.delete_many(where={"messenger": "telegram"})
    await tg_sse.broadcast("update", {"source": "reset"})
    return {"success": True}


# ═══════════════════════════════════════════════════════════════
#  ALERT SYSTEM SETTINGS
#  - reaction time (waitTimeoutMin / pendingTimeout / missedThreshold)
#  - alert chat binding (alertChatId)
#  - exceptions (IgnoredUser)
#  - alert recipients / responsibles (AlertUser)
# ═══════════════════════════════════════════════════════════════

# ── Reaction time + alert chat (read) ────────────────────────

@router.get("/alert-settings")
async def get_alert_settings():
    """Return reaction-time thresholds + currently bound alert chat."""
    s = await get_settings()
    alert_chat_id = s.alertChatId if s else ""
    alert_chat_title = None
    if alert_chat_id:
        gi = await db.groupinfo.find_first(
            where={"messenger": "telegram", "groupId": alert_chat_id}
        )
        alert_chat_title = gi.title if gi else None
    return {
        "success": True,
        "waitTimeoutMin": s.waitTimeoutMin if s else 15,
        "missedThreshold": s.missedThreshold if s else 900,
        "pendingTimeout": s.pendingTimeout if s else 1800,
        "alertChatId": alert_chat_id,
        "alertChatTitle": alert_chat_title,
    }


@router.post("/alert-settings")
async def set_alert_settings(body: dict):
    """Update reaction-time thresholds. Accepts any of waitTimeoutMin,
    missedThreshold, pendingTimeout (values in their natural units)."""
    updated = {}
    for field, lo in (("waitTimeoutMin", 1), ("missedThreshold", 30), ("pendingTimeout", 30)):
        if field in body and body[field] is not None:
            try:
                val = int(body[field])
            except (ValueError, TypeError):
                continue
            if val < lo:
                val = lo
            await update_settings_field(field, val)
            updated[field] = val
    return {"success": True, "updated": updated}


# ── Alert chat binding ───────────────────────────────────────

@router.get("/groups")
async def list_groups():
    """List known Telegram groups (for the alert-chat picker)."""
    groups = await db.groupinfo.find_many(
        where={"messenger": "telegram"}, order={"title": "asc"}
    )
    return {
        "success": True,
        "groups": [{"groupId": g.groupId, "title": g.title or g.groupId} for g in groups],
    }


@router.post("/alert-chat")
async def set_alert_chat(body: dict):
    """Bind (or change) the chat that receives overdue-ticket alerts."""
    chat_id = str(body.get("chatId") or "").strip()
    if not chat_id:
        return {"success": False, "error": "chatId required"}
    await update_settings_field("alertChatId", chat_id)
    # keep legacy SystemConfig in sync if present
    cfg = await db.systemconfig.find_first()
    if cfg:
        await db.systemconfig.update(where={"id": cfg.id}, data={"alertChatId": chat_id})
    gi = await db.groupinfo.find_first(
        where={"messenger": "telegram", "groupId": chat_id}
    )
    return {"success": True, "alertChatId": chat_id, "alertChatTitle": gi.title if gi else None}


@router.post("/alert-chat/clear")
async def clear_alert_chat():
    """Unbind the alert chat."""
    await update_settings_field("alertChatId", "")
    return {"success": True}


# ── Exceptions (ignored users) ───────────────────────────────

@router.get("/ignored-users")
async def get_ignored_users():
    users = await db.ignoreduser.find_many(order={"createdAt": "desc"})
    return {
        "success": True,
        "users": [
            {"id": u.id, "username": u.username, "createdAt": u.createdAt.isoformat() if u.createdAt else None}
            for u in users
        ],
    }


@router.post("/ignored-users")
async def add_ignored_user(body: dict):
    user_id = str(body.get("id") or body.get("telegramId") or "").strip()
    if not user_id or not user_id.lstrip("-").isdigit():
        return {"success": False, "error": "Укажите числовой Telegram ID"}
    username = (body.get("username") or "").strip() or None
    await db.ignoreduser.upsert(
        where={"id": user_id},
        data={
            "create": {"id": user_id, "username": username},
            "update": {"username": username},
        },
    )
    return {"success": True, "id": user_id, "username": username}


@router.delete("/ignored-users/{user_id}")
async def delete_ignored_user(user_id: str):
    try:
        await db.ignoreduser.delete(where={"id": user_id})
    except Exception:
        pass
    return {"success": True}


# ── Alert recipients (responsibles — alerts duplicated to DM) ──

@router.get("/alert-users")
async def get_alert_users():
    users = await db.alertuser.find_many(order={"createdAt": "desc"})
    return {
        "success": True,
        "users": [
            {"id": u.id, "telegramId": u.telegramId, "username": u.username, "name": u.name}
            for u in users
        ],
    }


@router.post("/alert-users")
async def add_alert_user(body: dict):
    tg_id = str(body.get("telegramId") or body.get("id") or "").strip()
    if not tg_id or not tg_id.lstrip("-").isdigit():
        return {"success": False, "error": "Укажите числовой Telegram ID"}
    username = (body.get("username") or "").strip() or None
    name = (body.get("name") or "").strip() or None
    rec = await db.alertuser.upsert(
        where={"telegramId": tg_id},
        data={
            "create": {"telegramId": tg_id, "username": username, "name": name},
            "update": {"username": username, "name": name},
        },
    )
    return {"success": True, "user": {"id": rec.id, "telegramId": rec.telegramId,
                                       "username": rec.username, "name": rec.name}}


@router.delete("/alert-users/{rec_id}")
async def delete_alert_user(rec_id: int):
    try:
        await db.alertuser.delete(where={"id": rec_id})
    except Exception:
        pass
    return {"success": True}


# ── SSE Stream ───────────────────────────────────────────────

@router.get("/events")
async def sse_events():
    q = tg_sse.connect()
    return StreamingResponse(
        tg_sse.stream(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
        background=None,
    )


# ── Ping ─────────────────────────────────────────────────────

@router.get("/ping")
async def ping():
    return {"ok": True}


# ── Group Queue ──────────────────────────────────────────────

@router.get("/group-queue")
async def get_group_queue():
    now = _now()
    s = await get_settings()
    msgs = await db.groupmessage.find_many(
        where={"messenger": "telegram", "answered": False},
        order={"sentAt": "asc"}
    )
    # Get group titles
    group_ids = list({m.groupId for m in msgs})
    gi_map = {}
    if group_ids:
        infos = await db.groupinfo.find_many(
            where={"messenger": "telegram", "groupId": {"in": group_ids}}
        )
        gi_map = {g.groupId: g.title for g in infos}

    waiting = [{
        "id": m.id, "chatId": m.groupId, "fromId": m.fromId,
        "fromName": m.fromName, "text": m.text, "sentAt": m.sentAt,
        "groupTitle": gi_map.get(m.groupId, str(m.groupId)),
        "waitSeconds": now - m.sentAt,
    } for m in msgs]
    waiting.sort(key=lambda x: -x["waitSeconds"])

    return {"success": True, "messages": waiting, "ownerId": s.tgOwnerId if s else None}
