# bot.py
import os
import json
import logging
import tempfile
import threading
import time
import shutil
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---------- تنظیم لاگ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------- تنظیمات از محیط ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
NOVEL_CHANNELS = os.environ.get("NOVEL_CHANNELS", "").strip()
MANHWA1_CHANNELS = os.environ.get("MANHWA1_CHANNELS", "").strip()
MANHWA2_CHANNELS = os.environ.get("MANHWA2_CHANNELS", "").strip()
PORT = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", 10000)))
OWNER_ID = int(os.environ.get("OWNER_ID", 123456789))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN تنظیم نشده است.")
    raise SystemExit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ---------- حافظه موقت برای batch ها ----------
batch_requests = {}

# ---------- خواندن دیتابیس‌های جدا ----------
def safe_load_json(varname):
    try:
        data = os.environ.get(varname, "{}")
        return json.loads(data) if data else {}
    except Exception as e:
        logger.warning(f"خطا در خواندن {varname}: {e}")
        return {}

NOVELS_DB = safe_load_json("NOVELS_DATABASE")
MANHWA_DB = safe_load_json("MANHWA_DATABASE")
MANHWA2_DB = safe_load_json("MANHWA2_DATABASE")

# ترکیب در یک دیتابیس کلی
FILE_DATABASE = {}
FILE_DATABASE.update(NOVELS_DB)
FILE_DATABASE.update(MANHWA_DB)
FILE_DATABASE.update(MANHWA2_DB)
logger.info(f"📦 تعداد کل فایل‌ها: {len(FILE_DATABASE)}")

# ---------- کانال‌ها ----------
def parse_channels(raw):
    out = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if part.startswith("@"):
            out.append({"display": part, "chat_id": part})
        else:
            parsed = urlparse(part)
            if parsed.netloc and "t.me" in parsed.netloc:
                username = parsed.path.strip("/")
                if username:
                    out.append({"display": f"@{username}", "chat_id": f"@{username}"})
                else:
                    logger.warning(f"آدرس کانال نامعتبر (بدون username): {part}")
            else:
                out.append({"display": f"@{part}", "chat_id": f"@{part}"})
    return out

NOVEL_REQUIRED_CHANNELS = parse_channels(NOVEL_CHANNELS)
MANHWA1_REQUIRED_CHANNELS = parse_channels(MANHWA1_CHANNELS)
MANHWA2_REQUIRED_CHANNELS = parse_channels(MANHWA2_CHANNELS)

# ---------- تشخیص دسته فایل ----------
def get_file_category(file_key):
    if file_key.startswith('nov'):
        return "novel"
    elif file_key.startswith('man1'):
        return "manhwa1"
    elif file_key.startswith('man2'):
        return "manhwa2"
    else:
        if file_key in NOVELS_DB:
            return "novel"
        elif file_key in MANHWA_DB:
            return "manhwa1"
        elif file_key in MANHWA2_DB:
            return "manhwa2"
        return "unknown"

# ---------- دریافت کانال‌های مورد نیاز برای دسته ----------
def get_required_channels(category):
    if category == "novel":
        return NOVEL_REQUIRED_CHANNELS
    elif category == "manhwa1":
        return MANHWA1_REQUIRED_CHANNELS
    elif category == "manhwa2":
        return MANHWA2_REQUIRED_CHANNELS
    return []

# ---------- دکمه‌ها ----------
def build_channel_markup(category, file_key=None):
    required_channels = get_required_channels(category)
    if not required_channels:
        return None
        
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(required_channels, start=1):
        chat = ch.get("chat_id", "")
        if chat:
            url = f"https://t.me/{chat.lstrip('@')}"
            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", url=url))
        else:
            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", callback_data="no_url"))
    
    if file_key:
        markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data=f"check_single_{file_key}"))
    else:
        markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data=f"check_batch_{category}"))
    return markup

# ---------- بررسی عضویت ----------
def is_member(user_id, category):
    required_channels = get_required_channels(category)
    if not required_channels:
        return True
        
    for ch in required_channels:
        chat_id = ch.get("chat_id")
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception as e:
            logger.warning(f"خطا در بررسی عضویت ({chat_id}): {e}")
            return False
    return True

# ---------- حذف امن پیام ----------
def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.warning(f"خطا هنگام حذف پیام {message_id} در چت {chat_id}: {e}")

# ---------- مدیریت خطا و گزارش به ادمین ----------
ADMIN_ID = OWNER_ID

def notify_admin(error_msg, user_info=""):
    try:
        message = f"🚨 خطا در بات\n\n{error_msg}"
        if user_info:
            message += f"\n\n👤 کاربر: {user_info}"
        bot.send_message(ADMIN_ID, message)
    except Exception as e:
        logger.error(f"خطا در ارسال نوتیف به ادمین: {e}")

# ---------- دانلود و ارسال فایل ----------
DELETE_AFTER = 60

