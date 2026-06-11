"""Daily KPI history archive routes."""
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query

from src.database.db import db
from src.tasks.scheduler import _compute_day_stats, day_bounds_local

router = APIRouter()

_TZ = timezone.utc


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _iter_dates(from_: str, to: str):
    """Yield YYYY-MM-DD strings from from_ to to inclusive."""
    cur = datetime.strptime(from_, "%Y-%m-%d")
    end = datetime.strptime(to, "%Y-%m-%d")
    while cur <= end:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


@router.get("/{messenger}")
async def get_history(
    messenger: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    """Get daily KPI snapshots for a messenger in date range.

    For days that have an archive record, return archived data.
    For today (or any day missing from the archive), compute live from raw tables.
    This ensures the current day always shows up-to-date partial stats.
    """
    if messenger not in ("telegram", "max"):
        return {"success": False, "error": "invalid messenger"}

    rows = await db.dailystats.find_many(
        where={
            "messenger": messenger,
            "date": {"gte": from_, "lte": to},
        },
        order={"date": "asc"},
    )
    archived: dict[str, dict] = {r.date: r for r in rows}

    today = _today_str()
    days = []

    for date_str in _iter_dates(from_, to):
        if date_str in archived:
            r = archived[date_str]
            days.append({
                "date": r.date,
                "waiting": r.waiting,
                "avgResponseSec": r.avgResponseSec,
                "todayChats": r.todayChats,
                "incomingToday": r.incomingToday,
                "agentMsgToday": r.agentMsgToday,
                "timeSpentSec": r.timeSpentSec,
                "missedToday": r.missedToday,
                "pendingNow": r.pendingNow,
                "byAgent": r.byAgent,
            })
        else:
            # Not archived yet — compute live (always for today, also catches gaps)
            day_start, day_end = day_bounds_local(date_str)
            if date_str == today:
                day_end = int(time.time()) + 1  # partial day up to now
            try:
                s = await _compute_day_stats(messenger, day_start, day_end)
                import json as _json
                days.append({
                    "date": date_str,
                    "waiting": s["waiting"],
                    "avgResponseSec": s["avgResponseSec"],
                    "todayChats": s["todayChats"],
                    "incomingToday": s["incomingToday"],
                    "agentMsgToday": s["agentMsgToday"],
                    "timeSpentSec": s["timeSpentSec"],
                    "missedToday": s["missedToday"],
                    "pendingNow": s["pendingNow"],
                    "byAgent": _json.dumps(s["byAgent"], ensure_ascii=False),
                })
            except Exception:
                pass  # skip days we can't compute

    return {"success": True, "messenger": messenger, "from": from_, "to": to, "days": days}
