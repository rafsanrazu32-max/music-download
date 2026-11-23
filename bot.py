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

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_BASE = os.getenv("API_BASE", "https://honeysee-api.vqx.workers.dev").rstrip("/")
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB Telegram Bot upload limit
USERS_FILE = os.getenv("USERS_FILE", "users.json")

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

# Bounded caches
SEARCH_CACHE = OrderedDict()         # {chat_id: OrderedDict({vid: meta})}
LAST_SEARCH_RESULTS = OrderedDict()  # {chat_id: [vid1, vid2, ...]}
DIRECT_VIDEO_CACHE = OrderedDict()   # {chat_id: vid}
MAX_CACHED_CHATS = 200
MAX_ITEMS_PER_CHAT = 20

# Admin interactive states {admin_id: state_str}
ADMIN_STATE = {}

# Persistent-during-runtime user & stats database
DB_LOCK = threading.Lock()
DB = {
    "users": {},    # {str(user_id): {...}}
    "recent": []    # [{"user_id": int, "name": str, "username": str, "title": str, "type": str, "time": str}]
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
                    # Migrate old list format [uid1, uid2]
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
    """Ensure user exists in DB. Returns (is_new, total_users)."""
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
        if media_fmt == "MP3":
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
    """Send full user details to ADMIN_IDS when a user starts the bot for the first time."""
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
# Colored Reply Keyboards (NO Inline Buttons)
# ============================================================

def kb_btn(text: str, style: str = None) -> types.KeyboardButton:
    """Create a colored Reply Keyboard button (primary=blue, success=green, danger=red)."""
    if style:
        return types.KeyboardButton(text, style=style)
    return types.KeyboardButton(text)


def get_main_keyboard(user_id: int = 0) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    markup.row(
        kb_btn("🔍 Search Music", style="primary"),
        kb_btn("⚡ Server Status", style="success")
    )
    markup.row(
        kb_btn("📖 Help & Guide", style="primary"),
        kb_btn("✖️ Hide Menu", style="danger")
    )
    if is_admin(user_id):
        markup.row(kb_btn("👑 Admin Panel", style="success"))
    return markup


def get_search_results_keyboard(count: int) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    for i in range(min(count, 5)):
        em = number_emojis[i]
        markup.row(
            kb_btn(f"{em} 🎵 MP3", style="success"),
            kb_btn(f"{em} 🎬 MP4", style="primary")
        )
    markup.row(kb_btn("🔙 Main Menu", style="danger"))
    return markup


def get_direct_link_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    markup.row(
        kb_btn("🎵 Download MP3 (192k)", style="success"),
        kb_btn("🎬 Download MP4 (HD)", style="primary")
    )
    markup.row(kb_btn("❌ Cancel", style="danger"))
    return markup


def get_admin_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
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
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    markup.row(kb_btn("🔙 Back to Admin Panel", style="danger"))
    return markup


def send_long_message(chat_id: int, text: str, reply_markup=None):
    """Safely send messages exceeding Telegram's 4096 character limit."""
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
            # Fallback without HTML in case a split broke a tag
            plain = re.sub(r"<[^>]+>", "", chunk)
            bot.send_message(chat_id, plain, parse_mode=None, reply_markup=rm)


# ============================================================
# Core Helpers & Download Engine
# ============================================================

class FileTooLargeError(Exception):
    def __init__(self, size_bytes: int):
        self.size_bytes = size_bytes
        super().__init__(f"File size {size_bytes} exceeds 50 MB limit")


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


def set_last_search(chat_id: int, vids: list):
    if chat_id in LAST_SEARCH_RESULTS:
        LAST_SEARCH_RESULTS.move_to_end(chat_id)
    elif len(LAST_SEARCH_RESULTS) >= MAX_CACHED_CHATS:
        LAST_SEARCH_RESULTS.popitem(last=False)
    LAST_SEARCH_RESULTS[chat_id] = vids


def set_direct_video(chat_id: int, vid: str):
    if chat_id in DIRECT_VIDEO_CACHE:
        DIRECT_VIDEO_CACHE.move_to_end(chat_id)
    elif len(DIRECT_VIDEO_CACHE) >= MAX_CACHED_CHATS:
        DIRECT_VIDEO_CACHE.popitem(last=False)
    DIRECT_VIDEO_CACHE[chat_id] = vid


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
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
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
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    except Exception as e:
        logger.warning(f"Muxing failed: {e}")
        return False


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

    clean_title = html.escape(title)
    clean_channel = html.escape(channel)
    clean_views = html.escape(views)

    with http_session.get(url, stream=True, timeout=45) as resp:
        resp.raise_for_status()
        total_bytes = int(resp.headers.get("Content-Length", 0) or 0)

        if total_bytes > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(total_bytes)

        total_mb = round(total_bytes / (1024 * 1024), 1) if total_bytes else 0

        with open(output_path, "wb") as f_out:
            for chunk in resp.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                f_out.write(chunk)
                downloaded += len(chunk)

                if downloaded > MAX_FILE_SIZE_BYTES:
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
        "🎵 <b>Download MP3 audio</b> — 192 kbps + Cover Art\n"
        "🎬 <b>Download MP4 video</b> — HD Quality\n"
        "🔍 <b>Search YouTube</b> — Instant Results"
        "</blockquote>\n\n"
        "💡 <i>Use a title, artist, or direct YouTube link.</i>\n"
        "👑 <i>Powered by <b>AYAN AHMED 🛐</b></i>"
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id if message.from_user else 0
    if uid in ADMIN_STATE:
        ADMIN_STATE.pop(uid, None)

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
        "• <code>/search &lt;query&gt;</code> — Search YouTube\n"
        "• <code>/ping</code> — Check response latency\n"
        "• <code>/status</code> — Check server health"
        "</blockquote>\n\n"
        "💡 <i>Or just type any song name or paste a YouTube link directly!</i>\n"
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
    send_server_status(message.chat.id, message.from_user.id if message.from_user else 0)


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


def send_server_status(chat_id: int, user_id: int):
    wait_msg = bot.send_message(chat_id, "🔄 <i>Checking status...</i>")
    try:
        r = http_session.get(f"{API_BASE}/health", timeout=5)
        status_text = (
            "🟢 <b>System Status: Operational</b>\n\n"
            "<blockquote>"
            "⚡ <b>Streaming Engine:</b> Active\n"
            "🎵 <b>Audio Quality:</b> 192 kbps MP3 (ID3)\n"
            "🎬 <b>Video Quality:</b> HD MP4\n"
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

    # Sort by downloads desc, then searches desc
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
# Text & Reply Keyboard Button Router
# ============================================================

@bot.message_handler(content_types=["text"], func=lambda msg: bool(msg.text and not msg.text.startswith("/")))
def handle_text_query(message):
    text = message.text.strip()
    if not text:
        return

    chat_id = message.chat.id
    user = message.from_user
    uid = user.id if user else 0
    ensure_user_in_db(user)

    # 1) Main Menu & Cancel Keyboard Buttons
    if text == "🔙 Main Menu" or text == "❌ Cancel":
        ADMIN_STATE.pop(uid, None)
        bot.send_message(
            chat_id,
            "🏠 <b>Main Menu</b>\n💡 <i>যেকোনো গানের নাম বা YouTube লিংক পাঠান!</i>",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if text == "✖️ Hide Menu":
        ADMIN_STATE.pop(uid, None)
        bot.send_message(
            chat_id,
            "✅ <b>Menu hidden.</b> আবার মেনু চালু করতে <code>/start</code> লিখুন।",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    if text == "🔍 Search Music":
        bot.send_message(
            chat_id,
            "🔍 <b>How to Search:</b>\n\n"
            "<blockquote>"
            "• যেকোনো গানের নাম লিখুন: <code>Kesariya</code>\n"
            "• শিল্পীর নাম লিখুন: <code>Tanveer Evan song</code>\n"
            "• সরাসরি YouTube লিংক দিন: <code>https://youtu.be/...</code>"
            "</blockquote>\n\n"
            "👇 <i>এখন চ্যাটে গানের নাম বা লিংক পাঠান!</i>",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if text == "⚡ Server Status":
        send_server_status(chat_id, uid)
        return

    if text == "📖 Help & Guide":
        cmd_help(message)
        return

    # 2) Admin Panel Keyboard Buttons (Strictly Admin Only)
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

    # If Admin is currently entering a User ID / Username to inspect
    if is_admin(uid) and ADMIN_STATE.get(uid) == "awaiting_user_lookup":
        handle_admin_user_lookup(chat_id, uid, text)
        return

    # 3) Search Result Keyboard Buttons (1️⃣ 🎵 MP3 / 1️⃣ 🎬 MP4 ... 5️⃣)
    m_res = re.match(r"^([1-5])️⃣\s*(🎵 MP3|🎬 MP4)$", text)
    if m_res:
        idx = int(m_res.group(1)) - 1
        media_type = "audio" if "MP3" in m_res.group(2) else "video"
        vids = LAST_SEARCH_RESULTS.get(chat_id, [])
        if 0 <= idx < len(vids):
            process_download(message, media_type, vids[idx])
        else:
            bot.send_message(
                chat_id,
                "⚠️ <b>Search session expired.</b> অনুগ্রহ করে আবার গানের নাম লিখে সার্চ করুন।",
                reply_markup=get_main_keyboard(uid)
            )
        return

    # 4) Direct YouTube Link Keyboard Buttons
    if text == "🎵 Download MP3 (192k)":
        vid = DIRECT_VIDEO_CACHE.get(chat_id)
        if vid:
            process_download(message, "audio", vid)
        else:
            bot.send_message(chat_id, "⚠️ <b>Link expired.</b> আবার লিংক পাঠান।", reply_markup=get_main_keyboard(uid))
        return

    if text == "🎬 Download MP4 (HD)":
        vid = DIRECT_VIDEO_CACHE.get(chat_id)
        if vid:
            process_download(message, "video", vid)
        else:
            bot.send_message(chat_id, "⚠️ <b>Link expired.</b> আবার লিংক পাঠান।", reply_markup=get_main_keyboard(uid))
        return

    # 5) Direct YouTube URL or Normal Search Query
    vid = extract_video_id(text)
    if vid:
        handle_direct_video(chat_id, user, vid)
        return

    perform_search(chat_id, user, text)


# ============================================================
# Search, Direct Video & Download Flow
# ============================================================

def perform_search(chat_id: int, user, query: str):
    uid = user.id if user else 0
    record_search(user)
    search_msg = bot.send_message(chat_id, f"🔍 <i>Searching for:</i> <b>{html.escape(query)}</b>...")

    results = search_youtube(query, limit=5)
    if not results:
        bot.edit_message_text(
            f"❌ No tracks found for: <b>'{html.escape(query)}'</b>\nPlease check your spelling and try again.",
            chat_id=chat_id,
            message_id=search_msg.message_id
        )
        return

    vids = []
    for r in results:
        cache_put(chat_id, r["id"], r)
        vids.append(r["id"])
    set_last_search(chat_id, vids)

    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    msg_lines = [
        f"🔍 <b>Search Results:</b> <code>{html.escape(query)}</code>\n",
        "<blockquote>"
    ]

    for idx, r in enumerate(results):
        emoji_num = number_emojis[idx] if idx < len(number_emojis) else f"{idx+1}."
        clean_title = html.escape(r["title"])
        clean_channel = html.escape(r["channel"])
        duration = r["duration"]
        views = r.get("views", "—")

        msg_lines.append(f"{emoji_num} <b>{clean_title}</b>")
        views_info = f" • 👁 <code>{views}</code>" if views and views != "—" else ""
        msg_lines.append(f"└ 👤 <code>{clean_channel}</code> • ⏱ <code>{duration}</code>{views_info}\n")

    msg_lines.append("</blockquote>\n👇 <b>নিচের রঙিন কিবোর্ড বাটন থেকে MP3 অথবা MP4 বেছে নিন:</b>")

    bot.edit_message_text("\n".join(msg_lines), chat_id=chat_id, message_id=search_msg.message_id)
    bot.send_message(
        chat_id,
        "🎹 <i>নিচে ১–৫ নম্বরের বাটনগুলো দেওয়া হলো:</i>",
        reply_markup=get_search_results_keyboard(len(results))
    )


def handle_direct_video(chat_id: int, user, vid: str):
    record_search(user)
    status_msg = bot.send_message(chat_id, "🔍 <i>Analyzing video...</i>")
    meta = fetch_video_metadata(chat_id, vid)
    title = meta.get("title", "YouTube Video")

    try:
        r = http_session.get(f"{API_BASE}/best?url={vid}&format=mp4", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                title = data.get("video", {}).get("title", title)
                meta["title"] = title
                cache_put(chat_id, vid, meta)
    except Exception:
        pass

    set_direct_video(chat_id, vid)

    card_text = (
        "🎬 <b>YouTube Media Detected</b>\n\n"
        "<blockquote>"
        f"📌 <b>Title:</b> {html.escape(title)}\n"
        f"📺 <b>Channel:</b> {html.escape(meta.get('channel', 'Unknown'))}\n"
        f"⏰ <b>Duration:</b> <code>{meta.get('duration', '—')}</code>\n"
        f"🆔 <b>Video ID:</b> <code>{vid}</code>"
        "</blockquote>\n\n"
        "👇 <b>নিচের কিবোর্ড থেকে ফরম্যাট সিলেক্ট করুন:</b>"
    )
    bot.edit_message_text(card_text, chat_id=chat_id, message_id=status_msg.message_id)
    bot.send_message(chat_id, "🎹 <i>Format options:</i>", reply_markup=get_direct_link_keyboard())


def process_download(message, media_type: str, vid: str):
    chat_id = message.chat.id
    user = message.from_user
    uid = user.id if user else 0
    is_audio = (media_type == "audio")
    req_format = "m4a" if is_audio else "mp4"

    # Downloaded by: @username or ID
    if user and user.username:
        downloaded_by = f"@{html.escape(user.username)}"
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

    best_api_url = f"{API_BASE}/best?url={vid}&format={req_format}"
    fallback_down_url = f"{API_BASE}/down?url={vid}&format={req_format}"
    selected_download_url = fallback_down_url
    temp_files = []

    try:
        video_has_audio = True
        audio_stream_url = None

        try:
            r_info = http_session.get(best_api_url, timeout=10)
            if r_info.status_code == 200:
                api_meta = r_info.json()
                if api_meta.get("success"):
                    title = api_meta.get("video", {}).get("title", title)
                    clean_title = html.escape(title)
                    selected_info = api_meta.get("selected", {})
                    selected_download_url = selected_info.get("download", fallback_down_url)
                    video_has_audio = selected_info.get("hasAudio", True)
        except Exception as err:
            logger.warning(f"Metadata query warning for {vid}: {err}")

        needs_audio_mapping = (not is_audio) and (not video_has_audio)
        if needs_audio_mapping:
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

        success = download_with_progress(
            url=selected_download_url,
            output_path=raw_tmp.name,
            chat_id=chat_id,
            msg_id=status_msg.message_id,
            title=title,
            channel=channel,
            duration=duration_str,
            views=views_str,
            media_type="Audio" if is_audio else "Video"
        )

        if not success:
            raise Exception("Download stream disconnected prematurely.")

        final_file_path = raw_tmp.name
        actual_audio_ext = "m4a"

        if is_audio:
            safe_edit_message(
                "🎛 <b>Converting to MP3 (192 kbps) &amp; Adding ID3 Tags...</b>\n\n"
                "<blockquote>"
                f"🎵 <b>{clean_title}</b>\n"
                f"👤 <b>Artist:</b> {clean_channel}"
                "</blockquote>",
                chat_id,
                status_msg.message_id
            )
            mp3_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_files.append(mp3_tmp.name)
            mp3_tmp.close()

            if convert_to_mp3(raw_tmp.name, mp3_tmp.name, title, channel):
                final_file_path = mp3_tmp.name
                actual_audio_ext = "mp3"

        elif needs_audio_mapping and audio_stream_url:
            safe_edit_message(
                "⚡ <b>Mapping Audio Track...</b>\n\n"
                "<blockquote>"
                f"🎬 <b>{clean_title}</b>\n"
                "🔊 Combining high-definition video with audio stream..."
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
                    final_file_path = muxed_tmp.name

        final_size = os.path.getsize(final_file_path)
        if final_size > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(final_size)

        file_size_mb = round(final_size / (1024 * 1024), 2)

        thumb_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        temp_files.append(thumb_tmp.name)
        thumb_tmp.close()
        has_thumb = prepare_thumbnail(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", thumb_tmp.name)

        safe_edit_message(
            "⚡ <b>Uploading to Telegram...</b>\n\n"
            "<blockquote>"
            f"🎵 <b>{clean_title}</b>\n"
            f"📺 <b>Channel:</b> {clean_channel}\n"
            f"⏰ <b>Duration:</b> {duration_str}"
            "</blockquote>\n\n"
            "<code>[▰▰▰▰▰▰▰▰▰▰]</code> <b>100%</b>\n"
            "📤 <i>Delivering track to your chat...</i>",
            chat_id,
            status_msg.message_id
        )

        fmt_label = f"{actual_audio_ext.upper()} audio" if is_audio else "MP4 video"
        quality_label = "192 kbps" if is_audio else "HD 720p / 1080p"

        extra_lines = []
        if clean_views and clean_views != "—":
            extra_lines.append(f"👁 <b>Views:</b> {clean_views}")
        if clean_published and clean_published != "—":
            extra_lines.append(f"📅 <b>Uploaded:</b> {clean_published}")
        extra_lines.append(f"📦 <b>File Size:</b> {file_size_mb} MB")
        extra_lines_str = "\n".join(extra_lines)

        yt_link = f"https://www.youtube.com/watch?v={vid}"
        caption_text = (
            f"<b>{clean_title}</b>\n\n"
            "<blockquote>"
            f"📺 <b>Channel:</b> {clean_channel}\n"
            f"⏰ <b>Duration:</b> {duration_str}\n"
            f"{extra_lines_str}\n"
            f"🎧 <b>Format:</b> {fmt_label}\n"
            f"🛠 <b>Quality:</b> {quality_label}\n"
            f"🙋‍♂️ <b>Downloaded by:</b> {downloaded_by}\n"
            f"🔗 <a href=\"{yt_link}\">Open on YouTube</a>"
            "</blockquote>\n\n"
            "👑 <i>Powered by <b>AYAN AHMED 🛐</b></i>"
        )

        clean_filename = re.sub(r'[\\/*?:"<>|]', "", title)[:50] or vid
        sent = False

        if is_audio:
            if has_thumb:
                try:
                    with open(final_file_path, "rb") as a_file, open(thumb_tmp.name, "rb") as t_file:
                        bot.send_audio(
                            chat_id,
                            a_file,
                            title=title,
                            performer=channel,
                            duration=duration_sec,
                            thumbnail=t_file,
                            caption=caption_text,
                            timeout=120
                        )
                    sent = True
                except Exception as e1:
                    logger.warning(f"send_audio with thumbnail failed: {e1}")

            if not sent:
                try:
                    with open(final_file_path, "rb") as a_file:
                        bot.send_audio(
                            chat_id,
                            a_file,
                            title=title,
                            performer=channel,
                            duration=duration_sec,
                            caption=caption_text,
                            timeout=120
                        )
                    sent = True
                except Exception as e2:
                    logger.warning(f"send_audio without thumbnail failed: {e2}")

            if not sent:
                with open(final_file_path, "rb") as a_file:
                    bot.send_document(
                        chat_id,
                        a_file,
                        caption=caption_text,
                        visible_file_name=f"{clean_filename}.{actual_audio_ext}",
                        timeout=120
                    )
        else:
            try:
                if has_thumb:
                    with open(final_file_path, "rb") as v_file, open(thumb_tmp.name, "rb") as t_file:
                        bot.send_video(
                            chat_id,
                            v_file,
                            caption=caption_text,
                            duration=duration_sec,
                            thumbnail=t_file,
                            supports_streaming=True,
                            timeout=180
                        )
                else:
                    with open(final_file_path, "rb") as v_file:
                        bot.send_video(
                            chat_id,
                            v_file,
                            caption=caption_text,
                            duration=duration_sec,
                            supports_streaming=True,
                            timeout=180
                        )
            except Exception as ev:
                logger.warning(f"send_video failed: {ev}")
                with open(final_file_path, "rb") as v_file:
                    bot.send_document(
                        chat_id,
                        v_file,
                        caption=caption_text,
                        visible_file_name=f"{clean_filename}.mp4",
                        timeout=180
                    )

        record_download(user, title, "MP3" if is_audio else "MP4")

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except FileTooLargeError as fle:
        size_mb = round(fle.size_bytes / (1024 * 1024), 1)
        safe_url = html.escape(selected_download_url, quote=True)
        safe_edit_message(
            f"⚠️ <b>File Size: {size_mb} MB</b>\n\n"
            "This file exceeds Telegram's 50 MB bot limit.\n"
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
    print("=" * 60)
    print("🚀 Music & Video Bot is running! Powered by AYAN AHMED 🛐")
    print(f"🤖 Connected to Telegram successfully... (Admins: {len(ADMIN_IDS)})")
    print("=" * 60)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(timeout=60, long_polling_timeout=25, skip_pending=True)


if __name__ == "__main__":
    main()
