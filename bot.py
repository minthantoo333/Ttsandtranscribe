import os
import logging
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from faster_whisper import WhisperModel
import ffmpeg

# ================= CONFIG =================
# We use your specific variable names here
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- FLASK SERVER (To keep Render happy) ---
app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is breathing and alive!"

def run_flask():
    # Listens on the port Render assigns, or defaults to 10000
    app.run(host="0.0.0.0", port=PORT)

# --- AI MODEL LOAD ---
# 'tiny' is fastest for free CPU. 
model = WhisperModel("tiny", device="cpu", compute_type="int8")

# --- HELPER: TIME FORMATTING ---
def format_timestamp(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

# --- BOT LOGIC ---
async def process_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    status_msg = await update.message.reply_text(f"Downloading video from {user.first_name}...\n(Limit: 20MB)")

    # Paths
    video_id = f"{user.id}_{update.message.message_id}"
    input_path = f"{video_id}.mp4"
    srt_path = f"{video_id}.srt"
    output_path = f"{video_id}_subbed.mp4"

    try:
        # 1. Download Video
        video_file = await update.message.video.get_file()
        await video_file.download_to_drive(input_path)

        await context.bot.edit_message_text("Transcribing audio... 📝", chat_id=update.message.chat_id, message_id=status_msg.message_id)

        # 2. Transcribe (Auto-detects language)
        # Using beam_size=5 for slightly better accuracy
        segments, info = model.transcribe(input_path, beam_size=5)
        
        # 3. Generate SRT File
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, start=1):
                start = format_timestamp(segment.start)
                end = format_timestamp(segment.end)
                text = segment.text.strip()
                f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

        await context.bot.edit_message_text("Burning subtitles... 🔥", chat_id=update.message.chat_id, message_id=status_msg.message_id)

        # 4. Burn Subtitles into Video
        input_ffmpeg = ffmpeg.input(input_path)
        
        (
            ffmpeg
            .output(
                input_ffmpeg, 
                output_path, 
                # Force styling for Burmese font support
                vf=f"subtitles={srt_path}:force_style='Fontname=Noto Sans Myanmar,FontSize=20'", 
                acodec='copy' 
            )
            .run(overwrite_output=True, quiet=True)
        )

        # 5. Upload Result
        await context.bot.edit_message_text("Uploading...", chat_id=update.message.chat_id, message_id=status_msg.message_id)
        await update.message.reply_video(video=open(output_path, 'rb'), caption="✅ Subtitles Added")

    except Exception as e:
        logging.error(e)
        await update.message.reply_text("Error processing video. (Check if file > 20MB)")

    finally:
        # Clean up files
        for p in [input_path, srt_path, output_path]:
            if os.path.exists(p):
                os.remove(p)

# --- MAIN ---
def main():
    # 1. Start Flask
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # 2. Start Bot
    if not TOKEN:
        print("Error: TELEGRAM_TOKEN environment variable is missing.")
        return

    app_bot = Application.builder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.VIDEO, process_video))
    app_bot.run_polling()

if __name__ == "__main__":
    main()
