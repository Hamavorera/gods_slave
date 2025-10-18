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
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
MESSAGE_ID_TO_EDIT = os.getenv("MESSAGE_ID_TO_EDIT")
# ❗️ Новая переменная для напоминаний
REMINDER_SECRET = os.getenv("REMINDER_SECRET", "default-secret-key")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")
else:
    model = None


# --- Вспомогательные функции ---

def parse_date_from_text(text: str) -> (str, str):
    """
    Ищет дату в тексте, возвращает (текст_без_даты, дата_в_ISO_формате_YYYY-MM-DD).
    Поддерживает форматы:
    - 31.10.2025
    - 31.10.25
    - 31.10 (подразумевает текущий или следующий год)
    """
    date_obj = None
    task_text = text

    # Сначала ищем полные даты (DD.MM.YYYY)
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4})', text)
    if match:
        date_str = match.group(1)
        try:
            date_obj = datetime.strptime(date_str, "%d.%m.%Y").date()
            task_text = text.replace(date_str, "").strip()
        except ValueError:
            pass  # Неверная дата

    # Затем ищем короткие даты (DD.MM.YY)
    if not date_obj:
        match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{2})', text)
        if match:
            date_str = match.group(1)
            try:
                date_obj = datetime.strptime(date_str, "%d.%m.%y").date()
                task_text = text.replace(date_str, "").strip()
            except ValueError:
                pass

    # Затем ищем даты без года (DD.MM)
    if not date_obj:
        match = re.search(r'(\d{1,2}\.\d{1,2})', text)
        if match:
            date_str = match.group(1)
            try:
                current_year = datetime.now().year
                date_obj = datetime.strptime(f"{date_str}.{current_year}", "%d.%m.%Y").date()
                # Если дата уже прошла в этом году, считаем, что это следующий год
                if date_obj < datetime.now().date():
                    date_obj = datetime.strptime(f"{date_str}.{current_year + 1}", "%d.%m.%Y").date()

                task_text = text.replace(date_str, "").strip()
            except ValueError:
                pass

    if date_obj:
        return task_text.strip(), date_obj.strftime("%Y-%m-%d")

    # Если дат не найдено
    return text.strip(), None


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
    """Читает закрепленное сообщение и возвращает список задач."""
    if not TARGET_CHAT_ID: return []
    try:
        chat_info = await context.bot.get_chat(chat_id=TARGET_CHAT_ID)
        if chat_info.pinned_message:
            return parse_tasks_from_text(chat_info.pinned_message.text)
        return []
    except Exception as e:
        print(f"Не удалось прочитать закрепленное сообщение: {e}")
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
        # ❗️ Сортировка по дате дедлайна. Задачи без даты - в конце.
        sorted_tasks = sorted(tasks, key=lambda x: datetime.strptime(x['deadline'], '%Y-%m-%d') if x[
            'deadline'] else datetime.max)
        for i, t in enumerate(sorted_tasks, start=1):
            line = t["task"]
            if t.get("deadline"):
                # ❗️ Ваша логика "раскраски" (она идеальна)
                date = datetime.strptime(t["deadline"], "%Y-%m-%d")
                days_left = (date.date() - now.date()).days
                if days_left < 0:
                    line = f"❌ ~{line}~ (просрочено)"
                elif days_left <= 2:
                    line = f"⚠️ *{line}* (осталось {days_left} дн.)"
                else:
                    line = f"{line} ({t['deadline']})"
            text += f"{i}. {line}\n"

    try:
        await context.bot.edit_message_text(text, chat_id=TARGET_CHAT_ID, message_id=MESSAGE_ID_TO_EDIT,
                                            parse_mode="Markdown")
    except error.BadRequest as e:
        if "message is not modified" not in str(e): print(f"Не удалось обновить сообщение: {e}")


