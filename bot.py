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
from io import StringIO

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
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
        self.wfile.write(b"Bot is running!")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# -------------------------------------------------------------------------
# CONFIGURATION & DICTIONARIES
# -------------------------------------------------------------------------
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if not TG_TOKEN:
    logger.critical("❌ ERROR: TELEGRAM_TOKEN missing.")
    sys.exit(1)

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# --- FULL VOICE ROSTER (From Code B) ---
VOICE_LIB = {
    "🇲🇲 Thiha (Male)": "my-MM-ThihaNeural",
    "🇲🇲 Nilar (Female)": "my-MM-NilarNeural",
    "🇬🇧 Sonia (Female)": "en-GB-SoniaNeural",
    "🇬🇧 Ryan (Male)": "en-GB-RyanNeural",
    "🇨🇳 Xiaoxiao (Female)": "zh-CN-XiaoxiaoNeural",
    "🇨🇳 Yunxi (Male)": "zh-CN-YunxiNeural",
    "🇰🇷 SunHi (Female)": "ko-KR-SunHiNeural",
    "🇰🇷 InJoon (Male)": "ko-KR-InJoonNeural",
    "🇺🇸 Remy (Multi)": "en-US-RemyMultilingualNeural",
    "🇺🇸 Andrew (Multi)": "en-US-AndrewMultilingualNeural",
    "🇯🇵 Nanami (Female)": "ja-JP-NanamiNeural",
    "🇯🇵 Keita (Male)": "ja-JP-KeitaNeural"
}

SPEAKER_ALIASES = {
    "thiha": "my-MM-ThihaNeural", "nilar": "my-MM-NilarNeural",
    "sonia": "en-GB-SoniaNeural", "ryan": "en-GB-RyanNeural",
    "xiaoxiao": "zh-CN-XiaoxiaoNeural", "yunxi": "zh-CN-YunxiNeural",
    "sunhi": "ko-KR-SunHiNeural", "injoon": "ko-KR-InJoonNeural",
    "remy": "en-US-RemyMultilingualNeural", "andrew": "en-US-AndrewMultilingualNeural",
    "nanami": "ja-JP-NanamiNeural", "keita": "ja-JP-KeitaNeural"
}

# --- AUDIO ENGINE SETTINGS (From Code B) ---
SPEED_LIB = {
    "🤖 Auto-Sync": "auto",
    "1.0x (Normal)": 1.0,
    "0.9x (Slow)": 0.9,
    "0.8x (Slower)": 0.8
}

RATE_LIB = {
    "🐢 -10%": "-10%",
    "🚶 +0%": "+0%",
    "🏃 +10%": "+10%",
    "🏎️ +20%": "+20%",
    "🚀 +30%": "+30%"
}

FIT_LIB = {
    "🗜️ Max +50%": 50,
    "🗜️ Max +60%": 60,
    "🗜️ Max +70%": 70
}

SRT_RULES = """
**FORMATTING INSTRUCTIONS:**
1. The input is an **SRT Subtitle File**.
2. **TIMESTAMPS:** Do NOT change, shift, or remove any timestamps. 
3. **SEQUENCE NUMBERS:** Preserve exact sequence.
**TIP:** If there are multiple speakers, label them (e.g. "nilar: မင်္ဂလာပါ", "thiha: ဟုတ်ကဲ့").
"""

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
            "custom_prompts": {}
        }
    return user_prefs[user_id]

def get_paths(user_id):
    return {
        "input": f"downloads/{user_id}_input.mp4",
        "audio": f"downloads/{user_id}_audio.mp3",
        "srt": f"downloads/{user_id}_subs.srt",
        "trans_result": f"downloads/{user_id}_translated.srt",
        "dub_audio": f"downloads/{user_id}_dubbed.mp3",
        "final_video": f"downloads/{user_id}_final.mp4"
    }

