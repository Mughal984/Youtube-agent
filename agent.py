"""Daily YouTube agent: Gemini script -> Urdu voice -> real photos -> ~3 min video -> YouTube upload."""
import os, re, json, random, asyncio, pathlib, subprocess, datetime
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import arabic_reshaper
from bidi.algorithm import get_display
import edge_tts

W, H, FPS = 1080, 1920, 30
OUT, TMP = pathlib.Path("out"), pathlib.Path("tmp")
UA = {"User-Agent": "youtube-agent/1.0 (personal educational project)"}
VOICE = os.environ.get("VOICE", "ur-PK-AsadNeural")

NUM_SCENES = 26          # about 6.5 seconds per scene -> about 170 seconds
MAX_SECONDS = 175.0      # hard limit, stays under 3 minutes
USED_URLS = set()        # avoid the same photo twice in one video


def run(cmd):
    subprocess.run(cmd, check=True)


# ---------------- 1. Script from Gemini ----------------
def gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    models = [os.environ.get("GEMINI_MODEL"), "gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest", "gemini-3.5-flash"]
    last = []
    for m in [x for x in models if x]:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0,
                                       "maxOutputTokens": 16000}},
            timeout=180)
        if not r.ok:
            last.append(f"{m}: {r.text[:150]}")
            continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
            return json.loads(text)
        except Exception as e:
            last.append(f"{m}: bad reply {e}")
    raise RuntimeError("Gemini failed: " + " | ".join(last))


def make_prompt(used):
    return f"""You write scripts for a YouTube channel in URDU about interesting facts and history
(Pakistan, Islamic history, world history, surprising true facts). Videos are about 3 minutes long.
Pick ONE new topic. Do NOT repeat these earlier topics: {used}.
Rules: only well-established, true facts; no politics, no sectarian or controversial claims; if unsure, choose another topic.
Tell it as a story with a clear flow: strong hook, background, the main events in order, surprising details, and a closing lesson.
Every scene must add NEW information; never repeat a fact. Use a different photo search in every scene.
Return ONLY JSON with this shape:
{{"topic": "short English topic",
 "title": "catchy Urdu title, max 70 characters",
 "description": "3-4 lines Urdu description",
 "tags": ["8 to 12 tags, mix Urdu and English"],
 "scenes": [{{"text": "1-2 short Urdu sentences, 12 to 16 words, shown as on-screen caption", "speech": "same sentences rewritten ONLY for text-to-speech", "search": "2-4 English words for a real photo search"}}]}}
Rules for "speech": pure Urdu script only; write every number in Urdu words (for example 280 becomes دو سو اسی); no digits, no English words, no abbreviations or symbols;
spell foreign names the way an Urdu speaker pronounces them; use short sentences with ۔ and ، so the voice pauses naturally.
Make exactly {NUM_SCENES} scenes. The whole video must be about 165 seconds when read aloud (never over 175). Scene 1 is a strong hook question.
The last scene asks viewers to follow and subscribe for daily new history. Use Urdu script (not Roman)."""


