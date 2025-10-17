import re

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import os
import google.generativeai as genai
import asyncio


# Глобальная переменная для хранения ID чата, где находится закрепленное сообщение
MAIN_CHAT_ID = None
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ======================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ========================

async def _get_pinned_message_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Находит закрепленное сообщение и возвращает его текст."""
    global MAIN_CHAT_ID
    if MAIN_CHAT_ID is None:
        return ""

    try:
        chat_info = await context.bot.get_chat(chat_id=MAIN_CHAT_ID)
        if chat_info.pinned_message:
            return chat_info.pinned_message.text
    except Exception:
        # Ошибка, если бот не имеет доступа или чат удален
        return ""
    return ""


def _parse_tasks_from_text(text: str) -> list:
    """Разбирает текст закрепленного сообщения обратно в список задач."""
    tasks = []
    # Удаляем заголовок и разбиваем по строкам
    lines = text.split('\n')[1:]

    # Шаблон для поиска: [Номер]. [Текст задачи] ( [Дата/Статус] )
    pattern = re.compile(r'^\d+\.\s+(.*?)(?:\s+\(([^)]+)\))?$')

    for line in lines:
        line = line.strip()
        if not line or line.startswith('_'):
            continue

        # Удаляем временную разметку
        line = line.replace('❌ ~', '').replace('~ (просрочено)', '').replace('⚠️ *', '').replace('*', '')

        match = pattern.match(line)
        if match:
            task_text = match.group(1).strip()
            deadline_or_status = match.group(2)

            deadline = None
            if deadline_or_status and re.match(r'\d{4}-\d{2}-\d{2}', deadline_or_status):
                deadline = deadline_or_status

            tasks.append({"task": task_text, "deadline": deadline})

    return tasks


async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, prefix: str = "💡 "):
    """Отправляет длинное сообщение, разбивая его на части."""
    MAX_LENGTH = 4096
    while text:
        chunk = text[:MAX_LENGTH]
        text = text[MAX_LENGTH:]

        if prefix:
            chunk = prefix + chunk
            prefix = ""

        await context.bot.send_message(chat_id=chat_id, text=chunk)


# ... (Код add_task, update_task_message, ask_gemini, start, remove_task) ...

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

    # 1. ЧИТАЕМ СТАРЫЙ СПИСОК ИЗ ЗАКРЕПЛЕННОГО СООБЩЕНИЯ
    pinned_text = await _get_pinned_message_text(context)
    tasks = _parse_tasks_from_text(pinned_text)

    # 2. ДОБАВЛЯЕМ НОВУЮ ЗАДАЧУ
    tasks.append({"task": task_text, "deadline": deadline})

    # 3. ОБНОВЛЯЕМ ЗАКРЕПЛЕННОЕ СООБЩЕНИЕ
    await update_task_message(context, tasks)

    await update.message.reply_text("✅ Задача добавлена!")


async def update_task_message(context: ContextTypes.DEFAULT_TYPE, tasks: list = None):
    global MAIN_CHAT_ID

    # Если список не передан, загружаем его из закрепленного сообщения
    if tasks is None:
        pinned_text = await _get_pinned_message_text(context)
        tasks = _parse_tasks_from_text(pinned_text)

    if MAIN_CHAT_ID is None:
        return

    chat_id = MAIN_CHAT_ID
    message_id = None

    # Получаем ID закрепленного сообщения
    try:
        chat_info = await context.bot.get_chat(chat_id=chat_id)
        if chat_info.pinned_message:
            message_id = chat_info.pinned_message.message_id
    except Exception:
        return

    # Логика создания сообщения, если оно не закреплено
    if message_id is None:
        new_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="📋 *Список задач:*\n_Задач нет_",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ **Пожалуйста, немедленно ЗАКРЕПИТЕ это сообщение.** Бот будет его обновлять.",
            reply_to_message_id=new_msg.message_id,
            parse_mode="Markdown"
        )
        return

    # 4. Формируем текст списка (используя tasks)
    # ... (ВАШ КОД ФОРМИРОВАНИЯ ТЕКСТА) ...
    text = "📋 *Список задач:*\n"
    if tasks:
        now = datetime.now()
        for i, t in enumerate(tasks, start=1):
            line = t["task"]
            if t.get("deadline"):
                try:
                    date = datetime.strptime(t["deadline"], "%Y-%m-%d")
                    days_left = (date - now).days

                    if days_left < 0:
                        line = f"❌ ~{line}~ (просрочено)"
                    elif days_left <= 2:
                        line = f"⚠️ *{line}* (осталось {days_left} дн.)"
                    else:
                        # Сохраняем дату в формате YYYY-MM-DD для парсинга
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


async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Напиши вопрос после команды /ask")
        return

    question = " ".join(context.args)
    # Ограничение длины
    prompt = f"Ответь на вопрос: {question}\n\nВАЖНО: Ответ должен быть кратким и не превышать 3500 символов."

    waiting_msg = await update.message.reply_text("🤔 Думаю...")

    # Вызов Gemini
    response = await asyncio.to_thread(model.generate_content, prompt)
    answer = response.text

    await waiting_msg.delete()

    # Отправка длинного сообщения
    await send_long_message(context, update.message.chat_id, answer)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass
    global MAIN_CHAT_ID

    # 1. Сохраняем ID чата для дальнейшего использования
    if MAIN_CHAT_ID is None:
        MAIN_CHAT_ID = update.message.chat_id
        await update.message.reply_text(
            "✅ ID чата сохранен. Теперь бот будет искать закрепленное сообщение в этом чате.")

    # 2. Пытаемся обновить сообщение (оно само создаст новое, если нет закрепленного)
    await update_task_message(context)


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

    # 1. ЧИТАЕМ СТАРЫЙ СПИСОК ИЗ ЗАКРЕПЛЕННОГО СООБЩЕНИЯ
    pinned_text = await _get_pinned_message_text(context)
    tasks = _parse_tasks_from_text(pinned_text)

    # 2. УДАЛЯЕМ ЗАДАЧУ
    if 0 <= index < len(tasks):
        tasks.pop(index)

        # 3. ОБНОВЛЯЕМ ЗАКРЕПЛЕННОЕ СООБЩЕНИЕ
        await update_task_message(context, tasks)
        await update.message.reply_text("✅ Задача удалена!")
    else:
        await update.message.reply_text(f"Неверный номер! Сейчас в списке {len(tasks)} задач.")


# Конфигурация Webhook
PORT = int(os.environ.get('PORT', '8080'))
URL_PATH = os.getenv("WEBHOOK_SECRET")
WEBHOOK_URL = f"https://gods-slave.onrender.com/{URL_PATH}" # Замените домен, если он другой
SECRET_TOKEN = os.getenv("WEBHOOK_SECRET")

TOKEN = os.getenv("TOKEN")
application = Application.builder().token(TOKEN).build()
# ❗️ Не нужно вызывать initialize() или asyncio.run(), это сделает run_webhook


def main():
    """Запускает бота."""
    print("Бот запускается в режиме вебхука...")

    # 1. Создаем экземпляр Application
    application = Application.builder().token(TOKEN).build()

    # 2. Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remove", remove_task))
    application.add_handler(CommandHandler("ask", ask_gemini))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))

    # 3. Запускаем вебхук. Эта функция сама управляет циклом asyncio.
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=URL_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=SECRET_TOKEN
    )

if __name__ == "__main__":
    # ❗️ Uvicorn будет вызывать main()
    main()
