import os
import re
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

def convert_link(text: str) -> str:
    # 1. Instagram: copre Reel, Post (/p/), Stories e Share
    if re.search(r'https?://(?:www\.)?instagram\.com/(?:reel|p|stories|share)/', text):
        clean = text.split('?')[0].strip()
        return re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://www.eeinstagram.com/', clean)

    # 2. TikTok: conversione verso tnktok
    if "tiktok.com" in text:
        clean = text.split('?')[0].strip()
        clean = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.tnktok.com/', clean)
        clean = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.tnktok.com/', clean)
        return clean

    # 3. Twitter / X: conversione verso fxtwitter
    if "twitter.com" in text or "x.com" in text:
        clean = text.split('?')[0].strip()
        return re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://fxtwitter.com/', clean)

    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Invia o condividi un link di TikTok, Instagram o X nel gruppo e genererò subito l'anteprima!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    new_url = convert_link(text)

    if new_url:
        await update.message.reply_text(
            text=new_url,
            reply_to_message_id=update.message.message_id,
            disable_web_page_preview=False
        )

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN non impostato!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Link Fixer Bot attivo!")
    app.run_polling()

if __name__ == "__main__":
    main()
