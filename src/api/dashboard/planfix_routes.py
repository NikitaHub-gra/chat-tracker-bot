"""PlanFix API proxy routes — ported from TG_Dashboard/server.js."""
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

from src.database.db import db

router = APIRouter()

ACTIVE_STATUS_IDS = {1, 2, 109, 114, 169, 180, 186, 195, 198}
COMPLETED_STATUS_IDS = {3}
ON_HOLD_STATUS_IDS = {4}


async def _get_pf_config():
    cfg = await db.planfixconfig.find_first()
    return cfg


async def _pf_get(endpoint: str, cfg=None):
    if not cfg:
        cfg = await _get_pf_config()
    if not cfg or not cfg.token:
        return {"error": "PlanFix not configured"}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{cfg.apiBase}{endpoint}",
            headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"},
            timeout=30,
        )
        return r.json()


async def _pf_post(endpoint: str, body: dict, cfg=None):
    if not cfg:
        cfg = await _get_pf_config()
    if not cfg or not cfg.token:
        return {"error": "PlanFix not configured"}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{cfg.apiBase}{endpoint}",
            headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        return r.json()


async def _fetch_all_tasks(filters, fields, cfg=None):
    tasks = []
    offset = 0
    page_size = 100
    while True:
        data = await _pf_post("/task/list", {
            "offset": offset, "pageSize": page_size,
            "filters": filters, "fields": fields,
        }, cfg)
        batch = data.get("tasks", [])
        if not batch:
            break
        tasks.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return tasks


def _add_working_days(date: datetime, days: int) -> datetime:
    from datetime import timedelta
    result = date
    added = 0
    while added < days:
        result += timedelta(days=1)
        if result.weekday() < 5:  # Mon-Fri
            added += 1
    return result


def _parse_task_date(task) -> datetime | None:
    dt = task.get("dateTime")
    if not dt:
        return None
    ts = dt.get("dateTimeUtcSeconds")
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def _classify_task(task) -> str:
    sid = (task.get("status") or {}).get("id")
    if sid in COMPLETED_STATUS_IDS:
        return "completed"
    if sid in ON_HOLD_STATUS_IDS:
        return "on_hold"
    if sid in ACTIVE_STATUS_IDS:
        return "active"
    return "unknown"


def _is_sla_breached(task, now=None) -> bool:
    if now is None:
        now = datetime.now(tz=timezone.utc)
    created = _parse_task_date(task)
    if not created:
        return False
    deadline = _add_working_days(created, 3)
    return now > deadline


def _days_open(task, now=None) -> int | None:
    if now is None:
        now = datetime.now(tz=timezone.utc)
    created = _parse_task_date(task)
    if not created:
        return None
    return int((now - created).total_seconds() / 86400)


# ── GET /stats ───────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    cfg = await _get_pf_config()
    if not cfg or not cfg.token:
        return {"success": False, "error": "PlanFix not configured"}

    filters = [{"type": 38, "operator": "equal", "value": {"id": cfg.supportGroupId}}]
    fields = "id,name,status,assignees,dateTime,counterparty,template"
    tasks = await _fetch_all_tasks(filters, fields, cfg)
    now = datetime.now(tz=timezone.utc)

    stats = {
        "total": len(tasks), "active": 0, "completed": 0, "on_hold": 0,
        "sla_breached": 0, "sla_ok": 0,
        "by_status": {}, "by_assignee": {}, "by_day": {},
    }

    for task in tasks:
        cls = _classify_task(task)
        stats[cls] = stats.get(cls, 0) + 1

        status_name = (task.get("status") or {}).get("name", "Неизвестен")
        stats["by_status"][status_name] = stats["by_status"].get(status_name, 0) + 1

        for user in (task.get("assignees") or {}).get("users") or []:
            name = user.get("name", "?")
            if name not in stats["by_assignee"]:
                stats["by_assignee"][name] = {"total": 0, "active": 0, "completed": 0, "sla_breached": 0}
            stats["by_assignee"][name]["total"] += 1
            stats["by_assignee"][name][cls] = stats["by_assignee"][name].get(cls, 0) + 1
            if cls == "active" and _is_sla_breached(task, now):
                stats["by_assignee"][name]["sla_breached"] += 1

        if cls == "active":
            if _is_sla_breached(task, now):
                stats["sla_breached"] += 1
            else:
                stats["sla_ok"] += 1

        created = _parse_task_date(task)
        if created:
            day_key = created.strftime("%Y-%m-%d")
            stats["by_day"][day_key] = stats["by_day"].get(day_key, 0) + 1

    return {"success": True, "stats": stats}


# ── GET /tasks/active ────────────────────────────────────────

@router.get("/tasks/active")
async def get_active_tasks():
    cfg = await _get_pf_config()
    if not cfg or not cfg.token:
        return {"success": False, "error": "PlanFix not configured"}

    filters = [{"type": 38, "operator": "equal", "value": {"id": cfg.supportGroupId}}]
    fields = "id,name,status,assignees,dateTime,counterparty,template"
    tasks = await _fetch_all_tasks(filters, fields, cfg)
    now = datetime.now(tz=timezone.utc)

    active = []
    for t in tasks:
        if _classify_task(t) != "active":
            continue
        created = _parse_task_date(t)
        deadline = _add_working_days(created, 3) if created else None
        active.append({
            "id": t["id"],
            "name": t.get("name"),
            "status": (t.get("status") or {}).get("name"),
            "statusId": (t.get("status") or {}).get("id"),
            "assignees": [u.get("name") for u in (t.get("assignees") or {}).get("users") or []],
            "client": (t.get("counterparty") or {}).get("name"),
            "created": (t.get("dateTime") or {}).get("datetime"),
            "daysOpen": _days_open(t, now),
            "slaBreached": _is_sla_breached(t, now),
            "deadline": deadline.isoformat() if deadline else None,
        })

    active.sort(key=lambda x: (-x["slaBreached"], -(x["daysOpen"] or 0)))
    return {"success": True, "tasks": active}


# ── GET /tasks/completed ─────────────────────────────────────

@router.get("/tasks/completed")
async def get_completed_tasks():
    cfg = await _get_pf_config()
    if not cfg or not cfg.token:
        return {"success": False, "error": "PlanFix not configured"}

    filters = [{"type": 38, "operator": "equal", "value": {"id": cfg.supportGroupId}}]
    fields = "id,name,status,assignees,dateTime,counterparty"
    tasks = await _fetch_all_tasks(filters, fields, cfg)
    now = datetime.now(tz=timezone.utc)

    completed = []
    for t in tasks:
        if _classify_task(t) != "completed":
            continue
        completed.append({
            "id": t["id"],
            "name": t.get("name"),
            "assignees": [u.get("name") for u in (t.get("assignees") or {}).get("users") or []],
            "client": (t.get("counterparty") or {}).get("name"),
            "created": (t.get("dateTime") or {}).get("datetime"),
            "daysOpen": _days_open(t, now),
        })

    completed.reverse()
    return {"success": True, "tasks": completed[:50]}
