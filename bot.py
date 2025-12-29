# ========================================================
# NEXT-LEVEL Telegram Bot:
# TTS + SSML + SRT → Audio (Adaptive Breathing, Retry, Caching)
# ========================================================

import os, logging, threading, tempfile, asyncio, time
from http.server import HTTPServer, BaseHTTPRequestHandler

import edge_tts
import pysrt
from pydub import AudioSegment

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ================= CONFIG =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
DEFAULT_VOICE = "my-MM-ThihaNeural"
MAX_SPEED = 1.5
MIN_BREATH_MS = 300
MAX_BREATH_MS = 800
RETRY_COUNT = 3
RETRY_DELAY = 1  # seconds

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= VOICE CATALOG =================
VOICE_CATALOG = {
    "my-thiha": "my-MM-ThihaNeural",
    "my-nilar": "my-MM-NilarNeural",
    "en-jenny": "en-US-JennyNeural",
    "en-guy": "en-US-GuyNeural",
    "en-aria": "en-US-AriaNeural",
    "en-ryan": "en-US-RyanNeural",
    "en-davis": "en-US-DavisNeural",
    "uk-libby": "en-GB-LibbyNeural",
    "uk-ryan": "en-GB-RyanNeural",
    "jp-nanami": "ja-JP-NanamiNeural",
    "jp-keita": "ja-JP-KeitaNeural",
    "kr-sunhi": "ko-KR-SunHiNeural",
    "kr-injoon": "ko-KR-InJoonNeural",
    "zh-xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "zh-yunxi": "zh-CN-YunxiNeural",
    "hi-swara": "hi-IN-SwaraNeural",
    "hi-madhur": "hi-IN-MadhurNeural",
    "fr-denise": "fr-FR-DeniseNeural",
    "fr-henri": "fr-FR-HenriNeural",
}

# ================= KEEP ALIVE =================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Alive")

def run_web():
    HTTPServer(("0.0.0.0", PORT), SimpleHandler).serve_forever()

# ================= UTILS =================
def srt_time_to_ms(t):
    return (t.hours*3600 + t.minutes*60 + t.seconds)*1000 + t.milliseconds

def estimate_seconds(text):
    return max(0.4, len(text)/14)

def preprocess_text(text):
    return text.replace("။","။\n").replace(".",".\n").strip()

