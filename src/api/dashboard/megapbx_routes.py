"""MegaPBX (MegaFon Cloud PBX) dashboard routes.

Endpoints:
  POST /webhook              — PBX call events (public)
  GET  /calls                — call history with filters
  GET  /stats                — call statistics
  GET  /employees            — employee directory
  POST /refresh-employees    — re-pull from PBX API
  GET  /raw-events           — debug: last N raw webhook payloads
"""
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from src.services.megapbx_service import (
    handle_webhook, get_calls, get_stats,
    get_employees, refresh_employees, get_raw_events,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook")
async def pbx_webhook(request: Request):
    """Receive PBX webhook events. Public endpoint."""
    try:
        # Try JSON first, then form-encoded, then raw text
        content_type = request.headers.get("content-type", "")
        if "json" in content_type:
            body = await request.json()
        else:
            raw = await request.body()
            text = raw.decode("utf-8", errors="replace")
            try:
                import json
                body = json.loads(text)
            except Exception:
                from urllib.parse import parse_qs
                parsed = parse_qs(text)
                body = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()} if parsed else {"_raw": text}

        # Merge query params
        if request.query_params:
            body = {**dict(request.query_params), **body}

        result = await handle_webhook(body, dict(request.headers))
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"MegaPBX webhook error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/calls")
async def list_calls(
    from_ms: int | None = None,
    to_ms: int | None = None,
    ext: str | None = None,
    status: str | None = None,
    search: str | None = None,
):
    """Get call records with optional filters."""
    try:
        # Accept from/to as query params
        from_val = from_ms
        to_val = to_ms
        calls = await get_calls(
            from_ms=from_val, to_ms=to_val,
            ext=ext, status=status, search=search,
        )
        # Serialize
        return {
            "success": True,
            "calls": [
                {
                    "id": c.id,
                    "callid": c.callid,
                    "phone": c.phone,
                    "telnum": c.telnum,
                    "ext": c.ext,
                    "employee": c.employee,
                    "direction": c.direction,
                    "status": c.status,
                    "duration": c.duration,
                    "recordLink": c.recordLink,
                    "lastCmd": c.lastCmd,
                    "createdAt": c.createdAt,
                    "updatedAt": c.updatedAt,
                    "lastEventAt": c.lastEventAt,
                }
                for c in calls
            ],
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/stats")
async def call_stats(from_ms: int | None = None, to_ms: int | None = None):
    """Get call statistics."""
    try:
        stats = await get_stats(from_ms=from_ms, to_ms=to_ms)
        return {"success": True, "stats": stats}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/employees")
async def employee_list():
    """Get cached employee directory."""
    employees = await get_employees()
    return {
        "success": True,
        "employees": [
            {
                "ext": e.ext, "name": e.name, "login": e.login,
                "position": e.position, "mobile": e.mobile, "telnum": e.telnum,
            }
            for e in employees
        ],
    }


@router.post("/refresh-employees")
async def refresh_emp():
    """Re-pull employee directory from PBX API."""
    try:
        employees = await refresh_employees()
        return {"success": True, "employees": employees}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/raw-events")
async def raw_events(limit: int = 50):
    """Get last N raw webhook payloads (debug)."""
    events = await get_raw_events(limit)
    return {
        "success": True,
        "events": [
            {
                "id": e.id,
                "receivedAt": e.receivedAt,
                "authOk": e.authOk,
                "body": e.body[:2000],  # Truncate for safety
            }
            for e in events
        ],
    }
