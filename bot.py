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

if not BOT_TOKEN:
    logger.error("BOT_TOKEN تنظیم نشده است.")
    raise SystemExit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

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
        # fallback برای فایل‌های قدیمی
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
def build_channel_markup(category):
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
    markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data=f"check_{category}"))
    return markup

# ---------- بررسی عضویت ----------
def is_member(user_id, category):
    required_channels = get_required_channels(category)
    if not required_channels:
        return True  # اگر چنلی تنظیم نشده، دسترسی آزاد
        
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

# ---------- دانلود و ارسال فایل ----------
DELETE_AFTER = 60  # ثانیه — 1 دقیقه

def download_and_send_file(chat_id, user_id, file_key):
    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        bot.send_message(chat_id, "❌ فایل یافت نشد.")
        return False

    # تشخیص دسته فایل
    category = get_file_category(file_key)
    
    # بررسی عضویت برای دسته مربوطه
    if not is_member(user_id, category):
        markup = build_channel_markup(category)
        if markup:
            try:
                bot.send_message(chat_id, "❌ برای دریافت فایل باید ابتدا در چنل‌ها عضو شوید.", reply_markup=markup, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"خطا در ارسال پیام عضویت: {e}")
        else:
            bot.send_message(chat_id, "❌ دسترسی denied.")
        return False

    file_id = file_data.get("file_id")
    
    # ساخت کپشن با پشتیبانی از name و description
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

    # ارسال مستقیم فایل از file_id تلگرام
    try:
        sent_msg = bot.send_document(
            chat_id,
            file_id,
            caption=caption,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(f"خطا در ارسال فایل به {chat_id}: {e}")
        bot.send_message(chat_id, "❌ خطا در ارسال فایل.")
        return False

    # زمان‌بندی حذف پیام از چت بعد از یک دقیقه
    try:
        threading.Timer(DELETE_AFTER, lambda: safe_delete_message(chat_id, sent_msg.message_id)).start()
    except Exception as e:
        logger.warning(f"خطا در زمان‌بندی حذف پیام: {e}")
    
    return True

# ---------- هندلر /start ----------
@bot.message_handler(commands=['start'])
def handle_start(message):
    parts = message.text.split()
    if len(parts) > 1:
        param = parts[1]
        
        # اگر پارامتر batch باشه
        if param.startswith('batch_'):
            file_keys = param.replace('batch_', '')
            files = file_keys.split('_')
            
            # بررسی عضویت برای همه فایل‌ها قبل از ارسال
            needs_membership = False
            required_category = None
            
            for file_key in files:
                if file_key in FILE_DATABASE:
                    category = get_file_category(file_key)
                    if not is_member(message.from_user.id, category):
                        needs_membership = True
                        required_category = category
                        break
            
            # اگر کاربر برای هر فایلی عضو نیست، پیام عضویت نمایش بده
            if needs_membership:
                markup = build_channel_markup(required_category)
                if markup:
                    bot.send_message(message.chat.id, "❌ برای دریافت فایل‌ها باید ابتدا در چنل‌ها عضو شوید.", reply_markup=markup, parse_mode="HTML")
                return
            
            # اگر کاربر برای همه فایل‌ها عضو هست، ارسال کن
            successful_sends = 0
            for file_key in files:
                if file_key in FILE_DATABASE:
                    if download_and_send_file(message.chat.id, message.from_user.id, file_key):
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
            # فایل تکی - مستقیما تابع رو صدا بزن
            download_and_send_file(message.chat.id, message.from_user.id, param)
            
        return

    # اگر پارامتر نداشت - فقط پیام خوش آمدگویی ساده
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
    if c.data.startswith("check_"):
        category = c.data.replace("check_", "")
        if is_member(c.from_user.id, category):
            bot.answer_callback_query(c.id, "✅ عضویت شما تأیید شد!", show_alert=True)
            # پیدا کردن فایل‌های این دسته و ارسال
            category_files = []
            for key in FILE_DATABASE.keys():
                if get_file_category(key) == category:
                    category_files.append(key)
            
            # شمارنده فایل‌های ارسال شده
            successful_sends = 0
            
            for key in category_files:
                if download_and_send_file(c.message.chat.id, c.from_user.id, key):
                    successful_sends += 1
                time.sleep(0.5)
            
            # فقط اگر حداقل یک فایل ارسال شده باشه، پیام تأیید نمایش بده
            if successful_sends > 0:
                try:
                    confirm_msg = bot.send_message(c.message.chat.id, "✅ فایل‌ها ارسال شد. (پیام‌ها پس از ۱ دقیقه حذف خواهند شد.لطفا ذخیره کنید.)")
                    threading.Timer(DELETE_AFTER, lambda: safe_delete_message(c.message.chat.id, confirm_msg.message_id)).start()
                except Exception:
                    pass
        else:
            bot.answer_callback_query(c.id, "❌ هنوز عضو نیستید!", show_alert=True)
    
    elif c.data == "no_url":
        bot.answer_callback_query(c.id, "این کانال لینک قابل‌استفاده ندارد.", show_alert=True)

# ---------- Health server برای Render ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

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

    # حذف webhook در صورت فعال بودن (جلوگیری از 409)
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