"""매일 아침 뉴스 릴스 자동 제작 + 검수 + 오전 9시 정각 게시

흐름
 1. 구글 뉴스에서 주요 뉴스 수집 (관련 기사 제목 묶음 포함)
 2. Gemini(무료)로 대본 작성 → 다른 호출로 사실 검수(출처에 없는 문장 삭제)
 3. 위키미디어 공용 사진 후보 → Gemini가 사진을 직접 보고 검수(인물 얼굴·엉뚱한 사진 제외)
    맞는 사진이 없으면 움직이는 이모지 장면으로 대체
 4. 음성(Edge TTS) + 영상 합성
 5. 영상 자동 점검(길이·소리·화면·용량)
 6. 임시 공개 주소에 올리고 인스타 컨테이너 준비 → 오전 9:00:00(KST)에 게시 → 게시 확인

환경 변수: IG_USER_ID, IG_ACCESS_TOKEN, GEMINI_API_KEY(없으면 단순 대본으로 대체)
옵션: --dry-run (게시 안 함), --now (9시 기다리지 않고 바로 게시)
"""
import argparse, asyncio, base64, datetime as dt, html, io, json, os, re, shutil, subprocess, sys, time
import urllib.parse, urllib.request, xml.etree.ElementTree as ET

os.environ.setdefault("OUT", "work")
import render_photo as RP
import render_anim as RA
from PIL import Image, ImageStat

KST = dt.timezone(dt.timedelta(hours=9))
WORK = os.environ["OUT"]
API = "https://graph.facebook.com/v23.0"
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]
VOICE, RATE = "ko-KR-HyunsuMultilingualNeural", "+50%"
THEME_BY_TAG = {"경제": "blue", "법원": "purple", "정치": "purple", "사회": "amber", "생활": "orange",
                "안보": "red", "북한": "red", "사건사고": "amber", "국제": "navy", "날씨": "sky",
                "IT": "blue", "과학": "sky", "문화": "orange", "스포츠": "orange", "아침 뉴스": "sunrise"}
LOG = []


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.append(msg)


def now_kst():
    return dt.datetime.now(KST)


def http(url, data=None, headers=None, timeout=60, method=None):
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
    elif isinstance(data, (bytes, str)):
        body = data.encode() if isinstance(data, str) else data
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ───────────────────────── 1. 뉴스 수집 ─────────────────────────
def fetch_news(n=25):
    raw = http("https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko", headers={"User-Agent": "Mozilla/5.0"})
    items = []
    for it in ET.fromstring(raw).iter("item"):
        d = html.unescape(it.findtext("description") or "")
        rel = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<li>(.*?)</li>", d)]
        items.append({"id": len(items), "title": it.findtext("title"), "related": rel[:6], "date": it.findtext("pubDate")})
        if len(items) >= n:
            break
    log(f"[수집] 뉴스 묶음 {len(items)}개")
    return items


# ───────────────────────── 2. Gemini ─────────────────────────
_models = None


def gemini_models(key):
    """지금 쓸 수 있는 Gemini 모델을 직접 조회해서 최신 flash 계열부터 사용 (모델 이름이 바뀌어도 동작)"""
    global _models
    if _models is not None:
        return _models
    found = []
    try:
        r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={key}", timeout=30))
        for m in r.get("models", []):
            name = m["name"].split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or any(b in name for b in ("image", "tts", "live", "audio", "embedding", "thinking-exp")):
                continue
            found.append(name)
    except Exception as e:
        log("  모델 목록 조회 실패:", e)

    def rank(n):
        v = re.search(r"(\d+(?:\.\d+)?)", n)
        return (float(v.group(1)) if v else 0, "lite" not in n, "preview" not in n and "exp" not in n, -len(n))
    found.sort(key=rank, reverse=True)
    _models = found[:8] + [m for m in GEMINI_MODELS if m not in found]
    log("  사용할 Gemini 모델 순서:", _models[:4])
    return _models


