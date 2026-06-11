from fastapi import APIRouter

from src.api.dashboard.tg_routes import router as tg_router
from src.api.dashboard.max_routes import router as max_router
from src.api.dashboard.planfix_routes import router as planfix_router
from src.api.dashboard.settings_routes import router as settings_router
from src.api.dashboard.history_routes import router as history_router
from src.api.dashboard.auth_routes import router as auth_router
from src.api.dashboard.megapbx_routes import router as megapbx_router

dashboard_router = APIRouter(prefix="/api/dashboard")
dashboard_router.include_router(tg_router, prefix="/tg", tags=["Telegram Dashboard"])
dashboard_router.include_router(max_router, prefix="/max", tags=["MAX Dashboard"])
dashboard_router.include_router(planfix_router, prefix="/planfix", tags=["PlanFix"])
dashboard_router.include_router(settings_router, prefix="/settings", tags=["Settings"])
dashboard_router.include_router(history_router, prefix="/history", tags=["History"])
dashboard_router.include_router(auth_router, prefix="/auth", tags=["Auth"])
dashboard_router.include_router(megapbx_router, prefix="/megapbx", tags=["MegaPBX"])
