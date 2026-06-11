from fastapi import APIRouter
from src.api.v1.webhooks import router as webhooks_v1_router

api_router = APIRouter(prefix="/api/v1")
# webhooks_v1_router already declares its own "/webhooks" prefix
api_router.include_router(webhooks_v1_router)
