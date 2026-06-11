"""Service for reading/writing BotSettings from DB with lightweight caching."""
import json
import time
from typing import Optional

from src.database.db import db

_cache: dict = {}
_cache_ts: float = 0
CACHE_TTL = 5  # seconds


async def get_settings():
    """Get BotSettings row, cached for CACHE_TTL seconds."""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        return _cache

    row = await db.botsettings.find_first()
    if row:
        _cache = row
        _cache_ts = now
    return row


def invalidate_cache():
    global _cache, _cache_ts
    _cache = {}
    _cache_ts = 0


async def get_tg_token() -> str:
    s = await get_settings()
    return s.tgBotToken if s else ""


async def get_max_token() -> str:
    s = await get_settings()
    return s.maxBotToken if s else ""


async def get_tg_team_ids() -> list[int]:
    s = await get_settings()
    if not s:
        return []
    try:
        return json.loads(s.tgTeamIds)
    except (json.JSONDecodeError, TypeError):
        return []


async def get_max_team_ids() -> list[int]:
    s = await get_settings()
    if not s:
        return []
    try:
        return json.loads(s.maxTeamIds)
    except (json.JSONDecodeError, TypeError):
        return []


async def get_phrases(field: str) -> list[str]:
    """Get a phrase list by field name: noReplyPhrases, pendingPhrases, positiveKeywords, negativeKeywords."""
    s = await get_settings()
    if not s:
        return []
    raw = getattr(s, field, "[]")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


async def update_phrases(field: str, phrases: list[str]):
    """Update a phrase list field."""
    s = await db.botsettings.find_first()
    if s:
        await db.botsettings.update(
            where={"id": s.id},
            data={field: json.dumps(phrases, ensure_ascii=False)}
        )
    invalidate_cache()


async def update_settings_field(field: str, value):
    """Update any single BotSettings field."""
    s = await db.botsettings.find_first()
    if s:
        await db.botsettings.update(
            where={"id": s.id},
            data={field: value}
        )
    invalidate_cache()


async def is_team_member(messenger: str, user_id: int) -> bool:
    """Check if user is a team member (engineer DB OR configured team IDs)."""
    tg_id_str = str(user_id)

    # Check Engineer table (self-registered via /start)
    engineer = await db.engineer.find_unique(where={"telegramId": tg_id_str})
    if engineer:
        return True

    # Check configured team IDs
    if messenger == "telegram":
        team_ids = await get_tg_team_ids()
    else:
        team_ids = await get_max_team_ids()
    return user_id in team_ids
