"""
DreamVibes-style Telegram Bot — YouTube-powered video feed
-----------------------------------------------------------
Features:
  - /start [deep-link]  -> begins a personal video feed for that user
  - /search <keywords>  -> real YouTube search, resets the feed to results
  - Inline buttons: Like, Save, Comment, Next
      * Like / Save   -> stored locally per-user (instant, no login needed)
      * Comment        -> shows real YouTube comments for that video
      * Next           -> advances THIS USER's position only (per-user state)

Secrets are read from environment variables — NEVER hardcode them here:
  BOT_TOKEN        -> from @BotFather
  YOUTUBE_API_KEY  -> from Google Cloud Console

Run locally for testing:
  BOT_TOKEN=xxx YOUTUBE_API_KEY=yyy python bot.py

On Render (deploy as a FREE "Web Service", not a paid Background Worker):
  Build Command:  pip install python-telegram-bot==21.6 requests
  Start Command:  python bot.py
  Environment variables: BOT_TOKEN, YOUTUBE_API_KEY  (set in Render dashboard, not in code)

Note: Render's free tier only exists for "Web Service" type, which requires
listening on a port. This script starts a tiny dummy HTTP server in a
background thread purely to satisfy that requirement -- it does nothing
else. The actual bot logic still runs via Telegram polling, unaffected.
"""

import os
import json
import sqlite3
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    LinkPreviewOptions,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
DB_PATH = "bot_data.db"
DEFAULT_QUERY = "trending shorts"  # used when a user starts with no search yet

if not BOT_TOKEN or not YOUTUBE_API_KEY:
    raise RuntimeError(
        "Missing BOT_TOKEN or YOUTUBE_API_KEY environment variables. "
        "Set them in Render's Environment tab (or locally before running)."
    )


