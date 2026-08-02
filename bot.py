import logging
import os
import asyncio
import glob
import subprocess
import torch
import pysrt
import math
import shutil
import re
import time
import sys
import threading
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler

# -------------------------------------------------------------------------
# LOGGING SETUP
# -------------------------------------------------------------------------
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

if shutil.which("ffmpeg") is None:
    logger.error("❌ FFmpeg is NOT installed. Audio processing will fail.")

try:
    from pydub import AudioSegment, effects
    from pydub.silence import detect_leading_silence
    import edge_tts
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
    import google.generativeai as genai 
    from faster_whisper import WhisperModel
except ImportError as e:
    logger.critical(f"❌ Missing Library: {e}")
    sys.exit(1)

# -------------------------------------------------------------------------
# DUMMY SERVER (To keep bot alive)
# -------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running! - Pro Video AI")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# -------------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------------
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if not TG_TOKEN:
    logger.critical("❌ ERROR: TELEGRAM_TOKEN missing.")
    sys.exit(1)

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"
logger.info(f"🚀 AI Engine running on: {DEVICE.upper()} (Compute: {COMPUTE_TYPE})")

WHISPER_LIB = {"⚡ Tiny (Render Safe)": "tiny", "⚖️ Base": "base", "🎯 Small": "small"}
VOICE_LIB = {"🇲🇲 Thiha (Male)": "my-MM-ThihaNeural", "🇲🇲 Nilar (Female)": "my-MM-NilarNeural", "🇬🇧 Sonia (Female)": "en-GB-SoniaNeural"}
SPEED_LIB = {"🤖 Auto-Sync": "auto", "1.0x (Normal)": 1.0}
RATE_LIB = {"🐢 -10%": "-10%", "🚶 +0%": "+0%", "🏃 +10%": "+10%", "🏎️ +20%": "+20%"}

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS: os.makedirs(f, exist_ok=True)

# -------------------------------------------------------------------------
# STATE MANAGEMENT
# -------------------------------------------------------------------------
user_prefs = {}

def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "dub_voice": "my-MM-ThihaNeural",
            "video_speed": "auto", 
            "base_rate": "+20%",
            "max_fit": 70,
            "whisper_model": "tiny", 
            "burn_subs": True,      # Subtitle ဖွင့်/ပိတ်
            "cover_subs": False     # မူလ Subtitle အဟောင်းကို Blur (Box) ဖြင့် ဖုံးရန်
        }
    return user_prefs[user_id]

def get_paths(user_id):
    return {
        "input": f"downloads/{user_id}_input.mp4",
        "audio": f"downloads/{user_id}_audio.mp3",
        "srt": f"downloads/{user_id}_subs.srt",
        "trans_result": f"downloads/{user_id}_translated.srt",
        "dub_audio": f"downloads/{user_id}_dubbed.mp3",
        "final_video": f"downloads/{user_id}_final.mp4",
        "compressed": f"downloads/{user_id}_compressed.mp4"
    }

def clean_temp_audio(user_id):
    for f in glob.glob(f"temp/{user_id}_*"):
        try: os.remove(f)
        except: pass

def wipe_all_user_data(user_id):
    clean_temp_audio(user_id)
    p = get_paths(user_id)
    for path in p.values():
        if os.path.exists(path):
            try: os.remove(path)
            except: pass
    for f in glob.glob(f"downloads/{user_id}_video*"):
        try: os.remove(f)
        except: pass

async def run_async_cmd(*cmd):
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        error_msg = stderr.decode('utf-8', errors='ignore')
        logger.error(f"Command failed: {' '.join(cmd)}\n{error_msg}")
        raise Exception(error_msg)
    return stdout.decode('utf-8', errors='ignore')

# -------------------------------------------------------------------------
# DUBBING LOGIC (Minified for space - keeps logic same as before)
# -------------------------------------------------------------------------
def trim_silence(audio_segment, silence_thresh=-40.0, chunk_size=5):
    if len(audio_segment) < 100: return audio_segment
    start_trim = detect_leading_silence(audio_segment, silence_threshold=silence_thresh, chunk_size=chunk_size)
    end_trim = detect_leading_silence(audio_segment.reverse(), silence_threshold=silence_thresh, chunk_size=chunk_size)
    return audio_segment[start_trim:len(audio_segment)-end_trim]

def process_length_and_trim(file_path):
    seg = AudioSegment.from_file(file_path)
    seg = trim_silence(seg)
    seg.export(file_path, format="mp3")
    return len(seg)

