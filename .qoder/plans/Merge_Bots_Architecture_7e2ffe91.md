# Merge Bots: Unified Architecture Plan

## Final Architecture

```
Telegram API ──(webhook)──┐
                          ▼
MAX API ──────(webhook)──┐
                          ▼
              ┌───────────────────────┐
              │  FastAPI + aiogram     │  (single Python server)
              │  ├── /webhook/tg       │  Telegram webhook endpoint
              │  ├── /webhook/max      │  MAX webhook endpoint
              │  ├── Bot commands      │  Only /set_alert_chat binding
              │  ├── Dashboard API     │  Ported from Express
              │  ├── Static files      │  hub.html, telegram.html, etc.
              │  └── Scheduler         │  Alert loop + pending timeouts
              └───────────┬───────────┘
                          │
                    Prisma / SQLite
                    (single source of truth)
```

---

## Task 1: Extend Prisma Schema

Add new models to `prisma/schema.prisma` for all settings previously stored in JSON files.

**File: `prisma/schema.prisma`** -- add:

```prisma
model BotSettings {
  id               Int      @id @default(autoincrement())
  // Telegram
  tgBotToken       String   @default("")
  tgOwnerId        Int?                          // from business_connection
  tgTeamIds        String   @default("[]")        // JSON array of ints
  // MAX
  maxBotToken      String   @default("")
  maxTeamIds       String   @default("[]")        // JSON array of ints
  // Shared settings
  waitTimeoutMin   Int      @default(15)
  missedThreshold  Int      @default(900)         // seconds (15 min)
  pendingTimeout   Int      @default(1800)        // seconds (30 min)
  alertChatId      String   @default("")
  // Phrase lists (JSON arrays stored as strings)
  noReplyPhrases   String   @default("[]")
  pendingPhrases   String   @default("[]")
  positiveKeywords String   @default("[]")
  negativeKeywords String   @default("[]")
  updatedAt        DateTime @updatedAt
}

model Conversation {
  id               Int       @id @default(autoincrement())
  messenger        String                         // "telegram" | "max"
  chatId           String
  source           String                         // "private" | "group"
  clientName       String    @default("")
  clientUsername   String?
  lastClientMsgAt  Int?                           // unix timestamp
  lastClientMsgText String?
  lastAgentMsgAt   Int?
  lastAgentName    String?
  status           String    @default("waiting")  // waiting | answered
  isPending        Boolean   @default(false)
  pendingAt        Int?
  isNegative       Boolean   @default(false)
  isPositive       Boolean   @default(false)
  hasControl       Boolean   @default(false)
  hasViolation     Boolean   @default(false)
  msgCount         Int       @default(0)
  createdAt        Int                            // unix timestamp
  updatedAt        DateTime  @updatedAt
  @@unique([messenger, chatId])
}

model ChatMessage {
  id               Int       @id @default(autoincrement())
  messenger        String                         // "telegram" | "max"
  msgId            String?
  chatId           String
  direction        String                         // "in" | "out"
  text             String    @default("")
  agentName        String?
  sentAt           Int                            // unix timestamp
  hasPhoto         Boolean   @default(false)
  createdAt        DateTime  @default(now())
  @@index([chatId, messenger])
  @@index([sentAt])
}

model GroupMessage {
  id               Int       @id @default(autoincrement())
  messenger        String
  groupId          String                         // group chat ID
  msgId            String?
  fromId           Int
  fromName         String    @default("")
  text             String    @default("")
  sentAt           Int
  answered         Boolean   @default(false)
  answeredAt       Int?
  isTeam           Boolean   @default(false)
  isPendingReply   Boolean   @default(false)
  createdAt        DateTime  @default(now())
  @@index([groupId, messenger])
  @@index([sentAt])
}

model MissedEvent {
  id               Int       @id @default(autoincrement())
  messenger        String
  chatId           String
  clientName       String    @default("")
  clientUsername   String?
  lastMsg          String    @default("")
  waitedSeconds    Int
  missedAt         Int                            // unix timestamp
  source           String    @default("timeout")  // timeout | pending_expired
  @@index([missedAt])
}

model ResolvedTask {
  id               Int       @id @default(autoincrement())
  messenger        String    @default("telegram")
  chatId           String
  clientName       String    @default("")
  taskType         String    @default("")
  description      String    @default("")
  objectId         String?
  planfixTaskId    String?
  planfixTaskUrl   String?
  timeSpentSec     Int       @default(0)
  resolvedAt       Int                            // unix timestamp
  isNegative       Boolean   @default(false)
}

model Control {
  id               String    @id                  // ctrl_{chatId}_{timestamp}
  messenger        String    @default("telegram")
  chatId           String
  clientName       String    @default("")
  action           String
  responsible      String    @default("")
  deadline         String?
  messageText      String    @default("")
  done             Boolean   @default(false)
  doneAt           Int?
  createdAt        Int
}

model Violation {
  id               String    @id                  // {chatId}_{timestamp}
  messenger        String    @default("telegram")
  chatId           String
  clientName       String    @default("")
  employeeName     String
  comment          String    @default("")
  messageText      String    @default("")
  recordedAt       Int
}

model GroupInfo {
  id               Int       @id @default(autoincrement())
  messenger        String
  groupId          String
  title            String    @default("")
  createdAt        Int
  @@unique([messenger, groupId])
}

model DailyStats {
  id               Int       @id @default(autoincrement())
  messenger        String
  date             String                         // YYYY-MM-DD
  waiting          Int?
  avgResponseSec   Int?
  todayChats       Int?
  incomingToday    Int?
  agentMsgToday    Int?
  timeSpentSec     Int?
  missedToday      Int?
  pendingNow       Int?
  byAgent          String    @default("{}")       // JSON
  recordedAt       Int
  @@unique([messenger, date])
}

model PlanFixConfig {
  id               Int       @id @default(autoincrement())
  token            String    @default("")
  apiBase          String    @default("https://restu.planfix.ru/rest")
  supportGroupId   Int       @default(185860)
  templateId       Int       @default(24609)
  updatedAt        DateTime  @updatedAt
}
```

