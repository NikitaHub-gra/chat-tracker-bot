"""Hub authentication & user management — ported from TG_Dashboard/auth.js.

Login flow:
1. User enters Telegram ID on login page
2. Bot sends a 6-digit code via DM
3. User enters code → session cookie set

Storage: HubUser, HubSession, HubSetting tables in Prisma SQLite.
"""
import hashlib
import json
import secrets
import time
import logging
from typing import Optional

from src.database.db import db

logger = logging.getLogger(__name__)

# Constants
SESSION_TTL_SEC = 30 * 24 * 3600  # 30 days
CODE_TTL_SEC = 5 * 60  # 5 minutes
MAX_ATTEMPTS = 5
SUPER_ADMIN_TG_ID = 623121882
# module keys — "settings" grants access to settings modals / admin panel
ALL_MODULES = ["telegram", "max", "planfix", "megapbx", "settings"]
# Default permissions for a new engineer (view stats only, no settings)
DEFAULT_ENGINEER_MODULES = ["telegram", "max"]

# In-memory login codes (ephemeral, 5 min TTL)
_login_codes: dict[int, dict] = {}  # telegram_id -> {code, expires_at, attempts}


def _now() -> int:
    return int(time.time())


# ── Users ──────────────────────────────────────────────────────────────────────

async def users_count() -> int:
    return await db.hubuser.count()


async def get_user_by_telegram_id(telegram_id: int):
    return await db.hubuser.find_unique(where={"telegramId": telegram_id})


async def get_user_by_id(user_id: int):
    return await db.hubuser.find_unique(where={"id": user_id})


async def list_users():
    return await db.hubuser.find_many(order={"createdAt": "asc"})


async def add_user(telegram_id: int, role: str = "engineer", first_name: str | None = None):
    now = _now()
    create_data: dict = {"telegramId": telegram_id, "role": role, "createdAt": now}
    if first_name:
        create_data["firstName"] = first_name
    update_data: dict = {"role": role}
    if first_name:
        update_data["firstName"] = first_name
    return await db.hubuser.upsert(
        where={"telegramId": telegram_id},
        data={"create": create_data, "update": update_data},
    )


async def set_user_role(user_id: int, role: str):
    await db.hubuser.update(where={"id": user_id}, data={"role": role})


def get_user_permissions(user) -> list[str]:
    """Return list of module keys this user can access.
    Admins always get full access. Engineers get explicit perms or DEFAULT_ENGINEER_MODULES."""
    if not user:
        return []
    if user.role == "admin" or user.telegramId == SUPER_ADMIN_TG_ID:
        return ALL_MODULES[:]
    if user.permissions:
        try:
            perms = json.loads(user.permissions)
            if isinstance(perms, list):
                return [p for p in perms if p in ALL_MODULES]
        except (json.JSONDecodeError, TypeError):
            pass
    # service_admin gets everything except they're not root
    if user.role == "service_admin":
        return ALL_MODULES[:]
    # engineers: view-only by default
    return DEFAULT_ENGINEER_MODULES[:]


async def set_user_permissions(user_id: int, perms: list[str] | None):
    value = json.dumps([p for p in perms if p in ALL_MODULES]) if isinstance(perms, list) else None
    await db.hubuser.update(where={"id": user_id}, data={"permissions": value})


def is_service_admin(user) -> bool:
    """True if user is admin or service_admin (can moderate)."""
    return bool(user) and user.role in ("admin", "service_admin")


def can_moderate(user) -> bool:
    return is_service_admin(user)


async def delete_user(user_id: int):
    await db.hubuser.delete(where={"id": user_id})
    await db.hubsession.delete_many(where={"userId": user_id})


# ── Login Codes ──────────────────────────────────────────────────────────────────

def create_login_code(telegram_id: int) -> str:
    """Generate a 6-digit code, store in memory for 5 minutes."""
    import random
    code = str(random.randint(100000, 999999))
    _login_codes[telegram_id] = {
        "code": code,
        "expires_at": _now() + CODE_TTL_SEC,
        "attempts": 0,
    }
    return code


async def verify_login_code(telegram_id: int, code: str, profile: dict = None):
    """Verify login code. Returns {ok, token, user} or {ok: false, error}."""
    profile = profile or {}
    entry = _login_codes.get(telegram_id)
    if not entry:
        return {"ok": False, "error": "Код не запрошен. Запросите код заново."}
    if _now() > entry["expires_at"]:
        _login_codes.pop(telegram_id, None)
        return {"ok": False, "error": "Код истёк. Запросите новый."}
    if entry["attempts"] >= MAX_ATTEMPTS:
        _login_codes.pop(telegram_id, None)
        return {"ok": False, "error": "Слишком много попыток. Запросите код заново."}
    if code.strip() != entry["code"]:
        entry["attempts"] += 1
        return {"ok": False, "error": "Неверный код."}

    _login_codes.pop(telegram_id, None)
    now = _now()

    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        count = await users_count()
        if count == 0 or telegram_id == SUPER_ADMIN_TG_ID:
            # First user OR super admin always becomes admin
            user = await db.hubuser.create(data={
                "telegramId": telegram_id,
                "username": profile.get("username"),
                "firstName": profile.get("firstName"),
                "lastName": profile.get("lastName"),
                "role": "admin",
                "createdAt": now,
                "lastLoginAt": now,
            })
        else:
            return {"ok": False, "error": "not_registered"}
    else:
        await db.hubuser.update(
            where={"id": user.id},
            data={
                "lastLoginAt": now,
                "username": profile.get("username") or user.username,
                "firstName": profile.get("firstName") or user.firstName,
                "lastName": profile.get("lastName") or user.lastName,
            }
        )
        user = await get_user_by_id(user.id)

    token = secrets.token_hex(32)
    await db.hubsession.create(data={
        "token": token,
        "userId": user.id,
        "createdAt": now,
        "expiresAt": now + SESSION_TTL_SEC,
    })

    return {"ok": True, "token": token, "user": user}


# ── Sessions ──────────────────────────────────────────────────────────────────────

async def get_session_user(token: str | None):
    """Get user from session token. Returns None if invalid/expired."""
    if not token:
        return None
    session = await db.hubsession.find_unique(where={"token": token})
    if not session:
        return None
    if _now() > session.expiresAt:
        await db.hubsession.delete(where={"token": token})
        return None
    return await get_user_by_id(session.userId)


async def destroy_session(token: str):
    try:
        await db.hubsession.delete(where={"token": token})
    except Exception:
        pass


# ── Hub Settings (key/value, JSON-encoded) ────────────────────────────────────────

async def get_setting(key: str, default=None):
    row = await db.hubsetting.find_unique(where={"key": key})
    if not row:
        return default
    try:
        return json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        return default


async def set_setting(key: str, value):
    await db.hubsetting.upsert(
        where={"key": key},
        data={
            "create": {"key": key, "value": json.dumps(value, ensure_ascii=False)},
            "update": {"value": json.dumps(value, ensure_ascii=False)},
        }
    )
