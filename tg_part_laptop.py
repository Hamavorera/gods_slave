import os
import re
import json
from datetime import datetime, date # Добавили date
from contextlib import asynccontextmanager
import logging
import time # Добавили time для замера времени

# --- Импорты для парсера ---
import requests
from bs4 import BeautifulSoup

from telegram import Update, error, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request, Response, BackgroundTasks # Добавили BackgroundTasks
import google.generativeai as genai

# --- Настройка ---
# Устанавливаем более подробный уровень логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__) # Используем именованный логгер

TOKEN = os.getenv("TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
MESSAGE_ID_TO_EDIT = os.getenv("MESSAGE_ID_TO_EDIT")
REMINDER_SECRET = os.getenv("REMINDER_SECRET", "default-secret-key")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        # Настраиваем safety settings, чтобы уменьшить вероятность блокировки
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]
        model = genai.GenerativeModel("gemini-2.5-flash", safety_settings=safety_settings)
        logger.info("Gemini модель успешно настроена.")
    except Exception as e:
        logger.error(f"Ошибка конфигурации Gemini: {e}")
        model = None
else:
    logger.warning("GEMINI_API_KEY не установлен.")
    model = None

# --- Константы для парсера KSE ---
HOMEWORK_URL = 'https://teaching.kse.org.ua/course/view.php?id=3162'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
MOODLE_SESSION_COOKIE = os.getenv("MOODLE_SESSION_COOKIE")
COOKIES = {'MoodleSession': MOODLE_SESSION_COOKIE} if MOODLE_SESSION_COOKIE else {}

# --- Парсер KSE (с проверкой дедлайна) ---
async def parse_homework() -> list[dict]:
    """
    Парсит сайт KSE, ищет НЕПРОСРОЧЕННЫЕ активности с "quiz icon" и дедлайном,
    возвращает СПИСОК СЛОВАРЕЙ с задачами.
    """
    logger.info("Запускаю парсер для KSE (фильтр по quiz icon и дате)...")
    start_time = time.time() # Замеряем время начала
    
    if not COOKIES:
        logger.warning("MOODLE_SESSION_COOKIE не установлен. Парсинг будет в гостевом режиме.")

    try:
        response = requests.get(HOMEWORK_URL, headers=HEADERS, cookies=COOKIES, timeout=20) # Увеличили таймаут
        response.raise_for_status() # Проверяем статус ответа (вызовет исключение для 4xx/5xx)

        if 'login/index.php' in response.url:
            logger.error("Парсер KSE: Ошибка! Перекинуло на страницу логина. `MOODLE_SESSION_COOKIE` неверный или истек.")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        weeks_container = soup.find('ul', class_='weeks')
        if not weeks_container:
            logger.warning("Парсер KSE: Не найден 'ul' с классом 'weeks'.")
            return []

        all_found_tasks = []
        sections = weeks_container.find_all('li', class_='section', recursive=False)
        today = date.today() # Получаем сегодняшнюю дату ОДИН раз

        for section in sections:
            section_title_element = section.find('h3', class_='sectionname')
            section_title = section_title_element.text.strip() if section_title_element else "Unknown Section"

            tasks = section.find_all('li', class_='activity')
            for task in tasks:
                quiz_icon = task.find('img', alt='quiz icon')
                if not quiz_icon: continue # Пропускаем, если не квиз

                task_name_element = task.find('span', class_='instancename')
                if not task_name_element: continue

                task_name_clone = BeautifulSoup(str(task_name_element), 'html.parser')
                accesshide = task_name_clone.find('span', class_='accesshide')
                if accesshide: accesshide.decompose()
                task_name = task_name_clone.text.strip()

                deadline_iso = None
                deadline_obj = None # Добавим объект даты для сравнения
                dates_div = task.find('div', {'data-region': 'activity-dates'})
                if dates_div:
                    date_lines = dates_div.find('div', class_='description-inner').find_all('div')
                    for line in date_lines:
                        line_text = line.text.strip()
                        # Ищем Closes или Due
                        if line_text.startswith(("Closed:", "Closes:", "Due:")):
                            date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', line_text)
                            if date_match:
                                try:
                                    date_str = date_match.group(1)
                                    # Используем английскую локаль для парсинга названий месяцев
                                    deadline_obj_dt = datetime.strptime(date_str, '%d %B %Y')
                                    deadline_obj = deadline_obj_dt.date() # Берем только дату
                                    deadline_iso = deadline_obj.strftime('%Y-%m-%d')
                                except ValueError as e: # Ловим конкретно ValueError
                                    logger.error(f"Парсер KSE: Не смог спарсить дату '{date_str}' (en): {e}.")
                                except Exception as e: # Ловим другие ошибки парсинга даты
                                    logger.error(f"Парсер KSE: Ошибка парсинга даты '{date_str}': {e}")
                            break # Нашли строку с датой, выходим

                # --- Проверка: Дедлайн еще не прошел? ---
                if deadline_iso and deadline_obj and deadline_obj >= today:
                    full_task_name = f"KSE: {task_name} ({section_title})"
                    all_found_tasks.append({"task": full_task_name, "deadline": deadline_iso})
                elif deadline_iso:
                     logger.debug(f"Парсер KSE: Пропущено просроченное задание '{task_name}' с дедлайном {deadline_iso}")


        end_time = time.time() # Замеряем время конца
        logger.info(f"Парсер KSE: Найдено {len(all_found_tasks)} актуальных заданий с 'quiz icon' за {end_time - start_time:.2f} сек.")
        return all_found_tasks

    except requests.exceptions.Timeout:
        logger.error("Парсер KSE: Ошибка! Истек таймаут при запросе к сайту.")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Парсер KSE: Ошибка сети: {e}")
        return []
    except Exception as e:
        logger.error(f"Парсер KSE: Неожиданная ошибка: {e}", exc_info=True)
        return []