**File: `prisma/schema.prisma`** -- update `SystemConfig` to be deprecated in favor of `BotSettings`.

After schema changes: `prisma generate` + `prisma db push`.

---

## Task 2: Refactor main.py -- Polling to Webhook

Convert from aiogram polling to webhook mode.

**File: `src/main.py`**
- Replace `dp.start_polling(bot)` with aiogram webhook setup
- Add FastAPI app with webhook route `/webhook/tg` that feeds updates to aiogram dispatcher
- Mount static files for dashboard (`public/` directory)
- Add MAX webhook route `/webhook/max`
- Register dashboard API routers
- Keep scheduler task

```python
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from aiogram import types
import uvicorn

app = FastAPI()

# Telegram webhook
@app.post("/webhook/tg")
async def tg_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}

# MAX webhook
@app.post("/webhook/max")
async def max_webhook(request: Request):
    # handle MAX update
    ...

# Dashboard static files
app.mount("/static", StaticFiles(directory="public"), name="static")

# Dashboard API routers (ported from Express)
app.include_router(dashboard_router)
```

---

## Task 3: Create Dashboard API Module (port Express routes to FastAPI)

**New file: `src/api/dashboard/__init__.py`**

Create FastAPI routers that mirror the Express endpoints, reading/writing to Prisma DB instead of JSON files:

