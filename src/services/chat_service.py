from datetime import datetime
from typing import Optional
from src.database.db import db

class ChatService:
    @staticmethod
    async def register_or_update_chat(
        chat_id: str,
        client_name: str,
        chat_url: str,
        message_text: str,
        status: str,
        engineer_tg_id: Optional[str] = None
    ):
        """
        Регистрирует новый чат или обновляет существующий при входящих событиях.
        Если чат перешел в статус отличный от 'opened', сбрасываем флаг алерта.
        """
        engineer_id = None
        
        if engineer_tg_id:
            engineer = await db.engineer.find_unique(where={"telegramId": engineer_tg_id})
            if engineer:
                engineer_id = engineer.id

        # Если пришло новое сообщение от клиента или статус изменился,
        # сбрасываем isAlerted в False, чтобы система могла заронить новый алерт при просрочке
        is_alerted = False
        if status == "answered" or status == "closed":
            is_alerted = True # Для закрытых или отвеченных алерты слать не нужно

        return await db.activechat.upsert(
            where={"id": chat_id},
            data={
                "create": {
                    "id": chat_id,
                    "clientName": client_name,
                    "externalChatUrl": chat_url,
                    "lastMessage": message_text,
                    "status": status,
                    "engineerId": engineer_id,
                    "isAlerted": is_alerted,
                    "updatedAt": datetime.utcnow()
                },
                "update": {
                    "clientName": client_name,
                    "externalChatUrl": chat_url,
                    "lastMessage": message_text,
                    "status": status,
                    "engineerId": engineer_id,
                    "isAlerted": is_alerted,
                    "updatedAt": datetime.utcnow()
                }
            }
        )