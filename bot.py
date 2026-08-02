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
    logger.critical(f"❌ Missing Library: {e}. Please run pip install -r requirements.txt")
    sys.exit(1)

# -------------------------------------------------------------------------
# DUMMY SERVER (To keep bot alive on Render/Heroku)
# -------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running! - Pro Video AI 2026")

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

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"
logger.info(f"🚀 AI Engine running on: {DEVICE.upper()} (Compute: {COMPUTE_TYPE})")

# --- WHISPER MODELS ---
WHISPER_LIB = {
    "⚡ Tiny (Render Safe - 150MB)": "tiny",
    "⚖️ Base (Standard - 300MB)": "base",
    "🎯 Small (Better - 1GB)": "small",
    "🧠 Medium (Heavy - 3GB)": "medium"
}

# --- FULL VOICE ROSTER ---
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

# --- AUDIO ENGINE SETTINGS ---
SPEED_LIB = {"🤖 Auto-Sync": "auto", "1.0x (Normal)": 1.0, "0.9x (Slow)": 0.9, "0.8x (Slower)": 0.8}
RATE_LIB = {"🐢 -10%": "-10%", "🚶 +0%": "+0%", "🏃 +10%": "+10%", "🏎️ +20%": "+20%", "🚀 +30%": "+30%"}
FIT_LIB = {"🗜️ Max +50%": 50, "🗜️ Max +60%": 60, "🗜️ Max +70%": 70}

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
            "whisper_model": "tiny", 
            "ask_srt_workflow": False, 
            "burn_subs": True,      
            "cover_subs": False     
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
# ADVANCED AUDIO ENGINE
# -------------------------------------------------------------------------
def get_audio_hash(text, voice, rate): return hashlib.md5(f"{text}|{voice}|{rate}".encode()).hexdigest()

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
        
        if not os.path.exists(path):
            for attempt in range(3):
                try:
                    comm = edge_tts.Communicate(text, voice, rate=base_rate_str, pitch="-2Hz")
                    await comm.save(path)
                    break
                except Exception:
                    if attempt == 2: return {"index": idx, "len": 0, "path": None, "text": "", "voice": voice}
                    await asyncio.sleep(1)
                    
        length = await asyncio.to_thread(process_length_and_trim, path)
        await progress_cb()
        return {"index": idx, "len": length, "path": path, "text": text, "voice": voice}

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
        
        if not os.path.exists(final_path):
            for attempt in range(3):
                try:
                    comm = edge_tts.Communicate(text, voice, rate=rate_str, pitch="-2Hz")
                    await comm.save(final_path)
                    await asyncio.to_thread(process_length_and_trim, final_path)
                    break
                except Exception:
                    if attempt == 2: 
                        await progress_cb()
                        return (new_start_ms, base_path) 
                    await asyncio.sleep(1)
                    
        await progress_cb()
        return (new_start_ms, final_path)

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
        last_posted_text = ""

        async def update_progress(phase_name):
            nonlocal completed_tasks, last_update_time, last_posted_text
            completed_tasks += 1
            now = time.time()
            if now - last_update_time > 2.5 or completed_tasks == total_tasks:
                percent = int((completed_tasks / total_tasks) * 100)
                new_text = f"🎬 **{phase_name}...**\n⏳ **Processing: {percent}%**"
                if new_text != last_posted_text:
                    try: 
                        await status_msg.edit_text(new_text)
                        last_posted_text = new_text
                    except Exception: pass
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
# FFMPEG MUXING WITH NEW SETTINGS (Burn-in & Cover)
# -------------------------------------------------------------------------
async def merge_video_audio(user_id, state):
    p = get_paths(user_id)
    if not os.path.exists(p['input']) or not os.path.exists(p['dub_audio']): 
        return False, "Media missing."

    vf_filters = ["hflip", "eq=brightness=0.05:saturation=1.1"]

    if state['cover_subs']:
        vf_filters.append("drawbox=y=ih-ih*0.15:color=black@0.7:width=iw:height=ih*0.15:t=fill")

    if state['burn_subs']:
        escaped_srt = p['srt'].replace("\\", "/").replace(":", "\\:")
        vf_filters.append(f"subtitles='{escaped_srt}'")

    vf_str = ",".join(vf_filters)

    cmd = [
        "ffmpeg", "-y", "-i", p['input'], "-i", p['dub_audio'],
        "-vf", vf_str,  
        "-c:v", "libx264", "-preset", "fast", "-crf", "26", 
        "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1",  
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

def run_whisper_engine(audio_path, srt_path, model_name="tiny"):
    try:
        logger.info(f"Running Whisper Model: {model_name}")
        model = WhisperModel(model_name, device=DEVICE, compute_type=COMPUTE_TYPE, cpu_threads=2)
        segments, _ = model.transcribe(
            audio_path, beam_size=3, vad_filter=True, 
            word_timestamps=True, condition_on_previous_text=False
        )
        
        with open(srt_path, "w", encoding="utf-8") as srt:
            for i, seg in enumerate(segments, 1):
                srt.write(f"{i}\n{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n{seg.text.strip()}\n\n")
        return f"Whisper ({model_name.upper()})"
    except Exception as e:
        return f"Error: {e}"

async def run_translate(user_id, prompt_text):
    p = get_paths(user_id)
    if not os.path.exists(p['srt']): return False, "No SRT found.", None
    if not GEMINI_KEY: return False, "GEMINI_API_KEY missing.", None

    model = genai.GenerativeModel('gemini-3.5-flash')
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

def get_translation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇲🇲 Burmese", callback_data="trans_burmese"), InlineKeyboardButton("🇯🇵 Japanese", callback_data="trans_japanese")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="trans_english"), InlineKeyboardButton("🇨🇳 Chinese", callback_data="trans_chinese")],
        [InlineKeyboardButton("🇰🇷 Korean", callback_data="trans_korean"), InlineKeyboardButton("🇪🇸 Spanish", callback_data="trans_spanish")],
        [InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]
    ])

