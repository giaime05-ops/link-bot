import os
import re
import logging
import asyncio
from pathlib import Path
import http.cookiejar
from datetime import datetime, timezone
import requests
import subprocess
from urllib.parse import quote

import static_ffmpeg
static_ffmpeg.add_paths()

import yt_dlp
import instaloader
from telegram import (
    Update,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Silenzia i falsi allarmi sui micro-timeout temporanei di Telegram
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_FILE = "cookies.txt"

# Mapping: message_id -> url
URL_STORE = {}

# Memoria repost: clean_url -> {"sender": "@username", "date": datetime}
REPOST_STORE = {}

# Instaloader configurato senza sleep/pause forzate
L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    quiet=True,
    max_connection_attempts=1
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
        r'https?://(?:www\.|vm\.|vt\.)?tiktok\.com/[^\s]+',
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

def get_instagram_photos_and_owner(shortcode: str):
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        owner = post.owner_username or (post.owner_profile.full_name if post.owner_profile else None)
        if post.is_video:
            return None, owner

        if post.mediacount > 1:
            photos = [node.display_url for node in post.get_sidecar_nodes() if not node.is_video]
            return (photos if photos else None), owner

        return [post.url], owner
    except Exception as e:
        logger.warning(f"Instaloader check fallito: {e}")
        return None, None

def get_single_story_media(url: str):
    m = re.search(r'instagram\.com/stories/([^/?#&]+)/(\d+)', url)
    if not m:
        return None, None, None
    
    username = m.group(1)
    media_id = m.group(2)
    
    try:
        session = L.context._session
        api_url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "X-IG-App-ID": "936619743392459",
        }
        res = session.get(api_url, headers=headers, timeout=6)
        if res.status_code == 200:
            data = res.json()
            items = data.get("items", [])
            if items:
                item = items[0]
                owner_name = item.get("user", {}).get("username") or username
                if item.get("video_versions"):
                    return "video", item["video_versions"][0]["url"], owner_name
                elif item.get("image_versions2"):
                    candidates = item["image_versions2"].get("candidates", [])
                    if candidates:
                        return "photo", candidates[0]["url"], owner_name
    except Exception as e:
        logger.warning(f"Errore API storia diretta: {e}")
    return None, None, None

def translate_to_italian_if_needed(text: str):
    if not text:
        return text
    try:
        encoded = quote(text)
        api = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=it&dt=t&q={encoded}"
        res = requests.get(api, timeout=4).json()
        translated_text = "".join([part[0] for part in res[0] if part[0]])
        detected_lang = res[2] if len(res) > 2 else "it"
        
        if detected_lang and not detected_lang.startswith("it"):
            return f"{translated_text}\n<i>(Tradotto da {detected_lang.upper()})</i>"
        return text
    except Exception as e:
        logger.warning(f"Errore traduzione automatica: {e}")
        return text

def get_twitter_data(url: str):
    try:
        clean_url = url.replace("https://x.com/", "https://api.vxtwitter.com/").replace("https://twitter.com/", "https://api.vxtwitter.com/")
        resp = requests.get(clean_url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            media_urls = data.get("mediaURLs", [])
            photos = [u for u in media_urls if not (u.endswith(".mp4") or ".mp4" in u or ".m3u8" in u)]
            raw_text = data.get("text", "")
            
            clean_text = re.sub(r'https?://\S+', '', raw_text).strip() if raw_text else ""
            translated = translate_to_italian_if_needed(clean_text) if clean_text else ""
            
            author_name = data.get("user_name") or data.get("user_screen_name")
            return photos, translated, author_name
    except Exception as e:
        logger.warning(f"Errore API Twitter: {e}")
    return None, None, None

def get_tiktok_photos_and_author(url: str):
    try:
        api_url = "https://www.tikwm.com/api/"
        resp = requests.post(api_url, data={"url": url}, timeout=6)
        if resp.status_code == 200:
            res = resp.json()
            if res.get("code") == 0:
                data = res.get("data", {})
                images = data.get("images", [])
                author = data.get("author", {}).get("nickname") or data.get("author", {}).get("unique_id")
                if images:
                    return images, author
    except Exception as e:
        logger.warning(f"Errore fallback TikTok photo: {e}")
    return None, None

def download_tiktok_video_fallback(url: str):
    """Fallback ad alta risoluzione per TikTok via API diretta."""
    try:
        api_url = "https://www.tikwm.com/api/"
        resp = requests.post(api_url, data={"url": url}, timeout=8)
        if resp.status_code == 200:
            res = resp.json()
            if res.get("code") == 0:
                data = res.get("data", {})
                video_url = data.get("play")
                video_id = data.get("id", "tiktok_video")
                author = data.get("author", {}).get("nickname") or data.get("author", {}).get("unique_id")
                
                if video_url:
                    dest_file = DOWNLOAD_DIR / f"{video_id}.mp4"
                    r = requests.get(video_url, timeout=15)
                    if r.status_code == 200:
                        with open(dest_file, "wb") as f:
                            f.write(r.content)
                        return {
                            'id': video_id,
                            'uploader': author
                        }
    except Exception as e:
        logger.warning(f"Fallback TikTok video fallito: {e}")
    return None

def download_video_or_audio(url: str, audio_only: bool = False, use_cookies: bool = True):
    ydl_opts = {
        'outtmpl': f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'concurrent_fragment_downloads': 5,
        'retries': 1,
        'socket_timeout': 15,
    }

    if use_cookies and os.path.exists(COOKIE_FILE) and "instagram.com" in url:
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

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)
    except Exception as e:
        # Se fallisce con HTTP 400 Bad Request su Instagram ritenta immediatamente SENZA cookie
        if use_cookies and "instagram.com" in url and "400" in str(e):
            logger.info("Errore 400 rilevato con i cookies. Ritento download pubblico senza cookies...")
            return download_video_or_audio(url, audio_only=audio_only, use_cookies=False)
        raise e

