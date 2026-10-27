import asyncio
import hashlib
import html
import io
import logging
import os
import sqlite3
import tempfile
import time
import aiohttp
from aiohttp import web
import yt_dlp

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    URLInputFile,
)

# ================= НАСТРОЙКИ =================
# Токен первого бота (Сохранялка)
SAVER_BOT_TOKEN = os.getenv("BOT_TOKEN", "9289471665:AAHXR4SyFFOmi7FOZIl_nM8AWUffPBEVbBI")

# Токен второго бота (Музыкальный)
MUSIC_BOT_TOKEN = os.getenv("MUSIC_BOT_TOKEN", "8760282415:AAG8D_lBkMmDKNnzo4RK6cHCMsnH-TsqGQc")

# Заглушка ссылки под песней — подставь ссылку на своего музыкального бота:
MUSIC_BOT_LINK = "https://t.me/MightyMusi3cBot"

DB_PATH = "bots_data.db"
PORT = int(os.getenv("PORT", 8080))
PER_PAGE = 9

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Создаём два отдельных экземпляра ботов и два отдельных диспетчера
saver_bot = Bot(token=SAVER_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
saver_dp = Dispatcher()

music_bot = Bot(token=MUSIC_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
music_dp = Dispatcher()

SEARCH_SESSIONS = {}


# ================= ОБЩАЯ БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # Таблицы для Бота №1 (Сохранялка)
        c.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                owner_chat_id INTEGER,
                owner_user_id INTEGER,
                is_enabled INTEGER
            )
        """)
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_protected (
                connection_id TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                PRIMARY KEY (connection_id, chat_id, message_id)
            )
        """)
        # Таблица для Бота №2 (Музыкальный кэш)
        c.execute("""
            CREATE TABLE IF NOT EXISTS track_cache (
                track_key TEXT PRIMARY KEY,
                file_id TEXT
            )
        """)
        conn.commit()


# =====================================================================
# ЧАСТЬ 1: ЛОГИКА БОТА-СОХРАНЯЛКИ (saver_dp / saver_bot)
# =====================================================================
def save_connection(conn_id: str, owner_chat_id: int, owner_user_id: int, is_enabled: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?)",
            (conn_id, owner_chat_id, owner_user_id, int(is_enabled)),
        )
        conn.commit()


def get_owner(conn_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT owner_chat_id, owner_user_id FROM connections WHERE connection_id = ? AND is_enabled = 1",
            (conn_id,),
        ).fetchone()


def extract_media(msg: Message):
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
            "SELECT 1 FROM saved_protected WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
            (conn_id, chat_id, message_id),
        ).fetchone()
        if row:
            return True
        conn.execute("INSERT INTO saved_protected VALUES (?, ?, ?)", (conn_id, chat_id, message_id))
        conn.commit()
        return False


def format_sender(name: str, username: str, user_id: int) -> str:
    safe_name = html.escape(name or "Пользователь")
    if username:
        return f"<b>{safe_name} (@{html.escape(username)})</b>"
    return f'<b><a href="tg://user?id={user_id}">{safe_name}</a></b>'


async def send_cached_media(owner_chat_id: int, media_type: str, file_id: str, caption: str):
    try:
        if media_type == "photo":
            await saver_bot.send_photo(owner_chat_id, file_id, caption=caption)
        elif media_type == "video":
            await saver_bot.send_video(owner_chat_id, file_id, caption=caption)
        elif media_type == "voice":
            await saver_bot.send_voice(owner_chat_id, file_id, caption=caption)
        elif media_type == "audio":
            await saver_bot.send_audio(owner_chat_id, file_id, caption=caption)
        elif media_type == "document":
            await saver_bot.send_document(owner_chat_id, file_id, caption=caption)
        elif media_type == "animation":
            await saver_bot.send_animation(owner_chat_id, file_id, caption=caption)
        elif media_type == "video_note":
            await saver_bot.send_video_note(owner_chat_id, file_id)
            await saver_bot.send_message(owner_chat_id, caption)
        elif media_type == "sticker":
            await saver_bot.send_sticker(owner_chat_id, file_id)
            await saver_bot.send_message(owner_chat_id, caption)
    except Exception as e:
        logging.error(f"Ошибка отправки медиа: {e}")
        await saver_bot.send_message(owner_chat_id, f"{caption}\n\n<i>⚠️ Не удалось прикрепить медиафайл.</i>")


