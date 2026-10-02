# lifeglow
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
