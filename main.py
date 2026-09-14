import os
import re
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
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

# Cache dei post inviati: message_id -> {orig, proxy}
MEDIA_CACHE = {}

def clean_url(url: str) -> str:
    """Rimuove parametri di tracciamento superflui."""
    return url.split('?')[0].split('&')[0].strip()

def convert_link(text: str):
    # 1. Instagram (Reels, Post, Storie)
    if "instagram.com" in text:
        match = re.search(r'https?://(?:www\.)?instagram\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://www.kkinstagram.com/', clean)
            return clean, proxy

    # 2. TikTok (Video / Foto)
    if "tiktok.com" in text:
        match = re.search(r'https?://[^\s]*tiktok\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.tnktok.com/', clean)
            proxy = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.tnktok.com/', proxy)
            return clean, proxy

    # 3. Twitter / X
    if "twitter.com" in text or "x.com" in text:
        match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            proxy = re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://vxtwitter.com/', clean)
            return clean, proxy

    return None, None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot attivo! Qui riceverai in privato i tuoi file audio o link originali.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    orig_url, proxy_url = convert_link(text)

    if not proxy_url:
        return

    user = update.message.from_user
    sender_name = f"@{user.username}" if user and user.username else (user.first_name if user else "Utente")
    chat_id = update.message.chat_id

    # Invia il messaggio con il proxy che genera il player Telegram
    bot_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"👤 Inviato da <b>{sender_name}</b>\n{proxy_url}",
        parse_mode="HTML",
        disable_web_page_preview=False
    )

    if bot_msg:
        # Salva sia l'id del messaggio del bot sia i link
        MEDIA_CACHE[bot_msg.message_id] = {
            "orig": orig_url,
            "proxy": proxy_url
        }

    # Elimina il messaggio originale con il link brutto
    try:
        await update.message.delete()
    except TelegramError:
        pass

async def get_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user
    chat_id = update.message.chat_id

    # Rimuove il comando /audio dal gruppo per non sporcare la chat
    try:
        await update.message.delete()
    except TelegramError:
        pass

    if not reply or not reply.text:
        return

    # Recupera il link o dalla cache o leggendolo dal messaggio a cui si risponde
    url_data = MEDIA_CACHE.get(reply.message_id)
    proxy_url = url_data["proxy"] if url_data else None

    if not proxy_url:
        match = re.search(r'(https?://[^\s]+)', reply.text)
        if match:
            proxy_url = match.group(0)

    if not proxy_url:
        return

    # URL audio diretto dal proxy
    audio_stream_url = f"{proxy_url}.mp3"

    try:
        await context.bot.send_audio(
            chat_id=user.id,
            audio=audio_stream_url,
            caption="🎵 Ecco la traccia audio estratta!"
        )
    except Forbidden:
        bot_info = await context.bot.get_me()
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Clicca qui per avviare il Bot", url=f"https://t.me/{bot_info.username}?start=audio")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {user.mention_html()}, per ricevere l'audio in privato devi prima avviare il bot:",
            parse_mode="HTML",
            reply_markup=btn
        )
    except Exception as e:
        logger.error(f"Errore invio audio: {e}")
        # Se Telegram rifiuta lo stream diretto, manda il link diretto all'audio
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"🎵 Non è stato possibile inviare il file audio nativo. Puoi ascoltarlo/scaricarlo qui:\n{audio_stream_url}"
            )
        except Exception:
            pass

async def get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    user = update.message.from_user
    chat_id = update.message.chat_id

    try:
        await update.message.delete()
    except TelegramError:
        pass

    if not reply or not reply.text:
        return

    url_data = MEDIA_CACHE.get(reply.message_id)
    orig_url = url_data["orig"] if url_data else None

    if not orig_url:
        match = re.search(r'(https?://[^\s]+)', reply.text)
        if match:
            raw = match.group(0)
            orig_url = raw.replace("kkinstagram.com", "instagram.com")\
                          .replace("tnktok.com", "tiktok.com")\
                          .replace("vxtwitter.com", "x.com")

    if not orig_url:
        return

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🔗 Link originale del post:\n{orig_url}"
        )
    except Forbidden:
        bot_info = await context.bot.get_me()
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Clicca qui per avviare il Bot", url=f"https://t.me/{bot_info.username}?start=link")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {user.mention_html()}, per ricevere il link in privato devi prima avviare il bot:",
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Media Link Fixer stabile operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
