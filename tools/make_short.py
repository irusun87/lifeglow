#!/usr/bin/env python3
"""라이프글로우 쇼츠 한 편을 스펙(JSON)대로 렌더링한다.

레이아웃 (720x1280, 30fps):
  - 상단 2줄 제목 고정 (1줄 노랑 / 2줄 흰색, 강조 키워드 하늘색)
  - 가운데 원본 클립 720x774 (y=273), 문장마다 1~2컷
  - 클립 아래쪽에 1~3어절 자막 (흰 글씨 + 검은 외곽선), 포인트 그래픽(칩·큰 라벨)
  - 내레이션 구간(TTS)과 원본 발언 구간(type: original, 원본 소리 + 노란 자막)을 번갈아 배치, BGM은 mix_audio.py로 덕킹

TTS 폴더(tools/typecast_tts.py 결과: 01.wav, 01.json …)가 있으면 문장 길이와 자막 타이밍을
그 음성에 맞춘다. 없으면 글자 수로 길이를 추정해 무음 미리보기를 만든다.

사용 예:
  python3 tools/make_short.py shorts/ep01_baejongok.json --tts out/ep01/tts --out ep01.mp4
  python3 tools/make_short.py shorts/ep01_baejongok.json --out preview.mp4   # 무음 미리보기
"""

import argparse
import shutil
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

W, H, FPS = 720, 1280, 30
# 기존 채널 영상(차승원·박미선 편) 실측: 영상 720x774 @ y=273, 제목 1줄 y≈147, 2줄 y≈233, 자막 y≈770
VIDEO_Y, VIDEO_H = 273, 774
TITLE_SIZE, TITLE_Y1, TITLE_Y2 = 68, 147, 233
CAPTION_Y = 770
FONT_DIR = os.environ.get("LIFEGLOW_FONTS", "/mnt/project-files/assets/fonts/paperlogy")
FONT = "Paperlogy 8 ExtraBold"
TOOLS = Path(__file__).resolve().parent

YELLOW, WHITE, CYAN, BLACK = "FCF103", "FFFFFF", "00FEFD", "000000"  # 기존 채널 영상 실측 색


def ass_color(rgb, alpha="00"):
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def estimate_words(text, rate):
    """TTS가 없을 때: 어절별 길이를 글자 수 비례로 나눈 가짜 타임스탬프."""
    words = text.split()
    weights = [max(len(re.sub(r"[^\w]", "", w)), 1) for w in words]
    total = sum(weights) / rate
    t, out = 0.0, []
    for w, n in zip(words, weights):
        d = n / rate
        out.append({"text": w, "start": t, "end": t + d})
        t += d
    return out, total + 0.25


def words_from_audio(text, wav, dur):
    """타임스탬프 없는 TTS 파일: 쉼표 위치를 음성 속 쉼(무음)에 맞추고, 구간 안에서는 글자 수 비례."""
    phrases = [ph.strip() for ph in re.split(r"(?<=,)\s+", text) if ph.strip()]
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(wav), "-af", "silencedetect=n=-40dB:d=0.1",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", out)]
    pauses = [(a, b) for a, b in zip(starts, ends) if 0.05 < a and b < dur - 0.05]
    pauses = sorted(sorted(pauses, key=lambda p: p[0] - p[1])[:len(phrases) - 1])
    if len(pauses) != len(phrases) - 1:
        pauses, phrases = [], [text]
    bounds = [0.0] + [x for p in pauses for x in p] + [dur]
    words = []
    for k, ph in enumerate(phrases):
        a, b = bounds[2 * k], bounds[2 * k + 1]
        ws, _ = estimate_words(ph, 1.0)
        span = ws[-1]["end"] or 1.0
        words += [{"text": w["text"], "start": a + w["start"] / span * (b - a),
                   "end": a + w["end"] / span * (b - a)} for w in ws]
    return words


def extract_original(src, parts, out, lufs=-16):
    """원본 영상의 발언 구간들을 잘라 이어 붙이고 내레이션과 비슷한 크기로 맞춘다."""
    ins, filt = [], []
    for i, p in enumerate(parts):
        d = p["end"] - p["start"]
        ins += ["-ss", f"{p['start']:.3f}", "-t", f"{d:.3f}", "-i", src]
        # 이어지는 구간(앞 구간 끝 = 이 구간 시작, 화면 위치만 바꾼 경우)은 페이드 없이 붙인다
        fin = "" if i > 0 and abs(parts[i - 1]["end"] - p["start"]) < 0.01 else "afade=t=in:d=0.03,"
        fout = ("" if i + 1 < len(parts) and abs(parts[i + 1]["start"] - p["end"]) < 0.01
                else f"afade=t=out:st={max(d - 0.04, 0):.3f}:d=0.04,")
        filt.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=mono,{fin}{fout}anull[p{i}]")
    filt.append("".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1,"
                f"loudnorm=I={lufs}:TP=-2:LRA=11,aresample=48000")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", ";".join(filt), str(out)],
                   check=True)