async def try_save_disappearing_media(target_msg: Message, conn_id: str, owner_chat_id: int):
    media_type, file_id = extract_media(target_msg)
    if media_type not in ("photo", "video", "voice", "video_note"):
        return False

    has_protected = getattr(target_msg, "has_protected_content", False)
    in_cache = get_cached_message(conn_id, target_msg.chat.id, target_msg.message_id)

    if in_cache and not has_protected:
        return False

    if is_media_already_saved(conn_id, target_msg.chat.id, target_msg.message_id):
        return False

    try:
        file_info = await saver_bot.get_file(file_id)
        file_buffer = io.BytesIO()
        await saver_bot.download_file(file_info.file_path, destination=file_buffer)
        file_bytes = file_buffer.getvalue()

        sender_str = ""
        if target_msg.from_user:
            sender_str = f"\nОт: {format_sender(target_msg.from_user.full_name, target_msg.from_user.username, target_msg.from_user.id)}"

        caption = f"💣 <b>Исчезающее медиа сохранено</b>{sender_str}"
        if target_msg.caption:
            caption += f"\n\n<blockquote>{html.escape(target_msg.caption)}</blockquote>"

        if media_type == "photo":
            await saver_bot.send_photo(owner_chat_id, BufferedInputFile(file_bytes, filename="photo.jpg"), caption=caption)
        elif media_type == "video":
            await saver_bot.send_video(owner_chat_id, BufferedInputFile(file_bytes, filename="video.mp4"), caption=caption)
        elif media_type == "voice":
            await saver_bot.send_voice(owner_chat_id, BufferedInputFile(file_bytes, filename="voice.ogg"), caption=caption)
        elif media_type == "video_note":
            await saver_bot.send_video_note(owner_chat_id, BufferedInputFile(file_bytes, filename="circle.mp4"))
            await saver_bot.send_message(owner_chat_id, caption)
        return True
    except Exception as e:
        logging.warning(f"Не удалось скачать исчезающее медиа: {e}")
        return False


@saver_dp.message(CommandStart())
async def saver_start(message: Message):
    await message.answer(
        "👋 <b>Привет! Я бот для сохранения удалённых/изменённых сообщений и исчезающих медиа. p.s создатель https://t.me/krystallse</b>\n\n"
        "<b>Как подключить:</b>\n"
        "1. Зайди в <b>Настройки Telegram</b> → <b>Telegram для бизнеса</b> → <b>Чат-боты</b>.\n"
        "2. Добавь меня в список (выбери «Все личные чаты»).\n\n"
        "💡 <i>Чтобы сохранить фото или видео с таймером, просто ответь (свайпни) на него любым сообщением в чате с собеседником!</i>"
    )


@saver_dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    save_connection(connection.id, connection.user_chat_id, connection.user.id, connection.is_enabled)
    if connection.is_enabled:
        await saver_bot.send_message(
            connection.user_chat_id,
            "✅ <b>Бот успешно подключён к твоему аккаунту!</b>\n"
            "Теперь все удалённые, изменённые сообщения и перехваченные медиа с таймером будут приходить сюда.",
        )
    else:
        await saver_bot.send_message(connection.user_chat_id, "❌ <b>Бот отключён от твоего аккаунта.</b>")


@saver_dp.business_message()
async def on_business_message(message: Message):
    conn_id = message.business_connection_id
    owner = get_owner(conn_id)
    if not owner:
        try:
            conn_info = await saver_bot.get_business_connection(conn_id)
            save_connection(conn_info.id, conn_info.user_chat_id, conn_info.user.id, conn_info.is_enabled)
            owner = (conn_info.user_chat_id, conn_info.user.id)
        except Exception as e:
            logging.error(f"Не удалось восстановить подключение: {e}")
            return

    owner_chat_id, _ = owner
    cache_message(message)

    if message.reply_to_message:
        await try_save_disappearing_media(message.reply_to_message, conn_id, owner_chat_id)


@saver_dp.edited_business_message()
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
        if old_text != new_text:
            text_alert = (
                f"✏️ {sender_title} изменил(а) сообщение\n\n"
                f"<b>Old:</b>\n"
                f"<blockquote>{html.escape(old_text) if old_text else '<i>[Без текста]</i>'}</blockquote>\n\n"
                f"<b>New:</b>\n"
                f"<blockquote>{html.escape(new_text) if new_text else '<i>[Без текста]</i>'}</blockquote>"
            )
            await saver_bot.send_message(owner_chat_id, text_alert)

    cache_message(message)


@saver_dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    conn_id = event.business_connection_id
    owner = get_owner(conn_id)
    if not owner:
        return
    owner_chat_id, owner_user_id = owner

    for msg_id in event.message_ids:
        cached = get_cached_message(conn_id, event.chat.id, msg_id)
        if not cached or cached["sender_id"] == owner_user_id:
            continue

        sender_title = format_sender(cached["sender_name"], cached["sender_username"], cached["sender_id"])
        msg_text = cached["text"]
        media_type = cached["media_type"]
        file_id = cached["file_id"]

        header = f"🗑️ {sender_title} удалил(а) сообщение"
        if msg_text:
            header += f"\n\n<blockquote>{html.escape(msg_text)}</blockquote>"

        if media_type and file_id:
            await send_cached_media(owner_chat_id, media_type, file_id, header)
        else:
            await saver_bot.send_message(owner_chat_id, header)


