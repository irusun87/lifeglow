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