def gemini(parts, schema=None, temp=0.4, timeout=150, prefer_lite=False, max_models=4, attempts=3):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY 없음")
    cfg = {"temperature": temp, "responseMimeType": "application/json"}
    if schema:
        cfg["responseSchema"] = schema
    body = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}).encode()
    last = None
    models = gemini_models(key)
    if prefer_lite:
        models = [m for m in models if "lite" in m] + [m for m in models if "lite" not in m]
    for model in models[:max_models]:
        for attempt in range(attempts):
            try:
                r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                    data=body, headers={"Content-Type": "application/json"}, timeout=timeout))
                txt = r["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(txt)
            except urllib.error.HTTPError as e:
                last = e
                log(f"  Gemini 오류({model}): {e.code}")
                if e.code in (400, 403, 404):  # 이 모델은 못 씀 → 다음 모델
                    break
                time.sleep(15 * (attempt + 1))  # 429(사용량)·5xx → 기다렸다 재시도
            except Exception as e:
                last = e
                log(f"  Gemini 재시도({model}, {attempt + 1}): {str(e)[:120]}")
                time.sleep(8 * (attempt + 1))
    raise RuntimeError(f"Gemini 실패: {last}")


SCRIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "stories": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "source_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "tag": {"type": "STRING"}, "title": {"type": "STRING"},
            "lines": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "text": {"type": "STRING"},
                "image_queries": {"type": "ARRAY", "items": {"type": "STRING"}},
                "emojis": {"type": "ARRAY", "items": {"type": "STRING"}}},
                "required": ["text", "image_queries", "emojis"]}}},
            "required": ["source_ids", "tag", "title", "lines"]}},
        "weather": {"type": "STRING"},
    },
    "required": ["stories"],
}


def write_script(news, today):
    src = "\n".join(f"[{n['id']}] {n['title']}\n" + "\n".join(f"    - {r}" for r in n["related"]) for n in news)
    prompt = f"""너는 인스타그램 아침 뉴스 릴스의 대본 작가야. 오늘은 {today}.
아래는 오늘 구글 뉴스 주요 뉴스 묶음이야 (각 묶음 = 같은 사건을 다룬 기사 제목들).

{src}

규칙:
- 가장 크고 사람들이 많이 관심 가질 뉴스 5~6개를 골라. 사건사고·생활·경제·국제·안보 이슈를 우선하고, 같은 사건은 하나로 합쳐. 정당 간 말싸움 위주 기사는 피하고, 큰 판결·정책·사건은 포함해도 돼.
- 개인의 사망·자살·성범죄 피해 등 선정적이거나 피해자가 특정될 수 있는 사건, 확인 안 된 의혹 폭로는 고르지 마.
- 위 기사 제목들에 **적힌 사실만** 써. 숫자·이름·날짜를 지어내지 마. 확실하지 않으면 빼.
- 낯선 용어(예: 기준금리, 전술핵)는 누구나 아는 일반 상식 수준으로 아주 짧게 풀어줘도 돼. 단, 이번 사건에 대한 새로운 사실을 추가하면 안 돼.
- 말투: 친근한 해요체("~했대요", "~거예요"). 한 문장(line)은 25~60자. 뉴스당 2~3문장.
- 혐의 단계인 사건은 단정하지 말고 "혐의", "의혹"이라고 써.
- tag: 경제/사회/생활/사건사고/국제/안보/법원/IT/날씨 중 하나. title: 화면 제목 2줄, 줄바꿈은 \\n, 한 줄 14자 이내.
- image_queries: 위키미디어 공용에서 찾을 **영어** 검색어 2~3개. 사람 얼굴이 아닌 건물·사물·장소 위주(예: "Bank of Korea building", "oil refinery"). 특정 인물 이름 금지.
- emojis: 이 문장을 표현할 이모지 3개(사람 얼굴 이모지 지양).
- 날씨 기사가 있으면 weather에 한 문장(해요체)으로, 없으면 빈 문자열.
- source_ids: 그 뉴스의 근거가 된 묶음 번호들.
JSON으로만 답해."""
    data = gemini([{"text": prompt}], SCRIPT_SCHEMA, timeout=120, attempts=2, max_models=8)
    log(f"[대본] 뉴스 {len(data['stories'])}개 작성")
    return data


VERIFY_SCHEMA = {"type": "OBJECT", "properties": {"results": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "story": {"type": "INTEGER"}, "line": {"type": "INTEGER"}, "ok": {"type": "BOOLEAN"},
    "fixed_text": {"type": "STRING"}, "reason": {"type": "STRING"}}, "required": ["story", "line", "ok"]}}},
    "required": ["results"]}


