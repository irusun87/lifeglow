#!/usr/bin/env python3
"""완성된 쇼츠를 납품 전에 자동 검사한다.

  python3 tools/qc_short.py out.mp4            # out.ass, out.voice.wav 를 함께 사용 (make_short.py 가 남김)
  python3 tools/qc_short.py out.mp4 --no-asr   # 받아쓰기 검사 생략 (빠름)

검사 항목
  1. 0.5초 미만 짧은 컷 (장면 전환이 두 번 잡힌 오탐은 프레임 비교로 걸러냄)
  2. 통합 음량 -14 LUFS (±1)
  3. 말하는 동안 자막이 비는 구간 (말소리 트랙을 다시 받아쓰기해서 대조)
  4. 자막 글자와 실제 발음이 크게 다른 곳
  5. 자막이 의미 단위 중간에서 끊긴 곳 ("발랐을 | 때")
  6. 제목·자막·강조 라벨이 화면 밖으로 넘치는지 (자막만 따로 그려서 실측)
  7. 컷마다 한 장씩 뽑은 확인용 이미지 (<out>.qc.jpg) — 사람/Claude 가 눈으로 본다
  8. 인물 중심: 0.25초마다 얼굴을 찾아 화면 가로 40~60% 밖으로 0.5초 이상 쏠리면 오류,
     얼굴이 너무 아래(세로 60% 아래)면 경고. 원본에서 옮길 x 를 함께 알려 준다 (OpenCV 필요)
  9. 영상 튐: 같은 장면 안에서 화면이 갑자기 건너뛰는 프레임, 움직이다 멈췄다 다시 움직이는 정지 구간

오류가 하나라도 있으면 종료 코드 1.
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

W, H = 720, 1280
CAPTION_Y = 770
FONT_DIR = os.environ.get("LIFEGLOW_FONTS", "/mnt/project-files/assets/fonts/paperlogy")
BOUND_NOUNS = {"때", "수", "것", "거", "정도", "듯", "줄", "만큼", "적", "뿐", "데"}
MIN_SHOT, EDGE = 0.5, 12


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                      str(path)]).stdout)


def ass_sec(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def read_ass(path):
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        f = line[len("Dialogue:"):].split(",", 9)
        text = re.sub(r"\{[^}]*\}", "", f[9]).strip()
        events.append({"start": ass_sec(f[1]), "end": ass_sec(f[2]), "style": f[3].strip(), "text": text})
    return events


def gray_frame(src, t, w=W, h=H, lavfi=False):
    cmd = ["ffmpeg", "-v", "error"] + (["-f", "lavfi"] if lavfi else []) + ["-ss", f"{t:.3f}", "-i", src,
           "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8)[: w * h].reshape(h, w) if len(raw) >= w * h else None


# ── 1. 짧은 컷 ─────────────────────────────────────────────────────────────
def check_shots(mp4, total):
    out = run(["ffmpeg", "-v", "error", "-i", mp4, "-vf", "scdet=threshold=4,metadata=print:file=-",
               "-an", "-f", "null", "-"]).stdout
    cuts = [float(x) for x in re.findall(r"lavfi\.scd\.time=([0-9.]+)", out)]
    bounds = [0.0] + cuts + [total]
    errors, notes = [], []
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= MIN_SHOT or a == 0.0 or b == total and b - a < 0.1:
            continue
        # 한 번의 전환이 두 프레임에 걸쳐 두 번 잡힌 경우: 조각 프레임이 바로 뒤 장면과 같다
        mid, after = gray_frame(mp4, (a + b) / 2, 90, 160), gray_frame(mp4, b + 0.05, 90, 160)
        if mid is not None and after is not None and np.abs(mid.astype(int) - after).mean() < 12:
            notes.append(f"{a:.2f}s 전환이 두 번 잡힘 (실제 컷은 하나)")
        else:
            errors.append(f"{a:.2f}s 에 {b - a:.2f}초짜리 짧은 컷")
    return cuts, errors, notes


# ── 2. 음량 ───────────────────────────────────────────────────────────────
def check_loudness(mp4):
    err = run(["ffmpeg", "-i", mp4, "-af", "ebur128", "-f", "null", "-"]).stderr
    m = re.findall(r"I:\s+(-?[0-9.]+) LUFS", err)
    lufs = float(m[-1]) if m else None
    bad = lufs is None or abs(lufs + 14) > 1
    return lufs, ([f"통합 음량 {lufs} LUFS (목표 -14)"] if bad else [])


# ── 3·4. 받아쓰기 대조 ───────────────────────────────────────────────────
def transcribe(wav):
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get("QC_WHISPER", "large-v3"), device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(wav), language="ko", word_timestamps=True, vad_filter=True, beam_size=5)
    return [{"w": w.word.strip(), "s": w.start, "e": w.end} for s in segs for w in s.words]


def norm(t):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t)


def check_captions_vs_speech(words, caps):
    errors, warns = [], []
    gap = []
    for w in words + [None]:
        covered = w is not None and any(c["start"] - 0.15 <= (w["s"] + w["e"]) / 2 <= c["end"] + 0.15
                                        for c in caps)
        if w is not None and not covered:
            gap.append(w)
            continue
        if gap:
            span = gap[-1]["e"] - gap[0]["s"]
            if span >= 0.3 or len(gap) >= 2:
                errors.append(f"{gap[0]['s']:.2f}~{gap[-1]['e']:.2f}s 말하는데 자막 없음: "
                              f"\"{' '.join(g['w'] for g in gap)}\"")
            gap = []
    for c in caps:
        pad = max(0.1, (0.8 - (c["end"] - c["start"])) / 2)  # 아주 짧은 자막은 앞뒤를 조금 넓혀 비교
        heard = " ".join(w["w"] for w in words if c["start"] - pad <= (w["s"] + w["e"]) / 2 <= c["end"] + pad)
        a, b = norm(c["text"]), norm(heard)
        if not a:
            continue
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio < 0.5:
            warns.append(f"{c['start']:.2f}s 자막 \"{c['text']}\" ↔ 들린 말 \"{heard}\" (일치 {ratio:.0%})")
    return errors, warns


# ── 5. 의미 단위 끊김 ─────────────────────────────────────────────────────
def check_breaks(caps):
    errors = []
    for prev, cur in zip(caps, caps[1:]):
        first = cur["text"].split()[0].rstrip(".,?!") if cur["text"].split() else ""
        if first in BOUND_NOUNS and abs(prev["end"] - cur["start"]) < 0.3:
            errors.append(f"{cur['start']:.2f}s \"{prev['text']} | {cur['text']}\" — '{first}'가 앞 말과 떨어짐")
    return errors


# ── 6. 화면 넘침 ─────────────────────────────────────────────────────────
def check_overflow(ass, events, total):
    """자막만 검은 화면에 그려서 글자 영역의 좌우 끝을 잰다."""
    src = f"color=black:s={W}x{H}:r=30:d={total:.3f},ass={ass}:fontsdir={FONT_DIR}"
    bands = {"Title": (90, 290), "Cap": (CAPTION_Y - 60, CAPTION_Y + 60), "CapY": (CAPTION_Y - 60, CAPTION_Y + 60)}
    errors, seen = [], set()
    for e in events:
        key = (e["style"], e["text"])
        if key in seen or not e["text"]:
            continue
        seen.add(key)
        y0, y1 = bands.get(e["style"], (0, H))
        if e["style"] not in bands and e["style"] != "Label":
            continue
        img = gray_frame(src, min(e["start"] + 0.4, (e["start"] + e["end"]) / 2), lavfi=True)
        if img is None:
            continue
        cols = np.where((img[y0:y1] > 40).any(axis=0))[0]
        if len(cols) and (cols[0] < EDGE or cols[-1] > W - EDGE):
            errors.append(f"{e['start']:.2f}s [{e['style']}] \"{e['text']}\" 화면 밖으로 넘침 "
                          f"(x {cols[0]}~{cols[-1]})")
    return errors


# ── 8. 인물 중심 ─────────────────────────────────────────────────────────
VIDEO_Y, VIDEO_H = 273, 774
CENTER_LO, CENTER_HI, CENTER_RUN = 0.40, 0.60, 0.5


def video_frames(mp4, fps, w, h, gray=True, vh=VIDEO_H):
    """영상 칸(제목·아래 띠 제외)만 잘라 fps 간격 프레임 배열로 읽는다. vh 로 아래 자막 줄을 뺄 수 있다."""
    fmt, ch = ("gray", 1) if gray else ("bgr24", 3)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", mp4, "-vf",
                          f"crop={W}:{vh}:0:{VIDEO_Y},fps={fps},scale={w}:{h}", "-f", "rawvideo",
                          "-pix_fmt", fmt, "-"], capture_output=True).stdout
    n = len(raw) // (w * h * ch)
    return np.frombuffer(raw[: n * w * h * ch], np.uint8).reshape((n, h, w) if gray else (n, h, w, ch))


def check_center(mp4, cuts):
    try:
        import cv2
    except ImportError:
        return [], ["OpenCV 없음: 인물 중심 검사 생략 (pip install opencv-python-headless)"]
    base = cv2.data.haarcascades
    front = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
    prof = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")
    eye = cv2.CascadeClassifier(base + "haarcascade_eye.xml")
    step = 0.25
    frames = video_frames(mp4, 1 / step, W, VIDEO_H)
    hits = []
    for i, g in enumerate(frames):
        found = list(front.detectMultiScale(g, 1.1, 6, minSize=(90, 90)))
        found += list(prof.detectMultiScale(g, 1.1, 6, minSize=(90, 90)))
        flip = prof.detectMultiScale(g[:, ::-1].copy(), 1.1, 6, minSize=(90, 90))
        found += [(W - x - w, y, w, h) for x, y, w, h in flip]
        if found:
            x, y, w, h = max(found, key=lambda f: f[2] * f[3])
            hits.append((i * step, (x + w / 2) / W, (y + h / 2) / VIDEO_H))
            continue
        # 얼굴이 화면보다 큰 클로즈업: 두 눈의 가운데를 얼굴 중심으로 본다
        eyes = sorted(eye.detectMultiScale(g, 1.1, 8, minSize=(70, 40)), key=lambda e: -e[2] * e[3])[:2]
        if len(eyes) == 2 and abs(eyes[0][1] - eyes[1][1]) < 80 and abs(eyes[0][0] - eyes[1][0]) > 150:
            cx = sum(e[0] + e[2] / 2 for e in eyes) / 2
            cy = sum(e[1] + e[3] / 2 for e in eyes) / 2
            hits.append((i * step, cx / W, cy / VIDEO_H))
    if os.environ.get("QC_CENTER_DEBUG"):
        for h in hits:
            print(f"    얼굴 {h[0]:6.2f}s 가로 {h[1]:.0%} 세로 {h[2]:.0%}")
    errors, warns = [], []
    shot = lambda t: sum(c <= t for c in cuts)
    run = []
    for h in hits + [None]:
        bad = h is not None and not CENTER_LO <= h[1] <= CENTER_HI
        if bad and (not run or (shot(h[0]) == shot(run[-1][0]) and h[0] - run[-1][0] <= step * 1.5)):
            run.append(h)
            continue
        if run and run[-1][0] - run[0][0] + step >= CENTER_RUN:
            cx = float(np.median([r[1] for r in run]))
            side = "왼쪽" if cx < 0.5 else "오른쪽"
            errors.append(f"{run[0][0]:.2f}~{run[-1][0] + step:.2f}s 인물이 {side}으로 쏠림 (가로 {cx:.0%}) → "
                          f"인물을 {abs(cx - 0.5) * W:.0f}px {'오른' if cx < 0.5 else '왼'}쪽으로 옮겨야 함 "
                          f"(spec 크롭 x 를 {'줄임' if cx < 0.5 else '늘림'})")
        run = [h] if bad else []
    low = [h for h in hits if h[2] > 0.6]
    if len(low) >= 2:
        warns.append(f"인물 얼굴이 너무 아래 (세로 60% 아래): " + ", ".join(f"{h[0]:.1f}s" for h in low[:8]))
    return errors, warns


# ── 9. 영상 튐 ───────────────────────────────────────────────────────────
def check_jumps(mp4, cuts):
    # 자막(영상 안 y≈985)이 바뀌는 건 튐이 아니므로 자막 줄 위쪽만 본다
    fr = video_frames(mp4, 30, 90, 80, vh=640).astype(np.int16)
    if len(fr) < 10:
        return [], []
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))  # d[i] = 프레임 i → i+1
    t = (np.arange(len(d)) + 1) / 30
    near_cut = lambda x: any(abs(x - c) < 0.07 for c in cuts)
    errors, warns = [], []
    for i in range(len(d)):
        if near_cut(t[i]):
            continue
        win = np.r_[d[max(0, i - 8):i], d[i + 1:i + 9]]
        m = float(np.median(win)) if len(win) else 0
        # 같은 장면인데 한 프레임만 확 바뀜 = 앞뒤로 건너뜀 (scdet 문턱보다 약한 점프)
        # 눈 깜빡임·빠른 손동작처럼 여러 프레임에 걸친 움직임은 빼고, 앞뒤 프레임은 조용한 한 프레임짜리 점프만 본다
        calm = lambda j: 0 <= j < len(d) and d[j] < max(2 * m, 3.0)
        if d[i] > max(4 * m, 5.0) and d[i] < 40 and calm(i - 1) and calm(i + 1):
            errors.append(f"{t[i]:.2f}s 같은 장면 안에서 화면이 튐 (변화 {d[i]:.1f}, 주변 {m:.1f})")
    # 움직이던 화면이 멈췄다가 다시 움직이거나, 컷 직전에 멈춤
    still = d < 0.15
    i = 0
    while i < len(d):
        if not still[i]:
            i += 1
            continue
        j = i
        while j < len(d) and still[j]:
            j += 1
        dur = (j - i) / 30
        before = float(np.median(d[max(0, i - 10):i])) if i else 0
        after = float(np.median(d[j:j + 10])) if j < len(d) else 0
        if dur >= 0.15 and before > 0.8:
            ends_at_cut = j >= len(d) or near_cut(t[min(j, len(d) - 1)])
            msg = f"{t[i]:.2f}~{t[min(j, len(d) - 1)]:.2f}s 화면 멈춤 {dur:.2f}초"
            if not ends_at_cut and after > 0.8:
                errors.append(msg + " 뒤 다시 움직임 (끊겨 보임)")
            elif dur >= 0.2:
                warns.append(msg + " (컷 직전 정지, 클립 길이 부족)")
        i = j
    return errors, warns


# ── 7. 확인용 이미지 ─────────────────────────────────────────────────────
def contact_sheet(mp4, cuts, total, out):
    bounds = [0.0] + cuts + [total]
    times = [(a + b) / 2 for a, b in zip(bounds, bounds[1:]) if b - a >= 0.1]
    tmp = Path(out).with_suffix(".qcframes")
    tmp.mkdir(exist_ok=True)
    for i, t in enumerate(times):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", mp4, "-frames:v", "1", "-vf",
                        "scale=180:-1,drawtext=text='%.1f':x=4:y=4:fontsize=18:fontcolor=yellow:box=1:"
                        "boxcolor=black" % t, str(tmp / f"{i:03}.png")])
    cols = min(8, len(times))
    rows = (len(times) + cols - 1) // cols
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp / "%03d.png"), "-vf",
                    f"tile={cols}x{rows}", "-frames:v", "1", "-q:v", "3", str(out)])
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return times


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mp4")
    p.add_argument("--ass", help="자막 파일 (기본: <mp4>.ass)")
    p.add_argument("--voice", help="BGM 없는 말소리 트랙 (기본: <mp4>.voice.wav, 없으면 영상 소리)")
    p.add_argument("--no-asr", action="store_true")
    a = p.parse_args()

    mp4 = a.mp4
    ass = Path(a.ass or Path(mp4).with_suffix(".ass"))
    voice = Path(a.voice or Path(mp4).with_suffix(".voice.wav"))
    total = duration(mp4)
    report = {"file": mp4, "duration": round(total, 2), "errors": [], "warnings": [], "notes": []}

    cuts, err, notes = check_shots(mp4, total)
    report["errors"] += err
    report["notes"] += notes
    err, warn = check_center(mp4, cuts)
    report["errors"] += err
    report["warnings"] += warn
    err, warn = check_jumps(mp4, cuts)
    report["errors"] += err
    report["warnings"] += warn
    lufs, err = check_loudness(mp4)
    report["lufs"] = lufs
    report["errors"] += err
    if total > 40:
        report["warnings"].append(f"길이 {total:.1f}초 (채널 기준 21~33초)")

    if ass.exists():
        events = read_ass(ass)
        caps = sorted((e for e in events if e["style"] in ("Cap", "CapY")), key=lambda e: e["start"])
        report["errors"] += check_breaks(caps)
        report["errors"] += check_overflow(ass, events, total)
        if not a.no_asr:
            words = transcribe(voice if voice.exists() else mp4)
            err, warn = check_captions_vs_speech(words, caps)
            report["errors"] += err
            report["warnings"] += warn
    else:
        report["warnings"].append(f"자막 파일 {ass} 없음: 자막 검사 생략")

    sheet = Path(mp4).with_suffix(".qc.jpg")
    contact_sheet(mp4, cuts, total, sheet)
    report["sheet"] = str(sheet)
    report["shots"] = len(cuts) + 1

    Path(mp4).with_suffix(".qc.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"검사: {mp4}  ({total:.1f}초, {len(cuts) + 1}컷, {lufs} LUFS)")
    for e in report["errors"]:
        print("  ❌", e)
    for w in report["warnings"]:
        print("  ⚠️ ", w)
    for n in report["notes"]:
        print("  ·", n)
    print("  확인용 이미지:", sheet)
    print("통과" if not report["errors"] else f"오류 {len(report['errors'])}건")
    sys.exit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