def clean_temp_audio(user_id):
    """Clears only temporary TTS chunks, keeps Video and SRT safe."""
    for f in glob.glob(f"temp/{user_id}_*"):
        try: os.remove(f)
        except: pass

def wipe_all_user_data(user_id):
    """Wipes everything completely."""
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
# ADVANCED AUDIO ENGINE (Two-Phase Native Speed)
# -------------------------------------------------------------------------
def get_audio_hash(text, voice, rate):
    return hashlib.md5(f"{text}|{voice}|{rate}".encode()).hexdigest()

def trim_silence(audio_segment, silence_thresh=-40.0, chunk_size=5):
    if len(audio_segment) < 100: return audio_segment
    start_trim = detect_leading_silence(audio_segment, silence_threshold=silence_thresh, chunk_size=chunk_size)
    end_trim = detect_leading_silence(audio_segment.reverse(), silence_threshold=silence_thresh, chunk_size=chunk_size)
    return audio_segment[start_trim:len(audio_segment)-end_trim]

def make_audio_crisp(audio_segment):
    return effects.normalize(audio_segment.high_pass_filter(150))

def process_length_and_trim(file_path):
    seg = AudioSegment.from_file(file_path)
    seg = trim_silence(seg)
    seg.export(file_path, format="mp3")
    return len(seg)

def parse_speaker(text, default_voice):
    match = re.match(r"^([a-zA-Z0-9]+)\s*:\s*(.*)", text)
    if match:
        speaker_name = match.group(1).lower()
        clean_text = match.group(2)
        if speaker_name in SPEAKER_ALIASES:
            return SPEAKER_ALIASES[speaker_name], clean_text
    return default_voice, text

def compose_final_audio(chunks_data, duration_ms, output_path):
    final_audio = AudioSegment.silent(duration=duration_ms + 2000) 
    for start_ms, file_path in chunks_data:
        if not file_path: continue
        try:
            segment = AudioSegment.from_file(file_path)
            segment = make_audio_crisp(segment)
            if start_ms + len(segment) > len(final_audio):
                final_audio += AudioSegment.silent(duration=(start_ms + len(segment) - len(final_audio) + 2000))
            final_audio = final_audio.overlay(segment, position=start_ms)
        except Exception as e:
            logger.error(f"Error chunk {file_path}: {e}")
    final_audio.export(output_path, format="mp3", bitrate="192k")

async def measure_base_chunk(chunk, user_id, base_rate_str, semaphore, progress_cb):
    async with semaphore:
        idx, text, voice = chunk["index"], chunk["text"], chunk["voice"]
        if not text: 
            await progress_cb()
            return {"index": idx, "len": 0, "path": None, "text": "", "voice": voice}
            
        audio_hash = get_audio_hash(text, voice, base_rate_str)
        path = f"temp/{user_id}_{audio_hash}.mp3"
        
        if os.path.exists(path):
            length = await asyncio.to_thread(process_length_and_trim, path)
            await progress_cb()
            return {"index": idx, "len": length, "path": path, "text": text, "voice": voice}

        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(text, voice, rate=base_rate_str, pitch="-2Hz")
                await comm.save(path)
                length = await asyncio.to_thread(process_length_and_trim, path)
                await progress_cb()
                return {"index": idx, "len": length, "path": path, "text": text, "voice": voice}
            except Exception:
                if attempt == 2: return {"index": idx, "len": 0, "path": None, "text": "", "voice": voice}
                await asyncio.sleep(1)

