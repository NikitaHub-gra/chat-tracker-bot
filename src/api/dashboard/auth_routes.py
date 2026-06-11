"""Hub Auth & User Management routes — ported from TG_Dashboard/server.js.

Endpoints:
  POST /request-code   — send 6-digit code via bot DM
  POST /verify         — verify code, create session
  GET  /me             — current session info
  POST /logout         — destroy session
  GET  /users          — list users (admin)
  POST /users          — add user (admin)
  PATCH /users/{id}    — update user role/perms (admin)
  DELETE /users/{id}   — delete user (admin)
  GET  /hub-settings   — get shared settings
  POST /hub-settings   — update shared settings (admin)
"""
import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from src.services.auth_service import (
    create_login_code, verify_login_code, get_session_user, destroy_session,
    list_users, add_user, set_user_role, set_user_permissions, delete_user,
    get_user_permissions, is_service_admin, get_setting, set_setting,
    ALL_MODULES, SESSION_TTL_SEC,
)
from src.services.settings_service import get_tg_token

logger = logging.getLogger(__name__)
router = APIRouter()

SESSION_COOKIE = "hub_session"
VALID_ROLES = {"admin", "service_admin", "engineer"}

HUB_SETTINGS_DEFAULTS = {"companyName": "Реста", "supportContact": ""}


def _normalize_role(role: str) -> str:
    if role == "employee":
        return "engineer"
    return role if role in VALID_ROLES else "engineer"


async def _get_tg_api() -> str:
    token = await get_tg_token()
    return f"https://api.telegram.org/bot{token}" if token else ""


# ── Auth Endpoints ──────────────────────────────────────────────────────────────

@router.post("/request-code")
async def request_code(request: Request):
    """Send a 6-digit login code to the user via the bot's DM."""
    body = await request.json()
    telegram_id = body.get("telegramId")
    if not telegram_id:
        return {"success": False, "error": "Укажите корректный Telegram ID"}
    telegram_id = int(telegram_id)

    code = create_login_code(telegram_id)

    tg_api = await _get_tg_api()
    if not tg_api:
        return {"success": False, "error": "Bot token не настроен"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{tg_api}/sendMessage", json={
                "chat_id": telegram_id,
                "text": f"🔐 Код для входа в Реста Hub: <b>{code}</b>\n\nДействителен 5 минут. Никому не сообщайте этот код.",
                "parse_mode": "HTML",
            })
            result = r.json()
        if not result.get("ok"):
            return {
                "success": False,
                "error": "Не удалось отправить код. Убедитесь, что вы написали боту /start, и что Telegram ID указан верно.",
            }
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": f"Ошибка отправки: {e}"}


@router.post("/verify")
async def verify(request: Request, response: Response):
    """Verify login code and create session."""
    body = await request.json()
    telegram_id = body.get("telegramId")
    code = (body.get("code") or "").strip()
    if not telegram_id or not code:
        return {"success": False, "error": "Укажите ID и код"}
    telegram_id = int(telegram_id)

    # Fetch profile info from Telegram
    profile = {}
    tg_api = await _get_tg_api()
    if tg_api:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                chat = await client.get(f"{tg_api}/getChat?chat_id={telegram_id}")
                chat_data = chat.json()
            if chat_data.get("ok"):
                r = chat_data["result"]
                profile = {
                    "username": r.get("username"),
                    "firstName": r.get("first_name"),
                    "lastName": r.get("last_name"),
                }
        except Exception:
            pass

    result = await verify_login_code(telegram_id, code, profile)
    if not result["ok"]:
        if result.get("error") == "not_registered":
            return {"success": False, "error": "Ваш Telegram ID не добавлен администратором хаба. Обратитесь к администратору."}
        return {"success": False, "error": result["error"]}

    # Set session cookie
    response.set_cookie(
        SESSION_COOKIE,
        result["token"],
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SEC,
    )

    user = result["user"]
    return {
        "success": True,
        "user": {
            "id": user.id,
            "name": user.firstName or user.username or str(user.telegramId),
            "role": user.role,
        },
    }


