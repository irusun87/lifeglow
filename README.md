# lifeglow
라이프글로우 쇼츠 제작 도구 모음.

## Typecast TTS (`tools/typecast_tts.py`)

대본을 Typecast API로 읽혀 음성(wav)과 단어 단위 타임스탬프, 쇼츠용 자막(SRT)을 만든다. 외부 패키지 없이 Python 3 + ffmpeg만 필요.

**API 키**는 코드나 채팅에 넣지 않고 환경 변수 `TYPECAST_API_KEY`로만 전달한다.

```bash
# 한국어 보이스 목록 (voice_id 확인용)
python3 tools/typecast_tts.py voices --gender female

# 대본: 한 줄 = 한 문장
python3 tools/typecast_tts.py speak script.txt --out out/ep01
```

결과물 (`--out` 폴더):
- `01.wav`, `02.wav` … 문장별 음성, `01.json` … 문장별 단어 타임스탬프
- `narration.wav` 문장들을 이어 붙인 전체 내레이션
- `narration.srt` 1~3어절 단위 자막 (영상 편집 단계에서 페이퍼로지 폰트로 렌더링)

기본값은 채널 설정 그대로 보이스 **수빈**, 감정 **일반(normal)**, 속도 **1.1배**, 문장 사이 쉼 **0초**.

옵션: `--voice`(이름 또는 voice_id), `--tempo`(기본 1.1), `--emotion`(normal/happy/toneup …), `--gap`, `--max-words`, `--max-chars`.