def verify_script(data, news):
    """두 번째 AI 호출로 문장마다 출처와 대조 → 틀린 문장은 고치거나 삭제"""
    byid = {n["id"]: n for n in news}
    blocks = []
    for si, s in enumerate(data["stories"]):
        srcs = "\n".join(f"  · {t}" for i in s["source_ids"] if i in byid for t in [byid[i]["title"]] + byid[i]["related"])
        lines = "\n".join(f"  ({si},{li}) {l['text']}" for li, l in enumerate(s["lines"]))
        blocks.append(f"뉴스 {si} 출처:\n{srcs}\n문장:\n{lines}")
    prompt = ("너는 깐깐한 팩트체커야. 각 문장이 바로 위 출처 제목들로 뒷받침되는지 확인해.\n"
              "- 출처에 없는 사실(숫자·이름·날짜·원인·결과)이 있으면 ok=false.\n"
              "- 일반 상식 수준의 짧은 용어 설명은 허용(ok=true).\n"
              "- 고칠 수 있으면 fixed_text에 출처에 맞게 고친 문장(해요체)을, 아니면 빈 문자열.\n"
              "- 단정하면 안 되는 혐의를 단정했으면 ok=false 로 하고 고쳐.\n\n" + "\n\n".join(blocks) + "\n\nJSON으로만 답해.")
    res = gemini([{"text": prompt}], VERIFY_SCHEMA, temp=0, timeout=120, attempts=2, max_models=8)
    bad = 0
    for r in res["results"]:
        try:
            line = data["stories"][r["story"]]["lines"][r["line"]]
        except (IndexError, KeyError):
            continue
        if not r["ok"]:
            bad += 1
            if r.get("fixed_text", "").strip():
                log(f"  [검수] 수정: {line['text']} → {r['fixed_text']}")
                line["text"] = r["fixed_text"].strip()
            else:
                log(f"  [검수] 삭제: {line['text']} ({r.get('reason', '')})")
                line["drop"] = True
    for s in data["stories"]:
        s["lines"] = [l for l in s["lines"] if not l.get("drop")]
    data["stories"] = [s for s in data["stories"] if s["lines"]]
    log(f"[검수] 문제 문장 {bad}개 처리, 남은 뉴스 {len(data['stories'])}개")
    return data


def fallback_script(news):
    """Gemini를 못 쓸 때: 기사 제목만으로 안전한 대본"""
    stories = []
    politics = ("국힘", "민주당", "의원", "탄핵", "윤리위", "특검", "장관", "후보자", "당대표", "복당", "탈당")
    ranked = [n for n in news if not any(w in n["title"] for w in politics)] + \
             [n for n in news if any(w in n["title"] for w in politics)]
    for n in ranked[:5]:
        t = re.sub(r"\s+-\s+[^-]+$", "", n["title"]).strip()
        t = re.sub(r"\[[^\]]+\]", "", t).strip()
        if len(t) > 15:  # 가운데 근처 띄어쓰기에서 두 줄로
            sp = [m.start() for m in re.finditer(" ", t)] or [len(t) // 2]
            cut = min(sp, key=lambda i: abs(i - min(len(t), 32) / 2))
            title = t[:cut].strip() + "\n" + t[cut:].strip()[:16]
        else:
            title = t
        stories.append({"source_ids": [n["id"]], "tag": "뉴스", "title": title,
                        "lines": [{"text": f"{t}, 라는 소식이에요.", "image_queries": [], "emojis": ["📰", "🗞️", "📢"]}]})
    log("[대본] Gemini 없이 제목 기반 대본 사용")
    return {"stories": stories, "weather": ""}


# ───────────────────────── 3. 사진 검수 ─────────────────────────
IMG_SCHEMA = {"type": "OBJECT", "properties": {"ok": {"type": "ARRAY", "items": {"type": "INTEGER"}}}, "required": ["ok"]}


VET_DEADLINE = None   # 이 시각이 지나면 사진 검수를 멈추고 이모지 장면 사용
_last_vet = [0.0]
_vet_fail = [0]


def vet_images(sentence, cands):
    """Gemini가 사진을 직접 보고 쓸 수 있는 것만 고름 (실패하면 사진 안 씀 = 안전한 쪽)"""
    if not cands or not os.environ.get("GEMINI_API_KEY"):
        return []
    if VET_DEADLINE and now_kst() > VET_DEADLINE:
        log("  사진 검수 마감 시간 지남 → 이모지 장면 사용")
        return []
    if _vet_fail[0] >= 8:  # 연속 실패가 많으면 오늘은 사진 검수 중단
        return []
    wait = 5 - (time.time() - _last_vet[0])  # 무료 사용량 제한을 피하려고 간격 두기
    if wait > 0:
        time.sleep(wait)
    _last_vet[0] = time.time()
    parts = [{"text": f"""뉴스 문장: "{sentence}"
아래 사진들 중 이 문장의 배경 화면으로 써도 되는 사진 번호만 골라.
반드시 제외: ① 사람 얼굴이 크게 알아볼 수 있게 나온 사진(인물사진. 멀리서 작게 보이는 사람들은 괜찮아) ② 문장과 관련 없는 장소·회사·제품 ③ 다른 회사 로고나 간판이 크게 보이는 사진(문장 속 회사 제외) ④ 그림·지도·국기·동전·도표 ⑤ 북한·특정 국가를 잘못 연상시키는 사진 ⑥ 폭력·시신 등 불쾌한 사진.
문장과 직접 관련된 실제 사진이 가장 좋고, 같은 분야를 보여주는 실제 사진(예: 증시 뉴스에 증권거래소, 철도 뉴스에 전철)도 괜찮아. 확실히 문제 있는 것만 제외해. JSON {{"ok": [번호...]}}로만 답해."""}]
    for i, (path, _) in enumerate(cands):
        im = Image.open(path).convert("RGB")
        im.thumbnail((384, 384))
        b = io.BytesIO(); im.save(b, "JPEG", quality=80)
        parts += [{"text": f"사진 {i}:"}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b.getvalue()).decode()}}]
    try:
        ok = [i for i in gemini(parts, IMG_SCHEMA, temp=0, timeout=60, prefer_lite=True, max_models=3, attempts=2)["ok"]
              if 0 <= i < len(cands)]
        _vet_fail[0] = 0
        return ok
    except Exception as e:
        _vet_fail[0] += 1
        log("  사진 검수 실패 → 이 장면은 이모지:", str(e)[:80])
        return []