# --- Вспомогательные функции ---

def parse_date_from_text(text: str) -> (str, str):
    # ... (код без изменений) ...
    date_obj = None
    task_text = text
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4})', text)
    if match:
        date_str = match.group(1)
        try:
            date_obj = datetime.strptime(date_str, "%d.%m.%Y").date()
            task_text = text.replace(date_str, "").strip()
        except ValueError: pass
    if not date_obj:
        match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{2})', text)
        if match:
            date_str = match.group(1)
            try:
                date_obj = datetime.strptime(date_str, "%d.%m.%y").date()
                task_text = text.replace(date_str, "").strip()
            except ValueError: pass
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
            except ValueError: pass
    if date_obj:
        return task_text.strip(), date_obj.strftime("%Y-%m-%d")
    return text.strip(), None


# --- ❗️❗️❗️ ПОЛНОСТЬЮ ПЕРЕПИСАННАЯ ФУНКЦИЯ ❗️❗️❗️ ---
def parse_tasks_from_text(text: str) -> list:
    """
    Парсит задачи из текста, корректно отделяя имя задачи от дедлайна.
    """
    if not text: 
        logger.info("parse_tasks_from_text: Получен пустой текст.")
        return []
        
    tasks = []
    lines = text.split('\n')
    
    # Регулярка для поиска строки задачи: "1. [ТЕКСТ ЗАДАЧИ] (ДЕДЛАЙН)"
    # Группа 1 (task_text): Все, после "N. " и до последних скобок.
    # Группа 2 (deadline_part): Опциональная часть в последних скобках.
    pattern = re.compile(r'^\d+\.\s+(.+?)(?:\s+\(([^)]*)\))?$')

    for line in lines:
        # Убираем Markdown-мусор
        cleaned_line = line.strip().replace('❌ ~', '').replace('~', '').replace('⚠️ *', '').replace('*', '')
        
        if not cleaned_line.startswith(tuple(f"{i}." for i in range(1, 200))):
             # Пропускаем строки, не начинающиеся с "N." (например, заголовок)
             continue
        
        match = pattern.match(cleaned_line)
        
        if match:
            task_text_base = match.group(1).strip() # "Базовое" имя
            deadline_part = match.group(2) # Содержимое скобок
            
            final_task_name = task_text_base
            final_deadline_str = None

            if deadline_part:
                # Ищем внутри скобок дату формата YYYY-MM-DD
                deadline_str_match = re.search(r'(\d{4}-\d{2}-\d{2})', deadline_part)
                if deadline_str_match:
                    # Нашли! Это дата.
                    final_deadline_str = deadline_str_match.group(1)
                    # Имя задачи - это "базовое" имя
                    final_task_name = task_text_base
                else:
                    # Это НЕ дата (например, "(просрочено)", "(⚠️ СЕГОДНЯ)" или "(Week 1)")
                    # В этом случае имя задачи - это все равно "базовое" имя.
                    # Мы НЕ добавляем `deadline_part` обратно к имени,
                    # потому что `update_tasks_message` добавит его сам.
                    final_task_name = task_text_base
                    
                    # Особый случай: если KSE задача была без дедлайна,
                    # ее имя в `task_text_base` может быть "KSE: Name"
                    # а в `deadline_part` - "(Section)".
                    # Нам нужно их склеить, чтобы получить уникальный ID.
                    if 'KSE: ' in task_text_base and not final_deadline_str:
                         # Проверяем, что в скобках НЕ динамический статус
                         if not (deadline_part.startswith("⚠️") or deadline_part == "просрочено"):
                              # Это, скорее всего, имя секции
                              final_task_name = f"{task_text_base} ({deadline_part})"

            tasks.append({"task": final_task_name, "deadline": final_deadline_str})
        
        elif line.strip() and not line.strip().startswith("📋"): # Логируем, если строка не пустая и не заголовок
             logger.warning(f"Не смог распарсить строку задачи: '{line.strip()}'")
             
    logger.info(f"parse_tasks_from_text: Найдено {len(tasks)} задач из текста.")
    return tasks


