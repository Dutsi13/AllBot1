import logging
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATA_FILE = "data.json"

# Conversation states
WAITING_LIST_NAME = 1
WAITING_USERNAME = 2

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"chats": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_chat_data(data, chat_id):
    chat_id = str(chat_id)
    if chat_id not in data["chats"]:
        data["chats"][chat_id] = {
            "members": {},
            "lists": {},
            "user_lists": {}   # user_id -> { list_name: [usernames] }
        }
    # migrate old data without user_lists
    if "user_lists" not in data["chats"][chat_id]:
        data["chats"][chat_id]["user_lists"] = {}
    return data["chats"][chat_id]

def get_user_lists(chat_data, user_id: str) -> dict:
    """Returns the personal lists dict for a user in this chat."""
    return chat_data["user_lists"].setdefault(user_id, {})

# ─────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>MentionBot — бот для призыва участников</b>\n\n"
        "📋 <b>Команды:</b>\n"
        "• /all — призвать всех участников чата (без регистрации!)\n"
        "• /list — вызвать меню <b>твоих личных</b> списков\n"
        "• /newlist — создать новый личный список\n"
        "• /lists — показать все твои списки\n"
        "• /dellist — удалить список\n\n"
        "ℹ️ <b>Как работает:</b>\n"
        "• /all автоматически упоминает всех участников чата\n"
        "• Каждый участник создаёт <b>свои</b> списки — они не видны другим\n"
        "• Добавляй бота в группу и сразу пользуйся!"
    )
    await update.message.reply_html(text)

# ─────────────────────────────────────────────
# /all — призвать всех участников чата (без регистрации)
# ─────────────────────────────────────────────
async def mention_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user

    if chat.type == "private":
        await update.message.reply_html("❌ Команда работает только в групповых чатах.")
        return

    # Try to collect members via getChatAdministrators + stored cache
    # Telegram Bot API doesn't allow full member list fetch for large groups,
    # so we use a hybrid: cache seen users + get admins
    data = load_data()
    chat_data = get_chat_data(data, str(chat.id))
    members_cache = chat_data.get("members", {})  # uid -> info (populated passively)

    # Always include admins (we can fetch them)
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for member in admins:
            u = member.user
            if u.is_bot:
                continue
            uid = str(u.id)
            members_cache[uid] = {
                "username": u.username or "",
                "has_username": bool(u.username),
                "first_name": u.first_name,
                "display": f"@{u.username}" if u.username else u.first_name
            }
        chat_data["members"] = members_cache
        save_data(data)
    except Exception as e:
        logger.warning(f"Could not fetch admins: {e}")

    if not members_cache:
        await update.message.reply_html(
            "⚠️ Пока не удалось получить список участников.\n"
            "Попроси всех написать любое сообщение или /register — "
            "бот запомнит их автоматически."
        )
        return

    mentions = []
    for uid, info in members_cache.items():
        if str(uid) == str(caller.id):
            continue  # skip the caller themselves
        if info.get("has_username"):
            mentions.append(f"@{info['username']}")
        else:
            mentions.append(f'<a href="tg://user?id={uid}">{info["first_name"]}</a>')

    if not mentions:
        await update.message.reply_html("👥 Больше нет участников для призыва.")
        return

    caller_display = f"@{caller.username}" if caller.username else caller.first_name
    chunk_size = 20
    chunks = [mentions[i:i+chunk_size] for i in range(0, len(mentions), chunk_size)]

    for i, chunk in enumerate(chunks):
        if i == 0:
            text = f"📢 <b>{caller_display}</b> призывает всех:\n\n" + " ".join(chunk)
        else:
            text = " ".join(chunk)
        await update.message.reply_html(text)

# ─────────────────────────────────────────────
# Passive user tracking — запоминаем каждого кто пишет
# ─────────────────────────────────────────────
async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Passively records every user who sends a message in the chat."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type == "private" or user.is_bot:
        return

    data = load_data()
    chat_data = get_chat_data(data, str(chat.id))
    uid = str(user.id)
    info = {
        "username": user.username or "",
        "has_username": bool(user.username),
        "first_name": user.first_name,
        "display": f"@{user.username}" if user.username else user.first_name
    }
    if chat_data["members"].get(uid) != info:
        chat_data["members"][uid] = info
        save_data(data)

    # Also handle inline add_user flow if active
    if "adding_to_list" in context.user_data:
        await handle_add_user_text(update, context)

