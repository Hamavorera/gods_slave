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
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
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
                        if line_text.startswith(("Closed:", "Closes:", "Due:")):
                            date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', line_text)
                            if date_match:
                                try:
                                    date_str = date_match.group(1)
                                    # Используем английскую локаль для парсинга названий месяцев
                                    # Это может потребовать установки локали на сервере, но requests/BS4 обычно справляются
                                    deadline_obj_dt = datetime.strptime(date_str, '%d %B %Y')
                                    deadline_obj = deadline_obj_dt.date() # Берем только дату
                                    deadline_iso = deadline_obj.strftime('%Y-%m-%d')
                                except ValueError as e: # Ловим конкретно ValueError
                                    # Попробуем русский формат месяца на всякий случай
                                    try:
                                        # Эта часть может потребовать настройки локали на сервере Render
                                        import locale
                                        try:
                                            locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
                                        except locale.Error:
                                            logger.warning("Не удалось установить русскую локаль, парсинг русских месяцев может не работать.")
                                            
                                        deadline_obj_dt = datetime.strptime(date_str, '%d %B %Y')
                                        deadline_obj = deadline_obj_dt.date() 
                                        deadline_iso = deadline_obj.strftime('%Y-%m-%d')
                                    except ValueError:
                                         logger.error(f"Парсер KSE: Не смог спарсить дату '{date_str}' (ни en, ни ru): {e}")
                                except Exception as e: # Ловим другие ошибки парсинга даты
                                    logger.error(f"Парсер KSE: Ошибка парсинга даты '{date_str}': {e}")
                            break

                # --- ❗️❗️❗️ НОВАЯ ПРОВЕРКА: Дедлайн еще не прошел? ❗️❗️❗️ ---
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
    # ... (твой код не трогаю)
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


def parse_tasks_from_text(text: str) -> list:
    # ... (твой код не трогаю)
    if not text: return []
    tasks, lines = [], text.split('\n')[1:] # Начинаем со второй строки
    # Паттерн ищет номер, текст задачи, и опционально дату в скобках в конце
    pattern = re.compile(r'^\d+\.\s+(.*?)(?:\s+\(([\d\-]+|\w+[\s\w]*)\))?$') 
    for line in lines:
        cleaned_line = line.strip().replace('❌ ~', '').replace('~', '').replace('⚠️ *', '').replace('*', '')
        # Убираем специфичные строки дедлайна перед матчингом
        cleaned_line = re.sub(r'\s+\(просрочено\)$', '', cleaned_line)
        cleaned_line = re.sub(r'\s+\(⚠️ СЕГОДНЯ\)$', '', cleaned_line)
        cleaned_line = re.sub(r'\s+\(⚠️ осталось \d+ дн\.\)$', '', cleaned_line)
        
        if not cleaned_line: continue
        
        match = pattern.match(cleaned_line)
        if match:
            task_text = match.group(1).strip()
            deadline_part = match.group(2)
            
            # Ищем дату YYYY-MM-DD внутри скобок или отдельно
            deadline_str_match = re.search(r'(\d{4}-\d{2}-\d{2})', deadline_part or '')
            deadline_str = deadline_str_match.group(1) if deadline_str_match else None
            
            # Восстанавливаем оригинальную строку для KSE, если была дата
            # KSE задачи теперь не будут иметь дату в скобках при парсинге из текста, т.к. ее формат другой
            if 'KSE: ' in task_text and deadline_part and not deadline_str:
                 # Если в скобках не дата, а, например, (Week 1), вернем это обратно
                 task_text = f"{task_text} ({deadline_part})"
                 
            tasks.append({"task": task_text, "deadline": deadline_str})
        elif line.strip(): # Если строка не пустая, но не подошла под паттерн, логируем
             logger.warning(f"Не смог распарсить строку задачи: '{line.strip()}'")
             
    return tasks