async def process_final_chunk(chunk, base_res, global_ratio, user_id, base_rate_int, max_fit, semaphore, progress_cb):
    async with semaphore:
        idx, start_ms = chunk["index"], chunk["start"]
        stretched_gap = chunk["gap"] * global_ratio
        new_start_ms = int(start_ms * global_ratio)

        if not base_res or base_res["len"] == 0:
            await progress_cb()
            return (new_start_ms, None)

        base_len, base_path, text, voice = base_res["len"], base_res["path"], base_res["text"], base_res["voice"]

        if base_len <= stretched_gap:
            await progress_cb()
            return (new_start_ms, base_path) 
            
        ratio = base_len / stretched_gap
        extra_speed = (ratio - 1) * 100
        new_rate_int = min(max_fit, int(base_rate_int + extra_speed + 5)) 

        rate_str = f"+{new_rate_int}%"
        audio_hash = get_audio_hash(text, voice, rate_str)
        final_path = f"temp/{user_id}_{audio_hash}.mp3"
        
        if os.path.exists(final_path):
            await progress_cb()
            return (new_start_ms, final_path)

        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(text, voice, rate=rate_str, pitch="-2Hz")
                await comm.save(final_path)
                await asyncio.to_thread(process_length_and_trim, final_path)
                await progress_cb()
                return (new_start_ms, final_path)
            except Exception:
                if attempt == 2: 
                    await progress_cb()
                    return (new_start_ms, base_path) 
                await asyncio.sleep(1)

async def generate_advanced_dubbing(user_id, srt_path, output_path, state, status_msg):
    try:
        subs = pysrt.open(srt_path)
        if not subs: return False, "SRT file is empty."

        global_voice = state['dub_voice']
        base_rate_str = state['base_rate']
        base_rate_int = int(base_rate_str.replace("%", "").replace("+", "").replace("-", ""))
        max_fit = state['max_fit']
        target_speed = state['video_speed']

        chunks_data = []
        for i, sub in enumerate(subs):
            start_ms = sub.start.ordinal
            end_ms = sub.end.ordinal
            next_start_ms = subs[i+1].start.ordinal if i + 1 < len(subs) else end_ms + 2000 
            gap = next_start_ms - start_ms
            raw_text = sub.text.replace("\n", " ").strip()
            chunk_voice, clean_text = parse_speaker(raw_text, global_voice)
            chunks_data.append({"index": i, "start": start_ms, "end": end_ms, "gap": gap, "text": clean_text, "voice": chunk_voice})

        total_tasks = len(chunks_data) * 2
        completed_tasks = 0
        last_update_time = time.time()

        async def update_progress(phase_name):
            nonlocal completed_tasks, last_update_time
            completed_tasks += 1
            now = time.time()
            if now - last_update_time > 2.0 or completed_tasks == total_tasks:
                percent = int((completed_tasks / total_tasks) * 100)
                try: await status_msg.edit_text(f"🎬 **{phase_name}...**\n⏳ **Processing: {percent}%**")
                except: pass
                last_update_time = now

        semaphore = asyncio.Semaphore(15) 
        p1_tasks = [measure_base_chunk(c, user_id, base_rate_str, semaphore, lambda: update_progress("Phase 1/2 (Measuring)")) for c in chunks_data]
        p1_results_list = await asyncio.gather(*p1_tasks)
        base_results = {res["index"]: res for res in p1_results_list if res is not None}

        max_ratio_needed = 1.0
        for chunk in chunks_data:
            res = base_results.get(chunk["index"])
            if not res or res["len"] == 0: continue
            if res["len"] > chunk["gap"]:
                min_len = res["len"] * ( (1.0 + (base_rate_int/100)) / (1.0 + (max_fit/100)) )
                if min_len > chunk["gap"]:
                    ratio = min_len / chunk["gap"]
                    if ratio > max_ratio_needed: max_ratio_needed = ratio

        if target_speed == "auto":
            clean_speed = max(0.6, math.floor((1.0 / max_ratio_needed) * 10) / 10.0)
            global_stretch_ratio = 1.0 / clean_speed
        else:
            global_stretch_ratio = 1.0 / float(target_speed)

        p2_tasks = [process_final_chunk(c, base_results.get(c["index"]), global_stretch_ratio, user_id, base_rate_int, max_fit, semaphore, lambda: update_progress("Phase 2/2 (Finalizing)")) for c in chunks_data]
        final_stretched_chunks = await asyncio.gather(*p2_tasks)

        last_sub_end_ms = int(subs[-1].end.ordinal * global_stretch_ratio)
        await asyncio.to_thread(compose_final_audio, final_stretched_chunks, last_sub_end_ms, output_path)
        
        return True, None
    except Exception as e:
        logger.error(f"Advanced Dubbing Failed: {e}")
        return False, str(e)

