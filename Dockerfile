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
        "-c:v", "libx264", 
        "-preset", "ultrafast",  # ⚠️ Render Free Plan အတွက် အရေးကြီးဆုံး (RAM မစားအောင်လုပ်ပေးသည်)
        "-threads", "1",         # ⚠️ CPU Thread ၁ ခုတည်းသာသုံးရန် (Crash မဖြစ်စေရန်)
        "-crf", "28",            # File Size ကို ထိန်းပေးရန်
        "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1",  
        p['final_video']
    ]
    try:
        await run_async_cmd(*cmd)
        return True, p['final_video']
    except Exception as e:
        return False, str(e)