# ─────────────────────────────────────────────
# /register — ручная регистрация (опционально)
# ─────────────────────────────────────────────
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    data = load_data()
    chat_data = get_chat_data(data, chat_id)

    uid = str(user.id)
    display = f"@{user.username}" if user.username else user.first_name
    chat_data["members"][uid] = {
        "username": user.username or "",
        "has_username": bool(user.username),
        "first_name": user.first_name,
        "display": display
    }
    save_data(data)
    await update.message.reply_html(
        f"✅ {display} зарегистрирован(а)!\n"
        f"Теперь тебя будут призывать командой /all"
    )

# ─────────────────────────────────────────────
# /newlist — создать личный список
# ─────────────────────────────────────────────
async def new_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ Команда работает только в групповых чатах.")
        return ConversationHandler.END

    await update.message.reply_html(
        "📝 Введи название нового <b>личного</b> списка (например: <b>команда</b>, <b>дежурные</b>):"
    )
    return WAITING_LIST_NAME

async def receive_list_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    list_name = update.message.text.strip().lower().replace(" ", "_")
    if not list_name:
        await update.message.reply_text("❌ Название не может быть пустым. Попробуй ещё раз:")
        return WAITING_LIST_NAME

    context.user_data["new_list_name"] = list_name
    context.user_data["new_list_chat_id"] = update.effective_chat.id
    context.user_data["new_list_users"] = []

    await update.message.reply_html(
        f"✅ Список <b>{list_name}</b> будет создан.\n\n"
        f"Отправляй юзернеймы по одному (с @ или без).\n"
        f"Когда закончишь, напиши <b>готово</b>."
    )
    return WAITING_USERNAME

async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() in ["готово", "done", "/done"]:
        list_name = context.user_data.get("new_list_name")
        chat_id = context.user_data.get("new_list_chat_id")
        users = context.user_data.get("new_list_users", [])
        user_id = str(update.effective_user.id)

        data = load_data()
        chat_data = get_chat_data(data, chat_id)
        user_lists = get_user_lists(chat_data, user_id)
        user_lists[list_name] = users
        save_data(data)

        if users:
            user_list_text = "\n".join([f"  • @{u}" for u in users])
            await update.message.reply_html(
                f"✅ Твой список <b>{list_name}</b> создан!\n\n"
                f"Участники ({len(users)}):\n{user_list_text}\n\n"
                f"Вызови через: /list"
            )
        else:
            await update.message.reply_html(
                f"✅ Твой список <b>{list_name}</b> создан (пустой).\n"
                f"Управляй через /list"
            )

        context.user_data.clear()
        return ConversationHandler.END

    username = text.lstrip("@").strip()
    if not username:
        await update.message.reply_text("❌ Некорректный юзернейм. Попробуй ещё раз:")
        return WAITING_USERNAME

    if username not in context.user_data["new_list_users"]:
        context.user_data["new_list_users"].append(username)
        await update.message.reply_html(
            f"➕ <code>@{username}</code> добавлен.\n"
            f"Добавь ещё или напиши <b>готово</b>."
        )
    else:
        await update.message.reply_text(f"⚠️ @{username} уже в списке.")

    return WAITING_USERNAME

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END