def scene_cuts(src, until, threshold=6.0):
    """원본 영상의 장면 전환 시각(초) 목록."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-t", f"{until:.2f}", "-i", src, "-an", "-vf",
                          f"scale=480:-2,scdet=threshold={threshold}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return [float(x) for x in re.findall(r"lavfi\.scd\.time: ([0-9.]+)", out)]


def avoid_slivers(start, dur, cuts, min_shot=0.5):
    """클립 앞뒤에 min_shot초보다 짧은 장면 조각이 걸리면 그 조각 대신 이웃 장면의 첫/끝 프레임을 늘린다.
    화면만 바뀌고 길이는 그대로라 원본 발언 구간의 소리 싱크가 유지된다. → (영상 시작, 앞 패딩, 영상 길이)"""
    inner = [c for c in cuts if start < c < start + dur]
    vs, ve = start, start + dur
    if inner and inner[0] - start < min_shot:
        vs = inner[0]
    if inner and start + dur - inner[-1] < min_shot and inner[-1] > vs:
        ve = inner[-1]
    return vs, vs - start, max(ve - vs, 0.04)


def face_x(src, start, end, default=None):
    """클립 구간에서 가장 큰 얼굴의 가로 중심(원본 픽셀)을 찾는다. OpenCV가 없거나 못 찾으면 화면 가운데."""
    src_w = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width", "-of", "csv=p=0", src], capture_output=True, text=True).stdout)
    if default is None:
        default = src_w // 2
    try:
        import cv2
        import numpy as np
    except ImportError:
        return default
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    prof = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    xs = []
    for k in range(5):
        t = start + (end - start) * (k + 0.5) / 5
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
                              "-vf", "scale=960:540", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True).stdout
        if len(raw) != 960 * 540:
            continue
        g = np.frombuffer(raw, np.uint8).reshape(540, 960)
        faces = list(casc.detectMultiScale(g, 1.1, 5, minSize=(50, 50)))
        if not faces:
            faces = list(prof.detectMultiScale(g, 1.1, 5, minSize=(50, 50)))
            faces += [(960 - x - w, y, w, h) for x, y, w, h in prof.detectMultiScale(g[:, ::-1].copy(), 1.1, 5, minSize=(50, 50))]
        if faces:
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + w / 2) * src_w / 960)
    return int(sorted(xs)[len(xs) // 2]) if xs else default


# 앞 단어와 떨어지면 어색한 의존 명사 ("발랐을 | 때" 같은 끊김 방지)
BOUND_NOUNS = {"때", "수", "것", "거", "정도", "듯", "줄", "만큼", "적", "뿐", "데"}


def chunk_words(words, max_words=3, max_chars=12):
    chunk = []
    for w in words:
        bound = w["text"].rstrip(".,?!") in BOUND_NOUNS
        if chunk and not bound and (len(chunk) >= max_words or
                      len(" ".join(x["text"] for x in chunk + [w])) > max_chars):
            yield chunk
            chunk = []
        chunk.append(w)
        if w["text"].rstrip().endswith((".", "?", "!", ",")):
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def rounded_rect(w, h, r):
    """ASS 벡터 드로잉용 둥근 사각형 경로."""
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def title_line(parts):
    """[[텍스트, 색]] 또는 "텍스트" → ASS 인라인 색 태그."""
    if isinstance(parts, str):
        return parts
    return "".join(f"{{\\c{ass_color(c)}}}{t}" for t, c in parts)


def build_ass(spec, timeline, total):
    fs = spec.get("caption_size", 60)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Title,{FONT},{TITLE_SIZE},{ass_color(WHITE)},{ass_color(WHITE)},{ass_color(BLACK)},"
        f"{ass_color(BLACK)},0,0,0,0,100,100,0,0,1,2,0,5,10,10,0,1",
        f"Style: Cap,{FONT},{fs},{ass_color(WHITE)},{ass_color(WHITE)},{ass_color(BLACK)},"
        f"{ass_color(BLACK, '80')},0,0,0,0,100,100,0,0,1,5,2,5,20,20,0,1",
        f"Style: CapY,{FONT},{fs},{ass_color(YELLOW)},{ass_color(YELLOW)},{ass_color(BLACK)},"
        f"{ass_color(BLACK, '80')},0,0,0,0,100,100,0,0,1,5,2,5,20,20,0,1",
        f"Style: Chip,{FONT},44,{ass_color(WHITE)},{ass_color(WHITE)},{ass_color(BLACK)},"
        f"{ass_color(BLACK)},0,0,0,0,100,100,0,0,3,8,0,5,0,0,0,1",
        f"Style: Big,{FONT},120,{ass_color(YELLOW)},{ass_color(YELLOW)},{ass_color(BLACK)},"
        f"{ass_color(BLACK, '60')},0,0,0,0,100,100,0,0,1,7,4,5,0,0,0,1",
        f"Style: Draw,{FONT},10,{ass_color(BLACK)},{ass_color(BLACK)},{ass_color(BLACK)},"
        f"{ass_color(BLACK)},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: Label,{FONT},52,{ass_color(WHITE)},{ass_color(WHITE)},{ass_color(BLACK)},"
        f"{ass_color(BLACK)},0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def ev(start, end, style, text, layer=0):
        lines.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}")

    t1, t2 = spec["title"]
    tsize = spec.get("title_size", TITLE_SIZE)
    ev(0, total, "Title", f"{{\\pos({W // 2},{TITLE_Y1})\\fs{tsize}\\c{ass_color(YELLOW)}}}{title_line(t1)}")
    ev(0, total, "Title", f"{{\\pos({W // 2},{TITLE_Y2})\\fs{tsize}\\c{ass_color(WHITE)}}}{title_line(t2)}")

    cap_y = CAPTION_Y
    for seg in timeline:
        s0, s1 = seg["start"], seg["end"]
        if seg.get("type") == "original":
            for a, b, text in seg["captions"]:
                ev(a, b, "CapY", f"{{\\pos({W // 2},{cap_y})}}{text}", layer=2)
            chunks = []
        elif seg.get("lines"):
            # 대본에 적은 의미 단위대로 자막을 끊는다 (단어 수로 TTS 단어 타이밍에 맞춤)
            chunks, i = [], 0
            for line in seg["lines"]:
                n = len(line.split())
                chunks.append(seg["words"][i:i + n])
                i += n
            if i != len(seg["words"]):
                raise SystemExit(f"자막 줄 단어 수가 대본과 다름: {seg['text']}")
        else:
            chunks = list(chunk_words(seg["words"], max_chars=seg.get("max_chars", 12)))
        for i, chunk in enumerate(chunks):
            a = s0 + chunk[0]["start"]
            b = s0 + chunks[i + 1][0]["start"] if i + 1 < len(chunks) else s1
            text = " ".join(w["text"] for w in chunk).rstrip(".,")
            ev(a, b, "Cap", f"{{\\pos({W // 2},{cap_y})}}{text}", layer=2)

        for ov in seg.get("overlay", []):
            a = s0 + ov.get("at", 0.0) * (s1 - s0)
            s1 = seg["start"] + ov.get("until", 1.0) * (seg["end"] - seg["start"])
            if "at_word" in ov:
                hit = [w for w in seg["words"] if w["text"].startswith(ov["at_word"])]
                if hit:
                    a = seg["start"] + hit[0]["start"]
            if "at_sec" in ov:
                a = seg["start"] + ov["at_sec"]
            if ov["type"] == "label":
                # 반투명 둥근 라벨: 짧게(기본 1.8초) 떴다가 사라진다
                b = a + ov.get("dur", 1.8)
                size = ov.get("size", 52)
                parts = ov["text"] if isinstance(ov["text"], list) else [[ov["text"], WHITE]]
                plain = "".join(t for t, _ in parts)
                tw = sum(size * (0.95 if ord(ch) > 0x3000 else 0.55) for ch in plain)
                bw, bh = int(tw + size * 1.3), int(size * 1.75)
                y = ov.get("y", VIDEO_Y + 103)
                fade = "\\fad(150,150)"
                ev(a, b, "Draw", f"{{\\pos({W // 2 - bw // 2},{y - bh // 2})\\p1\\c{ass_color(BLACK)}"
                                 f"\\1a&H50&{fade}}}{rounded_rect(bw, bh, bh // 2)}", layer=4)
                ev(a, b, "Label", f"{{\\pos({W // 2},{y})\\fs{size}{fade}}}{title_line(parts)}", layer=5)
                continue
            if ov["type"] == "chips":
                items, per_row = ov["items"], ov.get("per_row", 4)
                y0 = ov.get("y", cap_y - 170)
                n_rows = (len(items) + per_row - 1) // per_row
                for i, it in enumerate(items):
                    row, col = divmod(i, per_row)
                    n_in_row = min(per_row, len(items) - row * per_row)
                    gap = ov.get("gap", 165)
                    x = W // 2 + int((col - (n_in_row - 1) / 2) * gap)
                    y = y0 - (n_rows - 1 - row) * 78
                    appear = a + (i * ov.get("stagger", 0.0))
                    text = it["text"]
                    tag = (f"{{\\pos({x},{y})\\3c{ass_color(it.get('color', 'E53935'))}"
                           f"\\fs{ov.get('size', 44)}\\fad(120,0)}}")
                    ev(appear, s1, "Chip", tag + text, layer=1)
                    if it.get("cross"):
                        # 칩 오른쪽 위에 빨간 X 표시
                        cx, cy = x + int(len(text) * ov.get("size", 44) * 0.5) + 24, y - 26
                        ev(appear + 0.25, s1, "Big",
                           f"{{\\pos({cx},{cy})\\fs64\\c{ass_color('FF2D2D')}\\3c{ass_color(WHITE)}\\bord4"
                           f"\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)}}X", layer=3)
            elif ov["type"] == "big":
                y = ov.get("y", VIDEO_Y + 230)
                ev(a, s1, "Big", f"{{\\pos({W // 2},{y})\\fad(100,0)\\fscx80\\fscy80"
                                 f"\\t(0,150,\\fscx100\\fscy100)}}{title_line(ov['text'])}", layer=1)
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("spec")
    p.add_argument("--tts", help="typecast_tts.py 결과 폴더 (01.wav, 01.json …)")
    p.add_argument("--out", required=True)
    p.add_argument("--rate", type=float, default=7.0, help="TTS 없을 때 초당 글자 수 추정치")
    args = p.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    src = spec["source"]
    work = Path(tempfile.mkdtemp(prefix="short_"))

    # 1) 구간별 길이와 단어 타이밍
    #    내레이션 구간은 TTS 파일(01, 02 … 내레이션 순서), 원본 구간은 원본 영상의 소리와 화면을 그대로 쓴다.
    timeline, t, voices, k = [], 0.0, [], 0
    for seg in spec["segments"]:
        if seg.get("type") == "original":
            parts = seg["parts"]
            d = sum(p["end"] - p["start"] for p in parts)
            caps, off = [], t
            for p in parts:
                for a, b, text in seg.get("captions", []):
                    # 파트 경계를 넘는 자막은 다음 파트에서도 이어서 보여 준다
                    lo, hi = max(a, p["start"]), min(b, p["end"])
                    if hi - lo < 0.05:
                        continue
                    piece = (off + lo - p["start"], off + hi - p["start"], text)
                    if caps and caps[-1][2] == text and abs(caps[-1][1] - piece[0]) < 0.05:
                        caps[-1] = (caps[-1][0], piece[1], text)
                    else:
                        caps.append(piece)
                off += p["end"] - p["start"]
            wav = work / f"orig_{len(timeline):02}.wav"
            extract_original(src, parts, wav)
            voices.append(wav)
            timeline.append({**seg, "clips": parts, "start": t, "end": t + d, "words": [],
                             "captions": caps})
            t += d
            continue
        k += 1
        found = [f for f in sorted(Path(args.tts).glob(f"{k:02}.*"))
                 if f.suffix.lower() in (".wav", ".mp3", ".m4a")] if args.tts else []
        if args.tts and not found:
            sys.exit(f"{args.tts}에 {k:02}번 내레이션 음성(wav/mp3/m4a)이 없습니다")
        if found:
            wav = found[0]
            meta = Path(args.tts) / f"{k:02}.json"
            d = duration(wav)
            words = json.loads(meta.read_text(encoding="utf-8"))["words"] if meta.exists() else None
            if not words:
                words = words_from_audio(seg["text"], wav, d)
        else:
            words, d = estimate_words(seg["text"], args.rate)
            wav = work / f"silence_{k:02}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                            "-t", f"{d:.3f}", str(wav)], check=True)
        voices.append(wav)
        timeline.append({**seg, "start": t, "end": t + d, "words": words})
        t += d
    total = t

    # 2) 컷 편집: 문장 길이를 클립들에 나눠 배정 → 720x774
    crop = spec.get("crop", {"y": 100, "h": 730})
    ch = crop["h"]
    cw = round(ch * W / VIDEO_H) // 2 * 2
    inputs, filters, labels, n = [], [], [], 0
    last = max(c["end"] for seg in timeline for c in seg["clips"]) + 1
    cuts = scene_cuts(src, last)
    src_w = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width", "-of", "csv=p=0", src], capture_output=True, text=True).stdout)
    for seg in timeline:
        d = seg["end"] - seg["start"]
        clips = seg["clips"]
        lens = [c["end"] - c["start"] for c in clips]
        for c, ln in zip(clips, lens):
            cd = d * ln / sum(lens)
            if cd > ln + 0.05:
                print(f"경고: {c['start']}~{c['end']} 클립이 {cd - ln:.2f}s 부족해 마지막 프레임을 늘립니다",
                      file=sys.stderr)
            cx = c.get("x", "auto")
            if cx == "auto":
                cx = face_x(src, c["start"], c["start"] + cd)
                print(f"얼굴 위치 자동: {c['start']:.2f}~{c['start'] + cd:.2f} → x={cx}", file=sys.stderr)
            x = min(max(cx - cw // 2, 0), src_w - cw)
            vs, lead, vd = avoid_slivers(c["start"], cd, cuts)
            if lead > 0.02 or vd < cd - 0.02:
                print(f"짧은 장면 조각 제거: {c['start']:.2f}~{c['start'] + cd:.2f} → 화면 {vs:.2f}~{vs + vd:.2f}",
                      file=sys.stderr)
            inputs += ["-ss", f"{vs:.3f}", "-t", f"{vd:.3f}", "-i", src]
            pre = f"delogo={spec['delogo']}," if spec.get("delogo") else ""
            filters.append(
                f"[{n}:v]{pre}crop={cw}:{ch}:{x}:{c.get('y', crop['y'])},scale={W}:{VIDEO_H}:flags=lanczos,"
                f"fps={FPS},setsar=1,tpad=start_mode=clone:start_duration={lead:.3f}:stop_mode=clone:stop_duration=3,"
                f"trim=duration={cd:.3f},"
                f"setpts=PTS-STARTPTS[v{n}]")
            labels.append(f"[v{n}]")
            n += 1
    filters.append("".join(labels) + f"concat=n={n}:v=1:a=0,"
                   f"pad={W}:{H}:0:{VIDEO_Y}:black[cut]")
    ass = work / "overlay.ass"
    ass.write_text(build_ass(spec, timeline, total), encoding="utf-8")
    filters.append(f"[cut]ass={ass}:fontsdir={FONT_DIR}[vout]")
    video = work / "video.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(filters),
                    "-map", "[vout]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "19", "-pix_fmt", "yuv420p", str(video)], check=True)

    # 3) 오디오: 내레이션 + 원본 발언을 순서대로 이어 붙여 말소리 트랙 → BGM 믹스(말소리 구간 덕킹)
    voice = work / "voice.wav"
    ins = sum((["-i", str(v)] for v in voices), [])
    pre = "".join(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=mono[a{i}];" for i in range(len(voices)))
    subprocess.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex",
                    pre + "".join(f"[a{i}]" for i in range(len(voices))) + f"concat=n={len(voices)}:v=0:a=1",
                    str(voice)], check=True)
    mix = work / "mix.wav"
    subprocess.run([sys.executable, str(TOOLS / "mix_audio.py"), str(voice), "--out", str(mix)], check=True)
    audio, afilter = ["-i", str(mix)], []

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), *audio, *afilter,
                    "-map", "0:v", "-map", "1:a", "-t", f"{total:.3f}", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", args.out],
                   check=True)
    # 검사용 부산물: 자막 파일과 BGM 없는 말소리 트랙 (tools/qc_short.py 가 사용)
    shutil.copy(ass, Path(args.out).with_suffix(".ass"))
    shutil.copy(voice, Path(args.out).with_suffix(".voice.wav"))
    print(f"완료: {args.out} ({total:.2f}s, {n}컷)")


if __name__ == "__main__":
    main()
