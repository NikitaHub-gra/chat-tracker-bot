"""MAX API client — async HTTP wrapper for platform-api.max.ru."""
import logging
import httpx

from src.services.settings_service import get_max_token

logger = logging.getLogger(__name__)

MAX_API_BASE = "https://platform-api.max.ru"


async def max_api_call(method: str, path: str, body: dict | None = None) -> dict:
    """Generic MAX API call."""
    token = await get_max_token()
    if not token:
        return {"ok": False, "error": "MAX bot token not configured"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.request(
                method,
                f"{MAX_API_BASE}{path}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": token,
                },
                json=body if body else None,
            )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return {"ok": r.is_success, "status": r.status_code, **data}
    except Exception as e:
        logger.error(f"MAX API error: {e}")
        return {"ok": False, "error": str(e)}


async def send_message(chat_id: int | str, text: str) -> dict:
    """Send a text message via the MAX bot."""
    return await max_api_call("POST", f"/messages?chat_id={chat_id}", {"text": text})


async def get_me() -> dict:
    """Get bot info."""
    return await max_api_call("GET", "/me")


async def setup_max_webhook(base_url: str) -> dict:
    """Register webhook subscription for MAX bot."""
    token = await get_max_token()
    if not token:
        return {"ok": False, "error": "MAX bot token not configured"}

    webhook_url = f"{base_url.rstrip('/')}/webhook/max"
    result = await max_api_call("POST", "/subscriptions", {
        "url": webhook_url,
        "update_types": [
            "message_created", "message_edited",
            "bot_added", "bot_started",
        ],
    })
    return result