# 문장 검색어로 사진이 모자랄 때 쓰는 분야별 실사 검색어
TAG_QUERIES = {
    "경제": ["Seoul financial district", "stock market display", "Korean won banknotes", "container port Busan"],
    "사회": ["Seoul street", "Seoul city hall", "Seoul subway station", "Korean school"],
    "생활": ["Seoul market", "Seoul apartment complex", "Korean supermarket", "Seoul street"],
    "사건사고": ["police car Seoul", "fire truck Korea", "ambulance Korea", "police line"],
    "국제": ["United Nations headquarters New York", "Incheon International Airport terminal", "container ship sea", "airport departure board"],
    "안보": ["Republic of Korea Navy destroyer", "Republic of Korea Air Force F-15K", "Republic of Korea Army K2 tank", "fighter jet"],
    "법원": ["courtroom", "wooden gavel", "Supreme Court of Korea"],
    "정치": ["National Assembly Proceeding Hall", "Yongsan Seoul", "Seoul Gwanghwamun", "voting booth"],
    "IT": ["semiconductor wafer", "data center servers", "smartphone", "circuit board"],
    "과학": ["telescope observatory", "rocket launch", "laboratory", "space Earth"],
    "날씨": ["clouds sky", "rain umbrella street", "Seoul skyline sunny", "snow Seoul"],
    "아침 뉴스": ["Seoul skyline", "Han River Seoul", "Seoul skyline morning"],
}


def pick_images(line, tag, n, scene_tag=""):
    """후보 사진 받기 → AI 검수 → 통과한 것만 n장"""
    if VET_DEADLINE and now_kst() > VET_DEADLINE:  # 시간이 모자라면 사진 검색도 생략
        log("   사진 작업 마감 시간 지남")
        return []
    queries = list(line.get("image_queries") or [])
    queries += [q for q in TAG_QUERIES.get(scene_tag, ["Seoul skyline"]) if q not in queries]
    cands = RP.get_images(queries, max(5, n + 3), tag)
    ok = vet_images(line["text"], cands)
    keep = [cands[i] for i in ok][:n]
    log(f"   사진 후보 {len(cands)} → 통과 {len(ok)} → 사용 {len(keep)}")
    return keep