def compress_video_if_needed(file_path: Path) -> Path:
    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb <= 48:
        return file_path

    compressed_path = file_path.with_name(f"comp_{file_path.name}")
    cmd = [
        "ffmpeg", "-y", "-i", str(file_path),
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-crf", "28", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        str(compressed_path)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        file_path.unlink(missing_ok=True)
        return compressed_path
    except Exception as e:
        logger.warning(f"Compressione fallita: {e}")
        return file_path

def create_gif_clip(video_path: Path, start_sec: float, duration: float, out_path: Path):
    cmd = [
        "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(duration),
        "-i", str(video_path),
        "-an",
        "-vf", "scale=-2:480",
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(out_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def build_repost_notice_and_update(url: str, sender_name: str) -> str:
    clean_url = url.lower().rstrip('/')
    now = datetime.now(timezone.utc)
    repost_text = ""

    if clean_url in REPOST_STORE:
        prev = REPOST_STORE[clean_url]
        diff = now - prev["date"]
        days = diff.days
        hours = int(diff.seconds / 3600)
        
        if days > 0:
            time_ago = f"{days} giorn{'o' if days == 1 else 'i'} fa"
        elif hours > 0:
            time_ago = f"{hours} or{'a' if hours == 1 else 'e'} fa"
        else:
            time_ago = "poco fa"

        repost_text = f"⚠️ <b>Repost!</b> Già inviato da <b>{prev['sender']}</b> {time_ago}\n"

    REPOST_STORE[clean_url] = {"sender": sender_name, "date": now}
    return repost_text

def format_caption(repost_prefix: str, sender_name: str, author: str = None, extra_text: str = None) -> str:
    parts = []
    if repost_prefix:
        parts.append(repost_prefix.strip())
    if extra_text:
        parts.append(f"💬 <i>{extra_text}</i>\n")
    if author:
        clean_author = str(author).lstrip('@').strip()
        parts.append(f"📱 <b>{clean_author}</b>")
    
    parts.append(f"👤 Inviato da <b>{sender_name}</b>")
    return "\n".join(parts)

def get_action_keyboard(url: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🎵 Audio", callback_data="get_audio_inline"),
            InlineKeyboardButton("🔗 Link", url=url)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

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

    repost_prefix = build_repost_notice_and_update(url, sender_name)
    loop = asyncio.get_running_loop()

    # 1. Storie Instagram
    if "instagram.com/stories/" in url:
        media_type, media_url, story_author = await loop.run_in_executor(None, get_single_story_media, url)
        if media_url:
            caption = format_caption(repost_prefix, sender_name, author=story_author)
            if media_type == "photo":
                bot_msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=media_url,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=get_action_keyboard(url)
                )
                if bot_msg:
                    URL_STORE[bot_msg.message_id] = url
                return
            elif media_type == "video":
                bot_msg = await context.bot.send_video(
                    chat_id=chat_id,
                    video=media_url,
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True,
                    reply_markup=get_action_keyboard(url)
                )
                if bot_msg:
                    URL_STORE[bot_msg.message_id] = url
                return

    # 2. Caroselli / Foto Instagram
    if "instagram.com" in url and ("/p/" in url or "/reel/" not in url) and "/stories/" not in url:
        shortcode = extract_instagram_shortcode(url)
        if shortcode:
            photos, author = await loop.run_in_executor(None, get_instagram_photos_and_owner, shortcode)
            if photos:
                caption = format_caption(repost_prefix, sender_name, author=author)
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
                        parse_mode="HTML",
                        reply_markup=get_action_keyboard(url)
                    )
                    if bot_msg:
                        URL_STORE[bot_msg.message_id] = url
                    return

    # 3. Foto e Caroselli TikTok
    if "tiktok.com" in url:
        tiktok_photos, tk_author = await loop.run_in_executor(None, get_tiktok_photos_and_author, url)
        if tiktok_photos:
            caption = format_caption(repost_prefix, sender_name, author=tk_author)
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
                    parse_mode="HTML",
                    reply_markup=get_action_keyboard(url)
                )
                if bot_msg:
                    URL_STORE[bot_msg.message_id] = url
                return

    # 4. Twitter / X
    if "twitter.com" in url or "x.com" in url:
        photos, tweet_text, tw_author = await loop.run_in_executor(None, get_twitter_data, url)
        
        if photos:
            caption = format_caption(repost_prefix, sender_name, author=tw_author, extra_text=tweet_text)
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
                    parse_mode="HTML",
                    reply_markup=get_action_keyboard(url)
                )
                if bot_msg:
                    URL_STORE[bot_msg.message_id] = url
                return
        
        elif tweet_text and "video" not in str(photos):
            try:
                await loop.run_in_executor(None, download_video_or_audio, url, False)
            except Exception:
                caption = format_caption(repost_prefix, sender_name, author=tw_author, extra_text=tweet_text)
                link_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Apri su X", url=url)]])
                bot_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=link_btn
                )
                if bot_msg:
                    URL_STORE[bot_msg.message_id] = url
                return

    # 5. Download Video standard (Reels, TikTok video, X video)
    try:
        info = None
        try:
            info = await loop.run_in_executor(None, download_video_or_audio, url, False)
        except Exception as e_main:
            if "tiktok.com" in url:
                logger.info(f"yt-dlp fallito su TikTok ({e_main}), avvio fallback su TikWM...")
                info = await loop.run_in_executor(None, download_tiktok_video_fallback, url)
            if not info:
                raise e_main

        file_id = info.get('id')
        files = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))

        media_author = info.get('uploader') or info.get('channel') or info.get('uploader_id')

        is_twitter = ("twitter.com" in url or "x.com" in url)
        tweet_text = info.get('description') or info.get('title') or ""
        extra_desc = None

        if is_twitter and tweet_text and tweet_text != file_id:
            clean_tw = re.sub(r'https?://\S+', '', tweet_text).strip()
            if clean_tw:
                extra_desc = translate_to_italian_if_needed(clean_tw)

        caption = format_caption(repost_prefix, sender_name, author=media_author, extra_text=extra_desc)

        bot_msg = None
        if files:
            actual_file = files[0]
            ext = actual_file.suffix.lower().replace('.', '')
            if ext in ['jpg', 'jpeg', 'png', 'webp']:
                with open(actual_file, 'rb') as f:
                    bot_msg = await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=get_action_keyboard(url)
                    )
                actual_file.unlink(missing_ok=True)
            else:
                final_file = await loop.run_in_executor(None, compress_video_if_needed, actual_file)
                with open(final_file, 'rb') as f:
                    bot_msg = await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        parse_mode="HTML",
                        supports_streaming=True,
                        reply_markup=get_action_keyboard(url)
                    )
                final_file.unlink(missing_ok=True)

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

