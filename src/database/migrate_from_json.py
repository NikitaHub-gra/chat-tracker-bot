"""One-time migration script: imports data from TG_Dashboard JSON files into Prisma DB.

Usage:
    python -m src.database.migrate_from_json

Run from project root. The script reads:
  - TG_Dashboard/telegram_data.json
  - TG_Dashboard/max_data.json
and imports all data into the unified Prisma SQLite database.

Safe to run multiple times — uses upsert where possible and skips duplicates.
"""
import asyncio
import json
import os
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Paths
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TG_DATA = os.path.join(PROJECT_ROOT, "TG_Dashboard", "telegram_data.json")
MAX_DATA = os.path.join(PROJECT_ROOT, "TG_Dashboard", "max_data.json")

BATCH_SIZE = 100


def load_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        logger.warning(f"File not found: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {path}: {e}")
        return None


async def migrate_settings(db, tg_data: dict | None, max_data: dict | None):
    """Import settings/phrases/teamIds into BotSettings table."""
    from src.services.settings_service import invalidate_cache

    row = await db.botsettings.find_first()
    if not row:
        logger.info("BotSettings row missing — create defaults first (run main.py once).")
        return

    updates = {}

    if tg_data:
        if tg_data.get("botToken"):
            updates["tgBotToken"] = tg_data["botToken"]
        if tg_data.get("ownerId") is not None:
            updates["tgOwnerId"] = int(tg_data["ownerId"])
        if tg_data.get("teamIds"):
            updates["tgTeamIds"] = json.dumps(tg_data["teamIds"])
        if tg_data.get("noReplyPhrases"):
            updates["noReplyPhrases"] = json.dumps(tg_data["noReplyPhrases"], ensure_ascii=False)
        if tg_data.get("pendingPhrases"):
            updates["pendingPhrases"] = json.dumps(tg_data["pendingPhrases"], ensure_ascii=False)
        if tg_data.get("positiveKeywords"):
            updates["positiveKeywords"] = json.dumps(tg_data["positiveKeywords"], ensure_ascii=False)
        if tg_data.get("negativeKeywords"):
            updates["negativeKeywords"] = json.dumps(tg_data["negativeKeywords"], ensure_ascii=False)

    if max_data:
        if max_data.get("botToken"):
            updates["maxBotToken"] = max_data["botToken"]
        if max_data.get("teamIds"):
            updates["maxTeamIds"] = json.dumps(max_data["teamIds"])
        # If TG didn't have phrases, use MAX's (they share the same defaults)
        if not tg_data and max_data.get("noReplyPhrases"):
            updates["noReplyPhrases"] = json.dumps(max_data["noReplyPhrases"], ensure_ascii=False)
        if not tg_data and max_data.get("pendingPhrases"):
            updates["pendingPhrases"] = json.dumps(max_data["pendingPhrases"], ensure_ascii=False)
        if not tg_data and max_data.get("positiveKeywords"):
            updates["positiveKeywords"] = json.dumps(max_data["positiveKeywords"], ensure_ascii=False)
        if not tg_data and max_data.get("negativeKeywords"):
            updates["negativeKeywords"] = json.dumps(max_data["negativeKeywords"], ensure_ascii=False)

    if updates:
        await db.botsettings.update(where={"id": row.id}, data=updates)
        invalidate_cache()
        logger.info(f"BotSettings updated: {list(updates.keys())}")


async def migrate_conversations(db, data: dict, messenger: str):
    """Import conversations dict (keyed by chatId) into Conversation table."""
    convs = data.get("conversations", {})
    if not convs:
        logger.info(f"No conversations to migrate ({messenger})")
        return

    count = 0
    for chat_id, conv in convs.items():
        try:
            await db.conversation.upsert(
                where={
                    "messenger_chatId": {
                        "messenger": messenger,
                        "chatId": str(chat_id),
                    }
                },
                data={
                    "create": {
                        "messenger": messenger,
                        "chatId": str(chat_id),
                        "source": "private",
                        "clientName": conv.get("clientName", "") or "",
                        "clientUsername": conv.get("clientUsername"),
                        "lastClientMsgAt": conv.get("lastClientMsgAt"),
                        "lastClientMsgText": conv.get("lastClientMsgText"),
                        "lastAgentMsgAt": conv.get("lastAgentMsgAt"),
                        "lastAgentName": conv.get("lastAgentName"),
                        "status": conv.get("status", "waiting"),
                        "isPending": conv.get("isPending", False),
                        "pendingAt": conv.get("pendingAt"),
                        "isNegative": conv.get("isNegative", False),
                        "isPositive": conv.get("isPositive", False),
                        "hasControl": conv.get("hasControl", False),
                        "hasViolation": conv.get("hasViolation", False),
                        "msgCount": conv.get("msgCount", 0),
                        "createdAt": conv.get("createdAt", int(time.time())),
                    },
                    "update": {}  # Don't overwrite existing
                }
            )
            count += 1
        except Exception as e:
            logger.warning(f"Skip conv {chat_id} ({messenger}): {e}")

    logger.info(f"Migrated {count}/{len(convs)} conversations ({messenger})")