# -------------------------------------------------------------------------
# TELEGRAM HANDLERS & MENUS
# -------------------------------------------------------------------------
def get_settings_keyboard(state):
    v_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Voice")
    s_name = next((k for k, v in SPEED_LIB.items() if v == state['video_speed']), f"{state['video_speed']}x")
    w_name = next((k for k, v in WHISPER_LIB.items() if v == state['whisper_model']), state['whisper_model'])
    ask_srt_status = "🟢 ON (Ask First)" if state['ask_srt_workflow'] else "🔴 OFF (Auto-Transcribe)"
    subs_status = "🟢 ON" if state['burn_subs'] else "🔴 OFF"
    cover_status = "🟢 ON" if state['cover_subs'] else "🔴 OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗣️ Voice: {v_name}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"🧠 Whisper: {w_name}", callback_data="cmd_whisper")],
        [InlineKeyboardButton(f"❓ Prompt SRT Upload: {ask_srt_status}", callback_data="toggle_ask_srt")],
        [InlineKeyboardButton(f"📝 Subtitles Burn-in: {subs_status}", callback_data="toggle_subs")],
        [InlineKeyboardButton(f"🔲 Cover Old Subs: {cover_status}", callback_data="toggle_cover")],
        [InlineKeyboardButton(f"🎙️ Rate: {state['base_rate']}", callback_data="cmd_rate"), InlineKeyboardButton(f"🗜️ Fit: +{state['max_fit']}%", callback_data="cmd_fit")],
        [InlineKeyboardButton(f"⏱️ Video Speed: {s_name}", callback_data="cmd_speed")],
        [InlineKeyboardButton("🧹 Clear Workspace", callback_data="cmd_clear")]
    ])

