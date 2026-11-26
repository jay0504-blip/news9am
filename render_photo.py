"""script2.json -> out2/news.mp4
무료 구성: 위키미디어 공용(사진·영상) + Edge TTS + ffmpeg
- 문장마다 음성 생성, 약 3초마다 화면(사진/영상) 전환
- 자막은 짧은 구절 단위로 바뀜 (쇼츠 스타일)
"""
import asyncio, hashlib, json, os, random, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from PIL import Image, ImageDraw, ImageFont
import edge_tts, imageio_ffmpeg

W, H, FPS = 1080, 1920, 30
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT = os.environ.get("OUT", "work")
SEG = 3.0            # 화면 전환 간격(초)
GAP = 0.12           # 문장 사이 쉼(초)
HDR = {"User-Agent": "morning-news-video-test/0.2"}
# 뉴스에 엉뚱한 의미를 줄 수 있는 사진(인물 초상, 정당 깃발, 지도 등)은 제외
BAN = ("portrait", "초상", "person", "people", "man ", "woman", "president", "minister", "chief justice",
       "party", "당 ", "노동당", "flag", "coin", "map", "지도", "bridge", "breathalyzer test", "logo",
       "cj ", "headquarters in south korea", "red army", "assange", "sc.png", "dprk", "iaea", "haeundae", "homeplus digital", "runners", "taft", "long live")
OK_LIC = ("cc0", "public domain", "pdm", "cc by", "cc-by")
CACHE = "cache"
os.makedirs(OUT, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)
used = set()
random.seed(7)


from fonts import font  # noqa


def run(args):
    subprocess.run([FF, "-y", "-loglevel", "error"] + args, check=True)


def duration(path):
    r = subprocess.run([FF, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


# ---------- 위키미디어 공용 검색 ----------
def commons(query, kind="image", limit=20):
    ft = "filetype:video" if kind == "video" else "filetype:bitmap"
    if query.startswith("File:"):  # 특정 파일 지정
        api = ("https://commons.wikimedia.org/w/api.php?action=query&prop=imageinfo&iiprop=url|extmetadata|size"
               "&iiurlwidth=1280&format=json&titles=" + urllib.parse.quote(query))
    else:
        api = ("https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrnamespace=6"
               f"&gsrlimit={limit}&prop=imageinfo&iiprop=url|extmetadata|size&iiurlwidth=1280&format=json&gsrsearch="
               + urllib.parse.quote(f"{ft} {query}"))
    try:
        res = json.loads(fetch(api, 30))
    except Exception as e:
        print("   검색 실패:", e); return []
    out = []
    for pg in sorted(res.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99)):
        if pg["title"] in used or any(b in pg["title"].lower() for b in BAN):
            continue
        ii = pg["imageinfo"][0]; md = ii.get("extmetadata", {})
        lic = md.get("LicenseShortName", {}).get("value", "")
        if not any(k in lic.lower() for k in OK_LIC):
            continue
        if kind == "image" and ii.get("width", 0) < 900:
            continue
        artist = re.sub(r"<[^>]+>", "", md.get("Artist", {}).get("value", "")).strip()
        artist = re.sub(r"\s+", " ", artist)[:32] or "작자 미상"
        out.append({"title": pg["title"], "url": ii.get("url"), "thumb": ii.get("thumburl"),
                    "size": ii.get("size", 0), "credit": f"{artist} · {lic} · Wikimedia Commons"})
    return out


def fetch(url, timeout=120):
    """위키미디어 요청 제한(429)을 피하려고 천천히, 실패하면 기다렸다 재시도"""
    for attempt in range(5):
        time.sleep(1.0)
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            print("   요청 제한 → 대기")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("요청 제한 지속")


def download(url, path):
    key = os.path.join(CACHE, hashlib.md5(url.encode()).hexdigest() + os.path.splitext(url.split("?")[0])[1][:6])
    if not os.path.exists(key):
        open(key, "wb").write(fetch(url))
    shutil.copy(key, path)


def get_images(queries, n, tag):
    got = []
    if isinstance(queries, str):
        queries = [queries]
    def cands():  # 앞 검색어에서 부족할 때만 다음 검색어 검색
        for q in queries:
            if len(got) >= n:
                return
            yield from commons(q)
    for c in cands():
        if c["title"] in used:
            continue
        if len(got) >= n:
            break
        p = f"{OUT}/img_{tag}_{len(got)}.jpg"
        try:
            download(c["thumb"], p)
            Image.open(p).convert("RGB").save(p, quality=92)
            used.add(c["title"]); got.append((p, c["credit"]))
            print("     ", c["title"][:70])
        except Exception as e:
            print("   사진 실패:", e)
    return got


def get_video(query, tag):
    for c in commons(query, "video", 10):
        url = c["url"]
        fn = url.rsplit("/", 1)[1]
        trans = url.replace("/commons/", "/commons/transcoded/") + f"/{fn}.720p.vp9.webm"
        p = f"{OUT}/vid_{tag}.webm"
        for u in (trans, url if c["size"] < 60e6 else None):
            if not u:
                continue
            try:
                download(u, p)
                if duration(p) >= 2:
                    used.add(c["title"]); return p, c["credit"]
            except Exception:
                pass
    return None


# ---------- 그래픽 ----------
def wrap(text, fnt, maxw, d):
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) > maxw and cur:
            lines.append(cur); cur = w
        else:
            cur = t
    return lines + [cur]


