# bot.py
import os
import json
import logging
import threading
import tempfile
import time
import mimetypes
import html
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---------- تنظیم لاگ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------- متغیرهای محیطی ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
REQUIRED_CHANNELS_RAW = os.environ.get("REQUIRED_CHANNELS", "").strip()
PORT = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", 10000)))

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN تنظیم نشده است.")
    raise SystemExit(1)

# ---------- خواندن دیتابیس‌های جدا (در صورت نبودن، {} برمی‌گرداند) ----------
def safe_json_load(key):
    try:
        data = os.environ.get(key, "{}")
        return json.loads(data) if data else {}
    except Exception as e:
        logger.warning(f"⚠️ خطا در خواندن متغیر محیطی {key}: {e}")
        return {}

NOVELS_DB = safe_json_load("NOVELS_DATABASE")
MANHWA_DB = safe_json_load("MANHWA_DATABASE")
MOVIES_DB = safe_json_load("MOVIES_DATABASE")

# ---------- ترکیب دیتابیس‌ها در یک دیتابیس کلی ----------
FILE_DATABASE = {}
FILE_DATABASE.update(NOVELS_DB)
FILE_DATABASE.update(MANHWA_DB)
FILE_DATABASE.update(MOVIES_DB)
logger.info(f"📦 تعداد کل فایل‌ها: {len(FILE_DATABASE)}")

# ---------- راه‌اندازی بات ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# ---------- پردازش کانال‌های مورد نیاز ----------
def parse_required_channels(raw):
    out = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        # اجازه می‌ده کاربر @username یا https://t.me/username بد بده
        if part.startswith("@"):
            out.append({"display": part, "chat_id": part})
        else:
            parsed = urlparse(part)
            if parsed.netloc and "t.me" in parsed.netloc:
                username = parsed.path.strip("/")
                out.append({"display": f"@{username}", "chat_id": f"@{username}"})
            else:
                # اگر فرمت نامشخص بود، نِگه می‌داره اما قابل بررسی نیست
                out.append({"display": part, "chat_id": part})
    return out

REQUIRED_CHANNELS = parse_required_channels(REQUIRED_CHANNELS_RAW)

# ---------- بررسی عضویت در کانال‌ها ----------
def check_channel_membership(user_id):
    """
    برمی‌گرداند (True/None) یا (False, display_name) اگر کاربر در یکی از کانال‌ها عضو نیست
    """
    for ch in REQUIRED_CHANNELS:
        chat_id = ch.get("chat_id")
        display = ch.get("display") or chat_id
        try:
            member = bot.get_chat_member(chat_id, user_id)
            status = getattr(member, "status", None)
            if status in ("left", "kicked", "restricted", None):
                return False, display
        except Exception as e:
            # در صورت خطا (مثلاً بات عضو کانال نیست یا chat_id اشتباه است) عضویت رد می‌شود
            logger.warning(f"خطا در get_chat_member برای {display}: {e}")
            return False, display
    return True, None

# ---------- دکمه‌های عضویت (عنوان ساده بدون نشان دادن username) ----------
def build_channel_buttons_markup():
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(REQUIRED_CHANNELS, start=1):
        # دکمه‌ای که لینک join کانال رو باز می‌کنه
        chat_id = ch.get("chat_id", "")
        # اگر chat_id مثل @username بود، از اون برای لینک استفاده می‌کنیم
        url = f"https://t.me/{chat_id.lstrip('@')}" if chat_id else None
        if url:
            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", url=url))
        else:
            markup.row(InlineKeyboardButton(f"📢 عضویت در چنل {i}", callback_data="no_url"))
    # دکمه بررسی عضویت
    markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership"))
    return markup

# ---------- helper: استخراج filename از header content-disposition ----------
def filename_from_cd(cd):
    if not cd:
        return None
    # content-disposition: attachment; filename="fname.ext"
    try:
        parts = cd.split(';')
        for p in parts:
            p = p.strip()
            if p.lower().startswith("filename="):
                fn = p.split("=",1)[1].strip().strip('"')
                return fn
    except Exception:
        pass
    return None

# ---------- helper برای دانلود از Google Drive (مدیریت confirm token) ----------
def download_from_google_drive(file_id, dest_path):
    session = requests.Session()
    URL = "https://docs.google.com/uc?export=download"
    response = session.get(URL, params={'id': file_id}, stream=True)
    token = None
    # بررسی کوکی‌ها برای توکن دانلود
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            token = value
            break
    if token:
        response = session.get(URL, params={'id': file_id, 'confirm': token}, stream=True)
    response.raise_for_status()
    # نوشتن در فایل
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(32768):
            if chunk:
                f.write(chunk)
    # تلاش برای استخراج filename از header
    fname = filename_from_cd(response.headers.get('content-disposition'))
    return fname

