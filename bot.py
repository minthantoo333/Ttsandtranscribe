import os
import logging
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, 
    MessageHandler, 
    CommandHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes
)
from faster_whisper import WhisperModel
import ffmpeg

# ================= CONFIGURATION =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
MAX_FILE_SIZE_MB = 19  # Safety margin below Telegram's 20MB limit

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK SERVER (Keep-Alive) ---
app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is active and running!"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

# --- AI MODEL ---
# Using 'tiny' for speed on free servers.
model = WhisperModel("tiny", device="cpu", compute_type="int8")

# --- UTILITIES ---
def format_timestamp(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message with a menu."""
    user = update.effective_user
    
    welcome_text = (
        f"👋 <b>Hello, {user.first_name}!</b>\n\n"
        "I am your <b>Auto-Subtitle Bot</b>. 🎬\n"
        "Send me a video, and I will burn subtitles into it automatically.\n\n"
        "<i>Power limit: Files must be under 20MB.</i>"
    )
    
    # Create buttons
    keyboard = [
        [InlineKeyboardButton("📚 How to Use", callback_data='help_menu')],
        [InlineKeyboardButton("⚙️ About", callback_data='about_menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks."""
    query = update.callback_query
    await query.answer() # Acknowledge the click

    if query.data == 'help_menu':
        await query.edit_message_text(
            text="<b>📚 How to Use:</b>\n\n"
                 "1. Send a video file (MP4, MOV, etc).\n"
                 "2. Wait for me to process it.\n"
                 "3. I will send back the video with subtitles!\n\n"
                 "⚠️ <b>Note:</b> Video must be under <b>20MB</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='start_menu')]])
        )
    
    elif query.data == 'about_menu':
        await query.edit_message_text(
            text="<b>⚙️ About</b>\n\n"
                 "Built with Python, FFmpeg, and OpenAI Whisper.\n"
                 "Running on Render.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='start_menu')]])
        )

    elif query.data == 'start_menu':
        # Return to main menu (re-using the logic from start_command conceptually)
        keyboard = [
            [InlineKeyboardButton("📚 How to Use", callback_data='help_menu')],
            [InlineKeyboardButton("⚙️ About", callback_data='about_menu')]
        ]
        await query.edit_message_text(
            text="👋 <b>Main Menu</b>\n\nSend me a video to start!",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def process_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main logic with robust error handling."""
    video = update.message.video
    
    # 1. IMMEDIATE ERROR CHECK: File Size
    file_size_mb = video.file_size / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(
            f"❌ <b>File too large!</b>\n\n"
            f"Your file is {file_size_mb:.2f}MB.\n"
            f"I can only handle files under {MAX_FILE_SIZE_MB}MB due to Telegram limits.",
            parse_mode=ParseMode.HTML
        )
        return

    # Initial status message
    status_msg = await update.message.reply_text("⏳ <b>Initializing...</b>", parse_mode=ParseMode.HTML)

    # Paths
    user_id = update.effective_user.id
    unique_id = f"{user_id}_{update.message.message_id}"
    input_path = f"in_{unique_id}.mp4"
    srt_path = f"sub_{unique_id}.srt"
    output_path = f"out_{unique_id}.mp4"

    try:
        # --- STEP 1: DOWNLOAD ---
        await context.bot.edit_message_text("⬇️ <b>Downloading video...</b>", chat_id=update.message.chat_id, message_id=status_msg.message_id, parse_mode=ParseMode.HTML)
        video_file = await video.get_file()
        await video_file.download_to_drive(input_path)

        # --- STEP 2: TRANSCRIBE ---
        await context.bot.edit_message_text("✍️ <b>Transcribing audio...</b>\n<i>(This may take a moment)</i>", chat_id=update.message.chat_id, message_id=status_msg.message_id, parse_mode=ParseMode.HTML)
        
        # Run model
        segments, info = model.transcribe(input_path, beam_size=5)
        
        # Write SRT
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, start=1):
                start = format_timestamp(segment.start)
                end = format_timestamp(segment.end)
                text = segment.text.strip()
                f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

        # --- STEP 3: BURN SUBTITLES ---
        await context.bot.edit_message_text("🔥 <b>Burning subtitles...</b>", chat_id=update.message.chat_id, message_id=status_msg.message_id, parse_mode=ParseMode.HTML)
        
        input_ffmpeg = ffmpeg.input(input_path)
        (
            ffmpeg
            .output(
                input_ffmpeg, 
                output_path, 
                # Style: Yellow font, black outline for readability
                vf=f"subtitles={srt_path}:force_style='Fontname=Noto Sans Myanmar,FontSize=20,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1'", 
                acodec='copy'
            )
            .run(overwrite_output=True, quiet=True)
        )

        # --- STEP 4: UPLOAD ---
        await context.bot.edit_message_text("⬆️ <b>Uploading result...</b>", chat_id=update.message.chat_id, message_id=status_msg.message_id, parse_mode=ParseMode.HTML)
        
        with open(output_path, 'rb') as video_data:
            await update.message.reply_video(
                video=video_data, 
                caption="✅ <b>Subtitles Added Successfully!</b>",
                parse_mode=ParseMode.HTML
            )
        
        # Delete the status message to keep chat clean
        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=status_msg.message_id)

    except Exception as e:
        logger.error(f"Error processing video: {e}")
        await context.bot.edit_message_text(
            f"❌ <b>Error Occurred</b>\n\nSomething went wrong while processing.\nDetails: <code>{str(e)[:100]}</code>",
            chat_id=update.message.chat_id, 
            message_id=status_msg.message_id,
            parse_mode=ParseMode.HTML
        )

    finally:
        # cleanup
        for p in [input_path, srt_path, output_path]:
            if os.path.exists(p):
                os.remove(p)

async def post_init(application: Application):
    """Sets the bot menu commands automatically on startup."""
    await application.bot.set_my_commands([
        BotCommand("start", "Restart the bot"),
        BotCommand("help", "How to use this bot"),
    ])

# --- MAIN ---
def main():
    # Start Flask in background
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    if not TOKEN:
        print("Error: TELEGRAM_TOKEN is missing.")
        return

    # Application Builder
    app_bot = Application.builder().token(TOKEN).post_init(post_init).build()

    # Add Handlers
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CommandHandler("help", start_command)) # Map help to start for menu
    app_bot.add_handler(CallbackQueryHandler(button_callback)) # Handle button clicks
    app_bot.add_handler(MessageHandler(filters.VIDEO, process_video))

    # Run
    print("Bot is polling...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
