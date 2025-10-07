from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, CallbackContext, MessageHandler, filters
import requests
import os
import logging
import tempfile
import json
import asyncio

# تنظیمات
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# خواندن از متغیرهای محیطی
BOT_TOKEN = os.environ.get('BOT_TOKEN')
REQUIRED_CHANNELS = os.environ.get('REQUIRED_CHANNELS', '').split(',')
FILE_DATABASE_JSON = os.environ.get('FILE_DATABASE', '{}')

try:
    FILE_DATABASE = json.loads(FILE_DATABASE_JSON)
except:
    FILE_DATABASE = {}

# بررسی تنظیمات ضروری
if not BOT_TOKEN:
    logger.error("BOT_TOKEN تنظیم نشده است!")
    exit(1)

async def check_channel_membership(user_id, context):
    """بررسی عضویت کاربر در کانال‌های اجباری"""
    for channel in REQUIRED_CHANNELS:
        if channel.strip():
            try:
                member = await context.bot.get_chat_member(channel.strip(), user_id)
                if member.status in ['left', 'kicked']:
                    return False, channel.strip()
            except Exception as e:
                logger.error(f"خطا در بررسی کانال {channel}: {e}")
                return False, channel.strip()
    return True, None

async def start(update: Update, context: CallbackContext):
    """دستور شروع /start"""
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    
    # بررسی عضویت
    is_member, channel = await check_channel_membership(user_id, context)
    
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
        
        await update.message.reply_text(
            f"👋 سلام {first_name}!\n\n"
            "🤖 به ربات دانلود فایل خوش آمدید!\n\n"
            "📢 برای دسترسی به فایل‌ها، لطفاً در کانال‌های زیر عضو شوید:\n"
            + "\n".join([f"• {ch}" for ch in REQUIRED_CHANNELS if ch.strip()])
            + "\n\n🎯 پس از عضویت، روی دکمه 'بررسی عضویت' کلیک کنید.",
            reply_markup=reply_markup
        )
        return
    
    # نمایش منوی اصلی
    await show_main_menu(update, context, first_name)

