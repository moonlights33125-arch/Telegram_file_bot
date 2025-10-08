# bot.py
# نسخهٔ اصلاح‌شده — آمادهٔ دیپلوی روی Render (Web Service).
import os
import json
import logging
import tempfile
import threading
import time
import html
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---------- تنظیمات لاگ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------- خواندن متغیرهای محیطی ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
REQUIRED_CHANNELS_RAW = os.environ.get("REQUIRED_CHANNELS", "").strip()
FILE_DATABASE_JSON = os.environ.get("FILE_DATABASE", "{}")  # باید JSON معتبر باشد
PORT = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", 10000)))  # Render از PORT استفاده می‌کند

if not BOT_TOKEN:
    logger.error("متغیر محیطی BOT_TOKEN تعریف نشده است. لطفاً توکن ربات را تنظیم کنید.")
    raise SystemExit(1)

# ---------- بارگذاری دیتابیس فایل‌ها ----------
try:
    FILE_DATABASE = json.loads(FILE_DATABASE_JSON) if FILE_DATABASE_JSON else {}
    if not isinstance(FILE_DATABASE, dict):
        logger.warning("FILE_DATABASE باید یک شیٔ JSON باشد: {} -> تنظیم به {} خالی".format(FILE_DATABASE_JSON, "{}"))
        FILE_DATABASE = {}
except Exception as e:
    logger.exception("خطا در خواندن FILE_DATABASE از متغیر محیطی. مقدار باید JSON معتبر باشد.")
    FILE_DATABASE = {}

# ---------- نرمال‌سازی کانال‌ها ----------
def parse_required_channels(raw: str):
    """
    ورودی: رشته‌ای مثل "@chan1,@chan2,https://t.me/chan3,-100123456..."
    خروجی: لیستی از دیکشنری‌ها با فیلدهای:
      - display: نمایشی برای متن دکمه
      - url: لینک برای دکمه (ممکن است None باشد)
      - chat_id: مقداری که می‌توان به get_chat_member داد (مثل '@username' یا int) یا None اگر قابل بررسی نباشد
    """
    out = []
    for part in [p.strip() for p in raw.split(",")]:
        if not part:
            continue
        # لینک t.me
        if part.lower().startswith("http"):
            try:
                parsed = urlparse(part)
                path = parsed.path.strip("/")
                if path:
                    last = path.split("/")[-1]
                    if last.startswith("+") or last.lower().startswith("joinchat"):
                        # invite link — نمی‌توانیم membership را از طریق get_chat_member بررسی کنیم
                        out.append({"raw": part, "display": part, "url": part, "chat_id": None})
                    else:
                        username = last.lstrip("@")
                        out.append({
                            "raw": part,
                            "display": "@"+username,
                            "url": f"https://t.me/{username}",
                            "chat_id": "@"+username
                        })
                else:
                    out.append({"raw": part, "display": part, "url": part, "chat_id": None})
            except Exception:
                out.append({"raw": part, "display": part, "url": part, "chat_id": None})
        else:
            # ممکن است @username یا numeric id یا username
            candidate = part
            if candidate.startswith("@"):
                candidate = candidate[1:]
            # اگر عددی باشه (مثلا -100123...) -> تبدیل به int
            if candidate.lstrip("-").isdigit():
                try:
                    cid = int(candidate)
                    # برای لینک دکمه، اگر منفی و شبیه ID کانال، URL ندارد
                    url = None if str(candidate).startswith("-") else f"https://t.me/{candidate}"
                    out.append({"raw": part, "display": str(candidate), "url": url, "chat_id": cid})
                except Exception:
                    out.append({"raw": part, "display": part, "url": None, "chat_id": None})
            else:
                out.append({"raw": part, "display": "@"+candidate, "url": f"https://t.me/{candidate}", "chat_id": "@"+candidate})
    return out

REQUIRED_CHANNELS = parse_required_channels(REQUIRED_CHANNELS_RAW)

# ---------- تلگرام بوت ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)  # ما خودمان متن‌ها را escape و به HTML می‌دهیم

# ---------- ساخت مپ کوتاه برای callback_data (برای امنیت و محدودیت طول) ----------
SHORT_MAP = {}
SHORT_TO_DISPLAY = {}
for idx, key in enumerate(FILE_DATABASE.keys()):
    short = f"f{idx}"
    SHORT_MAP[short] = key
    try:
        name = FILE_DATABASE[key].get("name", str(key))
    except Exception:
        name = str(key)
    SHORT_TO_DISPLAY[short] = name

