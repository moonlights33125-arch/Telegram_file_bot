import os
import json
import logging
import threading
import tempfile
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---------- تنظیمات لاگ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------- متغیرهای محیطی ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
REQUIRED_CHANNELS_RAW = os.environ.get("REQUIRED_CHANNELS", "").strip()
PORT = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", 10000)))

# ---------- دیتابیس‌ها ----------
def safe_json_load(var_name):
    try:
        data = os.environ.get(var_name, "{}")
        return json.loads(data) if data else {}
    except Exception as e:
        logger.warning(f"خطا در خواندن {var_name}: {e}")
        return {}

NOVELS_DB = safe_json_load("NOVELS_DATABASE")
MANHWA_DB = safe_json_load("MANHWA_DATABASE")
MOVIES_DB = safe_json_load("MOVIES_DATABASE")

# ادغام دیتابیس‌ها در یک فایل کلی
FILE_DATABASE = {}
FILE_DATABASE.update(NOVELS_DB)
FILE_DATABASE.update(MANHWA_DB)
FILE_DATABASE.update(MOVIES_DB)
logger.info(f"📦 تعداد کل فایل‌ها: {len(FILE_DATABASE)}")

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN تنظیم نشده است.")
    raise SystemExit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# ---------- تبدیل لیست کانال‌ها ----------
def parse_required_channels(raw):
    out = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if part.startswith("@"):
            out.append({"display": part, "chat_id": part})
        else:
            parsed = urlparse(part)
            if parsed.netloc.endswith("t.me"):
                username = parsed.path.strip("/")
                out.append({"display": f"@{username}", "chat_id": f"@{username}"})
    return out

REQUIRED_CHANNELS = parse_required_channels(REQUIRED_CHANNELS_RAW)

# ---------- تابع بررسی عضویت ----------
def check_channel_membership(user_id):
    for ch in REQUIRED_CHANNELS:
        chat_id = ch.get("chat_id")
        display = ch.get("display")
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                return False, display
        except Exception as e:
            logger.warning(f"⚠️ خطا در بررسی {display}: {e}")
            return False, display
    return True, None

# ---------- منوهای بات ----------
def build_channel_buttons_markup():
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(REQUIRED_CHANNELS, start=1):
        markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", url=f"https://t.me/{ch['chat_id'].lstrip('@')}"))
    markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership"))
    return markup

# ---------- ارسال ایمن ----------
def safe_send_message(chat_id, text, markup=None):
    try:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"⚠️ خطا در ارسال پیام: {e}")

# ---------- دستور /start ----------
@bot.message_handler(commands=['start'])
def start_command(message):
    args = message.text.split()
    if len(args) > 1:  # مثلاً /start file1
        file_key = args[1]
        download_and_send_file(message.chat.id, message.from_user.id, file_key)
        return

    # بررسی عضویت
    is_member, channel = check_channel_membership(message.from_user.id)
    if not is_member:
        markup = build_channel_buttons_markup()
        safe_send_message(message.chat.id, "👋 خوش آمدید!\n\nبرای دسترسی به فایل‌ها ابتدا در کانال‌ها عضو شوید 👇", markup)
        return

    # اگر عضو بود
    send_all_files(message.chat.id)

# ---------- هندلر کال‌بک ----------
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "check_membership":
        is_member, channel = check_channel_membership(call.from_user.id)
        if not is_member:
            bot.answer_callback_query(call.id, "❌ هنوز عضو نیستید!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "✅ عضویت شما تایید شد!")
            send_all_files(call.message.chat.id)

# ---------- ارسال فایل ----------
def download_and_send_file(chat_id, user_id, file_key):
    is_member, channel = check_channel_membership(user_id)
    if not is_member:
        safe_send_message(chat_id, "❌ برای دریافت فایل باید در کانال‌ها عضو باشید.")
        return

    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        safe_send_message(chat_id, "❌ فایل یافت نشد.")
        return

    name = file_data.get("name", "فایل")
    link = file_data.get("direct_link")
    desc = file_data.get("description", "")
    size = file_data.get("size", "")
    ftype = file_data.get("type", "file")

    safe_send_message(chat_id, f"📥 در حال آماده‌سازی فایل <b>{name}</b> ...")

    try:
        r = requests.get(link, stream=True, timeout=60)
        r.raise_for_status()
        ext = os.path.splitext(link)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            for chunk in r.iter_content(8192):
                if chunk:
                    tf.write(chunk)
            temp_path = tf.name

        with open(temp_path, "rb") as f:
            if ftype == "video":
                sent = bot.send_video(chat_id, f, caption=f"🎬 <b>{name}</b>\n{desc}\n📦 {size}\n⏰ حذف بعد از ۳۰ ثانیه", parse_mode='HTML')
            elif ftype == "audio":
                sent = bot.send_audio(chat_id, f, caption=f"🎵 <b>{name}</b>\n{desc}\n📦 {size}\n⏰ حذف بعد از ۳۰ ثانیه", parse_mode='HTML')
            else:
                sent = bot.send_document(chat_id, f, caption=f"📄 <b>{name}</b>\n{desc}\n📦 {size}\n⏰ حذف بعد از ۳۰ ثانیه", parse_mode='HTML')

        # حذف فایل موقت از سرور
        os.unlink(temp_path)

        # حذف پیام از تلگرام بعد از ۳۰ ثانیه
        threading.Timer(30, lambda: bot.delete_message(chat_id, sent.message_id)).start()

    except Exception as e:
        logger.error(f"❌ خطا در دانلود یا ارسال فایل: {e}")
        safe_send_message(chat_id, "❌ خطا در ارسال فایل. لطفاً بعداً دوباره تلاش کنید.")

# ---------- ارسال همه فایل‌ها بعد از تایید عضویت ----------
def send_all_files(chat_id):
    if not FILE_DATABASE:
        safe_send_message(chat_id, "❌ هیچ فایلی در دیتابیس موجود نیست.")
        return

    for key in FILE_DATABASE.keys():
        download_and_send_file(chat_id, chat_id, key)

    safe_send_message(chat_id, "✅ فایل‌ها ارسال شد.\n\n⚠️ توجه: فایل‌ها پس از ۳۰ ثانیه از چت حذف می‌شوند.\n💾 لطفاً ذخیره کنید.")

# ---------- Health Check Server ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

def run_health():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"🌐 Health server running on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"خطا در health server: {e}")

# ---------- اجرای بات ----------
if __name__ == "__main__":
    threading.Thread(target=run_health, daemon=True).start()
    bot.remove_webhook()  # ✅ حذف وبهوک برای جلوگیری از خطای 409
    logger.info("🤖 Bot started. Running polling...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)