async def show_main_menu(update, context, first_name=None):
    """نمایش منوی اصلی"""
    if not first_name:
        first_name = ""
    
    # ایجاد دکمه‌های فایل‌ها
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
        f"🎉 **سلام {first_name}!**\n\n"
        "📁 **لیست فایل‌های موجود:**\n\n"
        "👉 روی فایل مورد نظر کلیک کنید تا دانلود شود.\n\n"
        "⚠️ **توجه:** فایل پس از دانلود، ۳۰ ثانیه در ربات باقی می‌ماند.\n"
        "💾 لطفاً فایل را promptly ذخیره کنید."
    )
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def download_and_send_file(update: Update, context: CallbackContext, file_key: str):
    """دانلود و ارسال فایل به کاربر"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    # بررسی عضویت
    is_member, channel = await check_channel_membership(user_id, context)
    if not is_member:
        await query.edit_message_text(f"❌ برای دانلود باید در {channel} عضو شوید!\nدستور /start را بزنید.")
        return
    
    file_data = FILE_DATABASE.get(file_key)
    if not file_data:
        await query.edit_message_text("❌ فایل پیدا نشد!")
        return
    
    # اطلاع‌رسانی شروع دانلود
    progress_msg = await query.edit_message_text(
        f"⏳ **در حال آماده‌سازی فایل...**\n\n"
        f"📝 **{file_data['name']}**\n"
        f"📦 حجم: {file_data['size']}\n\n"
        "لطفاً کمی صبر کنید...",
        parse_mode='Markdown'
    )
    
    try:
        # دانلود فایل از لینک مستقیم
        direct_link = file_data["direct_link"]
        
        await progress_msg.edit_text(
            f"📥 **در حال دانلود...**\n\n"
            f"📝 **{file_data['name']}**\n"
            f"📦 حجم: {file_data['size']}\n"
            f"📋 توضیحات: {file_data['description']}\n\n"
            "⏳ لطفاً منتظر بمانید...",
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
        await progress_msg.edit_text("📤 **در حال آپلود فایل...**")
        
        if file_data["type"] == "video":
            await context.bot.send_video(
                chat_id=user_id,
                video=open(temp_file_path, 'rb'),
                caption=(
                    f"🎬 **{file_data['name']}**\n\n"
                    f"📝 {file_data['description']}\n"
                    f"📦 حجم: {file_data['size']}\n\n"
                    "⏰ **این فایل ۳۰ ثانیه دیگر حذف خواهد شد!**\n"
                    "💾 لطفاً فایل را promptly ذخیره کنید."
                ),
                parse_mode='Markdown'
            )
        elif file_data["type"] == "audio":
            await context.bot.send_audio(
                chat_id=user_id,
                audio=open(temp_file_path, 'rb'),
                caption=(
                    f"🎵 **{file_data['name']}**\n\n"
                    f"📝 {file_data['description']}\n"
                    f"📦 حجم: {file_data['size']}\n\n"
                    "⏰ **این فایل ۳۰ ثانیه دیگر حذف خواهد شد!**\n"
                    "💾 لطفاً فایل را promptly ذخیره کنید."
                ),
                parse_mode='Markdown'
            )
        else:
            await context.bot.send_document(
                chat_id=user_id,
                document=open(temp_file_path, 'rb'),
                caption=(
                    f"📄 **{file_data['name']}**\n\n"
                    f"📝 {file_data['description']}\n"
                    f"📦 حجم: {file_data['size']}\n\n"
                    "⏰ **این فایل ۳۰ ثانیه دیگر حذف خواهد شد!**\n"
                    "💾 لطفاً فایل را promptly ذخیره کنید."
                ),
                parse_mode='Markdown'
            )
        
        # پاک کردن فایل موقت
        os.unlink(temp_file_path)
        
        # پیام موفقیت
        await progress_msg.edit_text(
            f"✅ **{file_data['name']}** با موفقیت ارسال شد!\n\n"
            "📥 فایل به پیوی شما ارسال شده است.\n"
            "⏰ به یاد داشته باشید: فایل ۳۰ ثانیه دیگر حذف می‌شود!\n\n"
            "🎉 از فایل لذت ببرید!",
            parse_mode='Markdown'
        )
        
        # حذف پیام هشدار بعد از ۳۵ ثانیه
        await asyncio.sleep(35)
        try:
            await progress_msg.edit_text(
                f"🎉 امیدواریم از **{file_data['name']}** لذت برده باشید!\n\n"
                "📂 برای دریافت فایل‌های بیشتر از منوی اصلی استفاده کنید.",
                parse_mode='Markdown'
            )
        except:
            pass
        
    except Exception as e:
        logger.error(f"خطا در دانلود فایل: {e}")
        await progress_msg.edit_text(
            "❌ **خطا در دانلود فایل!**\n\n"
            "⚠️ دلایل احتمالی:\n"
            "• لینک فایل مشکل دارد\n"
            "• سرور در دسترس نیست\n"
            "• حجم فایل زیاد است\n"
            "• محدودیت پهنای باند\n\n"
            "🔧 لطفاً:\n"
            "• دوباره تلاش کنید\n"
            "• با پشتیبانی تماس بگیرید\n"
            "• از فایل دیگری استفاده کنید"
        )

async def button_handler(update: Update, context: CallbackContext):
    """مدیریت کلیک روی دکمه‌ها"""
    query = update.callback_query
    data = query.data
    
    if data == "check_membership":
        user_id = query.from_user.id
        is_member, channel = await check_channel_membership(user_id, context)
        
        if not is_member:
            await query.answer("❌ هنوز عضو نشدید! لطفاً ابتدا عضو شوید.", show_alert=True)
        else:
            await query.answer("✅ عضویت شما تأیید شد!", show_alert=True)
            await show_main_menu(update, context)
    
    elif data == "refresh":
        await query.answer("🔄 لیست بروزرسانی شد!")
        await show_main_menu(update, context)
    
    elif data == "help":
        await query.answer()
        await show_help_menu(update, context)
    
    elif data.startswith("download_"):
        file_key = data[9:]  # حذف "download_" از ابتدا
        await download_and_send_file(update, context, file_key)

async def show_help_menu(update: Update, context: CallbackContext):
    """نمایش منوی راهنما"""
    query = update.callback_query
    
    help_text = (
        "📖 **راهنمای ربات دانلود فایل**\n\n"
        "🎯 **نحوه استفاده:**\n"
        "1. در کانال‌های اجباری عضو شوید\n"
        "2. از منوی اصلی فایل مورد نظر را انتخاب کنید\n"
        "3. روی فایل کلیک کنید تا دانلود شود\n"
        "4. فایل را promptly ذخیره کنید\n\n"
        "⚠️ **نکات مهم:**\n"
        "• فایل‌ها ۳۰ ثانیه پس از ارسال حذف می‌شوند\n"
        "• حتماً فایل‌ها را ذخیره کنید\n"
        "• برای مشکل در عضویت، /start را بزنید\n"
        "• برای بروزرسانی لیست، دکمه 🔄 را بزنید\n\n"
        "🔧 **پشتیبانی:**\n"
        "در صورت مشکل با پشتیبانی تماس بگیرید."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode='Markdown')

async def back_to_menu(update: Update, context: CallbackContext):
    """بازگشت به منوی اصلی"""
    query = update.callback_query
    await query.answer()
    await show_main_menu(update, context)

async def help_command(update: Update, context: CallbackContext):
    """دستور راهنما"""
    await update.message.reply_text(
        "📖 **راهنمای ربات:**\n\n"
        "• /start - شروع کار و نمایش منوی اصلی\n"
        "• /help - نمایش این راهنما\n\n"
        "🎯 برای شروع دستور /start را ارسال کنید.",
        parse_mode='Markdown'
    )

def main():
    """تابع اصلی"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # اضافه کردن هندلرها
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    # هندلرهای کال‌بک
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(check_membership|refresh|help|download_.*)$"))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    
    # اجرای ربات
    logger.info("ربات در حال اجرا است...")
    application.run_polling()

if __name__ == '__main__':
    main()