async def get_tasks_from_message(bot: Bot) -> list:
    if not TARGET_CHAT_ID: return []
    try:
        if not MESSAGE_ID_TO_EDIT:
             logger.error("MESSAGE_ID_TO_EDIT не установлен!")
             return []
        
        message = await bot.get_chat(chat_id=TARGET_CHAT_ID) 
        target_message_text = None
        
        # Проверяем ID сообщения в int
        message_id_int = 0
        try:
             message_id_int = int(MESSAGE_ID_TO_EDIT)
        except ValueError:
             logger.error(f"MESSAGE_ID_TO_EDIT ('{MESSAGE_ID_TO_EDIT}') не является корректным числом.")
             return []

        if message.pinned_message and message.pinned_message.message_id == message_id_int:
             target_message_text = message.pinned_message.text
             logger.info(f"Читаю задачи из закрепленного сообщения {message_id_int}.")
        else:
             try:
                 msg_obj = await bot.get_message(chat_id=TARGET_CHAT_ID, message_id=message_id_int)
                 target_message_text = msg_obj.text
                 logger.info(f"Читаю задачи из сообщения {message_id_int} (не закреплено).")
             except error.BadRequest as e:
                 logger.error(f"Не удалось получить сообщение по ID {message_id_int}: {e}. Возможно, оно удалено или ID неверен.")
                 if message.pinned_message:
                     logger.warning(f"Закрепленное сообщение ({message.pinned_message.message_id}) не совпадает с MESSAGE_ID_TO_EDIT ({message_id_int}).")
                 return [] 
                 
        if target_message_text:
            return parse_tasks_from_text(target_message_text)
        
        logger.warning(f"Текст сообщения {message_id_int} пуст.")
        return []
    except Exception as e:
        logger.error(f"Не удалось прочитать сообщение: {e}", exc_info=True)
        return []