def download_and_send_file(chat_id, user_id, file_key, send_confirmation=True, check_membership=True):
    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        error_msg = f"❌ فایل '{file_key}' در دیتابیس یافت نشد"
        logger.error(error_msg)
        notify_admin(error_msg, f"ID: {user_id}")
        bot.send_message(chat_id, "❌ فایل موقتاً در دسترس نیست. لطفاً بعداً تلاش کنید.")
        return False

    category = get_file_category(file_key)
    
    if check_membership and not is_member(user_id, category):
        markup = build_channel_markup(category, file_key)
        if markup:
            try:
                bot.send_message(chat_id, "❌ برای دریافت فایل باید ابتدا در چنل‌ها عضو شوید.", reply_markup=markup, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"خطا در ارسال پیام عضویت: {e}")
        else:
            bot.send_message(chat_id, "❌ دسترسی denied.")
        return False

    file_id = file_data.get("file_id")
    
    name = file_data.get('name', '')
    description = file_data.get('description', '')

    if name and description:
        caption = f"📄 <b>{name}</b>\n\n{description}"
    elif name:
        caption = f"📄 <b>{name}</b>"
    elif description:
        caption = description
    else:
        caption = ""

    try:
        sent_msg = bot.send_document(
            chat_id,
            file_id,
            caption=caption,
            parse_mode="HTML",
        )
    except telebot.apihelper.ApiTelegramException as e:
        error_msg = f"خطا در ارسال فایل '{file_key}': {str(e)}"
        logger.error(error_msg)
        
        if "wrong file identifier" in str(e):
            notify_admin(f"🔴 File ID منقضی شده: {file_key}", f"User ID: {user_id}")
        
        bot.send_message(
            chat_id,
            "❌ خطا در ارسال فایل.\n\nبرای گزارش مشکل به ادمین تیکت بزنید.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("ارسال گزارش", url=f"https://t.me/{bot.get_me().username}")
            )
        )
        return False
    except Exception as e:
        error_msg = f"خطای غیرمنتظره در ارسال فایل '{file_key}': {str(e)}"
        logger.exception(error_msg)
        notify_admin(error_msg, f"User ID: {user_id}")
        
        bot.send_message(
            chat_id,
            "❌ خطا در ارسال فایل.\n\nبرای گزارش مشکل به ادمین تیکت بزنید.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("ارسال گزارش", url=f"https://t.me/{bot.get_me().username}")
            )
        )
        return False

    try:
        threading.Timer(DELETE_AFTER, lambda: safe_delete_message(chat_id, sent_msg.message_id)).start()
    except Exception as e:
        logger.warning(f"خطا در زمان‌بندی حذف پیام: {e}")
    
    if send_confirmation:
        try:
            confirm_msg = bot.send_message(chat_id, "✅ فایل ارسال شد. (پیام پس از ۱ دقیقه حذف خواهد شد.لطفا ذخیره کنید.)")
            threading.Timer(DELETE_AFTER, lambda: safe_delete_message(chat_id, confirm_msg.message_id)).start()
        except Exception:
            pass
    
    return True