@router.get("/me")
async def me(request: Request):
    """Get current session user info."""
    token = request.cookies.get(SESSION_COOKIE)
    user = await get_session_user(token)
    if not user:
        return {"user": None}
    return {
        "user": {
            "id": user.id,
            "telegramId": user.telegramId,
            "name": user.firstName or user.username or str(user.telegramId),
            "username": user.username,
            "role": user.role,
            "permissions": get_user_permissions(user),
        }
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Destroy session and clear cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await destroy_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"success": True}


# ── Admin: User Management ────────────────────────────────────────────────────────

@router.get("/users")
async def get_users(request: Request):
    """List all hub users (admin only)."""
    user = await get_session_user(request.cookies.get(SESSION_COOKIE))
    if not user or user.role != "admin":
        return JSONResponse({"success": False, "error": "forbidden"}, status_code=403)
    users = await list_users()
    return {
        "success": True,
        "users": [
            {
                "id": u.id, "telegramId": u.telegramId, "username": u.username,
                "firstName": u.firstName, "lastName": u.lastName, "role": u.role,
                "permissions": get_user_permissions(u),
                "createdAt": u.createdAt, "lastLoginAt": u.lastLoginAt,
            }
            for u in users
        ],
        "modules": ALL_MODULES,
    }


@router.post("/users")
async def create_user(request: Request):
    """Add a new hub user (admin only)."""
    user = await get_session_user(request.cookies.get(SESSION_COOKIE))
    if not user or user.role != "admin":
        return JSONResponse({"success": False, "error": "forbidden"}, status_code=403)
    body = await request.json()
    telegram_id = body.get("telegramId")
    role = _normalize_role(body.get("role", "engineer"))
    first_name = (body.get("firstName") or "").strip() or None
    if not telegram_id:
        return {"success": False, "error": "telegramId required"}
    new_user = await add_user(int(telegram_id), role, first_name=first_name)
    return {"success": True, "user": {"id": new_user.id, "telegramId": new_user.telegramId,
                                       "firstName": new_user.firstName, "role": new_user.role}}


@router.patch("/users/{user_id}")
async def update_user(user_id: int, request: Request):
    """Update user role/permissions (admin only)."""
    user = await get_session_user(request.cookies.get(SESSION_COOKIE))
    if not user or user.role != "admin":
        return JSONResponse({"success": False, "error": "forbidden"}, status_code=403)
    body = await request.json()
    if "role" in body:
        await set_user_role(user_id, _normalize_role(body["role"]))
    if "permissions" in body:
        await set_user_permissions(user_id, body["permissions"])
    return {"success": True}


@router.delete("/users/{user_id}")
async def remove_user(user_id: int, request: Request):
    """Delete a hub user (admin only)."""
    user = await get_session_user(request.cookies.get(SESSION_COOKIE))
    if not user or user.role != "admin":
        return JSONResponse({"success": False, "error": "forbidden"}, status_code=403)
    if user.id == user_id:
        return {"success": False, "error": "Нельзя удалить самого себя"}
    await delete_user(user_id)
    return {"success": True}


# ── Hub Settings ───────────────────────────────────────────────────────────────────

@router.get("/hub-settings")
async def get_hub_settings():
    """Get shared hub settings (any logged-in user)."""
    settings = {**HUB_SETTINGS_DEFAULTS}
    stored = await get_setting("hub_general", {})
    if isinstance(stored, dict):
        settings.update(stored)
    return {"success": True, "settings": settings}


@router.post("/hub-settings")
async def update_hub_settings(request: Request):
    """Update shared hub settings (admin only)."""
    user = await get_session_user(request.cookies.get(SESSION_COOKIE))
    if not user or user.role != "admin":
        return JSONResponse({"success": False, "error": "forbidden"}, status_code=403)
    body = await request.json()
    current = {**HUB_SETTINGS_DEFAULTS}
    stored = await get_setting("hub_general", {})
    if isinstance(stored, dict):
        current.update(stored)
    next_settings = {
        "companyName": (body.get("companyName") or current["companyName"]).strip(),
        "supportContact": (body.get("supportContact") or current.get("supportContact", "")).strip(),
    }
    await set_setting("hub_general", next_settings)
    return {"success": True, "settings": next_settings}