async def update_tasks_message(bot: Bot, tasks: list):
    if not (TARGET_CHAT_ID and MESSAGE_ID_TO_EDIT):
        logger.error("Переменные ID не установлены. Обновление невозможно.")
        return
        
    # --- ❗️❗️❗️ НОВАЯ ЛОГИКА: Очистка от дубликатов перед обновлением ❗️❗️❗️ ---
    unique_tasks = []
    seen_task_names = set()
    for task in tasks:
        task_name = task.get('task')
        if task_name not in seen_task_names:
            unique_tasks.append(task)
            seen_task_names.add(task_name)
        else:
            logger.info(f"Обнаружен и удален дубликат задачи: '{task_name}'")
    
    # Обновляем `tasks` на отфильтрованный список
    tasks = unique_tasks 
    # --- Конец блока очистки ---

    text = "📋 *Список задач:*\n"
    if not tasks:
        text += "_Задач нет_"
    else:
        now = datetime.now()
        try:
            sorted_tasks = sorted(
                tasks,
                key=lambda x: (
                    datetime.strptime(x['deadline'], '%Y-%m-%d').date() if x.get('deadline') else date.max,
                    x.get('task', '') # Добавим .get для надежности
                )
            )
        except Exception as e:
            logger.error(f"Ошибка при сортировке задач: {e}", exc_info=True)
            sorted_tasks = tasks
            text += "\n⚠️ *Ошибка сортировки!* \n"

        for i, t in enumerate(sorted_tasks, start=1):
            line = t.get("task", "Без названия") 
            deadline_str_formatted = ""

            if t.get("deadline"):
                try:
                    deadline_dt = datetime.strptime(t["deadline"], "%Y-%m-%d")
                    deadline_d = deadline_dt.date()
                    days_left = (deadline_d - now.date()).days

                    if days_left < 0:
                        deadline_str_formatted = "(просрочено)"
                    elif days_left == 0:
                        deadline_str_formatted = f"(⚠️ СЕГОДНЯ)"
                    elif days_left <= 2:
                        deadline_str_formatted = f"(⚠️ осталось {days_left} дн.)"
                    else:
                        deadline_str_formatted = f"({t['deadline']})"

                    # Базовое имя - это УЖЕ `line`. KSE задачи уже имеют `(Section)` в имени.
                    # Нам не нужно ничего отрезать.
                    line = f"{line} {deadline_str_formatted}"

                    if days_left < 0:
                        line = f"❌ ~{line}~"
                    elif days_left <= 2:
                        line = f"⚠️ *{line}*"
                except ValueError:
                    logger.warning(f"Некорректная дата '{t['deadline']}' в задаче: {line}")
                    line = t.get("task", "Без названия") # Используем .get

            text += f"{i}. {line}\n"

    try:
        message_id_int = int(MESSAGE_ID_TO_EDIT) 
        await bot.edit_message_text(text, chat_id=TARGET_CHAT_ID, message_id=message_id_int,
                                         parse_mode="Markdown")
        logger.info(f"Сообщение {message_id_int} успешно обновлено. Новое кол-во задач: {len(tasks)}")
    except ValueError:
         logger.error(f"MESSAGE_ID_TO_EDIT ('{MESSAGE_ID_TO_EDIT}') не является корректным числом. Не могу обновить сообщение.")
    except error.BadRequest as e:
        if "message is not modified" not in str(e):
            logger.error(f"Не удалось обновить сообщение {MESSAGE_ID_TO_EDIT}: {e}")
        else:
            logger.info("Текст сообщения не изменился, пропуск обновления.")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при обновлении сообщения {MESSAGE_ID_TO_EDIT}: {e}", exc_info=True)


