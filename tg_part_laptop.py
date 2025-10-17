from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import json
import os
import google.generativeai as genai
import asyncio
from asgiref.wsgi import WsgiToAsgi
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()
TOKEN = os.getenv("TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

TASKS = []
MAIN_CHAT_ID = None

async def _get_pinned_message_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    global MAIN_CHAT_ID
    if MAIN_CHAT_ID is None:
        return ""

    try:
        chat_info = await context.bot.get_chat(chat_id=MAIN_CHAT_ID)
        if chat_info.pinned_message:
            return chat_info.pinned_message.text
    except Exception:
        # Если чат не найден или нет доступа
        return ""
    return ""

def get_tasks():
    global TASKS
    return TASKS


import re


def _parse_tasks_from_text(text: str) -> list:
    """Разбирает текст закрепленного сообщения обратно в список задач."""
    tasks = []
    # Удаляем заголовок и разбиваем по строкам
    lines = text.split('\n')[1:]

    # Регулярное выражение для поиска номера, текста и дедлайна
    # Оно пытается найти: [Номер]. [Текст задачи] ( [Дата/Статус] )
    pattern = re.compile(r'^\d+\.\s+(.*?)(?:\s+\(([^)]+)\))?$')

    for line in lines:
        line = line.strip()
        if not line or line.startswith('_'):  # Пропускаем пустые строки и "_Задач нет_"
            continue

        # Убираем Markdown и эмодзи-статусы, которые мы добавляем
        line = line.replace('❌ ~', '').replace('~ (просрочено)', '').replace('⚠️ *', '').replace('*', '')

        match = pattern.match(line)
        if match:
            # Текст задачи (группа 1)
            task_text = match.group(1).strip()
            # Дедлайн или статус (группа 2)
            deadline_or_status = match.group(2)

            deadline = None
            if deadline_or_status:
                # Если это дата в формате ГГГГ-ММ-ДД, используем ее
                if re.match(r'\d{4}-\d{2}-\d{2}', deadline_or_status):
                    deadline = deadline_or_status
                # Если это просто статус ("просрочено", "осталось N дн."), игнорируем его и оставляем пустую дату

            tasks.append({"task": task_text, "deadline": deadline})

    return tasks

# Эта функция будет возвращать глобальное состояние
def get_state():
    global STATE
    return STATE


# ========== Обработчики команд ==========

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass

    text = update.message.text.strip()
    if not text.startswith("-"):
        return



    task_text = text[1:].strip()
    deadline = None
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


    # ❗️ 1. ЧИТАЕМ СТАРЫЙ СПИСОК ИЗ ЗАКРЕПЛЕННОГО СООБЩЕНИЯ
    pinned_text = await _get_pinned_message_text(context)
    tasks = _parse_tasks_from_text(pinned_text) # <-- Обновляем список из текста

    # 2. ДОБАВЛЯЕМ НОВУЮ ЗАДАЧУ
    tasks.append({"task": task_text, "deadline": deadline})

    # 3. ОБНОВЛЯЕМ ЗАКРЕПЛЕННОЕ СООБЩЕНИЕ
    await update_task_message(context, tasks) # Передаем актуальный список

    await update.message.reply_text("✅ Задача добавлена!")


async def update_task_message(context: ContextTypes.DEFAULT_TYPE, tasks: list = None):
        global MAIN_CHAT_ID
        chat_id = MAIN_CHAT_ID
        try:
            chat_info = await context.bot.get_chat(chat_id=chat_id)
        except:
            pass
        # Если список не передан, загружаем его из закрепленного сообщения
        if tasks is None:
            pinned_text = await _get_pinned_message_text(context)
            tasks = _parse_tasks_from_text(pinned_text)

        # ... (Весь остальной код остается прежним, используя переданный аргумент tasks) ...

        if MAIN_CHAT_ID is None:
            return

        chat_id = MAIN_CHAT_ID
        message_id = None

        if chat_info.pinned_message:
            message_id = chat_info.pinned_message.message_id
        try:
            chat_info = await context.bot.get_chat(chat_id=chat_id)
            if chat_info.pinned_message:
                message_id = chat_info.pinned_message.message_id
        except Exception:
            return

        if message_id is None:
            return

            # 4. Формируем текст списка (Используем tasks, который был передан/загружен)
        text = "📋 *Список задач:*\n"
        if tasks:
            now = datetime.now()
            # ... (Ваша логика формирования текста) ...
            for i, t in enumerate(tasks, start=1):
                line = t["task"]
                # ... (Код обработки дедлайна) ...
                if t.get("deadline"):
                    try:
                        date = datetime.strptime(t["deadline"], "%Y-%m-%d")
                        days_left = (date - now).days

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

        # 5. Редактируем закрепленное сообщение
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            if "message is not modified" not in str(e):
                raise




    # 4. Формируем текст списка (КОД ОСТАЕТСЯ ПРЕЖНИМ)
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

        # 5. Редактируем закрепленное сообщение
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
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

    global MAIN_CHAT_ID

    if MAIN_CHAT_ID is None:
        MAIN_CHAT_ID = update.message.chat_id
        await update.message.reply_text("✅ ID чата сохранен. Теперь бот будет искать закрепленное сообщение в этом чате.")

    # 2. Пытаемся обновить сообщение (оно само создаст новое, если нет закрепленного)
    await update_task_message(context)


# ========================  УДАЛЕНИЕ ЗАДАЧ  ========================


async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        await update.message.delete()
    except:
        pass


    # ... (Ваш код проверки номера) ...
    if not context.args:
        await update.message.reply_text("❌ Укажи номер задачи для удаления")
        return

    try:
        index = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Укажи корректный номер задачи")
        return

    # ❗️ 1. ЧИТАЕМ СТАРЫЙ СПИСОК ИЗ ЗАКРЕПЛЕННОГО СООБЩЕНИЯ
    pinned_text = await _get_pinned_message_text(context)
    tasks = _parse_tasks_from_text(pinned_text)

    # 2. УДАЛЯЕМ ЗАДАЧУ
    if 0 <= index < len(tasks):
        tasks.pop(index)

        # 3. ОБНОВЛЯЕМ ЗАКРЕПЛЕННОЕ СООБЩЕНИЕ
        await update_task_message(context, tasks)  # Передаем актуальный список
        await update.message.reply_text("✅ Задача удалена!")
    else:
        await update.message.reply_text(f"Неверный номер! Сейчас в списке {len(tasks)} задач.")











WEBHOOK_URL = "https://gods-slave.onrender.com/"
SECRET_TOKEN = os.getenv("WEBHOOK_SECRET")
application = Application.builder().token(TOKEN).build()


async def init_application():
    await application.initialize()


try:
    asyncio.run(init_application())
except RuntimeError as e:
    if 'cannot run' in str(e).lower():
        pass
    else:
        raise

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("remove", remove_task))
application.add_handler(CommandHandler("ask", ask_gemini))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))

app = Flask(__name__)
asgi_app = WsgiToAsgi(app)

@app.route('/')
def home():
    return "I'm alive!"


@app.route("/" + SECRET_TOKEN, methods=["POST"])
async def webhook_handler():
    """Обрабатывает входящие обновления от Telegram."""
    update = Update.de_json(request.get_json(), application.bot)
    await application.process_update(update)
    return jsonify({"status": "ok"})