def compose_final_audio(chunks_data, duration_ms, output_path):
    final_audio = AudioSegment.silent(duration=duration_ms + 2000) 
    for start_ms, file_path in chunks_data:
        if not file_path: continue
        try:
            segment = effects.normalize(AudioSegment.from_file(file_path).high_pass_filter(150))
            if start_ms + len(segment) > len(final_audio):
                final_audio += AudioSegment.silent(duration=(start_ms + len(segment) - len(final_audio) + 2000))
            final_audio = final_audio.overlay(segment, position=start_ms)
        except: pass
    final_audio.export(output_path, format="mp3", bitrate="192k")

async def generate_advanced_dubbing(user_id, srt_path, output_path, state, status_msg):
    try:
        subs = pysrt.open(srt_path)
        chunks_data = []
        for i, sub in enumerate(subs):
            start_ms = sub.start.ordinal
            end_ms = sub.end.ordinal
            next_start_ms = subs[i+1].start.ordinal if i + 1 < len(subs) else end_ms + 2000 
            text = sub.text.replace("\n", " ").strip()
            chunks_data.append({"index": i, "start": start_ms, "end": end_ms, "gap": next_start_ms - start_ms, "text": text, "voice": state['dub_voice']})

        base_rate, max_fit = int(state['base_rate'].replace("%", "").replace("+", "").replace("-", "")), state['max_fit']
        semaphore = asyncio.Semaphore(10)
        
        # Audio generation logic...
        async def process_chunk(chunk):
            async with semaphore:
                text, voice, idx = chunk["text"], chunk["voice"], chunk["index"]
                if not text: return None
                path = f"temp/{user_id}_{hashlib.md5((text+voice).encode()).hexdigest()}.mp3"
                if not os.path.exists(path):
                    await edge_tts.Communicate(text, voice, rate=f"+{base_rate}%").save(path)
                await asyncio.to_thread(process_length_and_trim, path)
                return {"index": idx, "start": chunk["start"], "path": path}
                
        res = await asyncio.gather(*(process_chunk(c) for c in chunks_data))
        valid_res = [(c["start"], c["path"]) for c in res if c]
        last_sub_end = subs[-1].end.ordinal
        await asyncio.to_thread(compose_final_audio, valid_res, last_sub_end, output_path)
        
        return True, None
    except Exception as e:
        return False, str(e)

# -------------------------------------------------------------------------
# FFMPEG MUXING WITH NEW SETTINGS
# -------------------------------------------------------------------------
async def merge_video_audio(user_id, state):
    p = get_paths(user_id)
    if not os.path.exists(p['input']) or not os.path.exists(p['dub_audio']): return False, "Media missing."

    vf_filters = ["hflip", "eq=brightness=0.05:saturation=1.1"]

    # 🔲 မူလ Subtitle များကို အမည်းရောင် Translucent box ဖြင့်ဖုံးရန် (Blur)
    if state['cover_subs']:
        # ih*0.15 = အောက်ခြေ 15% အပိုင်းကို အမည်းရောင် 70% opacity နဲ့အုပ်ပါမည်
        vf_filters.append("drawbox=y=ih-ih*0.15:color=black@0.7:width=iw:height=ih*0.15:t=fill")

    # 📝 Subtitle (စာတန်းထိုး) အသစ်ထည့်ရန် (Toggle ON ဖြစ်နေမှသာ)
    if state['burn_subs']:
        escaped_srt = p['srt'].replace("\\", "/").replace(":", "\\:")
        # ⚠️ Subtitle font ကြီးကြီးလိုချင်ပါက :force_style='Fontsize=22' ကို ထပ်ပေါင်းထည့်နိုင်ပါတယ်
        vf_filters.append(f"subtitles='{escaped_srt}'")

    vf_str = ",".join(vf_filters)

    cmd = [
        "ffmpeg", "-y", "-i", p['input'], "-i", p['dub_audio'],
        "-vf", vf_str,  
        "-c:v", "libx264", "-preset", "fast", "-crf", "26", # Quality Standard
        "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1",  
        p['final_video']
    ]
    try:
        await run_async_cmd(*cmd)
        return True, p['final_video']
    except Exception as e:
        return False, str(e)

# -------------------------------------------------------------------------
# KEYBOARDS & MENUS
# -------------------------------------------------------------------------
def get_settings_keyboard(state):
    subs_status = "🟢 ON" if state['burn_subs'] else "🔴 OFF"
    cover_status = "🟢 ON" if state['cover_subs'] else "🔴 OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗣️ Voice: {state['dub_voice']}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"📝 Subtitles Burn-in: {subs_status}", callback_data="toggle_subs")],
        [InlineKeyboardButton(f"🔲 Cover Old Subs: {cover_status}", callback_data="toggle_cover")],
        [InlineKeyboardButton("🧹 Clear Workspace", callback_data="cmd_clear")]
    ])

