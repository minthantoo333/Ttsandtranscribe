import os, logging, threading, tempfile, asyncio, time, math
from http.server import HTTPServer, BaseHTTPRequestHandler

import edge_tts
import pysrt
from pydub import AudioSegment

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ================= CONFIG =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

# Defaults
DEFAULT_VOICE = "my-MM-ThihaNeural"
MAX_SPEED = 1.5
MIN_BREATH_MS = 300
MAX_BREATH_MS = 800
RETRY_COUNT = 3
RETRY_DELAY = 1

# ================= LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= VOICE CATALOG =================
VOICE_CATALOG = {
    "🇲🇲 Male (Thiha)": "my-MM-ThihaNeural",
    "🇲🇲 Female (Nilar)": "my-MM-NilarNeural",
    "🇺🇸 US Male (Guy)": "en-US-GuyNeural",
    "🇺🇸 US Female (Jenny)": "en-US-JennyNeural",
    "🇬🇧 UK Male (Ryan)": "en-GB-RyanNeural",
    "🇬🇧 UK Female (Libby)": "en-GB-LibbyNeural",
    "🇯🇵 JP Male (Keita)": "ja-JP-KeitaNeural",
    "🇯🇵 JP Female (Nanami)": "ja-JP-NanamiNeural",
    "🇰🇷 KR Female (SunHi)": "ko-KR-SunHiNeural",
    "🇨🇳 CN Female (Xiaoxiao)": "zh-CN-XiaoxiaoNeural",
    "🇹🇭 TH Female (Premwadee)": "th-TH-PremwadeeNeural"
}

# ================= KEEP ALIVE =================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Alive")

def run_web():
    HTTPServer(("0.0.0.0", PORT), SimpleHandler).serve_forever()

# ================= UI HELPERS =================
def create_progress_bar(current, total, length=10):
    percent = current / total
    filled_length = int(length * percent)
    bar = "█" * filled_length + "░" * (length - filled_length)
    return f"{bar} {int(percent * 100)}%"

async def update_status(message, text):
    """Safely edit message text ignoring 'Message Not Modified' errors"""
    try:
        if message.text != text:
            await message.edit_text(text)
    except Exception:
        pass

# ================= AUDIO UTILS =================
def srt_time_to_ms(t):
    return (t.hours*3600 + t.minutes*60 + t.seconds)*1000 + t.milliseconds

def estimate_seconds(text):
    return max(0.4, len(text)/14)

def preprocess_text(text):
    # Remove HTML tags often found in SRTs
    clean = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    return clean.replace("။", "။\n").replace(".", ".\n").strip()

