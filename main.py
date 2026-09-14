import os
import re
import logging
import asyncio
from pathlib import Path
import static_ffmpeg
static_ffmpeg.add_paths()

import yt_dlp
import requests
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

def fetch_instagram_media(url: str):
    """Estrae immagini/caroselli Instagram tramite proxy API senza blocco login."""
    headers = {"User-Agent": "Mozilla/5.0"}
    # Corretto: api.ddinstagram.com senza www
    clean = re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://api.ddinstagram.com/', url)
    try:
        res = requests.get(clean, headers=headers, timeout=5).json()
        # Se è un carosello
        if res.get("carousel_media"):
            photos = []
            for item in res["carousel_media"]:
                if item.get("image_versions2"):
                    photos.append(item["image_versions2"]["candidates"][0]["url"])
            if photos:
                return photos
        # Se è un singolo post foto
        elif res.get("image_versions2"):
            return [res["image_versions2"]["candidates"][0]["url"]]
    except Exception as e:
        logger.warning(f"Errore recupero API Instagram: {e}")
    return None

def fetch_tiktok_slideshow(url: str):
    """Estrae foto da caroselli/slideshow di TikTok."""
    headers = {"User-Agent": "Mozilla/5.0"}
    clean = url.replace("vm.tiktok.com", "api.vxtiktok.com").replace("www.tiktok.com", "api.vxtiktok.com")
    try:
        res = requests.get(clean, headers=headers, timeout=5).json()
        if res.get("image_post_info"):
            imgs = res["image_post_info"].get("images", [])
            return [img["display_image"]["url_list"][0] for img in imgs if img.get("display_image")]
    except Exception as e:
        logger.warning(f"Errore recupero API TikTok: {e}")
    return None

def download_media(url: str, audio_only: bool = False):
    """Esegue download ottimizzato con yt-dlp per ridurre al minimo la latenza."""
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

    # Elimina subito il messaggio con il link per pulizia chat
    try:
        await update.message.delete()
    except TelegramError:
        pass

    caption = f"👤 Inviato da <b>{sender_name}</b>"
    bot_msg = None

    # 1. Controllo caroselli o post foto Instagram
    if "instagram.com" in url:
        ig_photos = fetch_instagram_media(url)
        if ig_photos:
            if len(ig_photos) > 1:
                media_group = [InputMediaPhoto(media=u) for u in ig_photos[:10]]
                media_group[0].caption = caption
                media_group[0].parse_mode = "HTML"
                sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                if sent:
                    bot_msg = sent[0]
            else:
                bot_msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=ig_photos[0],
                    caption=caption,
                    parse_mode="HTML"
                )
            if bot_msg:
                URL_STORE[bot_msg.message_id] = url
                return

    # 2. Controllo slideshow/caroselli foto TikTok
    if "tiktok.com" in url:
        tt_photos = fetch_tiktok_slideshow(url)
        if tt_photos and len(tt_photos) > 1:
            media_group = [InputMediaPhoto(media=u) for u in tt_photos[:10]]
            media_group[0].caption = caption
            media_group[0].parse_mode = "HTML"
            sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
            if sent:
                bot_msg = sent[0]
                URL_STORE[bot_msg.message_id] = url
                return

    # 3. Video (TikTok, Reels, Twitter/X) tramite yt-dlp accelerato
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, download_media, url, False)
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
        logger.error(f"Errore download {url}: {e}")

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
        info = await loop.run_in_executor(None, download_media, url, True)
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
        raise ValueError("TELEGRAM_TOKEN non impostato!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("audio", get_audio))
    app.add_handler(CommandHandler("link", get_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Media Downloader ultra-veloce operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