# -------------------------------------------------------------------------
# FFMPEG MUXING
# -------------------------------------------------------------------------
async def merge_video_audio(user_id):
    p = get_paths(user_id)
    if not os.path.exists(p['input']) or not os.path.exists(p['dub_audio']):
        return False, "Media missing."

    cmd = [
        "ffmpeg", "-y",
        "-i", p['input'],
        "-i", p['dub_audio'],
        "-vf", "hflip,eq=brightness=0.05:saturation=1.1",  
        "-c:v", "libx264",  
        "-preset", "fast",  
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-map_metadata", "-1",  
        p['final_video']
    ]
    try:
        await run_async_cmd(*cmd)
        return True, p['final_video']
    except Exception as e:
        return False, str(e)

# -------------------------------------------------------------------------
# TRANSCRIBE & TRANSLATE
# -------------------------------------------------------------------------
def format_timestamp(seconds):
    hours = math.floor(seconds / 3600)
    minutes = math.floor((seconds % 3600) / 60)
    secs = math.floor(seconds % 60)
    millis = round((seconds - math.floor(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def run_whisper_engine(audio_path, srt_path):
    try:
        # Optimized for Render Free Tier (Base Model + 2 Threads)
        model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2)
        segments, _ = model.transcribe(audio_path, beam_size=5, vad_filter=True, word_timestamps=True)
        
        with open(srt_path, "w", encoding="utf-8") as srt:
            for i, seg in enumerate(segments, 1):
                srt.write(f"{i}\n{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n{seg.text.strip()}\n\n")
        return "Whisper Base"
    except Exception as e:
        return f"Error: {e}"

async def run_translate(user_id, prompt_text):
    p = get_paths(user_id)
    if not os.path.exists(p['srt']): return False, "No SRT found.", None
    if not GEMINI_KEY: return False, "GEMINI_API_KEY missing in Render.", None

    model = genai.GenerativeModel('gemini-1.5-flash')
    with open(p['srt'], "r", encoding="utf-8") as f: content = f.read()

    full_prompt = f"{prompt_text}\n\n**INPUT SRT:**\n{content}"
    try:
        response = model.generate_content(full_prompt)
        result = response.text.strip().replace("```srt", "").replace("```", "").strip()
        with open(p['trans_result'], "w", encoding="utf-8") as f: f.write(result)
        shutil.copy(p['trans_result'], p['srt']) 
        return True, result, p['trans_result']
    except Exception as e:
        return False, str(e), None

# -------------------------------------------------------------------------
# TELEGRAM HANDLERS & MENUS
# -------------------------------------------------------------------------
def get_settings_keyboard(state):
    v_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Voice")
    s_name = next((k for k, v in SPEED_LIB.items() if v == state['video_speed']), f"{state['video_speed']}x")
    r_name = next((k for k, v in RATE_LIB.items() if v == state['base_rate']), state['base_rate'])
    f_name = f"+{state['max_fit']}%"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗣️ Voice: {v_name}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"🎙️ Rate: {r_name}", callback_data="cmd_rate"), InlineKeyboardButton(f"🗜️ Fit: {f_name}", callback_data="cmd_fit")],
        [InlineKeyboardButton(f"⏱️ Video Speed: {s_name}", callback_data="cmd_speed")],
        [InlineKeyboardButton("🧹 Clear Workspace", callback_data="cmd_clear")]
    ])

