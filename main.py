import asyncio
import logging
import random
import sqlite3
import string
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.deep_linking import create_start_link

# ================== НАСТРОЙКИ ==================
TOKEN = "8705611407:AAGzJyhQc5-awRblRzz5v-mqzFKSmtpu-p0"
ADMIN_ID = 7430466040
PAY_LINK = "https://funpay.com/users/12971123/"

DB_PATH = "nickname_bot_v4.db"

logging.basicConfig(level=logging.INFO)

# ================== BOT / DP / ROUTER ==================
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ================== DB ==================
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

def ensure_schema():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        gens_today INTEGER DEFAULT 0,
        total_gens INTEGER DEFAULT 0,
        last_gen_date TEXT DEFAULT '',
        sub_type TEXT DEFAULT 'Free',
        sub_until TEXT DEFAULT '',
        invited_by INTEGER DEFAULT NULL,
        sub_origin TEXT DEFAULT 'free',
        vipplus_invite_used INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS keys (
        code TEXT PRIMARY KEY,
        uses_left INTEGER NOT NULL DEFAULT 0,
        sub_type TEXT NOT NULL DEFAULT 'Free',
        expires_at TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS invites (
        inviter_id INTEGER NOT NULL,
        friend_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (inviter_id, friend_id)
    )
    """)

    conn.commit()

    def existing_cols(table_name: str) -> set[str]:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    users_cols = existing_cols("users")
    users_needed = {
        "gens_today": "INTEGER DEFAULT 0",
        "total_gens": "INTEGER DEFAULT 0",
        "last_gen_date": "TEXT DEFAULT ''",
        "sub_type": "TEXT DEFAULT 'Free'",
        "sub_until": "TEXT DEFAULT ''",
        "invited_by": "INTEGER DEFAULT NULL",
        "sub_origin": "TEXT DEFAULT 'free'",
        "vipplus_invite_used": "INTEGER DEFAULT 0",
    }
    for col, ddl in users_needed.items():
        if col not in users_cols:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    keys_cols = existing_cols("keys")
    keys_needed = {
        "uses_left": "INTEGER NOT NULL DEFAULT 0",
        "sub_type": "TEXT NOT NULL DEFAULT 'Free'",
        "expires_at": "TEXT NOT NULL DEFAULT ''",
        "active": "INTEGER NOT NULL DEFAULT 1",
    }
    for col, ddl in keys_needed.items():
        if col not in keys_cols:
            cursor.execute(f"ALTER TABLE keys ADD COLUMN {col} {ddl}")

    invites_cols = existing_cols("invites")
    if not invites_cols:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            inviter_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (inviter_id, friend_id)
        )
        """)

    conn.commit()

ensure_schema()

# ================== STATE ==================
user_state = {}  # user_id -> {"step": "...", ...}

# ================== HELPERS ==================
def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()

def is_expired(date_str: str) -> bool:
    try:
        if not date_str:
            return True
        return parse_date(date_str) < datetime.now().date()
    except Exception:
        return True

def clear_state(user_id: int):
    user_state.pop(user_id, None)

def get_user(user_id: int):
    cursor.execute("""
        SELECT user_id, gens_today, total_gens, last_gen_date, sub_type, sub_until, invited_by, sub_origin, vipplus_invite_used
        FROM users
        WHERE user_id=?
    """, (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, last_gen_date) VALUES (?, ?)",
            (user_id, today_str())
        )
        conn.commit()
        cursor.execute("""
            SELECT user_id, gens_today, total_gens, last_gen_date, sub_type, sub_until, invited_by, sub_origin, vipplus_invite_used
            FROM users
            WHERE user_id=?
        """, (user_id,))
        row = cursor.fetchone()

    if not row:
        return (user_id, 0, 0, today_str(), "Free", "", None, "free", 0)

    return (
        row[0],
        row[1] or 0,
        row[2] or 0,
        row[3] or "",
        row[4] or "Free",
        row[5] or "",
        row[6],
        row[7] or "free",
        row[8] or 0,
    )

