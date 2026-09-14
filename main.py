import os
import re
import logging
import requests
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto
)
from telegram.error import Forbidden
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
# Registro in memoria per mappare ID messaggio bot -> URL originale per /link e /audio
MEDIA_REGISTRY = {}

def clean_url(url: str) -> str:
    return url.split('?')[0].split('&')[0].strip()

def detect_link(text: str):
    if "instagram.com" in text:
        match = re.search(r'https?://(?:www\.)?instagram\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://www.kkinstagram.com/', clean)
            return "instagram", clean, proxy

    if "tiktok.com" in text:
        match = re.search(r'https?://[^\s]*tiktok\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.tnktok.com/', clean)
            proxy = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.tnktok.com/', proxy)
            return "tiktok", clean, proxy

    if "twitter.com" in text or "x.com" in text:
        match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://fxtwitter.com/', clean)
            return "twitter", clean, proxy

    return None, None, None

def fetch_carousel_photos(platform: str, orig_url: str):
    """Interroga l'endpoint JSON dei proxy per verificare se il post è un carosello di foto."""
    try:
        if platform == "tiktok":
            # API JSON fornita dal proxy fxtiktok/tnktok
            clean = orig_url.replace("vm.tiktok.com", "api.vxtiktok.com").replace("www.tiktok.com", "api.vxtiktok.com")
            res = requests.get(clean, timeout=5).json()
            if res.get("image_post_info"):
                images = res["image_post_info"].get("images", [])
                urls = [img.get("display_image", {}).get("url_list", [None])[0] for img in images]
                return [u for u in urls if u]
        elif platform == "instagram":
            # Endpoint JSON di kkinstagram
            json_url = orig_url.replace("instagram.com", "kkinstagram.com") + ".json"
            res = requests.get(json_url, timeout=5).json()
            items = res.get("carousel_media") or res.get("media")
            if isinstance(items, list) and len(items) > 1:
                urls = []
                for item in items:
                    if item.get("image_versions2"):
                        urls.append(item["image_versions2"]["candidates"][0]["url"])
                if urls:
                    return urls
    except Exception as e:
        logger.warning(f"Controllo carosello non riuscito: {e}")
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot attivo! Ora puoi ricevere file multimediali, audio e link in privato.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    platform, orig_url, proxy_url = detect_link(text)

    if not platform:
        return

    user = update.message.from_user
    sender_name = f"@{user.username}" if user and user.username else (user.first_name if user else "Utente")
    chat_id = update.message.chat_id

    # Verifica se si tratta di un carosello di foto sfogliabili
    photos = fetch_carousel_photos(platform, orig_url)
    bot_msg = None

    if photos and len(photos) > 1:
        # Album nativo Telegram: sfogliabile a schermo intero in-app
        media_group = [InputMediaPhoto(media=u) for u in photos[:10]]
        media_group[0].caption = f"👤 Inviato da <b>{sender_name}</b>"
        media_group[0].parse_mode = "HTML"
        sent_messages = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        if sent_messages:
            bot_msg = sent_messages[0]
    else:
        # Link invisibile (Zero-Width Space): solo il player nativo, zero link blu a schermo
        message_text = f'👤 Inviato da <b>{sender_name}</b><a href="{proxy_url}">&#8203;</a>'
        bot_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )

    # Memorizza la relazione per i comandi /link e /audio
    if bot_msg:
        MEDIA_REGISTRY[bot_msg.message_id] = {
            "orig": orig_url,
            "proxy": proxy_url
        }

    # Rimuove il link dell'utente per tenere pulita la chat del gruppo
    try:
        await update.message.delete()
    except Exception:
        pass

async def get_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user
    chat_id = update.message.chat_id

    # Cancella subito il comando /audio dal gruppo
    try:
        await update.message.delete()
    except Exception:
        pass

    if not reply or reply.message_id not in MEDIA_REGISTRY:
        return

    data = MEDIA_REGISTRY[reply.message_id]
    # Endpoint audio diretto supportato dal proxy
    audio_stream_url = f"{data['proxy']}.mp3"

    try:
        # Invia il file audio nativo nei DM dell'utente
        await context.bot.send_audio(
            chat_id=user.id,
            audio=audio_stream_url,
            caption="🎵 Ecco la traccia audio estratta dal video!"
        )
    except Forbidden:
        # Se l'utente non ha mai avviato il bot in privato
        bot_info = await context.bot.get_me()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Avvia Bot in Privato", url=f"https://t.me/{bot_info.username}?start=audio")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {user.mention_html()}, per ricevere l'audio in privato devi prima avviare il bot cliccando qui sotto:",
            parse_mode="HTML",
            reply_markup=keyboard
        )

async def get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user
    chat_id = update.message.chat_id

    # Cancella subito il comando /link dal gruppo
    try:
        await update.message.delete()
    except Exception:
        pass

    if not reply or reply.message_id not in MEDIA_REGISTRY:
        return

    data = MEDIA_REGISTRY[reply.message_id]

    try:
        # Invia il link originale in privato
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🔗 Ecco il link originale del post:\n{data['orig']}"
        )
    except Forbidden:
        bot_info = await context.bot.get_me()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Avvia Bot in Privato", url=f"https://t.me/{bot_info.username}?start=link")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {user.mention_html()}, per ricevere il link in privato devi prima avviare il bot cliccando qui sotto:",
            parse_mode="HTML",
            reply_markup=keyboard
        )

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN non impostato!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", get_audio))
    app.add_handler(CommandHandler("link", get_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Embed Fixer Bot avanzato operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
