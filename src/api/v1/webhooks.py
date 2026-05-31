from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional
from src.services.chat_service import ChatService
from src.database.db import db

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# Схема входящего события от чат-платформы
class WebhookPayload(BaseModel):
    chat_id: str = Field(..., description="Уникальный идентификатор чата/диалога")
    client_name: str = Field(..., description="Имя клиента или компании")
    chat_url: str = Field(..., description="Прямая ссылка на чат для инлайн-кнопки")
    last_message: str = Field(..., description="Текст последнего сообщения в чате")
    status: str = Field(..., description="Статус чата: waiting, opened, answered, closed")
    engineer_tg_id: Optional[str] = Field(None, description="Telegram ID инженера, если чат открыт/назначен")


@router.post("/chat-event", status_code=status.HTTP_200_OK)
async def handle_chat_event(payload: WebhookPayload):
    """
    Эндпоинт для приема событий чата.
    Сюда чат-платформа шлет хуки при любых изменениях (новое сообщение, открытие инженером, закрытие).
    """
    try:
        # Если передан инфо об инженере, проверяем/актуализируем его в нашей локальной БД,
        # чтобы всегда иметь актуальный маппинг инженеров для тегов.
        if payload.engineer_tg_id:
            # Пытаемся найти инженера, если его нет — создаем дефолтную запись.
            # Настоящее имя и username обновятся, когда инженер активирует бота, 
            # но база уже будет знать его Telegram ID.
            await db.engineer.upsert(
                where={"telegramId": payload.engineer_tg_id},
                data={
                    "create": {
                        "telegramId": payload.engineer_tg_id,
                        "username": f"id{payload.engineer_tg_id}",
                        "name": "Сотрудник"
                    },
                    "update": {} # Если существует, ничего не трогаем
                }
            )

        # Вызываем наш сервис для сохранения состояния чата и сброса/расчета таймеров
        await ChatService.register_or_update_chat(
            chat_id=payload.chat_id,
            client_name=payload.client_name,
            chat_url=payload.chat_url,
            message_text=payload.last_message,
            status=payload.status,
            engineer_tg_id=payload.engineer_tg_id
        )
        
        return {"status": "success", "message": "Event processed successfully"}

    except Exception as e:
        # Логируем ошибку и отдаем 500 статус чат-платформе, чтобы она знала о сбое
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Ошибка при обработке вебхука чата: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during webhook processing"
        )