def user_exists(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone() is not None

def refresh_daily_limit(user_id: int):
    user = get_user(user_id)
    last_date = user[3] or ""
    if last_date != today_str():
        cursor.execute(
            "UPDATE users SET gens_today=0, last_gen_date=? WHERE user_id=?",
            (today_str(), user_id)
        )
        conn.commit()

def normalize_subscription(user_id: int):
    user = get_user(user_id)
    sub_type = user[4]
    sub_until = user[5]

    if sub_type != "Free" and sub_until and is_expired(sub_until):
        cursor.execute(
            "UPDATE users SET sub_type='Free', sub_until='', sub_origin='free' WHERE user_id=?",
            (user_id,)
        )
        conn.commit()

def sync_invite_benefit(user_id: int):
    """
    Если пользователь получил VIP через invite, то:
    - пока inviter активен как Vip+ -> синхронизируем срок
    - если inviter больше не Vip+ или срок истёк -> откатываем друга на Free
    """
    user = get_user(user_id)
    invited_by = user[6]
    sub_origin = user[7]

    if sub_origin != "invite" or not invited_by:
        return

    inviter = get_user(int(invited_by))
    inviter_sub = inviter[4]
    inviter_until = inviter[5]

    if inviter_sub != "Vip+" or not inviter_until or is_expired(inviter_until):
        cursor.execute(
            "UPDATE users SET sub_type='Free', sub_until='', sub_origin='free' WHERE user_id=? AND sub_origin='invite'",
            (user_id,)
        )
        conn.commit()
        return

    # держим друга на том же сроке, что и активный Vip+ у inviter
    cursor.execute(
        "UPDATE users SET sub_type='Vip', sub_until=? WHERE user_id=? AND sub_origin='invite'",
        (inviter_until, user_id)
    )
    conn.commit()

def refresh_access(user_id: int):
    refresh_daily_limit(user_id)
    sync_invite_benefit(user_id)
    normalize_subscription(user_id)

def generate_username(length: int) -> str:
    """
    Только английские буквы.
    """
    length = max(5, min(length, 32))
    letters = string.ascii_lowercase
    vowels = "aeiou"
    consonants = "bcdfghjklmnpqrstvwxyz"

    style = random.choice(["plain", "pronounceable", "mixed"])
    out = []

    if style == "plain":
        out = [random.choice(letters) for _ in range(length)]
    elif style == "pronounceable":
        for i in range(length):
            pool = consonants if i % 2 == 0 else vowels
            out.append(random.choice(pool))
    else:
        for _ in range(length):
            pool = random.choice([letters, consonants, vowels])
            out.append(random.choice(pool))

    return "@" + "".join(out)

def main_menu(user_id: int):
    rows = [
        [KeyboardButton(text="Сгенерировать юзернейм")],
        [KeyboardButton(text="Профиль"), KeyboardButton(text="Подписка")],
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(text="Админская панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True
    )

def register_invite(inviter_id: int, friend_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT OR IGNORE INTO invites (inviter_id, friend_id, created_at) VALUES (?, ?, ?)",
        (inviter_id, friend_id, now)
    )
    conn.commit()

def apply_invite_to_friend(friend_id: int, inviter_id: int):
    """
    Один раз на аккаунт:
    - friend получает Vip до даты inviter
    - inviter тратит свой единственный invite
    - если inviter раньше закончится, friend тоже потеряет Vip на ближайшей проверке
    """
    if friend_id == inviter_id:
        return False, "Нельзя пригласить самого себя."

    inviter = get_user(inviter_id)
    inviter_sub = inviter[4]
    inviter_until = inviter[5]
    inviter_invite_used = int(inviter[8] or 0)

    if inviter_sub != "Vip+":
        return False, "У пригласившего нет активной Vip+."
    if not inviter_until or is_expired(inviter_until):
        return False, "У пригласившего Vip+ уже не активна."
    if inviter_invite_used == 1:
        return False, "Этот Vip+ уже использовал свой единственный invite."

    friend = get_user(friend_id)
    friend_sub = friend[4]
    friend_until = friend[5]
    friend_invited_by = friend[6]
    friend_origin = friend[7]

    # не перезаписываем уже активную платную подписку
    if friend_sub != "Free" and friend_origin != "invite" and friend_until and not is_expired(friend_until):
        return False, "У пользователя уже есть активная подписка."

    # один invite на аккаунт
    if friend_invited_by not in (None, 0):
        return False, "Этот аккаунт уже был приглашён ранее."

    cursor.execute(
        "UPDATE users SET sub_type='Vip', sub_until=?, invited_by=?, sub_origin='invite' WHERE user_id=?",
        (inviter_until, inviter_id, friend_id)
    )
    cursor.execute(
        "UPDATE users SET vipplus_invite_used=1 WHERE user_id=?",
        (inviter_id,)
    )
    register_invite(inviter_id, friend_id)
    conn.commit()

    return True, f"Инвайт активирован: тебе выдан Vip до {inviter_until}."

def grant_key_to_user(user_id: int, sub_type: str, until_date: str):
    cursor.execute(
        "UPDATE users SET sub_type=?, sub_until=?, sub_origin='paid' WHERE user_id=?",
        (sub_type, until_date, user_id)
    )
    conn.commit()

# ================== START / DEEPLINK ==================
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    get_user(user_id)
    refresh_access(user_id)

    text = message.text or "/start"
    parts = text.split(maxsplit=1)

    invite_feedback = None

    if len(parts) == 2:
        payload = parts[1].strip()
        if payload.startswith("ref_"):
            try:
                inviter_id = int(payload.replace("ref_", "", 1))
                ok, feedback = apply_invite_to_friend(user_id, inviter_id)
                if feedback:
                    invite_feedback = feedback
            except ValueError:
                invite_feedback = "Инвайт не распознан."

    clear_state(user_id)

    if invite_feedback:
        await message.answer(invite_feedback)

    await message.answer(
        "Бот генерации редких юзернеймов 🚀",
        reply_markup=main_menu(user_id)
    )

@router.message(Command("invite"))
async def invite_cmd(message: Message):
    user_id = message.from_user.id
    refresh_access(user_id)
    user = get_user(user_id)

    sub_type = user[4]
    sub_until = user[5]
    invite_used = int(user[8] or 0)

    if sub_type != "Vip+":
        await message.answer("Эта функция доступна только в Vip+.")
        return

    if not sub_until or is_expired(sub_until):
        await message.answer("Vip+ уже не активна.")
        return

    if invite_used == 1:
        await message.answer("Твой единственный invite уже использован.")
        return

    link = await create_start_link(bot, f"ref_{user_id}")
    await message.answer(
        f"Твоя ссылка для приглашения:\n{link}\n\n"
        f"По ней можно выдать Vip одному человеку. Если твоя Vip+ закончится раньше, его Vip тоже откатится."
    )

# ================== BUTTONS ==================
@router.message(F.text == "Сгенерировать юзернейм")
async def start_generate(message: Message):
    user_id = message.from_user.id
    refresh_access(user_id)
    clear_state(user_id)
    user_state[user_id] = {"step": "await_length"}
    await message.answer("Сколько символов? От 5 до 32. В результате будут только английские буквы.")

@router.message(F.text == "Профиль")
async def profile(message: Message):
    user_id = message.from_user.id
    refresh_access(user_id)

    user = get_user(user_id)
    gens_today = int(user[1] or 0)
    total_gens = int(user[2] or 0)
    sub_type = user[4]
    sub_until = user[5] or "—"
    invited_by = user[6] if user[6] else "—"
    sub_origin = user[7]

    cursor.execute("SELECT COUNT(*) FROM invites WHERE inviter_id=?", (user_id,))
    row = cursor.fetchone()
    invites_count = int(row[0] or 0) if row else 0

    clear_state(user_id)
    user_state[user_id] = {"step": "await_code"}

    extra = ""
    if sub_type == "Vip+" and sub_origin == "paid" and sub_until != "—" and not is_expired(sub_until):
        extra = "\n\nVIP+ активна. Напиши /invite, чтобы получить ссылку-приглашение."

    await message.answer(
        f"👤 Профиль\n\n"
        f"Генераций сегодня: {gens_today}\n"
        f"Всего генераций: {total_gens}\n"
        f"Подписка: {sub_type}\n"
        f"До: {sub_until}\n"
        f"Пригласил: {invited_by}\n"
        f"Приглашено друзей: {invites_count}\n\n"
        f"Чтобы активировать код, просто отправь его сообщением."
        f"{extra}"
    )

@router.message(F.text == "Подписка")
async def subscription(message: Message):
    clear_state(message.from_user.id)
    await message.answer(
        "💎 Подписки:\n\n"
        "Free — 5 генераций в день, бесплатная\n"
        "Vip — безлимитная генерация, платная\n"
        "Vip+ — безлимитная генерация + приглашение друзей, платная\n\n"
        f"Купить подписку: {PAY_LINK}",
        reply_markup=main_menu(message.from_user.id)
    )

@router.message(F.text == "Админская панель")
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ запрещён.")
        return

    clear_state(message.from_user.id)
    user_state[message.from_user.id] = {"step": "admin_wait"}

    await message.answer(
        "Админ-панель\n\n"
        "Создать ключ:\n"
        "create КОД КОЛ-ВО_АКТИВАЦИЙ ВИД_ПОДПИСКИ YYYY-MM-DD\n"
        "Пример:\n"
        "create VIP123 5 Vip 2026-12-31\n\n"
        "Деактивировать ключ:\n"
        "delete КОД"
    )

# ================== CATCH-ALL ==================
@router.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    if text.startswith("/"):
        return

    # ===== GENERATION FLOW =====
    if user_id in user_state and user_state[user_id].get("step") in {"await_length", "await_times"}:
        state = user_state[user_id]

        if state["step"] == "await_length":
            if not text.isdigit():
                await message.answer("Введи число.")
                return

            length = int(text)
            if length < 5 or length > 32:
                await message.answer("Длина должна быть от 5 до 32.")
                return

            state["length"] = length
            state["step"] = "await_times"
            await message.answer("Сколько генераций выполнить?")
            return

        if state["step"] == "await_times":
            if not text.isdigit():
                await message.answer("Введи число.")
                return

            times = int(text)
            length = int(state["length"])

            if times < 1 or times > 50:
                await message.answer("За один раз можно от 1 до 50 генераций.")
                return

            refresh_access(user_id)
            user = get_user(user_id)
            sub_type = user[4]
            gens_today = int(user[1] or 0)

            if sub_type == "Free":
                left = 5 - gens_today
                if left <= 0:
                    await message.answer("Лимит Free на сегодня уже закончился.")
                    clear_state(user_id)
                    return
                if times > left:
                    await message.answer(f"Free: сегодня доступно только {left} генераций.")
                    clear_state(user_id)
                    return

            usernames = [generate_username(length) for _ in range(times)]

            cursor.execute("""
                UPDATE users
                SET gens_today = COALESCE(gens_today, 0) + ?,
                    total_gens = COALESCE(total_gens, 0) + ?,
                    last_gen_date = ?
                WHERE user_id = ?
            """, (times, times, today_str(), user_id))
            conn.commit()

            clear_state(user_id)
            await message.answer(
                f"Хорошо, ваша настройка: {length} символов, {times} раз\n\n" +
                "\n".join(usernames),
                reply_markup=main_menu(user_id)
            )
            return

    # ===== CODE ACTIVATION FROM PROFILE =====
    if user_id in user_state and user_state[user_id].get("step") == "await_code":
        code = text

        cursor.execute("""
            SELECT code, uses_left, sub_type, expires_at, active
            FROM keys
            WHERE code=?
        """, (code,))
        key = cursor.fetchone()

        if not key:
            await message.answer("Код не найден.")
            clear_state(user_id)
            return

        code_db, uses_left, sub_type, expires_at, active = key

        if not active:
            await message.answer("Код деактивирован.")
            clear_state(user_id)
            return

        if int(uses_left or 0) <= 0:
            await message.answer("У кода закончились активации.")
            clear_state(user_id)
            return

        if not expires_at or is_expired(expires_at):
            await message.answer("Срок кода истёк.")
            clear_state(user_id)
            return

        grant_key_to_user(user_id, sub_type, expires_at)

        cursor.execute(
            "UPDATE keys SET uses_left = uses_left - 1 WHERE code=?",
            (code_db,)
        )
        conn.commit()

        clear_state(user_id)
        await message.answer(
            f"Активировано: {sub_type} до {expires_at}",
            reply_markup=main_menu(user_id)
        )
        return

    # ===== ADMIN FLOW =====
    if user_id == ADMIN_ID and user_state.get(user_id, {}).get("step") == "admin_wait":
        parts = text.split()

        if len(parts) == 5 and parts[0].lower() == "create":
            _, code, uses, sub_type, expires_at = parts

            if not uses.isdigit():
                await message.answer("Кол-во активаций должно быть числом.")
                return

            if sub_type not in {"Free", "Vip", "Vip+"}:
                await message.answer("Вид подписки: Free, Vip или Vip+.")
                return

            try:
                parse_date(expires_at)
            except ValueError:
                await message.answer("Дата должна быть в формате YYYY-MM-DD.")
                return

            if is_expired(expires_at):
                await message.answer("Дата должна быть сегодня или позже.")
                return

            cursor.execute("""
                INSERT OR REPLACE INTO keys (code, uses_left, sub_type, expires_at, active)
                VALUES (?, ?, ?, ?, 1)
            """, (code, int(uses), sub_type, expires_at))
            conn.commit()

            await message.answer(
                f"Ключ создан:\n"
                f"Код: {code}\n"
                f"Активаций: {uses}\n"
                f"Подписка: {sub_type}\n"
                f"До: {expires_at}"
            )
            return

        if len(parts) == 2 and parts[0].lower() == "delete":
            _, code = parts
            cursor.execute("UPDATE keys SET active=0 WHERE code=?", (code,))
            conn.commit()
            await message.answer(f"Ключ {code} деактивирован.")
            return

        await message.answer(
            "Неверный формат.\n"
            "create КОД КОЛ-ВО_АКТИВАЦИЙ ВИД_ПОДПИСКИ YYYY-MM-DD\n"
            "delete КОД"
        )
        return

    await message.answer("Используй кнопки меню.", reply_markup=main_menu(user_id))

# ================== RUN ==================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