async def handle_menu(update, lib, prefix, text_msg, columns=1):
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
        "⚙️ **STUDIO SETTINGS**\nConfigure your AI narrator, Whisper model, and workflows.\n\n"
        "💡 *Render Tip:* Keep Whisper set to **Tiny** to prevent Render RAM crashes."
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=get_settings_keyboard(state))
    else:
        await update.message.reply_text(text, reply_markup=get_settings_keyboard(state))

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Directly translate an uploaded SRT file using /translate"""
    user_id = update.effective_user.id
    p = get_paths(user_id)
    if not os.path.exists(p['srt']):
        return await update.message.reply_text("❌ No SRT file found. Upload an `.srt` file first, then run /translate.")
    
    await update.message.reply_text("🌐 Choose Translation Language for your SRT:", reply_markup=get_translation_keyboard())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data
    p = get_paths(user_id)

    if data == "cmd_start": await start(update, context)
    elif data == "cmd_clear":
        wipe_all_user_data(user_id)
        await query.answer("Workspace cleared")
        await query.message.edit_text("🧹 All videos, audio, and cache cleared.")
    
    # Sub-menus
    elif data == "cmd_voices": await handle_menu(update, VOICE_LIB, "set_voice", "🗣️ **Select Narrator Voice:**", 2)
    elif data == "cmd_whisper": await handle_menu(update, WHISPER_LIB, "set_whisper", "🧠 **Select Whisper Model:**\n*(Choose 'tiny' for low-memory environments like Render)*", 1)
    elif data == "cmd_rate": await handle_menu(update, RATE_LIB, "set_rate", "🎙️ **Select Base TTS Speak Rate:**", 3)
    elif data == "cmd_fit": await handle_menu(update, FIT_LIB, "set_fit", "🗜️ **Select Max Emergency Squeeze Speed:**", 3)
    elif data == "cmd_speed": await handle_menu(update, SPEED_LIB, "set_speed", "⏱️ **Select Video Speed Mode:**", 2)
    
    # Toggles
    elif data == "toggle_ask_srt":
        state['ask_srt_workflow'] = not state['ask_srt_workflow']
        await start(update, context)
    elif data == "toggle_subs":
        state['burn_subs'] = not state['burn_subs']
        await start(update, context)
    elif data == "toggle_cover":
        state['cover_subs'] = not state['cover_subs']
        await start(update, context)

    # Menu Setters
    elif data.startswith("set_"):
        if data.startswith("set_voice_"): state['dub_voice'] = data.replace("set_voice_", "")
        elif data.startswith("set_whisper_"): state['whisper_model'] = data.replace("set_whisper_", "")
        elif data.startswith("set_rate_"): state['base_rate'] = data.replace("set_rate_", "")
        elif data.startswith("set_fit_"): state['max_fit'] = int(data.replace("set_fit_", ""))
        elif data.startswith("set_speed_"): 
            val = data.replace("set_speed_", "")
            state['video_speed'] = "auto" if val == "auto" else float(val)
        await start(update, context)

    # Manual workflow triggers
    elif data == "run_auto_transcribe":
        status = await query.message.reply_text(f"🎙️ Transcribing Audio with Whisper AI ({state['whisper_model'].upper()})...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_whisper_engine, p['audio'], p['srt'], state['whisper_model'])
        if os.path.exists(p['srt']):
            await context.bot.send_document(query.message.chat_id, document=open(p['srt'], "rb"), caption="✅ Transcribed by Whisper AI")
        await status.edit_text("✅ Transcription Complete.\nChoose Translation Language:", reply_markup=get_translation_keyboard())

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

    # 🎬 DUBBING PROCESS & 50MB SIZE CHECK
    elif data == "trigger_dub":
        if not os.path.exists(p['srt']):
            return await query.message.reply_text("❌ No SRT found. Translate or Upload one first.")

        clean_temp_audio(user_id)
        status = await query.message.reply_text("🚀 Audio Generating...")
        success, error = await generate_advanced_dubbing(user_id, p['srt'], p['dub_audio'], state, status)
        
        if success:
            if os.path.exists(p['input']):
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
                    await status.edit_text("❌ Video Merge Failed.")
                    await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="🎧 Advanced Dubbed Audio")
            else:
                await status.delete()
                await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), caption="🎧 Advanced Dubbed Audio")
        else:
            await status.edit_text(f"❌ Dubbing Failed: {error}")

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

async def process_media(update, context, is_url):
    msg = update.message
    user_id = msg.from_user.id
    state = get_user_state(user_id)
    p = get_paths(user_id)

    status = await msg.reply_text("⏳ Processing Media...")
    try:
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

        if has_subs:
            await context.bot.send_document(msg.chat_id, document=open(p['srt'], "rb"), caption="✅ Extracted YouTube Auto-Subtitles")
            await status.edit_text("✅ Download & Transcription Done.\nChoose Translation Language:", reply_markup=get_translation_keyboard())
            return

        # CHECK IF "ASK SRT WORKFLOW" IS ENABLED
        if state['ask_srt_workflow']:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎙️ Transcribe with Whisper AI", callback_data="run_auto_transcribe")],
                [InlineKeyboardButton("📤 I will upload custom SRT", callback_data="cmd_start")]
            ])
            await status.edit_text("✅ Video Downloaded! Choose how you want to handle subtitles:", reply_markup=kb)
        else:
            # FAST TRACK: Auto-transcribe using chosen Whisper model
            await status.edit_text(f"🎙️ Transcribing Audio with Whisper AI ({state['whisper_model'].upper()})...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_whisper_engine, p['audio'], p['srt'], state['whisper_model'])
            
            if os.path.exists(p['srt']):
                await context.bot.send_document(msg.chat_id, document=open(p['srt'], "rb"), caption=f"✅ Transcribed by Whisper AI ({state['whisper_model'].upper()})")

            await status.edit_text("✅ Download & Transcription Done.\nChoose Translation Language:", reply_markup=get_translation_keyboard())

    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:200]}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "http" in text and any(x in text.lower() for x in ["youtube.com", "youtu.be", "tiktok.com", "facebook.com", "instagram.com"]):
        await process_media(update, context, is_url=True)
    else:
        await update.message.reply_text("ℹ️ Please send a YouTube/TikTok link, Video file, or SRT file.\nUse /start to adjust settings or /translate to translate an SRT.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)

    if msg.document and msg.document.file_name.lower().endswith('.srt'):
        await (await msg.document.get_file()).download_to_drive(p['srt'])
        
        # Immediate Translation Keyboard for Direct SRT Uploads
        if os.path.exists(p['input']):
            await msg.reply_text("✅ **Custom SRT Loaded over existing Video!**\nSelect target translation language or process dubbing:", reply_markup=get_translation_keyboard())
        else:
            await msg.reply_text("✅ **Custom SRT Loaded!**\nTranslate it now or process audio-only dubbing:", reply_markup=get_translation_keyboard())
        return

    await process_media(update, context, is_url=False)

if __name__ == "__main__":
    logger.info("Starting Final Pro Video AI Bot (v2026)")
    try:
        threading.Thread(target=start_health_server, daemon=True).start()
        app = ApplicationBuilder().token(TG_TOKEN).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("settings", start))
        app.add_handler(CommandHandler("translate", translate_command))
        
        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
        app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO, file_handler))
        
        logger.info("Polling...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.critical(f"Fatal: {e}")