async def handle_menu(update, lib, prefix, text_msg, columns=2):
    keyboard, row = [], []
    for name, val in lib.items():
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}_{val}"))
        if len(row) == columns:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="cmd_start")])
    msg = update.message if update.message else update.callback_query.message
    await msg.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    text = (
        "⚙️ **STUDIO SETTINGS**\nConfigure your AI narrator and speed limits.\n\n"
        "*(Fast-Track: Upload your Video -> Upload your SRT -> Click Dub)*"
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=get_settings_keyboard(state))
    else:
        await update.message.reply_text(text, reply_markup=get_settings_keyboard(state))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data

    if data == "cmd_start": await start(update, context)
    elif data == "cmd_clear":
        wipe_all_user_data(user_id)
        await query.answer("Workspace cleared")
        await query.message.edit_text("🧹 All videos, audio, and cache cleared.")
    
    # Sub-menus
    elif data == "cmd_voices": await handle_menu(update, VOICE_LIB, "set_voice", "🗣️ **Select Narrator Voice:**", 2)
    elif data == "cmd_rate": await handle_menu(update, RATE_LIB, "set_rate", "🎙️ **Select Base TTS Speak Rate:**", 3)
    elif data == "cmd_fit": await handle_menu(update, FIT_LIB, "set_fit", "🗜️ **Select Max Emergency Squeeze Speed:**", 3)
    elif data == "cmd_speed": await handle_menu(update, SPEED_LIB, "set_speed", "⏱️ **Select Video Speed Mode:**", 2)
    
    # Menu Setters
    elif data.startswith("set_"):
        if data.startswith("set_voice_"): state['dub_voice'] = data.replace("set_voice_", "")
        elif data.startswith("set_rate_"): state['base_rate'] = data.replace("set_rate_", "")
        elif data.startswith("set_fit_"): state['max_fit'] = int(data.replace("set_fit_", ""))
        elif data.startswith("set_speed_"): 
            val = data.replace("set_speed_", "")
            state['video_speed'] = "auto" if val == "auto" else float(val)
        await start(update, context)

    # Actions
    elif data.startswith("trans_"):
        lang = data.replace("trans_", "")
        prompt = f"Target Language: {lang.title()}\nTranslate this subtitle file naturally. {SRT_RULES}"
        await query.message.reply_text(f"⏳ Translating to {lang.title()} via Gemini AI...")
        success, _, path = await run_translate(user_id, prompt)
        if success:
            await context.bot.send_document(query.message.chat_id, open(path, "rb"), caption=f"✅ {lang.title()} Translation")
            await context.bot.send_message(query.message.chat_id, "Ready to Dub:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]]))
        else:
            await query.message.reply_text("❌ Translation failed. Check API key.")

    elif data == "trigger_dub":
        p = get_paths(user_id)
        if not os.path.exists(p['srt']):
            return await query.message.reply_text("❌ No SRT found. Translate or Upload one first.")

        clean_temp_audio(user_id)
        status = await query.message.reply_text("🚀 Initializing Advanced Audio Engine...")
        success, error = await generate_advanced_dubbing(user_id, p['srt'], p['dub_audio'], state, status)
        
        if success:
            if os.path.exists(p['input']):
                await status.edit_text("✅ Audio Generated!\n🎥 Merging Audio with Video... (Applying Copyright Bypass)")
                merge_success, merge_res = await merge_video_audio(user_id)
                await status.delete()
                if merge_success:
                    try: await context.bot.send_video(query.message.chat_id, open(merge_res, "rb"), caption="🎬 Final Dubbed Video", supports_streaming=True)
                    except: await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="Video too large. Audio sent instead.")
                else:
                    await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="❌ Video merge failed. Audio sent instead.")
            else:
                await status.delete()
                await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="🎧 Advanced Dubbed Audio")
        else:
            await status.edit_text(f"❌ Dubbing Failed: {error}")

    await query.answer()