# ───────────────────────── 4. 영상 만들기 ─────────────────────────
def build_scenes(data, today_label):
    scenes = [{"tag": "아침 뉴스", "title": f"{today_label}\n핵심만 빠르게",
               "lines": [{"text": f"좋은 아침이에요! {today_label}, 꼭 알아야 할 뉴스 빠르게 정리해 드릴게요.",
                          "image_queries": ["Seoul skyline", "Seoul skyline morning"], "emojis": ["☀️", "📰", "☕"]}]}]
    for s in data["stories"]:
        scenes.append({"tag": s["tag"], "title": s["title"].replace("\\n", "\n"), "lines": s["lines"]})
    end = [{"text": "오늘 아침 뉴스는 여기까지예요. 좋은 하루 보내세요!", "image_queries": ["sunrise Seoul", "sunrise city"],
            "emojis": ["👋", "😊", "💖"]}]
    if data.get("weather", "").strip():
        end.insert(0, {"text": data["weather"].strip(), "image_queries": ["clouds sky", "rain umbrella street"],
                       "emojis": ["🌤️", "🌂", "🌡️"]})
    scenes.append({"tag": "날씨" if len(end) > 1 else "아침 뉴스", "title": "오늘 하루도\n좋은 하루 보내세요", "lines": end})
    for sc in scenes:
        sc["theme"] = THEME_BY_TAG.get(sc["tag"], "navy")
    return scenes


def make_video(scenes, date_label):
    shutil.rmtree(WORK, ignore_errors=True); os.makedirs(WORK)
    RP.DATE = RA.DATE = date_label
    RP.used.clear()
    n_story = len(scenes) - 2
    line_clips, stats = [], {"photo": 0, "emoji": 0}
    for si, scene in enumerate(scenes):
        log(f"== [{si + 1}/{len(scenes)}] {scene['title'].replace(chr(10), ' ')}")
        # 1) 문장마다 음성과 사진 먼저 준비
        prep = []
        for li, line in enumerate(scene["lines"]):
            tag = f"{si}_{li}"
            voice = f"{WORK}/v_{tag}.mp3"
            try:
                asyncio.run(RP.tts(line["text"], VOICE, RATE, voice))
            except Exception as e:  # 음성이 끝내 안 만들어지면 이 문장만 빼고 계속
                log(f"   [{tag}] 음성 실패 → 이 문장 생략:", str(e)[:60])
                continue
            dur = RP.duration(voice) + RP.GAP
            n = max(1, round(dur / 3.0))
            photos = pick_images(line, tag, n, scene["tag"]) if line.get("image_queries") is not None else []
            prep.append((li, line, tag, voice, dur, n, photos))
        # 2) 사진이 없는 문장은 같은 뉴스의 다른 문장 사진을 나눠 씀 (이모지는 최후 수단)
        pool = [p for x in prep for p in x[6]]
        for li, line, tag, voice, dur, n, photos in prep:
            if not photos and pool:
                photos = [pool[(li + j) % len(pool)] for j in range(min(n, len(pool)))]
                log(f"   [{tag}] 같은 뉴스의 사진 재사용 {len(photos)}장")
            segs = []
            for k in range(n):
                seg = f"{WORK}/seg_{tag}_{k}.mp4"
                d = dur / n
                if photos:
                    src, credit = photos[k % len(photos)]
                    ov = f"{WORK}/hdr_{tag}_{k}.png"
                    RP.header_png(scene, si, n_story, credit, ov)
                    RP.bg_clip(src, d, ov, seg)
                    stats["photo"] += 1
                else:  # 맞는 사진이 없으면 움직이는 이모지 장면
                    emo_segment(scene, si, n_story, line.get("emojis") or ["📰", "🗞️", "📢"], k, d, seg)
                    stats["emoji"] += 1
                segs.append(seg)
            bg = f"{WORK}/bg_{tag}.mp4"
            RP.concat(segs, bg)
            add_subs_and_voice(line["text"], bg, voice, dur, f"{WORK}/line_{tag}.mp4", tag)
            line_clips.append(f"{WORK}/line_{tag}.mp4")
    final = f"{WORK}/news.mp4"
    RP.concat(line_clips, f"{WORK}/joined.mp4")
    # 인스타 권장 규격으로 다시 저장
    RP.run(["-i", f"{WORK}/joined.mp4", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
            "-b:v", "4M", "-maxrate", "5M", "-bufsize", "10M", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            "-movflags", "+faststart", final])
    log(f"[영상] 완료 {RP.duration(final):.1f}초 (사진 장면 {stats['photo']}, 이모지 장면 {stats['emoji']})")
    return final


