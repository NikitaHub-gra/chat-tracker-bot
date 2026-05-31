from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from src.database.db import db
from src.config import settings

router = Router(name="admin")

class AdminSettings(StatesGroup):
    waiting_for_timeout = State()

async def is_has_admin_rights(tg_id: str) -> bool:
    if tg_id == settings.SUPER_ADMIN_ID:
        return True
    admin_exists = await db.adminuser.find_unique(where={"telegramId": tg_id})
    return admin_exists is not None

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    """Единая команда помощи для всех пользователей"""
    if not message.from_user:
        return
        
    tg_id = str(message.from_user.id)
    is_admin = await is_has_admin_rights(tg_id)
    is_super = tg_id == settings.SUPER_ADMIN_ID

    text = (
        "🤖 <b>Система контроля ответов поддержки</b>\n\n"
        "<b>Для инженеров:</b>\n"
        "▶️ /start — Зарегистрироваться в боте как инженер (активировать теги)\n\n"
    )

    if is_admin:
        text += (
            "🛠 <b>Команды администратора:</b>\n"
            "▶️ /set_alert_chat — Вызовите внутри группы/канала, куда бот должен присылать уведомления о просрочках.\n"
            "▶️ /set_timeout — Установить время ожидания ответа (в личке бота).\n\n"
        )
    
    if is_super:
        text += (
            "👑 <b>Команды Главного Админа:</b>\n"
            "▶️ <code>/add_admin ТГ_ID @username</code> — Добавить админа\n"
            "▶️ <code>/del_admin ТГ_ID</code> — Удалить админа\n"
        )

    await message.reply(text)


@router.message(Command("set_alert_chat"))
async def cmd_set_alert_chat(message: types.Message):
    if not message.from_user:
        return
        
    if not await is_has_admin_rights(str(message.from_user.id)):
        await message.reply("⛔️ У вас нет прав на настройку уведомлений системы.")
        return

    if message.chat.type in ["private"]:
        await message.reply("❌ Эту команду нужно вызывать внутри Группы, куда будут приходить уведомления о проёбах!")
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title

    config = await db.systemconfig.find_first()
    if config:
        await db.systemconfig.update(
            where={"id": config.id},
            data={"alertChatId": chat_id}
        )
        await message.reply(
            f"🎯 <b>Рабочий чат для алертов успешно привязан!</b>\n\n"
            f"Сюда будут приходить уведомления о забытых обращениях.\n"
            f"ID чата: <code>{chat_id}</code>\n"
            f"Название: <b>{chat_title}</b>"
        )


@router.message(Command("set_timeout"))
async def cmd_set_timeout(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply("❌ Эту команду можно использовать только в личке с ботом.")
        return

    if not await is_has_admin_rights(str(message.from_user.id)):
        await message.reply("⛔️ Доступ запрещен.")
        return

    await message.reply("⏳ Введите время ожидания для инженеров (<b>в минутах</b>):")
    await state.set_state(AdminSettings.waiting_for_timeout)


@router.message(AdminSettings.waiting_for_timeout)
async def process_timeout_input(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.reply("❌ Введите целое число минут:")
        return

    new_timeout = int(message.text)
    config = await db.systemconfig.find_first()
    if config:
        await db.systemconfig.update(
            where={"id": config.id},
            data={"waitTimeoutMin": new_timeout}
        )
        await message.reply(f"✅ Время ожидания установлено на <b>{new_timeout} мин.</b>")
    await state.clear()


@router.message(Command("add_admin"))
async def cmd_add_admin(message: types.Message):
    if not message.from_user or str(message.from_user.id) != settings.SUPER_ADMIN_ID:
        await message.reply("⛔️ Доступ запрещен.")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.reply("ℹ️ Формат: <code>/add_admin ТГ_ID @username</code>")
        return

    target_id = args[1]
    target_username = args[2] if len(args) > 2 else "Admin"

    await db.adminuser.upsert(
        where={"telegramId": target_id},
        data={
            "create": {"telegramId": target_id, "username": target_username},
            "update": {"username": target_username}
        }
    )
    await message.reply(f"👤 Пользователь <code>{target_id}</code> добавлен в админы.")


@router.message(Command("del_admin"))
async def cmd_del_admin(message: types.Message):
    if not message.from_user or str(message.from_user.id) != settings.SUPER_ADMIN_ID:
        await message.reply("⛔️ Доступ запрещен.")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.reply("ℹ️ Формат: <code>/del_admin ТГ_ID</code>")
        return

    target_id = args[1]
    try:
        await db.adminuser.delete(where={"telegramId": target_id})
        await message.reply(f"❌ Админ <code>{target_id}</code> удален.")
    except Exception:
        await message.reply("❌ Админ не найден.")