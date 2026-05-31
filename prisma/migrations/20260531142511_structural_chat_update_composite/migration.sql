-- CreateTable
CREATE TABLE "SystemConfig" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "waitTimeoutMin" INTEGER NOT NULL DEFAULT 15,
    "alertChatId" TEXT NOT NULL,
    "updatedAt" DATETIME NOT NULL
);

-- CreateTable
CREATE TABLE "ActiveChat" (
    "chatId" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "clientName" TEXT NOT NULL,
    "chatTitle" TEXT NOT NULL,
    "externalChatUrl" TEXT NOT NULL,
    "lastMessage" TEXT NOT NULL,
    "status" TEXT NOT NULL,
    "isAlerted" BOOLEAN NOT NULL DEFAULT false,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "engineerId" INTEGER,

    PRIMARY KEY ("chatId", "userId"),
    CONSTRAINT "ActiveChat_engineerId_fkey" FOREIGN KEY ("engineerId") REFERENCES "Engineer" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

-- CreateTable
CREATE TABLE "Engineer" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "telegramId" TEXT NOT NULL,
    "username" TEXT NOT NULL,
    "name" TEXT NOT NULL
);

-- CreateTable
CREATE TABLE "AdminUser" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "telegramId" TEXT NOT NULL,
    "username" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- CreateTable
CREATE TABLE "IgnoredUser" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "username" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- CreateIndex
CREATE UNIQUE INDEX "Engineer_telegramId_key" ON "Engineer"("telegramId");

-- CreateIndex
CREATE UNIQUE INDEX "AdminUser_telegramId_key" ON "AdminUser"("telegramId");
