"""애니메이션 스타일 뉴스 영상: 대본 JSON -> out3/news.mp4
- 그림: Google Noto 움직이는 이모지(CC BY 4.0) + 움직이는 배경 + 튀어나오는 효과
- 목소리: Edge TTS (무료), 합성: ffmpeg (무료)
"""
import asyncio, io, json, math, os, re, subprocess, sys, time, urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageSequence
import edge_tts, imageio_ffmpeg

W, H, FPS = 1080, 1920, 30
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT, CACHE = os.environ.get("OUT", "work"), "cache/emoji"
SEG = 2.6     # 그림 전환 간격(초)
GAP = 0.10    # 문장 사이 쉼(초)
os.makedirs(OUT, exist_ok=True); os.makedirs(CACHE, exist_ok=True)

THEMES = {  # (위 색, 아래 색, 동그라미 색)
    "sunrise": ((255, 166, 98), (250, 92, 124), (255, 230, 150)),
    "blue": ((40, 110, 230), (20, 40, 120), (120, 200, 255)),
    "purple": ((120, 80, 220), (40, 20, 90), (200, 160, 255)),
    "orange": ((255, 140, 50), (200, 60, 40), (255, 210, 120)),
    "red": ((200, 40, 60), (60, 10, 30), (255, 120, 120)),
    "amber": ((255, 190, 40), (220, 100, 20), (255, 240, 160)),
    "navy": ((30, 60, 120), (10, 15, 40), (90, 140, 230)),
    "sky": ((90, 190, 255), (40, 110, 220), (220, 245, 255)),
}


from fonts import font  # noqa


# ---------- 이모지 (움직이는 버전 우선) ----------
def emoji_code(e):
    return "_".join(f"{ord(c):x}" for c in e if ord(c) != 0xFE0F)


def flag_code(e):  # 국기 이모지
    return "_".join(f"{ord(c):x}" for c in e)


def fetch(url):
    key = os.path.join(CACHE, re.sub(r"[^a-z0-9_.]", "_", url.split("notoemoji/latest/")[-1]))
    if os.path.exists(key):
        return open(key, "rb").read() or None
    try:
        data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "news-anim/0.1"}), timeout=30).read()
    except Exception:
        data = b""
    open(key, "wb").write(data)
    return data or None


_sprites = {}


def emoji_frames(e, size):
    """이모지 → (프레임 목록, 프레임당 초). 움직이는 webp가 있으면 그걸 사용"""
    k = (e, size)
    if k in _sprites:
        return _sprites[k]
    frames, dur = None, 0.04
    for code in {emoji_code(e), flag_code(e)}:
        data = fetch(f"https://fonts.gstatic.com/s/e/notoemoji/latest/{code}/512.webp")
        if data:
            im = Image.open(io.BytesIO(data))
            fr = [f.convert("RGBA").resize((size, size), Image.LANCZOS) for f in ImageSequence.Iterator(im)]
            dur = (im.info.get("duration") or 40) / 1000
            # 프레임 수가 많으면 솎아서 메모리 절약
            step = max(1, len(fr) // 60)
            frames, dur = fr[::step], dur * step
            break
        data = fetch(f"https://fonts.gstatic.com/s/e/notoemoji/latest/{code}/512.png")
        if data:
            frames = [Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)]
            break
    if not frames:  # 못 찾으면 빈 그림
        frames = [Image.new("RGBA", (size, size), (0, 0, 0, 0))]
    _sprites[k] = (frames, dur)
    return _sprites[k]


def emoji_at(e, size, t):
    frames, dur = emoji_frames(e, size)
    return frames[int(t / dur) % len(frames)]


# ---------- 배경 ----------
_bg = {}


def background(theme):
    if theme in _bg:
        return _bg[theme]
    top, bot, blob = THEMES[theme]
    g = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        g.putpixel((0, y), tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    bg = g.resize((W, H)).convert("RGBA")
    # 부드러운 동그라미(움직이는 배경 장식)
    b = Image.new("RGBA", (420, 420), (0, 0, 0, 0))
    ImageDraw.Draw(b).ellipse([60, 60, 360, 360], fill=blob + (70,))
    b = b.filter(ImageFilter.GaussianBlur(40))
    halo = Image.new("RGBA", (760, 760), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([80, 80, 680, 680], fill=(255, 255, 255, 60))
    halo = halo.filter(ImageFilter.GaussianBlur(30))
    _bg[theme] = (bg, b, halo)
    return _bg[theme]


def ease_back(x):  # 튀어나오는 느낌
    x = min(max(x, 0), 1)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def scaled(im, s):
    if abs(s - 1) < 0.01:
        return im
    w = max(1, int(im.width * s))
    return im.resize((w, w), Image.BILINEAR)


def paste_center(canvas, im, cx, cy):
    canvas.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))


