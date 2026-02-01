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
    
    # --- FIXED IMPORT FOR GOOGLE AI ---
    import google.generativeai as genai
    # ----------------------------------

    from faster_whisper import WhisperModel
    logger.info("✅ All libraries imported.")
except ImportError as e:
    logger.critical(f"❌ Missing Library: {e}")
    sys.exit(1)

# -------------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------------
TG_TOKEN = os.getenv("TG_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not TG_TOKEN or not GEMINI_KEY:
    logger.critical("❌ ERROR: API Keys missing. Set TG_TOKEN and GEMINI_KEY env vars.")
    sys.exit(1)

# Configure Gemini
genai.configure(api_key=GEMINI_KEY)

VOICE_LIB = {
    "🇲🇲 Thiha (Male)": "my-MM-ThihaNeural",
    "🇲🇲 Nilar (Female)": "my-MM-NilarNeural",
    "🇯🇵 Nanami (Female)": "ja-JP-NanamiNeural",
    "🇯🇵 Keita (Male)": "ja-JP-KeitaNeural",
    "🇺🇸 Guy (Male)": "en-US-GuyNeural",
    "🇺🇸 Jenny (Female)": "en-US-JennyNeural",
    "🇺🇸 Aria (Good Narrator)": "en-US-AriaNeural",
    "🇬🇧 Sonia (British)": "en-GB-SoniaNeural",
    "🇨🇳 Xiaoxiao (Chinese)": "zh-CN-XiaoxiaoNeural",
    "🇰🇷 SunHi (Korean)": "ko-KR-SunHiNeural",
    "🇹🇭 Premwadee (Thai)": "th-TH-PremwadeeNeural",
}

SRT_RULES = """
**FORMATTING INSTRUCTIONS (STRICT):**
1. The input is an **SRT Subtitle File**.
2. **OUTPUT FORMAT:** You MUST return a valid SRT file.
3. **TIMESTAMPS:** Do NOT change, shift, or remove any timestamps. 
4. **SEQUENCE NUMBERS:** Preserve exact sequence.
"""

BURMESE_STYLE = """
Target Language: Burmese (Myanmar)
Role: Native Burmese professional video narrator.
Guidelines:
• Use smooth, conversational Burmese.
• Do NOT add “ပေါ့” at the end of sentences repeatedly.
• Translate by meaning and emotion, not word-by-word.
• **STRICT RULE:** NO ENGLISH CHARACTERS. Use Burmese pronunciation for loan words (e.g., ငမန်း).
"""

JAPANESE_STYLE = """
Target Language: Japanese
Role: Professional Japanese Video Translator.
Guidelines:
• Translate into natural, spoken Japanese suitable for narration.
• Use polite form (Desu/Masu) unless the context is clearly casual.
• Ensure the flow fits the timing of the subtitles.
• **STRICT RULE:** NO ENGLISH CHARACTERS. Use Katakana for loan words.
"""

DEFAULT_PROMPTS = {
    "burmese": BURMESE_STYLE,
    "japanese": JAPANESE_STYLE,
    "rephrase": "Rephrase this English text to be more clear, natural, and reliable."
}

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

# -------------------------------------------------------------------------
# USER STATE MANAGEMENT
# -------------------------------------------------------------------------
user_prefs = {}
user_modes = {}
chat_histories = {}
user_last_active = {}
user_srt_msgs = {}
user_srt_accum = {}

def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "transcribe_engine": "whisper_dub",
            "dub_voice": "my-MM-ThihaNeural",
            "custom_prompts": {},
            "transcript_format": "srt"
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

def wipe_user_data(user_id):
    clean_temp(user_id)
    user_prefs.pop(user_id, None)
    user_modes.pop(user_id, None)
    chat_histories.pop(user_id, None)
    user_srt_msgs.pop(user_id, None)
    user_srt_accum.pop(user_id, None)

async def send_copyable_message(chat_id, bot, text):
    if not text: return
    MAX_LEN = 4000
    safe_text = text.replace("`", "'")
    for i in range(0, len(safe_text), MAX_LEN):
        chunk = safe_text[i:i + MAX_LEN]
        try:
            await bot.send_message(chat_id=chat_id, text=f"```\n{chunk}\n```", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Message send error: {e}")

# -------------------------------------------------------------------------
# AUDIO PROCESSING
# -------------------------------------------------------------------------
def trim_silence(audio_segment, silence_thresh=-40.0, chunk_size=5):
    if len(audio_segment) < 100: return audio_segment
    start_trim = detect_leading_silence(audio_segment, silence_threshold=silence_thresh, chunk_size=chunk_size)
    end_trim = detect_leading_silence(audio_segment.reverse(), silence_threshold=silence_thresh, chunk_size=chunk_size)
    duration = len(audio_segment)
    return audio_segment[start_trim:duration - end_trim]

def make_audio_crisp(audio_segment):
    clean = audio_segment.high_pass_filter(150)
    return effects.normalize(clean)

async def generate_dubbing(user_id, srt_path, output_path, voice):
    logger.info(f"Starting dubbing for {user_id}")
    try:
        subs = pysrt.open(srt_path)
        final_audio = AudioSegment.empty()
        current_timeline_ms = 0
        DEFAULT_SPEED = 15

        for i, sub in enumerate(subs):
            start_ms = sub.start.ordinal
            end_ms = sub.end.ordinal
            duration_allowed = end_ms - start_ms
            text = sub.text.replace("\n", " ").strip()
            if not text: continue

            # Calculate Speed
            char_count = len(text)
            duration_sec = duration_allowed / 1000.0 or 1
            chars_per_sec = char_count / duration_sec

            if chars_per_sec > 18:
                rate = int(DEFAULT_SPEED + ((chars_per_sec - 18) * 5))
            elif chars_per_sec < 10:
                rate = 10
            else:
                rate = DEFAULT_SPEED
            rate = max(0, min(50, rate))

            # Generate TTS Chunk
            temp_filename = f"temp/{user_id}_chunk_{i}.mp3"
            communicate = edge_tts.Communicate(text, voice, rate=f"+{rate}%", pitch="-2Hz")
            await communicate.save(temp_filename)

            segment = AudioSegment.from_file(temp_filename)
            segment = trim_silence(segment)

            # Fit to timeline
            if len(segment) > duration_allowed + 100:
                factor = min(1.3, len(segment) / duration_allowed)
                segment = segment.speedup(playback_speed=factor, chunk_size=150, crossfade=25)

            if start_ms > current_timeline_ms:
                gap = start_ms - current_timeline_ms
                final_audio += AudioSegment.silent(duration=gap)
                current_timeline_ms += gap

            segment = make_audio_crisp(segment)
            final_audio += segment
            current_timeline_ms += len(segment)

            if os.path.exists(temp_filename):
                os.remove(temp_filename)

        final_audio.export(output_path, format="mp3")
        return True, None
    except Exception as e:
        logger.error(f"Dubbing failed: {e}")
        return False, str(e)

async def merge_video_audio(user_id):
    p = get_paths(user_id)
    if not os.path.exists(p['input']) or not os.path.exists(p['dub_audio']):
        return False, "Input video or Dubbed audio missing."

    # FFmpeg: map 0:v (video from file 0), map 1:a (audio from file 1), shortest ends with video
    cmd = [
        "ffmpeg", "-y",
        "-i", p['input'],
        "-i", p['dub_audio'],
        "-c:v", "copy", 
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        p['final_video']
    ]
    
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, p['final_video']
    except subprocess.CalledProcessError as e:
        return False, str(e)

# -------------------------------------------------------------------------
# WHISPER & GEMINI TRANSCRIBE
# -------------------------------------------------------------------------
def format_timestamp(seconds):
    hours = math.floor(seconds / 3600)
    seconds %= 3600
    minutes = math.floor(seconds / 60)
    seconds %= 60
    milliseconds = round((seconds - math.floor(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{math.floor(seconds):02d},{milliseconds:03d}"

def run_whisper_engine(audio_path, srt_path, txt_path, mode="sub"):
    try:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        model = WhisperModel("small", device=device, compute_type=compute_type)

        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True
        )
        logger.info(f"Detected: {info.language}")

        final_subs = []
        current_words = []
        current_start = None
        
        # Mode settings
        if mode == "sub":
            SOFT_LIMIT = 80
            HARD_LIMIT = 120
        else: # dub mode
            SOFT_LIMIT = 999 
            HARD_LIMIT = 300 # Longer segments for dubbing flow

        HONORIFICS = ["mr.", "mrs.", "dr.", "st.", "prof."]
        all_words = [w for seg in segments for w in seg.words]

        for word in all_words:
            if current_start is None:
                current_start = word.start
            current_words.append(word)

            text_str = " ".join(w.word.strip() for w in current_words)
            current_len = len(text_str)
            clean = word.word.strip().lower()

            has_punct = clean and clean[-1] in ".?!"
            is_honorific = clean in HONORIFICS
            is_end = has_punct and not is_honorific
            
            # Logic split for Sub vs Dub
            if mode == "sub":
                is_comma_break = clean and clean[-1] == "," and current_len > SOFT_LIMIT
                should_break = is_end or is_comma_break or (current_len > HARD_LIMIT)
            else:
                should_break = is_end or (current_len > HARD_LIMIT)

            if should_break:
                start_ts = format_timestamp(current_start)
                end_ts = format_timestamp(word.end)
                final_subs.append({"start": start_ts, "end": end_ts, "text": text_str})
                current_words = []
                current_start = None

        if current_words:
            start_ts = format_timestamp(current_start)
            end_ts = format_timestamp(all_words[-1].end)
            final_subs.append({"start": start_ts, "end": end_ts, "text": " ".join(w.word.strip() for w in current_words)})

        with open(srt_path, "w", encoding="utf-8") as srt, open(txt_path, "w", encoding="utf-8") as txt:
            for i, sub in enumerate(final_subs, 1):
                srt.write(f"{i}\n{sub['start']} --> {sub['end']}\n{sub['text']}\n\n")
                txt.write(f"{sub['text']} ")

        return f"Whisper {mode.title()}"
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return f"Error: {e}"

def run_gemini_transcribe(audio_path, srt_path, txt_path):
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        # Uploading audio to Gemini (or sending bytes directly if supported by method)
        # Note: For large files, File API is better, but here we use simple content generation
        # Only works well for small audio in direct bytes.
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
            
        # Using a simpler text prompt if byte upload is restricted or managing file upload
        # For this script we will assume simple byte passing or use Whisper as primary.
        response = model.generate_content([
            "Transcribe this audio accurately.",
            {"mime_type": "audio/mp3", "data": audio_bytes}
        ])
        
        text = response.text.strip()
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        if os.path.exists(srt_path): os.remove(srt_path)
        return "Gemini Flash"
    except Exception as e:
        logger.error(f"Gemini transcribe error: {e}")
        return "Error"

# -------------------------------------------------------------------------
# TRANSLATION & CHAT
# -------------------------------------------------------------------------
async def run_translate(user_id, prompt_text):
    p = get_paths(user_id)
    source = p['srt'] if os.path.exists(p['srt']) else p['txt'] if os.path.exists(p['txt']) else None
    if not source:
        return False, "No transcription found.", None

    is_srt = source.endswith('.srt')
    model = genai.GenerativeModel('gemini-1.5-flash')

    with open(source, "r", encoding="utf-8") as f:
        content = f.read()

    if is_srt:
        full_prompt = f"{SRT_RULES}\n{prompt_text}\n\n**INPUT SRT:**\n{content}"
        ext = ".srt"
    else:
        full_prompt = f"{prompt_text}\n\nInput:\n{content}"
        ext = ".txt"

    try:
        response = model.generate_content(full_prompt)
        result = response.text.strip().replace("```srt", "").replace("```", "").strip()
        out_path = p['trans_result'] + ext
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
        if is_srt:
            shutil.copy(out_path, p['srt']) # Update main SRT for dubbing
        return True, result, out_path
    except Exception as e:
        return False, str(e), None

async def run_chat_gemini(user_id, text):
    current_time = time.time()
    if user_id in user_last_active and (current_time - user_last_active[user_id] > 86400):
        chat_histories[user_id] = []
    user_last_active[user_id] = current_time

    if user_id not in chat_histories:
        chat_histories[user_id] = []

    model = genai.GenerativeModel('gemini-1.5-flash')

    try:
        chat = model.start_chat(history=chat_histories[user_id])
        response = chat.send_message(text)
        chat_histories[user_id] = chat.history
        return response.text
    except Exception as e:
        return f"Chat error: {e}"

# -------------------------------------------------------------------------
# TELEGRAM HANDLERS
# -------------------------------------------------------------------------
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Dashboard"),
        BotCommand("translate", "Translate"),
        BotCommand("dub", "Dub Video"),
        BotCommand("heygemini", "Chat AI"),
        BotCommand("clearall", "Reset All"),
        BotCommand("srt", "Multi-part SRT"),
        BotCommand("end", "Finish input")
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Unknown")
    
    text = (
        f"👋 **Video AI Studio**\n\n"
        f"Settings:\n"
        f"Engine: `{state['transcribe_engine']}`\n"
        f"Voice: `{voice_name}`\n\n"
        "Choose an action:"
    )

    keyboard = [
        [InlineKeyboardButton("Sub Mode", callback_data="set_eng_sub"),
         InlineKeyboardButton("Dub Mode", callback_data="set_eng_dub")],
        [InlineKeyboardButton("Voices", callback_data="cmd_voices"),
         InlineKeyboardButton("Chat AI", callback_data="cmd_chat")],
        [InlineKeyboardButton("Prompts", callback_data="menu_settings"),
         InlineKeyboardButton("Clear Data", callback_data="cmd_clear")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def voices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    row = []
    for name, code in VOICE_LIB.items():
        row.append(InlineKeyboardButton(name, callback_data=f"set_voice_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    text = "🗣️ **Select Voice**"
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data

    if data == "cmd_start":
        await start(update, context)

    elif data == "set_eng_sub":
        state['transcribe_engine'] = "whisper_sub"
        await query.answer("Engine: Whisper Subtitle Mode")
        await start(update, context)

    elif data == "set_eng_dub":
        state['transcribe_engine'] = "whisper_dub"
        await query.answer("Engine: Whisper Dubbing Mode")
        await start(update, context)

    elif data == "cmd_voices":
        await voices_command(update, context)

    elif data == "cmd_chat":
        user_modes[user_id] = "chat_gemini"
        await query.message.reply_text("🤖 Gemini Chat ON\n`/cancel` to exit")
        await query.answer()

    elif data == "cmd_clear":
        wipe_user_data(user_id)
        await query.answer("Data cleared")
        await query.message.reply_text("🧹 Workspace cleared")

    elif data.startswith("set_voice_"):
        voice = data.replace("set_voice_", "")
        state['dub_voice'] = voice
        name = next((k for k, v in VOICE_LIB.items() if v == voice), "Custom")
        await query.message.edit_text(f"Voice set to: **{name}**")

    elif data == "menu_settings":
        keyboard = [
            [InlineKeyboardButton("View Prompts", callback_data="st_view")],
            [InlineKeyboardButton("Edit Burmese", callback_data="st_edit_burmese"),
             InlineKeyboardButton("Edit Japanese", callback_data="st_edit_japanese")],
            [InlineKeyboardButton("Back", callback_data="cmd_start")]
        ]
        await query.message.edit_text("⚙️ Prompt Settings", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "st_view":
        await send_copyable_message(query.message.chat_id, context.bot, f"Burmese:\n{get_active_prompt(user_id, 'burmese')}")
        await send_copyable_message(query.message.chat_id, context.bot, f"Japanese:\n{get_active_prompt(user_id, 'japanese')}")

    elif data.startswith("st_edit_"):
        mode = data.replace("st_edit_", "")
        user_modes[user_id] = f"edit_prompt_{mode}"
        await query.message.edit_text(f"Send new **{mode.title()}** prompt:")

    # Translation Logic
    elif data.startswith("trans_"):
        lang = data.replace("trans_", "")
        prompt_key = lang if lang in ["burmese", "japanese"] else "burmese"
        prompt = get_active_prompt(user_id, prompt_key)
        
        await query.message.reply_text(f"Translating to {lang.title()}...")
        success, _, path = await run_translate(user_id, prompt)
        
        if success:
            await context.bot.send_document(query.message.chat_id, open(path, "rb"), caption=f"{lang.title()} Translation")
            kb = [[InlineKeyboardButton("Dub Video Now", callback_data="trigger_dub")]]
            await context.bot.send_message(query.message.chat_id, "Next Step:", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.message.reply_text("Translation failed.")

    # Dubbing & Video Merge Logic
    elif data == "trigger_dub":
        p = get_paths(user_id)
        if not os.path.exists(p['srt']):
            await query.message.reply_text("No SRT found. Translate first.")
            return

        voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Voice")
        status = await query.message.reply_text(f"1. Dubbing Audio ({voice_name})...")
        
        # 1. Generate Dubbed Audio
        success, error = await generate_dubbing(user_id, p['srt'], p['dub_audio'], state['dub_voice'])
        
        if success:
            await status.edit_text("2. Merging Audio with Video...")
            
            # 2. Merge with Video if input exists
            if os.path.exists(p['input']):
                merge_success, merge_res = await merge_video_audio(user_id)
                if merge_success:
                    await status.delete()
                    await context.bot.send_video(
                        query.message.chat_id, 
                        open(merge_res, "rb"), 
                        caption=f"🎬 Final Dubbed Video ({voice_name})",
                        width=1280, height=720,
                        supports_streaming=True
                    )
                else:
                    await status.edit_text(f"Video Merge Failed ({merge_res}). Sending Audio only.")
                    await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), title=f"Dubbed_{voice_name}")
            else:
                await status.delete()
                await context.bot.send_audio(query.message.chat_id, open(p['dub_audio'], "rb"), title=f"Dubbed_{voice_name}")
            
            # Cleanup
            user_srt_msgs[user_id] = []
        else:
            await status.edit_text(f"Dubbing Failed: {error}")

    await query.answer()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    text = msg.text.strip()
    mode = user_modes.get(user_id)

    if text == "/cancel":
        user_modes[user_id] = None
        await msg.reply_text("Mode cancelled")
        return

    if mode == "srt_accumulate":
        user_srt_accum[user_id] = (user_srt_accum.get(user_id, "") + "\n" + text).strip()
        user_srt_msgs[user_id].append(msg.message_id)
        await msg.reply_text("Part added. Send more or /end")
        return

    if mode and mode.startswith("edit_prompt_"):
        key = mode.replace("edit_prompt_", "")
        get_user_state(user_id).setdefault("custom_prompts", {})[key] = text
        user_modes[user_id] = None
        await msg.reply_text(f"{key.title()} prompt updated")
        return

    if mode == "chat_gemini":
        resp = await run_chat_gemini(user_id, text)
        await send_copyable_message(msg.chat_id, context.bot, resp)
        return

    if re.search(r'\d{2}:\d{2}:\d{2},\d{3} -->', text):
        try:
            # Quick SRT validation
            subs = pysrt.open(StringIO(text))
            dur = subs[-1].end
            dur_str = f"{dur.hours:02}:{dur.minutes:02}:{dur.seconds:02},{dur.milliseconds:03}"
            kb = [[InlineKeyboardButton("Confirm", callback_data="confirm_srt")]]
            await msg.reply_text(f"SRT Detected. Duration: {dur_str}", reply_markup=InlineKeyboardMarkup(kb))
            # Save strictly as SRT
            p = get_paths(user_id)
            with open(p['srt'], "w", encoding="utf-8") as f: f.write(text)
        except Exception as e:
            await msg.reply_text(f"Invalid SRT: {e}")
        return

    if "http" in text and any(x in text.lower() for x in ["youtube.com", "youtu.be", "tiktok.com"]):
        await process_media(update, context, is_url=True)
        return

    # Fallback: Save as text for translation
    if len(text) > 10:
        p = get_paths(user_id)
        with open(p['txt'], "w", encoding="utf-8") as f:
            f.write(text)
        await msg.reply_text("Text saved. Use /translate")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)

    if msg.document and msg.document.file_name.lower().endswith('.srt'):
        await msg.document.get_file().download_to_drive(p['srt'])
        kb = [[InlineKeyboardButton("Dub Now", callback_data="trigger_dub")]]
        await msg.reply_text("SRT loaded.", reply_markup=InlineKeyboardMarkup(kb))
        return

    if msg.document and msg.document.file_name.lower().endswith('.txt'):
        await msg.document.get_file().download_to_drive(p['txt'])
        await msg.reply_text("TXT loaded. Use /translate")
        return

    await process_media(update, context, is_url=False)

async def process_media(update, context, is_url):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)
    state = get_user_state(user_id)

    status = await msg.reply_text("⏳ Processing Media...")

    try:
        clean_temp(user_id)
        has_subs = False

        if is_url:
            url = msg.text.strip()
            await status.edit_text("1. Downloading Video...")
            
            # Download VIDEO (ensure mp4)
            subprocess.run([
                "yt-dlp", "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4",
                "--out", p['input'], 
                url
            ], check=True)

            # Check/Download Subs
            await status.edit_text("2. Checking subtitles...")
            subprocess.run([
                "yt-dlp", "--write-subs", "--write-auto-sub",
                "--convert-subs", "srt", "--sub-langs", "en,*",
                "--skip-download", "-o", f"downloads/{user_id}_video", url
            ], capture_output=True)

            subs_files = glob.glob(f"downloads/{user_id}_video*.srt")
            if subs_files:
                shutil.move(subs_files[0], p['srt'])
                has_subs = True
            
            # Extract Audio for transcription
            subprocess.run(["ffmpeg", "-y", "-i", p['input'], "-vn", "-acodec", "libmp3lame", "-q:a", "2", p['audio']], check=True)

        else:
            # Direct file upload
            if msg.video and msg.video.file_size > 50_000_000:
                await status.edit_text("Video > 50 MB (Telegram Limit). Send YouTube link instead.")
                return

            file = await (msg.video or msg.document or msg.audio).get_file()
            # If audio only, we can't output video later
            is_video_input = True
            if msg.audio or (msg.document and 'audio' in msg.document.mime_type):
                is_video_input = False
                await status.edit_text("⚠️ Audio input detected. Output will be Audio only.")

            await file.download_to_drive(p['input'])
            subprocess.run(["ffmpeg", "-y", "-i", p['input'], "-vn", "-acodec", "libmp3lame", "-q:a", "2", p['audio']], check=True)

        if not has_subs:
            await status.edit_text("3. Transcribing...")
            loop = asyncio.get_event_loop()
            engine_mode = "dub" if state['transcribe_engine'] == "whisper_dub" else "sub"
            
            if "whisper" in state['transcribe_engine']:
                engine = await loop.run_in_executor(None, run_whisper_engine, p['audio'], p['srt'], p['txt'], engine_mode)
            else:
                engine = await loop.run_in_executor(None, run_gemini_transcribe, p['audio'], p['srt'], p['txt'])
            
            caption = f"Transcribed by {engine}"
        else:
            caption = "Used YouTube Subtitles"

        # Send Transcript/SRT back to user
        doc_to_send = p['srt'] if os.path.exists(p['srt']) else p['txt']
        if os.path.exists(doc_to_send):
            await context.bot.send_document(msg.chat_id, document=open(doc_to_send, "rb"), caption=caption)

        await status.edit_text("Done ✓\nSelect Translation Language:")
        kb = [
            [InlineKeyboardButton("🇲🇲 Burmese", callback_data="trans_burmese"),
             InlineKeyboardButton("🇯🇵 Japanese", callback_data="trans_japanese")]
        ]
        await context.bot.send_message(msg.chat_id, "Translate to:", reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        logger.error(f"Process error: {e}")
        await status.edit_text(f"Error: {str(e)[:200]}")

async def start_srt_accum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_modes[user_id] = "srt_accumulate"
    user_srt_accum[user_id] = ""
    user_srt_msgs[user_id] = []
    await update.message.reply_text("SRT multi-part mode ON\nSend parts, finish with /end")

async def end_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_modes.get(user_id) != "srt_accumulate":
        await update.message.reply_text("Not in SRT mode")
        return

    content = user_srt_accum.get(user_id, "").strip()
    p = get_paths(user_id)
    with open(p['srt'], "w", encoding="utf-8") as f: f.write(content)
    
    user_modes[user_id] = None
    kb = [[InlineKeyboardButton("Dub Now", callback_data="trigger_dub")]]
    await update.message.reply_text("SRT Assembled.", reply_markup=InlineKeyboardMarkup(kb))

if __name__ == "__main__":
    logger.info("Starting Video AI Bot")
    try:
        app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("voices", voices_command))
        app.add_handler(CommandHandler("translate", lambda u, c: u.message.reply_text("Translate to:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Burmese", callback_data="trans_burmese"),
             InlineKeyboardButton("Japanese", callback_data="trans_japanese")]
        ]))))
        app.add_handler(CommandHandler("dub", lambda u, c: callback_handler(u, c))) 
        app.add_handler(CommandHandler("heygemini", lambda u, c: u.message.reply_text("Chat mode ON\n/cancel to exit") or setattr(user_modes, u.effective_user.id, "chat_gemini")))
        app.add_handler(CommandHandler("clearall", lambda u, c: wipe_user_data(u.effective_user.id) or u.message.reply_text("Cleared")))
        app.add_handler(CommandHandler("srt", start_srt_accum))
        app.add_handler(CommandHandler("end", end_input))
        app.add_handler(CommandHandler("cancel", lambda u, c: setattr(user_modes, u.effective_user.id, None) or u.message.reply_text("Cancelled")))

        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
        app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO, file_handler))

        logger.info("Polling...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.critical(f"Fatal: {e}")
