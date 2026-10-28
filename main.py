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

# ================= НАСТРОЙКИ ТОКЕНОВ =================
SAVER_BOT_TOKEN = os.getenv("BOT_TOKEN", "9289471665:AAHXR4SyFFOmi7FOZIl_nM8AWUffPBEVbBI")
MUSIC_BOT_TOKEN = os.getenv("MUSIC_BOT_TOKEN", "8760282415:AAG8D_lBkMmDKNnzo4RK6cHCMsnH-TsqGQc")
VIDEO_BOT_TOKEN = os.getenv("VIDEO_BOT_TOKEN", "9270180658:AAFCz2hkjODU1zWxd23EzW_p700-YRAK654")

MUSIC_BOT_LINK = "https://t.me/MightyMusi3cBot"
VIDEO_BOT_SIGNATURE = "скачано из @DownloadmightyBot"

DB_PATH = "all_bots_data.db"
PORT = int(os.getenv("PORT", 8080))
PER_PAGE = 9

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

saver_bot = Bot(token=SAVER_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
saver_dp = Dispatcher()

music_bot = Bot(token=MUSIC_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
music_dp = Dispatcher()

video_bot = Bot(token=VIDEO_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
video_dp = Dispatcher()

SEARCH_SESSIONS = {}


# ================= ОБЩАЯ БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS track_cache (
                track_key TEXT PRIMARY KEY,
                file_id TEXT
            )
        """)
        conn.commit()


# =====================================================================
# БОТ №1: СОХРАНЯЛКА УДАЛЕННЫХ / ИЗМЕНЕННЫХ СООБЩЕНИЙ И ТАЙМЕРОВ
# =====================================================================
def save_connection(conn_id: str, owner_chat_id: int, owner_user_id: int, is_enabled: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?)", (conn_id, owner_chat_id, owner_user_id, int(is_enabled)))
        conn.commit()

def get_owner(conn_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT owner_chat_id, owner_user_id FROM connections WHERE connection_id = ? AND is_enabled = 1", (conn_id,)).fetchone()

def extract_media(msg: Message):
    if msg.photo: return "photo", msg.photo[-1].file_id
    if msg.video: return "video", msg.video.file_id
    if msg.voice: return "voice", msg.voice.file_id
    if msg.video_note: return "video_note", msg.video_note.file_id
    if msg.audio: return "audio", msg.audio.file_id
    if msg.document: return "document", msg.document.file_id
    if msg.animation: return "animation", msg.animation.file_id
    if msg.sticker: return "sticker", msg.sticker.file_id
    return None, None

def cache_message(msg: Message):
    if not msg.business_connection_id or not msg.from_user: return
    media_type, file_id = extract_media(msg)
    text = msg.text or msg.caption or ""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (msg.business_connection_id, msg.chat.id, msg.message_id, msg.from_user.id,
             msg.from_user.full_name or "Без имени", msg.from_user.username or "", text, media_type, file_id, int(time.time())))
        conn.execute("DELETE FROM messages WHERE timestamp < ?", (int(time.time()) - 7 * 86400,))
        conn.commit()

def get_cached_message(conn_id: str, chat_id: int, message_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM messages WHERE connection_id = ? AND chat_id = ? AND message_id = ?", (conn_id, chat_id, message_id)).fetchone()
        return dict(row) if row else None

def is_media_already_saved(conn_id: str, chat_id: int, message_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM saved_protected WHERE connection_id = ? AND chat_id = ? AND message_id = ?", (conn_id, chat_id, message_id)).fetchone():
            return True
        conn.execute("INSERT INTO saved_protected VALUES (?, ?, ?)", (conn_id, chat_id, message_id))
        conn.commit()
        return False

def format_sender(name: str, username: str, user_id: int) -> str:
    safe_name = html.escape(name or "Пользователь")
    return f"<b>{safe_name} (@{html.escape(username)})</b>" if username else f'<b><a href="tg://user?id={user_id}">{safe_name}</a></b>'

async def try_save_disappearing_media(target_msg: Message, conn_id: str, owner_chat_id: int):
    media_type, file_id = extract_media(target_msg)
    if media_type not in ("photo", "video", "voice", "video_note"): return False
    if getattr(target_msg, "has_protected_content", False) is False and get_cached_message(conn_id, target_msg.chat.id, target_msg.message_id):
        return False
    if is_media_already_saved(conn_id, target_msg.chat.id, target_msg.message_id): return False

    try:
        file_info = await saver_bot.get_file(file_id)
        file_buffer = io.BytesIO()
        await saver_bot.download_file(file_info.file_path, destination=file_buffer)
        file_bytes = file_buffer.getvalue()
        sender_str = f"\nОт: {format_sender(target_msg.from_user.full_name, target_msg.from_user.username, target_msg.from_user.id)}" if target_msg.from_user else ""
        caption = f"💣 <b>Исчезающее медиа сохранено</b>{sender_str}"
        if target_msg.caption: caption += f"\n\n<blockquote>{html.escape(target_msg.caption)}</blockquote>"

        if media_type == "photo": await saver_bot.send_photo(owner_chat_id, BufferedInputFile(file_bytes, filename="photo.jpg"), caption=caption)
        elif media_type == "video": await saver_bot.send_video(owner_chat_id, BufferedInputFile(file_bytes, filename="video.mp4"), caption=caption)
        elif media_type == "voice": await saver_bot.send_voice(owner_chat_id, BufferedInputFile(file_bytes, filename="voice.ogg"), caption=caption)
        elif media_type == "video_note":
            await saver_bot.send_video_note(owner_chat_id, BufferedInputFile(file_bytes, filename="circle.mp4"))
            await saver_bot.send_message(owner_chat_id, caption)
        return True
    except Exception as e:
        logging.warning(f"Ошибка сохранения исчезающего медиа: {e}")
        return False

@saver_dp.message(CommandStart())
async def saver_start(message: Message):
    await message.answer("👋 <b>Привет! Я бот для сохранения удалённых/изменённых сообщений и исчезающих медиа. p.s создатель https://t.me/krystallse</b>\n\n1. Зайди в <b>Настройки Telegram</b> → <b>Telegram для бизнеса</b> → <b>Чат-боты</b>.\n2. Добавь меня в список («Все личные чаты»).")

@saver_dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    save_connection(connection.id, connection.user_chat_id, connection.user.id, connection.is_enabled)
    if connection.is_enabled:
        await saver_bot.send_message(connection.user_chat_id, "✅ <b>Бот успешно подключён!</b>")

@saver_dp.business_message()
async def on_business_message(message: Message):
    conn_id = message.business_connection_id
    owner = get_owner(conn_id)
    if not owner:
        try:
            conn_info = await saver_bot.get_business_connection(conn_id)
            save_connection(conn_info.id, conn_info.user_chat_id, conn_info.user.id, conn_info.is_enabled)
            owner = (conn_info.user_chat_id, conn_info.user.id)
        except: return
    cache_message(message)
    if message.reply_to_message: await try_save_disappearing_media(message.reply_to_message, conn_id, owner[0])

@saver_dp.edited_business_message()
async def on_edited_business_message(message: Message):
    conn_id = message.business_connection_id
    owner = get_owner(conn_id)
    if not owner: return
    if message.from_user and message.from_user.id == owner[1]:
        cache_message(message)
        return
    old_msg = get_cached_message(conn_id, message.chat.id, message.message_id)
    new_text = message.text or message.caption or ""
    if old_msg and old_msg.get("text") != new_text:
        sender = format_sender(message.from_user.full_name if message.from_user else "Собеседник", message.from_user.username if message.from_user else "", message.from_user.id if message.from_user else 0)
        await saver_bot.send_message(owner[0], f"✏️ {sender} изменил(а) сообщение\n\n<b>Old:</b>\n<blockquote>{html.escape(old_msg.get('text') or '[Без текста]')}</blockquote>\n\n<b>New:</b>\n<blockquote>{html.escape(new_text or '[Без текста]')}</blockquote>")
    cache_message(message)

@saver_dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    owner = get_owner(event.business_connection_id)
    if not owner: return
    for msg_id in event.message_ids:
        cached = get_cached_message(event.business_connection_id, event.chat.id, msg_id)
        if not cached or cached["sender_id"] == owner[1]: continue
        sender = format_sender(cached["sender_name"], cached["sender_username"], cached["sender_id"])
        header = f"🗑️ {sender} удалил(а) сообщение"
        if cached["text"]: header += f"\n\n<blockquote>{html.escape(cached['text'])}</blockquote>"
        if cached["media_type"] and cached["file_id"]:
            try: await saver_bot.send_photo(owner[0], cached["file_id"], caption=header)
            except: await saver_bot.send_message(owner[0], header)
        else: await saver_bot.send_message(owner[0], header)


# =====================================================================
# БОТ №2: МУЗЫКАЛЬНЫЙ ПОИСКОВИК
# =====================================================================
def get_cached_track(key):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT file_id FROM track_cache WHERE track_key = ?", (key,)).fetchone()
        return row[0] if row else None

def save_cached_track(key, fid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO track_cache VALUES (?, ?)", (key, fid))
        conn.commit()

def _ytdlp_music_search(query: str, limit=35):
    ydl_opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    tracks = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
        for entry in (info.get("entries", []) if info else []):
            if not entry or not entry.get("url"): continue
            dur = int(entry.get("duration") or 0)
            if dur == 30: continue
            title, uploader = entry.get("title") or "Трек", entry.get("uploader") or "Исполнитель"
            artist, song = (title.split(" - ", 1) if " - " in title else (uploader, title))
            tracks.append({"key": hashlib.md5(entry["url"].encode()).hexdigest()[:12], "url": entry["url"], "artist": artist.strip(), "title": song.strip(), "duration": dur, "dur_str": f"{dur//60}:{dur%60:02d}", "thumb": entry.get("thumbnail")})
    return tracks

def _download_track_sync(url, out):
    ydl_opts = {"format": "http_mp3_128/bestaudio[ext=mp3]/bestaudio/best", "outtmpl": out, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info: info = info["entries"][0]
        return {"filepath": ydl.prepare_filename(info), "thumb": info.get("thumbnail"), "duration": int(info.get("duration") or 0)}

@music_dp.message(CommandStart())
async def music_start(message: Message):
    await message.answer("🎵 <b>Привет! Отправь название песни или строчку из трека.</b>\n\nЯ найду аудиозаписи в MP3! p.s создатель https://t.me/krystallse")

@music_dp.message(F.text)
async def music_search(message: Message):
    if message.text.startswith("/"): return
    wait = await message.answer(f"🔎 Ищу: <b>{html.escape(message.text)}</b>...")
    tracks = await asyncio.to_thread(_ytdlp_music_search, message.text)
    if not tracks:
        await wait.edit_text("😕 Ничего не найдено.")
        return
    sid = hashlib.md5(f"{message.from_user.id}:{message.text}".encode()).hexdigest()[:10]
    SEARCH_SESSIONS[sid] = {"tracks": tracks}
    
    btns = [[InlineKeyboardButton(text=f"{t['dur_str']} {t['artist']} - {t['title']}"[:57], callback_data=f"mdl:{sid}:{i}")] for i, t in enumerate(tracks[:9])]
    await wait.edit_text(f"Аудиозаписи по запросу «<b>{html.escape(message.text)}</b>»", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@music_dp.callback_query(F.data.startswith("mdl:"))
async def music_download(call: CallbackQuery):
    _, sid, idx = call.data.split(":")
    session = SEARCH_SESSIONS.get(sid)
    if not session or int(idx) >= len(session["tracks"]):
        await call.answer("Устарело, введи заново.", show_alert=True)
        return
    track = session["tracks"][int(idx)]
    await call.answer("⏳ Загружаю трек...")
    
    cached = get_cached_track(track["key"])
    caption = f'<a href="{MUSIC_BOT_LINK}">🔎 Нажми чтобы найти песню</a>'
    if cached:
        try:
            await call.message.answer_audio(audio=cached, caption=caption)
            return
        except: pass

    with tempfile.TemporaryDirectory() as tmp:
        try:
            res = await asyncio.to_thread(_download_track_sync, track["url"], os.path.join(tmp, "track.%(ext)s"))
            thumb = URLInputFile(res["thumb"]) if res.get("thumb") else None
            sent = await call.message.answer_audio(audio=FSInputFile(res["filepath"]), title=track["title"], performer=track["artist"], duration=res["duration"], thumbnail=thumb, caption=caption)
            if sent.audio: save_cached_track(track["key"], sent.audio.file_id)
        except:
            await call.message.answer("⚠️ Не удалось скачать этот трек, попробуй другой.")


# =====================================================================
# БОТ №3: СКАЧИВАТЕЛЬ ВИДЕО (Instagram, TikTok, YouTube, VK и др.)
# =====================================================================
async def _download_tiktok_or_ytdlp(url: str, out_template: str):
    """Универсальная загрузка: для TikTok используется API-обходчик, для остальных — yt-dlp"""
    if "tiktok.com" in url:
        try:
            async with aiohttp.ClientSession() as session:
                # Используем стабильный публичный API для скачивания TikTok без водяных знаков
                api_url = f"https://tikwm.com/api/?url={url}"
                async with session.get(api_url) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        video_url = data["data"]["play"]
                        title = data["data"].get("title", "TikTok видео")
                        
                        # Скачиваем сам видеофайл
                        async with session.get(video_url) as vid_resp:
                            video_bytes = await vid_resp.read()
                            with open(out_template, "wb") as f:
                                f.write(video_bytes)
                            return out_template, title
        except Exception as e:
            logging.warning(f"TikTok API fallback error: {e}")

    # Стандартный yt-dlp для Instagram, YouTube, VK и т.д.
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 50 * 1024 * 1024,
    }
    def run_ytdlp():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, info.get("title", "Видео")

    return await asyncio.to_thread(run_ytdlp)

@video_dp.message(CommandStart())
async def video_start(message: Message):
    await message.answer(
        "📥 <b>Отправь мне ссылку</b> на видео из Instagram, TikTok, YouTube, VK и др. — скачаю видео за пару секунд."
    )

@video_dp.message(F.text)
async def video_handler(message: Message):
    text = message.text.strip()
    if not text.startswith("http"):
        return
    
    wait = await message.answer("⏳ Скачиваю видео, подожди пару секунд...")
    with tempfile.TemporaryDirectory() as tmp:
        out_tpl = os.path.join(tmp, "video.mp4")
        try:
            filepath, title = await _download_tiktok_or_ytdlp(text, out_tpl)
            caption = f"🎬 <b>{html.escape(title[:100])}</b>\n\n<i>{VIDEO_BOT_SIGNATURE}</i>"
            await message.answer_video(video=FSInputFile(filepath), caption=caption)
            await wait.delete()
        except Exception as e:
            logging.error(f"Ошибка загрузки видео: {e}")
            await wait.edit_text("⚠️ Не удалось скачать видео по этой ссылке. Убедись, что ссылка правильная и профиль открытый.")

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


# ================= ОДНОВРЕМЕННЫЙ ЗАПУСК ВСЕХ ТРЕХ БОТОВ =================
async def main():
    init_db()
    await start_web_server()

    await saver_bot.delete_webhook(drop_pending_updates=True)
    await music_bot.delete_webhook(drop_pending_updates=True)
    await video_bot.delete_webhook(drop_pending_updates=True)

    logging.info("Все 3 бота успешно запущены и работают раздельно на одном сервере!")

    await asyncio.gather(
        saver_dp.start_polling(saver_bot),
        music_dp.start_polling(music_bot),
        video_dp.start_polling(video_bot),
    )

if __name__ == "__main__":
    asyncio.run(main())