# =====================================================================
# ЧАСТЬ 2: ЛОГИКА МУЗЫКАЛЬНОГО БОТА С ОБХОДОМ DRM (music_dp / music_bot)
# =====================================================================
def get_cached_file_id(track_key: str):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT file_id FROM track_cache WHERE track_key = ?", (track_key,)).fetchone()
        return row[0] if row else None


def save_cached_file_id(track_key: str, file_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO track_cache VALUES (?, ?)", (track_key, file_id))
        conn.commit()


def format_duration(seconds) -> str:
    if not seconds:
        return "0:00"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"


async def resolve_lyrics_or_query(user_query: str) -> str:
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = "https://itunes.apple.com/search"
            params = {"term": user_query, "media": "music", "limit": 1}
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    results = data.get("results", [])
                    if results:
                        artist = results[0].get("artistName", "")
                        track = results[0].get("trackName", "")
                        if artist and track:
                            return f"{artist} - {track}"
    except Exception:
        pass
    return user_query


def _ytdlp_search_sync(query: str, limit: int = 35) -> list:
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    tracks = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
        entries = info.get("entries", []) if info else []
        for entry in entries:
            if not entry or not entry.get("url"):
                continue
            duration = int(entry.get("duration") or 0)
            if duration == 30:  # Отсеиваем демо-превью
                continue

            title = entry.get("title") or "Неизвестный трек"
            uploader = entry.get("uploader") or ""

            if " - " in title:
                parts = title.split(" - ", 1)
                artist, song_name = parts[0].strip(), parts[1].strip()
            else:
                artist = uploader if uploader else "Неизвестен"
                song_name = title.strip()

            track_url = entry.get("url")
            track_key = hashlib.md5(f"{artist.lower()}_{song_name.lower()}_{duration // 5}".encode()).hexdigest()[:12]

            tracks.append({
                "key": track_key,
                "url": track_url,
                "artist": artist,
                "title": song_name,
                "duration": duration,
                "dur_str": format_duration(duration),
                "thumb": entry.get("thumbnail"),
            })
    return tracks


async def search_music(user_query: str) -> list:
    tracks = await asyncio.to_thread(_ytdlp_search_sync, user_query, 35)
    if len(tracks) < 3:
        resolved = await resolve_lyrics_or_query(user_query)
        if resolved.lower() != user_query.lower():
            more_tracks = await asyncio.to_thread(_ytdlp_search_sync, resolved, 35)
            tracks = more_tracks + tracks
    return tracks


def _try_download_single(url: str, output_template: str) -> dict:
    ydl_opts = {
        "format": "http_mp3_128/bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 48 * 1024 * 1024,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        return {
            "filepath": filename,
            "thumb": info.get("thumbnail"),
            "duration": int(info.get("duration") or 0),
        }


def _smart_download_sync(track: dict, all_session_tracks: list, output_template: str) -> dict:
    urls_to_try = [track["url"]]
    target_dur = track["duration"]
    for other in all_session_tracks:
        if other["url"] != track["url"] and abs(other["duration"] - target_dur) <= 15:
            urls_to_try.append(other["url"])

    for candidate_url in urls_to_try[:5]:
        try:
            return _try_download_single(candidate_url, output_template)
        except Exception:
            continue

    search_q = f"{track['artist']} {track['title']}"
    alt_results = _ytdlp_search_sync(search_q, limit=15)
    for alt in alt_results:
        if alt["url"] not in urls_to_try:
            try:
                return _try_download_single(alt["url"], output_template)
            except Exception:
                continue

    raise RuntimeError("Все потоки этого трека защищены DRM")


def build_results_keyboard(search_id: str, page: int) -> InlineKeyboardMarkup:
    session = SEARCH_SESSIONS.get(search_id)
    if not session:
        return InlineKeyboardMarkup(inline_keyboard=[])

    tracks = session["tracks"]
    total_pages = max(1, (len(tracks) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * PER_PAGE
    page_tracks = tracks[start_idx : start_idx + PER_PAGE]

    buttons = []
    for idx, t in enumerate(page_tracks, start=start_idx):
        btn_text = f"{t['dur_str']} {t['artist']} - {t['title']}"
        if len(btn_text) > 60:
            btn_text = btn_text[:57] + "..."
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"dl:{search_id}:{idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="«", callback_data=f"pg:{search_id}:{page - 1}"))

    page_label = f"{page + 1} / {total_pages}+" if total_pages > 1 else "1 / 1"
    nav_row.append(InlineKeyboardButton(text=page_label, callback_data="noop"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="»", callback_data=f"pg:{search_id}:{page + 1}"))

    buttons.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@music_dp.message(CommandStart())
async def music_start(message: Message):
    await message.answer(
        "🎵 <b>Привет! Отправь мне название песни, имя исполнителя или строчку из трека.</b>\n\n"
        "Я найду подходящие аудиозаписи и пришлю их в формате MP3! p.s создатель https://t.me/krystallse"
    )


@music_dp.message(F.text)
async def handle_search(message: Message):
    query = message.text.strip()
    if query.startswith("/"):
        return

    wait_msg = await message.answer(f"🔎 Ищу: <b>{html.escape(query)}</b>...")

    try:
        tracks = await search_music(query)
    except Exception as e:
        logging.error(f"Ошибка поиска музыки: {e}")
        await wait_msg.edit_text("⚠️ Произошла ошибка при поиске. Попробуй другой запрос.")
        return

    if not tracks:
        await wait_msg.edit_text(f"😕 По запросу «<b>{html.escape(query)}</b>» ничего не найдено.")
        return

    search_id = hashlib.md5(f"{message.from_user.id}:{query}".encode()).hexdigest()[:10]
    SEARCH_SESSIONS[search_id] = {"query": query, "tracks": tracks}

    if len(SEARCH_SESSIONS) > 500:
        SEARCH_SESSIONS.pop(next(iter(SEARCH_SESSIONS)), None)

    kb = build_results_keyboard(search_id, page=0)
    await wait_msg.edit_text(f"Аудиозаписи по запросу «<b>{html.escape(query)}</b>»", reply_markup=kb)


@music_dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@music_dp.callback_query(F.data.startswith("pg:"))
async def cb_pagination(call: CallbackQuery):
    _, search_id, page_str = call.data.split(":")
    if search_id not in SEARCH_SESSIONS:
        await call.answer("Список устарел, введи запрос заново.", show_alert=True)
        return
    kb = build_results_keyboard(search_id, page=int(page_str))
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()


@music_dp.callback_query(F.data.startswith("dl:"))
async def cb_download(call: CallbackQuery):
    _, search_id, idx_str = call.data.split(":")
    idx = int(idx_str)

    session = SEARCH_SESSIONS.get(search_id)
    if not session or idx >= len(session["tracks"]):
        await call.answer("Запрос устарел, напиши название песни ещё раз.", show_alert=True)
        return

    track = session["tracks"][idx]
    await call.answer("⏳ Загружаю трек...")

    caption = f'<a href="{MUSIC_BOT_LINK}">🔎 Нажми чтобы найти песню</a>'

    cached_id = get_cached_file_id(track["key"])
    if cached_id:
        try:
            await call.message.answer_audio(audio=cached_id, caption=caption)
            return
        except Exception:
            pass

    await music_bot.send_chat_action(call.message.chat.id, "upload_voice")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_tmpl = os.path.join(tmpdir, "track.%(ext)s")
        try:
            dl_info = await asyncio.to_thread(
                _smart_download_sync, track, session["tracks"], out_tmpl
            )
            filepath = dl_info["filepath"]

            thumbnail = None
            thumb_url = dl_info.get("thumb") or track.get("thumb")
            if thumb_url:
                try:
                    thumbnail = URLInputFile(thumb_url)
                except Exception:
                    thumbnail = None

            sent_msg = await call.message.answer_audio(
                audio=FSInputFile(filepath, filename=f"{track['artist']} - {track['title']}.mp3"),
                title=track["title"],
                performer=track["artist"],
                duration=dl_info.get("duration") or track["duration"],
                thumbnail=thumbnail,
                caption=caption,
            )

            if sent_msg.audio:
                save_cached_file_id(track["key"], sent_msg.audio.file_id)

        except Exception as e:
            logging.error(f"Ошибка скачивания трека: {e}")
            await call.message.answer("⚠️ Этот трек защищен правообладателем. Попробуй выбрать другую версию из списка.")


# ================= ВЕБ-СЕРВЕР ДЛЯ 24/7 =================
async def healthcheck(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")


# ================= ОДНОВРЕМЕННЫЙ ЗАПУСК ОБОИХ БОТОВ =================
async def main():
    init_db()
    await start_web_server()

    await saver_bot.delete_webhook(drop_pending_updates=True)
    await music_bot.delete_webhook(drop_pending_updates=True)

    logging.info("Оба бота (Сохранялка + Музыкальный) успешно запущены и работают раздельно!")

    await asyncio.gather(
        saver_dp.start_polling(saver_bot),
        music_dp.start_polling(music_bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
