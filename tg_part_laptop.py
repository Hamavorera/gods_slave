import os
import re
import json
from datetime import datetime
from contextlib import asynccontextmanager
import logging 

# --- Новые импорты для парсера ---
import requests
from bs4 import BeautifulSoup

from telegram import Update, error, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request, Response
import google.generativeai as genai

# --- Настройка ---
logging.basicConfig(level=logging.INFO) # Включаем логи
TOKEN = os.getenv("TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
MESSAGE_ID_TO_EDIT = os.getenv("MESSAGE_ID_TO_EDIT")
REMINDER_SECRET = os.getenv("REMINDER_SECRET", "default-secret-key")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash") # Используем 1.5-flash
else:
    model = None

# --- Константы для парсера KSE ---
HOMEWORK_URL = 'https://teaching.kse.org.ua/course/view.php?id=3162'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

MOODLE_SESSION_COOKIE = os.getenv("MOODLE_SESSION_COOKIE")
COOKIES = {
    'MoodleSession': MOODLE_SESSION_COOKIE
} if MOODLE_SESSION_COOKIE else {}


# --- Парсер KSE ---
async def parse_homework() -> list[dict]:
    """
    Парсит сайт KSE, ищет активности с "quiz icon" и дедлайном,
    возвращает СПИСОК СЛОВАРЕЙ с задачами.
    """
    logging.info("Запускаю парсер для KSE (фильтр по quiz icon)...")
    
    if not COOKIES:
        logging.warning("MOODLE_SESSION_COOKIE не установлен. Парсинг будет в гостевом режиме.")

    try:
        response = requests.get(
            HOMEWORK_URL, 
            headers=HEADERS, 
            cookies=COOKIES,
            timeout=15
        )
        
        if response.status_code != 200:
            logging.error(f"Парсер KSE: Ошибка! Сайт вернул статус {response.status_code}")
            return [] 
        
        if 'login/index.php' in response.url:
            logging.error("Парсер KSE: Ошибка! Нас перекинуло на страницу логина. `MOODLE_SESSION_COOKIE` неверный или истек.")
            return [] 

        soup = BeautifulSoup(response.text, 'html.parser')
        weeks_container = soup.find('ul', class_='weeks')
        if not weeks_container:
            logging.warning("Парсер KSE: Не найден 'ul' с классом 'weeks'.")
            return []

        all_found_tasks = []
        sections = weeks_container.find_all('li', class_='section', recursive=False)

        for section in sections:
            section_title_element = section.find('h3', class_='sectionname')
            if not section_title_element:
                continue
            section_title = section_title_element.text.strip()
            
            tasks = section.find_all('li', class_='activity')
            if not tasks:
                continue

            for task in tasks:
                # --- ❗️❗️❗️ НОВАЯ ПРОВЕРКА: Ищем иконку квиза ❗️❗️❗️ ---
                quiz_icon = task.find('img', alt='quiz icon')
                if not quiz_icon:
                    continue # Если иконки нет, пропускаем эту активность

                # --- Если иконка есть, продолжаем как раньше ---
                task_name_element = task.find('span', class_='instancename')
                if not task_name_element:
                    continue
                
                task_name_clone = BeautifulSoup(str(task_name_element), 'html.parser')
                accesshide = task_name_clone.find('span', class_='accesshide')
                if accesshide:
                    accesshide.decompose()
                task_name = task_name_clone.text.strip()

                deadline_iso = None
                dates_div = task.find('div', {'data-region': 'activity-dates'})
                
                if dates_div:
                    date_lines = dates_div.find('div', class_='description-inner').find_all('div')
                    for line in date_lines:
                        # Ищем "Closed:", "Closes:", "Due:"
                        line_text = line.text.strip()
                        if line_text.startswith(("Closed:", "Closes:", "Due:")):
                            # Парсим дату 'Friday, 26 September 2025' или 'Monday, 27 October 2025'
                            # Учитываем день недели, запятую
                            date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', line_text)
                            if date_match:
                                try:
                                    date_str = date_match.group(1)
                                    # Используем правильный формат с %B для полного названия месяца
                                    deadline_obj = datetime.strptime(date_str, '%d %B %Y') 
                                    deadline_iso = deadline_obj.strftime('%Y-%m-%d')
                                except Exception as e:
                                    logging.error(f"Парсер KSE: Не смог спарсить дату '{date_str}' из строки '{line_text}': {e}")
                            break # Нашли нужную строку, выходим из цикла по строкам дат
                
                # Добавляем задачу, ТОЛЬКО если у нее есть иконка И дедлайн
                if deadline_iso:
                    full_task_name = f"KSE: {task_name} ({section_title})"
                    all_found_tasks.append({"task": full_task_name, "deadline": deadline_iso})

        logging.info(f"Парсер KSE: Найдено {len(all_found_tasks)} заданий с 'quiz icon' и дедлайнами.")
        return all_found_tasks

    except requests.exceptions.RequestException as e:
        logging.error(f"Парсер KSE: Ошибка сети: {e}")
        return []
    except Exception as e:
        logging.error(f"Парсер KSE: Неожиданная ошибка: {e}", exc_info=True)
        return []



# --- Вспомогательные функции ---

def parse_date_from_text(text: str) -> (str, str):
    date_obj = None
    task_text = text
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4})', text)
    if match:
        date_str = match.group(1)
        try:
            date_obj = datetime.strptime(date_str, "%d.%m.%Y").date()
            task_text = text.replace(date_str, "").strip()
        except ValueError:
            pass
    if not date_obj:
        match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{2})', text)
        if match:
            date_str = match.group(1)
            try:
                date_obj = datetime.strptime(date_str, "%d.%m.%y").date()
                task_text = text.replace(date_str, "").strip()
            except ValueError:
                pass
    if not date_obj:
        match = re.search(r'(\d{1,2}\.\d{1,2})', text)
        if match:
            date_str = match.group(1)
            try:
                current_year = datetime.now().year
                date_obj = datetime.strptime(f"{date_str}.{current_year}", "%d.%m.%Y").date()
                if date_obj < datetime.now().date():
                    date_obj = datetime.strptime(f"{date_str}.{current_year + 1}", "%d.%m.%Y").date()
                task_text = text.replace(date_str, "").strip()
            except ValueError:
                pass
    if date_obj:
        return task_text.strip(), date_obj.strftime("%Y-%m-%d")
    return text.strip(), None