| Express Route | FastAPI Route | Description |
|---|---|---|
| `GET /api/telegram/chats` | `GET /api/dashboard/tg/chats` | Queue + conversations list |
| `GET /api/telegram/stats` | `GET /api/dashboard/tg/stats` | KPI stats |
| `GET /api/telegram/chat-messages/:id` | `GET /api/dashboard/tg/chat-messages/{chatId}` | Chat history |
| `POST /api/telegram/dismiss/:id` | `POST /api/dashboard/tg/dismiss/{chatId}` | Mark answered |
| `POST /api/telegram/resolve` | `POST /api/dashboard/tg/resolve` | Record resolved task |
| `GET /api/telegram/resolved-tasks` | `GET /api/dashboard/tg/resolved-tasks` | List resolved |
| `GET/POST .../positive-keywords` | `GET/POST /api/dashboard/tg/positive-keywords` | Keyword config |
| `GET/POST .../negative-keywords` | `GET/POST /api/dashboard/tg/negative-keywords` | Keyword config |
| `GET/POST .../no-reply-phrases` | `GET/POST /api/dashboard/tg/no-reply-phrases` | Phrase config |
| `GET/POST .../pending-phrases` | `GET/POST /api/dashboard/tg/pending-phrases` | Phrase config |
| `POST /api/telegram/control` | `POST /api/dashboard/tg/control` | Create control |
| `GET /api/telegram/controls` | `GET /api/dashboard/tg/controls` | List controls |
| `POST /api/telegram/violation` | `POST /api/dashboard/tg/violation` | Record violation |
| `GET /api/telegram/violations` | `GET /api/dashboard/tg/violations` | List violations |
| `GET /api/telegram/missed` | `GET /api/dashboard/tg/missed` | Missed events |
| `GET /api/telegram/report-week` | `GET /api/dashboard/tg/report-week` | Weekly report |
| `GET/POST .../bot-config` | `GET/POST /api/dashboard/tg/bot-config` | Token management |
| `GET/POST .../team-ids` | `GET/POST /api/dashboard/tg/team-ids` | Team ID config |
| `GET .../webhook-info` | `GET /api/dashboard/tg/webhook-info` | Webhook status |
| `GET .../setup-webhook` | `GET /api/dashboard/tg/setup-webhook` | Set webhook URL |
| `GET /api/telegram/events` | `GET /api/dashboard/tg/events` | SSE stream |
| `POST /api/telegram/reset` | `POST /api/dashboard/tg/reset` | Wipe data |
| All `/api/max/*` routes | `/api/dashboard/max/*` | Same for MAX |
| `GET /api/stats` | `GET /api/dashboard/planfix/stats` | PlanFix stats |
| `GET /api/tasks/active` | `GET /api/dashboard/planfix/tasks/active` | PlanFix tasks |
| `GET /api/history/:messenger` | `GET /api/dashboard/history/{messenger}` | Daily KPI archive |

**New files:**
- `src/api/dashboard/tg_routes.py` -- Telegram dashboard endpoints
- `src/api/dashboard/max_routes.py` -- MAX dashboard endpoints  
- `src/api/dashboard/planfix_routes.py` -- PlanFix proxy endpoints
- `src/api/dashboard/settings_routes.py` -- Global settings (tokens, phrases, team IDs)
- `src/api/dashboard/sse.py` -- SSE broadcast (replace Node.js `sseClients` Set with asyncio Queue)

---

## Task 4: Port Telegram Webhook Handler (message processing logic)

**New file: `src/services/tg_handler.py`**

Port the core message processing logic from `TG_Dashboard/server.js` lines 400-580 into the existing aiogram handlers in `src/bot/handlers/common.py`. Merge the two processing pipelines:

Key logic to merge:
- **Business messages**: Combine `handle_business_messages` (src) with `business_message` handler (TG_Dashboard). Add: sentiment detection (positive/negative keywords), pending phrase detection, no-reply phrase filtering, agent tag parsing `[Name]`.
- **Group messages**: Combine `handle_group_messages` (src) with `update.message` handler (TG_Dashboard). Add: team ID check alongside engineer DB check, pending reply tracking, no-reply filtering.
- **Reactions**: Keep existing `handle_message_reaction` (src), add owner-based fallback from TG_Dashboard.

**Unification of agent identification:**
- Check `Engineer` table in DB first (self-registered via `/start`)
- Also check `BotSettings.tgTeamIds` (configured via dashboard)
- Union of both = "is team member"

---

## Task 5: Create MAX Handler Module

**New file: `src/services/max_handler.py`**

Port MAX webhook processing from `TG_Dashboard/server.js` lines 1432-1539 to Python.

Key functions:
- `handle_max_update(update)` -- process `message_created` events
- Distinguish `dialog` (private) vs `chat`/`channel` (group) 
- Apply same logic: team ID check, no-reply filtering, pending detection, sentiment
- Store to Prisma `Conversation`, `ChatMessage`, `GroupMessage` tables

**New file: `src/services/max_api.py`**
- `max_api_call(method, path, body)` -- async HTTP client to `https://platform-api.max.ru`
- `send_message(chat_id, text)` -- POST /messages
- `setup_webhook(url)` -- POST /subscriptions
- `get_me()` -- GET /me
- Authorization via header: `Authorization: <token>`

---

## Task 6: Port Scheduler and Background Tasks

**File: `src/tasks/scheduler.py`** -- extend existing scheduler:

1. **Telegram alert loop** (existing `check_forgotten_chats_loop`) -- adapt to read from `Conversation` table + `BotSettings`
2. **Pending timeout checker** (port from TG_Dashboard `setInterval` lines 1318-1373) -- check `isPending` conversations past `pendingTimeout`, create `MissedEvent` records
3. **MAX pending timeout checker** -- same logic for MAX conversations
4. **Daily KPI archiver** (port from TG_Dashboard `archiveCatchUp` lines 2099-2140) -- compute daily stats, store in `DailyStats` table

