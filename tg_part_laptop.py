from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import json
import os
import google.generativeai as genai
import asyncio

from dotenv import load_dotenv
from flask import Flask, request, jsonify


load_dotenv()
TOKEN = os.getenv("TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# Файлы для хранения данных
TASKS = []
STATE = {}

# Эта функция будет возвращать глобальный список задач
def get_tasks():
    global TASKS
    return TASKS

# Эта функция будет возвращать глобальное состояние
def get_state():
    global STATE
    return STATE

# ========== Обработчики команд ==========
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Удаляем сообщение пользователя
    try:
        await update.message.delete()
    except:
        pass

    text = update.message.text.strip()
    if not text.startswith("-"):
        return

    task_text = text[1:].strip()
    deadline = None

    # Поиск даты (дд.мм или дд.мм.гг)
    for part in task_text.split():
        try:
            if len(part.split(".")) == 3:
                deadline = datetime.strptime(part, "%d.%m.%y").strftime("%Y-%m-%d")
                task_text = task_text.replace(part, "").strip()
            elif len(part.split(".")) == 2:
                year = datetime.now().year
                deadline = datetime.strptime(f"{part}.{year}", "%d.%m.%Y").strftime("%Y-%m-%d")
                task_text = task_text.replace(part, "").strip()
        except ValueError:
            pass

    global TASKS 
    
    tasks = TASKS 
    tasks.append({"task": task_text, "deadline": deadline})

    await update_task_message(context)
    await update.message.reply_text("✅ Задача добавлена!")


async def update_task_message(context: ContextTypes.DEFAULT_TYPE):
    tasks = TASKS
    state = STATE
    if not state:
        return

    text = "📋 *Список задач:*\n"
    if tasks:
        now = datetime.now()
        for i, t in enumerate(tasks, start=1):
            line = t["task"]
            if t.get("deadline"):
                try:
                    date = datetime.strptime(t["deadline"], "%Y-%m-%d")
                    days_left = (date - now).days

                    # Подсветка
                    if days_left < 0:
                        line = f"❌ ~{line}~ (просрочено)"
                    elif days_left <= 2:
                        line = f"⚠️ *{line}* (осталось {days_left} дн.)"
                    else:
                        line = f"{line} ({t['deadline']})"
                except Exception:
                    line = f"{line} ({t['deadline']})"
            text += f"{i}. {line}\n"
    else:
        text += "_Задач нет_"

    await context.bot.edit_message_text(
        chat_id=state["chat_id"],
        message_id=state["message_id"],
        text=text,
        parse_mode="Markdown"
    )
async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Напиши вопрос после команды /ask")
        return

    question = " ".join(context.args)
    waiting_msg = await update.message.reply_text("🤔 Думаю...")

    # вызываем Gemini в отдельном потоке, чтобы не блокировать event loop
    response = await asyncio.to_thread(model.generate_content, question)
    answer = response.text

    await waiting_msg.delete()  # убираем "Думаю..."
    await update.message.reply_text(f"💡 {answer}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Удаляем команду /start
    try:
        await update.message.delete()
    except:
        pass

    global STATE # Объявляем, что будем менять глобальную переменную
    
    # state = load_state() <--- УДАЛИТЬ
    state = STATE # <--- ЗАМЕНИТЬ
    
    if not state:
        msg = await update.message.reply_text("📋 *Список задач:*\n_Задач нет_", parse_mode="Markdown")
        # save_state({"chat_id": msg.chat_id, "message_id": msg.message_id}) <--- УДАЛИТЬ
        STATE = {"chat_id": msg.chat_id, "message_id": msg.message_id} # <--- ЗАМЕНИТЬ
    else:
        await update_task_message(context)


# ========================  УДАЛЕНИЕ ЗАДАЧ  ========================
async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Удаляем команду /remove
    try:
        await update.message.delete()
    except:
        pass

    if not context.args:
        await update.message.reply_text("❌ Укажи номер задачи для удаления")
        return

    try:
        index = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Укажи корректный номер задачи")
        return

    global TASKS # Объявляем, что будем менять глобальную переменную

    # tasks = load_tasks() <--- УДАЛИТЬ
    tasks = TASKS # <--- ЗАМЕНИТЬ
    
    if 0 <= index < len(tasks):
        tasks.pop(index)
        # save_tasks(tasks) <--- УДАЛИТЬ
        await update.message.reply_text("✅ Задача удалена!")
        await update_task_message(context)
    else:
        await update.message.reply_text("Неверный номер!")


WEBHOOK_URL = "https://your-hosting-domain.com/webhook"
SECRET_TOKEN = os.getenv("WEBHOOK_SECRET")
application = Application.builder().token(TOKEN).build()

application.initialize() 

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("remove", remove_task))
application.add_handler(CommandHandler("ask", ask_gemini))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))

app = Flask(__name__)

@app.route('/')
def home():
    return "I'm alive!"

@app.route("/" + SECRET_TOKEN, methods=["POST"])
async def webhook_handler():
    """Обрабатывает входящие обновления от Telegram."""
    update = Update.de_json(request.get_json(), application.bot)
    await application.process_update(update)
    return jsonify({"status": "ok"})



# Ваш хостинг-провайдер должен предоставить инструкцию,
# как настроить WSGI-приложение, например, через cPanel.

# Там нужно будет указать, что точкой входа является "app".





