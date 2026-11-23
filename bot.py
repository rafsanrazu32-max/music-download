#!/usr/bin/env python3
# ============================================================
# XZ Music & Video Downloader Bot
# Powered by: AYAN AHMED 🛐
# ============================================================

import os
import sys
import re
import json
import time
import html
import asyncio
import logging
import tempfile
import threading
import subprocess
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests
from PIL import Image
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from telethon import TelegramClient, functions
from telethon.sessions import MemorySession
from telethon.tl import types as tl_types
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
)

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_BASE = os.getenv("API_BASE", "https://honeysee-api.vqx.workers.dev").rstrip("/")
USERS_FILE = os.getenv("USERS_FILE", "users.json")

# Telethon MTProto credentials (Required for 200-300+ MB uploads & live upload %)
_raw_api_id = os.getenv("API_ID", "").strip()
API_ID = int(_raw_api_id) if _raw_api_id.isdigit() else 0
API_HASH = os.getenv("API_HASH", "").strip()

# Parse single or comma-separated ADMIN_ID(s)
ADMIN_IDS = []
for _aid in os.getenv("ADMIN_ID", "").split(","):
    _aid = _aid.strip()
    if _aid.lstrip("-").isdigit():
        ADMIN_IDS.append(int(_aid))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("XZMusicBot")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is missing! Please set BOT_TOKEN in environment variables.")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

http_session = requests.Session()
http_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9"
})

# Telethon background loop & client
TELETHON_LOOP = None
TG_CLIENT = None
TELETHON_READY = threading.Event()


def telethon_enabled() -> bool:
    return bool(API_ID and API_HASH and TG_CLIENT is not None)


def max_allowed_bytes() -> int:
    # 2 GB with Telethon MTProto, 50 MB fallback with standard HTTP Bot API
    return (2000 * 1024 * 1024) if telethon_enabled() else (50 * 1024 * 1024)


def start_telethon_worker():
    if not (API_ID and API_HASH):
        logger.warning("API_ID or API_HASH missing — falling back to 50 MB Bot API limit.")
        TELETHON_READY.set()
        return

    def _runner():
        global TELETHON_LOOP, TG_CLIENT
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        TELETHON_LOOP = loop

        async def _boot():
            global TG_CLIENT
            try:
                client = TelegramClient(MemorySession(), API_ID, API_HASH)
                await client.start(bot_token=BOT_TOKEN)
                TG_CLIENT = client
                logger.info("Telethon MTProto connected! Up to 2 GB uploads & live % enabled.")
            except Exception as e:
                logger.error(f"Telethon failed to connect: {e}")
            finally:
                TELETHON_READY.set()

        loop.run_until_complete(_boot())
        loop.run_forever()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    TELETHON_READY.wait(timeout=15)


# Bounded per-chat caches
SEARCH_CACHE = OrderedDict()           # {chat_id: OrderedDict({vid: meta})}
LAST_SEARCH_ITEMS = OrderedDict()      # {chat_id: [meta_dict]}
PENDING_VIDEO_QUALITY = OrderedDict()  # {chat_id: vid}
FILE_PICKER_MAP = OrderedDict()        # {chat_id: {button_label: vid}}
USER_MODE = OrderedDict()              # {chat_id: "awaiting_video_link"}
MAX_CACHED_CHATS = 200
MAX_ITEMS_PER_CHAT = 20

# Admin interactive states {admin_id: state_str}
ADMIN_STATE = {}

# Runtime user & stats database
DB_LOCK = threading.Lock()
DB = {
    "users": {},
    "recent": []
}

BD_TZ = timezone(timedelta(hours=6))


def now_bd_str(short: bool = False) -> str:
    fmt = "%d-%m %I:%M %p" if short else "%d-%m-%Y %I:%M:%S %p (BD)"
    return datetime.now(BD_TZ).strftime(fmt)


def is_admin(user_id: int) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def load_db():
    global DB
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "users" in data:
                    DB["users"] = data.get("users", {})
                    DB["recent"] = data.get("recent", [])
                elif isinstance(data, list):
                    for uid in data:
                        suid = str(uid)
                        DB["users"][suid] = {
                            "id": int(uid),
                            "name": "User",
                            "username": "",
                            "joined_at": "Unknown",
                            "last_active": "Unknown",
                            "searches": 0,
                            "downloads": 0,
                            "audio_downloads": 0,
                            "video_downloads": 0,
                            "history": []
                        }
    except Exception as e:
        logger.warning(f"Could not load {USERS_FILE}: {e}")