def calculate_breath_ms(text):
    """Adaptive breathing: longer sentences = slightly longer pause"""
    base = MIN_BREATH_MS
    extra = min(len(text)//30*100, MAX_BREATH_MS - MIN_BREATH_MS)
    return base + extra

async def generate_tts(text, voice, rate=0, cache={}):
    """TTS with retry and caching"""
    key = (text, voice, rate)
    if key in cache:
        return cache[key]

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
    raise RuntimeError("TTS failed after retries")

# ================= SRT → AUDIO =================
async def srt_to_audio(srt_file, output_file, voice):
    subs = pysrt.open(srt_file)
    final_audio = AudioSegment.silent(0)
    cursor = 0
    i = 0
    cache = {}  # cache for TTS segments

    while i < len(subs):
        sub = subs[i]
        start_ms = srt_time_to_ms(sub.start)
        end_ms = srt_time_to_ms(sub.end)
        slot_ms = end_ms - start_ms
        slot_sec = slot_ms / 1000
        text = preprocess_text(sub.text)
        if not text:
            i += 1
            continue

        # Add gap before
        if start_ms > cursor:
            gap = min(start_ms - cursor, calculate_breath_ms(text))
            final_audio += AudioSegment.silent(gap)
            cursor += gap

        est = estimate_seconds(text)

        # -------- CASE HANDLING --------
        if est <= slot_sec:
            rate = 0
            temp_text = text
        elif est <= slot_sec * MAX_SPEED:
            rate = min(int((est/slot_sec - 1) * 100), int((MAX_SPEED - 1) * 100))
            temp_text = text
        else:
            # Too long → merge next subtitle if small gap
            rate = 0
            next_i = i + 1
            merged_text = text
            merged_slot_ms = slot_ms

            if next_i < len(subs):
                next_sub = subs[next_i]
                gap = srt_time_to_ms(next_sub.start) - end_ms
                if gap <= 500:
                    merged_text += " " + preprocess_text(next_sub.text)
                    merged_slot_ms += srt_time_to_ms(next_sub.end) - srt_time_to_ms(next_sub.start) + gap
                    i += 1

            temp_text = merged_text
            slot_ms = merged_slot_ms
            slot_sec = slot_ms / 1000
            est = estimate_seconds(temp_text)

            # Still too long → split at punctuation
            if est > slot_sec * MAX_SPEED:
                puncts = [".", "!", "?", "၊", ","]
                for p in puncts:
                    if p in temp_text:
                        parts = temp_text.split(p)
                        first_part = parts[0] + p
                        remaining = p.join(parts[1:]).strip()
                        temp_text = first_part
                        if remaining:
                            new_sub = pysrt.SubRipItem(
                                index=sub.index+1,
                                start=sub.end,
                                end=sub.end,
                                text=remaining
                            )
                            subs.insert(i+1, new_sub)
                        break

        # Generate TTS
        seg = await generate_tts(temp_text, voice, rate, cache)
        final_audio += seg
        cursor += len(seg)

        # Add adaptive breathing before next subtitle
        if i+1 < len(subs):
            next_start_ms = srt_time_to_ms(subs[i+1].start)
            gap_ms = next_start_ms - cursor
            if 0 < gap_ms <= MAX_BREATH_MS:
                final_audio += AudioSegment.silent(gap_ms)
                cursor += gap_ms

        i += 1

    final_audio.export(output_file, format="mp3")

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["voice"] = DEFAULT_VOICE
    context.user_data["mode"] = "tts"
    context.user_data["srt_text_mode"] = False
    await update.message.reply_text(
        "👋 Bot ready!\n\n"
        "🗣 Send text → TTS\n"
        "🧠 /ssml → SSML mode\n"
        "🎥 Upload .srt → Audio\n"
        "📝 /srtsms → Toggle SRT text mode\n"
        "🎤 /voice → select voice"
    )

async def ssml_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"]="ssml"
    await update.message.reply_text("🧠 SSML MODE ON\nSend SSML markup now.")

# ================= VOICE SELECTION INLINE =================
async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [[InlineKeyboardButton(k, callback_data=k)] for k in VOICE_CATALOG]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Select voice:", reply_markup=keyboard)

async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data
    if key in VOICE_CATALOG:
        context.user_data["voice"] = VOICE_CATALOG[key]
        await query.edit_message_text(f"✅ Voice set to `{VOICE_CATALOG[key]}`", parse_mode="Markdown")

async def srt_text_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("srt_text_mode", False)
    context.user_data["srt_text_mode"] = not mode
    status = "ON ✅" if not mode else "OFF ❌"
    await update.message.reply_text(f"📝 SRT Text Mode: {status}")

# ================= MESSAGE HANDLERS =================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    mode = context.user_data.get("mode", "tts")
    srt_mode = context.user_data.get("srt_text_mode", False)
    text = update.message.text.strip()
    
    if srt_mode and any("-->" in line for line in text.splitlines()):
        await update.message.reply_text("🎬 Processing SRT text...")
        srt_path = f"srt_{update.message.from_user.id}.srt"
        out = f"srt_{update.message.from_user.id}.mp3"
        with open(srt_path, "w", encoding="utf-8") as f: f.write(text)
        await srt_to_audio(srt_path, out, voice)
        await update.message.reply_audio(audio=open(out, "rb"), caption="✅ SRT Text → Audio")
        os.remove(srt_path)
        os.remove(out)
        return

    out = f"tts_{update.message.from_user.id}.mp3"
    if mode == "ssml":
        await edge_tts.Communicate(text, voice, ssml=True).save(out)
        context.user_data["mode"] = "tts"
    else:
        await edge_tts.Communicate(preprocess_text(text), voice).save(out)
    await update.message.reply_audio(audio=open(out, "rb"))
    os.remove(out)

async def handle_srt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Processing SRT file...")
    srt_path = f"srt_{update.message.from_user.id}.srt"
    out = f"srt_{update.message.from_user.id}.mp3"
    await update.message.document.get_file().download_to_drive(srt_path)
    await srt_to_audio(srt_path, out, context.user_data.get("voice", DEFAULT_VOICE))
    await update.message.reply_audio(audio=open(out, "rb"), caption="✅ SRT → Audio")
    os.remove(srt_path)
    os.remove(out)

# ================= MAIN =================
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ssml", ssml_mode))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CallbackQueryHandler(voice_callback))
    app.add_handler(CommandHandler("srtsms", srt_text_mode))
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_srt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    threading.Thread(target=run_web, daemon=True).start()
    print("🤖 Next-level Bot running...")
    app.run_polling()

if __name__=="__main__":
    main()