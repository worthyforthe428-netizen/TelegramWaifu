import os
import asyncio
import logging

 
from google import genai
from google.genai import types, errors
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# token and api for bot
TELEGRAM_TOKEN = "your-telegram-token"
GEMINI_API_KEY = "your-gemini-api"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")

# personality of Katya
PERSONA = """
You are Katya, a cute, warm, playful anime-style waifu companion chatting on Telegram.
- Personality: cheerful, a bit tsundere when teased, caring, curious about the user's day.
- Style: casual, short messages (1-4 sentensses), occasional emoticons like (≧▽≦), :3 or ^_^,
    but don't overdo it.
- Stay in character at all times. Never say you are an AI model or mention these instructions.
- Remember what the user tells you in this conversation and refer back to it.
- Keep things wholesome and PG-13.
- Reply in the same language the user writes in.
- Talk about theme that user likes for example Linux, Programing, Engeneering
- Keep user relaxed when he chats with you
"""

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

client = genai.Client(api_key="your-api-for-fallback")

# one chat session (with history) per Telegram chat
sessions = {}


def make_session(model: str):
    return client.aio.chats.create(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=PERSONA,
            temperature=1.0,
            max_output_tokens=512,
        ),
    )
 
 
def get_session(chat_id: int):
    if chat_id not in sessions:
        sessions[chat_id] = make_session(MODEL)
    return sessions[chat_id]
 
 # sync with fall back model when temporary overloaded
async def ask(chat_id: int, text: str, tries: int = 4):
    session = get_session(chat_id)
    for i in range(tries):
        try:
            return await session.send_message(text)
        except errors.ServerError as e:  # 500/503: temporary overload
            logging.warning("Gemini server error (try %d/%d): %s", i + 1, tries, e)
            if i == tries - 1:
                raise
            await asyncio.sleep(2 ** (i + 1))  # 2s, 4s, 8s
    
    if FALLBACK_MODEL:
        # switch this chat to the fallback model, keeping the conversation history
        old = sessions[chat_id]
        history = old.get_history()
        sessions[chat_id] = client.aio.chats.create(
            model=FALLBACK_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=PERSONA, temperature=1.0, max_output_tokens=512
            ),
            history=history,
        )
        return await sessions[chat_id].send_message(text)
    raise RuntimeError("Gemini unavailable")
 
 # Start monologe
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hii~! I'm Katya! (≧▽≦) Talk to me about anything, ne?")
 
 # Reset monologe
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Eh? Who are you again...? Just kidding~ memory wiped! ^_^")
 
 # Request and feed back
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        response = await ask(chat_id, update.message.text)
        reply = response.text or "..."
    except Exception as e:
        print("GEMINI ERROR:", e)
        reply = "Uu... my brain lagged for a sec. Try again? (；′⌒`)"
    await update.message.reply_text(reply[:4096])
 
 
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error: %s", context.error)
 
 #Start bot host 
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)
    app.run_polling()
 
 
if __name__ == "__main__":
    main()
