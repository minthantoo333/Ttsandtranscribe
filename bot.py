# ========================================================
# ALL-IN-ONE Telegram Bot (UI Improved + SRT Text Support)
# ========================================================

import os, logging, threading, subprocess, re
from http.server import HTTPServer, BaseHTTPRequestHandler

import edge_tts
import pysrt
from pydub import AudioSegment
import google.generativeai as genai
from faster_whisper import WhisperModel

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ================= CONFIG =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
DEFAULT_VOICE = "my-MM-ThihaNeural"
MAX_SPEED = 1.5

genai.configure(api_key=GEMINI_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")

whisper_model = WhisperModel("small", device="cpu", compute_type="int8")

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= VOICES =================
VOICE_CATALOG = {
    "my-thiha": "my-MM-ThihaNeural",
    "my-nilar": "my-MM-NilarNeural",
    "en-jenny": "en-US-JennyNeural",
    "en-guy": "en-US-GuyNeural",
    "en-aria": "en-US-AriaNeural",
    "uk-libby": "en-GB-LibbyNeural",
    "jp-nanami": "ja-JP-NanamiNeural",
    "kr-sunhi": "ko-KR-SunHiNeural",
    "zh-xiaoxiao": "zh-CN-XiaoxiaoNeural",
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
    return max(0.4, len(text)/14)

def preprocess_text(text):
    return text.replace("။","။\n").replace(".",".\n").strip()

def is_srt_text(text):
    return bool(re.search(r"\d+\s*\n\d\d:\d\d:\d\d,\d\d\d\s-->", text))

# ================= GEMINI =================
async def shorten_text(text, target_sec):
    try:
        r = gemini.generate_content(
            f"Shorten to fit {target_sec:.1f}s, natural speech:\n{text}"
        )
        return r.text.strip()
    except:
        return text

# ================= SRT → AUDIO =================
async def srt_to_audio(srt_file, output_file, voice):
    subs = pysrt.open(srt_file)
    audio = AudioSegment.silent(0)
    cursor = 0

    for i, sub in enumerate(subs):
        start = srt_time_to_ms(sub.start)
        end = srt_time_to_ms(sub.end)
        slot_ms = end - start
        text = preprocess_text(sub.text)

        if start > cursor:
            audio += AudioSegment.silent(start - cursor)
            cursor = start

        est = estimate_seconds(text)
        if est > slot_ms/1000 * MAX_SPEED:
            text = await shorten_text(text, slot_ms/1000)

        temp = f"_seg{i}.wav"
        await edge_tts.Communicate(text, voice).save(temp)
        seg = AudioSegment.from_file(temp)
        os.remove(temp)

        audio += seg[:slot_ms]
        cursor += len(seg)

    audio.export(output_file, format="mp3")

# ================= TRANSCRIPTION =================
def extract_audio(input_file, output_wav):
    subprocess.run(
        ["ffmpeg","-y","-i",input_file,"-ar","16000","-ac","1",output_wav],
        check=True
    )

def transcribe_to_txt_srt(wav, txt, srt):
    segments, _ = whisper_model.transcribe(wav, beam_size=5, vad_filter=True)

    with open(txt,"w",encoding="utf-8") as t, open(srt,"w",encoding="utf-8") as s:
        i = 1
        for seg in segments:
            t.write(seg.text.strip()+"\n")
            s.write(f"{i}\n{seg.start:.3f} --> {seg.end:.3f}\n{seg.text.strip()}\n\n")
            i += 1

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["voice"] = DEFAULT_VOICE
    context.user_data["mode"] = "tts"

    await update.message.reply_text(
        "🤖 **AI Media Bot Ready**\n\n"
        "🗣 Text → Voice\n"
        "🎬 SRT (file or text) → Audio\n"
        "🎥 Video/Audio → TXT + SRT\n\n"
        "⚙ Commands:\n"
        "/voice – choose voice\n"
        "/ssml – SSML mode",
        parse_mode="Markdown"
    )

async def ssml_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "ssml"
    await update.message.reply_text("🧠 **SSML MODE ON**\nSend SSML markup.")

async def set_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        msg = "🎙 **Available Voices**\n\n"
        msg += "\n".join([f"• `{k}`" for k in VOICE_CATALOG])
        msg += "\n\nUse:\n`/voice en-jenny`"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    key = context.args[0].lower()
    if key not in VOICE_CATALOG:
        await update.message.reply_text("❌ Voice not found.")
        return

    context.user_data["voice"] = VOICE_CATALOG[key]
    await update.message.reply_text(f"✅ Voice set to `{VOICE_CATALOG[key]}`", parse_mode="Markdown")

# ================= HANDLERS =================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    text = update.message.text

    # SRT pasted as text
    if is_srt_text(text):
        await update.message.reply_text("🎬 **Detected SRT text – generating audio...**")
        srt_path = "temp.srt"
        out = "srt_audio.mp3"
        open(srt_path,"w",encoding="utf-8").write(text)
        await srt_to_audio(srt_path, out, voice)
        await update.message.reply_audio(open(out,"rb"))
        os.remove(srt_path)
        os.remove(out)
        return

    # Normal TTS / SSML
    out = "tts.mp3"
    if context.user_data.get("mode") == "ssml":
        await edge_tts.Communicate(text, voice, ssml=True).save(out)
        context.user_data["mode"] = "tts"
    else:
        await edge_tts.Communicate(preprocess_text(text), voice).save(out)

    await update.message.reply_audio(open(out,"rb"))
    os.remove(out)

async def handle_srt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Processing SRT file...")
    srt = "file.srt"
    out = "srt_audio.mp3"
    await update.message.document.get_file().download_to_drive(srt)
    await srt_to_audio(srt, out, context.user_data.get("voice", DEFAULT_VOICE))
    await update.message.reply_audio(open(out,"rb"))
    os.remove(srt)
    os.remove(out)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎧 Transcribing media...")
    file = update.message.video or update.message.audio or update.message.voice
    tg = await file.get_file()

    inp = "input"
    wav = "audio.wav"
    txt = "out.txt"
    srt = "out.srt"

    await tg.download_to_drive(inp)
    extract_audio(inp, wav)
    transcribe_to_txt_srt(wav, txt, srt)

    await update.message.reply_document(open(txt,"rb"))
    await update.message.reply_document(open(srt,"rb"))

    for f in [inp,wav,txt,srt]:
        os.remove(f)

# ================= MAIN =================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ssml", ssml_mode))
    app.add_handler(CommandHandler("voice", set_voice))

    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_srt_file))
    app.add_handler(MessageHandler(filters.VIDEO | filters.AUDIO | filters.VOICE, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    threading.Thread(target=run_web, daemon=True).start()
    print("🤖 Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()