def parse_tasks_from_text(text: str) -> list:
    if not text: return []
    tasks, lines = [], text.split('\n')[1:]
    pattern = re.compile(r'^\d+\.\s+(.*?)(?:\s+\(([^)]+)\))?$')
    for line in lines:
        line = line.strip().replace('❌ ~', '').replace('~ (просрочено)', '').replace('⚠️ *', '').replace('*', '')
        if not line: continue
        match = pattern.match(line)
        if match:
            task_text = match.group(1).strip()
            deadline_str_match = re.search(r'(\d{4}-\d{2}-\d{2})', match.group(2) or '')
            deadline_str = deadline_str_match.group(1) if deadline_str_match else None
            
            if 'KSE: ' in task_text and match.group(2):
                 task_text = f"{task_text} ({match.group(2)})"
                 
            tasks.append({"task": task_text, "deadline": deadline_str})
    return tasks


async def get_tasks_from_message(bot: Bot) -> list:
    if not TARGET_CHAT_ID: return []
    try:
        chat_info = await bot.get_chat(chat_id=TARGET_CHAT_ID)
        if chat_info.pinned_message:
            return parse_tasks_from_text(chat_info.pinned_message.text)
        return []
    except Exception as e:
        logging.error(f"Не удалось прочитать закрепленное сообщение: {e}")
        return []