def header_png(scene, si, n_story, credit, path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(560):  # 위쪽 어둡게
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(200 * (1 - y / 560) ** 1.3)))
    for y in range(1050, H):  # 아래쪽 어둡게
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(215 * ((y - 1050) / (H - 1050)) ** 0.9)))
    d.text((60, 70), "아침 뉴스  ·  " + DATE, font=font(36), fill=(255, 255, 255, 230))
    if 0 < si <= n_story:  # 진행 표시
        bw = (W - 120 - (n_story - 1) * 10) / n_story
        for k in range(n_story):
            x = 60 + k * (bw + 10)
            d.rounded_rectangle([x, 130, x + bw, 138], 4,
                                fill=(255, 255, 255, 255) if k < si else (255, 255, 255, 80))
    y = 175
    tf = font(40)
    tw = d.textlength(scene["tag"], font=tf)
    d.rounded_rectangle([60, y, 60 + tw + 44, y + 68], 16, fill=(235, 55, 55, 255))
    d.text((82, y + 10), scene["tag"], font=tf, fill="white")
    y += 92
    for line in scene["title"].split("\n"):
        d.text((60, y), line, font=font(74), fill="white", stroke_width=3, stroke_fill="black")
        y += 94
    if credit:
        d.text((60, H - 60), "사진·영상: " + credit, font=font(22, False), fill=(200, 200, 200, 220))
    img.save(path)


def sub_png(text, path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(66)
    lines = wrap(text, f, W - 140, d)
    y = 1430 - (len(lines) - 1) * 44
    for line in lines:
        tw = d.textlength(line, font=f)
        d.text(((W - tw) / 2, y), line, font=f, fill=(255, 255, 255), stroke_width=6, stroke_fill="black")
        y += 88
    img.save(path)


def chunks(text):
    parts = [p.strip() for p in re.split(r"(?<=[,.?!])\s+", text) if p.strip()]
    out = []
    for p in parts:
        while len(p) > 24:  # 길면 가운데 근처 띄어쓰기에서 자르기
            sp = [m.start() for m in re.finditer(" ", p)]
            if not sp:
                break
            cut = min(sp, key=lambda i: abs(i - len(p) / 2))
            out.append(p[:cut]); p = p[cut + 1:]
        out.append(p)
    return out


# ---------- 화면 클립 ----------
def bg_clip(src, dur, overlay, path, is_video=False):
    frames = int(round(dur * FPS))
    if is_video:
        vf = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1[b]"
        inp = ["-stream_loop", "-1", "-i", src]
    else:
        N = max(frames, 1)
        mode = random.choice(["in", "out", "left", "right"])
        z = {"in": f"1+0.12*on/{N}", "out": f"1.12-0.12*on/{N}", "left": "1.15", "right": "1.15"}[mode]
        x = {"in": "iw/2-iw/zoom/2", "out": "iw/2-iw/zoom/2",
             "left": f"(iw-iw/zoom)*(1-on/{N})", "right": f"(iw-iw/zoom)*on/{N}"}[mode]
        vf = (f"[0:v]scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
              f"zoompan=z='{z}':x='{x}':y='ih/2-ih/zoom/2':d={N}:s={W}x{H}:fps={FPS},setsar=1[b]")
        inp = ["-loop", "1", "-i", src]
    run(inp + ["-i", overlay, "-filter_complex", vf + ";[b][1:v]overlay=0:0,format=yuv420p",
               "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", path])


def fallback(path):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(15 + 25 * t), int(25 + 35 * t), int(60 + 50 * t)))
    img.save(path)
    return path


