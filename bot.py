# ========================================================
# ALL-IN-ONE Telegram Bot:
# TTS + SSML + SRT → Audio (Professional Handling)
# ========================================================

import os, logging, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess

import edge_tts
import pysrt
from pydub import AudioSegment

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ================= CONFIG =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
DEFAULT_VOICE = "my-MM-ThihaNeural"
MAX_SPEED = 1.5

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= VOICE CATALOG =================
VOICE_CATALOG = {
    # Burmese
    "my-thiha": "my-MM-ThihaNeural",
    "my-nilar": "my-MM-NilarNeural",

    # English US
    "en-jenny": "en-US-JennyNeural",
    "en-guy": "en-US-GuyNeural",
    "en-aria": "en-US-AriaNeural",
    "en-ryan": "en-US-RyanNeural",
    "en-davis": "en-US-DavisNeural",

    # English UK
    "uk-libby": "en-GB-LibbyNeural",
    "uk-ryan": "en-GB-RyanNeural",

    # Japanese
    "jp-nanami": "ja-JP-NanamiNeural",
    "jp-keita": "ja-JP-KeitaNeural",

    # Korean
    "kr-sunhi": "ko-KR-SunHiNeural",
    "kr-injoon": "ko-KR-InJoonNeural",

    # Chinese
    "zh-xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "zh-yunxi": "zh-CN-YunxiNeural",

    # Hindi
    "hi-swara": "hi-IN-SwaraNeural",
    "hi-madhur": "hi-IN-MadhurNeural",

    # French
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

# ================= UTIL =================
def srt_time_to_ms(t):
    return (t.hours*3600 + t.minutes*60 + t.seconds)*1000 + t.milliseconds

def estimate_seconds(text):
    return max(0.4, len(text)/14)  # ~14 chars/sec

def preprocess_text(text):
    return text.replace("။","။\n").replace(".",".\n").strip()

# ================= SRT → AUDIO =================
async def srt_to_audio(srt_file, output_file, voice):
    subs = pysrt.open(srt_file)
    final_audio = AudioSegment.silent(0)
    cursor = 0
    i = 0

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

        if start_ms > cursor:
            final_audio += AudioSegment.silent(start_ms - cursor)
            cursor = start_ms

        est = estimate_seconds(text)

        # -------- CASE HANDLING --------
        if est <= slot_sec:
            # Case A: fits naturally
            rate = 0
            temp_text = text
        elif est <= slot_sec * MAX_SPEED:
            # Case B: slightly longer → speed up
            rate = min(int((est/slot_sec - 1) * 100), int((MAX_SPEED - 1) * 100))
            temp_text = text
        else:
            # Case C: too long → merge or split
            rate = 0
            next_i = i + 1
            merged_text = text
            merged_slot_ms = slot_ms

            # Try merge with next subtitle if gap small
            if next_i < len(subs):
                next_sub = subs[next_i]
                gap = srt_time_to_ms(next_sub.start) - end_ms
                if gap <= 500:  # ≤0.5 sec gap
                    merged_text += " " + preprocess_text(next_sub.text)
                    merged_slot_ms += srt_time_to_ms(next_sub.end) - srt_time_to_ms(next_sub.start) + gap
                    i += 1  # skip next subtitle
            temp_text = merged_text
            slot_ms = merged_slot_ms
            slot_sec = slot_ms / 1000
            est = estimate_seconds(temp_text)
            
            # Still too long? Split by punctuation
            if est > slot_sec * MAX_SPEED:
                puncts = [".", "!", "?", "၊", ","]
                for p in puncts:
                    if p in temp_text:
                        parts = temp_text.split(p)
                        first_part = parts[0] + p
                        remaining = p.join(parts[1:]).strip()
                        temp_text = first_part
                        # Carry remaining to next slot
                        if remaining:
                            new_sub = pysrt.SubRipItem(
                                index=sub.index+1,
                                start=sub.end,
                                end=sub.end,  # auto adjust later
                                text=remaining
                            )
                            subs.insert(i+1, new_sub)
                        break

        # Generate audio
        rate_str = f"+{rate}%"
        temp_file = f"_seg_{i}.wav"
        await edge_tts.Communicate(text=temp_text, voice=voice, rate=rate_str).save(temp_file)
        seg = AudioSegment.from_file(temp_file)
        os.remove(temp_file)

        # Trim if slightly over slot
        if len(seg) > slot_ms:
            seg = seg[:slot_ms]

        final_audio += seg
        cursor += len(seg)
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
        "📌 /voice → select voice"
    )

async def ssml_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"]="ssml"
    await update.message.reply_text(
        "🧠 SSML MODE ON\nSend SSML markup now."
    )

async def set_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        msg="🎙 Available voices:\n"
        for k in VOICE_CATALOG: msg+=f"• {k}\n"
        msg+="\nUse:\n/voice en-jenny"
        await update.message.reply_text(msg)
        return
    key = context.args[0].lower()
    if key not in VOICE_CATALOG:
        await update.message.reply_text("❌ Voice not found. Use /voice to list.")
        return
    context.user_data["voice"]=VOICE_CATALOG[key]
    await update.message.reply_text(f"✅ Voice set to `{VOICE_CATALOG[key]}`",parse_mode="Markdown")

async def srt_text_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle SRT Text Mode"""
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
    
    # If SRT-text mode ON and message looks like SRT
    if srt_mode and any("-->" in line for line in text.splitlines()):
        await update.message.reply_text("🎬 Processing SRT text...")
        srt_path = f"srt_{update.message.from_user.id}.srt"
        out = f"srt_{update.message.from_user.id}.mp3"
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(text)
        await srt_to_audio(srt_path, out, voice)
        await update.message.reply_audio(audio=open(out, "rb"), caption="✅ SRT Text → Audio")
        os.remove(srt_path)
        os.remove(out)
        return

    # Normal TTS
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
    app.add_handler(CommandHandler("voice", set_voice))
    app.add_handler(CommandHandler("srtsms", srt_text_mode))
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_srt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    threading.Thread(target=run_web, daemon=True).start()
    print("🤖 Bot running...")
    app.run_polling()

if __name__=="__main__":
    main()