def calculate_breath_ms(text):
    base = MIN_BREATH_MS
    extra = min(len(text)//30*100, MAX_BREATH_MS - MIN_BREATH_MS)
    return base + extra

async def generate_tts(text, voice, rate=0, cache={}):
    key = (text, voice, rate)
    if key in cache: return cache[key]

    rate_str = f"+{rate}%"
    for attempt in range(RETRY_COUNT):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                temp_path = tmp.name
            await edge_tts.Communicate(text=text, voice=voice, rate=rate_str).save(temp_path)
            audio = AudioSegment.from_file(temp_path)
            os.remove(temp_path)
            cache[key] = audio
            return audio
        except Exception as e:
            logging.warning(f"TTS attempt {attempt+1} failed: {e}")
            await asyncio.sleep(RETRY_DELAY * (attempt+1))
    # Return silent audio if fails to avoid crashing entire render
    return AudioSegment.silent(duration=1000) 

# ================= CORE ENGINE: SRT → AUDIO =================
async def srt_to_audio(srt_file, output_file, voice, status_msg):
    subs = pysrt.open(srt_file)
    total_subs = len(subs)
    final_audio = AudioSegment.silent(0)
    cursor = 0
    i = 0
    cache = {}
    
    last_update_time = 0
    
    while i < total_subs:
        # --- UI PROGRESS UPDATE (Every 3 seconds or 10%) ---
        current_time = time.time()
        if current_time - last_update_time > 3:
            bar = create_progress_bar(i, total_subs)
            await update_status(status_msg, f"⚙️ **Processing Audio...**\n\n{bar}\nLine: {i}/{total_subs}")
            last_update_time = current_time

        sub = subs[i]
        start_ms = srt_time_to_ms(sub.start)
        end_ms = srt_time_to_ms(sub.end)
        slot_ms = end_ms - start_ms
        slot_sec = slot_ms / 1000
        text = preprocess_text(sub.text)
        
        if not text:
            i += 1
            continue

        if start_ms > cursor:
            gap = min(start_ms - cursor, calculate_breath_ms(text))
            final_audio += AudioSegment.silent(gap)
            cursor += gap

        est = estimate_seconds(text)
        rate = 0
        temp_text = text

        # Simple Logic for Speed/Merge
        if est <= slot_sec:
            pass
        elif est <= slot_sec * MAX_SPEED:
            rate = min(int((est/slot_sec - 1) * 100), int((MAX_SPEED - 1) * 100))
        else:
            # Check next for merge
            if i + 1 < total_subs:
                next_sub = subs[i+1]
                gap = srt_time_to_ms(next_sub.start) - end_ms
                if gap <= 500:
                    temp_text += " " + preprocess_text(next_sub.text)
                    slot_ms += srt_time_to_ms(next_sub.end) - srt_time_to_ms(next_sub.start) + gap
                    slot_sec = slot_ms / 1000
                    i += 1 # Skip next

            est = estimate_seconds(temp_text)
            if est > slot_sec:
                 rate = min(int((est/slot_sec - 1) * 100), int((MAX_SPEED - 1) * 100))

        # Generate
        seg = await generate_tts(temp_text, voice, rate, cache)
        final_audio += seg
        cursor += len(seg)

        # Adaptive Pause
        if i+1 < total_subs:
            next_start = srt_time_to_ms(subs[i+1].start)
            if next_start - cursor > 0 and next_start - cursor <= MAX_BREATH_MS:
                final_audio += AudioSegment.silent(next_start - cursor)
                cursor += (next_start - cursor)
        
        i += 1

    await update_status(status_msg, "💾 **Rendering Final MP3...**")
    final_audio.export(output_file, format="mp3")

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["voice"] = DEFAULT_VOICE
    
    # Beautiful Inline Keyboard
    keyboard = [
        [InlineKeyboardButton("🎤 Change Voice", callback_data="cmd_voice"),
         InlineKeyboardButton("📝 Text Mode", callback_data="cmd_srtsms")],
        [InlineKeyboardButton("ℹ️ Help / How-To", callback_data="cmd_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👋 **Welcome to AI Dubbing Bot!**\n\n"
        "I can turn subtitles into human-like narration.\n"
        "Just **send me an .srt file** to begin.\n\n"
        f"Current Voice: `{DEFAULT_VOICE}`",
        reply_markup=reply_markup,
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 **Bot Guide**\n\n"
        "1️⃣ **SRT to Audio:** Upload any `.srt` file. I will sync the voice to the timestamps.\n"
        "2️⃣ **Text to Speech:** Just type any text to get audio.\n"
        "3️⃣ **Copy-Paste SRT:** Click 'Text Mode', then paste your SRT content directly."
    )
    # If called from button
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

# ================= VOICE UI =================
async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Create 2 columns of buttons
    keys = list(VOICE_CATALOG.keys())
    buttons = []
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(keys[i], callback_data=f"set_{keys[i]}")]
        if i + 1 < len(keys):
            row.append(InlineKeyboardButton(keys[i+1], callback_data=f"set_{keys[i+1]}"))
        buttons.append(row)
    
    await update.message.reply_text("🗣 **Select a Narrator:**", reply_markup=InlineKeyboardMarkup(buttons))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "cmd_voice":
        await query.answer()
        await voice_command(query, context)
        return
    if data == "cmd_srtsms":
        await query.answer()
        await srt_text_toggle(query, context)
        return
    if data == "cmd_help":
        await help_menu(update, context)
        return

    if data.startswith("set_"):
        key = data.replace("set_", "")
        if key in VOICE_CATALOG:
            context.user_data["voice"] = VOICE_CATALOG[key]
            await query.answer(f"Voice set to {key}")
            await query.edit_message_text(f"✅ **Voice Updated!**\n\nNow using: `{key}`", parse_mode="Markdown")

async def srt_text_toggle(update_obj, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("srt_text_mode", False)
    context.user_data["srt_text_mode"] = not mode
    status = "ON ✅" if not mode else "OFF ❌"
    
    msg = f"📝 **SRT Copy-Paste Mode: {status}**\n\nWhen ON, you can paste SRT text directly into the chat."
    
    if isinstance(update_obj, Update):
        await update_obj.message.reply_text(msg, parse_mode="Markdown")
    else:
        # Called from callback
        await update_obj.message.reply_text(msg, parse_mode="Markdown")

# ================= HANDLERS =================
async def handle_srt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⬇️ **Downloading file...**", parse_mode="Markdown")
    
    try:
        srt_path = f"temp_{update.message.from_user.id}.srt"
        out_path = f"dub_{update.message.from_user.id}.mp3"
        voice = context.user_data.get("voice", DEFAULT_VOICE)

        file = await update.message.document.get_file()
        await file.download_to_drive(srt_path)

        await update_status(status_msg, "⚙️ **Initializing AI Engine...**")
        
        # Run conversion
        await srt_to_audio(srt_path, out_path, voice, status_msg)

        await update_status(status_msg, "⬆️ **Uploading MP3...**")
        await update.message.reply_audio(
            audio=open(out_path, "rb"),
            caption=f"✅ **Done!**\n🗣 Voice: `{voice}`",
            parse_mode="Markdown"
        )
        # Cleanup
        os.remove(srt_path)
        os.remove(out_path)
        await status_msg.delete()

    except Exception as e:
        await update_status(status_msg, f"❌ **Error:**\n`{str(e)}`")
        logging.error(e)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    
    # Check for SRT Mode
    if context.user_data.get("srt_text_mode", False) and "-->" in text:
        status_msg = await update.message.reply_text("📝 **Reading SRT Text...**", parse_mode="Markdown")
        srt_path = f"temp_text_{update.message.from_user.id}.srt"
        out_path = f"dub_text_{update.message.from_user.id}.mp3"
        
        with open(srt_path, "w", encoding="utf-8") as f: f.write(text)
        
        try:
            await srt_to_audio(srt_path, out_path, voice, status_msg)
            await update_status(status_msg, "⬆️ **Uploading...**")
            await update.message.reply_audio(audio=open(out_path, "rb"), caption="✅ **SRT Text Dubbed**")
            os.remove(srt_path)
            os.remove(out_path)
            await status_msg.delete()
        except Exception as e:
            await update_status(status_msg, f"❌ Error: {str(e)}")
        return

    # Normal TTS
    status_msg = await update.message.reply_text(f"🗣 **Generating Audio...**\n`{voice}`", parse_mode="Markdown")
    out = f"tts_{update.message.from_user.id}.mp3"
    await edge_tts.Communicate(preprocess_text(text), voice).save(out)
    await update.message.reply_audio(audio=open(out, "rb"))
    os.remove(out)
    await status_msg.delete()

# ================= MAIN =================
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CommandHandler("srtsms", srt_text_toggle))
    app.add_handler(CommandHandler("help", help_menu))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_srt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    threading.Thread(target=run_web, daemon=True).start()
    print("🚀 Bot with UI upgrades running...")
    app.run_polling()

if __name__=="__main__":
    main()