async def update_tasks_message(bot: Bot, tasks: list):
    if not (TARGET_CHAT_ID and MESSAGE_ID_TO_EDIT):
        logging.error("Переменные ID не установлены. Обновление невозможно.")
        return

    text = "📋 *Список задач:*\n"
    if not tasks:
        text += "_Задач нет_"
    else:
        now = datetime.now()
        sorted_tasks = sorted(tasks, key=lambda x: datetime.strptime(x['deadline'], '%Y-%m-%d') if x[
            'deadline'] else datetime.max)
        
        for i, t in enumerate(sorted_tasks, start=1):
            line = t["task"]
            deadline_str = "" 
            
            if t.get("deadline"):
                date = datetime.strptime(t["deadline"], "%Y-%m-%d")
                days_left = (date.date() - now.date()).days
                
                if days_left < 0:
                    deadline_str = "(просрочено)"
                elif days_left == 0:
                    deadline_str = f"(⚠️ СЕГОДНЯ)"
                elif days_left <= 2:
                    deadline_str = f"(⚠️ осталось {days_left} дн.)"
                else:
                    deadline_str = f"({t['deadline']})"
                
                if 'KSE: ' in line and deadline_str:
                    line = re.sub(r'\s+\(.*\)$', '', line) 
                    line = f"{line} {deadline_str}" 
                elif deadline_str:
                     line = f"{line} {deadline_str}" 

                if days_left < 0:
                    line = f"❌ ~{line}~"
                elif days_left <= 2:
                    line = f"⚠️ *{line}*"
                    
            text += f"{i}. {line}\n"

    try:
        await bot.edit_message_text(text, chat_id=TARGET_CHAT_ID, message_id=MESSAGE_ID_TO_EDIT,
                                         parse_mode="Markdown")
    except error.BadRequest as e:
        if "message is not modified" not in str(e): 
            logging.error(f"Не удалось обновить сообщение: {e}")


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
        "5. (Для парсера ДЗ) `MOODLE_SESSION_COOKIE`: вставь свою куки-сессию из KSE.\n"
        "6. Сохрани. Render перезапустит бота.\n\n"
        "Бот готов к работе."
    )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = await get_tasks_from_message(context.bot) 
    text = update.message.text.strip().lstrip('-').strip()
    task_text, deadline_iso = parse_date_from_text(text)
    tasks.append({"task": task_text, "deadline": deadline_iso})
    await update_tasks_message(context.bot, tasks) 
    await update.message.delete()


async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = await get_tasks_from_message(context.bot) 
    if not tasks:
        await update.message.reply_text("❌ Список задач и так пуст.", quote=False)
        return

    text = update.message.text.strip()
    match = re.search(r'\d+', text)
    if not match:
        await update.message.reply_text("❌ Не могу понять, какой номер задачи удалить. Напиши 'удали 2'.", quote=False)
        await update.message.delete()
        return

    try:
        index = int(match.group(0)) - 1
        sorted_tasks_with_indices = sorted(
            enumerate(tasks),
            key=lambda x: datetime.strptime(x[1]['deadline'], '%Y-%m-%d') if x[1]['deadline'] else datetime.max
        )
        if 0 <= index < len(tasks):
            original_index = sorted_tasks_with_indices[index][0]
            removed_task = tasks.pop(original_index)
            await update_tasks_message(context.bot, tasks) 
        else:
            await update.message.reply_text(f"❌ Неверный номер! Сейчас в списке {len(tasks)} задач.", quote=False)
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Ошибка. Укажи корректный номер.", quote=False)

    await update.message.delete()


async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not model:
        await update.message.reply_text("Ключ Gemini API не настроен.")
        return
    question = update.message.text
    if not question:
        return
    prompt = f"Ответь на вопрос: {question}\n\nВАЖНО: Ответ должен быть кратким."
    waiting_msg = await update.message.reply_text("🤔 Думаю...")
    try:
        response = await model.generate_content_async(prompt)
        await waiting_msg.edit_text(response.text)
    except Exception as e:
        logging.error(f"Ошибка Gemini: {e}")
        await waiting_msg.edit_text(f"Произошла ошибка при запросе к Gemini: {e}")


# --- Настройка сервера FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if TOKEN: await application.initialize()
    yield
    if TOKEN: await application.shutdown()

api = FastAPI(lifespan=lifespan)
application = Application.builder().token(TOKEN).build()

# --- Обработчики ---
application.add_handler(CommandHandler("setup", setup))
application.add_handler(CommandHandler("ask", ask_gemini))
application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^(удали|Удали)'), remove_task))
application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^-'), add_task))
application.add_handler(MessageHandler(
    filters.TEXT &
    ~filters.COMMAND &
    ~filters.Regex(r'^(удали|Удали)') &
    ~filters.Regex(r'^-'),
    ask_gemini
))

