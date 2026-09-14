import os
import re
import logging
import asyncio
from pathlib import Path

import static_ffmpeg
static_ffmpeg.add_paths()

import yt_dlp
import instaloader
from telegram import Update, InputMediaPhoto
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

URL_STORE = {}

# Inizializza Instaloader (anonimo, senza login)
L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    quiet=True
)

def extract_supported_url(text: str):
    patterns = [
        r'https?://(?:www\.)?instagram\.com/[^\s]+',
        r'https?://[^\s]*tiktok\.com/[^\s]+',
        r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s]+'
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0).split('?')[0]
    return None

def extract_instagram_shortcode(url: str) -> str:
    """Estrae lo shortcode da link come /p/SHORTCODE/ o /reel/SHORTCODE/."""
    m = re.search(r'instagram\.com/(?:p|reel|tv)/([^/?#&]+)', url)
    return m.group(1) if m else None

def get_instagram_photos(shortcode: str):
    """Estrae gli URL diretti delle foto da post singoli o caroselli tramite Instaloader."""
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        if post.is_video:
            return None  # I video vengono gestiti da yt-dlp

        # Carosello di immagini
        if post.mediacount > 1:
            photos = []
            for node in post.get_sidecar_nodes():
                if not node.is_video:
                    photos.append(node.display_url)
            return photos if photos else None

        # Singola immagine
        return [post.url]
    except Exception as e:
        logger.warning(f"Instaloader fallito per shortcode {shortcode}: {e}")
        return None

def download_video_or_audio(url: str, audio_only: bool = False):
    ydl_opts = {
        'outtmpl': f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'concurrent_fragment_downloads': 5,
    }

    if audio_only:
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': 'best[ext=mp4]/bestvideo+bestaudio/best',
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    url = extract_supported_url(text)
    if not url:
        return

    user = update.message.from_user
    sender_name = f"@{user.username}" if user and user.username else (user.first_name if user else "Utente")
    chat_id = update.message.chat_id

    try:
        await update.message.delete()
    except TelegramError:
        pass

    caption = f"👤 Inviato da <b>{sender_name}</b>"
    bot_msg = None

    # 1. Gestione Foto / Caroselli Instagram tramite Instaloader
    if "instagram.com" in url:
        shortcode = extract_instagram_shortcode(url)
        if shortcode:
            loop = asyncio.get_running_loop()
            photos = await loop.run_in_executor(None, get_instagram_photos, shortcode)

            if photos:
                if len(photos) > 1:
                    media_group = [InputMediaPhoto(media=u) for u in photos[:10]]
                    media_group[0].caption = caption
                    media_group[0].parse_mode = "HTML"
                    sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                    if sent:
                        URL_STORE[sent[0].message_id] = url
                    return
                elif len(photos) == 1:
                    bot_msg = await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=photos[0],
                        caption=caption,
                        parse_mode="HTML"
                    )
                    if bot_msg:
                        URL_STORE[bot_msg.message_id] = url
                    return

    # 2. Gestione Video (TikTok, Reels, Twitter/X) tramite yt-dlp
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, download_video_or_audio, url, False)
        file_id = info.get('id')
        files = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))

        if files:
            actual_file = files[0]
            ext = actual_file.suffix.lower().replace('.', '')
            with open(actual_file, 'rb') as f:
                if ext in ['jpg', 'jpeg', 'png', 'webp']:
                    bot_msg = await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption,
                        parse_mode="HTML"
                    )
                else:
                    bot_msg = await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        parse_mode="HTML",
                        supports_streaming=True
                    )
            actual_file.unlink(missing_ok=True)

        if bot_msg:
            URL_STORE[bot_msg.message_id] = url

    except Exception as e:
        logger.error(f"Errore caricamento per {url}: {e}")

async def get_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user

    try:
        await update.message.delete()
    except TelegramError:
        pass

    if not reply or reply.message_id not in URL_STORE:
        return

    url = URL_STORE[reply.message_id]
    loop = asyncio.get_running_loop()

    try:
        info = await loop.run_in_executor(None, download_video_or_audio, url, True)
        file_id = info.get('id')
        audio_files = list(DOWNLOAD_DIR.glob(f"{file_id}.mp3"))

        if audio_files:
            with open(audio_files[0], 'rb') as f:
                await context.bot.send_audio(
                    chat_id=user.id,
                    audio=f,
                    caption="🎵 Traccia audio estratta!"
                )
            audio_files[0].unlink(missing_ok=True)
    except Forbidden:
        bot_info = await context.bot.get_me()
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"⚠️ {user.mention_html()}, avvia il bot in privato (t.me/{bot_info.username}) per ricevere l'audio!",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Errore invio audio: {e}")

async def get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user

    try:
        await update.message.delete()
    except TelegramError:
        pass

    if not reply or reply.message_id not in URL_STORE:
        return

    url = URL_STORE[reply.message_id]
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🔗 Link originale:\n{url}"
        )
    except Forbidden:
        bot_info = await context.bot.get_me()
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"⚠️ {user.mention_html()}, avvia il bot in privato (t.me/{bot_info.username}) per ricevere il link!",
            parse_mode="HTML"
        )

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN mancante!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("audio", get_audio))
    app.add_handler(CommandHandler("link", get_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot ibrido operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