async def get_tasks_from_message(bot: Bot) -> list:
    # ... (твой код не трогаю)
    if not TARGET_CHAT_ID: return []
    try:
        # Убедимся, что ID сообщения существует
        if not MESSAGE_ID_TO_EDIT:
             logger.error("MESSAGE_ID_TO_EDIT не установлен!")
             return []
        
        message = await bot.get_chat(chat_id=TARGET_CHAT_ID) # Получаем инфо о чате
        # Ищем закрепленное сообщение (если оно есть и совпадает с нашим ID)
        # ИЛИ просто читаем сообщение по ID, если оно не закреплено (на всякий случай)
        target_message_text = None
        if message.pinned_message and str(message.pinned_message.message_id) == MESSAGE_ID_TO_EDIT:
             target_message_text = message.pinned_message.text
        else:
             # Попробуем прочитать сообщение напрямую по ID
             try:
                 msg_obj = await bot.get_message(chat_id=TARGET_CHAT_ID, message_id=MESSAGE_ID_TO_EDIT)
                 target_message_text = msg_obj.text
             except error.BadRequest as e:
                 logger.error(f"Не удалось получить сообщение по ID {MESSAGE_ID_TO_EDIT}: {e}")
                 # Возможно, сообщение удалено. Попробуем найти закрепленное, если есть.
                 if message.pinned_message:
                      logger.warning(f"Пытаюсь использовать текст из закрепленного сообщения {message.pinned_message.message_id} вместо {MESSAGE_ID_TO_EDIT}")
                      target_message_text = message.pinned_message.text
                 else:
                      return [] # Сообщения нет
                 
        if target_message_text:
            return parse_tasks_from_text(target_message_text)
        return []
    except Exception as e:
        logger.error(f"Не удалось прочитать сообщение: {e}", exc_info=True)
        return []


async def update_tasks_message(bot: Bot, tasks: list):
    # ... (твой код не трогаю, но добавил больше логов)
    if not (TARGET_CHAT_ID and MESSAGE_ID_TO_EDIT):
        logger.error("Переменные ID не установлены. Обновление невозможно.")
        return

    text = "📋 *Список задач:*\n"
    if not tasks:
        text += "_Задач нет_"
    else:
        now = datetime.now()
        try:
            # Сортировка: сначала по дате (None или прошедшие в конце), потом по имени
            sorted_tasks = sorted(
                tasks,
                key=lambda x: (
                    datetime.strptime(x['deadline'], '%Y-%m-%d').date() if x.get('deadline') else date.max,
                    x['task']
                )
            )
        except Exception as e:
            logger.error(f"Ошибка при сортировке задач: {e}", exc_info=True)
            # В случае ошибки сортировки, выводим как есть
            sorted_tasks = tasks
            text += "\n⚠️ *Ошибка сортировки!* \n"

        for i, t in enumerate(sorted_tasks, start=1):
            line = t.get("task", "Без названия") # На случай, если ключ 'task' отсутствует
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

                    # Аккуратно добавляем/заменяем дедлайн
                    line_base = re.sub(r'\s+\([^)]*\)$', '', line).strip()
                    line = f"{line_base} {deadline_str_formatted}"

                    # Markdown
                    if days_left < 0:
                        line = f"❌ ~{line}~"
                    elif days_left <= 2:
                        line = f"⚠️ *{line}*"
                except ValueError:
                    logger.warning(f"Некорректная дата '{t['deadline']}' в задаче: {line}")
                    # Оставляем оригинальную строку без изменений, если дата не парсится
                    line = t["task"]

            text += f"{i}. {line}\n"

    try:
        await bot.edit_message_text(text, chat_id=TARGET_CHAT_ID, message_id=MESSAGE_ID_TO_EDIT,
                                         parse_mode="Markdown")
        logger.info(f"Сообщение {MESSAGE_ID_TO_EDIT} успешно обновлено.")
    except error.BadRequest as e:
        if "message is not modified" not in str(e):
            logger.error(f"Не удалось обновить сообщение {MESSAGE_ID_TO_EDIT}: {e}")
        else:
            logger.info("Текст сообщения не изменился, пропуск обновления.")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при обновлении сообщения {MESSAGE_ID_TO_EDIT}: {e}", exc_info=True)


# --- Команды ---

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (твой код не трогаю)
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
        # ... (текст не трогаю) ...
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
    # ... (твой код не трогаю)
    tasks = await get_tasks_from_message(context.bot)
    text = update.message.text.strip().lstrip('-').strip()
    task_text, deadline_iso = parse_date_from_text(text)
    if not task_text: # Проверка, что текст задачи не пустой
         logger.warning("Попытка добавить пустую задачу.")
         await update.message.delete()
         return
    tasks.append({"task": task_text, "deadline": deadline_iso})
    await update_tasks_message(context.bot, tasks)
    await update.message.delete()