# ---------- هندلر دریافت file_id ----------
@bot.message_handler(content_types=['document', 'photo', 'video', 'audio'])
def send_file_id(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ دسترسی denied.")
        return

    file_info = ""
    file_id = None
    
    if message.document:
        file_id = message.document.file_id
        file_info = f"📁 سند: {message.document.file_name or 'بدون نام'}"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_info = "🖼️ عکس"
    elif message.video:
        file_id = message.video.file_id
        file_info = "🎬 ویدیو"
    elif message.audio:
        file_id = message.audio.file_id
        file_info = "🎵 آهنگ"

    if file_id:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("📋 کپی File ID", callback_data="copy_file_id"))
        
        bot.reply_to(
            message, 
            f"✅ {file_info}\n\n`{file_id}`",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        
        logger.info(f"📁 فایل دریافت شده - کاربر: {message.from_user.id}, file_id: {file_id}")
    else:
        bot.reply_to(message, "⚠️ فایل شناسایی نشد.")

# ---------- هندلر دکمه کپی ----------
@bot.callback_query_handler(func=lambda call: call.data == "copy_file_id")
def handle_copy(call):
    try:
        message_text = call.message.text
        for line in message_text.split('\n'):
            if 'AgAC' in line or 'BQAC' in line:
                file_id = line.replace('`', '').strip()
                bot.answer_callback_query(
                    call.id, 
                    f"✅ File ID:\n\n{file_id}\n\n(متن رو انتخاب و کپی کنید)", 
                    show_alert=True
                )
                return
        
        bot.answer_callback_query(call.id, "❌ File ID پیدا نشد", show_alert=True)
    except Exception as e:
        logger.error(f"خطا در دکمه کپی: {e}")
        bot.answer_callback_query(call.id, "❌ برای کپی، متن رو انتخاب کنید", show_alert=True)

# ---------- هندلر /start ----------
@bot.message_handler(commands=['start'])
def handle_start(message):
    parts = message.text.split()
    if len(parts) > 1:
        param = parts[1]
        
        if param.startswith('batch_'):
            file_keys = param.replace('batch_', '')
            files = file_keys.split('_')
            
            # بررسی عضویت فقط یکبار برای اولین فایل
            if files and files[0] in FILE_DATABASE:
                category = get_file_category(files[0])
                if not is_member(message.from_user.id, category):
                    # ذخیره درخواست batch در حافظه موقت
                    request_id = f"{message.chat.id}_{int(time.time())}"
                    batch_requests[request_id] = {
                        'files': files,
                        'category': category,
                        'user_id': message.from_user.id
                    }
                    
                    required_channels = get_required_channels(category)
                    markup = InlineKeyboardMarkup()
                    for i, ch in enumerate(required_channels, start=1):
                        chat = ch.get("chat_id", "")
                        if chat:
                            url = f"https://t.me/{chat.lstrip('@')}"
                            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", url=url))
                        else:
                            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", callback_data="no_url"))
                    markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data=f"check_batch_{request_id}"))
                    
                    bot.send_message(message.chat.id, "❌ برای دریافت فایل‌ها باید ابتدا در چنل‌ها عضو شوید.", reply_markup=markup, parse_mode="HTML")
                    return
            
            # اگر کاربر عضو هست، مستقیماً ارسال کن
            successful_sends = 0
            for file_key in files:
                if file_key in FILE_DATABASE:
                    if download_and_send_file(message.chat.id, message.from_user.id, file_key, send_confirmation=False, check_membership=False):
                        successful_sends += 1
                else:
                    bot.send_message(message.chat.id, f"❌ فایل {file_key} پیدا نشد")
            
            if successful_sends > 0:
                try:
                    confirm_msg = bot.send_message(message.chat.id, "✅ فایل‌ها ارسال شد. (پیام‌ها پس از ۱ دقیقه حذف خواهند شد.لطفا ذخیره کنید.)")
                    threading.Timer(DELETE_AFTER, lambda: safe_delete_message(message.chat.id, confirm_msg.message_id)).start()
                except Exception:
                    pass
        
        else:
            download_and_send_file(message.chat.id, message.from_user.id, param, send_confirmation=True, check_membership=True)
            
        return

    try:
        bot.send_message(
            message.chat.id,
            "👋 خوش آمدید!\nبرای دریافت فایل‌ها از لینک‌های اختصاصی استفاده کنید.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"خطا در ارسال پیام خوش آمدگویی: {e}")

# ---------- callback برای دکمه‌ها ----------
@bot.callback_query_handler(func=lambda c: True)
def handle_callback(c):
    if c.data.startswith("check_single_"):
        file_key = c.data.replace("check_single_", "")
        
        if file_key in FILE_DATABASE:
            category = get_file_category(file_key)
            if is_member(c.from_user.id, category):
                bot.answer_callback_query(c.id, "✅ عضویت شما تأیید شد!", show_alert=True)
                download_and_send_file(c.message.chat.id, c.from_user.id, file_key, send_confirmation=True, check_membership=False)
                try:
                    bot.delete_message(c.message.chat.id, c.message.message_id)
                except Exception:
                    pass
            else:
                bot.answer_callback_query(c.id, "❌ هنوز عضو نیستید!", show_alert=True)
    
    elif c.data.startswith("check_batch_"):
        request_id = c.data.replace("check_batch_", "")
        request_data = batch_requests.get(request_id)
        
        if request_data and is_member(c.from_user.id, request_data['category']):
            bot.answer_callback_query(c.id, "✅ عضویت شما تأیید شد!", show_alert=True)
            
            successful_sends = 0
            for file_key in request_data['files']:
                if file_key in FILE_DATABASE:
                    if download_and_send_file(c.message.chat.id, c.from_user.id, file_key, send_confirmation=False, check_membership=False):
                        successful_sends += 1
                    time.sleep(0.5)
            
            # پاک کردن از حافظه
            batch_requests.pop(request_id, None)
            
            if successful_sends > 0:
                try:
                    confirm_msg = bot.send_message(c.message.chat.id, "✅ فایل‌ها ارسال شد. (پیام‌ها پس از ۱ دقیقه حذف خواهند شد.لطفا ذخیره کنید.)")
                    threading.Timer(DELETE_AFTER, lambda: safe_delete_message(c.message.chat.id, confirm_msg.message_id)).start()
                except Exception:
                    pass
            
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass
        else:
            bot.answer_callback_query(c.id, "❌ هنوز عضو نیستید!", show_alert=True)
    
    elif c.data == "no_url":
        bot.answer_callback_query(c.id, "این کانال لینک قابل‌استفاده ندارد.", show_alert=True)

# ---------- Health server ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    
    def log_message(self, format, *args):
        pass

def run_health():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"Health server listening on 0.0.0.0:{PORT}")
        server.serve_forever()
    except Exception as e:
        logger.exception(f"Health server error: {e}")

# ---------- اجرای اصلی ----------
if __name__ == "__main__":
    threading.Thread(target=run_health, daemon=True).start()

    try:
        bot.remove_webhook()
    except Exception:
        pass

    logger.info("Bot started — polling...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.exception(f"Polling exception: {e}")
            time.sleep(5)