# --- Команды ---

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...
    user_id = update.message.from_user.id
    setup_msg = await update.message.reply_text("Создаю хранилище задач...")
    message_id_to_edit = setup_msg.message_id
    try:
        await context.bot.pin_chat_message(
            chat_id=user_id, message_id=message_id_to_edit, disable_notification=True
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
    # ... (код без изменений) ...
    tasks = await get_tasks_from_message(context.bot)
    text = update.message.text.strip().lstrip('-').strip()
    task_text, deadline_iso = parse_date_from_text(text)
    if not task_text: 
         logger.warning("Попытка добавить пустую задачу.")
         await update.message.delete()
         return
    tasks.append({"task": task_text, "deadline": deadline_iso})
    await update_tasks_message(context.bot, tasks)
    await update.message.delete()


# --- Команда удаления (Bulk Delete) ---
async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...
    tasks = await get_tasks_from_message(context.bot)
    if not tasks:
        await update.message.reply_text("❌ Список задач и так пуст.", quote=False)
        return

    text = update.message.text.strip()
    indices_to_remove_str = re.findall(r'\d+', text)

    if not indices_to_remove_str:
        await update.message.reply_text("❌ Не найдены номера задач для удаления. Напиши 'удали 2' или 'удали 1 3 5'.", quote=False)
        await update.message.delete()
        return

    try:
        indices_to_remove = {int(i) - 1 for i in indices_to_remove_str}
    except ValueError:
        await update.message.reply_text("❌ Ошибка. Укажи корректные номера.", quote=False)
        await update.message.delete()
        return

    sorted_tasks_with_indices = sorted(
        enumerate(tasks), 
        key=lambda x: (
            datetime.strptime(x[1]['deadline'], '%Y-%m-%d').date() if x[1].get('deadline') else date.max,
            x[1].get('task', '') 
        )
    )

    actual_indices_to_delete = set()
    removed_tasks_names = []
    invalid_indices = []

    for display_index in indices_to_remove:
        if 0 <= display_index < len(sorted_tasks_with_indices):
            original_index = sorted_tasks_with_indices[display_index][0]
            actual_indices_to_delete.add(original_index)
            removed_tasks_names.append(sorted_tasks_with_indices[display_index][1].get('task', '')) 
        else:
            invalid_indices.append(display_index + 1) 

    if invalid_indices:
        await update.message.reply_text(f"❌ Неверные номера: {', '.join(map(str, invalid_indices))}. Всего задач: {len(tasks)}.", quote=False)

    if not actual_indices_to_delete:
        await update.message.delete()
        return 

    # Создаем новый список задач, сохраняя порядок
    new_tasks = []
    original_indices_to_delete_sorted = sorted(list(actual_indices_to_delete), reverse=True) 
    
    temp_tasks = list(tasks) 
    
    for index_to_del in original_indices_to_delete_sorted:
         if 0 <= index_to_del < len(temp_tasks):
              del temp_tasks[index_to_del]
         else:
              logger.warning(f"Попытка удалить несуществующий индекс {index_to_del} при bulk delete.")
              
    new_tasks = temp_tasks

    await update_tasks_message(context.bot, new_tasks)
    
    if len(removed_tasks_names) == 1:
         confirmation_text = f"✅ Задача '{removed_tasks_names[0]}' удалена!"
    else:
         confirmation_text = f"✅ Удалено задач: {len(removed_tasks_names)}."
    
    # Не отвечаем, чтобы не засорять чат
    # await update.message.reply_text(confirmation_text, quote=False) 

    await update.message.delete()


# --- Команда Ask Gemini ---
async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...
    if not model:
        await update.message.reply_text("Ключ Gemini API не настроен.")
        return
    question = update.message.text
    if not question:
        logger.debug("Получено пустое сообщение для Gemini.")
        return

    logger.info(f"Получен вопрос для Gemini: '{question}'")
    prompt = f"Ответь на вопрос: {question}\n\nВАЖНО: Ответ до 2000 символов."
    waiting_msg = await update.message.reply_text("🤔 Думаю...")

    try:
        response = await model.generate_content_async(
             prompt,
             request_options={'timeout': 60} 
        )
        if response.parts:
             answer = "".join(part.text for part in response.parts)
             logger.info(f"Gemini ответил: '{answer[:50]}...'")
             await waiting_msg.edit_text(answer)
        else:
             logger.warning("Gemini вернул пустой ответ (возможно, сработали safety settings).")
             block_reason = ""
             if response.prompt_feedback and response.prompt_feedback.block_reason:
                 block_reason = f" Причина: {response.prompt_feedback.block_reason.name}"
             await waiting_msg.edit_text("Извините, не могу сгенерировать ответ на этот запрос.")


    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}", exc_info=True)
        await waiting_msg.edit_text("Произошла ошибка при обращении к AI.")


# --- Настройка сервера FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... (код с исправлением _initialized) ...
    logger.info("FastAPI приложение запускается (lifespan start)...")
    if TOKEN and application:
        try:
            if not application._initialized: # Use the private attribute
                await application.initialize()
                logger.info("Telegram Application инициализировано.")
            else:
                logger.info("Telegram Application уже было инициализировано.")
            
            webhook_url = os.getenv("RENDER_EXTERNAL_URL") 
            if webhook_url:
                 full_webhook_url = f"{webhook_url}/{URL_PATH}"
                 current_webhook = await application.bot.get_webhook_info()
                 if current_webhook.url != full_webhook_url:
                      logger.info(f"Устанавливаю вебхук: {full_webhook_url}")
                      # Устанавливаем вебхук, чтобы он принимал ТОЛЬКО 'message'
                      await application.bot.set_webhook(full_webhook_url, allowed_updates=["message"]) 
                 else:
                      logger.info(f"Вебхук уже установлен: {current_webhook.url}")
            else:
                 logger.warning("RENDER_EXTERNAL_URL не найден, не могу установить вебхук автоматически.")

        except Exception as e:
            logger.error(f"Ошибка инициализации Telegram Application или установки вебхука: {e}", exc_info=True)
    elif not TOKEN:
         logger.error("TOKEN не найден! Telegram Application не будет инициализировано.")

    logger.info("FastAPI приложение ГОТОВО к работе (после yield в lifespan).")
    yield 
    
    logger.info("FastAPI приложение останавливается (lifespan shutdown)...")
    if application and application._initialized: # Используем _initialized
        try:
            await application.shutdown()
            logger.info("Telegram Application остановлено.")
        except Exception as e:
            logger.error(f"Ошибка остановки Telegram Application: {e}", exc_info=True)
    logger.info("FastAPI приложение остановлено.")