async def process_media(update, context, is_url):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)

    status = await msg.reply_text("⏳ Processing Media...")
    try:
        # Wipe ONLY if it's a completely new video process to avoid messing up manual SRTs
        if os.path.exists(p['srt']): os.remove(p['srt'])
        clean_temp_audio(user_id)
        has_subs = False

        if is_url:
            url = msg.text.strip()
            await status.edit_text("⬇️ Downloading Video...")
            yt_cmd = ["yt-dlp"]
            if os.path.exists("cookies.txt"): yt_cmd.extend(["--cookies", "cookies.txt"])
            yt_cmd.extend(["--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4", "--out", p['input'], url])
            await run_async_cmd(*yt_cmd)

            await status.edit_text("🔎 Checking subtitles...")
            sub_cmd = ["yt-dlp", "--write-subs", "--write-auto-sub", "--convert-subs", "srt", "--sub-langs", "en,*", "--skip-download", "-o", f"downloads/{user_id}_video", url]
            if os.path.exists("cookies.txt"): sub_cmd.insert(1, "--cookies"); sub_cmd.insert(2, "cookies.txt")
            subprocess.run(sub_cmd, capture_output=True)

            subs_files = glob.glob(f"downloads/{user_id}_video*.srt")
            if subs_files:
                shutil.move(subs_files[0], p['srt'])
                has_subs = True
            
            await run_async_cmd("ffmpeg", "-y", "-i", p['input'], "-vn", "-acodec", "libmp3lame", "-q:a", "2", p['audio'])
        else:
            if msg.video and msg.video.file_size > 50_000_000:
                return await status.edit_text("❌ Video > 50 MB (Telegram Limit). Send YouTube link instead.")
            file = await (msg.video or msg.document or msg.audio).get_file()
            await file.download_to_drive(p['input'])
            await run_async_cmd("ffmpeg", "-y", "-i", p['input'], "-vn", "-acodec", "libmp3lame", "-q:a", "2", p['audio'])

        if not has_subs:
            await status.edit_text("🎙️ Transcribing Audio with Whisper AI (Base Model)...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_whisper_engine, p['audio'], p['srt'])
            caption = "✅ Transcribed by Whisper AI"
        else:
            caption = "✅ Extracted YouTube Auto-Subtitles"

        if os.path.exists(p['srt']):
            await context.bot.send_document(msg.chat_id, document=open(p['srt'], "rb"), caption=caption)

        await status.edit_text("✅ Download & Transcription Done.\nChoose Translation Language (Or upload custom SRT):")
        kb = [
            [InlineKeyboardButton("🇲🇲 Burmese", callback_data="trans_burmese"), InlineKeyboardButton("🇯🇵 Japanese", callback_data="trans_japanese")],
            [InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]
        ]
        await context.bot.send_message(msg.chat_id, "Next Action:", reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:200]}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "http" in text and any(x in text.lower() for x in ["youtube.com", "youtu.be", "tiktok.com", "facebook.com"]):
        await process_media(update, context, is_url=True)
    else:
        await update.message.reply_text("ℹ️ Please send a YouTube link, Video file, or SRT file.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)

    if msg.document and msg.document.file_name.lower().endswith('.srt'):
        await (await msg.document.get_file()).download_to_drive(p['srt'])
        kb = [[InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]]
        
        # Check if they already uploaded a video
        if os.path.exists(p['input']):
            await msg.reply_text("✅ **Custom SRT Loaded over existing Video!**\nHit Dub below to start rendering.", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await msg.reply_text("✅ **Custom SRT Loaded!**\n⚠️ No video found. Upload a video first, then upload this SRT again if you want a final video output. Or click Dub for Audio only.", reply_markup=InlineKeyboardMarkup(kb))
        return

    await process_media(update, context, is_url=False)

if __name__ == "__main__":
    logger.info("Starting Final Pro Video AI Bot")
    try:
        threading.Thread(target=start_health_server, daemon=True).start()
        app = ApplicationBuilder().token(TG_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
        app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO, file_handler))
        
        logger.info("Polling...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.critical(f"Fatal: {e}")
