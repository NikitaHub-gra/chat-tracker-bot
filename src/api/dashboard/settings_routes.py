"""Global settings routes — manage all bot settings via web UI."""
import json

from fastapi import APIRouter

from src.database.db import db
from src.services.settings_service import get_settings, update_settings_field, invalidate_cache

router = APIRouter()


@router.get("/")
async def get_all_settings():
    """Return all current settings."""
    s = await get_settings()
    if not s:
        return {"success": False, "error": "no settings"}
    return {
        "success": True,
        "settings": {
            "tgBotToken": s.tgBotToken[:8] + "***" if s.tgBotToken else "",
            "tgOwnerId": s.tgOwnerId,
            "tgTeamIds": json.loads(s.tgTeamIds),
            "maxBotToken": s.maxBotToken[:8] + "***" if s.maxBotToken else "",
            "maxTeamIds": json.loads(s.maxTeamIds),
            "waitTimeoutMin": s.waitTimeoutMin,
            "missedThreshold": s.missedThreshold,
            "pendingTimeout": s.pendingTimeout,
            "alertChatId": s.alertChatId,
            "noReplyPhrases": json.loads(s.noReplyPhrases),
            "pendingPhrases": json.loads(s.pendingPhrases),
            "positiveKeywords": json.loads(s.positiveKeywords),
            "negativeKeywords": json.loads(s.negativeKeywords),
        },
    }


@router.post("/update")
async def update_setting(body: dict):
    """Update one or more settings fields."""
    allowed_fields = {
        "waitTimeoutMin", "missedThreshold", "pendingTimeout", "alertChatId",
        "tgOwnerId",
    }
    updated = {}
    for key, value in body.items():
        if key in allowed_fields:
            if key in ("tgTeamIds", "maxTeamIds") and isinstance(value, list):
                value = json.dumps(value)
            await update_settings_field(key, value)
            updated[key] = value

    return {"success": True, "updated": updated}


@router.get("/planfix-config")
async def get_planfix_config():
    cfg = await db.planfixconfig.find_first()
    if not cfg:
        return {"success": True, "configured": False}
    return {
        "success": True, "configured": True,
        "apiBase": cfg.apiBase,
        "supportGroupId": cfg.supportGroupId,
        "templateId": cfg.templateId,
        "tokenMasked": cfg.token[:6] + "***" if cfg.token else "",
    }

@router.post("/planfix-config")
async def set_planfix_config(body: dict):
    cfg = await db.planfixconfig.find_first()
    data = {}
    if "token" in body:
        data["token"] = body["token"]
    if "apiBase" in body:
        data["apiBase"] = body["apiBase"]
    if "supportGroupId" in body:
        data["supportGroupId"] = int(body["supportGroupId"])
    if "templateId" in body:
        data["templateId"] = int(body["templateId"])

    if cfg:
        await db.planfixconfig.update(where={"id": cfg.id}, data=data)
    else:
        await db.planfixconfig.create(data=data)
    return {"success": True}
