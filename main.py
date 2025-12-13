import os
import logging
import uuid
import base64
import asyncio
import datetime
from typing import Optional
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

# --- Telegram Imports ---
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    ChatMember,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ================= LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= DATABASE =================
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI environment variable not set!")

client = MongoClient(MONGODB_URI)
db = client["protected_bot_db"]

links_collection = db["protected_links"]
users_collection = db["users"]
broadcast_collection = db["broadcast_history"]
channels_collection = db["channels"]

def init_db():
    client.admin.command("ismaster")
    users_collection.create_index("user_id", unique=True)
    links_collection.create_index("created_by")
    links_collection.create_index("active")
    channels_collection.create_index("channel_id", unique=True)
    logger.info("✅ MongoDB connected")

# ================= MULTI SUPPORT CHANNEL =================
def get_support_channels():
    raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]

# ================= INVITE LINK =================
async def get_channel_invite_link(context, channel_id: str) -> str:
    cached = channels_collection.find_one({"channel_id": channel_id})
    if cached and cached.get("invite_link"):
        return cached["invite_link"]

    try:
        chat_id = int(channel_id)
    except ValueError:
        chat_id = channel_id if channel_id.startswith("@") else f"@{channel_id}"

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=True,
            name="Bot Access"
        )
        channels_collection.update_one(
            {"channel_id": channel_id},
            {"$set": {"invite_link": invite.invite_link}},
            upsert=True
        )
        return invite.invite_link
    except BadRequest:
        return f"https://t.me/{channel_id.lstrip('@')}"

# ================= MEMBERSHIP CHECK =================
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    channels = get_support_channels()
    if not channels:
        return True

    for ch in channels:
        try:
            try:
                chat_id = int(ch)
            except ValueError:
                chat_id = ch if ch.startswith("@") else f"@{ch}"

            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status not in (
                ChatMember.MEMBER,
                ChatMember.ADMINISTRATOR,
                ChatMember.OWNER
            ):
                return False
        except Exception:
            return False
    return True

# ================= JOIN KEYBOARD =================
async def build_join_keyboard(context, encoded_id: Optional[str] = None):
    keyboard = []
    for ch in get_support_channels():
        link = await get_channel_invite_link(context, ch)
        keyboard.append([InlineKeyboardButton("📢 Join Channel", url=link)])

    cb = f"check_join_{encoded_id}" if encoded_id else "check_join"
    keyboard.append([InlineKeyboardButton("✅ Check", callback_data=cb)])
    return InlineKeyboardMarkup(keyboard)

# ================= BOT =================
telegram_bot_app = Application.builder().token(
    os.environ.get("TELEGRAM_TOKEN")
).build()

# ================= START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    users_collection.update_one(
        {"user_id": user.id},
        {"$set": {
            "username": user.username,
            "first_name": user.first_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )

    if not await check_channel_membership(user.id, context):
        await update.message.reply_text(
            "🔐 *Protected Access*\n\n"
            "Please join **ALL channels below**, then press ✅ Check.",
            reply_markup=await build_join_keyboard(
                context,
                context.args[0] if context.args else None
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if context.args:
        token = context.args[0]
        data = links_collection.find_one({"_id": token, "active": True})
        if not data:
            await update.message.reply_text("❌ Link expired or revoked")
            return

        web_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={token}"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_url))]]
        )
        await update.message.reply_text("🔐 Protected Link", reply_markup=kb)
        return

    await update.message.reply_text(
        "🤖 *Link Protection Bot*\n\n"
        "• /protect – Create protected link\n"
        "• /revoke – Revoke links\n"
        "• /help – Help",
        parse_mode=ParseMode.MARKDOWN
    )

# ================= CALLBACK =================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.startswith("check_join"):
        if not await check_channel_membership(q.from_user.id, context):
            await q.answer("❌ Join all channels first", show_alert=True)
            return

        if "_" in q.data:
            token = q.data.split("_", 2)[-1]
            data = links_collection.find_one({"_id": token, "active": True})
            if not data:
                await q.message.edit_text("❌ Link expired")
                return

            web_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={token}"
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_url))]]
            )
            await q.message.edit_text("✅ Verified", reply_markup=kb)
        else:
            await q.message.edit_text("✅ Verified! You can now use the bot.")

# ================= PROTECT =================
async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_membership(update.effective_user.id, context):
        await update.message.reply_text(
            "🔐 Join all support channels first.",
            reply_markup=await build_join_keyboard(context)
        )
        return

    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text("Usage:\n/protect https://t.me/yourgroup")
        return

    uid = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")

    links_collection.insert_one({
        "_id": uid,
        "telegram_link": context.args[0],
        "created_by": update.effective_user.id,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start={uid}"
    await update.message.reply_text(
        f"✅ Protected Link:\n`{link}`",
        parse_mode=ParseMode.MARKDOWN
    )

# ================= BROADCAST =================
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ Admin only")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message with /broadcast")
        return

    context.user_data["broadcast"] = update.message.reply_to_message

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ])

    await update.message.reply_text("⚠️ Confirm broadcast?", reply_markup=kb)

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel_broadcast":
        await q.message.edit_text("❌ Broadcast cancelled")
        return

    msg = context.user_data.get("broadcast")
    users = list(users_collection.find({}))

    success = 0
    for u in users:
        try:
            await msg.copy(chat_id=u["user_id"])
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await q.message.edit_text(f"✅ Broadcast done: {success} users")

# ================= HELP =================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ *Help*\n\n"
        "/start\n/protect\n/revoke\n/help",
        parse_mode=ParseMode.MARKDOWN
    )

# ================= IGNORE NON COMMAND =================
async def ignore_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return

# ================= ERROR HANDLER =================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)

# ================= REGISTER =================
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))

telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))
telegram_bot_app.add_handler(
    CallbackQueryHandler(
        handle_broadcast_confirmation,
        pattern="^(confirm_broadcast|cancel_broadcast)$"
    )
)

telegram_bot_app.add_handler(
    MessageHandler(filters.ALL & ~filters.COMMAND, ignore_message)
)

telegram_bot_app.add_error_handler(error_handler)

# ================= FASTAPI =================
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def startup():
    init_db()
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    await telegram_bot_app.bot.set_webhook(
        f"{os.environ['RENDER_EXTERNAL_URL']}/{os.environ['TELEGRAM_TOKEN']}"
    )

@app.post("/{token}")
async def webhook(request: Request, token: str):
    if token != os.environ.get("TELEGRAM_TOKEN"):
        raise HTTPException(status_code=403)
    update = Update.de_json(await request.json(), telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    return Response(status_code=200)

@app.get("/join")
async def join_page(request: Request, token: str):
    return templates.TemplateResponse("join.html", {"request": request, "token": token})

@app.get("/")
async def root():
    return {"status": "ok"}
