"""Unified bot + dashboard server.

Runs FastAPI with:
- /webhook/tg  — Telegram webhook (feeds aiogram dispatcher)
- /webhook/max — MAX webhook
- /api/dashboard/* — Dashboard API (ported from Express)
- /public/* — Static dashboard HTML files
"""
import logging
import os
import json

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import types

from src.database.db import connect_db, disconnect_db, db
from src.bot import dispatcher
from src.bot.dispatcher import dp, setup_bot
from src.config import settings
from src.services.settings_service import (
    get_tg_token, get_max_token, get_settings,
    invalidate_cache
)
from src.services.text_utils import (
    DEFAULT_NO_REPLY, DEFAULT_PENDING,
    DEFAULT_POSITIVE, DEFAULT_NEGATIVE
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ── FastAPI App ──────────────────────────────────────────────
app = FastAPI(title="Chat Tracker Bot + Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Telegram Webhook ─────────────────────────────────────────
async def _handle_tg_webhook(request: Request):
    try:
        if dispatcher.bot is None:
            logger.warning("TG webhook received but bot is not configured yet.")
            return {"ok": True}
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_update(dispatcher.bot, update)
    except Exception as e:
        logger.error(f"TG webhook error: {e}", exc_info=True)
    return {"ok": True}

@app.post("/webhook/tg")
async def tg_webhook(request: Request):
    return await _handle_tg_webhook(request)

# Alias for old TG_Dashboard webhook path
@app.post("/telegram/webhook")
async def tg_webhook_legacy(request: Request):
    return await _handle_tg_webhook(request)


# ── MAX Webhook ──────────────────────────────────────────────
@app.post("/webhook/max")
async def max_webhook(request: Request):
    try:
        from src.services.max_handler import handle_max_update
        data = await request.json()
        await handle_max_update(data)
    except Exception as e:
        logger.error(f"MAX webhook error: {e}", exc_info=True)
    return {"ok": True}


# ── MegaPBX Webhook (top-level alias) ──────────────────────────────
@app.post("/megapbx/webhook")
async def megapbx_webhook_top(request: Request):
    """Top-level MegaPBX webhook for backward compat with TG_Dashboard."""
    from src.services.megapbx_service import handle_webhook
    try:
        content_type = request.headers.get("content-type", "")
        if "json" in content_type:
            body = await request.json()
        else:
            raw = await request.body()
            text = raw.decode("utf-8", errors="replace")
            try:
                import json as _json
                body = _json.loads(text)
            except Exception:
                from urllib.parse import parse_qs
                parsed = parse_qs(text)
                body = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()} if parsed else {"_raw": text}
        if request.query_params:
            body = {**dict(request.query_params), **body}
        result = await handle_webhook(body, dict(request.headers))
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"MegaPBX webhook error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Dashboard API ────────────────────────────────────────────
from src.api.dashboard import dashboard_router
app.include_router(dashboard_router)

# Keep existing webhook API for external platforms
from src.api.router import api_router
app.include_router(api_router)


# ── Static Files & Dashboard HTML ────────────────────────────
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "..", "public")

if os.path.isdir(PUBLIC_DIR):
    @app.get("/")
    async def serve_hub():
        return FileResponse(os.path.join(PUBLIC_DIR, "hub.html"))

    @app.get("/telegram.html")
    async def serve_telegram():
        return FileResponse(os.path.join(PUBLIC_DIR, "telegram.html"))

    @app.get("/index.html")
    async def serve_index():
        return FileResponse(os.path.join(PUBLIC_DIR, "index.html"))

    @app.get("/login.html")
    async def serve_login():
        return FileResponse(os.path.join(PUBLIC_DIR, "login.html"))

    @app.get("/admin.html")
    async def serve_admin():
        return FileResponse(os.path.join(PUBLIC_DIR, "admin.html"))

    @app.get("/auth-widget.js")
    async def serve_auth_widget():
        return FileResponse(os.path.join(PUBLIC_DIR, "auth-widget.js"),
                            media_type="application/javascript")

    # Serve any other static assets (CSS/JS if added later)
    app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")

    # Root-level static fallback: serves logo PNG, fonts, and any other
    # asset referenced relative to "/" (e.g. the Реста logo on auth pages).
    # Registered last, so all API routes and explicit page routes win first.
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=False), name="root_static")


