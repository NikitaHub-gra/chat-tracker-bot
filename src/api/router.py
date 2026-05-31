from fastapi import APIRouter
from src.api.v1.webhooks import router as webhooks_v1_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(webhooks_v1_router, prefix="/webhooks")
