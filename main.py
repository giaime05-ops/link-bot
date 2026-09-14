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
    CallbackQueryHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEDIA_REGISTRY = {}

def clean_url(url: str) -> str:
    return url.split('?')[0].split('&')[0].strip()

def detect_link(text: str):
    # 1. Instagram
    if "instagram.com" in text:
        match = re.search(r'https?://(?:www\.)?instagram\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://www.ddinstagram.com/', clean)
            return "instagram", clean, proxy

    # 2. TikTok: usa tiktxk/tnktok senza metadati descrittivi invasivi
    if "tiktok.com" in text:
        match = re.search(r'https?://[^\s]*tiktok\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.tiktxk.com/', clean)
            proxy = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.tiktxk.com/', proxy)
            return "tiktok", clean, proxy

    # 3. Twitter / X: usa vxtwitter per garantire il player video esteso
    if "twitter.com" in text or "x.com" in text:
        match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://vxtwitter.com/', clean)
            return "twitter", clean, proxy

    return None, None, None

def get_carousel_images(platform: str, orig_url: str):
    """Recupera la lista dei link immagine per i caroselli."""
    try:
        if platform == "tiktok":
            api_url = orig_url.replace("vm.tiktok.com", "api.vxtiktok.com").replace("www.tiktok.com", "api.vxtiktok.com")
            res = requests.get(api_url, timeout=5).json()
            if res.get("image_post_info"):
                imgs = res["image_post_info"].get("images", [])
                return [img["display_image"]["url_list"][0] for img in imgs if img.get("display_image")]
        elif platform == "instagram":
            api_url = orig_url.replace("instagram.com", "api.ddinstagram.com")
            res = requests.get(api_url, timeout=5).json()
            if res.get("carousel_media"):
                return [item["image_versions2"]["candidates"][0]["url"] for item in res["carousel_media"]]
    except Exception as e:
        logger.warning(f"Errore recupero carosello: {e}")
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot attivo! Riceverai qui i tuoi link e tracce audio.")

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

    # Verifica presenza carosello
    carousel_photos = get_carousel_images(platform, orig_url)
    keyboard = None

    if carousel_photos and len(carousel_photos) > 1:
        # Se ci sono più foto, predispone i bottoni di scelta rapida
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🖼 Prima foto", callback_data=f"car_one_{chat_id}"),
                InlineKeyboardButton("📚 Mostra tutte", callback_data=f"car_all_{chat_id}")
            ]
        ])

    message_text = f'👤 Inviato da <b>{sender_name}</b><a href="{proxy_url}">&#8203;</a>'
    bot_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=message_text,
        parse_mode="HTML",
        disable_web_page_preview=False,
        reply_markup=keyboard
    )

    if bot_msg:
        MEDIA_REGISTRY[bot_msg.message_id] = {
            "orig": orig_url,
            "proxy": proxy_url,
            "photos": carousel_photos or [],
            "sender": sender_name
        }

    try:
        await update.message.delete()
    except Exception:
        pass

async def handle_carousel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce la scelta tra 'Prima foto' e 'Mostra tutte'."""
    query = update.callback_query
    await query.answer()
    msg_id = query.message.message_id

    if msg_id not in MEDIA_REGISTRY:
        return

    data = MEDIA_REGISTRY[msg_id]
    photos = data.get("photos", [])
    if not photos:
        return

    if query.data.startswith("car_one_"):
        # Mostra solo la prima immagine senza ricaricare altro
        await query.edit_message_reply_markup(reply_markup=None)

    elif query.data.startswith("car_all_"):
        # Rimuove i bottoni dal post originale e invia l'album nativo sfogliabile
        await query.edit_message_reply_markup(reply_markup=None)
        media_group = [InputMediaPhoto(media=u) for u in photos[:10]]
        media_group[0].caption = f"📚 Carosello inviato da <b>{data['sender']}</b>"
        media_group[0].parse_mode = "HTML"
        await context.bot.send_media_group(chat_id=query.message.chat_id, media=media_group)

async def get_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user
    chat_id = update.message.chat_id

    try:
        await update.message.delete()
    except Exception:
        pass

    if not reply or reply.message_id not in MEDIA_REGISTRY:
        return

    data = MEDIA_REGISTRY[reply.message_id]
    audio_url = f"{data['proxy']}.mp3"

    try:
        # Tenta invio diretto del file MP3 nei messaggi diretti
        await context.bot.send_audio(
            chat_id=user.id,
            audio=audio_url,
            caption="🎵 Traccia audio estratta con successo!"
        )
    except Forbidden:
        bot_info = await context.bot.get_me()
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Clicca qui per abilitare il Bot", url=f"https://t.me/{bot_info.username}?start=audio")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {user.mention_html()}, avvia il bot in privato per ricevere i file audio:",
            parse_mode="HTML",
            reply_markup=btn
        )
    except Exception as e:
        logger.error(f"Errore caricamento audio: {e}")
        # Fallback: invio del file audio nativo tramite proxy secondario
        alt_audio = data['orig'].replace("tiktok.com", "vxtiktok.com") + ".mp3"
        try:
            await context.bot.send_audio(chat_id=user.id, audio=alt_audio)
        except Exception:
            pass

async def get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user
    chat_id = update.message.chat_id

    try:
        await update.message.delete()
    except Exception:
        pass

    if not reply or reply.message_id not in MEDIA_REGISTRY:
        return

    data = MEDIA_REGISTRY[reply.message_id]
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🔗 Link originale:\n{data['orig']}"
        )
    except Forbidden:
        bot_info = await context.bot.get_me()
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Clicca qui per abilitare il Bot", url=f"https://t.me/{bot_info.username}?start=link")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {user.mention_html()}, avvia il bot in privato per ricevere i link:",
            parse_mode="HTML",
            reply_markup=btn
        )

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN non impostato!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", get_audio))
    app.add_handler(CommandHandler("link", get_link))
    app.add_handler(CallbackQueryHandler(handle_carousel_buttons, pattern=r"^car_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Media Bot aggiornato operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
