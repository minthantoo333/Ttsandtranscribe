import os, logging, threading, tempfile, asyncio, time, math
from http.server import HTTPServer, BaseHTTPRequestHandler

import edge_tts
import pysrt
from pydub import AudioSegment, effects

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ================= CONFIG =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

# Settings
DEFAULT_VOICE = "my-MM-ThihaNeural"
RETRY_COUNT = 3
RETRY_DELAY = 1

# ================= LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= 🌏 EXPANDED VOICE CATALOG =================
# Updated with Remy, Giuseppe, Brian, Andrew + Top Multilingual Choices
VOICE_CATALOG = {
    # --- MYANMAR ---
    "🇲🇲 MM Male (Thiha)": "my-MM-ThihaNeural",
    "🇲🇲 MM Female (Nilar)": "my-MM-NilarNeural",
    
    # --- ENGLISH (US) ---
    "🇺🇸 US Male (Andrew)": "en-US-AndrewNeural",  # Requested
    "🇺🇸 US Male (Brian)": "en-US-BrianNeural",    # Requested
    "🇺🇸 US Male (Guy)": "en-US-GuyNeural",
    "🇺🇸 US Female (Jenny)": "en-US-JennyNeural",
    "🇺🇸 US Female (Ana)": "en-US-AnaNeural",

    # --- ENGLISH (UK/AU) ---
    "🇬🇧 UK Male (Ryan)": "en-GB-RyanNeural",
    "🇬🇧 UK Female (Libby)": "en-GB-LibbyNeural",
    "🇬🇧 UK Female (Sonia)": "en-GB-SoniaNeural",
    "🇦🇺 AU Female (Natasha)": "en-AU-NatashaNeural",

    # --- EUROPEAN ---
    "🇫🇷 French (Remy)": "fr-FR-RemyMultilingualNeural", # Requested
    "🇫🇷 French (Vivienne)": "fr-FR-VivienneMultilingualNeural",
    "🇮🇹 Italian (Giuseppe)": "it-IT-GiuseppeNeural",    # Requested
    "🇮🇹 Italian (Elsa)": "it-IT-ElsaNeural",
    "🇩🇪 German (Conrad)": "de-DE-ConradNeural",
    "🇩🇪 German (Katja)": "de-DE-KatjaNeural",
    "🇪🇸 Spanish (Alvaro)": "es-ES-AlvaroNeural",
    "🇪🇸 Spanish (Elvira)": "es-ES-ElviraNeural",
    "🇷🇺 Russian (Dmitry)": "ru-RU-DmitryNeural",

    # --- ASIAN ---
    "🇯🇵 Japanese (Keita)": "ja-JP-KeitaNeural",
    "🇯🇵 Japanese (Nanami)": "ja-JP-NanamiNeural",
    "🇰🇷 Korean (InJoon)": "ko-KR-InJoonNeural",
    "🇰🇷 Korean (SunHi)": "ko-KR-SunHiNeural",
    "🇨🇳 Chinese (Yunxi)": "zh-CN-YunxiNeural",
    "🇨🇳 Chinese (Xiaoxiao)": "zh-CN-XiaoxiaoNeural",
    "🇹🇭 Thai (Niwat)": "th-TH-NiwatNeural",
    "🇹🇭 Thai (Premwadee)": "th-TH-PremwadeeNeural",
    "🇻🇳 Viet (NamMinh)": "vi-VN-NamMinhNeural",
    "🇮🇩 Indo (Ardi)": "id-ID-ArdiNeural",
}

# ================= KEEP ALIVE =================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Alive")

def run_web():
    HTTPServer(("0.0.0.0", PORT), SimpleHandler).serve_forever()

# ================= UTILS & UI =================
def create_progress_bar(current, total, length=10):
    percent = current / total
    filled_length = int(length * percent)
    bar = "█" * filled_length + "░" * (length - filled_length)
    return f"{bar} {int(percent * 100)}%"

async def update_status(message, text):
    try:
        if message.text != text:
            await message.edit_text(text)
    except Exception:
        pass

# ================= 🧠 PROFESSIONAL SYNC ENGINE =================

def srt_time_to_ms(t):
    return (t.hours*3600 + t.minutes*60 + t.seconds)*1000 + t.milliseconds

