import asyncio
import html
import io
import logging
import os
import sqlite3
import time
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    BusinessConnection,
    BusinessMessagesDeleted,
    Message,
)

# ================= НАСТРОЙКИ =================
# Токен можно задать через переменные окружения на хостинге (BOT_TOKEN) или вписать прямо сюда:
BOT_TOKEN = os.getenv("BOT_TOKEN", "9289471665:AAHXR4SyFFOmi7FOZIl_nM8AWUffPBEVbBI")
DB_PATH = "bot_cache.db"
PORT = int(os.getenv("PORT", 8080))  # Порт для Render / Koyeb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ================= БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # Таблица подключенных бизнес-аккаунтов (для работы на 3-5+ человек)
        c.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                owner_chat_id INTEGER,
                owner_user_id INTEGER,
                is_enabled INTEGER
            )
        """)
        # Таблица кэша сообщений для отслеживания изменений и удалений
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                connection_id TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                sender_id INTEGER,
                sender_name TEXT,
                sender_username TEXT,
                text TEXT,
                media_type TEXT,
                file_id TEXT,
                timestamp INTEGER,
                PRIMARY KEY (connection_id, chat_id, message_id)
            )
        """)
        # Таблица чтобы не дублировать сохранение одного и того же таймера
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_Protected (
                connection_id TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                PRIMARY KEY (connection_id, chat_id, message_id)
            )
        """)
        conn.commit()


def save_connection(conn_id: str, owner_chat_id: int, owner_user_id: int, is_enabled: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?)",
            (conn_id, owner_chat_id, owner_user_id, int(is_enabled)),
        )
        conn.commit()


def get_owner(conn_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT owner_chat_id, owner_user_id FROM connections WHERE connection_id = ? AND is_enabled = 1",
            (conn_id,),
        ).fetchone()
        return row  # (owner_chat_id, owner_user_id) или None


def extract_media(msg: Message):
    """Извлекает тип медиа и file_id из сообщения, если они есть."""
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.voice:
        return "voice", msg.voice.file_id
    if msg.video_note:
        return "video_note", msg.video_note.file_id
    if msg.audio:
        return "audio", msg.audio.file_id
    if msg.document:
        return "document", msg.document.file_id
    if msg.animation:
        return "animation", msg.animation.file_id
    if msg.sticker:
        return "sticker", msg.sticker.file_id
    return None, None


def cache_message(msg: Message):
    if not msg.business_connection_id or not msg.from_user:
        return
    media_type, file_id = extract_media(msg)
    text = msg.text or msg.caption or ""
    sender_name = msg.from_user.full_name or "Без имени"
    sender_username = msg.from_user.username or ""

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.business_connection_id,
                msg.chat.id,
                msg.message_id,
                msg.from_user.id,
                sender_name,
                sender_username,
                text,
                media_type,
                file_id,
                int(time.time()),
            ),
        )
        # Автоочистка сообщений старше 7 дней, чтобы база не раздувалась
        conn.execute("DELETE FROM messages WHERE timestamp < ?", (int(time.time()) - 7 * 86400,))
        conn.commit()


def get_cached_message(conn_id: str, chat_id: int, message_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM messages WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
            (conn_id, chat_id, message_id),
        ).fetchone()
        return dict(row) if row else None


def is_media_already_saved(conn_id: str, chat_id: int, message_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM saved_Protected WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
            (conn_id, chat_id, message_id),
        ).fetchone()
        if row:
            return True
        conn.execute(
            "INSERT INTO saved_Protected VALUES (?, ?, ?)",
            (conn_id, chat_id, message_id),
        )
        conn.commit()
        return False


def format_sender(name: str, username: str, user_id: int) -> str:
    safe_name = html.escape(name or "Пользователь")
    if username:
        return f"<b>{safe_name} (@{html.escape(username)})</b>"
    return f'<b><a href="tg://user?id={user_id}">{safe_name}</a></b>'


async def send_cached_media(owner_chat_id: int, media_type: str, file_id: str, caption: str):
    """Отправляет сохраненное медиа владельцу бота."""
    try:
        if media_type == "photo":
            await bot.send_photo(owner_chat_id, file_id, caption=caption)
        elif media_type == "video":
            await bot.send_video(owner_chat_id, file_id, caption=caption)
        elif media_type == "voice":
            await bot.send_voice(owner_chat_id, file_id, caption=caption)
        elif media_type == "audio":
            await bot.send_audio(owner_chat_id, file_id, caption=caption)
        elif media_type == "document":
            await bot.send_document(owner_chat_id, file_id, caption=caption)
        elif media_type == "animation":
            await bot.send_animation(owner_chat_id, file_id, caption=caption)
        elif media_type == "video_note":
            await bot.send_video_note(owner_chat_id, file_id)
            await bot.send_message(owner_chat_id, caption)
        elif media_type == "sticker":
            await bot.send_sticker(owner_chat_id, file_id)
            await bot.send_message(owner_chat_id, caption)
    except Exception as e:
        logging.error(f"Ошибка отправки медиа: {e}")
        await bot.send_message(owner_chat_id, f"{caption}\n\n<i>⚠️ Не удалось прикрепить медиафайл.</i>")


# ================= ПЕРЕХВАТ ИСЧЕЗАЮЩИХ МЕДИА =================
async def try_save_disappearing_media(target_msg: Message, conn_id: str, owner_chat_id: int):
    """
    Скачивает фото/видео из target_msg (например, из reply_to_message) в оперативную память
    и отправляет напрямую в чат с ботом.
    """
    media_type, file_id = extract_media(target_msg)
    if media_type not in ("photo", "video", "voice", "video_note"):
        return False

    # Проверяем, есть ли у сообщения флаг защищенного/исчезающего контента
    # Либо если у него нет обычного текста и на него ответили
    has_protected = getattr(target_msg, "has_protected_content", False)

    # Чтобы бот не сохранял вообще каждую обычную картинку, на которую ты ответила,
    # проверяем: если это обычное фото, оно уже есть в нашей базе messages.
    # А вот фото с таймером Telegram НЕ присылает в обычный поток, поэтому в кэше его НЕТ!
    in_cache = get_cached_message(conn_id, target_msg.chat.id, target_msg.message_id)

    if in_cache and not has_protected:
        return False  # Это обычное не исчезающее сообщение, оно и так в базе

    if is_media_already_saved(conn_id, target_msg.chat.id, target_msg.message_id):
        return False

    try:
        file_info = await bot.get_file(file_id)
        file_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=file_buffer)
        file_bytes = file_buffer.getvalue()

        sender_str = ""
        if target_msg.from_user:
            sender_str = f"\nОт: {format_sender(target_msg.from_user.full_name, target_msg.from_user.username, target_msg.from_user.id)}"

        caption = f"💣 <b>Исчезающее медиа сохранено</b>{sender_str}"
        if target_msg.caption:
            caption += f"\n\n<blockquote>{html.escape(target_msg.caption)}</blockquote>"

        if media_type == "photo":
            input_file = BufferedInputFile(file_bytes, filename="saved_photo.jpg")
            await bot.send_photo(owner_chat_id, input_file, caption=caption)
        elif media_type == "video":
            input_file = BufferedInputFile(file_bytes, filename="saved_video.mp4")
            await bot.send_video(owner_chat_id, input_file, caption=caption)
        elif media_type == "voice":
            input_file = BufferedInputFile(file_bytes, filename="saved_voice.ogg")
            await bot.send_voice(owner_chat_id, input_file, caption=caption)
        elif media_type == "video_note":
            input_file = BufferedInputFile(file_bytes, filename="saved_circle.mp4")
            await bot.send_video_note(owner_chat_id, input_file)
            await bot.send_message(owner_chat_id, caption)
        return True
    except Exception as e:
        logging.warning(f"Не удалось скачать исчезающее медиа: {e}")
        return False


# ================= ХЭНДЛЕРЫ TELEGRAM =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет! Я бот для сохранения удалённых/изменённых сообщений и исчезающих медиа.</b>\n\n"
        "<b>Как подключить:</b>\n"
        "1. Зайди в <b>Настройки Telegram</b> → <b>Telegram для бизнеса</b> → <b>Чат-боты</b>.\n"
        "2. Добавь меня в список (выбери «Все личные чаты»).\n\n"
        "💡 <i>Чтобы сохранить фото или видео с таймером, просто ответь (свайпни) на него любым сообщением в чате с собеседником!</i>"
    )


@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    save_connection(
        conn_id=connection.id,
        owner_chat_id=connection.user_chat_id,
        owner_user_id=connection.user.id,
        is_enabled=connection.is_enabled,
    )
    if connection.is_enabled:
        await bot.send_message(
            connection.user_chat_id,
            "✅ <b>Бот успешно подключён к твоему аккаунту!</b>\n"
            "Теперь все удалённые, изменённые сообщения и перехваченные медиа с таймером будут приходить сюда.",
        )
    else:
        await bot.send_message(
            connection.user_chat_id,
            "❌ <b>Бот был отключён от твоего аккаунта.</b>",
        )


@dp.business_message()
async def on_business_message(message: Message):
    conn_id = message.business_connection_id
    owner = get_owner(conn_id)
    if not owner:
        # Если бот был подключен до запуска базы, пробуем сохранить, когда пишет сам владелец
        return

    owner_chat_id, owner_user_id = owner

    cache_message(message)

    if message.reply_to_message:
        await try_save_disappearing_media(message.reply_to_message, conn_id, owner_chat_id)


@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    conn_id = message.business_connection_id
    owner = get_owner(conn_id)
    if not owner:
        return

    owner_chat_id, owner_user_id = owner

    if message.from_user and message.from_user.id == owner_user_id:
        cache_message(message)
        return

    old_msg = get_cached_message(conn_id, message.chat.id, message.message_id)
    new_text = message.text or message.caption or ""

    sender_title = format_sender(
        message.from_user.full_name if message.from_user else "Собеседник",
        message.from_user.username if message.from_user else "",
        message.from_user.id if message.from_user else 0,
    )

    if old_msg:
        old_text = old_msg.get("text") or ""
        # Если текст реально изменился
        if old_text != new_text:
            text_alert = (
                f"✏️ {sender_title} изменил(а) сообщение\n\n"
                f"<b>Old:</b>\n"
                f"<blockquote>{html.escape(old_text) if old_text else '<i>[Без текста]</i>'}</blockquote>\n\n"
                f"<b>New:</b>\n"
                f"<blockquote>{html.escape(new_text) if new_text else '<i>[Без текста]</i>'}</blockquote>"
            )
            await bot.send_message(owner_chat_id, text_alert)

    # Обновляем сообщение в кэше на актуальную версию
    cache_message(message)


@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    conn_id = event.business_connection_id
    owner = get_owner(conn_id)
    if not owner:
        return

    owner_chat_id, owner_user_id = owner

    for msg_id in event.message_ids:
        cached = get_cached_message(conn_id, event.chat.id, msg_id)
        if not cached:
            continue

        # Не присылаем уведомления, если сообщение удалил сам владелец (по желанию можно убрать эту проверку)
        if cached["sender_id"] == owner_user_id:
            continue

        sender_title = format_sender(
            cached["sender_name"],
            cached["sender_username"],
            cached["sender_id"],
        )
        msg_text = cached["text"]
        media_type = cached["media_type"]
        file_id = cached["file_id"]

        header = f"🗑️ {sender_title} удалил(а) сообщение"
        if msg_text:
            header += f"\n\n<blockquote>{html.escape(msg_text)}</blockquote>"

        if media_type and file_id:
            await send_cached_media(owner_chat_id, media_type, file_id, header)
        else:
            await bot.send_message(owner_chat_id, header)


# ================= ВЕБ-СЕРВЕР ДЛЯ ХОСТИНГА 24/7 =================
async def healthcheck(request):
    return web.Response(text="Bot is alive and running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер для поддержки 24/7 запущен на порту {PORT}")


# ================= ЗАПУСК =================
async def main():
    init_db()
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())