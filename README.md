# lifeglow
## 쇼츠 렌더링 (`tools/make_short.py`)

에피소드 스펙(JSON, `shorts/`)대로 컷 편집 → 720x1280 레이아웃 → 상단 제목·자막·포인트 그래픽 → 내레이션 + BGM 믹스까지 한 번에 만든다.
`tools/typecast_tts.py`(PR #1)와 `tools/mix_audio.py`(PR #2)를 함께 쓴다.

```bash
# TTS 폴더(01.wav, 01.json …)에 맞춰 최종본
python3 tools/make_short.py shorts/ep01_baejongok.json --tts out/ep01/tts --out ep01.mp4
# TTS 없이 글자 수로 길이를 추정한 무음 미리보기
python3 tools/make_short.py shorts/ep01_baejongok.json --out preview.mp4
```

TTS 파일을 타입캐스트에서 직접 받은 경우 `01.wav` … 처럼 문장 순서대로 이름을 붙여 한 폴더에 두면 된다 (타임스탬프 json이 없으면 글자 수 비례로 자막 타이밍을 나눈다).

## 완성본 검사 (tools/qc_short.py)

`make_short.py` 는 결과 옆에 `<out>.ass`(자막)와 `<out>.voice.wav`(BGM 없는 말소리)를 남긴다. 납품 전에:

```
python3 tools/qc_short.py out.mp4
```

짧은 컷(0.5초 미만), 음량(-14 LUFS), 말하는데 자막이 비는 구간(말소리 재받아쓰기 대조), 자막·발음 불일치,
의미 단위 끊김("발랐을 | 때"), 제목·자막·라벨 화면 넘침을 검사하고, 컷마다 한 장씩 모은 `<out>.qc.jpg` 를 만든다.
오류가 있으면 종료 코드 1.

## 에셋

- **폰트**: `assets/fonts/` 페이퍼로지 (제목 ExtraBold, 자막 Bold)
- **BGM**: New Day (Patrick Patrikios). 공개 저장소라 음원 파일은 올리지 않고 프로젝트 공유 폴더
  `/mnt/project-files/assets/bgm/new_day.mp3` 에 둔다. 다른 경로는 `--bgm` 또는 `LIFEGLOW_BGM`.

## 오디오 믹스 (`tools/mix_audio.py`)

말소리 트랙(원본 클립 음성 + TTS 합본) 아래에 BGM을 깔고 업로드용 음량으로 맞춘다.

```bash
python3 tools/mix_audio.py voice.wav --out mix.wav
```

기본값:
- BGM 볼륨 **-15 dB** (원본 대비, 채널에서 쓰던 값). 원본이 약 -10 LUFS라 BGM만 약 -25 LUFS
- 말소리가 나오는 구간에서는 BGM을 **4 dB 더** 낮춤 (`--duck`, 0이면 끔). 무음 감지로 구간을 찾음
- 처음 0.3초 페이드인, 끝 1.5초 페이드아웃 (`--fade-in`, `--fade-out`)
- 최종 믹스 **-14 LUFS / True Peak -1.5 dB** (유튜브 쇼츠 기준, 2-pass loudnorm)

BGM이 크게 느껴지면 `--bgm-gain -17`, 말 사이가 너무 비어 보이면 `--duck 2` 정도로 조절.