# ---------- توابع کمکی ----------
def esc(text):
    """escape برای HTML (نام‌ها و متن‌های داینامیک)"""
    return html.escape(str(text)) if text is not None else ""

def check_channel_membership(user_id):
    """
    بررسی می‌کند کاربر عضو همهٔ کانال‌های REQUIRED_CHANNELS هست یا نه.
    برمی‌گرداند: (True/False, problematic_channel_display_or_None)
    """
    for ch in REQUIRED_CHANNELS:
        display = ch.get("display") or ch.get("raw")
        chat_id = ch.get("chat_id")
        if chat_id is None:
            logger.warning(f"قادر به بررسی کانال {display} نیستم (invite/link یا نام نامعتبر).")
            # رفتار فعلی: اگر قابل بررسی نباشد، عضویت را تایید‌نشده می‌دانیم
            return False, display
        try:
            member = bot.get_chat_member(chat_id, user_id)
            status = getattr(member, "status", None)
            if status in ("left", "kicked", "restricted", None):
                return False, display
        except Exception as e:
            # معمولا خطا زمانی می‌آید که:
            # - بات عضو کانال نیست
            # - یا آی‌دی/نام کانال اشتباه است
            logger.warning(f"خطا هنگام get_chat_member برای {display}: {e}")
            return False, display
    return True, None

def build_menu_markup():
    """می‌سازد منوی فایل‌ها و دکمه‌های کمکی"""
    markup = InlineKeyboardMarkup()
    # هر فایل را در یک ردیف قرار می‌دهیم
    for short, display_name in SHORT_TO_DISPLAY.items():
        text = display_name if len(display_name) <= 40 else display_name[:37] + "..."
        btn = InlineKeyboardButton(text, callback_data=f"download_{short}")
        markup.row(btn)
    # دکمه‌های کمکی
    markup.row(
        InlineKeyboardButton("🔄 بروزرسانی", callback_data="refresh"),
        InlineKeyboardButton("ℹ️ راهنما", callback_data="help")
    )
    return markup