URL_PATH = os.getenv("WEBHOOK_SECRET", "webhook")
@api.post(f"/{URL_PATH}")
async def process_telegram_update(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logging.error(f"Error processing update: {e}")
        return Response(status_code=500)


# --- ❗️❗️❗️ НОВЫЙ ЭНДПОИНТ-"БУДИЛЬНИК" ❗️❗️❗️ ---
@api.get("/health")
async def health_check():
    """
    Простой GET-эндпоинт, который "будит" сервис.
    Вызывается каждые 10-14 минут внешним cron-job (типа UptimeRobot).
    """
    logging.info("PING: Сервис 'разбудили'.")
    return Response(status_code=200, content='{"status": "alive"}')
# --- ❗️❗️❗️ КОНЕЦ НОВОГО БЛОКА ❗️❗️❗️ ---


# --- Эндпоинт для Напоминаний и Парсинга ---
@api.post(f"/check_reminders/{REMINDER_SECRET}")
async def check_reminders_and_parse_homework_endpoint():
    """
    Эндпоинт 2-в-1:
    1. Проверяет дедлайны и шлет напоминания.
    2. Парсит KSE, ищет новые ДЗ и добавляет их в список.
    Вызывается каждые 30-60 минут внешним cron-job (типа cron-job.org).
    """
    logging.info(f"CRON: Запуск проверки напоминаний и парсинга KSE...")
    if not (TARGET_CHAT_ID and application.bot):
        logging.error("CRON: TARGET_CHAT_ID не установлен или бот не готов.")
        return Response(status_code=500, content="Bot not ready")

    bot = application.bot
    
    try:
        current_tasks = await get_tasks_from_message(bot)
    except Exception as e:
         logging.error(f"CRON: Не смог получить задачи из сообщения: {e}")
         return Response(status_code=500, content=f"Error reading message: {e}")

    # --- 1. Логика напоминаний ---
    today = datetime.now().date()
    reminders_sent = []
    for task in current_tasks:
        if task.get("deadline"):
            try:
                deadline_date = datetime.strptime(task["deadline"], "%Y-%m-%d").date()
                days_left = (deadline_date - today).days
                
                if days_left == 0:
                    reminder_text = f"❗️ **НАПОМИНАНИЕ (дедлайн сегодня):**\n{task['task']}"
                    await bot.send_message(chat_id=TARGET_CHAT_ID, text=reminder_text, parse_mode="Markdown")
                    reminders_sent.append(task['task'])
                
                elif days_left == 1:
                    reminder_text = f"🔔 **НАПОМИНАНИЕ (дедлайн завтра):**\n{task['task']}"
                    await bot.send_message(chat_id=TARGET_CHAT_ID, text=reminder_text, parse_mode="Markdown")
                    reminders_sent.append(task['task'])
            except ValueError:
                continue 

    # --- 2. Логика Парсера KSE ---
    parser_message = ""
    try:
        new_hw_tasks = await parse_homework() 
        
        # --- 3. Логика слияния ---
        tasks_updated = False
        current_task_strings = {t['task'] for t in current_tasks}
        new_tasks_added_count = 0
        
        for new_task in new_hw_tasks:
            if new_task['task'] not in current_task_strings:
                current_tasks.append(new_task) 
                tasks_updated = True
                new_tasks_added_count += 1
                
        if tasks_updated:
            logging.info(f"CRON: Парсер KSE нашел {new_tasks_added_count} новых заданий. Обновляю список.")
            await update_tasks_message(bot, current_tasks) 
            parser_message = f"Parser added {new_tasks_added_count} new tasks."
        else:
            logging.info("CRON: Парсер KSE не нашел новых заданий.")
            parser_message = "Parser found no new tasks."

    except Exception as e:
        logging.error(f"CRON: Ошибка во время парсинга KSE: {e}", exc_info=True)
        parser_message = f"Parser failed: {e}"

    # --- 4. Финальный ответ для cron-job ---
    reminder_message = "No reminders sent"
    if reminders_sent:
        reminder_message = f"Sent reminders for: {reminders_sent}"
    
    logging.info(f"CRON: Проверка завершена. {reminder_message}. {parser_message}")
    return Response(status_code=200, content=f"{reminder_message}. {parser_message}")


