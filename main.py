import os
import re
import logging
import asyncio
from pathlib import Path
import http.cookiejar
import requests

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
COOKIE_FILE = "cookies.txt"

URL_STORE = {}

L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    quiet=True
)

if os.path.exists(COOKIE_FILE):
    try:
        cj = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        cj.load(ignore_discard=True, ignore_expires=True)
        L.context._session.cookies = cj
        logger.info("Sessione cookies caricata in Instaloader.")
    except Exception as e:
        logger.warning(f"Errore caricamento cookies: {e}")

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
    m = re.search(r'instagram\.com/(?:p|reel|tv)/([^/?#&]+)', url)
    return m.group(1) if m else None

def get_instagram_photos(shortcode: str):
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        if post.is_video:
            return None

        if post.mediacount > 1:
            photos = [node.display_url for node in post.get_sidecar_nodes() if not node.is_video]
            return photos if photos else None

        return [post.url]
    except Exception as e:
        logger.warning(f"Errore Instaloader: {e}")
        return None

def get_twitter_photos_and_text(url: str):
    try:
        clean_url = url.replace("https://x.com/", "https://api.vxtwitter.com/").replace("https://twitter.com/", "https://api.vxtwitter.com/")
        resp = requests.get(clean_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            media_urls = data.get("mediaURLs", [])
            photos = [u for u in media_urls if not (u.endswith(".mp4") or ".mp4" in u or ".m3u8" in u)]
            text = data.get("text", "")
            if photos:
                return photos, text
    except Exception as e:
        logger.warning(f"Errore fallback foto Twitter: {e}")
    return None, None

def get_tiktok_photos(url: str):
    """
    Risolve i caroselli fotografici di TikTok (endpoint /photo/) tramite TikWM.
    Restituisce la lista di immagini se si tratta di uno slideshow.
    """
    try:
        api_url = "https://www.tikwm.com/api/"
        resp = requests.post(api_url, data={"url": url}, timeout=10)
        if resp.status_code == 200:
            res = resp.json()
            if res.get("code") == 0:
                data = res.get("data", {})
                images = data.get("images", [])
                if images:
                    return images
    except Exception as e:
        logger.warning(f"Errore fallback TikTok photo: {e}")
    return None

def download_video_or_audio(url: str, audio_only: bool = False):
    ydl_opts = {
        'outtmpl': f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'concurrent_fragment_downloads': 5,
    }

    if os.path.exists(COOKIE_FILE) and "instagram.com" in url:
        ydl_opts['cookiefile'] = COOKIE_FILE

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

    loop = asyncio.get_running_loop()

    # 1. Caroselli / Foto Instagram
    if "instagram.com" in url and ("/p/" in url or "/reel/" not in url):
        shortcode = extract_instagram_shortcode(url)
        if shortcode:
            photos = await loop.run_in_executor(None, get_instagram_photos, shortcode)
            if photos:
                caption = f"👤 Inviato da <b>{sender_name}</b>"
                if len(photos) > 1:
                    media_group = [
                        InputMediaPhoto(media=photos[0], caption=caption, parse_mode="HTML")
                    ] + [
                        InputMediaPhoto(media=u) for u in photos[1:10]
                    ]
                    sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                    if sent:
                        for msg in sent:
                            URL_STORE[msg.message_id] = url
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

    # 2. Controllo Foto e Caroselli TikTok
    if "tiktok.com" in url:
        tiktok_photos = await loop.run_in_executor(None, get_tiktok_photos, url)
        if tiktok_photos:
            caption = f"👤 Inviato da <b>{sender_name}</b>"
            if len(tiktok_photos) > 1:
                media_group = [
                    InputMediaPhoto(media=tiktok_photos[0], caption=caption, parse_mode="HTML")
                ] + [
                    InputMediaPhoto(media=u) for u in tiktok_photos[1:10]
                ]
                sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                if sent:
                    for msg in sent:
                        URL_STORE[msg.message_id] = url
                return
            elif len(tiktok_photos) == 1:
                bot_msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=tiktok_photos[0],
                    caption=caption,
                    parse_mode="HTML"
                )
                if bot_msg:
                    URL_STORE[bot_msg.message_id] = url
                return

    # 3. Controllo Foto X / Twitter
    if "twitter.com" in url or "x.com" in url:
        photos, tweet_text = await loop.run_in_executor(None, get_twitter_photos_and_text, url)
        if photos:
            clean_tweet = re.sub(r'https?://\S+', '', tweet_text or "").strip()
            if clean_tweet:
                caption = f"💬 <i>{clean_tweet}</i>\n\n👤 Inviato da <b>{sender_name}</b>"
            else:
                caption = f"👤 Inviato da <b>{sender_name}</b>"

            if len(photos) > 1:
                media_group = [
                    InputMediaPhoto(media=photos[0], caption=caption, parse_mode="HTML")
                ] + [
                    InputMediaPhoto(media=u) for u in photos[1:10]
                ]
                sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                if sent:
                    for msg in sent:
                        URL_STORE[msg.message_id] = url
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

    # 4. Download Video standard (TikTok video, Reels, Twitter/X video)
    try:
        info = await loop.run_in_executor(None, download_video_or_audio, url, False)
        file_id = info.get('id')
        files = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))

        is_twitter = ("twitter.com" in url or "x.com" in url)
        tweet_text = info.get('description') or info.get('title') or ""

        if is_twitter and tweet_text and tweet_text != file_id:
            clean_tweet = re.sub(r'https?://\S+', '', tweet_text).strip()
            if clean_tweet:
                caption = f"💬 <i>{clean_tweet}</i>\n\n👤 Inviato da <b>{sender_name}</b>"
            else:
                caption = f"👤 Inviato da <b>{sender_name}</b>"
        else:
            caption = f"👤 Inviato da <b>{sender_name}</b>"

        bot_msg = None
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
            return

    except Exception as e:
        logger.error(f"Errore download {url}: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Impossibile scaricare il contenuto da questo link.",
            parse_mode="HTML"
        )

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
        else:
            await context.bot.send_message(
                chat_id=user.id,
                text="ℹ️ Questo post non contiene alcuna traccia audio."
            )
    except Forbidden:
        bot_info = await context.bot.get_me()
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"⚠️ {user.mention_html()}, avvia il bot in privato (t.me/{bot_info.username}) per ricevere l'audio!",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Errore estrazione audio: {e}")

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

    print("Bot avviato con supporto foto TikTok e Twitter attivo!")
    app.run_polling()

if __name__ == "__main__":
    main()