# --- ❗️❗️❗️ ОБНОВЛЕННАЯ КОМАНДА УДАЛЕНИЯ (Bulk Delete) ❗️❗️❗️ ---
async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет задачи по номерам 'удали N M K'."""
    tasks = await get_tasks_from_message(context.bot)
    if not tasks:
        await update.message.reply_text("❌ Список задач и так пуст.", quote=False)
        return

    text = update.message.text.strip()
    # Ищем ВСЕ числа в сообщении
    indices_to_remove_str = re.findall(r'\d+', text)

    if not indices_to_remove_str:
        await update.message.reply_text("❌ Не найдены номера задач для удаления. Напиши 'удали 2' или 'удали 1 3 5'.", quote=False)
        await update.message.delete()
        return

    # Преобразуем строки в числа и вычитаем 1 для 0-based индексации
    try:
        indices_to_remove = {int(i) - 1 for i in indices_to_remove_str}
    except ValueError:
        await update.message.reply_text("❌ Ошибка. Укажи корректные номера.", quote=False)
        await update.message.delete()
        return

    # Сортируем текущие задачи так же, как они отображаются
    sorted_tasks_with_indices = sorted(
        enumerate(tasks), # Получаем пары (original_index, task_dict)
        key=lambda x: (
            datetime.strptime(x[1]['deadline'], '%Y-%m-%d').date() if x[1].get('deadline') else date.max,
            x[1]['task']
        )
    )

    actual_indices_to_delete = set()
    removed_tasks_names = []
    invalid_indices = []

    for display_index in indices_to_remove:
        if 0 <= display_index < len(sorted_tasks_with_indices):
            # Находим реальный (original) индекс задачи в исходном списке `tasks`
            original_index = sorted_tasks_with_indices[display_index][0]
            actual_indices_to_delete.add(original_index)
            removed_tasks_names.append(sorted_tasks_with_indices[display_index][1]['task']) # Сохраняем имя для ответа
        else:
            invalid_indices.append(display_index + 1) # Сохраняем невалидный номер (1-based)

    if invalid_indices:
        await update.message.reply_text(f"❌ Неверные номера: {', '.join(map(str, invalid_indices))}. Всего задач: {len(tasks)}.", quote=False)

    if not actual_indices_to_delete:
        await update.message.delete()
        return # Нечего удалять

    # Создаем новый список задач, исключая те, что нужно удалить
    # Идем по индексам в ОБРАТНОМ порядке, чтобы не сбить нумерацию при удалении
    new_tasks = [task for i, task in enumerate(tasks) if i not in actual_indices_to_delete]
    
    # Обновляем сообщение с новым списком
    await update_tasks_message(context.bot, new_tasks)
    
    # Отправляем подтверждение (опционально)
    if len(removed_tasks_names) == 1:
         confirmation_text = f"✅ Задача '{removed_tasks_names[0]}' удалена!"
    else:
         confirmation_text = f"✅ Удалено задач: {len(removed_tasks_names)}."
    # await update.message.reply_text(confirmation_text, quote=False) # Можно раскомментировать

    await update.message.delete()


# --- Команда Ask Gemini (добавлено логирование) ---
async def ask_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not model:
        await update.message.reply_text("Ключ Gemini API не настроен.")
        return
    question = update.message.text
    if not question:
        logger.debug("Получено пустое сообщение для Gemini.")
        return

    logger.info(f"Получен вопрос для Gemini: '{question}'")
    prompt = f"Ответь на вопрос: {question}\n\nВАЖНО: Ответ должен быть кратким."
    waiting_msg = await update.message.reply_text("🤔 Думаю...")

    try:
        # Используем асинхронный вызов с таймаутом
        response = await model.generate_content_async(
             prompt,
             request_options={'timeout': 60} # Таймаут 60 секунд
        )
        # Проверяем, есть ли текст в ответе
        if response.parts:
             answer = "".join(part.text for part in response.parts)
             logger.info(f"Gemini ответил: '{answer[:50]}...'")
             await waiting_msg.edit_text(answer)
        # Обработка случая, когда Gemini вернул пустой ответ (например, из-за safety settings)
        else:
             logger.warning("Gemini вернул пустой ответ (возможно, сработали safety settings).")
             # Пытаемся получить причину блокировки, если она есть
             block_reason = ""
             if response.prompt_feedback and response.prompt_feedback.block_reason:
                 block_reason = f" Причина: {response.prompt_feedback.block_reason.name}"
             await waiting_msg.edit_text(f"Не могу сгенерировать ответ.{block_reason}")

    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}", exc_info=True)
        await waiting_msg.edit_text(f"Произошла ошибка при запросе к Gemini.")


# --- Настройка сервера FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI приложение запускается...")
    if TOKEN and application:
        try:
            await application.initialize()
            logger.info("Telegram Application инициализировано.")
        except Exception as e:
            logger.error(f"Ошибка инициализации Telegram Application: {e}", exc_info=True)
    elif not TOKEN:
         logger.error("TOKEN не найден! Telegram Application не будет инициализировано.")

    yield # Приложение работает

    logger.info("FastAPI приложение останавливается...")
    if application and application.initialized:
        try:
            await application.shutdown()
            logger.info("Telegram Application остановлено.")
        except Exception as e:
            logger.error(f"Ошибка остановки Telegram Application: {e}", exc_info=True)

api = FastAPI(lifespan=lifespan)
# Проверяем наличие токена перед созданием Application
if TOKEN:
    try:
        application = Application.builder().token(TOKEN).build()
        logger.info("Telegram Application создано.")
    except Exception as e:
        logger.error(f"Критическая ошибка при создании Telegram Application: {e}. Бот не будет работать.", exc_info=True)
        application = None # Указываем, что приложение не создано
else:
    logger.error("Критическая ошибка: TOKEN не найден! Бот не будет работать.")
    application = None

# --- Обработчики ---
if application: # Добавляем хэндлеры, только если application создано
    application.add_handler(CommandHandler("setup", setup))
    application.add_handler(CommandHandler("ask", ask_gemini))
    # Удаление - более строгий регекс, чтобы не ловить "удалил"
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^[Уу]дали\s+(\d+\s*)+$'), remove_task))
    # Добавление
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^-'), add_task))
    # Gemini - должен быть ПОСЛЕДНИМ MessageHandler'ом
    application.add_handler(MessageHandler(
        filters.TEXT &
        ~filters.COMMAND &
        ~filters.Regex(r'^(?i)удали\s+(\d+\s*)+$') & # Обновленный регекс
        ~filters.Regex(r'^-'),
        ask_gemini
    ))
else:
    logger.error("Хэндлеры Telegram не будут добавлены, так как Application не инициализировано.")

URL_PATH = os.getenv("WEBHOOK_SECRET", "webhook")
@api.post(f"/{URL_PATH}")
async def process_telegram_update(request: Request):
    if not application:
        logger.error("Получен Telegram update, но Application не инициализировано.")
        return Response(status_code=500, content="Bot not initialized")
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Ошибка обработки Telegram update: {e}", exc_info=True)
        return Response(status_code=500)


# --- Эндпоинт-"будильник" /health ---
@api.get("/health")
async def health_check():
    """ Простой GET-эндпоинт, который "будит" сервис. """
    logger.info("PING: Сервис 'разбудили'.")
    return Response(status_code=200, content='{"status": "alive"}')


# --- ❗️❗️❗️ ОБНОВЛЕННЫЙ ЭНДПОИНТ для Напоминаний и Парсинга ❗️❗️❗️ ---
# Используем BackgroundTasks для парсера
@api.post(f"/check_reminders/{REMINDER_SECRET}")
async def check_reminders_and_schedule_parse(background_tasks: BackgroundTasks):
    """
    Эндпоинт, который:
    1. БЫСТРО проверяет напоминания и отправляет их.
    2. ДОБАВЛЯЕТ В ОЧЕРЕДЬ фоновую задачу для парсинга KSE и обновления списка.
    """
    endpoint_start_time = time.time()
    logger.info(f"CRON: Запуск проверки напоминаний...")
    if not (TARGET_CHAT_ID and application and application.bot):
        logger.error("CRON: TARGET_CHAT_ID не установлен или бот не готов.")
        return Response(status_code=500, content="Bot not ready")

    bot = application.bot
    reminders_sent_count = 0

    try:
        current_tasks = await get_tasks_from_message(bot) # Получаем задачи для проверки напоминаний
        
        # --- 1. Логика напоминаний (быстрая часть) ---
        today = date.today()
        for task in current_tasks:
            if task.get("deadline"):
                try:
                    deadline_date = datetime.strptime(task["deadline"], "%Y-%m-%d").date()
                    days_left = (deadline_date - today).days
                    reminder_text = None
                    if days_left == 0:
                        reminder_text = f"❗️ **НАПОМИНАНИЕ (дедлайн сегодня):**\n{task['task']}"
                    elif days_left == 1:
                        reminder_text = f"🔔 **НАПОМИНАНИЕ (дедлайн завтра):**\n{task['task']}"
                    
                    if reminder_text:
                        await bot.send_message(chat_id=TARGET_CHAT_ID, text=reminder_text, parse_mode="Markdown")
                        reminders_sent_count += 1
                except ValueError: continue
                except Exception as e: logger.error(f"CRON: Ошибка отправки напоминания: {e}")

        logger.info(f"CRON: Проверка напоминаний завершена. Отправлено: {reminders_sent_count}.")

        # --- 2. Добавляем ПАРСИНГ и ОБНОВЛЕНИЕ в фоновую задачу ---
        # Передаем текущие задачи, чтобы не читать их снова в фоне
        background_tasks.add_task(run_parser_and_update, bot, current_tasks)
        logger.info("CRON: Задача парсинга KSE добавлена в фон.")

        endpoint_duration = time.time() - endpoint_start_time
        logger.info(f"CRON: Эндпоинт завершил работу за {endpoint_duration:.2f} сек.")
        
        # СРАЗУ возвращаем ответ, не дожидаясь парсера
        return Response(status_code=200, content=f"Reminders checked ({reminders_sent_count} sent). Parser scheduled.")

    except Exception as e:
        logger.error(f"CRON: Критическая ошибка в эндпоинте /check_reminders: {e}", exc_info=True)
        return Response(status_code=500, content=f"Error in reminder check: {e}")

# --- ❗️❗️❗️ НОВАЯ ФУНКЦИЯ для фоновой задачи парсинга ❗️❗️❗️ ---
async def run_parser_and_update(bot: Bot, current_tasks: list):
    """
    Эта функция выполняется в ФОНЕ.
    Она парсит KSE, сравнивает задачи и обновляет сообщение.
    """
    logger.info("BG_TASK: Запуск фонового парсинга KSE...")
    task_start_time = time.time()
    parser_message = ""
    try:
        new_hw_tasks = await parse_homework() # Запускаем парсер

        # --- Логика слияния ---
        tasks_updated = False
        current_task_strings = {t['task'] for t in current_tasks}
        new_tasks_added_count = 0

        for new_task in new_hw_tasks:
            if new_task['task'] not in current_task_strings:
                current_tasks.append(new_task)
                tasks_updated = True
                new_tasks_added_count += 1

        if tasks_updated:
            logger.info(f"BG_TASK: Парсер KSE нашел {new_tasks_added_count} новых заданий. Обновляю список...")
            await update_tasks_message(bot, current_tasks) # Обновляем сообщение
            parser_message = f"Parser added {new_tasks_added_count} new tasks."
        else:
            logger.info("BG_TASK: Парсер KSE не нашел новых актуальных заданий.")
            parser_message = "Parser found no new tasks."

    except Exception as e:
        logger.error(f"BG_TASK: Ошибка во время фонового парсинга KSE: {e}", exc_info=True)
        parser_message = f"Background parser failed: {e}"
    
    task_duration = time.time() - task_start_time
    logger.info(f"BG_TASK: Фоновый парсинг завершен за {task_duration:.2f} сек. {parser_message}")

# --- Точка входа (если запускаем не через uvicorn напрямую) ---
# Обычно Render использует команду uvicorn, эта часть может не выполняться
if __name__ == "__main__":

    import uvicorn
    port = int(os.getenv("PORT", 8080)) # Берем порт из окружения или дефолтный
    logger.info(f"Запуск Uvicorn на порту {port}...")

    config = uvicorn.Config(app=api, host="0.0.0.0", port=port, lifespan="on")
    server = uvicorn.Server(config)
    
    # Запускаем асинхронно (нужно для Python 3.7+)
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")

    