api = FastAPI(lifespan=lifespan)
if TOKEN:
    try:
        application = Application.builder().token(TOKEN).build()
        logger.info("Telegram Application создано.")
    except Exception as e:
        logger.critical(f"Критическая ошибка при создании Telegram Application: {e}. Бот не будет работать.", exc_info=True)
        application = None 
else:
    logger.critical("Критическая ошибка: TOKEN не найден! Бот не будет работать.")
    application = None

# --- Обработчики ---
if application: 
    application.add_handler(CommandHandler("setup", setup))
    application.add_handler(CommandHandler("ask", ask_gemini))
    
    # --- ❗️❗️❗️ ИСПРАВЛЕННЫЙ РЕГЕКС (Fix 2) ❗️❗️❗️ ---
    # Используем [Уу] вместо (?i)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^[Уу]дали\s+(\d+\s*)+$'), remove_task))
    
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^-'), add_task))
    
    application.add_handler(MessageHandler(
        filters.TEXT &
        ~filters.COMMAND &
        # --- ❗️❗️❗️ ИСПРАВЛЕННЫЙ РЕГЕКС (Fix 2) ❗️❗️❗️ ---
        ~filters.Regex(r'^[Уу]дали\s+(\d+\s*)+$') & 
        ~filters.Regex(r'^-'),
        ask_gemini
    ))
    logger.info("Обработчики Telegram добавлены.")
else:
    logger.error("Хэндлеры Telegram не будут добавлены, так как Application не инициализировано.")

URL_PATH = os.getenv("WEBHOOK_SECRET", "webhook")
@api.post(f"/{URL_PATH}")
async def process_telegram_update(request: Request):
    # ... (код без изменений) ...
    if not application:
        logger.error("Получен Telegram update, но Application не инициализировано.")
        return Response(status_code=500, content="Bot not initialized")
    try:
        data = await request.json()
        logger.debug(f"Получен Telegram update: {data}") 
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Ошибка обработки Telegram update: {e}", exc_info=True)
        return Response(status_code=500)


# --- Эндпоинт-"будильник" /health ---
@api.get("/health")
async def health_check():
    # ... (код без изменений) ...
    logger.info("PING: /health вызван.")
    if application and application._initialized:
        return Response(status_code=200, content='{"status": "alive"}')
    else:
        logger.error("Health check: Telegram Application не инициализировано!")
        return Response(status_code=503, content='{"status": "initializing_or_failed"}')


