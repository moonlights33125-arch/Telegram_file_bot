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
REQUIRED_CHANNELS_RAW = os.environ.get("REQUIRED_CHANNELS", "").strip()
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

# ترکیب در یک دیتابیس کلی
FILE_DATABASE = {}
FILE_DATABASE.update(NOVELS_DB)
FILE_DATABASE.update(MANHWA_DB)
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
                out.append({"display": f"@{username}", "chat_id": f"@{username}"})
            else:
                # قبول می‌کنیم اما ممکن است قابل بررسی نباشد
                out.append({"display": part, "chat_id": part})
    return out

REQUIRED_CHANNELS = parse_channels(REQUIRED_CHANNELS_RAW)

# ---------- دکمه‌ها ----------
def build_channel_markup():
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(REQUIRED_CHANNELS, start=1):
        chat = ch.get("chat_id", "")
        if chat:
            url = f"https://t.me/{chat.lstrip('@')}"
            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", url=url))
        else:
            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", callback_data="no_url"))
    markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data="check"))
    return markup

# ---------- بررسی عضویت ----------
def is_member(user_id):
    for ch in REQUIRED_CHANNELS:
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
    # بررسی عضویت
    if not is_member(user_id):
        try:
            bot.send_message(chat_id, "❌ برای دریافت فایل باید ابتدا در چنل‌ها عضو شوید.", reply_markup=build_channel_markup(), parse_mode="HTML")
        except Exception as e:
            logger.warning(f"خطا در ارسال پیام عضویت: {e}")
        return

    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        bot.send_message(chat_id, "❌ فایل یافت نشد.")
        return

    file_id = file_data.get("file_id")  # تغییر: direct_link → file_id
    description = file_data.get("description", "")

    # ارسال مستقیم فایل از file_id تلگرام
    try:
        sent_msg = bot.send_document(
            chat_id,
            file_id,  # استفاده مستقیم از file_id
            caption=f"📄 <b>{file_data.get('name', 'فایل')}</b>\n\n{(description and (description + '\\n\\n')) or ''}⏰ این فایل ۱ دقیقه بعد حذف خواهد شد. لطفاً آن را ذخیره کنید.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception(f"خطا در ارسال فایل به {chat_id}: {e}")
        bot.send_message(chat_id, "❌ خطا در ارسال فایل.")
        return

    # زمان‌بندی حذف پیام از چت بعد از یک دقیقه
    try:
        threading.Timer(DELETE_AFTER, lambda: safe_delete_message(chat_id, sent_msg.message_id)).start()
    except Exception as e:
        logger.warning(f"خطا در زمان‌بندی حذف پیام: {e}")

# ---------- ارسال همه فایل‌ها (پس از تایید عضویت) ----------
def send_all_files(chat_id, user_id):
    if not FILE_DATABASE:
        bot.send_message(chat_id, "❌ هیچ فایلی موجود نیست.")
        return
    for key in list(FILE_DATABASE.keys()):
        download_and_send_file(chat_id, user_id, key)
    try:
        bot.send_message(chat_id, "✅ فایل‌ها ارسال شد. (پیام‌ها پس از ۱ دقیقه حذف خواهند شد.)")
    except Exception:
        pass

# ---------- هندلر /start ----------
@bot.message_handler(commands=['start'])
def handle_start(message):
    parts = message.text.split()
    if len(parts) > 1:
        file_key = parts[1]
        download_and_send_file(message.chat.id, message.from_user.id, file_key)
        return

    if not is_member(message.from_user.id):
        try:
            bot.send_message(message.chat.id, "👋 خوش آمدید!\nبرای دریافت فایل‌ها ابتدا در چنل‌ها عضو شوید:", reply_markup=build_channel_markup(), parse_mode="HTML")
        except Exception:
            pass
    else:
        send_all_files(message.chat.id, message.from_user.id)

# ---------- callback برای دکمه‌ها ----------
@bot.callback_query_handler(func=lambda c: True)
def handle_callback(c):
    if c.data == "check":
        if is_member(c.from_user.id):
            bot.answer_callback_query(c.id, "✅ عضویت شما تأیید شد!", show_alert=True)
            send_all_files(c.message.chat.id, c.from_user.id)
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