async def handle_inline_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data != "get_audio_inline":
        return

    msg_id = query.message.message_id
    user = query.from_user

    if msg_id not in URL_STORE:
        await query.answer("Post non più disponibile.", show_alert=True)
        return

    url = URL_STORE[msg_id]
    await query.answer("🎵 Invio traccia audio in chat privata...")

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, download_video_or_audio, url, True)
        if not info:
            raise ValueError("Impossibile recuperare audio")
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
            chat_id=query.message.chat_id,
            text=f"⚠️ {user.mention_html()}, avvia prima il bot in privato (t.me/{bot_info.username}) per ricevere l'audio!",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Errore estrazione audio inline: {e}")

async def handle_gif_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    if not reply or reply.message_id not in URL_STORE:
        return

    url = URL_STORE[reply.message_id]
    chat_id = update.message.chat_id

    args = context.args
    start_sec = 0.0
    duration = 5.0

    if args:
        raw_text = " ".join(args).replace('-', ' ')
        parts = re.findall(r'\d+(?:\.\d+)?', raw_text)
        if len(parts) >= 2:
            s = float(parts[0])
            e = float(parts[1])
            if e > s:
                start_sec = s
                duration = min(e - s, 10.0)
        elif len(parts) == 1:
            duration = min(float(parts[0]), 10.0)

    try:
        await update.message.delete()
    except TelegramError:
        pass

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, download_video_or_audio, url, False)
        if not info:
            return
        file_id = info.get('id')
        files = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))

        if not files:
            return

        source_video = files[0]
        ext = source_video.suffix.lower().replace('.', '')
        if ext in ['jpg', 'jpeg', 'png', 'webp']:
            source_video.unlink(missing_ok=True)
            return

        gif_path = DOWNLOAD_DIR / f"gif_{file_id}.mp4"
        await loop.run_in_executor(None, create_gif_clip, source_video, start_sec, duration, gif_path)
        source_video.unlink(missing_ok=True)

        if gif_path.exists():
            with open(gif_path, 'rb') as f:
                await context.bot.send_animation(
                    chat_id=chat_id,
                    animation=f,
                    reply_to_message_id=reply.message_id
                )
            gif_path.unlink(missing_ok=True)

    except Exception as e:
        logger.error(f"Errore creazione GIF: {e}")

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
        if not info:
            return
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

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    
    app.add_handler(CommandHandler("audio", get_audio))
    app.add_handler(CommandHandler("link", get_link))
    app.add_handler(CommandHandler("gif", handle_gif_command))
    
    app.add_handler(CallbackQueryHandler(handle_inline_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot riavviato e pienamente operativo!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
