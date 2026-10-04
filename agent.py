"""Daily YouTube Shorts agent: Gemini script -> Urdu voice -> real photos -> video."""
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
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0}},
            timeout=120)
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
    return f"""You write scripts for a YouTube Shorts channel in URDU about interesting facts and history
(Pakistan, Islamic history, world history, surprising true facts).
Pick ONE new topic. Do NOT repeat these earlier topics: {used}.
Rules: only well-established, true facts; no politics, no sectarian or controversial claims; if unsure, choose another topic.
Return ONLY JSON with this shape:
{{"topic": "short English topic",
 "title": "catchy Urdu title, max 70 characters",
 "description": "2-3 lines Urdu description",
 "tags": ["8 to 12 tags, mix Urdu and English"],
 "scenes": [{{"text": "1-2 short Urdu sentences, 12 to 16 words, shown as on-screen caption", "speech": "same sentences rewritten ONLY for text-to-speech", "search": "2-4 English words for a real photo search"}}]}}
Rules for "speech": pure Urdu script only; write every number in Urdu words (for example 280 becomes دو سو اسی); no digits, no English words, no abbreviations or symbols;
spell foreign names the way an Urdu speaker pronounces them; use short sentences with ۔ and ، so the voice pauses naturally.
Make exactly 9 scenes. The whole video must be about 55 seconds when read aloud (never over 58). Scene 1 is a strong hook question.
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
        md = ii.get("extmetadata", {})
        lic = md.get("LicenseShortName", {}).get("value", "")
        if not re.search(r"CC|public domain|PD", lic, re.I) or re.search(r"-N[CD]", lic):
            continue
        artist = re.sub("<[^>]+>", "", md.get("Artist", {}).get("value", "Unknown")).strip()
        credit = f"{p['title']} by {artist} ({lic}) {ii.get('descriptionurl', '')}"
        return ii.get("thumburl") or ii["url"], credit
    return None


def pixabay_photo(q):
    for orient in ("vertical", "all"):
        r = requests.get("https://pixabay.com/api/", timeout=60, params={
            "key": os.environ["PIXABAY_API_KEY"], "q": q, "image_type": "photo",
            "per_page": 5, "safesearch": "true", "orientation": orient})
        hits = r.json().get("hits", []) if r.ok else []
        if hits:
            return random.choice(hits[:3])["largeImageURL"], "Photo from Pixabay (pixabay.com)"
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


# ---------------- 5. Main ----------------
def main():
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    hist_file = pathlib.Path("history.json")
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    used = [h["topic"] for h in hist[-80:]]

    data = gemini(make_prompt(used))
    scenes = data["scenes"]
    assert len(scenes) >= 3, "Gemini returned too few scenes"
    print("Topic:", data["topic"])

    font = find_font()
    # voice first, so we know the real length
    for i, sc in enumerate(scenes):
        sc["audio"] = TMP / f"a{i}.mp3"
        tts(sc.get("speech") or sc["text"], sc["audio"])
        sc["dur"] = duration(sc["audio"])
    # keep the final video under 59 seconds: drop middle scenes if too long
    while len(scenes) > 4 and sum(sc["dur"] + 0.5 for sc in scenes) > 58.5:
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

    hist.append({"date": datetime.date.today().isoformat(), "topic": data["topic"]})
    hist_file.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Done:", meta["title"])


if __name__ == "__main__":
    main()
