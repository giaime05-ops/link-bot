import os
import re
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

def convert_link(text: str) -> str:
    # 1. Instagram: rimuove i parametri dopo '?' e usa instagramez
    ig_match = re.search(r'https?://(?:www\.)?instagram\.com/(?:reel|p|share)/([a-zA-Z0-9_-]+)', text)
    if ig_match:
        reel_id = ig_match.group(1)
        return f"https://www.instagramez.com/reel/{reel_id}/"

    # 2. TikTok: usa tnktok al posto del defunto vxtiktok
    if re.search(r'https?://(?:(?:vt|vm)\.tiktok\.com/|www\.tiktok\.com/)', text):
        text = re.sub(r'https?://(?:vt|vm)\.tiktok\.com/', 'https://vm.tnktok.com/', text)
        text = re.sub(r'https?://(?:www\.)?tiktok\.com/', 'https://www.tnktok.com/', text)
        # Pulisce eventuali parametri alla fine del link
        clean_url = text.split('?')[0]
        return clean_url

    # 3. Twitter / X: usa fxtwitter
    x_match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/\S+', text)
    if x_match:
        url = x_match.group(0).split('?')[0]
        return re.sub(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/', 'https://fxtwitter.com/', url)

    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Invia o condividi un link di TikTok, Instagram o X nel gruppo e genererò subito l'anteprima video!")

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
