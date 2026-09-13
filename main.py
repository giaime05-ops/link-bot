import os
import re
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

def clean_url(url: str) -> str:
    """Rimuove query string e parametri di tracciamento."""
    return url.split('?')[0].split('&')[0].strip()

def convert_link(text: str) -> str:
    # 1. Instagram (Reels, Post, Caroselli, Stories)
    if "instagram.com" in text:
        match = re.search(r'https?://(?:www\.)?instagram\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            return re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://www.kkinstagram.com/', clean)

    # 2. TikTok (Video e Caroselli di foto)
    if "tiktok.com" in text:
        match = re.search(r'https?://[^\s]*tiktok\.com/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            clean = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.tnktok.com/', clean)
            clean = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.tnktok.com/', clean)
            return clean

    # 3. Twitter / X
    if "twitter.com" in text or "x.com" in text:
        match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s]+', text)
        if match:
            clean = clean_url(match.group(0))
            return re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://fxtwitter.com/', clean)

    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo! Condividi un link di TikTok, Instagram o X per l'anteprima pulita.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    new_url = convert_link(text)

    if new_url:
        user = update.message.from_user
        sender_name = f"@{user.username}" if user and user.username else (user.first_name if user else "Utente")

        # Invia il post pulito
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"👤 Inviato da <b>{sender_name}</b>\n{new_url}",
            parse_mode="HTML",
            disable_web_page_preview=False
        )

        # Rimuove il link originale nel gruppo per evitare il doppione
        try:
            await update.message.delete()
        except Exception:
            pass

async def get_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estrae l'audio se si risponde a un messaggio convertito con /audio"""
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await update.message.reply_text("💡 Per estrarre l'audio, rispondi con /audio al messaggio inviato dal bot.")
        return

    # Trova il link nel messaggio a cui si è risposto
    match = re.search(r'(https?://[^\s]+)', reply.text)
    if match:
        url = match.group(0)
        # Sfrutta il redirect audio supportato dai proxy
        audio_url = f"{url}.mp3"
        await update.message.reply_text(f"🎵 Traccia audio:\n{audio_url}")
    else:
        await update.message.reply_text("❌ Nessun link multimediale trovato nel messaggio citato.")

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN non impostato!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", get_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Media Link Fixer operativo!")
    app.run_polling()

if __name__ == "__main__":
    main()