# ---------- 글자 ----------
def wrap(text, fnt, maxw, d):
    words, lines, cur = text.split(" "), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if d.textlength(t, font=fnt) > maxw and cur:
            lines.append(cur); cur = w_
        else:
            cur = t
    return lines + [cur]


def header_png(scene, si, n_story):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((60, 70), "아침 뉴스  ·  " + DATE, font=font(36), fill=(255, 255, 255, 235))
    if 0 < si <= n_story:
        bw = (W - 120 - (n_story - 1) * 10) / n_story
        for k in range(n_story):
            x = 60 + k * (bw + 10)
            d.rounded_rectangle([x, 130, x + bw, 138], 4, fill=(255, 255, 255, 255) if k < si else (255, 255, 255, 90))
    y = 175
    tf = font(40)
    tw = d.textlength(scene["tag"], font=tf)
    d.rounded_rectangle([60, y, 60 + tw + 44, y + 68], 34, fill=(255, 255, 255, 255))
    d.text((82, y + 10), scene["tag"], font=tf, fill=THEMES[scene["theme"]][1])
    y += 92
    for line in scene["title"].split("\n"):
        d.text((60, y), line, font=font(74), fill="white", stroke_width=4, stroke_fill=(0, 0, 0, 160))
        y += 94
    d.text((60, H - 56), "그림: Noto Emoji (Google, CC BY 4.0) · 자동 생성 영상", font=font(22, False), fill=(255, 255, 255, 170))
    return img


def sub_png(text):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(64)
    lines = wrap(text, f, W - 160, d)
    lh = 86
    top = 1500 - (len(lines) * lh) // 2
    boxw = max(d.textlength(l, font=f) for l in lines) + 70
    d.rounded_rectangle([(W - boxw) / 2, top - 22, (W + boxw) / 2, top + len(lines) * lh + 10], 36, fill=(0, 0, 0, 120))
    for i, line in enumerate(lines):
        tw = d.textlength(line, font=f)
        d.text(((W - tw) / 2, top + i * lh), line, font=f, fill="white", stroke_width=3, stroke_fill="black")
    return img


def chunks(text):
    parts = [p.strip() for p in re.split(r"(?<=[,.?!])\s+", text) if p.strip()]
    out = []
    for p in parts:
        while len(p) > 22:
            sp = [m.start() for m in re.finditer(" ", p)]
            if not sp:
                break
            cut = min(sp, key=lambda i: abs(i - len(p) / 2))
            out.append(p[:cut]); p = p[cut + 1:]
        out.append(p)
    return out


# ---------- 음성 ----------
async def tts(text, voice, rate, path):
    for attempt in range(5):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(path)
            return
        except Exception as e:
            print("   음성 재시도:", type(e).__name__)
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError("음성 생성 실패")