# ─────────────────────────────────────────────
# /list — меню личных списков пользователя
# ─────────────────────────────────────────────
async def list_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    data = load_data()
    chat_data = get_chat_data(data, chat_id)
    user_lists = get_user_lists(chat_data, user_id)

    # /list listname — прямой вызов по имени
    if context.args:
        list_name = context.args[0].lower()
        await call_list_by_name(update, chat_id, list_name, user_lists)
        return

    if not user_lists:
        await update.message.reply_html(
            "📋 У тебя нет созданных списков.\n"
            "Создай через /newlist"
        )
        return

    keyboard = []
    for lname, users in user_lists.items():
        keyboard.append([
            InlineKeyboardButton(
                f"📋 {lname} ({len(users)} чел.)",
                callback_data=f"call_list:{lname}"
            ),
            InlineKeyboardButton("✏️", callback_data=f"edit_list:{lname}"),
            InlineKeyboardButton("🗑️", callback_data=f"del_list:{lname}")
        ])

    await update.message.reply_html(
        "📋 <b>Твои списки:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def call_list_by_name(update, chat_id, list_name, user_lists):
    if list_name not in user_lists:
        await update.message.reply_html(f"❌ Список <b>{list_name}</b> не найден.")
        return

    users = user_lists[list_name]
    if not users:
        await update.message.reply_html(f"⚠️ Список <b>{list_name}</b> пуст.")
        return

    mentions = [f"@{u}" for u in users]
    caller = update.effective_user
    caller_display = f"@{caller.username}" if caller.username else caller.first_name

    chunk_size = 20
    chunks = [mentions[i:i+chunk_size] for i in range(0, len(mentions), chunk_size)]
    for i, chunk in enumerate(chunks):
        if i == 0:
            text = f"📢 <b>{caller_display}</b> призывает список <b>{list_name}</b>:\n\n" + " ".join(chunk)
        else:
            text = " ".join(chunk)
        await update.message.reply_html(text)

# ─────────────────────────────────────────────
# /lists — показать все личные списки
# ─────────────────────────────────────────────
async def show_all_lists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    data = load_data()
    chat_data = get_chat_data(data, chat_id)
    user_lists = get_user_lists(chat_data, user_id)

    if not user_lists:
        await update.message.reply_html(
            "📋 У тебя нет созданных списков.\n"
            "Создай через /newlist"
        )
        return

    text = "📋 <b>Твои списки:</b>\n\n"
    for lname, users in user_lists.items():
        text += f"<b>{lname}</b> ({len(users)} чел.):\n"
        if users:
            text += "  " + ", ".join([f"@{u}" for u in users]) + "\n"
        else:
            text += "  (пусто)\n"
        text += "\n"

    await update.message.reply_html(text)

# ─────────────────────────────────────────────
# /dellist — удалить личный список
# ─────────────────────────────────────────────
async def del_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    data = load_data()
    chat_data = get_chat_data(data, chat_id)
    user_lists = get_user_lists(chat_data, user_id)

    if not user_lists:
        await update.message.reply_text("❌ У тебя нет списков для удаления.")
        return

    if context.args:
        list_name = context.args[0].lower()
        if list_name in user_lists:
            del user_lists[list_name]
            save_data(data)
            await update.message.reply_html(f"✅ Список <b>{list_name}</b> удалён.")
        else:
            await update.message.reply_html(f"❌ Список <b>{list_name}</b> не найден.")
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑️ {lname}", callback_data=f"del_list:{lname}")]
        for lname in user_lists
    ]
    await update.message.reply_html(
        "🗑️ <b>Выбери список для удаления:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─────────────────────────────────────────────
# /members — показать всех известных участников
# ─────────────────────────────────────────────
async def show_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = load_data()
    chat_data = get_chat_data(data, chat_id)
    members = chat_data.get("members", {})

    if not members:
        await update.message.reply_html(
            "👥 Бот ещё не запомнил участников.\n"
            "Попросите всех написать любое сообщение или /register"
        )
        return

    text = f"👥 <b>Известные участники ({len(members)}):</b>\n\n"
    for uid, info in members.items():
        text += f"  • {info['display']}\n"

    await update.message.reply_html(text)

# ─────────────────────────────────────────────
# Callback handlers
# ─────────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_str = query.data
    chat_id = query.message.chat_id
    user_id = str(query.from_user.id)

    data = load_data()
    chat_data = get_chat_data(data, str(chat_id))
    user_lists = get_user_lists(chat_data, user_id)

    # ── Call a list ──
    if data_str.startswith("call_list:"):
        list_name = data_str.split(":", 1)[1]
        users = user_lists.get(list_name, [])

        if not users:
            await query.edit_message_text(f"⚠️ Список <b>{list_name}</b> пуст.", parse_mode="HTML")
            return

        mentions = [f"@{u}" for u in users]
        caller = query.from_user
        caller_display = f"@{caller.username}" if caller.username else caller.first_name

        await query.edit_message_text(
            f"📢 <b>{caller_display}</b> призывает список <b>{list_name}</b>:\n\n" + " ".join(mentions),
            parse_mode="HTML"
        )

    # ── Edit a list ──
    elif data_str.startswith("edit_list:"):
        list_name = data_str.split(":", 1)[1]
        users = user_lists.get(list_name, [])
        user_list_text = "\n".join([f"  • @{u}" for u in users]) if users else "  (пусто)"

        keyboard = [
            [InlineKeyboardButton("➕ Добавить пользователя", callback_data=f"add_user:{list_name}")],
            [InlineKeyboardButton("➖ Удалить пользователя", callback_data=f"remove_user_menu:{list_name}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_lists")]
        ]
        await query.edit_message_text(
            f"✏️ Редактирование списка <b>{list_name}</b>\n\n"
            f"Участники:\n{user_list_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── Add user to list (prompt) ──
    elif data_str.startswith("add_user:"):
        list_name = data_str.split(":", 1)[1]
        context.user_data["adding_to_list"] = list_name
        context.user_data["adding_chat_id"] = chat_id
        await query.edit_message_text(
            f"➕ Отправь юзернейм для добавления в <b>{list_name}</b>\n"
            f"(с @ или без)\n\nНапиши /cancel для отмены.",
            parse_mode="HTML"
        )

    # ── Remove user menu ──
    elif data_str.startswith("remove_user_menu:"):
        list_name = data_str.split(":", 1)[1]
        users = user_lists.get(list_name, [])

        if not users:
            await query.answer("Список пуст!", show_alert=True)
            return

        keyboard = [
            [InlineKeyboardButton(f"❌ @{u}", callback_data=f"remove_user:{list_name}:{u}")]
            for u in users
        ]
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"edit_list:{list_name}")])

        await query.edit_message_text(
            f"➖ Выбери кого удалить из <b>{list_name}</b>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── Remove specific user ──
    elif data_str.startswith("remove_user:"):
        parts = data_str.split(":", 2)
        list_name, username = parts[1], parts[2]

        if list_name in user_lists and username in user_lists[list_name]:
            user_lists[list_name].remove(username)
            save_data(data)
            await query.answer(f"@{username} удалён из {list_name}")

        # Refresh edit menu
        users = user_lists.get(list_name, [])
        user_list_text = "\n".join([f"  • @{u}" for u in users]) if users else "  (пусто)"
        keyboard = [
            [InlineKeyboardButton("➕ Добавить пользователя", callback_data=f"add_user:{list_name}")],
            [InlineKeyboardButton("➖ Удалить пользователя", callback_data=f"remove_user_menu:{list_name}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_lists")]
        ]
        await query.edit_message_text(
            f"✏️ Редактирование списка <b>{list_name}</b>\n\n"
            f"Участники:\n{user_list_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── Delete list (confirm) ──
    elif data_str.startswith("del_list:"):
        list_name = data_str.split(":", 1)[1]
        keyboard = [[
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_del:{list_name}"),
            InlineKeyboardButton("❌ Отмена", callback_data="back_to_lists")
        ]]
        await query.edit_message_text(
            f"⚠️ Удалить список <b>{list_name}</b>?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── Confirm delete ──
    elif data_str.startswith("confirm_del:"):
        list_name = data_str.split(":", 1)[1]
        if list_name in user_lists:
            del user_lists[list_name]
            save_data(data)
            await query.answer(f"Список {list_name} удалён")

        await show_lists_keyboard(query, user_lists)

    # ── Back to lists ──
    elif data_str == "back_to_lists":
        await show_lists_keyboard(query, user_lists)

async def show_lists_keyboard(query, user_lists: dict):
    if not user_lists:
        await query.edit_message_text("📋 У тебя нет списков. Создай через /newlist")
        return

    keyboard = []
    for lname, users in user_lists.items():
        keyboard.append([
            InlineKeyboardButton(
                f"📋 {lname} ({len(users)} чел.)",
                callback_data=f"call_list:{lname}"
            ),
            InlineKeyboardButton("✏️", callback_data=f"edit_list:{lname}"),
            InlineKeyboardButton("🗑️", callback_data=f"del_list:{lname}")
        ])

    await query.edit_message_text(
        "📋 <b>Твои списки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─────────────────────────────────────────────
# Inline add_user text handler
# ─────────────────────────────────────────────
async def handle_add_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called from track_user when adding_to_list is active."""
    list_name = context.user_data["adding_to_list"]
    chat_id = context.user_data["adding_chat_id"]
    user_id = str(update.effective_user.id)
    username = update.message.text.strip().lstrip("@")

    if not username:
        await update.message.reply_text("❌ Некорректный юзернейм.")
        return

    data = load_data()
    chat_data = get_chat_data(data, chat_id)
    user_lists = get_user_lists(chat_data, user_id)

    if list_name not in user_lists:
        user_lists[list_name] = []

    if username not in user_lists[list_name]:
        user_lists[list_name].append(username)
        save_data(data)
        await update.message.reply_html(
            f"✅ <code>@{username}</code> добавлен в список <b>{list_name}</b>!\n"
            f"Можешь добавить ещё или написать /list для управления."
        )
    else:
        await update.message.reply_text(f"⚠️ @{username} уже есть в списке {list_name}.")

    context.user_data.clear()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌ Укажите BOT_TOKEN в переменных окружения!")
        print("Пример: export BOT_TOKEN='your_token_here'")
        return

    app = Application.builder().token(token).build()

    # Conversation handler for /newlist
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("newlist", new_list)],
        states={
            WAITING_LIST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_list_name)
            ],
            WAITING_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("all", mention_all))
    app.add_handler(CommandHandler("list", list_menu))
    app.add_handler(CommandHandler("lists", show_all_lists))
    app.add_handler(CommandHandler("dellist", del_list))
    app.add_handler(CommandHandler("members", show_members))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(button_callback))

    # Passive tracking — must be LAST (lowest priority)
    # handles both user tracking and inline add_user flow
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        track_user
    ))

    print("🤖 MentionBot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