async def generate_tts(text, voice, rate_str, cache):
    """Generates raw audio segment"""
    key = (text, voice, rate_str)
    if key in cache: return cache[key]

    for attempt in range(RETRY_COUNT):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                temp_path = tmp.name
            
            # Communicate with EdgeTTS
            await edge_tts.Communicate(text=text, voice=voice, rate=rate_str).save(temp_path)
            
            audio = AudioSegment.from_file(temp_path)
            os.remove(temp_path)
            cache[key] = audio
            return audio
        except Exception as e:
            logging.warning(f"TTS Retry {attempt}: {e}")
            await asyncio.sleep(RETRY_DELAY)
    
    return AudioSegment.silent(duration=500) # Fallback

def fit_audio_to_slot(audio_seg, max_duration_ms):
    """
    PROFESSIONAL SYNC:
    Compresses audio to fit perfectly into the slot if it's too long.
    """
    current_dur = len(audio_seg)
    if current_dur <= max_duration_ms:
        return audio_seg
    
    # Calculate ratio needed
    ratio = current_dur / max_duration_ms
    
    # Cap compression at 2.0x (otherwise it sounds like chipmunks/broken)
    if ratio > 2.0:
        ratio = 2.0
        
    try:
        # High quality time-compression
        compressed = effects.speedup(audio_seg, playback_speed=ratio, chunk_size=50, crossfade=25)
        if len(compressed) > max_duration_ms:
             return compressed[:max_duration_ms] # Trim excess if still too long
        return compressed
    except Exception as e:
        logging.error(f"Compression failed: {e}")
        return audio_seg[:max_duration_ms] # Panic fallback

def preprocess_text(text):
    # Remove HTML and special chars
    clean = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    clean = clean.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
    return clean.replace("။", "။\n").replace(".", ".\n").strip()

async def srt_to_audio(srt_file, output_file, voice, status_msg):
    subs = pysrt.open(srt_file)
    total_subs = len(subs)
    
    final_audio = AudioSegment.silent(0)
    current_timeline_pos = 0
    cache = {}
    last_ui_update = 0

    for i, sub in enumerate(subs):
        # --- UI Update (Every 3s) ---
        now = time.time()
        if now - last_ui_update > 3:
            bar = create_progress_bar(i, total_subs)
            await update_status(status_msg, f"⚙️ **Syncing Audio...**\n{bar}\nLine: {i+1}/{total_subs}")
            last_ui_update = now

        text = preprocess_text(sub.text)
        if not text: continue

        start_ms = srt_time_to_ms(sub.start)
        end_ms = srt_time_to_ms(sub.end)
        
        # 1. Timeline Management (Fill gaps with silence)
        if start_ms > current_timeline_pos:
            silence_gap = start_ms - current_timeline_pos
            final_audio += AudioSegment.silent(duration=silence_gap)
            current_timeline_pos = start_ms
        
        # 2. Calculate Strict Slot
        slot_duration = end_ms - start_ms
        if i + 1 < len(subs):
            next_start = srt_time_to_ms(subs[i+1].start)
            if next_start < end_ms: # Overlapping subtitles?
                slot_duration = next_start - start_ms

        if slot_duration <= 0: continue

        # 3. Smart Rate Calculation
        # Check density of text vs time
        char_count = len(text)
        chars_per_sec = char_count / (slot_duration / 1000)
        
        rate_str = "+0%"
        if chars_per_sec > 15: rate_str = "+20%"
        if chars_per_sec > 20: rate_str = "+40%"
        if chars_per_sec > 25: rate_str = "+60%"

        raw_audio = await generate_tts(text, voice, rate_str, cache)

        # 4. Fit to Slot (Compression)
        fitted_audio = fit_audio_to_slot(raw_audio, slot_duration)
        
        final_audio += fitted_audio
        current_timeline_pos += len(fitted_audio)

    await update_status(status_msg, "💾 **Rendering Final MP3...**")
    final_audio.export(output_file, format="mp3")