# ---------------------------------------------------------------------------
# Database (per-user feed position, likes, saves)
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            video_ids TEXT DEFAULT '[]',
            current_index INTEGER DEFAULT 0,
            liked_ids TEXT DEFAULT '[]',
            saved_ids TEXT DEFAULT '[]'
        )
        """
    )
    return conn


def get_user(user_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        row = (user_id, "[]", 0, "[]", "[]")
    conn.close()
    return {
        "user_id": row[0],
        "video_ids": json.loads(row[1]),
        "current_index": row[2],
        "liked_ids": json.loads(row[3]),
        "saved_ids": json.loads(row[4]),
    }


def save_user(user_id: int, **fields):
    conn = db_connect()
    for key in ("video_ids", "liked_ids", "saved_ids"):
        if key in fields:
            fields[key] = json.dumps(fields[key])
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE users SET {set_clause} WHERE user_id=?",
        (*fields.values(), user_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# YouTube API helpers
# ---------------------------------------------------------------------------
def youtube_search(query: str, max_results: int = 15):
    """Real YouTube search — returns a list of video IDs."""
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [item["id"]["videoId"] for item in items if "videoId" in item.get("id", {})]


def youtube_video_details(video_id: str):
    """Fetch title, channel, like/view counts for a single video."""
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,statistics",
        "id": video_id,
        "key": YOUTUBE_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        return None
    item = items[0]
    snippet = item["snippet"]
    stats = item.get("statistics", {})
    return {
        "id": video_id,
        "title": snippet.get("title", "Untitled"),
        "channel": snippet.get("channelTitle", "Unknown"),
        "views": stats.get("viewCount", "0"),
        "likes": stats.get("likeCount", "0"),
        "url": f"https://youtu.be/{video_id}",
    }


def youtube_top_comments(video_id: str, max_results: int = 3):
    """Fetch a few real top-level comments for a video (public, no login needed)."""
    url = "https://www.googleapis.com/youtube/v3/commentThreads"
    params = {
        "part": "snippet",
        "videoId": video_id,
        "maxResults": max_results,
        "order": "relevance",
        "key": YOUTUBE_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except requests.HTTPError:
        return []  # comments disabled on this video, or other API restriction
    comments = []
    for item in items:
        top = item["snippet"]["topLevelComment"]["snippet"]
        comments.append(f"👤 {top['authorDisplayName']}: {top['textDisplay']}")
    return comments


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------
def build_caption(video: dict, liked: bool, saved: bool) -> str:
    heart = "❤️" if liked else "🤍"
    star = "⭐" if saved else "☆"
    return (
        f"🎬 *{video['title']}*\n"
        f"📺 {video['channel']}\n"
        f"👁 {video['views']} views  |  {heart} {video['likes']} likes\n\n"
        f"🔗 {video['url']}\n\n"
        f"{heart} Liked   {star} Saved"
    )


def build_keyboard(video_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❤️ Like", callback_data=f"like:{video_id}"),
                InlineKeyboardButton("⭐ Save", callback_data=f"save:{video_id}"),
                InlineKeyboardButton("💬 Comments", callback_data=f"comment:{video_id}"),
            ],
            [
                InlineKeyboardButton("🔍 Search", callback_data="search_prompt"),
                InlineKeyboardButton("➡️ Next", callback_data="next"),
            ],
        ]
    )


async def send_current_video(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    user = get_user(user_id)
    if not user["video_ids"]:
        await context.bot.send_message(chat_id, "No videos loaded yet — tap 🔍 Search below.")
        return
    idx = user["current_index"] % len(user["video_ids"])
    video_id = user["video_ids"][idx]
    video = youtube_video_details(video_id)
    if not video:
        # skip a broken/unavailable video automatically
        user["current_index"] = (idx + 1) % len(user["video_ids"])
        save_user(user_id, current_index=user["current_index"])
        await send_current_video(user_id, context, chat_id)
        return
    liked = video_id in user["liked_ids"]
    saved = video_id in user["saved_ids"]
    await context.bot.send_message(
        chat_id,
        build_caption(video, liked, saved),
        parse_mode="Markdown",
        reply_markup=build_keyboard(video_id),
        # prefer_large_media asks Telegram to render the biggest possible
        # inline preview (closer to a "video card" look). Whether it plays
        # inline vs. opens the YouTube app still depends on the user's
        # Telegram client -- that part isn't controllable from bot code.
        link_preview_options=LinkPreviewOptions(
            is_disabled=False,
            prefer_large_media=True,
        ),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user = get_user(user_id)
    if not user["video_ids"]:
        video_ids = youtube_search(DEFAULT_QUERY)
        save_user(user_id, video_ids=video_ids, current_index=0)
    await context.bot.send_message(
        chat_id,
        "🎥 Welcome! Here's your feed — tap ➡️ Next to keep scrolling, "
        "or tap 🔍 Search to find something specific.",
    )
    await send_current_video(user_id, context, chat_id)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    query = " ".join(context.args) if context.args else None
    if not query:
        await context.bot.send_message(chat_id, "Usage: /search cooking recipes")
        return
    video_ids = youtube_search(query)
    if not video_ids:
        await context.bot.send_message(chat_id, f"No results found for '{query}'.")
        return
    save_user(user_id, video_ids=video_ids, current_index=0)
    await context.bot.send_message(chat_id, f"🔎 Results for: {query}")
    await send_current_video(user_id, context, chat_id)


async def handle_search_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the user's typed reply to the '🔍 What do you want to search for?' prompt."""
    replied_to = update.message.reply_to_message
    if not replied_to or "search for" not in (replied_to.text or ""):
        return  # not a reply to our search prompt -- ignore
    query = update.message.text.strip()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    video_ids = youtube_search(query)
    if not video_ids:
        await context.bot.send_message(chat_id, f"No results found for '{query}'.")
        return
    save_user(user_id, video_ids=video_ids, current_index=0)
    await context.bot.send_message(chat_id, f"🔎 Results for: {query}")
    await send_current_video(user_id, context, chat_id)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data = query.data

    if data == "next":
        user = get_user(user_id)
        if user["video_ids"]:
            new_index = (user["current_index"] + 1) % len(user["video_ids"])
            save_user(user_id, current_index=new_index)
        await send_current_video(user_id, context, chat_id)
        return

    if data == "search_prompt":
        # Ask the user to type a keyword; the reply is caught by
        # handle_search_reply() below via the ForceReply marker.
        await context.bot.send_message(
            chat_id,
            "🔍 What do you want to search for?",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. cooking recipes"),
        )
        return

    action, video_id = data.split(":", 1)
    user = get_user(user_id)

    if action == "like":
        liked_ids = user["liked_ids"]
        if video_id in liked_ids:
            liked_ids.remove(video_id)
        else:
            liked_ids.append(video_id)
        save_user(user_id, liked_ids=liked_ids)
        await send_current_video(user_id, context, chat_id)

    elif action == "save":
        saved_ids = user["saved_ids"]
        if video_id in saved_ids:
            saved_ids.remove(video_id)
        else:
            saved_ids.append(video_id)
        save_user(user_id, saved_ids=saved_ids)
        await send_current_video(user_id, context, chat_id)

    elif action == "comment":
        comments = youtube_top_comments(video_id)
        if comments:
            text = "💬 Top comments:\n\n" + "\n\n".join(comments)
        else:
            text = "No comments available for this video (disabled or none yet)."
        await context.bot.send_message(chat_id, text)


# ---------------------------------------------------------------------------
# Dummy web server (only exists so Render's free "Web Service" tier
# sees an open port and considers the deploy healthy)
# ---------------------------------------------------------------------------
class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        pass  # silence noisy default request logging


def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))  # Render sets PORT automatically
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    logger.info(f"Dummy web server listening on port {port} (health check only)")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    db_connect().close()  # ensure table exists on boot

    # Run the dummy web server in a background thread so Render sees a live port
    threading.Thread(target=start_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_reply))
    logger.info("Bot starting (polling mode)...")
    app.run_polling()


if __name__ == "__main__":
    main()