def duration(path):
    r = subprocess.run([FF, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


# ---------- 한 프레임 그리기 ----------
def draw_frame(theme, visual, t_seg, t_all, header, sub):
    bg, blob, halo = background(theme)
    c = bg.copy()
    for i in range(4):  # 떠다니는 동그라미
        x = (i * 310 + t_all * (25 + i * 12)) % (W + 400) - 200
        y = 500 + i * 330 + 60 * math.sin(t_all * 0.7 + i)
        c.alpha_composite(blob, (int(x - 210), int(y - 210)))
    main, *sides = visual
    cx, cy = W / 2, 930
    paste_center(c, scaled(halo, 0.85 + 0.15 * ease_back(t_seg / 0.45)), cx, cy)
    s = ease_back(t_seg / 0.4)
    bob = 18 * math.sin(t_all * 2.2)
    paste_center(c, scaled(emoji_at(main, 500, t_all), max(s, 0.01)), cx, cy + bob)
    pos = [(225, 1170, -1), (860, 700, 1)]
    for k, e in enumerate(sides[:2]):
        px, py, side = pos[k]
        p = ease_back((t_seg - 0.15 - 0.12 * k) / 0.4)
        x = px + side * (1 - min(p, 1)) * 500
        y = py + 22 * math.sin(t_all * 2.6 + k * 2)
        if p > 0:
            paste_center(c, emoji_at(e, 230, t_all + k), x, y)
    c.alpha_composite(header)
    if sub is not None:
        c.alpha_composite(sub)
    return c.convert("RGB")


def main(script):
    global DATE
    sc = json.load(open(script, encoding="utf-8"))
    DATE = sc["date"]
    scenes = sc["scenes"]
    n_story = len(scenes) - 2
    # 1) 음성 만들고 시간표 짜기
    timeline, voices, t0 = [], [], 0.0
    for si, scene in enumerate(scenes):
        for li, line in enumerate(scene["lines"]):
            v = f"{OUT}/v_{si}_{li}.mp3"
            if not os.path.exists(v):
                asyncio.run(tts(line["text"], sc["voice"], sc["rate"], v))
            d = duration(v) + GAP
            timeline.append((t0, d, si, line)); voices.append((v, d)); t0 += d
    total = t0
    print(f"총 길이 {total:.1f}초, 문장 {len(timeline)}개")
    # 2) 음성 이어붙이기
    inputs, filt = [], ""
    for i, (v, d) in enumerate(voices):
        inputs += ["-i", v]
        filt += f"[{i}:a]apad,atrim=0:{d:.3f},aresample=44100[a{i}];"
    filt += "".join(f"[a{i}]" for i in range(len(voices))) + f"concat=n={len(voices)}:v=0:a=1[out]"
    subprocess.run([FF, "-y", "-loglevel", "error"] + inputs + ["-filter_complex", filt, "-map", "[out]",
                    "-c:a", "aac", "-b:a", "160k", f"{OUT}/voice.m4a"], check=True)
    # 3) 장면 정보 미리 준비 (제목, 자막, 그림 전환 시점)
    headers = {si: header_png(sc_, si, n_story) for si, sc_ in enumerate(scenes)}
    plan = []  # (시작, 끝, 테마, 그림묶음, 헤더, 자막목록)
    for (st, d, si, line) in timeline:
        cs = chunks(line["text"])
        tot = sum(len(c.replace(" ", "")) for c in cs)
        subs, t = [], st
        for ci, ctext in enumerate(cs):
            t2 = st + d if ci == len(cs) - 1 else t + (d - GAP) * len(ctext.replace(" ", "")) / tot
            subs.append((t, t2, sub_png(ctext))); t = t2
        n = max(1, round(d / SEG))
        vis = line["visuals"]
        for k in range(n):
            plan.append((st + d * k / n, st + d * (k + 1) / n, scenes[si]["theme"], vis[k % len(vis)], headers[si], subs))
    # 이모지 미리 받기
    for p in plan:
        for k, e in enumerate(p[3]):
            emoji_frames(e, 500 if k == 0 else 230)
    # 4) 프레임 그려서 ffmpeg로 보내기
    proc = subprocess.Popen([FF, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                             "-r", str(FPS), "-i", "-", "-i", f"{OUT}/voice.m4a", "-c:v", "libx264", "-preset", "medium",
                             "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart",
                             f"{OUT}/news.mp4"], stdin=subprocess.PIPE)
    nframes, pi, t_start = int(total * FPS), 0, time.time()
    for fi in range(nframes):
        t = fi / FPS
        while pi < len(plan) - 1 and t >= plan[pi][1]:
            pi += 1
        st, en, theme, vis, hdr, subs = plan[pi]
        sub = next((im for (a, b, im) in subs if a <= t < b), None)
        proc.stdin.write(draw_frame(theme, vis, t - st, t, hdr, sub).tobytes())
        if fi % 300 == 0:
            print(f"  {fi}/{nframes} 프레임 ({time.time() - t_start:.0f}초 경과)")
    proc.stdin.close(); proc.wait()
    print(f"완료: {OUT}/news.mp4  {duration(OUT + '/news.mp4'):.1f}초")


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["script_1126.json"]))
