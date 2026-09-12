import os
import re
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

def convert_link(text: str) -> str:
    converted = False
    
    # Instagram -> ddinstagram
    if re.search(r'https?://(?:www\.)?instagram\.com/(?:reel|p|share)/', text):
        text = re.sub(r'https?://(?:www\.)?instagram\.com/', 'https://www.ddinstagram.com/', text)
        converted = True

    # TikTok -> vxtiktok
    elif re.search(r'https?://(?:(?:vt|vm)\.tiktok\.com/|www\.tiktok\.com/)', text):
        text = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.vxtiktok.com/', text)
        text = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.vxtiktok.com/', text)
        converted = True

    # Twitter/X -> fixupx
    elif re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', text):
        text = re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://fixupx.com/', text)
        converted = True

    return text if converted else None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Inviami o posta in un gruppo un link di Instagram, TikTok o Twitter/X e genererò subito l'anteprima video!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    new_text = convert_link(text)

    if new_text:
        # Invia subito il link convertito citando il messaggio originale
        await update.message.reply_text(
            text=new_text,
            reply_to_message_id=update.message.message_id,
            disable_web_page_preview=False
        )

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN non presente nelle variabili!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Embed Fixer Bot avviato!")
    app.run_polling()

if __name__ == "__main__":
    main()
