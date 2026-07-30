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
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

if shutil.which("ffmpeg") is None:
    logger.error("❌ FFmpeg is NOT installed. Audio processing will fail.")
else:
    logger.info("✅ FFmpeg found.")

try:
    from pydub import AudioSegment, effects
    from pydub.silence import detect_leading_silence
    import edge_tts
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
    from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
    import google.generativeai as genai 
    from faster_whisper import WhisperModel
    logger.info("✅ All libraries imported.")
except ImportError as e:
    logger.critical(f"❌ Missing Library: {e}")
    sys.exit(1)

# -------------------------------------------------------------------------
# DUMMY SERVER (To keep bot alive on cloud hosts)
# -------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"🌍 Dummy server started on port {port}")
    server.serve_forever()

# -------------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------------
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if not TG_TOKEN or not GEMINI_KEY:
    logger.critical("❌ ERROR: API Keys missing. Check TELEGRAM_TOKEN and GEMINI_API_KEY.")
    sys.exit(1)

genai.configure(api_key=GEMINI_KEY)

VOICE_LIB = {
    "🇲🇲 Thiha (Male)": "my-MM-ThihaNeural",
    "🇲🇲 Nilar (Female)": "my-MM-NilarNeural",
    "🇯🇵 Nanami (Female)": "ja-JP-NanamiNeural",
    "🇯🇵 Keita (Male)": "ja-JP-KeitaNeural",
    "🇺🇸 Guy (Male)": "en-US-GuyNeural",
    "🇺🇸 Jenny (Female)": "en-US-JennyNeural",
    "🇬🇧 Sonia (British)": "en-GB-SoniaNeural",
    "🇨🇳 Xiaoxiao (Chinese)": "zh-CN-XiaoxiaoNeural",
    "🇰🇷 SunHi (Korean)": "ko-KR-SunHiNeural",
}

SPEAKER_ALIASES = {
    "thiha": "my-MM-ThihaNeural",
    "nilar": "my-MM-NilarNeural",
    "nanami": "ja-JP-NanamiNeural",
    "keita": "ja-JP-KeitaNeural",
    "guy": "en-US-GuyNeural",
    "jenny": "en-US-JennyNeural",
}

SRT_RULES = """
**FORMATTING INSTRUCTIONS (STRICT):**
1. The input is an **SRT Subtitle File**.
2. **OUTPUT FORMAT:** You MUST return a valid SRT file.
3. **TIMESTAMPS:** Do NOT change, shift, or remove any timestamps. 
4. **SEQUENCE NUMBERS:** Preserve exact sequence.
**TIP:** If there are multiple speakers, you can label them (e.g. "nilar: မင်္ဂလာပါ", "thiha: ဟုတ်ကဲ့") to trigger multi-speaker dubbing.
"""

BURMESE_STYLE = f"""
Target Language: Burmese (Myanmar)
Role: Native Burmese professional video narrator.
Guidelines:
• Use smooth, conversational Burmese.
• Translate by meaning and emotion, not word-by-word.
• **STRICT RULE:** NO ENGLISH CHARACTERS. Use Burmese pronunciation for loan words.
{SRT_RULES}
"""

JAPANESE_STYLE = f"""
Target Language: Japanese
Role: Professional Japanese Video Translator.
Guidelines:
• Translate into natural, spoken Japanese suitable for narration.
• **STRICT RULE:** NO ENGLISH CHARACTERS. Use Katakana for loan words.
{SRT_RULES}
"""

DEFAULT_PROMPTS = {
    "burmese": BURMESE_STYLE,
    "japanese": JAPANESE_STYLE,
}

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

# -------------------------------------------------------------------------
# STATE MANAGEMENT
# -------------------------------------------------------------------------
user_prefs = {}
user_modes = {}
chat_histories = {}
user_last_active = {}

def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "transcribe_engine": "whisper_dub",
            "dub_voice": "my-MM-ThihaNeural",
            "video_speed": "auto", 
            "base_rate": "+20%",
            "max_fit": 70,
            "custom_prompts": {}
        }
    return user_prefs[user_id]

def get_active_prompt(user_id, key):
    state = get_user_state(user_id)
    custom = state.get("custom_prompts", {}).get(key)
    return custom if custom else DEFAULT_PROMPTS.get(key, "")

def get_paths(user_id):
    return {
        "input": f"downloads/{user_id}_input.mp4",
        "audio": f"downloads/{user_id}_audio.mp3",
        "srt": f"downloads/{user_id}_subs.srt",
        "txt": f"downloads/{user_id}_transcript.txt",
        "trans_result": f"downloads/{user_id}_translated",
        "dub_audio": f"downloads/{user_id}_dubbed.mp3",
        "final_video": f"downloads/{user_id}_final.mp4"
    }

