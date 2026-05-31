from src.bot.dispatcher import dp
from src.bot.handlers.common import router as common_router
from src.bot.handlers.admin import router as admin_router

# Сначала регистрируем админку и команды, чтобы они обрабатывались первыми
dp.include_router(admin_router)
dp.include_router(common_router)