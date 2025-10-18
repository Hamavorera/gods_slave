# tg_part_laptop.py

import os
import re
import json
from datetime import datetime
from contextlib import asynccontextmanager

from telegram import Update, error
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request, Response
import google.generativeai as genai

# --- Настройка ---
TOKEN = os.getenv("TOKEN")
# ID вашего личного чата с ботом (ваш user_id)
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
# ID сообщения, которое бот будет редактировать
MESSAGE_ID_TO_EDIT = os.getenv("MESSAGE_ID_TO_EDIT")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")
else:
    model = None

# --- Вспомогательные функции ---

def parse_tasks_from_text(text: str) -> list:
    """Парсит задачи из текста."""
    if not text: return []
    tasks, lines = [], text.split('\n')[1:]
    pattern = re.compile(r'^\d+\.\s+(.*?)(?:\s+\(([^)]+)\))?$')
    for line in lines:
        line = line.strip().replace('❌ ~', '').replace('~ (просрочено)', '').replace('⚠️ *', '').replace('*', '')
        if not line: continue
        match = pattern.match(line)
        if match:
            task_text = match.group(1).strip()
            deadline_str = match.group(2) if match.group(2) and re.match(r'\d{4}-\d{2}-\d{2}', match.group(2)) else None
            tasks.append({"task": task_text, "deadline": deadline_str})
    return tasks

async def get_tasks_from_message(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Читает сообщение-хранилище и возвращает список задач."""
    if not (TARGET_CHAT_ID and MESSAGE_ID_TO_EDIT): return []
    try:
        msg = await context.bot.get_message(chat_id=TARGET_CHAT_ID, message_id=MESSAGE_ID_TO_EDIT)
        return parse_tasks_from_text(msg.text)
    except Exception as e:
        print(f"Не удалось прочитать сообщение с задачами: {e}")
        return []

async def update_tasks_message(context: ContextTypes.DEFAULT_TYPE, tasks: list):
    """Обновляет текст сообщения-хранилища."""
    if not (TARGET_CHAT_ID and MESSAGE_ID_TO_EDIT):
        print("Переменные ID не установлены. Обновление невозможно.")
        return
    
    text = "📋 *Список задач:*\n"
    if not tasks:
        text += "_Задач нет_"
    else:
        now = datetime.now()
        sorted_tasks = sorted(tasks, key=lambda x: datetime.strptime(x['deadline'], '%Y-%m-%d') if x['deadline'] else datetime.max)
        for i, t in enumerate(sorted_tasks, start=1):
            line = t["task"]
            if t.get("deadline"):
                date = datetime.strptime(t["deadline"], "%Y-%m-%d")
                days_left = (date.date() - now.date()).days
                if days_left < 0: line = f"❌ ~{line}~ (просрочено)"
                elif days_left <= 2: line = f"⚠️ *{line}* (осталось {days_left} дн.)"
                else: line = f"{line} ({t['deadline']})"
            text += f"{i}. {line}\n"

    try:
        await context.bot.edit_message_text(text, chat_id=TARGET_CHAT_ID, message_id=MESSAGE_ID_TO_EDIT, parse_mode="Markdown")
    except error.BadRequest as e:
        if "message is not modified" not in str(e): print(f"Не удалось обновить сообщение: {e}")

# --- Команды ---

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для первоначальной настройки."""
    # ID вашего личного чата - это ID пользователя, который пишет боту
    user_id = update.message.from_user.id
    
    setup_msg = await update.message.reply_text("Это будет твое новое хранилище задач.")
    # ID сообщения, которое мы будем редактировать - это ID сообщения, которое отправил сам бот
    message_id_to_edit = setup_msg.message_id
    
    await setup_msg.edit_text(
        "**Это твое хранилище задач.**\n\n"
        "**Инструкция по настройке:**\n"
        "1. Зайди в переменные окружения на Render.\n"
        "2. Создай/обнови переменную `TARGET_CHAT_ID` вот этим значением:\n"
        f"`{user_id}`\n"
        "3. Создай/обнови переменную `MESSAGE_ID_TO_EDIT` вот этим значением:\n"
        f"`{message_id_to_edit}`\n"
        "4. Сохрани. Render сам перезапустит бота.\n"
        "5. После этого можешь для удобства закрепить это сообщение."
    )

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = await get_tasks_from_message(context)
    text = update.message.text.strip().lstrip('-').strip()
    tasks.append({"task": text, "deadline": None}) # Упрощено, добавьте свою логику даты
    await update_tasks_message(context, tasks)
    await update.message.delete()

async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = await get_tasks_from_message(context)
    try:
        index = int(context.args[0]) - 1
        sorted_tasks_original_indices = sorted(range(len(tasks)), key=lambda k: datetime.strptime(tasks[k]['deadline'], '%Y-%m-%d') if tasks[k]['deadline'] else datetime.max)
        if 0 <= index < len(tasks):
            task_index_to_remove = sorted_tasks_original_indices[index]
            tasks.pop(task_index_to_remove)
            await update_tasks_message(context, tasks)
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Неверный номер.", quote=False)
    await update.message.delete()

async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Напиши вопрос после команды /ask")
        return

    question = " ".join(context.args)
    # Ограничение длины
    prompt = f"Ответь на вопрос: {question}\n\nВАЖНО: Ответ должен быть кратким и не превышать 3500 символов."

    waiting_msg = await update.message.reply_text("🤔 Думаю...")

    # Вызов Gemini
    response = await model.generate_content_async(prompt)
    answer = response.text

    await waiting_msg.delete()

    # Отправка длинного сообщения
    await send_long_message(context, update.message.chat_id, answer)

# --- Настройка сервера FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN: await application.initialize()
    yield
    if TOKEN: await application.shutdown()

api = FastAPI(lifespan=lifespan)
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("setup", setup))
application.add_handler(CommandHandler("remove", remove_task))
application.add_handler(CommandHandler("ask", ask_gemini))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))

URL_PATH = os.getenv("WEBHOOK_SECRET", "webhook")
@api.post(f"/{URL_PATH}")
async def process_telegram_update(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        print(f"Error processing update: {e}")
        return Response(status_code=500)