def emo_segment(scene, si, n_story, emojis, k, d, path):
    from PIL import Image as _I
    ov = f"{WORK}/hdr_emo_{si}.png"
    if not os.path.exists(ov):
        RP.header_png(scene, si, n_story, "Noto Emoji (Google, CC BY 4.0)", ov)
    header = _I.open(ov).convert("RGBA")
    vis = emojis[k % len(emojis):] + emojis[:k % len(emojis)]
    frames = int(round(d * RA.FPS))
    p = subprocess.Popen([RA.FF, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{RA.W}x{RA.H}",
                          "-r", str(RA.FPS), "-i", "-", "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
                          "-crf", "20", "-pix_fmt", "yuv420p", path], stdin=subprocess.PIPE)
    for f in range(frames):
        t = f / RA.FPS
        p.stdin.write(RA.draw_frame(scene["theme"], vis[:3], t, t + k * 2.0, header, None).tobytes())
    p.stdin.close(); p.wait()


def add_subs_and_voice(text, bg, voice, dur, out, tag):
    cs = RP.chunks(text)
    total = sum(len(c.replace(" ", "")) for c in cs)
    speech = dur - RP.GAP
    t, inputs, filt, last = 0.0, ["-i", bg, "-i", voice], "", "0:v"
    for ci, c in enumerate(cs):
        p = f"{WORK}/sub_{tag}_{ci}.png"
        RP.sub_png(c, p)
        inputs += ["-i", p]
        t2 = dur if ci == len(cs) - 1 else t + speech * len(c.replace(" ", "")) / total
        filt += f"[{last}][{ci + 2}:v]overlay=0:0:enable='between(t,{t:.3f},{t2:.3f})'[o{ci}];"
        last, t = f"o{ci}", t2
    RP.run(inputs + ["-filter_complex", filt.rstrip(";"), "-map", f"[{last}]", "-map", "1:a", "-af", "apad",
                     "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "160k", "-ar", "44100", out])


# ───────────────────────── 5. 영상 자동 점검 ─────────────────────────
def qa_video(path, expect_min=30, expect_max=180):
    problems = []
    d = RP.duration(path)
    if not (expect_min <= d <= expect_max):
        problems.append(f"길이 이상 {d:.1f}초")
    info = subprocess.run([RP.FF, "-i", path], capture_output=True, text=True).stderr
    if "Audio:" not in info:
        problems.append("소리 없음")
    if "1080x1920" not in info:
        problems.append("해상도 이상")
    size = os.path.getsize(path) / 1e6
    if size > 95:
        problems.append(f"용량 큼 {size:.0f}MB")
    # 화면이 검거나 하얗게 비었는지 10장 확인
    for i in range(10):
        ts = d * (i + 0.5) / 10
        fr = f"{WORK}/qa_{i}.jpg"
        subprocess.run([RP.FF, "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", path, "-frames:v", "1", fr])
        if not os.path.exists(fr):
            problems.append(f"{ts:.0f}초 화면 추출 실패"); continue
        m = ImageStat.Stat(Image.open(fr).convert("L")).mean[0]
        if m < 8 or m > 248:
            problems.append(f"{ts:.0f}초 화면이 비어 있음(밝기 {m:.0f})")
    # 소리가 실제로 나는지
    vol = subprocess.run([RP.FF, "-i", path, "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    mv = re.search(r"mean_volume: (-?[\d.]+) dB", vol)
    if not mv or float(mv.group(1)) < -45:
        problems.append("소리가 너무 작거나 없음")
    log(f"[점검] 길이 {d:.1f}초, 용량 {size:.1f}MB, 문제 {len(problems)}개 {problems}")
    return problems


# ───────────────────────── 6. 인스타 게시 ─────────────────────────
def ig(method, path, params):
    params = dict(params, access_token=os.environ["IG_ACCESS_TOKEN"])
    url = f"{API}/{path}"
    try:
        if method == "GET":
            return json.loads(http(url + "?" + urllib.parse.urlencode(params)))
        return json.loads(http(url, data=params))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"인스타 API {e.code}: {e.read().decode()[:300]}")


def already_posted_today():
    r = ig("GET", f"{os.environ['IG_USER_ID']}/media", {"fields": "timestamp,caption", "limit": 5})
    today = now_kst().date()
    for m in r.get("data", []):
        t = dt.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(KST)
        if t.date() == today and "아침 뉴스" in (m.get("caption") or ""):
            return True
    return False


def host_on_pages(path):
    """GitHub Pages(무료)에 오늘 영상 한 개만 올려 공개 주소 만들기. 매번 덮어써서 용량이 쌓이지 않음"""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return None
    owner, name = repo.split("/")
    fname = f"news_{now_kst():%Y%m%d_%H%M%S}.mp4"
    d = os.path.abspath("pages_out")
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    shutil.copy(path, os.path.join(d, fname))
    open(os.path.join(d, ".nojekyll"), "w").close()
    open(os.path.join(d, "index.html"), "w").write("<meta charset=utf-8>news9am")
    cmds = [["git", "init", "-q"], ["git", "checkout", "-q", "-b", "gh-pages"],
            ["git", "-c", "user.name=news-bot", "-c", "user.email=news-bot@users.noreply.github.com", "add", "-A"],
            ["git", "-c", "user.name=news-bot", "-c", "user.email=news-bot@users.noreply.github.com", "commit", "-q", "-m", fname],
            ["git", "push", "-q", "-f", f"https://x-access-token:{token}@github.com/{repo}.git", "gh-pages"]]
    for c in cmds:
        r = subprocess.run(c, cwd=d, capture_output=True, text=True)
        if r.returncode:
            log("  Pages 업로드 실패:", r.stderr.replace(token, "***")[:200]); return None
    url = f"https://{owner.lower()}.github.io/{name}/{fname}"
    size = os.path.getsize(path)
    for i in range(40):  # Pages 반영까지 보통 1~3분
        time.sleep(15)
        head = subprocess.run(["curl", "-sI", url], capture_output=True, text=True).stdout
        first = head.splitlines()[0] if head else ""
        if " 200" in first and "video/mp4" in head.lower() and str(size) in head:
            log("[업로드] 공개 주소(GitHub Pages):", url)
            return url
    log("  Pages 반영 시간 초과")
    return None


def host_video(path):
    """인스타가 가져갈 수 있는 공개 주소: GitHub Pages 우선, 안 되면 임시 업로드 서비스"""
    url = host_on_pages(path)
    if url:
        return url
    tries = [("https://litterbox.catbox.moe/resources/internals/api.php", {"reqtype": "fileupload", "time": "72h"}, "fileToUpload"),
             ("https://catbox.moe/user/api.php", {"reqtype": "fileupload"}, "fileToUpload")]
    for url, fields, fname in tries:
        for attempt in range(3):
            args = ["curl", "-s", "-m", "600"]
            for k, v in fields.items():
                args += ["--form-string", f"{k}={v}"]
            args += ["-F", f"{fname}=@{path}", url]
            r = subprocess.run(args, capture_output=True, text=True).stdout.strip()
            if r.startswith("https://"):
                head = subprocess.run(["curl", "-sI", r], capture_output=True, text=True).stdout
                if " 200" in head.splitlines()[0] and "video/mp4" in head:
                    log("[업로드] 임시 주소:", r)
                    return r
            log(f"  임시 업로드 재시도 ({url.split('/')[2]}): {r[:100]}")
            time.sleep(10)
    raise RuntimeError("임시 공개 주소 업로드 실패")


def prepare_container(video_url, caption):
    uid = os.environ["IG_USER_ID"]
    for attempt in range(3):
        c = ig("POST", f"{uid}/media", {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true"})
        cid = c["id"]
        for _ in range(90):
            s = ig("GET", cid, {"fields": "status_code,status"})
            if s.get("status_code") == "FINISHED":
                log("[인스타] 게시 준비 완료:", cid)
                return cid
            if s.get("status_code") == "ERROR":
                log("  인스타 처리 오류:", s.get("status")); break
            time.sleep(10)
    raise RuntimeError("인스타 게시 준비 실패")


def wait_until(hh, mm):
    target = now_kst().replace(hour=hh, minute=mm, second=0, microsecond=0)
    left = (target - now_kst()).total_seconds()
    if left > 0:
        log(f"[대기] {target:%H:%M:%S}까지 {left / 60:.1f}분 기다림")
        while (target - now_kst()).total_seconds() > 0:
            time.sleep(min(30, max(0.2, (target - now_kst()).total_seconds())))
    else:
        log(f"[대기] 이미 {hh:02d}:{mm:02d} 지남 → 바로 게시")


def publish(cid):
    uid = os.environ["IG_USER_ID"]
    for attempt in range(5):
        try:
            p = ig("POST", f"{uid}/media_publish", {"creation_id": cid})
            m = ig("GET", p["id"], {"fields": "permalink,timestamp"})
            log(f"[게시 완료] {m.get('permalink')} ({now_kst():%H:%M:%S})")
            return m
        except Exception as e:
            log(f"  게시 재시도 {attempt + 1}: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("게시 실패")


def make_caption(data, date_label):
    nums = "①②③④⑤⑥⑦⑧"
    lines = [f"☀️ {date_label} 아침 뉴스, 핵심만 빠르게", ""]
    for i, s in enumerate(data["stories"]):
        lines.append(f"{nums[i]} {s['title'].replace(chr(10), ' ').replace(chr(92) + 'n', ' ')}")
    if data.get("weather"):
        lines.append(f"🌤️ {data['weather']}")
    lines += ["", "※ 이 영상은 자동으로 만들어졌습니다. 자세한 내용은 원문 기사를 확인해 주세요.",
              "사진: Wikimedia Commons(장면 하단 저작자·라이선스 표기) / 그림: Noto Emoji (Google, CC BY 4.0)", "",
              "#아침뉴스 #뉴스요약 #오늘의뉴스 #1분뉴스 #시사"]
    return "\n".join(lines)[:2100]


# ───────────────────────── 실행 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="영상만 만들고 게시 안 함")
    ap.add_argument("--now", action="store_true", help="9시까지 기다리지 않고 바로 게시")
    ap.add_argument("--at", default="09:00", help="게시 시각 (KST)")
    ap.add_argument("--prepare-only", action="store_true", help="게시 직전(인스타 준비)까지만 시험하고 게시 안 함")
    a = ap.parse_args()
    start = now_kst()
    days = "월화수목금토일"
    date_label = f"{start.month}월 {start.day}일 ({days[start.weekday()]})"
    today_label = f"{start.month}월 {start.day}일 {days[start.weekday()]}요일"
    log(f"시작 {start:%Y-%m-%d %H:%M:%S} KST, dry_run={a.dry_run}")
    global VET_DEADLINE
    hh, mm = map(int, a.at.split(":"))
    post_at = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # 게시 40분 전까지는 영상이 완성되도록: 사진 검수는 (게시 70분 전) 또는 (시작 후 60분) 중 빠른 때까지만
    # GitHub 예약 실행은 40~50분 늦게 시작하기도 해서, 시작 시각이 아니라 게시 시각 기준으로 넉넉히
    VET_DEADLINE = post_at - dt.timedelta(minutes=30) \
        if (not a.dry_run and not a.now and post_at > start) else start + dt.timedelta(minutes=60)

    if not a.dry_run and not a.prepare_only and already_posted_today():
        log("오늘 이미 게시됨 → 종료"); return

    news = fetch_news()
    # 무료 AI가 붐빌 때가 있어서 최대 30분 동안 몇 번이고 다시 시도
    script_deadline = now_kst() + dt.timedelta(minutes=30)
    data, rnd = None, 0
    while data is None:
        rnd += 1
        try:
            d = write_script(news, today_label)
            d = verify_script(d, news)
            if len(d["stories"]) < 3:
                raise RuntimeError("검수 후 뉴스가 너무 적음")
            data = d
        except Exception as e:
            log(f"대본 AI 실패({rnd}회차):", str(e)[:150])
            if now_kst() + dt.timedelta(minutes=2) > script_deadline:
                data = fallback_script(news)
            else:
                time.sleep(90)
    json.dump(data, open(f"script_{start:%Y%m%d}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    scenes = build_scenes(data, today_label)
    video = make_video(scenes, date_label)
    problems = qa_video(video)
    if problems:  # 한 번 다시 만들어 보고, 그래도 문제면 사진 없이(이모지만) 만들기
        log("점검 실패 → 이모지 장면으로 다시 제작")
        for sc in scenes:
            for l in sc["lines"]:
                l["image_queries"] = None  # None = 사진 없이
        video = make_video(scenes, date_label)
        problems = qa_video(video)
        if problems:
            raise RuntimeError(f"영상 점검 실패: {problems}")
    caption = make_caption(data, date_label)
    open(f"{WORK}/caption.txt", "w", encoding="utf-8").write(caption)
    if a.dry_run:
        log("[dry-run] 게시하지 않고 종료"); return

    url = host_video(video)
    cid = prepare_container(url, caption)
    if a.prepare_only:
        log("[시험] 게시 직전 단계까지 성공 → 게시하지 않고 종료 (준비된 게시물은 24시간 뒤 자동 삭제)"); return
    if not a.now:
        hh, mm = map(int, a.at.split(":"))
        wait_until(hh, mm)
    if already_posted_today():
        log("다른 실행에서 이미 게시됨 → 종료"); return
    publish(cid)


if __name__ == "__main__":
    try:
        main()
    finally:
        os.makedirs("logs", exist_ok=True)
        open(f"logs/{now_kst():%Y%m%d_%H%M%S}.log", "w", encoding="utf-8").write("\n".join(LOG))