# --- Эндпоинт для Напоминаний и Парсинга ---
@api.post(f"/check_reminders/{REMINDER_SECRET}")
async def check_reminders_and_schedule_parse(background_tasks: BackgroundTasks):
    # ... (код без изменений) ...
    endpoint_start_time = time.time()
    logger.info(f"CRON: Запуск /check_reminders...")
    if not (TARGET_CHAT_ID and application and application.bot):
        logger.error("CRON: /check_reminders - Необходимые компоненты не готовы.")
        return Response(status_code=503, content="Bot not ready or not configured")

    bot = application.bot
    reminders_sent_count = 0

    try:
        if not MESSAGE_ID_TO_EDIT or not MESSAGE_ID_TO_EDIT.isdigit():
             logger.error(f"CRON: /check_reminders - MESSAGE_ID_TO_EDIT ('{MESSAGE_ID_TO_EDIT}') неверен.")
             return Response(status_code=500, content="MESSAGE_ID_TO_EDIT not configured correctly")
             
        current_tasks = await get_tasks_from_message(bot) 
        
        # --- 1. Логика напоминаний ---
        today = date.today()
        for task in current_tasks:
            if task.get("deadline"):
                try:
                    deadline_date = datetime.strptime(task["deadline"], "%Y-%m-%d").date()
                    days_left = (deadline_date - today).days
                    reminder_text = None
                    if days_left == 0:
                        reminder_text = f"❗️ **НАПОМИНАНИЕ (дедлайн сегодня):**\n{task.get('task', 'Название отсутствует')}"
                    elif days_left == 1:
                        reminder_text = f"🔔 **НАПОМИНАНИЕ (дедлайн завтра):**\n{task.get('task', 'Название отсутствует')}"
                    
                    if reminder_text:
                        await bot.send_message(chat_id=TARGET_CHAT_ID, text=reminder_text, parse_mode="Markdown")
                        reminders_sent_count += 1
                except ValueError: continue
                except Exception as e: logger.error(f"CRON: Ошибка отправки напоминания для '{task.get('task', '?')}': {e}")

        logger.info(f"CRON: /check_reminders - Напоминания проверены ({reminders_sent_count} отправлено).")

        # --- 2. Добавляем ПАРСИНГ в фон ---
        background_tasks.add_task(run_parser_and_update, bot, current_tasks)
        logger.info("CRON: /check_reminders - Задача парсинга добавлена в фон.")

        endpoint_duration = time.time() - endpoint_start_time
        logger.info(f"CRON: /check_reminders - Эндпоинт завершил работу за {endpoint_duration:.2f} сек.")
        
        return Response(status_code=200, content=f"Reminders checked ({reminders_sent_count} sent). Parser scheduled.")

    except Exception as e:
        logger.error(f"CRON: /check_reminders - Критическая ошибка: {e}", exc_info=True)
        return Response(status_code=500, content=f"Error in reminder check: {e}")

# --- Функция фоновой задачи парсинга ---
async def run_parser_and_update(bot: Bot, current_tasks: list):
    """
    Эта функция выполняется в ФОНЕ.
    Она парсит KSE, сравнивает задачи и обновляет сообщение.
    """
    logger.info("BG_TASK: Запуск фонового парсинга KSE...")
    task_start_time = time.time()
    parser_message = ""
    try:
        new_hw_tasks = await parse_homework() # Использует обновленный парсер

        # --- Логика слияния ---
        tasks_updated = False
        # --- ❗️❗️❗️ ИСПОЛЬЗУЕМ СЕТ ИЗ ФИКСИРОВАННОЙ ФУНКЦИИ ---
        current_task_strings = {t.get('task') for t in current_tasks if t.get('task')} 
        new_tasks_added_count = 0

        for new_task in new_hw_tasks:
            new_task_name = new_task.get('task')
            if new_task_name and new_task_name not in current_task_strings:
                current_tasks.append(new_task) # Добавляем в список, который будет передан в update
                tasks_updated = True
                new_tasks_added_count += 1
            elif not new_task_name:
                 logger.warning("BG_TASK: Парсер вернул задачу без имени.")
            elif new_task_name in current_task_strings:
                 logger.info(f"BG_TASK: Задача '{new_task_name}' уже есть в списке, пропуск.")


        if tasks_updated:
            logger.info(f"BG_TASK: Парсер KSE нашел {new_tasks_added_count} новых заданий. Обновляю список...")
            if not MESSAGE_ID_TO_EDIT or not MESSAGE_ID_TO_EDIT.isdigit():
                 logger.error(f"BG_TASK: MESSAGE_ID_TO_EDIT ('{MESSAGE_ID_TO_EDIT}') неверен. Не могу обновить сообщение.")
            else:
                 # Передаем обновленный current_tasks (включая старые и новые)
                 await update_tasks_message(bot, current_tasks) 
            parser_message = f"Parser added {new_tasks_added_count} new tasks."
        else:
            logger.info("BG_TASK: Парсер KSE не нашел новых актуальных заданий.")
            parser_message = "Parser found no new tasks."

    except Exception as e:
        logger.error(f"BG_TASK: Ошибка во время фонового парсинга KSE: {e}", exc_info=True)
        parser_message = f"Background parser failed: {e}"
    
    task_duration = time.time() - task_start_time
    logger.info(f"BG_TASK: Фоновый парсинг завершен за {task_duration:.2f} сек. {parser_message}")


# --- Точка входа ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080)) 
    logger.info(f"Запуск Uvicorn локально на порту {port}...")
    # Указываем `main:api` как строку, чтобы reload работал
    config = uvicorn.Config(app="main:api", host="0.0.0.0", port=port, lifespan="on", reload=True) 
    server = uvicorn.Server(config)
    
    import asyncio
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")


