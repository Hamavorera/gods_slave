from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import json
import os

TOKEN = "7452283327:AAE1nwv-I-45qvPrEmIrLMZxxom7VP3ZMe8"  # не забудь вставить свой токен

TASKS_FILE = "tasks.json"
STATE_FILE = "state.json"

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return []
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ========================  ДОБАВЛЕНИЕ ЗАДАЧ  ========================
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

    tasks = load_tasks()
    tasks.append({"task": task_text, "deadline": deadline})
    save_tasks(tasks)

    await update_task_message(context)
    await update.message.reply_text("✅ Задача добавлена!")


# ========================  ОБНОВЛЕНИЕ СПИСКА  ========================
async def update_task_message(context: ContextTypes.DEFAULT_TYPE):
    tasks = load_tasks()
    state = load_state()
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


# ========================  СТАРТ  ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Удаляем команду /start
    try:
        await update.message.delete()
    except:
        pass

    state = load_state()
    if not state:
        msg = await update.message.reply_text("📋 *Список задач:*\n_Задач нет_", parse_mode="Markdown")
        save_state({"chat_id": msg.chat_id, "message_id": msg.message_id})
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

    tasks = load_tasks()
    if 0 <= index < len(tasks):
        tasks.pop(index)
        save_tasks(tasks)
        await update.message.reply_text("✅ Задача удалена!")
        await update_task_message(context)
    else:
        await update.message.reply_text("❌ Неверный номер задачи")


# ========================  ЗАПУСК  ========================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("remove", remove_task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))

    print("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