def clean_temp(user_id):
    p = get_paths(user_id)
    for key, path in p.items():
        if os.path.exists(path):
            try: os.remove(path)
            except: pass
    for f in glob.glob(f"temp/{user_id}_*"):
        try: os.remove(f)
        except: pass
    for f in glob.glob(f"downloads/{user_id}_video*"):
        try: os.remove(f)
        except: pass

async def send_copyable_message(chat_id, bot, text):
    if not text: return
    MAX_LEN = 4000
    safe_text = text.replace("`", "'")
    for i in range(0, len(safe_text), MAX_LEN):
        chunk = safe_text[i:i + MAX_LEN]
        try:
            await bot.send_message(chat_id=chat_id, text=f"```\n{chunk}\n```", parse_mode='Markdown')
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
# ADVANCED AUDIO ENGINE (FROM CODE B)
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
                try:
                    await status_msg.edit_text(f"🎬 **{phase_name}...**\n⏳ **Processing: {percent}%**")
                    last_update_time = now
                except Exception: pass

        semaphore = asyncio.Semaphore(15) 
        p1_tasks = [measure_base_chunk(c, user_id, base_rate_str, semaphore, lambda: update_progress("Phase 1/2 (Measuring)")) for c in chunks_data]
        p1_results_list = await asyncio.gather(*p1_tasks)
        base_results = {res["index"]: res for res in p1_results_list if res is not None}

        # Calculate exact stretch if auto
        max_ratio_needed = 1.0
        for chunk in chunks_data:
            res = base_results.get(chunk["index"])
            if not res or res["len"] == 0: continue
            if res["len"] > chunk["gap"]:
                min_len = res["len"] * ( (1.0 + (base_rate_int/100)) / (1.0 + (max_fit/100)) )
                if min_len > chunk["gap"]:
                    ratio = min_len / chunk["gap"]
                    if ratio > max_ratio_needed: max_ratio_needed = ratio

        clean_speed = max(0.6, math.floor((1.0 / max_ratio_needed) * 10) / 10.0)
        global_stretch_ratio = 1.0 / clean_speed

        p2_tasks = [process_final_chunk(c, base_results.get(c["index"]), global_stretch_ratio, user_id, base_rate_int, max_fit, semaphore, lambda: update_progress("Phase 2/2 (Finalizing)")) for c in chunks_data]
        final_stretched_chunks = await asyncio.gather(*p2_tasks)

        last_sub_end_ms = int(subs[-1].end.ordinal * global_stretch_ratio)
        await asyncio.to_thread(compose_final_audio, final_stretched_chunks, last_sub_end_ms, output_path)
        
        return True, None
    except Exception as e:
        logger.error(f"Advanced Dubbing Failed: {e}")
        return False, str(e)

# -------------------------------------------------------------------------
# FFMPEG MUXING & BYPASS
# -------------------------------------------------------------------------
async def merge_video_audio(user_id):
    p = get_paths(user_id)
    if not os.path.exists(p['input']) or not os.path.exists(p['dub_audio']):
        return False, "Input video or Dubbed audio missing."

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
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, beam_size=5, vad_filter=True, word_timestamps=True)
        
        with open(srt_path, "w", encoding="utf-8") as srt:
            for i, seg in enumerate(segments, 1):
                start_ts = format_timestamp(seg.start)
                end_ts = format_timestamp(seg.end)
                srt.write(f"{i}\n{start_ts} --> {end_ts}\n{seg.text.strip()}\n\n")
        return "Whisper Base"
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return f"Error: {e}"

async def run_translate(user_id, prompt_text):
    p = get_paths(user_id)
    if not os.path.exists(p['srt']): return False, "No SRT found.", None

    model = genai.GenerativeModel('gemini-1.5-flash')
    with open(p['srt'], "r", encoding="utf-8") as f:
        content = f.read()

    full_prompt = f"{prompt_text}\n\n**INPUT SRT:**\n{content}"
    try:
        response = model.generate_content(full_prompt)
        result = response.text.strip().replace("```srt", "").replace("```", "").strip()
        out_path = p['trans_result'] + ".srt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
        shutil.copy(out_path, p['srt']) 
        return True, result, out_path
    except Exception as e:
        return False, str(e), None