async def tts(text, voice, rate, path):
    for attempt in range(5):  # 무료 음성 서비스가 가끔 응답을 안 줄 때 재시도
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(path)
            return
        except Exception as e:
            print("   음성 재시도:", type(e).__name__)
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError("음성 생성 실패")


def concat(files, path, extra=()):
    lst = path + ".txt"
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["-f", "concat", "-safe", "0", "-i", lst] + list(extra) + ["-c", "copy", path])


def main(script="script2.json"):
    global DATE
    sc = json.load(open(script, encoding="utf-8"))
    DATE = sc["date"]
    scenes = sc["scenes"]
    n_story = len(scenes) - 2
    line_clips = []
    for si, scene in enumerate(scenes):
        print(f"\n== [{si+1}/{len(scenes)}] {scene['title'].replace(chr(10), ' ')}")
        for li, line in enumerate(scene["lines"]):
            tag = f"{si}_{li}"
            voice = f"{OUT}/v_{tag}.mp3"
            asyncio.run(tts(line["text"], sc["voice"], sc["rate"], voice))
            dur = duration(voice) + GAP
            n = max(1, round(dur / SEG))
            # 화면 소스 모으기 (영상 우선 → 사진)
            media = []
            if line.get("video"):
                v = get_video(line["video"], tag)
                if v:
                    media.append((v[0], v[1], True))
            imgs = get_images(line["query"], n - len(media), tag) if n > len(media) else []
            media += [(p, c, False) for p, c in imgs]
            if not media:
                media = [(fallback(f"{OUT}/fb_{tag}.jpg"), None, False)]
            while len(media) < n:  # 모자라면 앞의 것 재사용
                media.append(media[len(media) % max(1, len(imgs) or 1)])
            print(f"   {dur:.1f}초 / 화면 {n}개:", ", ".join(("영상" if m[2] else "사진") for m in media))
            segs = []
            for k, (src, credit, isv) in enumerate(media[:n]):
                ov = f"{OUT}/hdr_{tag}_{k}.png"
                header_png(scene, si, n_story, credit, ov)
                seg = f"{OUT}/seg_{tag}_{k}.mp4"
                bg_clip(src, dur / n, ov, seg, isv)
                segs.append(seg)
            bg = f"{OUT}/bg_{tag}.mp4"
            concat(segs, bg)
            # 자막: 구절 길이에 비례해 시간 배분
            cs = chunks(line["text"])
            total = sum(len(c.replace(" ", "")) for c in cs)
            speech = dur - GAP
            t, inputs, filt, last = 0.0, ["-i", bg, "-i", voice], "", "0:v"
            for ci, c in enumerate(cs):
                p = f"{OUT}/sub_{tag}_{ci}.png"
                sub_png(c, p)
                inputs += ["-i", p]
                t2 = dur if ci == len(cs) - 1 else t + speech * len(c.replace(" ", "")) / total
                filt += f"[{last}][{ci+2}:v]overlay=0:0:enable='between(t,{t:.3f},{t2:.3f})'[o{ci}];"
                last, t = f"o{ci}", t2
            clip = f"{OUT}/line_{tag}.mp4"
            run(inputs + ["-filter_complex", filt.rstrip(";"), "-map", f"[{last}]", "-map", "1:a",
                          "-af", "apad", "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                          "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", clip])
            line_clips.append(clip)
    final = f"{OUT}/news.mp4"
    concat(line_clips, final, ["-movflags", "+faststart"])
    print(f"\n완료: {final}  {duration(final):.1f}초")


if __name__ == "__main__":
    main(*sys.argv[1:])