# ---------- دانلود فایل به مسیر موقت و تعیین اسم نهایی ----------
def download_file_to_temp(link, suggested_name=None):
    """
    لینک را دانلود می‌کند و مسیر فایل محلی و نام پیشنهادی برای ارسال را برمی‌گرداند.
    suggested_name را اگر داشته باشیم به عنوان نام پیش‌فرض استفاده می‌کنیم.
    """
    parsed = urlparse(link)
    temp_path = None
    final_name = None

    # اگر لینک گوگل درایو باشه، از روش مخصوص استفاده کن
    if parsed.netloc and "drive.google.com" in parsed.netloc:
        # استخراج id
        qs = parse_qs(parsed.query)
        file_id = None
        if "id" in qs:
            file_id = qs.get("id")[0]
        else:
            # ممکنه لینک به شکل /file/d/<id>/ باشد
            parts = parsed.path.split("/")
            if "d" in parts:
                try:
                    di = parts.index("d")
                    file_id = parts[di + 1]
                except Exception:
                    file_id = None
        if not file_id:
            raise ValueError("Cannot extract Google Drive file id from URL")
        # مسیر موقت با پسوند از suggested_name یا بدون پسوند
        ext = os.path.splitext(suggested_name or "")[1] or ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            temp_path = tf.name
        # دانلود با مدیریت confirm token
        fname = download_from_google_drive(file_id, temp_path)
        final_name = fname or suggested_name or os.path.basename(temp_path)
        return temp_path, final_name

    # برای بقیه لینک‌ها
    resp = requests.get(link, stream=True, timeout=60)
    resp.raise_for_status()
    cd = resp.headers.get('content-disposition')
    fname = filename_from_cd(cd)
    # یا از suggested_name یا از URL path
    if fname:
        final_name = fname
    else:
        # از بخش path استفاده کن اگر پسوند و اسم دارد
        path_name = os.path.basename(parsed.path)
        final_name = path_name if path_name else (suggested_name or "file")

    # تعیین پسوند اگر نیاز باشه با content-type
    ext = os.path.splitext(final_name)[1]
    if not ext:
        ctype = resp.headers.get('content-type', '')
        guessed = mimetypes.guess_extension(ctype.split(";")[0].strip()) if ctype else None
        if guessed:
            final_name += guessed

    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(final_name)[1] or "") as tf:
        temp_path = tf.name
        for chunk in resp.iter_content(32768):
            if chunk:
                tf.write(chunk)

    return temp_path, final_name

# ---------- حذف امن پیام ----------
def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.warning(f"خطا در حذف پیام {message_id} از چت {chat_id}: {e}")

# ---------- ارسال فایل با نام درست و حذف خودکار بعد 30 ثانیه ----------
def send_file_and_schedule_delete(chat_id, file_path, send_name, ftype, caption):
    sent = None
    try:
        with open(file_path, "rb") as f:
            # تلاش برای ارسال با نام مورد نظر (اگر کتابخانه از filename پشتیبانی کند)
            try:
                # بیشتر ورژن‌های pyTelegramBotAPI پارامتر filename را می‌پذیرند
                sent = bot.send_document(chat_id, f, caption=caption, parse_mode='HTML', filename=send_name)
            except TypeError:
                # اگر signature متفاوت بود، تلاش بدون filename
                f.seek(0)
                sent = bot.send_document(chat_id, f, caption=caption, parse_mode='HTML')
    except Exception as e:
        logger.exception(f"خطا در ارسال فایل به کاربر {chat_id}: {e}")
        raise

    # زمان‌بندی حذف پیام ارسالی بعد از 30 ثانیه
    if sent is not None:
        threading.Timer(30, lambda: safe_delete_message(chat_id, sent.message_id)).start()
    return sent