def get_translation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇲🇲 Burmese", callback_data="trans_burmese"), InlineKeyboardButton("🇬🇧 English", callback_data="trans_english")],
        [InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_user_state(update.effective_user.id)
    text = "⚙️ **STUDIO SETTINGS**\nConfigure your Video output settings here."
    if update.callback_query: await update.callback_query.message.edit_text(text, reply_markup=get_settings_keyboard(state))
    else: await update.message.reply_text(text, reply_markup=get_settings_keyboard(state))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data
    p = get_paths(user_id)

    if data == "cmd_start": await start(update, context)
    elif data == "cmd_clear":
        wipe_all_user_data(user_id)
        await query.message.edit_text("🧹 All videos, audio, and cache cleared.")
    
    # Toggles
    elif data == "toggle_subs":
        state['burn_subs'] = not state['burn_subs']
        await start(update, context)
    elif data == "toggle_cover":
        state['cover_subs'] = not state['cover_subs']
        await start(update, context)

    # 🎬 DUBBING PROCESS & 50MB SIZE CHECK
    elif data == "trigger_dub":
        if not os.path.exists(p['srt']): return await query.message.reply_text("❌ No SRT found.")
        
        status = await query.message.reply_text("🚀 Audio Generating...")
        success, error = await generate_advanced_dubbing(user_id, p['srt'], p['dub_audio'], state, status)
        
        if success and os.path.exists(p['input']):
            await status.edit_text("🎥 Merging Video & Audio (Processing Effects & Subtitles)...")
            merge_success, merge_res = await merge_video_audio(user_id, state)
            
            if merge_success:
                # 🗜️ Check File Size
                size_mb = os.path.getsize(merge_res) / (1024 * 1024)
                if size_mb > 49.5:
                    kb = [
                        [InlineKeyboardButton("🗜️ Compress Video", callback_data="do_compress")],
                        [InlineKeyboardButton("🎧 Send Audio Only", callback_data="send_audio_only")],
                        [InlineKeyboardButton("🧹 Cancel & Clear", callback_data="cmd_clear")]
                    ]
                    await status.edit_text(
                        f"⚠️ **ဗီဒီယို File Size က {size_mb:.1f} MB ရှိနေပါတယ်။**\nTelegram က 50MB အထိပဲ လက်ခံလို့ ဘယ်လိုဆက်လုပ်မလဲ ရွေးပေးပါ။", 
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                else:
                    await context.bot.send_video(query.message.chat_id, open(merge_res, "rb"), caption="🎬 Final Dubbed Video")
                    await status.delete()
                    wipe_all_user_data(user_id)
            else:
                await status.edit_text("❌ Merge Failed.")
        else:
            await status.edit_text(f"❌ Error: {error}")

    # 🗜️ USER CLICKED COMPRESS
    elif data == "do_compress":
        status = await query.message.edit_text("⏳ ဗီဒီယိုကို Size သေးအောင် ချုံ့နေပါတယ်။ (အချိန် အနည်းငယ်ကြာနိုင်ပါသည်)...")
        comp_cmd = ["ffmpeg", "-y", "-i", p['final_video'], "-c:v", "libx264", "-crf", "32", "-preset", "veryfast", "-c:a", "aac", p['compressed']]
        try:
            await run_async_cmd(*comp_cmd)
            comp_size = os.path.getsize(p['compressed']) / (1024 * 1024)
            
            if comp_size > 49.5:
                await status.edit_text("❌ Compress လုပ်တာတောင် 50MB ကျော်နေသေးလို့ Audio ပဲ ပို့ပေးလိုက်ပါမယ်။")
                await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="🎧 Audio Only")
            else:
                await context.bot.send_video(query.message.chat_id, open(p['compressed'], "rb"), caption="🎬 Compressed Dubbed Video")
                await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ Compress Error: {e}")
            
        wipe_all_user_data(user_id)

    # 🎧 USER CLICKED AUDIO ONLY
    elif data == "send_audio_only":
        await query.message.delete()
        await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="🎧 Advanced Dubbed Audio")
        wipe_all_user_data(user_id)

    await query.answer()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if "http" in msg.text:
        await msg.reply_text("⬇️ YouTube URL လက်ခံရရှိပါတယ်။\n(Video ဒေါင်းလုပ်လုပ်ပြီး ပါက Subtitle ပြင်ဆင်ရန် ဆက်လုပ်ပါမည်)")
        # ... your youtube dl code here ...
    else:
        await msg.reply_text("ℹ️ Please send a YouTube link, Video file, or SRT file.\nUse /start to adjust settings.")

if __name__ == "__main__":
    try:
        threading.Thread(target=start_health_server, daemon=True).start()
        app = ApplicationBuilder().token(TG_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
        app.run_polling()
    except Exception as e:
        logger.critical(f"Fatal: {e}")