---

## Task 7: Update Bot Commands

**File: `src/bot/handlers/admin.py`**

Remove all settings commands that move to web dashboard:
- REMOVE: `/set_timeout` (now via web settings page)
- REMOVE: `/add_admin`, `/del_admin` (now via web settings page)
- REMOVE: `/ignore_user`, `/unignore_user`, `/ignore_list` (now via web settings page)
- REMOVE: `/add_alert_user`, `/del_alert_user`, `/alert_users_list` (now via web settings page)
- KEEP: `/set_alert_chat` -- binds alert chat (requires being IN the group, cannot be done via web)
- KEEP: `/help` -- update help text to reflect web dashboard
- KEEP: `/start` -- engineer registration

**File: `src/bot/handlers/common.py`**

- KEEP: `/start`, `/active`, inline query handlers, reaction handler
- MERGE: business_message + group_message handlers with TG_Dashboard logic (Task 4)

---

## Task 8: Update Dashboard HTML

**Files: `TG_Dashboard/public/telegram.html`, `hub.html`, `index.html`**

Update API endpoint URLs in the frontend JavaScript:
- `/api/telegram/*` -> `/api/dashboard/tg/*`
- `/api/max/*` -> `/api/dashboard/max/*`
- `/api/stats` -> `/api/dashboard/planfix/stats`
- `/api/history/*` -> `/api/dashboard/history/*`

Move files to project root `public/` directory for FastAPI static serving.

---

## Task 9: Config and Environment Updates

**File: `src/config.py`** -- simplify:
```python
class Settings(BaseSettings):
    DATABASE_URL: str
    SUPER_ADMIN_ID: str
    # Tokens are now in BotSettings DB table, managed via web UI
    # TELEGRAM_BOT_TOKEN removed from .env
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    BASE_URL: str = ""  # for webhook setup (e.g., https://your-domain.com)
```

**File: `.env`** -- remove `TELEGRAM_BOT_TOKEN`, add `BASE_URL`.

**File: `src/bot/dispatcher.py`** -- read token from DB at startup:
```python
# Token loaded from BotSettings table, not .env
bot = Bot(token=get_tg_token_from_db(), ...)
```

---

## Task 10: Startup and Migration Script

**New file: `src/database/migrate_from_json.py`**

One-time script to import data from `TG_Dashboard/telegram_data.json` and `TG_Dashboard/max_data.json` into Prisma DB. Maps:
- `conversations` -> `Conversation` table
- `messages` -> `ChatMessage` table
- `groupMessages` -> `GroupMessage` table
- `missedEvents` -> `MissedEvent` table
- `resolvedTasks` -> `ResolvedTask` table
- `controls` -> `Control` table
- `violations` -> `Violation` table
- Settings/phrases/teamIds -> `BotSettings` table

**File: `src/main.py`** -- add startup logic:
- Initialize `BotSettings` row if none exists (with defaults from TG_Dashboard)
- Auto-setup Telegram webhook on startup
- Auto-setup MAX webhook on startup (if token configured)

---

## Execution Order

1. Task 1 (schema) -- foundation for everything
2. Task 9 (config) -- simplify env vars
3. Task 2 (main.py refactor) -- FastAPI skeleton
4. Task 4 (merge TG handlers) -- core bot logic
5. Task 5 (MAX handler) -- new messenger support
6. Task 3 (dashboard API) -- port Express routes
7. Task 6 (scheduler) -- background tasks
8. Task 7 (bot commands cleanup)
9. Task 8 (frontend URL updates)
10. Task 10 (migration script)

## Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Webhook requires HTTPS | Use ngrok/cloudflare tunnel for dev; production server has HTTPS |
| Token change breaks existing bot | Migration script reads current .env token into BotSettings |
| Large data migration from JSON | Chunked inserts, run once during maintenance window |
| SSE in FastAPI (different from Express) | Use `sse-starlette` package or `StreamingResponse` with asyncio |
| aiogram webhook + FastAPI on same port | aiogram 3.x supports `SimpleWebhookServer` or manual feed via `dp.feed_update()` |
| Frontend API URL changes break dashboard | Update all URLs in JS in one pass; test each endpoint |
