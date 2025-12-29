# ========================================================
# ALL-IN-ONE Telegram Bot:
# TTS + SSML + SRT → Audio + Video/Audio → TXT+SRT
# ========================================================

import os, logging, threading, subprocess
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
    return max(0.4, len(text)/14)

def preprocess_text(text):
    return text.replace("။","။\n").replace(".",".\n").strip()

def format_srt_time(seconds):
    ms = int((seconds % 1)*1000)
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

# ================= GEMINI SHORTENER =================
async def shorten_text(text, target_sec):
    prompt = f"""
Shorten the following subtitle to fit {target_sec:.1f} seconds spoken.
Rules:
- Keep meaning
- Natural spoken language
- One sentence
- No new info

Text:
{text}
"""
    try:
        r = gemini.generate_content(prompt)
        return r.text.strip()
    except:
        return text

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
            i+=1
            continue
        if start_ms > cursor:
            final_audio += AudioSegment.silent(start_ms-cursor)
            cursor = start_ms
        est = estimate_seconds(text)
        if est > slot_sec*MAX_SPEED:
            text = await shorten_text(text, slot_sec)
            est = estimate_seconds(text)
        if est > slot_sec*MAX_SPEED and i+1<len(subs):
            subs[i+1].text = text + " " + subs[i+1].text
            i+=1
            continue
        rate = 0
        if est > slot_sec:
            rate = min(int((est/slot_sec-1)*100), int((MAX_SPEED-1)*100))
        rate_str = f"+{rate}%"
        temp = f"_seg_{i}.wav"
        await edge_tts.Communicate(text=text, voice=voice, rate=rate_str).save(temp)
        seg = AudioSegment.from_file(temp)
        os.remove(temp)
        if len(seg) > slot_ms:
            seg = seg[:slot_ms]
        final_audio += seg
        cursor += len(seg)
        i+=1
    final_audio.export(output_file, format="mp3")

# ================= VIDEO/AUDIO → TXT+SRT =================
def extract_audio(input_file, output_wav):
    subprocess.run([
        "ffmpeg","-y","-i",input_file,"-ar","16000","-ac","1",output_wav
    ], check=True)

def transcribe_to_txt_srt(wav_path, txt_out, srt_out):
    segments, info = whisper_model.transcribe(wav_path, beam_size=5, vad_filter=True)
    # TXT
    with open(txt_out,"w",encoding="utf-8") as f:
        for seg in segments:
            f.write(seg.text.strip()+"\n")
    # SRT
    with open(srt_out,"w",encoding="utf-8") as f:
        idx=1
        for seg in segments:
            start,end=seg.start,seg.end
            f.write(f"{idx}\n")
            f.write(f"{format_srt_time(start)} --> {format_srt_time(end)}\n")
            f.write(seg.text.strip()+"\n\n")
            idx+=1

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["voice"] = DEFAULT_VOICE
    context.user_data["mode"] = "tts"
    await update.message.reply_text(
        "👋 Bot ready!\n\n"
        "🗣 Send text → TTS\n"
        "🎬 Upload .srt → Lip-sync audio\n"
        "🧠 /ssml → SSML mode\n"
        "🎥 Upload video/audio → TXT + SRT transcription\n"
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

# ================= MESSAGE HANDLERS =================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = context.user_data.get("voice",DEFAULT_VOICE)
    mode = context.user_data.get("mode","tts")
    out = f"tts_{update.message.from_user.id}.mp3"
    if mode=="ssml":
        await edge_tts.Communicate(update.message.text,voice,ssml=True).save(out)
        context.user_data["mode"]="tts"
    else:
        await edge_tts.Communicate(preprocess_text(update.message.text),voice).save(out)
    await update.message.reply_audio(audio=open(out,"rb"))
    os.remove(out)

async def handle_srt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Processing SRT...")
    srt_path = f"srt_{update.message.from_user.id}.srt"
    out = f"srt_{update.message.from_user.id}.mp3"
    await update.message.document.get_file().download_to_drive(srt_path)
    await srt_to_audio(srt_path,out,context.user_data.get("voice",DEFAULT_VOICE))
    await update.message.reply_audio(audio=open(out,"rb"),caption="✅ SRT → Audio")
    os.remove(srt_path)
    os.remove(out)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎧 Transcribing...")
    file = update.message.video or update.message.audio or update.message.voice
    if not file: return
    tg_file = await file.get_file()
    input_path = f"input_{update.message.from_user.id}"
    wav_path = f"audio_{update.message.from_user.id}.wav"
    await tg_file.download_to_drive(input_path)
    extract_audio(input_path,wav_path)
    txt_out = f"transcript_{update.message.from_user.id}.txt"
    srt_out = f"transcript_{update.message.from_user.id}.srt"
    transcribe_to_txt_srt(wav_path,txt_out,srt_out)
    await update.message.reply_document(open(txt_out,"rb"))
    await update.message.reply_document(open(srt_out,"rb"))
    for f in [input_path,wav_path,txt_out,srt_out]: os.remove(f)
    await msg.delete()

# ================= MAIN =================
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("ssml",ssml_mode))
    app.add_handler(CommandHandler("voice",set_voice))
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"),handle_srt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.AUDIO | filters.VOICE, handle_media))
    threading.Thread(target=run_web,daemon=True).start()
    print("🤖 Bot running...")
    app.run_polling()

if __name__=="__main__":
    main()