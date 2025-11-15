import os
import asyncio
import sqlite3
from datetime import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, Text
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
import qrcode
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
BOT_USERNAME = os.getenv("BOT_USERNAME")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is not set in .env")
if not BOT_USERNAME:
    # BOT_USERNAME необязателен, но предупреждаем
    print("Warning: BOT_USERNAME is not set in .env (qr links will be tg://user?id=...).")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ------------------ БД ------------------
DB_PATH = os.getenv("DB_PATH", "database.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    stamps INTEGER DEFAULT 0,
    coffee_bank INTEGER DEFAULT 0
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT,
    timestamp TEXT
)
""")
conn.commit()

# ------------------ Вспомогательные ------------------
def create_or_update_user(user_id:int, first_name:str=None, username:str=None):
    cursor.execute("""
    INSERT INTO users (user_id, first_name, username)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username
    """, (user_id, first_name, username))
    conn.commit()

def get_user(user_id:int):
    cursor.execute("SELECT user_id, username, first_name, stamps, coffee_bank FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()

def add_stamp(user_id:int):
    user = get_user(user_id)
    if not user:
        return None
    stamps = (user[3] or 0) + 1
    cursor.execute("UPDATE users SET stamps=? WHERE user_id=?", (stamps, user_id))
    conn.commit()
    log_action(user_id, f"Начислен штамп, теперь {stamps}/10")
    return stamps

def use_free_coffee(user_id:int, reset_stamps:bool=True):
    user = get_user(user_id)
    if not user:
        return False
    if reset_stamps:
        cursor.execute("UPDATE users SET stamps=0 WHERE user_id=?", (user_id,))
    else:
        cursor.execute("UPDATE users SET coffee_bank=coffee_bank+1, stamps=0 WHERE user_id=?", (user_id,))
    conn.commit()
    log_action(user_id, "Выдан бесплатный кофе" if reset_stamps else "Бесплатный кофе в копилку")
    return True

def log_action(user_id:int, action:str):
    now = datetime.utcnow().isoformat()
    cursor.execute("INSERT INTO history (user_id, action, timestamp) VALUES (?, ?, ?)", (user_id, action, now))
    conn.commit()
    user = get_user(user_id)
    username = user[1] if user and user[1] else str(user_id)
    print(f"[LOG] @{username} | {action}")

def stamp_bar(stamps:int):
    # эмоджи: 🟤 заполненный, ⚪ пустой
    return "".join("🟤" if i < stamps else "⚪" for i in range(10))

def generate_qr_image_bytes(user_id:int):
    # Генерируем ссылку, которая открывает бота с аргументом admin_<user_id>
    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=admin_{user_id}"
    else:
        # fallback: tg deep link by user id (may open chat)
        link = f"tg://user?id={user_id}&start=admin_{user_id}"
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ------------------ Клавиатуры ------------------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("☕ Мои штампы", callback_data="show_stamps")],
        [InlineKeyboardButton("🔳 Мой QR", callback_data="send_qr")]
    ])

def admin_kb(user_id:int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("➕ Начислить штамп", callback_data=f"admin_add:{user_id}")],
        [InlineKeyboardButton("🎁 Выдать бесплатный кофе", callback_data=f"admin_coffee:{user_id}")],
        [InlineKeyboardButton("💾 В копилку", callback_data=f"admin_bank:{user_id}")],
        [InlineKeyboardButton("📜 История клиента", callback_data=f"admin_history:{user_id}")],
        [InlineKeyboardButton("⬅️ Список клиентов", callback_data="admin_all")]
    ])

# ------------------ Админ состояния ------------------
# admins_waiting_password: ключ chat_id -> значение:
#   None  -> ждем пароль от /admin (показать список клиентов после)
#   int   -> ждем пароль для конкретного client_id (QR-переход)
admins_waiting_password = {}
admins_authenticated = {}  # chat_id -> True (если ввёл пароль и активен)
admins_active = {}  # chat_id -> user_id текущего клиента

# ------------------ Пагинация ------------------
CLIENTS_PER_PAGE = 5

def get_clients_page(page:int=0):
    cursor.execute("SELECT user_id, username, first_name, stamps, coffee_bank FROM users ORDER BY user_id")
    rows = cursor.fetchall()
    total = len(rows)
    total_pages = max(1, (total - 1) // CLIENTS_PER_PAGE + 1)
    start = page * CLIENTS_PER_PAGE
    page_rows = rows[start:start + CLIENTS_PER_PAGE]

    kb = []
    for u in page_rows:
        uname = u[1] if u[1] else (u[2] if u[2] else str(u[0]))
        kb.append([InlineKeyboardButton(f"{uname} | {u[3]}🟤 | {u[4]}☕", callback_data=f"admin_select_client:{u[0]}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_clients_page:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Вперёд", callback_data=f"admin_clients_page:{page+1}"))
    if nav:
        kb.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ------------------ Хендлеры ------------------
@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    uid = msg.from_user.id
    # создаём/обновляем юзера
    create_or_update_user(uid, msg.from_user.first_name, msg.from_user.username)

    args = msg.get_args() or ""
    # если есть args вида admin_<id> — это переход по qr в админ режим
    if args.startswith("admin_"):
        try:
            client_id = int(args.split("_", 1)[1])
        except Exception:
            client_id = None

        # если админ уже аутентифицирован — сразу открываем панель клиента
        if admins_authenticated.get(uid):
            if client_id and get_user(client_id):
                admins_active[uid] = client_id
                user = get_user(client_id)
                await msg.answer(f"Админ-панель @{user[1] or user[2]} | Штампы: {user[3]}/10 | Копилка: {user[4]}",
                                 reply_markup=admin_kb(client_id))
                return
            else:
                await msg.answer("Клиент не найден.")
                return

        # если админ не в сессии — пометим, что ждем пароль для конкретного client_id
        admins_waiting_password[uid] = client_id
        await msg.answer("Введите пароль для входа в админ-панель клиента:")
        return

    # обычный старт для клиента
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("▶️ Начать", callback_data="open_menu")]])
    await msg.answer("Добро пожаловать в MollyCoffee Loyalty Bot!\nНажмите 'Начать', чтобы открыть меню.", reply_markup=kb)

@dp.callback_query(Text("open_menu"))
async def open_menu_cb(c: types.CallbackQuery):
    await c.message.edit_text("Меню:", reply_markup=main_kb())
    await c.answer()

# ------------------ Админ: /admin ------------------
@dp.message(Command("admin"))
async def admin_cmd(msg: types.Message):
    # пометим, что ждем пароль для общего списка клиентов (None)
    admins_waiting_password[msg.from_user.id] = None
    await msg.answer("Введите пароль для доступа в админ-панель:")

# Обработка ввода пароля (только если ожидаем пароль)
@dp.message()
async def on_text(msg: types.Message):
    chat_id = msg.from_user.id
    text = msg.text.strip()

    # Обрабатываем только если сессия ожидает пароль
    if chat_id in admins_waiting_password:
        expected_client = admins_waiting_password.pop(chat_id)  # int or None
        if text == ADMIN_PASSWORD:
            admins_authenticated[chat_id] = True
            # если ожидали пароль для конкретного клиента (QR), сразу открываем его панель
            if isinstance(expected_client, int) and get_user(expected_client):
                admins_active[chat_id] = expected_client
                user = get_user(expected_client)
                await msg.answer(f"Админ-панель @{user[1] or user[2]} | Штампы: {user[3]}/10 | Копилка: {user[4]}",
                                 reply_markup=admin_kb(expected_client))
                return
            # иначе — показываем список клиентов (страница 0)
            await msg.answer("Доступ предоставлен. Список клиентов:", reply_markup=get_clients_page(0))
        else:
            await msg.answer("❌ Неверный пароль")
        return

    # Если админ уже аутентифицирован и пишет user_id вручную — откроем панель
    if admins_authenticated.get(chat_id):
        # проверяем — возможно админ ввёл user_id клиента вручную
        try:
            client_id = int(text)
            user = get_user(client_id)
            if user:
                admins_active[chat_id] = client_id
                await msg.answer(f"Админ-панель @{user[1] or user[2]} | Штампы: {user[3]}/10 | Копилка: {user[4]}",
                                 reply_markup=admin_kb(client_id))
            else:
                await msg.answer("Клиент не найден.")
        except Exception:
            # обычные сообщения — игнорируем (не ломаем функционал)
            pass
        return

    # не админ и не ожидаем пароль — не вмешиваемся (обычный пользователь)
    return

# ------------------ Клиент: кнопки ------------------
@dp.callback_query(Text("show_stamps"))
async def show_stamps_cb(c: types.CallbackQuery):
    user = get_user(c.from_user.id)
    if not user:
        await c.answer("Пользователь не найден.", show_alert=True)
        return
    bar = stamp_bar(user[3] or 0)
    text = f"Ваши штампы: {user[3]}/10\n{bar}\nБесплатных кофе в копилке: {user[4]}"
    try:
        await c.message.edit_text(text, reply_markup=main_kb())
    except:
        await c.message.answer(text, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(Text("send_qr"))
async def send_qr_cb(c: types.CallbackQuery):
    bio = generate_qr_image_bytes(c.from_user.id)
    # BufferedInputFile принимает bytes (передаём bio.getvalue())
    file = BufferedInputFile(bio.getvalue(), filename="qr.png")
    await c.message.answer_photo(photo=file, caption="Покажите этот QR бариста для начисления штампов.")
    await c.answer()

# ------------------ Админ: кнопки ------------------
@dp.callback_query(lambda call: call.data.startswith("admin_") or call.data in ["admin_all"])
async def admin_actions(call: types.CallbackQuery):
    chat_id = call.from_user.id
    data = call.data

    # блокируем действия, если не аутентифицирован
    if not admins_authenticated.get(chat_id):
        await call.answer("Доступ запрещён. Введите /admin и пароль.", show_alert=True)
        return

    # Начислить штамп
    if data.startswith("admin_add:"):
        client_id = int(data.split(":",1)[1])
        res = add_stamp(client_id)
        if res is None:
            await call.answer("Клиент не найден", show_alert=True)
            return
        admins_active[chat_id] = client_id
        await call.answer("Штамп начислен ✅")

    # Выдать кофе (сброс штампов)
    elif data.startswith("admin_coffee:"):
        client_id = int(data.split(":",1)[1])
        ok = use_free_coffee(client_id, reset_stamps=True)
        if not ok:
            await call.answer("Клиент не найден", show_alert=True)
            return
        admins_active[chat_id] = client_id
        await call.answer("Бесплатный кофе выдан 🎉")

    # В копилку
    elif data.startswith("admin_bank:"):
        client_id = int(data.split(":",1)[1])
        ok = use_free_coffee(client_id, reset_stamps=False)
        if not ok:
            await call.answer("Клиент не найден", show_alert=True)
            return
        admins_active[chat_id] = client_id
        await call.answer("Бесплатный кофе положен в копилку ☑️")

    # История клиента
    elif data.startswith("admin_history:"):
        client_id = int(data.split(":",1)[1])
        cursor.execute("SELECT action, timestamp FROM history WHERE user_id=? ORDER BY id DESC LIMIT 50", (client_id,))
        rows = cursor.fetchall()
        uname = get_user(client_id)
        uname = uname[1] if uname and uname[1] else str(client_id)
        text = f"История {uname}:\n"
        for act, ts in rows:
            text += f"{ts[:19]} - {act}\n"
        await call.message.answer(text)
        await call.answer()
        return

    # Список всех клиентов (страница 0)
    elif data == "admin_all":
        await call.message.edit_text("Список клиентов (нажмите на клиента для выбора):", reply_markup=get_clients_page(0))
        await call.answer()
        return

    # Навигация по страницам / выбор клиента
    elif data.startswith("admin_clients_page:") or data.startswith("admin_select_client:"):
        if data.startswith("admin_clients_page:"):
            page = int(data.split(":",1)[1])
            await call.message.edit_text("Список клиентов (нажмите на клиента для выбора):", reply_markup=get_clients_page(page))
        else:
            client_id = int(data.split(":",1)[1])
            user = get_user(client_id)
            if user:
                admins_active[chat_id] = client_id
                await call.message.edit_text(f"@{user[1] or user[2]} | Штампы: {user[3]}/10 | Копилка: {user[4]}",
                                             reply_markup=admin_kb(client_id))
        await call.answer()
        return

    # обновление панели текущего клиента (после действия)
    user_id = admins_active.get(chat_id)
    if user_id:
        user = get_user(user_id)
        if user:
            await call.message.edit_text(f"@{user[1] or user[2]} | Штампы: {user[3]}/10 | Копилка: {user[4]}",
                                         reply_markup=admin_kb(user_id))
    await call.answer()

# ------------------ Запуск ------------------
async def main():
    print("Bot started")
    await dp.start_polling()

if __name__=="__main__":
    asyncio.run(main())
