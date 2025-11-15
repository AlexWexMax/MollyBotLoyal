import os
import asyncio
import sqlite3
from datetime import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, Text
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import qrcode
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
BOT_USERNAME = os.getenv("BOT_USERNAME")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ---------------- База данных ----------------
conn = sqlite3.connect("database.db", check_same_thread=False)
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

# ---------------- Вспомогательные функции ----------------
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
    stamps = user[3] + 1
    cursor.execute("UPDATE users SET stamps=? WHERE user_id=?", (stamps, user_id))
    conn.commit()
    log_action(user_id, f"Начислен штамп, теперь {stamps}/10")
    return stamps

def use_free_coffee(user_id:int, reset_stamps:bool=True):
    if reset_stamps:
        cursor.execute("UPDATE users SET stamps=0 WHERE user_id=?", (user_id,))
    else:
        cursor.execute("UPDATE users SET coffee_bank=coffee_bank+1, stamps=0 WHERE user_id=?", (user_id,))
    conn.commit()
    log_action(user_id, "Выдан бесплатный кофе" if reset_stamps else "Бесплатный кофе в копилку")

def log_action(user_id:int, action:str):
    now = datetime.utcnow().isoformat()
    cursor.execute("INSERT INTO history (user_id, action, timestamp) VALUES (?, ?, ?)", (user_id, action, now))
    conn.commit()
    user = get_user(user_id)
    username = user[1] or str(user_id)
    print(f"[LOG] @{username} | {action}")

def stamp_bar(stamps:int):
    return "".join("🟤" if i<stamps else "⚪" for i in range(10))

def generate_qr(user_id:int):
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    img = qrcode.make(link)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ---------------- Клавиатуры ----------------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("☕ Мои штампы", callback_data="show_stamps")],
        [InlineKeyboardButton("🔳 Мой QR", callback_data="send_qr")]
    ])

def admin_kb(user_id:int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("➕ Начислить штамп", callback_data=f"admin_add:{user_id}")],
        [InlineKeyboardButton("🎁 Выдать бесплатный кофе", callback_data=f"admin_coffee:{user_id}")],
        [InlineKeyboardButton("💾 В копилку", callback_data=f"admin_bank:{user_id}")]
    ])

# ---------------- Хендлеры ----------------
admins_waiting_password = {}  # chat_id -> True
admins_active = {}  # chat_id -> user_id клиента для редактирования

@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    uid = msg.from_user.id
    create_or_update_user(uid, msg.from_user.first_name, msg.from_user.username)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("▶️ Начать", callback_data="start")]])
    await msg.answer("Добро пожаловать в MollyCoffee Loyalty Bot!\nНажмите кнопку 'Начать' чтобы открыть меню.", reply_markup=kb)

@dp.callback_query(Text("start"))
async def start_button(c: types.CallbackQuery):
    await c.message.edit_text("Меню:", reply_markup=main_kb())
    await c.answer()

# ---------------- Админ вход ----------------
@dp.message(Command("admin"))
async def admin_cmd(msg: types.Message):
    admins_waiting_password[msg.from_user.id] = True
    await msg.answer("Введите пароль для доступа в админ-панель:")

@dp.message()
async def check_password(msg: types.Message):
    if admins_waiting_password.get(msg.from_user.id):
        if msg.text.strip() == ADMIN_PASSWORD:
            admins_waiting_password.pop(msg.from_user.id)
            await msg.answer("Пароль верный! Отсканируйте QR клиента или введите его user_id, чтобы открыть панель:", reply_markup=None)
        else:
            await msg.answer("❌ Неверный пароль")
            admins_waiting_password.pop(msg.from_user.id)
        return
    if admins_active.get(msg.from_user.id):
        try:
            client_id = int(msg.text.strip())
            admins_active[msg.from_user.id] = client_id
            user = get_user(client_id)
            if user:
                await msg.answer(f"@{user[1]} | Штампы: {user[3]}/10 | Копилка: {user[4]}", reply_markup=admin_kb(client_id))
            else:
                await msg.answer("Клиент не найден.")
        except:
            await msg.answer("Введите корректный user_id клиента.")

# ---------------- Кнопки клиента ----------------
@dp.callback_query(Text("show_stamps"))
async def show_stamps_cb(c: types.CallbackQuery):
    user = get_user(c.from_user.id)
    bar = stamp_bar(user[3])
    text = f"Ваши штампы: {user[3]}/10\n{bar}\nБесплатных кофе в копилке: {user[4]}"
    await c.message.edit_text(text, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(Text("send_qr"))
async def send_qr_cb(c: types.CallbackQuery):
    bio = generate_qr(c.from_user.id)
    await c.message.answer_photo(photo=bio, caption="Покажите этот QR бариста для начисления штампов.")
    await c.answer()

# ---------------- Админ кнопки ----------------
@dp.callback_query(lambda c: c.data.startswith("admin_"))
async def admin_actions(c: types.CallbackQuery):
    parts = c.data.split(":")
    action = parts[0]
    user_id = int(parts[1])
    if action=="admin_add":
        stamps = add_stamp(user_id)
        await c.answer(f"Штамп начислен, теперь {stamps}/10")
    elif action=="admin_coffee":
        use_free_coffee(user_id)
        await c.answer("Бесплатный кофе выдан и штампы сброшены")
    elif action=="admin_bank":
        use_free_coffee(user_id, reset_stamps=False)
        await c.answer("Бесплатный кофе положен в копилку")
    user = get_user(user_id)
    await c.message.edit_text(f"@{user[1]} | Штампы: {user[3]}/10 | Копилка: {user[4]}", reply_markup=admin_kb(user_id))

# ---------------- Запуск ----------------
async def main():
    print("Bot started")
    await dp.start_polling()

if __name__=="__main__":
    asyncio.run(main())
