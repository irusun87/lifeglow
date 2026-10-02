#!/usr/bin/env python3
"""말소리(원본 클립 음성 + TTS) 아래에 BGM을 깔고 쇼츠 업로드용 음량으로 맞춘다.

라이프글로우 기본값 (채널에서 써 온 설정 기준):
  - BGM: New Day (Patrick Patrikios)
  - BGM 볼륨: 원본 대비 -15 dB (원본이 약 -10 LUFS라 BGM 단독 약 -25 LUFS)
  - 말소리가 나올 때만 BGM을 추가로 약 4 dB 더 낮춤 (사이드체인 덕킹)
  - 처음 0.3초 페이드인, 끝 1.5초 페이드아웃
  - 최종 믹스: -14 LUFS / True Peak -1.5 dB (유튜브 쇼츠 기준)

사용 예:
  python3 tools/mix_audio.py voice.wav --out mix.wav
  python3 tools/mix_audio.py voice.wav --out mix.wav --bgm-gain -17 --duck 6

BGM 파일은 공개 저장소에 올리지 않고 프로젝트 공유 폴더에서 읽는다.
경로는 --bgm 또는 환경 변수 LIFEGLOW_BGM 으로 바꿀 수 있다.
"""

import argparse
import json
import os
import subprocess
import sys

DEFAULT_BGM = os.environ.get("LIFEGLOW_BGM", "/mnt/project-files/assets/bgm/new_day.mp3")
DEFAULT_BGM_GAIN = -15.0   # dB, 원본 BGM 대비
DEFAULT_DUCK = 4.0         # dB, 말소리 구간에서 추가로 낮출 양
TARGET_LUFS = -14.0
TARGET_TP = -1.5


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", path], capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def loudnorm(src, dst):
    base = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11"
    first = subprocess.run(["ffmpeg", "-hide_banner", "-i", src, "-af", base + ":print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True, check=True)
    stats = json.loads(first.stderr[first.stderr.rindex("{"):])
    second = (f"{base}:linear=true:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
              f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
              f"offset={stats['target_offset']}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-af", second, "-ar", "48000", dst],
                   check=True)
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("voice", help="말소리 트랙 (원본 클립 음성 + TTS 합본, wav/mp3/mp4)")
    p.add_argument("--out", required=True, help="결과 wav")
    p.add_argument("--bgm", default=DEFAULT_BGM)
    p.add_argument("--bgm-gain", type=float, default=DEFAULT_BGM_GAIN, help="BGM 볼륨 dB (기본 -15)")
    p.add_argument("--duck", type=float, default=DEFAULT_DUCK,
                   help="말소리 구간 추가 감쇠 dB (기본 4, 0이면 덕킹 없음)")
    p.add_argument("--bgm-start", type=float, default=0.0, help="BGM을 몇 초 지점부터 쓸지")
    p.add_argument("--fade-in", type=float, default=0.3)
    p.add_argument("--fade-out", type=float, default=1.5)
    args = p.parse_args()

    if not os.path.exists(args.bgm):
        sys.exit(f"BGM 파일이 없습니다: {args.bgm}")
    dur = duration(args.voice)
    tmp = args.out + ".premix.wav"

    # 말소리 구간(무음 감지)에서만 BGM을 duck dB 더 낮춘다. 컴프레서보다 결과가 예측 가능하다.
    filt = duck_by_envelope(args, dur) if args.duck > 0 else mix_filter_plain(args, dur)

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", args.voice, "-stream_loop", "-1", "-i", args.bgm,
                    "-filter_complex", filt, "-map", "[mix]", "-t", f"{dur}", "-ar", "48000", tmp],
                   check=True)
    stats = loudnorm(tmp, args.out)
    os.remove(tmp)
    print(f"완료: {args.out} ({dur:.2f}s, 믹스 전 {stats['input_i']} LUFS → {TARGET_LUFS} LUFS)")


def mix_filter_plain(args, dur):
    fade_out_start = max(dur - args.fade_out, 0)
    return (
        f"[1:a]aformat=channel_layouts=stereo,atrim=start={args.bgm_start},asetpts=PTS-STARTPTS,"
        f"volume={args.bgm_gain}dB,"
        f"afade=t=in:d={args.fade_in},afade=t=out:st={fade_out_start}:d={args.fade_out}[bgm];"
        f"[0:a]aformat=channel_layouts=stereo[voice];"
        f"[voice][bgm]amix=inputs=2:duration=first:normalize=0[mix]"
    )


def speech_segments(path, noise_db=-40, min_silence=0.35):
    """말소리 트랙에서 소리가 나는 구간 [(start, end)]을 찾는다."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af",
                          f"silencedetect=n={noise_db}dB:d={min_silence}", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    dur = duration(path)
    silences, start = [], None
    for line in out.splitlines():
        if "silence_start:" in line:
            start = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and start is not None:
            silences.append((max(start, 0), float(line.split("silence_end:")[1].split()[0])))
            start = None
    if start is not None:
        silences.append((start, dur))
    segs, t = [], 0.0
    for s, e in silences:
        if s > t:
            segs.append((t, s))
        t = e
    if t < dur:
        segs.append((t, dur))
    return segs


def duck_by_envelope(args, dur, ramp=0.15):
    """말소리 구간에서 BGM을 duck dB 만큼 부드럽게(ramp 초) 낮추는 볼륨 자동화."""
    segs = speech_segments(args.voice)
    g = 10 ** (-args.duck / 20)
    # 각 구간마다 0~1 사이 '덕킹 정도'를 만들고, 겹치면 최댓값을 쓴다.
    terms = []
    for s, e in segs:
        a, b = max(s - ramp, 0), e + ramp
        terms.append(f"clip(min((t-{a:.3f})/{ramp},({b:.3f}-t)/{ramp}),0,1)")
    amount = "0"
    for term in terms:
        amount = f"max({amount},{term})"
    vol = f"1-(1-{g:.5f})*{amount}"
    fade_out_start = max(dur - args.fade_out, 0)
    return (
        f"[1:a]aformat=channel_layouts=stereo,atrim=start={args.bgm_start},asetpts=PTS-STARTPTS,"
        f"volume={args.bgm_gain}dB,volume='{vol}':eval=frame,"
        f"afade=t=in:d={args.fade_in},afade=t=out:st={fade_out_start}:d={args.fade_out}[bgm];"
        f"[0:a]aformat=channel_layouts=stereo[voice];"
        f"[voice][bgm]amix=inputs=2:duration=first:normalize=0[mix]"
    )


if __name__ == "__main__":
    main()
