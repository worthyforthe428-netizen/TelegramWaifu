import os
import json
import time
import random
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types, errors
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# token and api for bot
TELEGRAM_TOKEN = "Telegram bot token"
GEMINI_API_KEY = "Gemini api key"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")

PING_MIN_MINUTES = 10
PING_MAX_MINUTES = 20
QUIET_START_HOUR = 23
QUIET_END_HOUR = 9
TIMEZONE = ZoneInfo("Europe/Kyiv")
SKIP_IF_ACTIVE_MINUTES = 8
STATE_FILE = "katya_state.json"

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

PING_PROMPT = (
    "[Internal note, never mention it: some time has passed and the user hasn't written. "
    "Start a new short message on your own, like a friend texting first. Ask about his day, "
    "follow up on something he told you earlier, or share a small fun thought about Linux, "
    "programming or engineering. 1-3 sentences, don't repeat earlier openers.]"
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

client = genai.Client(api_key="Gemini api key")

# one chat session (with history) per Telegram chat
sessions = {}
# last time the user wrote, per chat
last_seen = {}
# chat_id (str) -> pings enabled (bool); saved to disk so its survives restarts
state = {}

def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f) 

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
 
 # sync with fall back model if temporary overloaded
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

def in_quiet_hours() -> bool:
    hour = datetime.now(TIMEZONE).hour
    if QUIET_START_HOUR > QUIET_END_HOUR:  # window wraps past midnight
        return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR
    return QUIET_START_HOUR <= hour < QUIET_END_HOUR
 
 
def schedule_next_ping(app: Application, chat_id: int):
    name = f"ping-{chat_id}"
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    delay = random.randint(PING_MIN_MINUTES * 60, PING_MAX_MINUTES * 60)
    app.job_queue.run_once(ping_job, delay, chat_id=chat_id, name=name)
    logging.info("Next ping for %s in %d min", chat_id, delay // 60)
 
 
async def ping_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        if not state.get(str(chat_id), False):
            return  # pings turned off; chain stops here
        if in_quiet_hours():
            return
        if time.time() - last_seen.get(chat_id, 0) < SKIP_IF_ACTIVE_MINUTES * 60:
            return  # you're already chatting with her
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        response = await ask(chat_id, PING_PROMPT)
        if response.text:
            await context.bot.send_message(chat_id, response.text[:4096])
    except Exception as e:
        logging.error("Ping failed for %s: %s", chat_id, e)
    finally:
        # always plan the next one while pings are enabled
        if state.get(str(chat_id), False):
            schedule_next_ping(context.application, chat_id)
 
 
def enable_pings(app: Application, chat_id: int):
    state[str(chat_id)] = True
    save_state()
    schedule_next_ping(app, chat_id)
 
 
async def post_init(app: Application):
    # restore pings for everyone who had them on before a restart
    for chat_id, enabled in state.items():
        if enabled:
            schedule_next_ping(app, int(chat_id))
 
 # start monologe
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hii~! I'm Katya! (≧▽≦) Talk to me about anything, ne?")
 
 # reset monologe
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Eh? Who are you again...? Just kidding~ memory wiped! ^_^")
 
async def pings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if arg == "on":
        enable_pings(context.application, chat_id)
        await update.message.reply_text("Okay~ I'll text you first sometimes! :3")
    elif arg == "off":
        state[str(chat_id)] = False
        save_state()
        await update.message.reply_text("Fine, I'll stay quiet... until you call me. (；′⌒`)")
    else:
        status = "on" if state.get(str(chat_id), False) else "off"
        await update.message.reply_text(f"Pings are {status}. Use /pings on or /pings off ^_^")

 # request and feed back
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    last_seen[chat_id] = time.time()
    if str(chat_id) not in state:
        enable_pings(context.application, chat_id)
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
 
 # start our bot host
def main():
    load_state()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("pings", pings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)
    app.run_polling()
 
 
if __name__ == "__main__":
    main()