def build_channel_buttons_markup():
    """می‌سازد دکمه‌های عضویت در کانال‌ها + دکمه بررسی"""
    markup = InlineKeyboardMarkup()
    for ch in REQUIRED_CHANNELS:
        url = ch.get("url")
        display = ch.get("display") or ch.get("raw")
        if url:
            markup.row(InlineKeyboardButton(f"📢 عضویت در {display}", url=url))
        else:
            # اگر URL نداریم، یک دکمه متن ثابت اضافه می‌کنیم که صرفا نمایش است
            markup.row(InlineKeyboardButton(f"📢 {display}", callback_data="no_url"))
    markup.row(InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership"))
    return markup

def safe_send_message(chat_id, text, reply_markup=None):
    """ارسال امن پیام (لاگ خطا، جلوگیری از کرش)"""
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception as e:
        logger.exception(f"خطا هنگام ارسال پیام به {chat_id}: {e}")
        # تلاش دوباره بدون parse_mode (fallback)
        try:
            return bot.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as e2:
            logger.exception(f"دوباره نتوانستم پیام را ارسال کنم به {chat_id}: {e2}")
            return None

# ---------- هندلرها ----------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    first_name = esc(message.from_user.first_name or message.from_user.username or "دوست")
    # بررسی عضویت
    is_member, channel = check_channel_membership(message.from_user.id)
    if not is_member:
        # دکمه‌های عضویت نمایش داده شود
        markup = build_channel_buttons_markup()
        text = (
            f"👋 سلام <b>{first_name}</b>!\n\n"
            "🤖 به ربات دانلود فایل خوش آمدید!\n\n"
            "📢 برای دسترسی به فایل‌ها، لطفاً در کانال‌های زیر عضو شوید:\n"
            + "\n".join([f"• {esc(ch.get('display') or ch.get('raw'))}" for ch in REQUIRED_CHANNELS if (ch.get('display') or ch.get('raw'))])
            + "\n\n🎯 پس از عضویت، روی دکمه 'بررسی عضویت' کلیک کنید."
        )
        safe_send_message(message.chat.id, text, reply_markup=markup)
        return
    # در غیر این صورت منوی اصلی را نشان بده
    show_main_menu(message)

def show_main_menu(message):
    markup = build_menu_markup()
    first_name = esc(message.from_user.first_name or message.from_user.username or "دوست")
    text = (
        f"🎉 <b>سلام {first_name}!</b>\n\n"
        "📁 <b>لیست فایل‌های موجود:</b>\n\n"
        "👉 روی فایل مورد نظر کلیک کنید تا دانلود شود.\n\n"
        "⚠️ <b>توجه:</b> فایل پس از دانلود، ۳۰ ثانیه در ربات باقی می‌ماند.\n"
        "💾 لطفاً فایل را ذخیره کنید."
    )
    safe_send_message(message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data or ""
    # بررسی عضویت
    if data == "check_membership":
        user_id = call.from_user.id
        is_member, channel = check_channel_membership(user_id)
        if not is_member:
            bot.answer_callback_query(call.id, "❌ هنوز عضو نشدید یا امکان بررسی نیست! لطفاً ابتدا عضو شوید.", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "✅ عضویت شما تأیید شد!", show_alert=True)
            show_main_menu(call.message)
        return

    if data == "refresh":
        bot.answer_callback_query(call.id, "🔄 لیست بروزرسانی شد!")
        show_main_menu(call.message)
        return

    if data == "help":
        bot.answer_callback_query(call.id)
        show_help_menu(call.message)
        return

    if data == "no_url":
        bot.answer_callback_query(call.id, "این آیتم لینک دعوت مستقیم دارد یا قابل بررسی نیست.", show_alert=True)
        return

    if data.startswith("download_"):
        short = data[len("download_"):]
        # فراخوانی تابع دانلود با شیٔ call تا هم از call.from_user و هم call.message استفاده کنیم
        download_and_send_file(call, short)
        return

    if data == "back_to_menu":
        bot.answer_callback_query(call.id)
        show_main_menu(call.message)
        return

def download_and_send_file(call, file_short):
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    # بررسی عضویت
    is_member, channel = check_channel_membership(user_id)
    if not is_member:
        bot.send_message(chat_id, f"❌ برای دانلود باید در {esc(channel)} عضو شوید!\nدستور /start را بزنید.")
        return

    file_key = SHORT_MAP.get(file_short)
    if not file_key:
        bot.send_message(chat_id, "❌ شناسهٔ فایل نامعتبر است.")
        return

    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        bot.send_message(chat_id, "❌ فایل پیدا نشد (دیتابیس فایل‌ها خالی یا نامعتبر است).")
        return

    # اعتبارسنجی فیلدهای ضروری
    direct_link = file_data.get("direct_link")
    if not direct_link:
        bot.send_message(chat_id, "❌ لینک مستقیم فایل در فایل دیتابیس تعریف نشده است.")
        return

    name = esc(file_data.get("name", "فایل"))
    size = esc(file_data.get("size", "نامشخص"))
    description = esc(file_data.get("description", ""))
    ftype = file_data.get("type", "file")  # video, audio, file

    # ارسال پیام پیشروی
    progress_msg = safe_send_message(
        chat_id,
        f"⏳ <b>در حال آماده‌سازی فایل...</b>\n\n"
        f"📝 <b>{name}</b>\n"
        f"📦 حجم: {size}\n\n"
        "لطفاً کمی صبر کنید..."
    )
    try:
        # دانلود فایل
        bot.edit_message_text(
            f"📥 <b>در حال دانلود...</b>\n\n"
            f"📝 <b>{name}</b>\n"
            f"📦 حجم: {size}\n"
            f"📋 توضیحات: {description}\n\n"
            "⏳ لطفاً منتظر بمانید...",
            chat_id,
            progress_msg.message_id
        )
        resp = requests.get(direct_link, stream=True, timeout=60)
        resp.raise_for_status()

        # تعیین پسوند
        ext = os.path.splitext(direct_link)[1] or ".bin"
        temp_path = None
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            temp_path = tf.name
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    tf.write(chunk)

        # آپلود به تلگرام
        bot.edit_message_text("📤 <b>در حال آپلود فایل...</b>", chat_id, progress_msg.message_id)

        with open(temp_path, "rb") as f:
            if ftype == "video":
                bot.send_video(chat_id, f, caption=f"🎬 <b>{name}</b>\n\n{description}\n\n📦 {size}\n\n⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد!", parse_mode='HTML')
            elif ftype == "audio":
                bot.send_audio(chat_id, f, caption=f"🎵 <b>{name}</b>\n\n{description}\n\n📦 {size}\n\n⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد!", parse_mode='HTML')
            else:
                bot.send_document(chat_id, f, caption=f"📄 <b>{name}</b>\n\n{description}\n\n📦 {size}\n\n⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد!", parse_mode='HTML')

        # حذف فایل موقت
        try:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            logger.exception("خطا هنگام حذف فایل موقت")

        # پیام موفقیت
        bot.edit_message_text(
            f"✅ <b>{name}</b> با موفقیت ارسال شد!\n\n"
            "📥 فایل به پیوی شما ارسال شده است.\n"
            "⏰ به یاد داشته باشید: فایل ۳۰ ثانیه دیگر حذف می‌شود!\n\n"
            "🎉 از فایل لذت ببرید!",
            chat_id,
            progress_msg.message_id
        )

    except Exception as e:
        logger.exception(f"خطا در دانلود/ارسال فایل ({file_key}): {e}")
        # سعی کن پیام خطا را ویرایش کنی، و اگر نتوانستی، پیامی جدید بفرست
        try:
            bot.edit_message_text(
                "❌ <b>خطا در دانلود یا ارسال فایل!</b>\n\n"
                "⚠️ دلایل احتمالی:\n"
                "• لینک فایل مشکل دارد\n"
                "• سرور در دسترس نیست\n"
                "• حجم فایل بسیار زیاد است\n\n"
                "🔧 لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.",
                chat_id,
                progress_msg.message_id,
                parse_mode='HTML'
            )
        except Exception:
            bot.send_message(chat_id, "❌ خطا در دانلود یا ارسال فایل. لطفاً دوباره تلاش کنید.")

        # تلاش برای حذف فایل موقت در صورت وجود
        try:
            if 'temp_path' in locals() and temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            logger.exception("خطا هنگام حذف فایل موقت در حالت استثنا")

def show_help_menu(message):
    help_text = (
        "📖 <b>راهنمای ربات دانلود فایل</b>\n\n"
        "🎯 <b>نحوه استفاده:</b>\n"
        "1. در کانال‌های اجباری عضو شوید\n"
        "2. از منوی اصلی فایل مورد نظر را انتخاب کنید\n"
        "3. روی فایل کلیک کنید تا دانلود شود\n"
        "4. فایل را ذخیره کنید\n\n"
        "⚠️ <b>نکات مهم:</b>\n"
        "• فایل‌ها ۳۰ ثانیه پس از ارسال حذف می‌شوند\n"
        "• برای مشکل در عضویت، /start را بزنید\n"
        "• برای بروزرسانی لیست، دکمه 🔄 را بزنید\n\n"
        "🔧 <b>پشتیبانی:</b>\n"
        "در صورت مشکل با پشتیبانی تماس بگیرید."
    )
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu"))
    safe_send_message(message.chat.id, help_text, reply_markup=markup)

@bot.message_handler(commands=['help'])
def cmd_help(message):
    text = (
        "📖 <b>راهنمای ربات:</b>\n\n"
        "• /start - شروع کار و نمایش منوی اصلی\n"
        "• /help - نمایش این راهنما\n\n"
        "🎯 برای شروع دستور /start را ارسال کنید."
    )
    safe_send_message(message.chat.id, text)

# ---------- سرور HTTP ساده برای health (تا Render متوجه Web Service شود) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

def run_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"Health HTTP server listening on 0.0.0.0:{PORT}")
        server.serve_forever()
    except Exception as e:
        logger.exception(f"خطا در راه‌اندازی health server روی پورت {PORT}: {e}")

# ---------- main: راه‌اندازی سرور و polling ----------
if __name__ == "__main__":
    # ابتدا سرور HTTP را در ترد جداگانه اجرا می‌کنیم تا Render سرویس را شناسایی کند
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()

    logger.info("ربات در حال اجرا است — شروع polling...")
    # حلقهٔ ساده با تلاش مجدد در صورت بروز خطا (برای روبرو شدن با قطع‌های کوتاه‌مدت شبکه)
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as exc:
            logger.exception(f"خطا در polling: {exc}. تلاش مجدد پس از 5 ثانیه...")
            time.sleep(5)