def save_db():
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(DB, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Could not save {USERS_FILE}: {e}")


def ensure_user_in_db(user) -> tuple[bool, int]:
    if not user:
        return False, len(DB["users"])
    suid = str(user.id)
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "User"
    username = user.username or ""
    ts = now_bd_str()

    with DB_LOCK:
        is_new = suid not in DB["users"]
        if is_new:
            DB["users"][suid] = {
                "id": user.id,
                "name": full_name,
                "username": username,
                "joined_at": ts,
                "last_active": ts,
                "searches": 0,
                "downloads": 0,
                "audio_downloads": 0,
                "video_downloads": 0,
                "history": []
            }
        else:
            DB["users"][suid]["name"] = full_name
            DB["users"][suid]["username"] = username
            DB["users"][suid]["last_active"] = ts
        save_db()
        return is_new, len(DB["users"])


def record_search(user):
    if not user:
        return
    ensure_user_in_db(user)
    suid = str(user.id)
    with DB_LOCK:
        DB["users"][suid]["searches"] = DB["users"][suid].get("searches", 0) + 1
        DB["users"][suid]["last_active"] = now_bd_str()
        save_db()


def record_download(user, title: str, media_fmt: str):
    if not user:
        return
    ensure_user_in_db(user)
    suid = str(user.id)
    short_ts = now_bd_str(short=True)
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "User"
    username = user.username or ""

    with DB_LOCK:
        u = DB["users"][suid]
        u["downloads"] = u.get("downloads", 0) + 1
        if media_fmt.startswith("MP3"):
            u["audio_downloads"] = u.get("audio_downloads", 0) + 1
        else:
            u["video_downloads"] = u.get("video_downloads", 0) + 1
        u["last_active"] = now_bd_str()

        history = u.setdefault("history", [])
        history.append({"title": title[:80], "type": media_fmt, "time": short_ts})
        if len(history) > 25:
            u["history"] = history[-25:]

        DB["recent"].append({
            "user_id": user.id,
            "name": full_name,
            "username": username,
            "title": title[:80],
            "type": media_fmt,
            "time": short_ts
        })
        if len(DB["recent"]) > 30:
            DB["recent"] = DB["recent"][-30:]

        save_db()


def notify_admin_new_user(message, total_users: int):
    if not ADMIN_IDS or not message.from_user:
        return

    user = message.from_user
    first_name = html.escape(user.first_name or "—")
    last_name = html.escape(user.last_name or "—")
    full_name = html.escape(
        f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"
    )
    mention = f'<a href="tg://user?id={user.id}">{full_name}</a>'
    username = f"@{html.escape(user.username)}" if user.username else "None"
    lang = html.escape(getattr(user, "language_code", None) or "Unknown")
    is_premium = "Yes ⭐" if getattr(user, "is_premium", False) else "No"
    is_bot = "Yes 🤖" if getattr(user, "is_bot", False) else "No"
    chat_type = html.escape(message.chat.type or "private")

    parts = (message.text or "").split(maxsplit=1)
    start_param = html.escape(parts[1].strip()) if len(parts) > 1 else "None"
    now_str = now_bd_str()

    profile_line = (
        f'\n🔗 <b>Profile Link:</b> https://t.me/{html.escape(user.username)}'
        if user.username else ""
    )

    admin_text = (
        "🆕 <b>New User Started the Bot!</b>\n\n"
        "<blockquote>"
        f"👤 <b>Profile:</b> {mention}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"📛 <b>First Name:</b> {first_name}\n"
        f"📛 <b>Last Name:</b> {last_name}\n"
        f"🔗 <b>Username:</b> {username}{profile_line}\n"
        f"🌐 <b>Language:</b> <code>{lang}</code>\n"
        f"⭐ <b>Telegram Premium:</b> {is_premium}\n"
        f"🤖 <b>Is Bot:</b> {is_bot}\n"
        f"💬 <b>Chat Type:</b> <code>{chat_type}</code>\n"
        f"🔑 <b>Start Param:</b> <code>{start_param}</code>\n"
        f"🕒 <b>Time:</b> <code>{now_str}</code>\n"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>"
        "</blockquote>"
    )

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            logger.warning(f"Failed to notify admin {admin_id}: {e}")


# ============================================================
# Colored Reply Keyboards (No Inline Buttons, No Hide Button)
# ============================================================

def kb_btn(text: str, style: str = None) -> types.KeyboardButton:
    """Create a colored Reply Keyboard button (primary=blue, success=green, danger=red)."""
    if style:
        return types.KeyboardButton(text, style=style)
    return types.KeyboardButton(text)


def get_main_keyboard(user_id: int = 0) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    markup.row(
        kb_btn("🔍 Search Music", style="primary"),
        kb_btn("🎬 Video Downloader", style="success")
    )
    markup.row(
        kb_btn("⚡ Server Status", style="success"),
        kb_btn("📖 Help & Guide", style="primary")
    )
    if is_admin(user_id):
        markup.row(kb_btn("👑 Admin Panel", style="success"))
    return markup


def get_song_two_buttons_keyboard() -> types.ReplyKeyboardMarkup:
    """Strictly 2 buttons for searched song."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    markup.row(
        kb_btn("🎵 MP3", style="success"),
        kb_btn("🎬 MP4", style="primary")
    )
    return markup


def get_video_quality_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    markup.row(
        kb_btn("🎬 HD (720p)", style="success"),
        kb_btn("🔥 1080p", style="primary")
    )
    markup.row(
        kb_btn("📺 480p / 360p", style="primary"),
        kb_btn("📱 144p", style="primary")
    )
    markup.row(
        kb_btn("📁 Send as File", style="success"),
        kb_btn("🔙 Main Menu", style="danger")
    )
    return markup


def get_cancel_menu_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    markup.row(kb_btn("🔙 Main Menu", style="danger"))
    return markup


def get_file_picker_keyboard(chat_id: int, items: list) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    mapping = {}

    for item in items[:1]:
        short_title = item.get("title", "Song").strip()
        if len(short_title) > 34:
            short_title = short_title[:31].rstrip() + "..."
        btn_label = f"📁 {short_title}"
        mapping[btn_label] = item["id"]
        markup.row(kb_btn(btn_label, style="success"))

    set_bounded_dict(FILE_PICKER_MAP, chat_id, mapping)
    markup.row(kb_btn("🔙 Main Menu", style="danger"))
    return markup


def get_admin_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    markup.row(
        kb_btn("👥 All Users", style="primary"),
        kb_btn("📊 User Stats", style="success")
    )
    markup.row(
        kb_btn("🔍 User Details", style="primary"),
        kb_btn("📥 Recent Downloads", style="success")
    )
    markup.row(kb_btn("🔙 Main Menu", style="danger"))
    return markup


def get_admin_back_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    markup.row(kb_btn("🔙 Back to Admin Panel", style="danger"))
    return markup


def send_long_message(chat_id: int, text: str, reply_markup=None):
    if len(text) <= 4000:
        bot.send_message(chat_id, text, reply_markup=reply_markup)
        return

    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks):
        rm = reply_markup if idx == len(chunks) - 1 else None
        try:
            bot.send_message(chat_id, chunk, reply_markup=rm)
        except Exception:
            plain = re.sub(r"<[^>]+>", "", chunk)
            bot.send_message(chat_id, plain, parse_mode=None, reply_markup=rm)


# ============================================================
# Cache, Media & Telethon Upload Helpers
# ============================================================

class FileTooLargeError(Exception):
    def __init__(self, size_bytes: int):
        self.size_bytes = size_bytes
        super().__init__(f"File size {size_bytes} exceeds allowed limit")


def set_bounded_dict(store: OrderedDict, key, value):
    if key in store:
        store.move_to_end(key)
    elif len(store) >= MAX_CACHED_CHATS:
        store.popitem(last=False)
    store[key] = value


def cache_put(chat_id: int, vid: str, meta: dict):
    if chat_id not in SEARCH_CACHE:
        if len(SEARCH_CACHE) >= MAX_CACHED_CHATS:
            SEARCH_CACHE.popitem(last=False)
        SEARCH_CACHE[chat_id] = OrderedDict()
    else:
        SEARCH_CACHE.move_to_end(chat_id)

    chat_cache = SEARCH_CACHE[chat_id]
    chat_cache[vid] = meta
    chat_cache.move_to_end(vid)
    while len(chat_cache) > MAX_ITEMS_PER_CHAT:
        chat_cache.popitem(last=False)


def parse_duration_seconds(d_str: str) -> int:
    if not d_str or d_str in ("N/A", "—", "-"):
        return 0
    parts = d_str.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return 0
    return 0


def format_views(view_str: str) -> str:
    if not view_str or view_str in ("—", "N/A"):
        return "—"
    num_match = re.search(r"([\d,]+)", view_str)
    if num_match:
        try:
            num = int(num_match.group(1).replace(",", ""))
            if num >= 1_000_000_000:
                return f"{num / 1_000_000_000:.1f}B views"
            elif num >= 1_000_000:
                return f"{num / 1_000_000:.1f}M views"
            elif num >= 1_000:
                return f"{num / 1_000:.1f}K views"
            else:
                return f"{num} views"
        except Exception:
            return view_str
    return view_str


def search_youtube(query: str, limit: int = 5):
    try:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        resp = http_session.get(url, timeout=10)
        resp.raise_for_status()
        html_doc = resp.text

        match = re.search(r"var ytInitialData = ({.*?});</script>", html_doc)
        if not match:
            match = re.search(r"window\[\"ytInitialData\"\] = ({.*?});</script>", html_doc)
        if not match:
            return []

        data = json.loads(match.group(1))
        contents = (
            data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )

        results = []
        for section in contents:
            items = section.get("itemSectionRenderer", {}).get("contents", [])
            for item in items:
                v = item.get("videoRenderer")
                if v and "videoId" in v:
                    vid = v.get("videoId")
                    title = v.get("title", {}).get("runs", [{}])[0].get("text", "Unknown Title")
                    duration = v.get("lengthText", {}).get("simpleText", "—")
                    channel = v.get("ownerText", {}).get("runs", [{}])[0].get("text", "Unknown Artist")
                    raw_views = v.get("viewCountText", {}).get("simpleText", "—")
                    views = format_views(raw_views)
                    published = v.get("publishedTimeText", {}).get("simpleText", "—")
                    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

                    results.append({
                        "id": vid,
                        "title": title,
                        "duration": duration,
                        "channel": channel,
                        "views": views,
                        "published": published,
                        "thumb": thumb
                    })
                    if len(results) >= limit:
                        return results
        return results
    except Exception as e:
        logger.error(f"Search error for query '{query}': {e}")
        return []


def fetch_video_metadata(chat_id: int, vid: str) -> dict:
    cached = SEARCH_CACHE.get(chat_id, {}).get(vid)
    if cached:
        return cached

    meta = {
        "id": vid,
        "title": "Media Track",
        "duration": "—",
        "channel": "Unknown Artist",
        "views": "—",
        "published": "—",
        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    }

    results = search_youtube(vid, limit=3)
    for r in results:
        if r.get("id") == vid:
            cache_put(chat_id, vid, r)
            return r

    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
        r = http_session.get(oembed_url, timeout=6)
        if r.status_code == 200:
            data = r.json()
            meta["title"] = data.get("title", meta["title"])
            meta["channel"] = data.get("author_name", meta["channel"])
            cache_put(chat_id, vid, meta)
    except Exception:
        pass

    return meta


def extract_video_id(text: str):
    text = text.strip()
    if re.match(r"^[a-zA-Z0-9_-]{11}$", text):
        return text
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11})",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"youtube\.com\/shorts\/([0-9A-Za-z_-]{11})"
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def prepare_thumbnail(thumb_url: str, output_path: str) -> bool:
    tmp_raw = output_path + ".raw"
    try:
        resp = http_session.get(thumb_url, timeout=8)
        resp.raise_for_status()
        with open(tmp_raw, "wb") as f:
            f.write(resp.content)

        with Image.open(tmp_raw) as img:
            img.thumbnail((320, 320))
            img.convert("RGB").save(output_path, "JPEG", quality=85)

        return os.path.exists(output_path) and os.path.getsize(output_path) > 500
    except Exception as e:
        logger.warning(f"Failed to prepare thumbnail: {e}")
        return False
    finally:
        if os.path.exists(tmp_raw):
            try:
                os.remove(tmp_raw)
            except Exception:
                pass


def convert_to_mp3(input_path: str, output_path: str, title: str, artist: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        "-id3v2_version", "3",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", "album=YouTube Music",
        output_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    except Exception as e:
        logger.warning(f"MP3 conversion failed: {e}")
        return False


def mux_video_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-movflags", "+faststart",
        output_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    except Exception as e:
        logger.warning(f"Muxing failed: {e}")
        return False


def probe_video_streams(file_path: str) -> tuple:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json",
        file_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        if res.returncode != 0:
            return None, None, 0, 0
        info = json.loads(res.stdout.decode("utf-8", errors="ignore"))
        vcodec = None
        acodec = None
        width = 0
        height = 0
        for s in info.get("streams", []):
            ctype = s.get("codec_type")
            if ctype == "video" and not vcodec:
                vcodec = (s.get("codec_name") or "").lower()
                width = int(s.get("width") or 0)
                height = int(s.get("height") or 0)
            elif ctype == "audio" and not acodec:
                acodec = (s.get("codec_name") or "").lower()
        return vcodec, acodec, width, height
    except Exception:
        return None, None, 0, 0


def ensure_phone_compatible_mp4(input_path: str, output_path: str, target_quality: str) -> tuple[str, int, int]:
    """
    Keep video gallery/phone compatible while protecting Render's 512 MB RAM on big files.
    """
    vcodec, acodec, w, h = probe_video_streams(input_path)
    file_size = os.path.getsize(input_path) if os.path.exists(input_path) else 0

    # For large videos (> 60 MB), avoid heavy CPU/RAM re-encoding on free instances; fast-remux only
    if file_size > 60 * 1024 * 1024:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c", "copy", "-movflags", "+faststart",
            output_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return output_path, w, h
        except Exception:
            pass
        return input_path, w, h

    if target_quality == "144p" and (h > 200 or vcodec != "h264"):
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "scale=-2:144",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-threads", "1",
            output_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                _, _, nw, nh = probe_video_streams(output_path)
                return output_path, nw, nh
        except Exception as e:
            logger.warning(f"144p transcode warning: {e}")

    if vcodec and vcodec != "h264":
        scale_filter = ["-vf", "scale=-2:720"] if (target_quality == "HD" and h > 720) else []
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            *scale_filter,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-threads", "1",
            output_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                _, _, nw, nh = probe_video_streams(output_path)
                return output_path, nw, nh
        except Exception as e:
            logger.warning(f"H.264 transcode warning: {e}")

    audio_opts = ["-c:a", "copy"] if acodec in ("aac", "mp3", None) else ["-c:a", "aac", "-b:a", "128k"]
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "copy", *audio_opts,
        "-movflags", "+faststart",
        output_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            _, _, nw, nh = probe_video_streams(output_path)
            return output_path, (nw or w), (nh or h)
    except Exception as e:
        logger.warning(f"faststart remux warning: {e}")

    return input_path, w, h


def pick_video_stream_from_api(vid: str, target_quality: str) -> tuple[str, str, bool]:
    target_height = {"144p": 144, "480p": 480, "HD": 720, "1080p": 1080}.get(target_quality, 720)
    fallback_url = f"{API_BASE}/down?url={vid}&format=mp4"

    endpoints = [
        f"{API_BASE}/best?url={vid}&format=mp4&quality={target_height}",
        f"{API_BASE}/best?url={vid}&format=mp4"
    ]

    for ep in endpoints:
        try:
            r = http_session.get(ep, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data.get("success"):
                continue

            title = data.get("video", {}).get("title", "")
            selected = data.get("selected", {})
            dl_url = selected.get("download", fallback_url)
            has_audio = selected.get("hasAudio", True)

            formats = data.get("formats") or data.get("qualities") or data.get("streams") or []
            if isinstance(formats, list) and formats:
                mp4_candidates = []
                for fmt in formats:
                    if not isinstance(fmt, dict):
                        continue
                    f_url = fmt.get("download") or fmt.get("url")
                    if not f_url:
                        continue
                    q_label = str(fmt.get("qualityLabel") or fmt.get("quality") or fmt.get("height") or "")
                    m_h = re.search(r"(\d{3,4})", q_label)
                    f_height = int(fmt.get("height") or (m_h.group(1) if m_h else 0) or 0)
                    f_mime = str(fmt.get("mimeType") or fmt.get("ext") or fmt.get("container") or "").lower()
                    if not fmt.get("hasVideo", True):
                        continue
                    if "mp4" in f_mime or "avc" in f_mime or not f_mime:
                        score_h = abs(f_height - target_height) if f_height else 9999
                        is_avc = 0 if "avc" in f_mime else 1
                        no_aud = 0 if fmt.get("hasAudio", True) else 1
                        mp4_candidates.append((score_h, is_avc, no_aud, f_url, fmt.get("hasAudio", True)))

                if mp4_candidates:
                    mp4_candidates.sort(key=lambda x: (x[0], x[1], x[2]))
                    best = mp4_candidates[0]
                    dl_url = best[3]
                    has_audio = best[4]

            return title, dl_url, has_audio
        except Exception as err:
            logger.warning(f"Stream query warning ({ep}): {err}")

    return "", fallback_url, True


def safe_edit_message(text: str, chat_id: int, message_id: int):
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except ApiTelegramException as e:
        if e.error_code == 429:
            retry_after = int(e.result_json.get("parameters", {}).get("retry_after", 3))
            time.sleep(min(retry_after, 10))
    except Exception:
        pass


def download_with_progress(
    url: str,
    output_path: str,
    chat_id: int,
    msg_id: int,
    title: str,
    channel: str,
    duration: str,
    views: str,
    media_type: str = "Audio"
) -> bool:
    last_update_time = 0.0
    start_time = time.time()
    downloaded = 0
    limit_bytes = max_allowed_bytes()

    clean_title = html.escape(title)
    clean_channel = html.escape(channel)
    clean_views = html.escape(views)

    with http_session.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total_bytes = int(resp.headers.get("Content-Length", 0) or 0)

        if total_bytes > limit_bytes:
            raise FileTooLargeError(total_bytes)

        total_mb = round(total_bytes / (1024 * 1024), 1) if total_bytes else 0

        with open(output_path, "wb") as f_out:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f_out.write(chunk)
                downloaded += len(chunk)

                if downloaded > limit_bytes:
                    raise FileTooLargeError(downloaded)

                now = time.time()
                if now - last_update_time >= 2.5:
                    last_update_time = now
                    pct = int((downloaded / total_bytes) * 100) if total_bytes else 0
                    pct = min(pct, 99)
                    filled = int(pct / 10)
                    bar = "▰" * filled + "▱" * (10 - filled)
                    dl_mb = round(downloaded / (1024 * 1024), 1)
                    elapsed = now - start_time
                    speed_kb = round((downloaded / 1024) / elapsed, 1) if elapsed > 0 else 0
                    speed_str = f"{speed_kb:.0f} KB/s" if speed_kb < 1024 else f"{(speed_kb/1024):.1f} MB/s"

                    size_text = f"<code>{dl_mb} MB / {total_mb} MB</code>" if total_mb else f"<code>{dl_mb} MB</code>"
                    views_line = f"  •  👁 <b>Views:</b> <code>{clean_views}</code>" if clean_views and clean_views != "—" else ""
                    status_text = (
                        f"⚡ <b>Downloading {media_type}...</b>\n\n"
                        "<blockquote>"
                        f"🎵 <b>{clean_title}</b>\n"
                        f"📺 <b>Channel:</b> {clean_channel}\n"
                        f"⏰ <b>Duration:</b> <code>{duration}</code>{views_line}"
                        "</blockquote>\n\n"
                        f"<code>[{bar}]</code> <b>{pct}%</b>\n"
                        f"📊 <b>Size:</b> {size_text}  •  ⚡ <code>{speed_str}</code>"
                    )
                    safe_edit_message(status_text, chat_id, msg_id)

    return os.path.exists(output_path) and os.path.getsize(output_path) > 0


async def _resolve_telethon_entity(chat_id: int, username: str):
    try:
        return await TG_CLIENT.get_input_entity(chat_id)
    except ValueError:
        if username:
            try:
                return await TG_CLIENT.get_input_entity(username)
            except Exception:
                pass
        if chat_id > 0:
            await TG_CLIENT(functions.users.GetUsersRequest([tl_types.InputUser(user_id=chat_id, access_hash=0)]))
            return await TG_CLIENT.get_input_entity(chat_id)
        raise


async def _telethon_upload_async(
    chat_id: int,
    username: str,
    file_path: str,
    thumb_path: str,
    caption: str,
    is_audio: bool,
    as_document: bool,
    title: str,
    channel: str,
    duration_sec: int,
    vid_w: int,
    vid_h: int,
    file_name: str,
    msg_id: int,
    duration_str: str,
    sending_label: str
):
    entity = await _resolve_telethon_entity(chat_id, username)
    clean_title = html.escape(title)
    clean_channel = html.escape(channel)

    state = {"last": 0.0, "busy": False, "start": time.time()}

    def _edit_bg(status_text: str):
        try:
            safe_edit_message(status_text, chat_id, msg_id)
        finally:
            state["busy"] = False

    def _progress(current: int, total: int):
        now = time.time()
        if state["busy"] or (now - state["last"] < 2.5 and current < total):
            return
        state["last"] = now
        state["busy"] = True

        pct = int((current / total) * 100) if total else 0
        pct = min(pct, 100)
        filled = int(pct / 10)
        bar = "▰" * filled + "▱" * (10 - filled)
        up_mb = round(current / (1024 * 1024), 1)
        tot_mb = round(total / (1024 * 1024), 1) if total else 0
        elapsed = now - state["start"]
        speed_kb = round((current / 1024) / elapsed, 1) if elapsed > 0 else 0
        speed_str = f"{speed_kb:.0f} KB/s" if speed_kb < 1024 else f"{(speed_kb/1024):.1f} MB/s"

        status_text = (
            f"📤 <b>{sending_label}...</b>\n\n"
            "<blockquote>"
            f"🎵 <b>{clean_title}</b>\n"
            f"📺 <b>Channel:</b> {clean_channel}\n"
            f"⏰ <b>Duration:</b> <code>{duration_str}</code>"
            "</blockquote>\n\n"
            f"<code>[{bar}]</code> <b>{pct}%</b>\n"
            f"📊 <b>Sent:</b> <code>{up_mb} MB / {tot_mb} MB</code>  •  ⚡ <code>{speed_str}</code>"
        )
        threading.Thread(target=_edit_bg, args=(status_text,), daemon=True).start()

    if is_audio:
        attrs = [
            DocumentAttributeAudio(duration=duration_sec, title=title, performer=channel),
            DocumentAttributeFilename(file_name=file_name)
        ]
        force_doc = False
    elif as_document:
        attrs = [DocumentAttributeFilename(file_name=file_name)]
        force_doc = True
    else:
        attrs = [
            DocumentAttributeVideo(
                duration=duration_sec,
                w=vid_w or 1280,
                h=vid_h or 720,
                supports_streaming=True
            ),
            DocumentAttributeFilename(file_name=file_name)
        ]
        force_doc = False

    await TG_CLIENT.send_file(
        entity,
        file=file_path,
        thumb=thumb_path if thumb_path else None,
        caption=caption,
        parse_mode="html",
        attributes=attrs,
        supports_streaming=(not as_document and not is_audio),
        force_document=force_doc,
        progress_callback=_progress
    )


def send_via_telethon(**kwargs) -> bool:
    if not (telethon_enabled() and TELETHON_LOOP):
        return False
    fut = asyncio.run_coroutine_threadsafe(_telethon_upload_async(**kwargs), TELETHON_LOOP)
    fut.result(timeout=1800)  # Up to 30 min for large files
    return True


# ============================================================
# Welcome & User Commands
# ============================================================

def get_bold_start_text(user) -> str:
    user_name = html.escape(user.first_name if user and user.first_name else "there")
    user_id = user.id if user else 0
    mention = f'<a href="tg://user?id={user_id}"><b>{user_name}</b></a>' if user_id else f"<b>{user_name}</b>"

    return (
        f"✨ <b>Hi, {mention}!</b> Turn a YouTube link or title into audio or video.\n\n"
        "<blockquote>"
        "🎵 <b>Music Downloader</b> — 192 kbps MP3 + Cover Art\n"
        "🎬 <b>Video Downloader</b> — Up to 300+ MB (HD / 1080p / 144p)\n"
        "📁 <b>Send as File</b> — Direct Phone Save Support\n"
        "🔍 <b>Search YouTube</b> — Instant Results"
        "</blockquote>\n\n"
        "💡 <i>গানের নাম লিখুন অথবা 🎬 Video Downloader ব্যবহার করুন।</i>\n"
        "👑 <i>Powered by <b>AYAN AHMED 🛐</b></i>"
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id if message.from_user else 0
    ADMIN_STATE.pop(uid, None)
    USER_MODE.pop(message.chat.id, None)
    PENDING_VIDEO_QUALITY.pop(message.chat.id, None)

    text = get_bold_start_text(message.from_user)
    bot.reply_to(message, text, reply_markup=get_main_keyboard(uid))

    is_new, total_users = ensure_user_in_db(message.from_user)
    if is_new and ADMIN_IDS:
        threading.Thread(
            target=notify_admin_new_user,
            args=(message, total_users),
            daemon=True
        ).start()


@bot.message_handler(commands=["help"])
def cmd_help(message):
    uid = message.from_user.id if message.from_user else 0
    help_text = (
        "📖 <b>Bot Commands &amp; Help</b>\n\n"
        "<blockquote>"
        "• <code>/search &lt;query&gt;</code> — Search Song\n"
        "• <b>🎬 Video Downloader</b> — Download large YouTube videos\n"
        "• <code>/ping</code> — Check response latency\n"
        "• <code>/status</code> — Check server health"
        "</blockquote>\n\n"
        "💡 <i>অথবা সরাসরি যেকোনো গানের নাম বা YouTube লিংক পাঠান!</i>\n"
        "👑 <i>Powered by <b>AYAN AHMED 🛐</b></i>"
    )
    bot.reply_to(message, help_text, reply_markup=get_main_keyboard(uid))


@bot.message_handler(commands=["ping"])
def cmd_ping(message):
    start = time.time()
    msg = bot.reply_to(message, "🏓 <i>Pinging...</i>")
    latency = round((time.time() - start) * 1000, 1)
    bot.edit_message_text(f"⚡ <b>Pong!</b> <code>{latency} ms</code>", chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    send_server_status(message.chat.id)


@bot.message_handler(commands=["search", "song", "play"])
def cmd_search(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "ℹ️ <b>Usage:</b> <code>/search &lt;song or artist name&gt;</code>\n<i>Example:</i> <code>/search Kesariya</code>")
        return
    perform_search(message.chat.id, message.from_user, parts[1].strip())


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid):
        bot.reply_to(message, "⛔ <b>Access Denied.</b> This command is for Admin only.")
        return
    open_admin_panel(message.chat.id, uid)


def send_server_status(chat_id: int):
    wait_msg = bot.send_message(chat_id, "🔄 <i>Checking status...</i>")
    max_mb = "2 GB (MTProto Active)" if telethon_enabled() else "50 MB (Standard)"
    try:
        r = http_session.get(f"{API_BASE}/health", timeout=5)
        status_text = (
            "🟢 <b>System Status: Operational</b>\n\n"
            "<blockquote>"
            "⚡ <b>Streaming Engine:</b> Active\n"
            "🎵 <b>Audio Quality:</b> 192 kbps MP3 (ID3)\n"
            "🎬 <b>Video Quality:</b> HD / 1080p / 480p / 144p\n"
            f"📦 <b>Max Telegram Upload:</b> {max_mb}\n"
            "🚀 <b>Network:</b> Cloud-Accelerated CDN"
            "</blockquote>\n\n"
            "👑 <i>Powered by <b>AYAN AHMED 🛐</b></i>"
        ) if r.status_code == 200 else f"🟡 Status Code: {r.status_code}"
    except Exception as e:
        status_text = f"🔴 Connection error: {html.escape(str(e))}"
    bot.edit_message_text(status_text, chat_id=chat_id, message_id=wait_msg.message_id)


# ============================================================
# Admin Panel & User Management (ADMIN ONLY)
# ============================================================

def open_admin_panel(chat_id: int, admin_id: int):
    if not is_admin(admin_id):
        return
    ADMIN_STATE.pop(admin_id, None)

    with DB_LOCK:
        total_users = len(DB["users"])
        total_searches = sum(u.get("searches", 0) for u in DB["users"].values())
        total_downloads = sum(u.get("downloads", 0) for u in DB["users"].values())
        total_mp3 = sum(u.get("audio_downloads", 0) for u in DB["users"].values())
        total_mp4 = sum(u.get("video_downloads", 0) for u in DB["users"].values())

    text = (
        "👑 <b>Admin Panel — User Management</b>\n\n"
        "<blockquote>"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
        f"🔍 <b>Total Searches:</b> <code>{total_searches}</code>\n"
        f"📥 <b>Total Downloads:</b> <code>{total_downloads}</code>\n"
        f"   ├ 🎵 <b>MP3:</b> <code>{total_mp3}</code>\n"
        f"   └ 🎬 <b>MP4:</b> <code>{total_mp4}</code>"
        "</blockquote>\n\n"
        "👇 <i>নিচের বাটন থেকে অপশন সিলেক্ট করুন:</i>"
    )
    bot.send_message(chat_id, text, reply_markup=get_admin_keyboard())


def handle_admin_all_users(chat_id: int, admin_id: int):
    if not is_admin(admin_id):
        return
    with DB_LOCK:
        users_list = list(DB["users"].values())

    if not users_list:
        bot.send_message(chat_id, "👥 কোনো ইউজার পাওয়া যায়নি।", reply_markup=get_admin_keyboard())
        return

    lines = [f"👥 <b>All Bot Users ({len(users_list)})</b>\n"]
    for idx, u in enumerate(users_list, 1):
        uid = u.get("id")
        name = html.escape(u.get("name", "User"))
        uname = f"@{html.escape(u['username'])}" if u.get("username") else "No Username"
        searches = u.get("searches", 0)
        downloads = u.get("downloads", 0)
        lines.append(
            f"<b>{idx}.</b> <a href=\"tg://user?id={uid}\">{name}</a> ({uname})\n"
            f"   └ 🆔 <code>{uid}</code> | 🔍 <code>{searches}</code> | 📥 <code>{downloads}</code>"
        )

    lines.append("\n💡 <i>নির্দিষ্ট ইউজারের হিস্ট্রি দেখতে</i> <b>🔍 User Details</b> <i>চাপুন।</i>")
    send_long_message(chat_id, "\n".join(lines), reply_markup=get_admin_keyboard())


def handle_admin_user_stats(chat_id: int, admin_id: int):
    if not is_admin(admin_id):
        return
    with DB_LOCK:
        users_list = list(DB["users"].values())

    if not users_list:
        bot.send_message(chat_id, "📊 এখনো কোনো ইউজার ডেটা নেই।", reply_markup=get_admin_keyboard())
        return

    users_sorted = sorted(
        users_list,
        key=lambda x: (x.get("downloads", 0), x.get("searches", 0)),
        reverse=True
    )

    lines = [f"📊 <b>User Activity Stats ({len(users_sorted)} Users)</b>\n"]
    for idx, u in enumerate(users_sorted, 1):
        uid = u.get("id")
        name = html.escape(u.get("name", "User"))
        uname = f"@{html.escape(u['username'])}" if u.get("username") else f"ID:{uid}"
        s_cnt = u.get("searches", 0)
        d_cnt = u.get("downloads", 0)
        mp3_cnt = u.get("audio_downloads", 0)
        mp4_cnt = u.get("video_downloads", 0)
        history = u.get("history", [])

        last_items = ""
        if history:
            recent_titles = [f"<code>[{h['type']}]</code> {html.escape(h['title'])}" for h in history[-3:]]
            last_items = "\n   └ 🎶 <i>Latest:</i> " + " | ".join(recent_titles)

        lines.append(
            f"<b>{idx}. {name}</b> ({uname}) • 🆔 <code>{uid}</code>\n"
            f"   ├ 🔍 Searches: <code>{s_cnt}</code> | 📥 Downloads: <code>{d_cnt}</code> (MP3: <code>{mp3_cnt}</code>, MP4: <code>{mp4_cnt}</code>)"
            f"{last_items}"
        )

    send_long_message(chat_id, "\n".join(lines), reply_markup=get_admin_keyboard())


def handle_admin_recent_downloads(chat_id: int, admin_id: int):
    if not is_admin(admin_id):
        return
    with DB_LOCK:
        recent = list(reversed(DB["recent"]))

    if not recent:
        bot.send_message(chat_id, "📥 এখনো কোনো ডাউনলোড হয়নি।", reply_markup=get_admin_keyboard())
        return

    lines = [f"📥 <b>Recent Downloads (Last {len(recent)})</b>\n"]
    for idx, item in enumerate(recent, 1):
        name = html.escape(item.get("name", "User"))
        uname = f"@{html.escape(item['username'])}" if item.get("username") else f"ID:{item.get('user_id')}"
        title = html.escape(item.get("title", "Track"))
        mtype = item.get("type", "MP3")
        ts = item.get("time", "")
        lines.append(
            f"<b>{idx}. [{mtype}]</b> {title}\n"
            f"   └ 👤 {name} ({uname}) • 🕒 <code>{ts}</code>"
        )

    send_long_message(chat_id, "\n".join(lines), reply_markup=get_admin_keyboard())


def handle_admin_user_lookup(chat_id: int, admin_id: int, query_str: str):
    if not is_admin(admin_id):
        return
    q = query_str.strip().lstrip("@").lower()

    found = None
    with DB_LOCK:
        if query_str.strip() in DB["users"]:
            found = DB["users"][query_str.strip()]
        else:
            for u in DB["users"].values():
                if str(u.get("id")) == q or (u.get("username") or "").lower() == q:
                    found = u
                    break

    if not found:
        bot.send_message(
            chat_id,
            f"❌ <b>User Not Found:</b> <code>{html.escape(query_str)}</code>\n"
            "সঠিক User ID অথবা @username পাঠান।",
            reply_markup=get_admin_back_keyboard()
        )
        return

    uid = found.get("id")
    name = html.escape(found.get("name", "User"))
    uname = f"@{html.escape(found['username'])}" if found.get("username") else "None"
    joined = found.get("joined_at", "Unknown")
    last_active = found.get("last_active", "Unknown")
    searches = found.get("searches", 0)
    downloads = found.get("downloads", 0)
    mp3_cnt = found.get("audio_downloads", 0)
    mp4_cnt = found.get("video_downloads", 0)
    history = found.get("history", [])

    lines = [
        f"👤 <b>User Full Report: <a href=\"tg://user?id={uid}\">{name}</a></b>\n",
        "<blockquote>",
        f"🆔 <b>User ID:</b> <code>{uid}</code>",
        f"🔗 <b>Username:</b> {uname}",
        f"📅 <b>Joined:</b> <code>{joined}</code>",
        f"🕒 <b>Last Active:</b> <code>{last_active}</code>",
        f"🔍 <b>Total Searches:</b> <code>{searches}</code>",
        f"📥 <b>Total Downloads:</b> <code>{downloads}</code> (🎵 MP3: <code>{mp3_cnt}</code> | 🎬 MP4: <code>{mp4_cnt}</code>)",
        "</blockquote>\n"
    ]

    if history:
        lines.append(f"🎶 <b>Downloaded Items (Last {len(history)}):</b>")
        for idx, h in enumerate(reversed(history), 1):
            lines.append(f"{idx}. <code>[{h['type']}]</code> {html.escape(h['title'])} <i>({h['time']})</i>")
    else:
        lines.append("🎶 <b>Downloaded Items:</b> <i>এখনো কিছু ডাউনলোড করেনি।</i>")

    ADMIN_STATE.pop(admin_id, None)
    send_long_message(chat_id, "\n".join(lines), reply_markup=get_admin_keyboard())


# ============================================================
# Text & Reply Keyboard Router
# ============================================================

def prompt_video_quality(chat_id: int, vid: str):
    meta = fetch_video_metadata(chat_id, vid)
    title = html.escape(meta.get("title", "YouTube Video"))
    set_bounded_dict(PENDING_VIDEO_QUALITY, chat_id, vid)
    bot.send_message(
        chat_id,
        "🎬 <b>Select Video Quality</b>\n\n"
        "<blockquote>"
        f"📌 <b>Title:</b> {title}\n"
        f"📺 <b>Channel:</b> {html.escape(meta.get('channel', 'Unknown'))}\n"
        f"⏰ <b>Duration:</b> <code>{meta.get('duration', '—')}</code>"
        "</blockquote>\n\n"
        "👇 <i>নিচের কিবোর্ড থেকে ভিডিও কোয়ালিটি বেছে নিন (অথবা 📁 Send as File):</i>",
        reply_markup=get_video_quality_keyboard()
    )


@bot.message_handler(content_types=["text"], func=lambda msg: bool(msg.text and not msg.text.startswith("/")))
def handle_text_query(message):
    text = message.text.strip()
    if not text:
        return

    chat_id = message.chat.id
    user = message.from_user
    uid = user.id if user else 0
    ensure_user_in_db(user)

    # 1) Main Menu & Navigation Buttons
    if text == "🔙 Main Menu":
        ADMIN_STATE.pop(uid, None)
        USER_MODE.pop(chat_id, None)
        PENDING_VIDEO_QUALITY.pop(chat_id, None)
        bot.send_message(
            chat_id,
            "🏠 <b>Main Menu</b>\n💡 <i>যেকোনো গানের নাম লিখুন অথবা 🎬 Video Downloader চাপুন!</i>",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if text == "🔍 Search Music":
        USER_MODE.pop(chat_id, None)
        bot.send_message(
            chat_id,
            "🔍 <b>Search Music:</b>\n\n"
            "<blockquote>"
            "• যেকোনো গানের নাম লিখুন: <code>Kesariya</code>\n"
            "• শিল্পীর নাম লিখুন: <code>Tanveer Evan song</code>"
            "</blockquote>\n\n"
            "👇 <i>এখন চ্যাটে গানের নাম লিখুন!</i>",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if text == "🎬 Video Downloader":
        USER_MODE[chat_id] = "awaiting_video_link"
        bot.send_message(
            chat_id,
            "🎬 <b>YouTube Video Downloader</b>\n\n"
            "<blockquote>"
            "📦 যেকোনো YouTube ভিডিও (২০০–৩০০+ MB পর্যন্ত) সরাসরি টেলিগ্রামেই ডাউনলোড করা যাবে।"
            "</blockquote>\n\n"
            "🔗 <i>এখন YouTube ভিডিওর লিংক (অথবা ভিডিওর নাম) পাঠান:</i>",
            reply_markup=get_cancel_menu_keyboard()
        )
        return

    if text == "⚡ Server Status":
        send_server_status(chat_id)
        return

    if text == "📖 Help & Guide":
        cmd_help(message)
        return

    # 2) Admin Panel Buttons (Strictly Admin Only)
    if text == "👑 Admin Panel":
        if is_admin(uid):
            open_admin_panel(chat_id, uid)
        return

    if text == "🔙 Back to Admin Panel":
        if is_admin(uid):
            open_admin_panel(chat_id, uid)
        return

    if text == "👥 All Users" and is_admin(uid):
        ADMIN_STATE.pop(uid, None)
        handle_admin_all_users(chat_id, uid)
        return

    if text == "📊 User Stats" and is_admin(uid):
        ADMIN_STATE.pop(uid, None)
        handle_admin_user_stats(chat_id, uid)
        return

    if text == "📥 Recent Downloads" and is_admin(uid):
        ADMIN_STATE.pop(uid, None)
        handle_admin_recent_downloads(chat_id, uid)
        return

    if text == "🔍 User Details" and is_admin(uid):
        ADMIN_STATE[uid] = "awaiting_user_lookup"
        bot.send_message(
            chat_id,
            "🔍 <b>Check User Details</b>\n\n"
            "যে ইউজারের ডিটেইলস দেখতে চান তার <b>User ID</b> অথবা <b>@username</b> লিখুন:",
            reply_markup=get_admin_back_keyboard()
        )
        return

    if is_admin(uid) and ADMIN_STATE.get(uid) == "awaiting_user_lookup":
        handle_admin_user_lookup(chat_id, uid, text)
        return

    # 3) Two-button Song Selection ("🎵 MP3" / "🎬 MP4")
    if text == "🎵 MP3":
        items = LAST_SEARCH_ITEMS.get(chat_id, [])
        if items:
            process_download(message, "audio", items[0]["id"])
        else:
            bot.send_message(chat_id, "⚠️ <b>Session expired.</b> আবার গানের নাম লিখে সার্চ করুন।", reply_markup=get_main_keyboard(uid))
        return

    if text == "🎬 MP4":
        items = LAST_SEARCH_ITEMS.get(chat_id, [])
        if items:
            prompt_video_quality(chat_id, items[0]["id"])
        else:
            bot.send_message(chat_id, "⚠️ <b>Session expired.</b> আবার গানের নাম লিখে সার্চ করুন।", reply_markup=get_main_keyboard(uid))
        return

    # 4) "📁 Send as File" -> Show Searched Song Title Button
    if text == "📁 Send as File":
        items = LAST_SEARCH_ITEMS.get(chat_id, [])
        if not items:
            bot.send_message(
                chat_id,
                "⚠️ <b>কোনো সার্চ করা গান/ভিডিও পাওয়া যায়নি।</b>\nপ্রথমে গানের নাম বা ভিডিও লিংক পাঠান।",
                reply_markup=get_main_keyboard(uid)
            )
            return

        bot.send_message(
            chat_id,
            "📁 <b>Send as File (Phone Save Mode)</b>\n\n"
            "👇 <i>আপনার গান/ভিডিও নিচে দেখানো হলো—ক্লিক করলে সরাসরি ফোনে সেভযোগ্য MP4 ফাইল হিসেবে পাবেন:</i>",
            reply_markup=get_file_picker_keyboard(chat_id, items)
        )
        return

    picker_map = FILE_PICKER_MAP.get(chat_id, {})
    if text in picker_map:
        vid = picker_map[text]
        process_download(message, "video", vid, quality="HD", as_document=True)
        return

    # 5) Video Quality Buttons (HD / 1080p / 480p / 144p)
    if text in ("🎬 HD (720p)", "🔥 1080p", "📺 480p / 360p", "📱 144p"):
        vid = PENDING_VIDEO_QUALITY.get(chat_id)
        if not vid:
            items = LAST_SEARCH_ITEMS.get(chat_id, [])
            vid = items[0]["id"] if items else None
        if not vid:
            bot.send_message(
                chat_id,
                "⚠️ <b>Session expired.</b> আবার ভিডিও লিংক বা গানের নাম পাঠান।",
                reply_markup=get_main_keyboard(uid)
            )
            return
        q_map = {
            "🎬 HD (720p)": "HD",
            "🔥 1080p": "1080p",
            "📺 480p / 360p": "480p",
            "📱 144p": "144p"
        }
        process_download(message, "video", vid, quality=q_map[text], as_document=False)
        return

    # 6) Direct YouTube URL or Video Downloader Mode
    vid = extract_video_id(text)
    if vid:
        USER_MODE.pop(chat_id, None)
        handle_direct_video(chat_id, user, vid)
        return

    if USER_MODE.get(chat_id) == "awaiting_video_link":
        USER_MODE.pop(chat_id, None)
        record_search(user)
        search_msg = bot.send_message(chat_id, f"🔍 <i>Finding video:</i> <b>{html.escape(text)}</b>...")
        results = search_youtube(text, limit=1)
        if not results:
            bot.edit_message_text("❌ কোনো ভিডিও পাওয়া যায়নি। সঠিক লিংক বা নাম পাঠান।", chat_id=chat_id, message_id=search_msg.message_id)
            return
        r = results[0]
        cache_put(chat_id, r["id"], r)
        set_bounded_dict(LAST_SEARCH_ITEMS, chat_id, [r])
        try:
            bot.delete_message(chat_id, search_msg.message_id)
        except Exception:
            pass
        prompt_video_quality(chat_id, r["id"])
        return

    # 7) Default: Search Song and Show Only 2 Buttons (MP3 / MP4)
    perform_search(chat_id, user, text)


# ============================================================
# Search, Direct Video & Download Flow
# ============================================================

def perform_search(chat_id: int, user, query: str):
    record_search(user)
    search_msg = bot.send_message(chat_id, f"🔍 <i>Searching for:</i> <b>{html.escape(query)}</b>...")

    results = search_youtube(query, limit=3)
    if not results:
        bot.edit_message_text(
            f"❌ No tracks found for: <b>'{html.escape(query)}'</b>\nPlease check your spelling and try again.",
            chat_id=chat_id,
            message_id=search_msg.message_id
        )
        return

    # Pick top non-live result if available
    best = results[0]
    for item in results:
        if item.get("duration") not in ("—", "N/A", ""):
            best = item
            break

    cache_put(chat_id, best["id"], best)
    set_bounded_dict(LAST_SEARCH_ITEMS, chat_id, [best])

    clean_title = html.escape(best["title"])
    clean_channel = html.escape(best["channel"])
    duration = best["duration"]
    views = best.get("views", "—")
    views_info = f"\n👁 <b>Views:</b> <code>{views}</code>" if views and views != "—" else ""

    card_text = (
        f"🎵 <b>{clean_title}</b>\n\n"
        "<blockquote>"
        f"👤 <b>Channel:</b> {clean_channel}\n"
        f"⏱ <b>Duration:</b> <code>{duration}</code>{views_info}"
        "</blockquote>\n\n"
        "👇 <b>নিচের বাটন থেকে MP3 অথবা MP4 সিলেক্ট করুন:</b>"
    )

    bot.edit_message_text(card_text, chat_id=chat_id, message_id=search_msg.message_id)
    bot.send_message(
        chat_id,
        "🎹 <i>অপশন বেছে নিন:</i>",
        reply_markup=get_song_two_buttons_keyboard()
    )


def handle_direct_video(chat_id: int, user, vid: str):
    record_search(user)
    status_msg = bot.send_message(chat_id, "🔍 <i>Analyzing YouTube video...</i>")
    meta = fetch_video_metadata(chat_id, vid)

    try:
        r = http_session.get(f"{API_BASE}/best?url={vid}&format=mp4", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                meta["title"] = data.get("video", {}).get("title", meta["title"])
                cache_put(chat_id, vid, meta)
    except Exception:
        pass

    set_bounded_dict(LAST_SEARCH_ITEMS, chat_id, [meta])
    try:
        bot.delete_message(chat_id, status_msg.message_id)
    except Exception:
        pass

    prompt_video_quality(chat_id, vid)


def process_download(message, media_type: str, vid: str, quality: str = "HD", as_document: bool = False):
    chat_id = message.chat.id
    user = message.from_user
    uid = user.id if user else 0
    username_raw = user.username if (user and user.username) else ""
    is_audio = (media_type == "audio")

    if username_raw:
        downloaded_by = f"@{html.escape(username_raw)}"
    else:
        downloaded_by = f"ID: <code>{uid}</code>"

    meta = fetch_video_metadata(chat_id, vid)
    title = meta.get("title", "Media Track")
    channel = meta.get("channel", "Unknown Artist")
    duration_str = meta.get("duration", "—")
    duration_sec = parse_duration_seconds(duration_str)
    views_str = meta.get("views", "—")
    published_str = meta.get("published", "—")

    clean_title = html.escape(title)
    clean_channel = html.escape(channel)
    clean_views = html.escape(views_str)
    clean_published = html.escape(published_str)

    views_info = f"  •  👁 <b>Views:</b> <code>{clean_views}</code>" if clean_views and clean_views != "—" else ""
    status_msg = bot.send_message(
        chat_id,
        "⚡ <b>Connecting to server...</b>\n\n"
        "<blockquote>"
        f"🎵 <b>{clean_title}</b>\n"
        f"📺 <b>Channel:</b> {clean_channel}\n"
        f"⏰ <b>Duration:</b> <code>{duration_str}</code>{views_info}"
        "</blockquote>"
    )

    selected_download_url = f"{API_BASE}/down?url={vid}&format={'m4a' if is_audio else 'mp4'}"
    temp_files = []

    try:
        video_has_audio = True
        audio_stream_url = None

        if is_audio:
            try:
                r_info = http_session.get(f"{API_BASE}/best?url={vid}&format=m4a", timeout=10)
                if r_info.status_code == 200:
                    api_meta = r_info.json()
                    if api_meta.get("success"):
                        title = api_meta.get("video", {}).get("title", title)
                        clean_title = html.escape(title)
                        selected_download_url = api_meta.get("selected", {}).get("download", selected_download_url)
            except Exception as err:
                logger.warning(f"Audio query warning for {vid}: {err}")
        else:
            api_title, selected_download_url, video_has_audio = pick_video_stream_from_api(vid, quality)
            if api_title:
                title = api_title
                clean_title = html.escape(title)

            if not video_has_audio:
                try:
                    r_aud = http_session.get(f"{API_BASE}/best?url={vid}&format=m4a", timeout=10)
                    if r_aud.status_code == 200:
                        d_aud = r_aud.json()
                        if d_aud.get("success"):
                            audio_stream_url = d_aud.get("selected", {}).get(
                                "download", f"{API_BASE}/down?url={vid}&format=m4a"
                            )
                except Exception:
                    pass

        ext = "m4a" if is_audio else "mp4"
        raw_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
        temp_files.append(raw_tmp.name)
        raw_tmp.close()

        media_label = "Song" if is_audio else (f"Video ({quality})" if not as_document else "File")
        success = download_with_progress(
            url=selected_download_url,
            output_path=raw_tmp.name,
            chat_id=chat_id,
            msg_id=status_msg.message_id,
            title=title,
            channel=channel,
            duration=duration_str,
            views=views_str,
            media_type=media_label
        )

        if not success:
            raise Exception("Download stream disconnected prematurely.")

        final_file_path = raw_tmp.name
        actual_audio_ext = "m4a"
        vid_w, vid_h = 0, 0

        if is_audio:
            safe_edit_message(
                "📤 <b>Preparing Song...</b>\n\n"
                "<blockquote>"
                f"🎵 <b>{clean_title}</b>\n"
                f"👤 <b>Artist:</b> {clean_channel}"
                "</blockquote>\n\n"
                "<code>[▰▰▰▰▰▰▰▰▰▱]</code> <b>90%</b>",
                chat_id,
                status_msg.message_id
            )
            mp3_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_files.append(mp3_tmp.name)
            mp3_tmp.close()

            if convert_to_mp3(raw_tmp.name, mp3_tmp.name, title, channel):
                final_file_path = mp3_tmp.name
                actual_audio_ext = "mp3"
                try:
                    os.remove(raw_tmp.name)
                except Exception:
                    pass
        else:
            working_video = raw_tmp.name

            if not video_has_audio and audio_stream_url:
                safe_edit_message(
                    "⚡ <b>Preparing Audio &amp; Video...</b>\n\n"
                    "<blockquote>"
                    f"🎬 <b>{clean_title}</b>"
                    "</blockquote>",
                    chat_id,
                    status_msg.message_id
                )
                aud_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".m4a")
                temp_files.append(aud_tmp.name)
                aud_tmp.close()

                aud_ok = download_with_progress(
                    url=audio_stream_url,
                    output_path=aud_tmp.name,
                    chat_id=chat_id,
                    msg_id=status_msg.message_id,
                    title=title,
                    channel=channel,
                    duration=duration_str,
                    views=views_str,
                    media_type="Audio Track"
                )
                if aud_ok:
                    muxed_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                    temp_files.append(muxed_tmp.name)
                    muxed_tmp.close()
                    if mux_video_audio(raw_tmp.name, aud_tmp.name, muxed_tmp.name):
                        working_video = muxed_tmp.name
                        try:
                            os.remove(raw_tmp.name)
                            os.remove(aud_tmp.name)
                        except Exception:
                            pass

            safe_edit_message(
                "📤 <b>Preparing Video for Phone...</b>\n\n"
                "<blockquote>"
                f"🎬 <b>{clean_title}</b>\n"
                f"🛠 <b>Quality:</b> {quality}"
                "</blockquote>",
                chat_id,
                status_msg.message_id
            )
            compat_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            temp_files.append(compat_tmp.name)
            compat_tmp.close()

            final_file_path, vid_w, vid_h = ensure_phone_compatible_mp4(working_video, compat_tmp.name, quality)
            if final_file_path != working_video:
                try:
                    os.remove(working_video)
                except Exception:
                    pass

        final_size = os.path.getsize(final_file_path)
        if final_size > max_allowed_bytes():
            raise FileTooLargeError(final_size)

        file_size_mb = round(final_size / (1024 * 1024), 2)

        thumb_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        temp_files.append(thumb_tmp.name)
        thumb_tmp.close()
        has_thumb = prepare_thumbnail(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", thumb_tmp.name)

        if is_audio:
            fmt_label = f"{actual_audio_ext.upper()} audio"
            quality_label = "192 kbps"
            dl_stat_type = "MP3"
            sending_label = "Sending Song"
        elif as_document:
            fmt_label = "MP4 File (Phone Save)"
            quality_label = f"{quality} (MP4)"
            dl_stat_type = "MP4 File"
            sending_label = "Sending File"
        else:
            fmt_label = "MP4 video"
            quality_label = {
                "144p": "144p",
                "480p": "480p / 360p",
                "HD": "HD (720p)",
                "1080p": "1080p Full HD"
            }.get(quality, quality)
            dl_stat_type = f"MP4 {quality}"
            sending_label = "Sending Video"

        extra_lines = []
        if clean_views and clean_views != "—":
            extra_lines.append(f"👁 <b>Views:</b> {clean_views}")
        if clean_published and clean_published != "—":
            extra_lines.append(f"📅 <b>Uploaded:</b> {clean_published}")
        extra_lines.append(f"📦 <b>File Size:</b> {file_size_mb} MB")
        extra_lines_str = "\n".join(extra_lines)

        yt_link = f"https://www.youtube.com/watch?v={vid}"
        direct_safe = html.escape(selected_download_url, quote=True)
        caption_text = (
            f"<b>{clean_title}</b>\n\n"
            "<blockquote>"
            f"📺 <b>Channel:</b> {clean_channel}\n"
            f"⏰ <b>Duration:</b> {duration_str}\n"
            f"{extra_lines_str}\n"
            f"🎧 <b>Format:</b> {fmt_label}\n"
            f"🛠 <b>Quality:</b> {quality_label}\n"
            f"🙋‍♂️ <b>Downloaded by:</b> {downloaded_by}\n"
            f"🔗 <a href=\"{yt_link}\">Open on YouTube</a>  •  📥 <a href=\"{direct_safe}\">Direct Save Link</a>"
            "</blockquote>\n\n"
            "👑 <i>Powered by <b>AYAN AHMED 🛐</b></i>"
        )

        clean_filename = re.sub(r'[\\/*?:"<>|]', "", title)[:50] or vid
        out_filename = f"{clean_filename}.{actual_audio_ext if is_audio else 'mp4'}"

        # Prefer Telethon MTProto (Supports 2 GB + Real Upload %)
        sent_ok = False
        if telethon_enabled():
            try:
                safe_edit_message(
                    f"📤 <b>{sending_label}...</b>\n\n"
                    "<blockquote>"
                    f"🎵 <b>{clean_title}</b>\n"
                    f"📺 <b>Channel:</b> {clean_channel}"
                    "</blockquote>\n\n"
                    "<code>[▱▱▱▱▱▱▱▱▱▱]</code> <b>0%</b>",
                    chat_id,
                    status_msg.message_id
                )
                sent_ok = send_via_telethon(
                    chat_id=chat_id,
                    username=username_raw,
                    file_path=final_file_path,
                    thumb_path=thumb_tmp.name if has_thumb else None,
                    caption=caption_text,
                    is_audio=is_audio,
                    as_document=as_document,
                    title=title,
                    channel=channel,
                    duration_sec=duration_sec,
                    vid_w=vid_w,
                    vid_h=vid_h,
                    file_name=out_filename,
                    msg_id=status_msg.message_id,
                    duration_str=duration_str,
                    sending_label=sending_label
                )
            except Exception as te:
                logger.warning(f"Telethon send failed, checking fallback: {te}")
                if final_size > 50 * 1024 * 1024:
                    raise

        # Fallback to HTTP Bot API for files <= 50 MB if Telethon wasn't used
        if not sent_ok:
            safe_edit_message(
                f"📤 <b>{sending_label}...</b>\n\n"
                "<blockquote>"
                f"🎵 <b>{clean_title}</b>\n"
                f"📦 <b>Size:</b> {file_size_mb} MB"
                "</blockquote>\n\n"
                "<code>[▰▰▰▰▰▰▰▰▰▱]</code> <b>95%</b>",
                chat_id,
                status_msg.message_id
            )
            if is_audio:
                try:
                    if has_thumb:
                        with open(final_file_path, "rb") as af, open(thumb_tmp.name, "rb") as tf:
                            bot.send_audio(chat_id, af, title=title, performer=channel, duration=duration_sec, thumbnail=tf, caption=caption_text, timeout=120)
                    else:
                        with open(final_file_path, "rb") as af:
                            bot.send_audio(chat_id, af, title=title, performer=channel, duration=duration_sec, caption=caption_text, timeout=120)
                except Exception:
                    with open(final_file_path, "rb") as af:
                        bot.send_document(chat_id, af, caption=caption_text, visible_file_name=out_filename, timeout=120)
            elif as_document:
                with open(final_file_path, "rb") as df:
                    bot.send_document(chat_id, df, caption=caption_text, visible_file_name=out_filename, timeout=180)
            else:
                v_kwargs = {"caption": caption_text, "duration": duration_sec, "supports_streaming": True, "timeout": 180}
                if vid_w > 0 and vid_h > 0:
                    v_kwargs["width"] = vid_w
                    v_kwargs["height"] = vid_h
                try:
                    if has_thumb:
                        with open(final_file_path, "rb") as vf, open(thumb_tmp.name, "rb") as tf:
                            bot.send_video(chat_id, vf, thumbnail=tf, **v_kwargs)
                    else:
                        with open(final_file_path, "rb") as vf:
                            bot.send_video(chat_id, vf, **v_kwargs)
                except Exception:
                    with open(final_file_path, "rb") as vf:
                        bot.send_document(chat_id, vf, caption=caption_text, visible_file_name=out_filename, timeout=180)

        record_download(user, title, dl_stat_type)

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except FileTooLargeError as fle:
        size_mb = round(fle.size_bytes / (1024 * 1024), 1)
        safe_url = html.escape(selected_download_url, quote=True)
        safe_edit_message(
            f"⚠️ <b>File Size: {size_mb} MB</b>\n\n"
            "This file exceeds the upload limit.\n"
            f"🚀 <a href=\"{safe_url}\"><b>Click Here for Direct Download</b></a>",
            chat_id,
            status_msg.message_id
        )

    except Exception as exc:
        logger.error(f"Error handling download for {vid}: {exc}", exc_info=True)
        safe_url = html.escape(selected_download_url, quote=True)
        safe_edit_message(
            "❌ <b>Download Error:</b>\n\n"
            "<blockquote>"
            "Could not complete delivery directly to Telegram.\n"
            f"🚀 <a href=\"{safe_url}\"><b>Try Direct Download Link</b></a>"
            "</blockquote>",
            chat_id,
            status_msg.message_id
        )

    finally:
        for f_path in temp_files:
            try:
                if os.path.exists(f_path):
                    os.remove(f_path)
            except Exception:
                pass


def main():
    load_db()
    start_telethon_worker()
    print("=" * 60)
    print("🚀 Music & Video Bot is running! Powered by AYAN AHMED 🛐")
    print(f"🤖 Telethon 2GB Mode: {'ON' if telethon_enabled() else 'OFF'} | Admins: {len(ADMIN_IDS)}")
    print("=" * 60)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(timeout=60, long_polling_timeout=25, skip_pending=True)


if __name__ == "__main__":
    main()
