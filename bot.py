 import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import os
import json
import requests
import tempfile
import logging

# تنظیمات
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(name)

# خواندن از متغیرهای محیطی
BOT_TOKEN = os.environ.get('BOT_TOKEN')
REQUIRED_CHANNELS = os.environ.get('REQUIRED_CHANNELS', '').split(',')
FILE_DATABASE_JSON = os.environ.get('FILE_DATABASE', '{}')

try:
    FILE_DATABASE = json.loads(FILE_DATABASE_JSON)
except:
    FILE_DATABASE = {}

if not BOT_TOKEN:
    logger.error("BOT_TOKEN تنظیم نشده است!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

def check_channel_membership(user_id):
    """بررسی عضویت کاربر در کانال‌های اجباری"""
    for channel in REQUIRED_CHANNELS:
        if channel.strip():
            try:
                chat_member = bot.get_chat_member(channel.strip(), user_id)
                if chat_member.status in ['left', 'kicked']:
                    return False, channel.strip()
            except Exception as e:
                logger.error(f"خطا در بررسی کانال {channel}: {e}")
                return False, channel.strip()
    return True, None

@bot.message_handler(commands=['start'])
def start_command(message):
    """دستور شروع /start"""
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    
    # بررسی عضویت
    is_member, channel = check_channel_membership(user_id)
    
    if not is_member:
        # ایجاد دکمه‌های عضویت
        keyboard = []
        for req_channel in REQUIRED_CHANNELS:
            if req_channel.strip():
                keyboard.append([InlineKeyboardButton(
                    f"📢 عضویت در {req_channel}", 
                    url=f"https://t.me/{req_channel.strip()[1:]}"
                )])
        
        keyboard.append([InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        bot.send_message(
            message.chat.id,
            f"👋 سلام {first_name}!\n\n"
            "🤖 به ربات دانلود فایل خوش آمدید!\n\n"
            "📢 برای دسترسی به فایل‌ها، لطفاً در کانال‌های زیر عضو شوید:\n"
            + "\n".join([f"• {ch}" for ch in REQUIRED_CHANNELS if ch.strip()])
            + "\n\n🎯 پس از عضویت، روی دکمه 'بررسی عضویت' کلیک کنید.",
            reply_markup=reply_markup
        )
        return
    
    # نمایش منوی اصلی
    show_main_menu(message)

def show_main_menu(message):
    """نمایش منوی اصلی"""
    keyboard = []
    for file_id, file_data in FILE_DATABASE.items():
        keyboard.append([InlineKeyboardButton(
            file_data["name"], 
            callback_data=f"download_{file_id}"
        )])
    
    # دکمه‌های کمکی
    keyboard.append([
        InlineKeyboardButton("🔄 بروزرسانی", callback_data="refresh"),
        InlineKeyboardButton("ℹ️ راهنما", callback_data="help")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        f"🎉 سلام {message.from_user.first_name}!\n\n"
        "📁 لیست فایل‌های موجود:\n\n"
        "👉 روی فایل مورد نظر کلیک کنید تا دانلود شود.\n\n"
        "⚠️ توجه: فایل پس از دانلود، ۳۰ ثانیه در ربات باقی می‌ماند.\n"
        "💾 لطفاً فایل را promptly ذخیره کنید."
    )
    
    bot.send_message(message.chat.id, text, reply_markup=reply_markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    """مدیریت کلیک روی دکمه‌ها"""
    if call.data == "check_membership":
        user_id = call.from_user.id
        is_member, channel = check_channel_membership(user_id)
        
        if not is_member:
            bot.answer_callback_query(call.id, "❌ هنوز عضو نشدید! لطفاً ابتدا عضو شوید.", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "✅ عضویت شما تأیید شد!", show_alert=True)
            show_main_menu(call.message)
    
    elif call.data == "refresh":
        bot.answer_callback_query(call.id, "🔄 لیست بروزرسانی شد!")
        show_main_menu(call.message)
    
    elif call.data == "help":
        bot.answer_callback_query(call.id)
        show_help_menu(call.message)
    
    elif call.data.startswith("download_"):
        file_key = call.data[9:]  # حذف "download_" از ابتدا
        download_and_send_file(call.message, file_key)

def download_and_send_file(message, file_key):
    """دانلود و ارسال فایل به کاربر"""
    user_id = message.chat.id
    
    # بررسی عضویت
    is_member, channel = check_channel_membership(user_id)
    if not is_member:
        bot.send_message(user_id, f"❌ برای دانلود باید در {channel} عضو شوید!\nدستور /start را بزنید.")
        return
    
    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        bot.send_message(user_id, "❌ فایل پیدا نشد!")
        return
    
    # اطلاع‌رسانی شروع دانلود
    progress_msg = bot.send_message(
        user_id,
        f"⏳ در حال آماده‌سازی فایل...\n\n"
        f"📝 {file_data['name']}\n"
        f"📦 حجم: {file_data['size']}\n\n"
        "لطفاً کمی صبر کنید...",
        parse_mode='Markdown'
    )
    
    try:
        # دانلود فایل از لینک مستقیم
        direct_link = file_data["direct_link"]
        
        bot.edit_message_text(
            f"📥 در حال دانلود...\n\n"
            f"📝 {file_data['name']}\n"
            f"📦 حجم: {file_data['size']}\n"
            f"📋 توضیحات: {file_data['description']}\n\n"
            "⏳ لطفاً منتظر بمانید...",
            user_id,
            progress_msg.message_id,
            parse_mode='Markdown'
        )
        
        # دانلود فایل
        response = requests.get(direct_link, stream=True, timeout=30)
        response.raise_for_status()
        
        # ایجاد فایل موقت
        file_extension = os.path.splitext(direct_link)[1] or '.bin'
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    temp_file.write(chunk)
            temp_file_path = temp_file.name
        
        # ارسال فایل به کاربر
        bot.edit_message_text(
            "📤 در حال آپلود فایل...",
            user_id,
            progress_msg.message_id
        )
        
        if file_data["type"] == "video":
            with open(temp_file_path, 'rb') as file:
                bot.send_video(
                    user_id,
                    file,
                    caption=(
                        f"🎬 {file_data['name']}\n\n"
                        f"📝 {file_data['description']}\n"
                        f"📦 حجم: {file_data['size']}\n\n"
                        "⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد!\n"
                        "💾 لطفاً فایل را promptly ذخیره کنید."
                    ),
                    parse_mode='Markdown'
                )
        elif file_data["type"] == "audio":
            with open(temp_file_path, 'rb') as file:
                bot.send_audio(
                    user_id,
                    file,
                    caption=(
                        f"🎵 {file_data['name']}\n\n"
                        f"📝 {file_data['description']}\n"
                        f"📦 حجم: {file_data['size']}\n\n"
                        "⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد!\n"
                        "💾 لطفاً فایل را promptly ذخیره کنید."
                    ),
                    parse_mode='Markdown'
                )
        else:
            with open(temp_file_path, 'rb') as file:
                bot.send_document(
                    user_id,
                    file,
                    caption=(
            f"📄 {file_data['name']}\n\n"
                        f"📝 {file_data['description']}\n"
                        f"📦 حجم: {file_data['size']}\n\n"
                        "⏰ این فایل ۳۰ ثانیه دیگر حذف خواهد شد!\n"
                        "💾 لطفاً فایل را promptly ذخیره کنید."
                    ),
                    parse_mode='Markdown'
                )
        
        # پاک کردن فایل موقت
        os.unlink(temp_file_path)
        
        # پیام موفقیت
        bot.edit_message_text(
            f"✅ {file_data['name']} با موفقیت ارسال شد!\n\n"
            "📥 فایل به پیوی شما ارسال شده است.\n"
            "⏰ به یاد داشته باشید: فایل ۳۰ ثانیه دیگر حذف می‌شود!\n\n"
            "🎉 از فایل لذت ببرید!",
            user_id,
            progress_msg.message_id,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"خطا در دانلود فایل: {e}")
        bot.edit_message_text(
            "❌ خطا در دانلود فایل!\n\n"
            "⚠️ دلایل احتمالی:\n"
            "• لینک فایل مشکل دارد\n"
            "• سرور در دسترس نیست\n"
            "• حجم فایل زیاد است\n"
            "• محدودیت پهنای باند\n\n"
            "🔧 لطفاً:\n"
            "• دوباره تلاش کنید\n"
            "• با پشتیبانی تماس بگیرید\n"
            "• از فایل دیگری استفاده کنید",
            user_id,
            progress_msg.message_id
        )

def show_help_menu(message):
    """نمایش منوی راهنما"""
    help_text = (
        "📖 راهنمای ربات دانلود فایل\n\n"
        "🎯 نحوه استفاده:\n"
        "1. در کانال‌های اجباری عضو شوید\n"
        "2. از منوی اصلی فایل مورد نظر را انتخاب کنید\n"
        "3. روی فایل کلیک کنید تا دانلود شود\n"
        "4. فایل را promptly ذخیره کنید\n\n"
        "⚠️ نکات مهم:\n"
        "• فایل‌ها ۳۰ ثانیه پس از ارسال حذف می‌شوند\n"
        "• حتماً فایل‌ها را ذخیره کنید\n"
        "• برای مشکل در عضویت، /start را بزنید\n"
        "• برای بروزرسانی لیست، دکمه 🔄 را بزنید\n\n"
        "🔧 پشتیبانی:\n"
        "در صورت مشکل با پشتیبانی تماس بگیرید."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    bot.send_message(message.chat.id, help_text, reply_markup=reply_markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data == "back_to_menu")
def back_to_menu(call):
    """بازگشت به منوی اصلی"""
    bot.answer_callback_query(call.id)
    show_main_menu(call.message)

@bot.message_handler(commands=['help'])
def help_command(message):
    """دستور راهنما"""
    bot.send_message(
        message.chat.id,
        "📖 راهنمای ربات:\n\n"
        "• /start - شروع کار و نمایش منوی اصلی\n"
        "• /help - نمایش این راهنما\n\n"
        "🎯 برای شروع دستور /start را ارسال کنید.",
        parse_mode='Markdown'
    )

if name == 'main':
    logger.info("ربات در حال اجرا است...")
    bot.infinity_polling()            