async def migrate_messages(db, data: dict, messenger: str):
    """Import messages list into ChatMessage table."""
    msgs = data.get("messages", [])
    if not msgs:
        logger.info(f"No messages to migrate ({messenger})")
        return

    count = 0
    # Process in batches
    for i in range(0, len(msgs), BATCH_SIZE):
        batch = msgs[i:i + BATCH_SIZE]
        for m in batch:
            try:
                await db.chatmessage.create(data={
                    "messenger": messenger,
                    "msgId": str(m.get("msgId", "")),
                    "chatId": str(m.get("chatId", "")),
                    "direction": m.get("direction", "in"),
                    "text": (m.get("text", "") or "")[:500],
                    "agentName": m.get("agentName"),
                    "sentAt": m.get("sentAt", int(time.time())),
                    "hasPhoto": m.get("hasPhoto", False),
                })
                count += 1
            except Exception as e:
                pass  # Skip duplicates silently
        logger.info(f"  messages batch {i // BATCH_SIZE + 1}: {count}/{len(msgs)}")

    logger.info(f"Migrated {count}/{len(msgs)} messages ({messenger})")


async def migrate_group_messages(db, data: dict, messenger: str):
    """Import groupMessages list into GroupMessage table."""
    gms = data.get("groupMessages", [])
    if not gms:
        logger.info(f"No group messages to migrate ({messenger})")
        return

    count = 0
    for i in range(0, len(gms), BATCH_SIZE):
        batch = gms[i:i + BATCH_SIZE]
        for m in batch:
            try:
                await db.groupmessage.create(data={
                    "messenger": messenger,
                    "groupId": str(m.get("chatId", m.get("groupId", ""))),
                    "msgId": str(m.get("msgId", "")),
                    "fromId": int(m.get("fromId", 0)),
                    "fromName": m.get("fromName", "") or "",
                    "text": (m.get("text", "") or "")[:500],
                    "sentAt": m.get("sentAt", int(time.time())),
                    "answered": m.get("answered", False),
                    "answeredAt": m.get("answeredAt"),
                    "isTeam": m.get("isTeam", False),
                    "isPendingReply": m.get("isPendingReply", False),
                })
                count += 1
            except Exception:
                pass

    logger.info(f"Migrated {count}/{len(gms)} group messages ({messenger})")


async def migrate_missed_events(db, data: dict, messenger: str):
    """Import missedEvents list into MissedEvent table."""
    events = data.get("missedEvents", [])
    if not events:
        logger.info(f"No missed events to migrate ({messenger})")
        return

    count = 0
    for e in events:
        try:
            await db.missedevent.create(data={
                "messenger": messenger,
                "chatId": str(e.get("chatId", "")),
                "clientName": e.get("clientName", "") or "",
                "clientUsername": e.get("clientUsername"),
                "lastMsg": (e.get("lastMsg", "") or "")[:300],
                "waitedSeconds": e.get("waitedSeconds", 0),
                "missedAt": e.get("missedAt", int(time.time())),
                "source": e.get("source", "timeout"),
            })
            count += 1
        except Exception:
            pass

    logger.info(f"Migrated {count}/{len(events)} missed events ({messenger})")


async def migrate_resolved_tasks(db, data: dict, messenger: str):
    """Import resolvedTasks list into ResolvedTask table."""
    tasks = data.get("resolvedTasks", [])
    if not tasks:
        return

    count = 0
    for t in tasks:
        try:
            await db.resolvedtask.create(data={
                "messenger": messenger,
                "chatId": str(t.get("chatId", "")),
                "clientName": t.get("clientName", "") or "",
                "taskType": t.get("taskType", "") or "",
                "description": (t.get("description", "") or "")[:1000],
                "objectId": str(t.get("objectId", "")) if t.get("objectId") else None,
                "planfixTaskId": str(t.get("planfixTaskId", "")) if t.get("planfixTaskId") else None,
                "planfixTaskUrl": t.get("planfixTaskUrl"),
                "timeSpentSec": t.get("timeSpentSec", 0),
                "resolvedAt": t.get("resolvedAt", int(time.time())),
                "isNegative": t.get("isNegative", False),
            })
            count += 1
        except Exception:
            pass

    logger.info(f"Migrated {count}/{len(tasks)} resolved tasks ({messenger})")