# ---------- دانلود و ارسال فایل (اصلاح‌شده) ----------
def download_and_send_file(chat_id, user_id, file_key):
    # ابتدا بررسی عضویت و اگر نبود، دکمه‌های عضویت را ارسال کن (تا کاربر بتواند عضو شود)
    is_member, channel = check_channel_membership(user_id)
    if not is_member:
        markup = build_channel_buttons_markup()
        safe_text = "❌ برای دانلود فایل باید ابتدا در چنل(ها) عضو شوید."
        # ارسال پیام همراه با دکمه‌های join
        try:
            bot.send_message(chat_id, safe_text, reply_markup=markup, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"خطا در ارسال پیام عضویت: {e}")
        return

    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        bot.send_message(chat_id, "❌ فایل یافت نشد.")
        return

    # اطلاعات فایل
    name_raw = file_data.get("name", "file")
    # اگر نام شامل پسوند نیست، خوبه که کاربر نام با پسوند وارد کنه؛ اما ما سعی می‌کنیم از header هم اسم بگیریم.
    direct_link = file_data.get("direct_link")
    ftype = file_data.get("type", "file")
    size = file_data.get("size", "")
    desc = file_data.get("description", "")

    # پیام آماده‌سازی
    try:
        bot.send_message(chat_id, f"⏳ در حال آماده‌سازی فایل <b>{html.escape(name_raw)}</b> ...", parse_mode='HTML')
    except Exception:
        pass

    # دانلود فایل به temp و تعیین نام نهایی
    try:
        temp_path, final_name = download_file_to_temp(direct_link, suggested_name=name_raw)
    except Exception as e:
        logger.exception(f"خطا در دانلود فایل {file_key}: {e}")
        bot.send_message(chat_id, "❌ خطا در دانلود فایل. احتمالاً لینک قابل دانلود مستقیم نیست.")
        return

    # ساخت کپشن برای ارسال
    caption = f"{html.escape(final_name)}\n\n{html.escape(desc)}\n\n📦 {html.escape(size)}\n\n⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد. لطفاً ذخیره کنید."

    # ارسال فایل و زمان‌بندی حذف پیام
    try:
        sent_msg = send_file_and_schedule_delete(chat_id, temp_path, final_name, ftype, caption)
    except Exception as e:
        logger.exception(f"خطا در ارسال فایل به {chat_id}: {e}")
        bot.send_message(chat_id, "❌ خطا در ارسال فایل. لطفاً بعداً تلاش کنید.")
        # حذف فایل موقت در صورت وجود
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            pass
        return

    # حذف فایل موقت از سرور (بعد از ارسال)
    try:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    except Exception as e:
        logger.warning(f"خطا در حذف فایل موقت: {e}")

# ---------- ارسال همه فایل‌ها پس از تایید عضویت ----------
def send_all_files(chat_id):
    if not FILE_DATABASE:
        bot.send_message(chat_id, "❌ هیچ فایلی موجود نیست.")
        return
    for key in FILE_DATABASE.keys():
        # برای هر فایل دانلود و ارسال می‌کنیم
        # در اینجا user_id نامش لازم نیست؛ ما چک عضویت را در download_and_send_file دوباره انجام می‌دهیم
        # برای ارسال دسته‌ای همان چت را به عنوان user_id نیز می‌فرستیم
        download_and_send_file(chat_id, chat_id, key)
    bot.send_message(chat_id, "✅ فایل‌ها ارسال شد.\n\n⚠️ توجه: فایل‌ها پس از ۳۰ ثانیه از چت حذف می‌شوند.\n💾 لطفاً ذخیره کنید.")

# ---------- هندلر /start (با پشتیبانی از پارامتر start) ----------
@bot.message_handler(commands=['start'])
def start_command(message):
    parts = message.text.split()
    if len(parts) > 1:
        # اگر کاربری با لینک مثل ?start=file1 اومده باشه، پارامتر همان parts[1] ست
        file_key = parts[1]
        download_and_send_file(message.chat.id, message.from_user.id, file_key)
        return

    # در حالت عادی، بررسی عضویت و نمایش متن/دکمه
    is_member, channel = check_channel_membership(message.from_user.id)
    if not is_member:
        markup = build_channel_buttons_markup()
        safe_send = "👋 خوش آمدید!\nبرای دسترسی به فایل‌ها لطفاً ابتدا در چنل‌ها عضو شوید:"
        bot.send_message(message.chat.id, safe_send, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(message.chat.id, "✅ فایل‌ها ارسال شد.\n\n⚠️ توجه: فایل‌ها پس از ۳۰ ثانیه از چت حذف می‌شوند.\n💾 لطفاً ذخیره کنید.", parse_mode='HTML')

# ---------- کال‌بک دکمه‌ها ----------
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "check_membership":
        is_member, channel = check_channel_membership(call.from_user.id)
        if is_member:
            bot.answer_callback_query(call.id, "✅ عضویت شما تأیید شد!", show_alert=True)
            send_all_files(call.message.chat.id)
        else:
            bot.answer_callback_query(call.id, "❌ هنوز عضو نیستید!", show_alert=True)
    elif call.data == "no_url":
        bot.answer_callback_query(call.id, "این کانال لینک مستقیم ندارد.", show_alert=True)

# ---------- Health server برای Render ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"Health server listening on 0.0.0.0:{PORT}")
        server.serve_forever()
    except Exception as e:
        logger.exception(f"Health server error: {e}")

# ---------- main ----------
if __name__ == "__main__":
    # راه‌اندازی health server در ترد جداگانه
    threading.Thread(target=run_health_server, daemon=True).start()

    # حذف webhook اگر فعال است (جلوگیری از خطای 409)
    try:
        bot.remove_webhook()
    except Exception as e:
        logger.warning(f"خطا هنگام حذف webhook (بی‌اهمیت اگر webhook قبلا حذف شده): {e}")

    logger.info("Bot started — starting polling...")
    # حلقه polling با تلاش مجدد در صورت خطا
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.exception(f"Polling exception: {e}")
            time.sleep(5)