# ================= MENU HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["voice"] = DEFAULT_VOICE
    
    keyboard = [
        [InlineKeyboardButton("🎤 Select Voice", callback_data="page_0")],
        [InlineKeyboardButton("📝 Copy-Paste SRT Mode", callback_data="cmd_srtsms")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="cmd_help")]
    ]
    
    await update.message.reply_text(
        "🎧 **Multilingual Dubbing Bot**\n\n"
        "I can sync subtitles with professional timing in **20+ languages**.\n"
        "Send me an `.srt` file to start!\n\n"
        f"🗣 Current Voice: `{DEFAULT_VOICE}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ================= PAGINATED VOICE MENU =================
ITEMS_PER_PAGE = 10 # Increased since we have more voices now

async def show_voice_page(update, page_num):
    voice_keys = list(VOICE_CATALOG.keys())
    total_pages = math.ceil(len(voice_keys) / ITEMS_PER_PAGE)
    
    start = page_num * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    current_items = voice_keys[start:end]
    
    buttons = []
    # Voice Buttons (2 columns)
    for i in range(0, len(current_items), 2):
        row = []
        name1 = current_items[i]
        row.append(InlineKeyboardButton(name1, callback_data=f"set_{name1}"))
        if i + 1 < len(current_items):
            name2 = current_items[i+1]
            row.append(InlineKeyboardButton(name2, callback_data=f"set_{name2}"))
        buttons.append(row)
    
    # Navigation
    nav_row = []
    if page_num > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"page_{page_num-1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page_num+1}/{total_pages}", callback_data="noop"))
    if page_num < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page_num+1}"))
    
    buttons.append(nav_row)
    
    markup = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        await update.callback_query.edit_message_text("🗣 **Select a Voice:**", reply_markup=markup)
    else:
        await update.message.reply_text("🗣 **Select a Voice:**", reply_markup=markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("page_"):
        await query.answer()
        page = int(data.split("_")[1])
        await show_voice_page(update, page)
        return

    if data.startswith("set_"):
        key = data.replace("set_", "")
        if key in VOICE_CATALOG:
            context.user_data["voice"] = VOICE_CATALOG[key]
            await query.answer(f"Selected: {key}")
            await query.edit_message_text(f"✅ **Voice Set!**\n\nNow using: `{key}`\n\nSend your SRT file now.", parse_mode="Markdown")
        return

    if data == "cmd_srtsms":
        await query.answer()
        context.user_data["srt_text_mode"] = True
        await query.edit_message_text("📝 **Text Mode Active**\n\nPaste your subtitle text (with timestamps) here.")
        return

    if data == "noop":
        await query.answer()

# ================= FILE HANDLER =================
async def handle_srt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⬇️ **Downloading...**")
    
    try:
        srt_path = f"temp_{update.message.from_user.id}.srt"
        out_path = f"dub_{update.message.from_user.id}.mp3"
        voice = context.user_data.get("voice", DEFAULT_VOICE)

        file = await update.message.document.get_file()
        await file.download_to_drive(srt_path)

        await update_status(status_msg, "⚙️ **Analyzing Timeline...**")
        await srt_to_audio(srt_path, out_path, voice, status_msg)

        await update_status(status_msg, "⬆️ **Uploading...**")
        await update.message.reply_audio(
            audio=open(out_path, "rb"),
            caption=f"✅ **Synced Audio**\n🗣 `{voice}`",
            parse_mode="Markdown"
        )
        os.remove(srt_path)
        os.remove(out_path)
        await status_msg.delete()

    except Exception as e:
        await update_status(status_msg, f"❌ Error: {e}")
        logging.error(e)

# ================= TEXT HANDLER =================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    
    if context.user_data.get("srt_text_mode", False) and "-->" in text:
        # SRT Copy Paste
        status_msg = await update.message.reply_text("📝 **Processing Text SRT...**")
        srt_path = f"temp_text_{update.message.from_user.id}.srt"
        out_path = f"dub_text_{update.message.from_user.id}.mp3"
        with open(srt_path, "w", encoding="utf-8") as f: f.write(text)
        
        try:
            await srt_to_audio(srt_path, out_path, voice, status_msg)
            await update.message.reply_audio(audio=open(out_path, "rb"), caption="✅ **SRT Text Dubbed**")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        finally:
            if os.path.exists(srt_path): os.remove(srt_path)
            if os.path.exists(out_path): os.remove(out_path)
            await status_msg.delete()
    else:
        # Simple TTS
        out = f"tts_{update.message.from_user.id}.mp3"
        await edge_tts.Communicate(text, voice).save(out)
        await update.message.reply_audio(audio=open(out, "rb"))
        os.remove(out)

# ================= MAIN =================
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_srt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    threading.Thread(target=run_web, daemon=True).start()
    print("🤖 Multilingual Bot Running...")
    app.run_polling()

if __name__=="__main__":
    main()