async def migrate_controls(db, data: dict, messenger: str):
    """Import controls list into Control table."""
    ctrls = data.get("controls", [])
    if not ctrls:
        return

    count = 0
    for c in ctrls:
        ctrl_id = c.get("id", f"ctrl_{c.get('chatId', '')}_{c.get('createdAt', int(time.time()))}")
        try:
            await db.control.upsert(
                where={"id": ctrl_id},
                data={
                    "create": {
                        "id": ctrl_id,
                        "messenger": messenger,
                        "chatId": str(c.get("chatId", "")),
                        "clientName": c.get("clientName", "") or "",
                        "action": c.get("action", "") or "",
                        "responsible": c.get("responsible", "") or "",
                        "deadline": c.get("deadline"),
                        "messageText": (c.get("messageText", "") or "")[:500],
                        "done": c.get("done", False),
                        "doneAt": c.get("doneAt"),
                        "createdAt": c.get("createdAt", int(time.time())),
                    },
                    "update": {}
                }
            )
            count += 1
        except Exception:
            pass

    logger.info(f"Migrated {count}/{len(ctrls)} controls ({messenger})")


async def migrate_violations(db, data: dict, messenger: str):
    """Import violations list into Violation table."""
    violations = data.get("violations", [])
    if not violations:
        return

    count = 0
    for v in violations:
        viol_id = v.get("id", f"{v.get('chatId', '')}_{v.get('recordedAt', int(time.time()))}")
        try:
            await db.violation.upsert(
                where={"id": viol_id},
                data={
                    "create": {
                        "id": viol_id,
                        "messenger": messenger,
                        "chatId": str(v.get("chatId", "")),
                        "clientName": v.get("clientName", "") or "",
                        "employeeName": v.get("employeeName", "") or "",
                        "comment": (v.get("comment", "") or "")[:500],
                        "messageText": (v.get("messageText", "") or "")[:500],
                        "recordedAt": v.get("recordedAt", int(time.time())),
                    },
                    "update": {}
                }
            )
            count += 1
        except Exception:
            pass

    logger.info(f"Migrated {count}/{len(violations)} violations ({messenger})")


async def migrate_groups(db, data: dict, messenger: str):
    """Import groups dict (keyed by groupId) into GroupInfo table."""
    groups = data.get("groups", {})
    if not groups:
        return

    count = 0
    for gid, info in groups.items():
        try:
            title = info.get("title", "") if isinstance(info, dict) else str(info)
            await db.groupinfo.upsert(
                where={
                    "messenger_groupId": {
                        "messenger": messenger,
                        "groupId": str(gid),
                    }
                },
                data={
                    "create": {
                        "messenger": messenger,
                        "groupId": str(gid),
                        "title": title,
                        "createdAt": int(time.time()),
                    },
                    "update": {}
                }
            )
            count += 1
        except Exception:
            pass

    logger.info(f"Migrated {count}/{len(groups)} groups ({messenger})")


async def main():
    logger.info("=" * 60)
    logger.info("JSON -> Prisma Migration")
    logger.info("=" * 60)

    tg_data = load_json(TG_DATA)
    max_data = load_json(MAX_DATA)

    if not tg_data and not max_data:
        logger.error("No JSON data files found. Nothing to migrate.")
        sys.exit(1)

    # Connect to DB
    from src.database.db import connect_db, disconnect_db, db
    await connect_db()
    logger.info("Database connected.")

    try:
        # 1. Settings
        logger.info("--- Migrating settings ---")
        await migrate_settings(db, tg_data, max_data)

        # 2. Telegram data
        if tg_data:
            logger.info("--- Migrating Telegram data ---")
            await migrate_conversations(db, tg_data, "telegram")
            await migrate_messages(db, tg_data, "telegram")
            await migrate_group_messages(db, tg_data, "telegram")
            await migrate_missed_events(db, tg_data, "telegram")
            await migrate_resolved_tasks(db, tg_data, "telegram")
            await migrate_controls(db, tg_data, "telegram")
            await migrate_violations(db, tg_data, "telegram")
            await migrate_groups(db, tg_data, "telegram")

        # 3. MAX data
        if max_data:
            logger.info("--- Migrating MAX data ---")
            await migrate_conversations(db, max_data, "max")
            await migrate_messages(db, max_data, "max")
            await migrate_group_messages(db, max_data, "max")
            await migrate_missed_events(db, max_data, "max")
            await migrate_resolved_tasks(db, max_data, "max")
            await migrate_controls(db, max_data, "max")
            await migrate_violations(db, max_data, "max")
            await migrate_groups(db, max_data, "max")

        logger.info("=" * 60)
        logger.info("Migration complete!")
        logger.info("=" * 60)

    finally:
        await disconnect_db()


if __name__ == "__main__":
    asyncio.run(main())