# --- Команды ---

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    setup_msg = await update.message.reply_text("Создаю хранилище задач...")
    message_id_to_edit = setup_msg.message_id
    try:
        await context.bot.pin_chat_message(
            chat_id=user_id,
            message_id=message_id_to_edit,
            disable_notification=True
        )
    except Exception as e:
        await update.message.reply_text(f"Не удалось закрепить сообщение: {e}")
        return
    await setup_msg.edit_text(
        "**Это твое хранилище задач.**\n\n"
        "**Инструкция по настройке:**\n"
        "1. Зайди в переменные окружения на Render.\n"
        "2. `TARGET_CHAT_ID`:\n"
        f"`{user_id}`\n"
        "3. `MESSAGE_ID_TO_EDIT`:\n"
        f"`{message_id_to_edit}`\n"
        "4. (Для напоминаний) `REMINDER_SECRET`: придумай и впиши любой секретный ключ.\n"
        "5. Сохрани. Render перезапустит бота.\n\n"
        "Бот готов к работе."
    )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """❗️ ОБНОВЛЕННАЯ ФУНКЦИЯ ❗️"""
    tasks = await get_tasks_from_message(context)
    text = update.message.text.strip().lstrip('-').strip()

    # ❗️ Новая логика парсинга даты
    task_text, deadline_iso = parse_date_from_text(text)

    tasks.append({"task": task_text, "deadline": deadline_iso})
    await update_tasks_message(context, tasks)
    await update.message.delete()


async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = await get_tasks_from_message(context)
    try:
        index = int(context.args[0]) - 1
        # Сортируем задачи так же, как они отображаются, чтобы индексы совпадали
        sorted_tasks_with_indices = sorted(
            enumerate(tasks),
            key=lambda x: datetime.strptime(x[1]['deadline'], '%Y-%m-%d') if x[1]['deadline'] else datetime.max
        )
        if 0 <= index < len(tasks):
            # Получаем реальный индекс задачи из неотсортированного списка
            original_index = sorted_tasks_with_indices[index][0]
            # Удаляем по реальному индексу
            tasks.pop(original_index)
            await update_tasks_message(context, tasks)
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Неверный номер.", quote=False)
    await update.message.delete()


async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not model:
        await update.message.reply_text("Ключ Gemini API не настроен.")
        return
    if not context.args:
        await update.message.reply_text("❓ Напиши вопрос после команды /ask")
        return
    question = " ".join(context.args)
    prompt = f"Ответь на вопрос: {question}\n\nВАЖНО: Ответ должен быть кратким."
    waiting_msg = await update.message.reply_text("🤔 Думаю...")
    response = await model.generate_content_async(prompt)
    await waiting_msg.edit_text(response.text)


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


# --- ❗️ Часть 2: Эндпоинт для Напоминаний ❗️ ---
@api.post(f"/check_reminders/{REMINDER_SECRET}")
async def check_reminders_endpoint():
    """
    Эндпоинт, который будет дергать внешний cron-job.
    Проверяет задачи и отправляет напоминания.
    """
    print("Проверка напоминаний запущена...")
    if not (TARGET_CHAT_ID and application.bot):
        print("TARGET_CHAT_ID не установлен или бот не готов.")
        return Response(status_code=500, content="Bot not ready")

    bot = application.bot
    tasks = []
    try:
        chat_info = await bot.get_chat(chat_id=TARGET_CHAT_ID)
        if chat_info.pinned_message:
            tasks = parse_tasks_from_text(chat_info.pinned_message.text)
        else:
            return Response(status_code=200, content="No pinned message")
    except Exception as e:
        return Response(status_code=500, content=f"Error reading message: {e}")

    today = datetime.now().date()
    reminders_sent = []

    for task in tasks:
        if task.get("deadline"):
            try:
                deadline_date = datetime.strptime(task["deadline"], "%Y-%m-%d").date()
                days_left = (deadline_date - today).days

                # Отправляем напоминание, если дедлайн СЕГОДНЯ
                if days_left == 0:
                    reminder_text = f"❗️ **НАПОМИНАНИЕ (дедлайн сегодня):**\n{task['task']}"
                    await bot.send_message(chat_id=TARGET_CHAT_ID, text=reminder_text, parse_mode="Markdown")
                    reminders_sent.append(task['task'])

                # Отправляем напоминание, если дедлайн ЗАВТРА
                elif days_left == 1:
                    reminder_text = f"🔔 **НАПОМИНАНИЕ (дедлайн завтра):**\n{task['task']}"
                    await bot.send_message(chat_id=TARGET_CHAT_ID, text=reminder_text, parse_mode="Markdown")
                    reminders_sent.append(task['task'])
            except ValueError:
                continue

    if reminders_sent:
        return Response(status_code=200, content=f"Sent reminders for: {reminders_sent}")

    return Response(status_code=200, content="No reminders to send")