# ── Startup / Shutdown ───────────────────────────────────────


@app.on_event("startup")
async def on_startup():
    await connect_db()
    logger.info("Database connected.")

    # Ensure BotSettings row exists with defaults
    settings_row = await db.botsettings.find_first()
    if not settings_row:
        logger.info("No BotSettings found — creating defaults...")
        # Migrate token from old .env if present
        old_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        await db.botsettings.create(data={
            "tgBotToken": old_token,
            "noReplyPhrases": json.dumps(DEFAULT_NO_REPLY, ensure_ascii=False),
            "pendingPhrases": json.dumps(DEFAULT_PENDING, ensure_ascii=False),
            "positiveKeywords": json.dumps(DEFAULT_POSITIVE, ensure_ascii=False),
            "negativeKeywords": json.dumps(DEFAULT_NEGATIVE, ensure_ascii=False),
        })
        invalidate_cache()
        settings_row = await db.botsettings.find_first()

    # Ensure super-admin (623121882) always exists in HubUser as admin
    from src.services.auth_service import SUPER_ADMIN_TG_ID
    super_admin = await db.hubuser.find_unique(where={"telegramId": SUPER_ADMIN_TG_ID})
    if not super_admin:
        import time as _time
        await db.hubuser.create(data={
            "telegramId": SUPER_ADMIN_TG_ID,
            "role": "admin",
            "createdAt": int(_time.time()),
        })
        logger.info(f"Super admin {SUPER_ADMIN_TG_ID} seeded.")
    elif super_admin.role != "admin":
        await db.hubuser.update(where={"telegramId": SUPER_ADMIN_TG_ID}, data={"role": "admin"})
        logger.info(f"Super admin {SUPER_ADMIN_TG_ID} role restored to admin.")

    # Also ensure PlanFixConfig row exists
    pf_config = await db.planfixconfig.find_first()
    if not pf_config:
        await db.planfixconfig.create(data={})

    # Setup Telegram bot
    tg_token = await get_tg_token()
    if tg_token:
        await setup_bot(tg_token)  # sets dispatcher.bot (single source of truth)
        logger.info(f"Telegram bot initialized.")

        # Register handlers
        import src.bot  # noqa — registers routers on dp

        # Auto-setup webhook if BASE_URL is configured
        if settings.BASE_URL:
            import httpx
            webhook_url = f"{settings.BASE_URL.rstrip('/')}/webhook/tg"
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(
                        f"https://api.telegram.org/bot{tg_token}/setWebhook",
                        json={
                            "url": webhook_url,
                            "allowed_updates": [
                                "message", "edited_message", "callback_query",
                                "message_reaction", "business_connection",
                                "business_message", "inline_query",
                                "chosen_inline_result"
                            ],
                        }
                    )
                    result = r.json()
                    if result.get("ok"):
                        logger.info(f"Telegram webhook set to {webhook_url}")
                    else:
                        logger.warning(f"Failed to set TG webhook: {result}")
            except Exception as e:
                logger.error(f"Error setting TG webhook: {e}")
    else:
        logger.warning("No Telegram bot token configured! Set it via Dashboard UI.")

    # Setup MAX webhook if configured
    max_token = await get_max_token()
    if max_token and settings.BASE_URL:
        from src.services.max_api import setup_max_webhook
        try:
            await setup_max_webhook(settings.BASE_URL)
            logger.info("MAX webhook configured.")
        except Exception as e:
            logger.error(f"Error setting MAX webhook: {e}")

    # Start background tasks
    import asyncio
    from src.tasks.scheduler import (
        check_forgotten_chats_loop,
        pending_timeout_checker,
        daily_archive_catchup,
    )
    asyncio.create_task(check_forgotten_chats_loop())
    asyncio.create_task(pending_timeout_checker())
    asyncio.create_task(daily_archive_catchup())
    logger.info("Background tasks started.")


@app.on_event("shutdown")
async def on_shutdown():
    if dispatcher.bot:
        await dispatcher.bot.session.close()
    await disconnect_db()
    logger.info("Shutdown complete.")


# ── Entry Point ──────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
