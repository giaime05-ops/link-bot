import os
import re
import logging
import asyncio
from pathlib import Path
import yt_dlp
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

# Mappa per i comandi /audio e /link: bot_msg_id -> orig_url
URL_STORE = {}

def extract_supported_url(text: str):
    """Intercetta link Instagram, TikTok e Twitter/X."""
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

def download_media(url: str, audio_only: bool = False):
    """Scarica video/foto o estrae audio con yt-dlp."""
    ydl_opts = {
        'outtmpl': f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        'quiet': True,
        'no_warnings': True,
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
            'format': 'best[ext=mp4]/best',
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info

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

    # Elimina subito il messaggio originale con il link
    try:
        await update.message.delete()
    except TelegramError:
        pass

    loop = asyncio.get_running_loop()
    try:
        # Scarica il media in background senza bloccare il bot
        info = await loop.run_in_executor(None, download_media, url, False)
        caption = f"👤 Inviato da <b>{sender_name}</b>"

        bot_msg = None

        # 1. Carosello di immagini (galleria)
        if 'entries' in info:
            media_group = []
            files_to_clean = []
            for entry in info['entries']:
                file_path = Path(DOWNLOAD_DIR) / f"{entry['id']}.{entry.get('ext', 'jpg')}"
                if file_path.exists():
                    files_to_clean.append(file_path)
                    media_group.append(InputMediaPhoto(media=open(file_path, 'rb')))

            if media_group:
                media_group[0].caption = caption
                media_group[0].parse_mode = "HTML"
                sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group[:10])
                bot_msg = sent[0]

            for f in files_to_clean:
                f.unlink(missing_ok=True)

        # 2. Video o singola foto
        else:
            file_id = info.get('id')
            ext = info.get('ext', 'mp4')
            file_path = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))

            if file_path and file_path[0].exists():
                actual_file = file_path[0]
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

        if audio_files and audio_files[0].exists():
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

    print("Downloader Bot operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
