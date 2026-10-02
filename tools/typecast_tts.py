#!/usr/bin/env python3
"""Typecast API로 대본을 TTS 음성 + 단어 타임스탬프로 만든다.

API 키는 환경 변수 TYPECAST_API_KEY 에서만 읽는다 (코드/채팅에 키를 넣지 말 것).

사용 예:
  # 사용 가능한 한국어 보이스 목록
  python3 tools/typecast_tts.py voices

  # 대본(한 줄 = 한 문장)을 문장별 wav + 전체 합본 + 자막(SRT)으로 생성
  python3 tools/typecast_tts.py speak script.txt --out out/ep01   # 기본: 수빈/일반/1.1배속/쉼 0초
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.typecast.ai"
DEFAULT_MODEL = "ssfm-v30"
# 라이프글로우 기본 보이스 설정: 수빈 / 일반 / 1.1배속 / 문장 사이 쉼 0초
DEFAULT_VOICE = "수빈"


def api_key():
    key = os.environ.get("TYPECAST_API_KEY")
    if not key:
        sys.exit("TYPECAST_API_KEY 환경 변수가 없습니다. Project settings에서 등록하세요.")
    return key


def request(method, path, body=None, query=None):
    url = API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-KEY", api_key())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Typecast API 오류 {e.code} ({path}): {e.read().decode(errors='replace')}")


def cmd_voices(args):
    query = {"model": args.model}
    if args.gender:
        query["gender"] = args.gender
    voices = request("GET", "/v2/voices", query=query)
    for v in voices:
        print(f"{v.get('voice_id')}\t{v.get('voice_name')}\t{v.get('gender', '')}\t{v.get('age', '')}"
              f"\t{','.join(v.get('use_cases') or [])}")


def resolve_voice(voice, model):
    """voice_id(tc_/uc_)는 그대로, 보이스 이름(예: 수빈)이면 목록에서 찾아 voice_id로 바꾼다."""
    if voice.startswith(("tc_", "uc_")):
        return voice
    matches = [v for v in request("GET", "/v2/voices", query={"model": model})
               if (v.get("voice_name") or "").strip() == voice]
    if not matches:
        sys.exit(f"'{voice}' 보이스를 찾지 못했습니다. `voices` 명령으로 이름을 확인하세요.")
    if len(matches) > 1:
        print(f"'{voice}' 보이스가 여러 개라 첫 번째를 사용: "
              + ", ".join(v["voice_id"] for v in matches), file=sys.stderr)
    return matches[0]["voice_id"]


def synthesize(text, args):
    body = {
        "voice_id": args.voice,
        "text": text,
        "model": args.model,
        "language": "kor",
        "prompt": {
            "emotion_type": "preset",
            "emotion_preset": args.emotion,
            "emotion_intensity": args.intensity,
        },
        "output": {
            "audio_format": "wav",
            "audio_tempo": args.tempo,
            "audio_pitch": args.pitch,
            "target_lufs": -16,
        },
    }
    return request("POST", "/v1/text-to-speech/with-timestamps", body, {"granularity": "word"})


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def chunk_words(words, max_words, max_chars):
    """쇼츠 자막처럼 1~3어절 단위로 묶는다."""
    chunk = []
    for w in words:
        if chunk and (len(chunk) >= max_words or
                      len(" ".join(x["text"] for x in chunk + [w])) > max_chars):
            yield chunk
            chunk = []
        chunk.append(w)
        if w["text"].rstrip().endswith((".", "?", "!", ",")):
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def cmd_speak(args):
    args.voice = resolve_voice(args.voice, args.model)
    lines = [l.strip() for l in Path(args.script).read_text(encoding="utf-8").splitlines() if l.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    all_words, wavs, offset = [], [], 0.0
    for i, text in enumerate(lines, 1):
        res = synthesize(text, args)
        wav = out / f"{i:02}.wav"
        wav.write_bytes(base64.b64decode(res["audio"]))
        wavs.append(wav)
        words = res.get("words") or []
        (out / f"{i:02}.json").write_text(
            json.dumps({"text": text, "duration": res["audio_duration"], "words": words},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        for w in words:
            all_words.append({"text": w["text"], "start": w["start"] + offset, "end": w["end"] + offset})
        offset += res["audio_duration"] + args.gap
        print(f"[{i}/{len(lines)}] {res['audio_duration']:.2f}s  {text}")

    # 문장 사이에 gap 초 무음을 넣어 하나의 narration.wav 로 합친다.
    inputs, filters = [], []
    for n, wav in enumerate(wavs):
        inputs += ["-i", str(wav)]
        filters.append(f"[{n}:a]apad=pad_dur={args.gap}[a{n}]" if n < len(wavs) - 1 and args.gap > 0 else f"[{n}:a]anull[a{n}]")
    concat = "".join(f"[a{n}]" for n in range(len(wavs))) + f"concat=n={len(wavs)}:v=0:a=1[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs,
                    "-filter_complex", ";".join(filters + [concat]), "-map", "[out]",
                    str(out / "narration.wav")], check=True)

    srt = []
    for n, chunk in enumerate(chunk_words(all_words, args.max_words, args.max_chars), 1):
        text = " ".join(w["text"] for w in chunk).rstrip(".,")
        srt.append(f"{n}\n{srt_time(chunk[0]['start'])} --> {srt_time(chunk[-1]['end'])}\n{text}\n")
    (out / "narration.srt").write_text("\n".join(srt), encoding="utf-8")
    (out / "words.json").write_text(json.dumps(all_words, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료: {out}/narration.wav ({offset - args.gap:.2f}s), narration.srt")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=DEFAULT_MODEL)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("voices", help="보이스 목록")
    v.add_argument("--gender", choices=["male", "female"])
    v.set_defaults(func=cmd_voices)

    s = sub.add_parser("speak", help="대본 → 음성 + 자막")
    s.add_argument("script", help="한 줄에 한 문장인 대본 파일")
    s.add_argument("--voice", default=DEFAULT_VOICE, help="보이스 이름(예: 수빈) 또는 voice_id (tc_...)")
    s.add_argument("--out", required=True)
    s.add_argument("--emotion", default="normal",
                   choices=["normal", "happy", "sad", "angry", "whisper", "toneup", "tonedown"])
    s.add_argument("--intensity", type=float, default=1.0)
    s.add_argument("--tempo", type=float, default=1.1, help="말 속도 (쇼츠는 1.1 전후)")
    s.add_argument("--pitch", type=int, default=0)
    s.add_argument("--gap", type=float, default=0.0, help="문장 사이 무음(초)")
    s.add_argument("--max-words", type=int, default=3)
    s.add_argument("--max-chars", type=int, default=12)
    s.set_defaults(func=cmd_speak)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
