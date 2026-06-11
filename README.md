# Chat Tracker Bot + Dashboard

Унифицированный бот и веб-дашборд для отслеживания обращений в Telegram, MAX и PlanFix.  
Работает на одном Telegram-токене, одном сервере (FastAPI), одной базе данных (SQLite через Prisma).

## Возможности

- **Telegram Dashboard** — очередь чатов, статистика, пропущенные, контроль, нарушения, отчёты
- **MAX Dashboard** — аналогичный функционал для мессенджера MAX
- **PlanFix** — интеграция с задачами (активные, завершённые, SLA)
- **MegaPBX** — интеграция с МегаФон Cloud PBX (звонки, сотрудники)
- **Hub Auth** — авторизация через Telegram ID + 6-значный код, роли (admin/service_admin/engineer)
- **SSE** — real-time обновления дашборда
- **Фоновые задачи** — проверка забытых чатов, таймаут pending, ежедневная архивация статистики

## Стек

- **Python 3.12+** / FastAPI / uvicorn
- **aiogram v3** (Telegram Bot API, webhook mode)
- **Prisma ORM (Python)** + SQLite
- **httpx** (HTTP-клиент для внешних API)

## Установка

```bash
# 1. Клонируй репозиторий
cd chat-tracker-bot

# 2. Установи зависимости
pip install -r requirements.txt

# 3. Сгенерируй Prisma-клиент и создай БД
#    (на Windows обязательно PYTHONUTF8=1 для кириллицы в схеме)
$env:PYTHONUTF8="1"
python -m prisma generate
python -m prisma db push
```

## Настройка `.env`

```env
DATABASE_URL="file:./src/database/dev.db"
SUPER_ADMIN_ID="YOUR_TELEGRAM_ID"
# BASE_URL="https://your-domain.com"  # Раскомментируй после настройки ngrok
```

## Запуск

### Локальный запуск

```bash
$env:PYTHONUTF8="1"
python -m src.main
```

Сервер запустится на `http://localhost:8000`.

Дашборд доступен по адресам:

- **Hub (главная):** `http://localhost:8000/`
- **Telegram Dashboard:** `http://localhost:8000/telegram.html`
- **MAX / Index:** `http://localhost:8000/index.html`

### Настройка ngrok (для webhook)

Telegram и MAX требуют HTTPS-URL для webhook. Самый простой способ — ngrok:

```bash
# 1. Установи ngrok: https://ngrok.com/download

# 2. Авторизуйся (одноразово)
ngrok config add-authtoken YOUR_NGROK_AUTH_TOKEN

# 3. Запусти туннель к локальному серверу
ngrok http 8000
```

ngrok выдаст HTTPS-URL вида `https://abc123.ngrok-free.app`.

### Подключение webhook

1. Скопируй HTTPS-URL от ngrok (например `https://abc123.ngrok-free.app`)

2. **Вариант A — через веб-интерфейс:**
   - Открой `http://localhost:8000/telegram.html` → Settings → Webhook
   - Вставь URL ngrok и нажми Setup
   - Аналогично для MAX

3. **Вариант B — через API:**

   ```bash
   # Telegram
   curl "http://localhost:8000/api/dashboard/tg/setup-webhook?url=https://abc123.ngrok-free.app"

   # MAX
   curl "http://localhost:8000/api/dashboard/max/setup-webhook?url=https://abc123.ngrok-free.app"
   ```

4. **Вариант C — через `.env`** (автоматическая настройка при старте):
   ```env
   BASE_URL="https://abc123.ngrok-free.app"
   ```

### Полный цикл запуска (Windows PowerShell)

```powershell
# Терминал 1 — сервер
cd C:\path\to\chat-tracker-bot
$env:PYTHONUTF8="1"
python -m src.main

# Терминал 2 — ngrok туннель
ngrok http 8000
```

После запуска:

1. Скопируй HTTPS-URL из ngrok
2. Настрой webhook через UI или API (см. выше)
3. Настрой токен бота через дашборд (Settings → Bot Token) или через `/set_alert_chat` в боте

## Первый вход в Hub

1. Открой `http://localhost:8000/` (hub.html)
2. Введи свой Telegram ID (числовой)
3. Нажми "Запросить код" — бот пришлёт 6-значный код в личку
4. Введи код — ты автоматически станешь **admin** (первый пользователь)
5. Добавляй остальных пользователей через раздел Users

## Структура проекта

```
chat-tracker-bot/
├── prisma/
│   └── schema.prisma          # Модели данных (SQLite)
├── public/
│   ├── hub.html               # Главная страница хаба
│   ├── telegram.html          # Telegram дашборд
│   └── index.html             # MAX дашборд
├── src/
│   ├── api/
│   │   └── dashboard/         # API-роуты дашборда
│   │       ├── auth_routes.py
│   │       ├── tg_routes.py
│   │       ├── max_routes.py
│   │       ├── planfix_routes.py
│   │       ├── megapbx_routes.py
│   │       ├── settings_routes.py
│   │       └── history_routes.py
│   ├── bot/
│   │   └── handlers/          # Telegram-бот (aiogram)
│   ├── services/              # Бизнес-логика
│   │   ├── auth_service.py
│   │   ├── megapbx_service.py
│   │   ├── settings_service.py
│   │   ├── text_utils.py
│   │   └── chat_service.py
│   ├── tasks/
│   │   └── scheduler.py       # Фоновые задачи
│   ├── database/
│   │   ├── db.py              # Prisma-клиент
│   │   └── migrate_from_json.py  # Миграция с TG_Dashboard
│   ├── config.py
│   └── main.py                # FastAPI-приложение
├── .env
└── requirements.txt
```

## API Endpoints

| Модуль      | Префикс                     | Описание                  |
| ----------- | --------------------------- | ------------------------- |
| Auth        | `/api/dashboard/auth/*`     | Авторизация, пользователи |
| Telegram    | `/api/dashboard/tg/*`       | TG дашборд                |
| MAX         | `/api/dashboard/max/*`      | MAX дашборд               |
| PlanFix     | `/api/dashboard/planfix/*`  | Задачи PlanFix            |
| MegaPBX     | `/api/dashboard/megapbx/*`  | Звонки, сотрудники        |
| Settings    | `/api/dashboard/settings/*` | Настройки                 |
| History     | `/api/dashboard/history/*`  | Историческая статистика   |
| TG Webhook  | `/webhook/tg`               | Telegram webhook          |
| MAX Webhook | `/webhook/max`              | MAX webhook               |
| PBX Webhook | `/megapbx/webhook`          | MegaPBX webhook           |

## Миграция с TG_Dashboard

Если у тебя есть данные из старого `TG_Dashboard` (JSON-файлы):

```bash
$env:PYTHONUTF8="1"
python -m src.database.migrate_from_json
```

Скрипт импортирует данные из `TG_Dashboard/telegram_data.json` и `TG_Dashboard/max_data.json` в SQLite.