# ---------------- 2. Real photos ----------------
def commons_photo(q):
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=60, params={
        "action": "query", "format": "json", "generator": "search", "gsrsearch": q + " filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 1400})
    pages = (r.json().get("query", {}).get("pages") or {}).values()
    for p in sorted(pages, key=lambda x: x.get("index", 99)):
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") != "image/jpeg" or ii.get("width", 0) < 900:
            continue
        url = ii.get("thumburl") or ii["url"]
        if url in USED_URLS:
            continue
        md = ii.get("extmetadata", {})
        lic = md.get("LicenseShortName", {}).get("value", "")
        if not re.search(r"CC|public domain|PD", lic, re.I) or re.search(r"-N[CD]", lic):
            continue
        artist = re.sub("<[^>]+>", "", md.get("Artist", {}).get("value", "Unknown")).strip()
        credit = f"{p['title']} by {artist} ({lic}) {ii.get('descriptionurl', '')}"
        return url, credit
    return None


def pixabay_photo(q):
    for orient in ("vertical", "all"):
        r = requests.get("https://pixabay.com/api/", timeout=60, params={
            "key": os.environ["PIXABAY_API_KEY"], "q": q, "image_type": "photo",
            "per_page": 8, "safesearch": "true", "orientation": orient})
        hits = r.json().get("hits", []) if r.ok else []
        hits = [h for h in hits if h["largeImageURL"] not in USED_URLS]
        if hits:
            return random.choice(hits[:4])["largeImageURL"], "Photo from Pixabay (pixabay.com)"
    return None


def get_photo(q, path):
    for finder in (commons_photo, pixabay_photo):
        try:
            found = finder(q)
            if found:
                data = requests.get(found[0], headers=UA, timeout=90)
                data.raise_for_status()
                path.write_bytes(data.content)
                Image.open(path).verify()
                USED_URLS.add(found[0])
                return found[1]
        except Exception as e:
            print(f"  photo source {finder.__name__} failed: {e}")
    return None


# ---------------- 3. Voice ----------------
def tts(text, path):
    async def go():
        await edge_tts.Communicate(text, VOICE, rate="-6%").save(str(path))
    for attempt in range(3):
        try:
            asyncio.run(go())
            return
        except Exception as e:
            print(f"  tts retry {attempt + 1}: {e}")
    raise RuntimeError("Voice generation failed")


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return float(out.strip())


# ---------------- 4. Frames ----------------
def find_font():
    for q in ("Noto Naskh Arabic:bold", "Noto Sans Arabic:bold", "DejaVu Sans:bold"):
        p = subprocess.run(["fc-match", "-f", "%{file}", q], capture_output=True, text=True).stdout.strip()
        if p and os.path.exists(p):
            return p


def make_bg(photo, out):
    if photo and pathlib.Path(photo).exists():
        im = Image.open(photo).convert("RGB")
    else:
        im = Image.new("RGB", (W, H), (40, 50, 90))
    s = max(W / im.width, H / im.height)
    bg = im.resize((int(im.width * s) + 1, int(im.height * s) + 1))
    l, t = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((l, t, l + W, t + H)).filter(ImageFilter.GaussianBlur(28))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.35)
    fs = min(W / im.width, (H * 0.62) / im.height)
    fg = im.resize((int(im.width * fs), int(im.height * fs)))
    bg.paste(fg, ((W - fg.width) // 2, 260 + (int(H * 0.62) - fg.height) // 2))
    bg.save(out, quality=92)


def caption_png(text, path, font_path):
    f = ImageFont.truetype(font_path, 62, layout_engine=ImageFont.Layout.BASIC)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    shaped = lambda s: get_display(arabic_reshaper.reshape(s))
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(shaped(t), font=f) <= 900:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    lh = 104
    top = 1250 - (len(lines) * lh) // 2
    d.rounded_rectangle([50, top - 30, W - 50, top + len(lines) * lh + 10], radius=40, fill=(0, 0, 0, 175))
    for i, ln in enumerate(lines):
        d.text((W / 2, top + i * lh + lh / 2 - 8), shaped(ln), font=f, fill=(255, 255, 255, 255),
               anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
    img.save(path)


def build_scene(bg, cap, audio, dur, out):
    total = dur + 0.5
    frames = int(total * FPS)
    fc = (f"[0:v]scale=2160:3840,zoompan=z='min(zoom+0.0006,1.15)':x='iw/2-(iw/zoom/2)':"
          f"y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}[v];"
          f"[v][1:v]overlay=0:0,format=yuv420p[o]")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(bg), "-i", str(cap), "-i", str(audio),
         "-filter_complex", fc, "-map", "[o]", "-map", "2:a", "-af", "apad=pad_dur=0.5",
         "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", str(FPS),
         "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)])


# ---------------- 5. YouTube upload ----------------
def youtube_upload(video_path, meta):
    cid = os.environ.get("YT_CLIENT_ID")
    secret = os.environ.get("YT_CLIENT_SECRET")
    refresh = os.environ.get("YT_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        print("YouTube secrets not set, skipping upload.")
        return None

    tok = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": secret,
        "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=60)
    tok.raise_for_status()
    access = tok.json()["access_token"]

    # YouTube limits: tags total 500 chars, description 5000 bytes, no < or >
    tags, total = [], 0
    for t in meta["tags"]:
        t = str(t).replace("<", "").replace(">", "").strip()
        if t and total + len(t) + 1 < 450:
            tags.append(t)
            total += len(t) + 1
    desc = meta["description"].replace("<", "").replace(">", "")
    while len(desc.encode("utf-8")) > 4800:
        desc = desc[:-200]

    body = {
        "snippet": {"title": meta["title"].replace("<", "").replace(">", "")[:100],
                    "description": desc, "tags": tags, "categoryId": "27",
                    "defaultLanguage": "ur", "defaultAudioLanguage": "ur"},
        "status": {"privacyStatus": os.environ.get("PRIVACY", "public"),
                   "selfDeclaredMadeForKids": False},
    }
    size = os.path.getsize(video_path)
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {access}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=60)
    if not init.ok:
        raise RuntimeError(f"YouTube init failed: {init.status_code} {init.text[:300]}")
    location = init.headers["Location"]

    with open(video_path, "rb") as f:
        up = requests.put(location, data=f, headers={"Content-Type": "video/mp4",
                                                     "Content-Length": str(size)}, timeout=1200)
    if not up.ok:
        raise RuntimeError(f"YouTube upload failed: {up.status_code} {up.text[:300]}")
    vid = up.json().get("id")
    print("Uploaded: https://www.youtube.com/watch?v=" + str(vid))
    return vid


# ---------------- 6. Main ----------------
def main():
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    hist_file = pathlib.Path("history.json")
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    used = [h["topic"] for h in hist[-80:]]

    data = gemini(make_prompt(used))
    scenes = data["scenes"]
    assert len(scenes) >= 3, "Gemini returned too few scenes"
    print("Topic:", data["topic"], "| scenes:", len(scenes))

    font = find_font()
    # voice first, so we know the real length
    for i, sc in enumerate(scenes):
        sc["audio"] = TMP / f"a{i}.mp3"
        tts(sc.get("speech") or sc["text"], sc["audio"])
        sc["dur"] = duration(sc["audio"])
    # keep the final video under 3 minutes: drop middle scenes if too long
    while len(scenes) > 4 and sum(sc["dur"] + 0.5 for sc in scenes) > MAX_SECONDS:
        scenes.pop(len(scenes) // 2)
    print("Video length: %.1f s" % sum(sc["dur"] + 0.5 for sc in scenes))

    clips, credits = [], []
    for i, sc in enumerate(scenes):
        print(f"Scene {i + 1}/{len(scenes)}")
        photo = TMP / f"p{i}.jpg"
        credit = get_photo(sc["search"], photo)
        if credit and credit not in credits:
            credits.append(credit)
        bg, cap, clip = TMP / f"bg{i}.jpg", TMP / f"cap{i}.png", TMP / f"s{i}.mp4"
        make_bg(photo if credit else None, bg)
        caption_png(sc["text"], cap, font)
        build_scene(bg, cap, sc["audio"], sc["dur"], clip)
        clips.append(clip)

    lst = TMP / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(OUT / "video.mp4")])

    desc = data["description"] + "\n\n" + " ".join("#" + t.replace(" ", "") for t in data["tags"][:5])
    desc += " #Shorts\n\nPhoto credits:\n" + "\n".join(credits)
    meta = {"title": data["title"][:100], "description": desc, "tags": data["tags"], "topic": data["topic"]}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        youtube_upload(OUT / "video.mp4", meta)
    except Exception as e:
        print("UPLOAD ERROR (video is still saved in out/):", e)

    hist.append({"date": datetime.date.today().isoformat(), "topic": data["topic"]})
    hist_file.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Done:", meta["title"])


if __name__ == "__main__":
    main()
