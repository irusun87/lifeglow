#!/usr/bin/env python3
"""ElevenLabs API로 대본을 TTS 음성 + 단어 타임스탬프로 만든다 (typecast_tts.py 와 같은 출력).

API 키는 환경 변수 ELEVENLABS_API_KEY 에서만 읽는다 (코드/채팅에 키를 넣지 말 것).
api.elevenlabs.io 가 클라우드 환경의 허용 도메인에 있어야 한다.

사용 예:
  # 키/네트워크 확인 (계정 정보, 남은 크레딧)
  python3 tools/elevenlabs_tts.py check

  # 내 계정에서 쓸 수 있는 보이스 목록
  python3 tools/elevenlabs_tts.py voices

  # 보이스 라이브러리의 한국어 여성 보이스 (미리듣기 URL 포함)
  python3 tools/elevenlabs_tts.py library --gender female

  # 대본(한 줄 = 한 문장)을 문장별 wav + 전체 합본 + 자막(SRT)으로 생성
  python3 tools/elevenlabs_tts.py speak script.txt --voice <voice_id 또는 이름> --out out/ep01
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

API_BASE = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
# 라이프글로우 기본: 1.15배속 (2026-10-06 이호준 결정) / 문장 사이 쉼 0초
# 채널 기본 음성: Sian (2026-10-03 이호준 결정, 속도 1.1)
DEFAULT_VOICE = os.environ.get("ELEVENLABS_VOICE", "5n5gqmaQi9Ewevrz7bOS")


def api_key():
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY 환경 변수가 없습니다. 클라우드 환경 설정에서 등록하세요.")
    return key


def request(method, path, body=None, query=None):
    url = API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("xi-api-key", api_key())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs API 오류 {e.code} ({path}): {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"ElevenLabs 접속 실패 ({path}): {e.reason}  (허용 도메인에 api.elevenlabs.io 가 있는지 확인)")


def cmd_check(args):
    sub = request("GET", "/v1/user/subscription")
    used, limit = sub.get("character_count", 0), sub.get("character_limit", 0)
    print(f"플랜: {sub.get('tier')}  크레딧: {used:,} / {limit:,} 사용 (남음 {limit - used:,})")


def account_voices():
    voices, token = [], None
    while True:
        query = {"page_size": 100}
        if token:
            query["next_page_token"] = token
        res = request("GET", "/v2/voices", query=query)
        voices += res.get("voices", [])
        if not res.get("has_more"):
            return voices
        token = res.get("next_page_token")


def cmd_voices(args):
    for v in account_voices():
        labels = v.get("labels") or {}
        print(f"{v['voice_id']}\t{v.get('name')}\t{labels.get('gender', '')}\t{labels.get('age', '')}"
              f"\t{labels.get('accent', '')}\t{v.get('category', '')}")


def cmd_library(args):
    query = {"language": "ko", "page_size": args.limit, "sort": "trending"}
    if args.gender:
        query["gender"] = args.gender
    if args.search:
        query["search"] = args.search
    for v in request("GET", "/v1/shared-voices", query=query).get("voices", []):
        print(f"{v['voice_id']}\t{v.get('name')}\t{v.get('gender', '')}\t{v.get('age', '')}"
              f"\t{v.get('use_case', '')}\t{v.get('preview_url', '')}")
        if args.verbose and v.get("description"):
            print(f"\t{v['description']}")


def cmd_add(args):
    """라이브러리 보이스를 내 계정 보이스로 추가한다 (API로 쓰려면 필요)."""
    lib = request("GET", "/v1/shared-voices", query={"search": args.voice_id, "page_size": 5}).get("voices", [])
    match = next((v for v in lib if v["voice_id"] == args.voice_id), None)
    if not match:
        sys.exit(f"라이브러리에서 {args.voice_id} 를 찾지 못했습니다.")
    name = args.name or match.get("name")
    res = request("POST", f"/v1/voices/add/{match['public_owner_id']}/{args.voice_id}", {"new_name": name})
    print(f"추가됨: {res.get('voice_id')}\t{name}")


def resolve_voice(voice):
    """voice_id 는 그대로, 이름이면 내 계정 보이스에서 찾아 voice_id 로 바꾼다."""
    if not voice:
        sys.exit("--voice 가 필요합니다 (`voices` 로 확인하거나 ELEVENLABS_VOICE 환경 변수로 기본값 지정).")
    voices = account_voices()
    if any(v["voice_id"] == voice for v in voices):
        return voice
    matches = [v for v in voices if (v.get("name") or "").strip().lower() == voice.strip().lower()]
    if not matches:
        if len(voice) == 20 and voice.isalnum():
            return voice  # 계정 목록에 없어도 id 형태면 그대로 시도
        sys.exit(f"'{voice}' 보이스를 찾지 못했습니다. `voices` 명령으로 이름을 확인하세요.")
    if len(matches) > 1:
        print(f"'{voice}' 보이스가 여러 개라 첫 번째를 사용: "
              + ", ".join(v["voice_id"] for v in matches), file=sys.stderr)
    return matches[0]["voice_id"]


def synthesize(text, args, previous_text=None, next_text=None):
    body = {
        "text": text,
        "model_id": args.model,
        "voice_settings": {
            "stability": args.stability,
            "similarity_boost": args.similarity,
            "style": args.style,
            "use_speaker_boost": True,
            "speed": args.speed,
        },
    }
    # 앞뒤 문장을 주면 문장 경계 억양이 자연스러워진다 (크레딧은 text 만 차감).
    if previous_text:
        body["previous_text"] = previous_text
    if next_text:
        body["next_text"] = next_text
    if args.model.startswith(("eleven_flash_v2_5", "eleven_turbo_v2_5")):
        body["language_code"] = "ko"
    return request("POST", f"/v1/text-to-speech/{args.voice}/with-timestamps", body,
                   {"output_format": "mp3_44100_128"})


def char_alignment_to_words(alignment):
    """문자 단위 타임스탬프를 공백 기준 어절 단위로 묶는다."""
    words, cur = [], None
    for ch, start, end in zip(alignment["characters"],
                              alignment["character_start_times_seconds"],
                              alignment["character_end_times_seconds"]):
        if ch.isspace():
            if cur:
                words.append(cur)
                cur = None
            continue
        if cur is None:
            cur = {"text": ch, "start": start, "end": end}
        else:
            cur["text"] += ch
            cur["end"] = end
    if cur:
        words.append(cur)
    return words


def audio_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


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
    args.voice = resolve_voice(args.voice)
    lines = [l.strip() for l in Path(args.script).read_text(encoding="utf-8").splitlines() if l.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    all_words, wavs, offset = [], [], 0.0
    for i, text in enumerate(lines, 1):
        res = synthesize(text, args,
                         lines[i - 2] if i > 1 else None,
                         lines[i] if i < len(lines) else None)
        mp3 = out / f"{i:02}.mp3"
        wav = out / f"{i:02}.wav"
        mp3.write_bytes(base64.b64decode(res["audio_base64"]))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-ar", "44100", "-ac", "1", str(wav)],
                       check=True)
        mp3.unlink()
        wavs.append(wav)
        duration = audio_duration(wav)
        words = char_alignment_to_words(res.get("alignment") or res.get("normalized_alignment"))
        (out / f"{i:02}.json").write_text(
            json.dumps({"text": text, "duration": duration, "words": words},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        for w in words:
            all_words.append({"text": w["text"], "start": w["start"] + offset, "end": w["end"] + offset})
        offset += duration + args.gap
        print(f"[{i}/{len(lines)}] {duration:.2f}s  {text}")

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
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="키/네트워크/남은 크레딧 확인").set_defaults(func=cmd_check)
    sub.add_parser("voices", help="내 계정 보이스 목록").set_defaults(func=cmd_voices)

    lib = sub.add_parser("library", help="보이스 라이브러리의 한국어 보이스")
    lib.add_argument("--gender", choices=["male", "female", "neutral"])
    lib.add_argument("--search")
    lib.add_argument("--limit", type=int, default=30)
    lib.add_argument("--verbose", action="store_true")
    lib.set_defaults(func=cmd_library)

    add = sub.add_parser("add", help="라이브러리 보이스를 내 계정에 추가")
    add.add_argument("voice_id")
    add.add_argument("--name")
    add.set_defaults(func=cmd_add)

    s = sub.add_parser("speak", help="대본 → 음성 + 자막")
    s.add_argument("script", help="한 줄에 한 문장인 대본 파일")
    s.add_argument("--voice", default=DEFAULT_VOICE, help="voice_id 또는 내 계정 보이스 이름")
    s.add_argument("--out", required=True)
    s.add_argument("--model", default=DEFAULT_MODEL,
                   help="eleven_multilingual_v2(기본, 1자=1크레딧) / eleven_flash_v2_5(빠름, 1자=0.5크레딧)")
    s.add_argument("--speed", type=float, default=1.15, help="말 속도 0.7~1.2 (쇼츠 기본 1.15)")
    s.add_argument("--stability", type=float, default=0.5)
    s.add_argument("--similarity", type=float, default=0.75)
    s.add_argument("--style", type=float, default=0.0)
    s.add_argument("--gap", type=float, default=0.0, help="문장 사이 무음(초)")
    s.add_argument("--max-words", type=int, default=3)
    s.add_argument("--max-chars", type=int, default=12)
    s.set_defaults(func=cmd_speak)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