# -------------------------------------------------------------------------
# TELEGRAM HANDLERS
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Unknown")
    
    text = (
        f"👋 **Pro Video AI Studio**\n\n"
        f"🗣️ Active Voice: `{voice_name}`\n"
        f"*(Send a YouTube link, video, audio, or SRT file to begin)*\n\n"
        "Choose an action:"
    )

    keyboard = [
        [InlineKeyboardButton("Change Voice", callback_data="cmd_voices"),
         InlineKeyboardButton("Clear Workspace", callback_data="cmd_clear")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data

    if data == "cmd_start":
        await start(update, context)

    elif data == "cmd_voices":
        keyboard = []
        row = []
        for name, code in VOICE_LIB.items():
            row.append(InlineKeyboardButton(name, callback_data=f"set_voice_{code}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row: keyboard.append(row)
        await query.message.edit_text("🗣️ **Select Master Voice:**", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "cmd_clear":
        clean_temp(user_id)
        await query.answer("Workspace cleared")
        await query.message.edit_text("🧹 Workspace and temporary files cleared.")

    elif data.startswith("set_voice_"):
        voice = data.replace("set_voice_", "")
        state['dub_voice'] = voice
        name = next((k for k, v in VOICE_LIB.items() if v == voice), "Custom")
        await query.message.edit_text(f"✅ Voice set to: **{name}**\n*(Multi-speaker tagging in SRT is also supported)*")

    elif data.startswith("trans_"):
        lang = data.replace("trans_", "")
        prompt = get_active_prompt(user_id, lang)
        
        await query.message.reply_text(f"⏳ Translating to {lang.title()} via Gemini AI...")
        success, _, path = await run_translate(user_id, prompt)
        
        if success:
            await context.bot.send_document(query.message.chat_id, open(path, "rb"), caption=f"✅ {lang.title()} Translation")
            kb = [[InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]]
            await context.bot.send_message(query.message.chat_id, "Next Step:", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.message.reply_text("❌ Translation failed.")

    elif data == "trigger_dub":
        p = get_paths(user_id)
        if not os.path.exists(p['srt']):
            await query.message.reply_text("❌ No SRT found. Translate or Upload one first.")
            return

        status = await query.message.reply_text("🚀 Initializing Advanced Audio Engine...")
        success, error = await generate_advanced_dubbing(user_id, p['srt'], p['dub_audio'], state, status)
        
        if success:
            await status.edit_text("✅ Audio Generated!\n🎥 Merging Audio with Video... (Applying Copyright Bypass Filters)")
            
            if os.path.exists(p['input']):
                merge_success, merge_res = await merge_video_audio(user_id)
                if merge_success:
                    await status.delete()
                    try:
                        await context.bot.send_video(
                            query.message.chat_id, open(merge_res, "rb"), 
                            caption="🎬 Final Dubbed Video (Pro Sync)", supports_streaming=True
                        )
                    except Exception as e:
                        await context.bot.send_message(query.message.chat_id, f"Video too large for Telegram. Audio sent instead.")
                        await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"))
                else:
                    await status.edit_text(f"❌ Video Merge Failed. Sending Audio only.")
                    await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"))
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
        clean_temp(user_id)
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
            await status.edit_text("🎙️ Transcribing Audio with Whisper AI...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_whisper_engine, p['audio'], p['srt'])
            caption = "✅ Transcribed by Whisper AI"
        else:
            caption = "✅ Extracted YouTube Auto-Subtitles"

        if os.path.exists(p['srt']):
            await context.bot.send_document(msg.chat_id, document=open(p['srt'], "rb"), caption=caption)

        await status.edit_text("✅ Download & Transcription Done.\nChoose Translation Language:")
        kb = [
            [InlineKeyboardButton("🇲🇲 Burmese", callback_data="trans_burmese"),
             InlineKeyboardButton("🇯🇵 Japanese", callback_data="trans_japanese")]
        ]
        await context.bot.send_message(msg.chat_id, "Translate to:", reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:200]}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text.strip()
    
    if "http" in text and any(x in text.lower() for x in ["youtube.com", "youtu.be", "tiktok.com", "facebook.com"]):
        await process_media(update, context, is_url=True)
    else:
        await msg.reply_text("ℹ️ Please send a YouTube link, Video, Audio, or SRT file.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)

    if msg.document and msg.document.file_name.lower().endswith('.srt'):
        await msg.document.get_file().download_to_drive(p['srt'])
        kb = [[InlineKeyboardButton("🎬 Process Pro Dubbing", callback_data="trigger_dub")]]
        await msg.reply_text("✅ SRT loaded. Ready to Dub!", reply_markup=InlineKeyboardMarkup(kb))
        return

    await process_media(update, context, is_url=False)

if __name__ == "__main__":
    logger.info("Starting Pro Video AI Bot")
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
