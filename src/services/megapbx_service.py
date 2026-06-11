"""MegaPBX (MegaFon Cloud PBX) integration — ported from TG_Dashboard/megapbx.js.

Two data sources:
1. Push (webhook): PBX POSTs call events to /api/dashboard/megapbx/webhook
2. Pull (crmapi/v1): employee directory (ext -> name mapping)

Storage: PBXCall, PBXEmployee, PBXRawEvent tables in Prisma SQLite.
"""
import json
import logging
import time
from typing import Optional

import httpx

from src.database.db import db

logger = logging.getLogger(__name__)

# MegaPBX API configuration
CRM_AUTH_KEY = "62656e74"  # PBX webhook auth key
PBX_API_BASE = "https://vats998613.megapbx.ru/crmapi/v1"
PBX_API_KEY = "75c0a28b-bdb4-437d-97f5-a15fb849bf8c"

CALL_RETENTION_MS = 120 * 24 * 3600 * 1000  # 120 days in ms
MAX_RAW_EVENTS = 3000


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Employee Directory ───────────────────────────────────────────────────────────

async def refresh_employees() -> list[dict]:
    """Pull employee list from PBX crmapi/v1/users."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{PBX_API_BASE}/users", headers={"X-API-Key": PBX_API_KEY})
        r.raise_for_status()
        data = r.json()
        employees = []
        now = _now_ms()
        for u in data.get("items", []):
            emp = {
                "ext": u.get("ext"),
                "name": u.get("name") or u.get("login", ""),
                "login": u.get("login"),
                "position": u.get("position", ""),
                "mobile": u.get("mobile", ""),
                "telnum": u.get("telnum", ""),
            }
            employees.append(emp)

        # Upsert all employees
        for emp in employees:
            await db.pbxemployee.upsert(
                where={"ext": emp["ext"]} if emp["ext"] else {"name": emp["name"]},
                data={
                    "create": {**emp, "updatedAt": now},
                    "update": {**emp, "updatedAt": now},
                }
            )

        logger.info(f"MegaPBX: loaded {len(employees)} employees")
        return employees
    except Exception as e:
        logger.error(f"MegaPBX refresh error: {e}")
        raise


async def get_employees() -> list:
    return await db.pbxemployee.find_many(order={"name": "asc"})


async def _employee_by_ext(ext: str | None) -> Optional[dict]:
    if not ext:
        return None
    emp = await db.pbxemployee.find_first(where={"ext": str(ext)})
    return emp


# ── Webhook Ingestion ──────────────────────────────────────────────────────────────

def _extract_timestamp(body: dict) -> int:
    """Best-effort timestamp extraction from webhook payload."""
    for field in ("start_time", "date", "timestamp", "time", "created_at"):
        val = body.get(field)
        if not val:
            continue
        try:
            ms = int(val) if isinstance(val, (int, float)) else int(float(val))
            if ms < 1e12:
                ms *= 1000
            return ms
        except (ValueError, TypeError):
            pass
    return _now_ms()


def _is_authenticated(body: dict, headers: dict) -> bool:
    """Check if webhook is authenticated via CRM key."""
    candidates = [
        body.get("key"), body.get("token"), body.get("crm_token"),
        body.get("auth"), body.get("secret"),
        headers.get("x-crm-key"), headers.get("x-api-key"),
        headers.get("x-crm-token"),
    ]
    auth_header = headers.get("authorization", "")
    if auth_header:
        candidates.append(auth_header.replace("Bearer ", "", 1))
    return any(str(v).strip() == CRM_AUTH_KEY for v in candidates if v)


async def handle_webhook(body: dict, headers: dict) -> dict:
    """Process a PBX webhook event. Upserts call records."""
    auth_ok = _is_authenticated(body, headers)

    # Store raw event
    await db.pbxrawevent.create(data={
        "receivedAt": _now_ms(),
        "authOk": auth_ok,
        "body": json.dumps(body, ensure_ascii=False)[:10000],
    })

    # Trim old raw events
    count = await db.pbxrawevent.count()
    if count > MAX_RAW_EVENTS:
        oldest = await db.pbxrawevent.find_many(
            order={"id": "asc"},
            take=count - MAX_RAW_EVENTS,
        )
        if oldest:
            await db.pbxrawevent.delete_many(where={"id": {"in": [e.id for e in oldest]}})

    # Upsert call record
    callid = body.get("callid") or body.get("call_id") or body.get("id")
    if not callid:
        return {"authOk": auth_ok, "cmd": body.get("cmd")}

    ts = _extract_timestamp(body)
    emp = await _employee_by_ext(body.get("ext"))
    cmd = body.get("cmd")

    call = await db.pbxcall.find_unique(where={"callid": str(callid)})
    if not call:
        call = await db.pbxcall.create(data={
            "callid": str(callid),
            "phone": body.get("phone"),
            "telnum": body.get("telnum"),
            "ext": str(body["ext"]) if body.get("ext") is not None else None,
            "employee": emp.name if emp else None,
            "direction": body.get("type") or ("incoming" if cmd == "contact" else None),
            "status": "unknown",
            "duration": 0,
            "lastCmd": cmd,
            "createdAt": ts,
            "updatedAt": _now_ms(),
            "lastEventAt": ts,
        })
    else:
        updates = {"updatedAt": _now_ms(), "lastEventAt": ts}
        if body.get("phone"):
            updates["phone"] = body["phone"]
        if body.get("telnum"):
            updates["telnum"] = body["telnum"]
        if body.get("ext") is not None:
            updates["ext"] = str(body["ext"])
            if emp:
                updates["employee"] = emp.name
        if body.get("type"):
            updates["direction"] = body["type"]
        elif not call.direction and cmd == "contact":
            updates["direction"] = "incoming"
        if body.get("duration") is not None:
            updates["duration"] = int(body["duration"])
        if cmd == "history":
            updates["status"] = body.get("status") or call.status or "unknown"
            if body.get("link"):
                updates["recordLink"] = body["link"]
        elif not call.status or call.status == "unknown":
            status = body.get("status") or body.get("disposition") or body.get("type") or "unknown"
            if status != "unknown" and status != cmd:
                updates["status"] = status
        if cmd:
            updates["lastCmd"] = cmd

        call = await db.pbxcall.update(where={"id": call.id}, data=updates)

    # Prune old calls
    cutoff = _now_ms() - CALL_RETENTION_MS
    await db.pbxcall.delete_many(where={"lastEventAt": {"lt": cutoff}})

    return {"authOk": auth_ok, "cmd": cmd}


# ── Query Helpers ──────────────────────────────────────────────────────────────────

async def get_calls(
    from_ms: int | None = None,
    to_ms: int | None = None,
    ext: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> list:
    """Get call records with optional filters."""
    where = {}
    if from_ms:
        where["lastEventAt"] = {"gte": from_ms}
    if to_ms:
        where.setdefault("lastEventAt", {})["lte"] = to_ms
    if ext:
        where["ext"] = ext
    if status:
        where["status"] = status

    calls = await db.pbxcall.find_many(
        where=where,
        order={"lastEventAt": "desc"},
        take=500,
    )

    if search:
        q = search.lower()
        calls = [c for c in calls if
                 (c.phone and search in c.phone) or
                 (c.employee and q in c.employee.lower())]

    return calls


async def get_stats(from_ms: int | None = None, to_ms: int | None = None) -> dict:
    """Compute call statistics."""
    calls = await get_calls(from_ms=from_ms, to_ms=to_ms)

    stats = {
        "total": len(calls),
        "incoming": 0,
        "outgoing": 0,
        "answered": 0,
        "missed": 0,
        "totalDurationSec": 0,
        "byEmployee": {},
    }

    for c in calls:
        if c.direction in ("incoming", "in"):
            stats["incoming"] += 1
        elif c.direction in ("outgoing", "out"):
            stats["outgoing"] += 1

        status = (c.status or "").lower()
        if status in ("success", "answered"):
            stats["answered"] += 1
        elif status in ("missed", "noanswer", "busy", "notavailable"):
            stats["missed"] += 1

        stats["totalDurationSec"] += c.duration or 0

        name = c.employee or (f"Доб. {c.ext}" if c.ext else "Без сотрудника")
        if name not in stats["byEmployee"]:
            stats["byEmployee"][name] = {"total": 0, "answered": 0, "missed": 0, "durationSec": 0}
        stats["byEmployee"][name]["total"] += 1
        if status in ("success", "answered"):
            stats["byEmployee"][name]["answered"] += 1
        if status in ("missed", "noanswer", "busy", "notavailable"):
            stats["byEmployee"][name]["missed"] += 1
        stats["byEmployee"][name]["durationSec"] += c.duration or 0

    return stats


async def get_raw_events(limit: int = 50) -> list:
    """Get last N raw webhook events (for debugging)."""
    events = await db.pbxrawevent.find_many(
        order={"id": "desc"},
        take=limit,
    )
    return events
