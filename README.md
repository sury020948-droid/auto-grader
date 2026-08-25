# Smart Auto-Grader (정답기)

사진 한 장으로 문제집 정답지("빠른 정답")를 디지털화하고, 매일의 셀프 채점을 자동화하는
고등학생 대상 로컬 웹 애플리케이션입니다.

- **Gemini Vision 추출**: Google Gemini API가 사진 속 표/단면 레이아웃을 시각적으로 이해해
  정답을 구조화된 JSON으로 추출 — 전통 OCR의 줄바꿈·열 밀림 오류 없음
- **지원 문항 유형**: 객관식(Multiple Choice)과 숫자 단답형(Numeric) **두 유형만** 지원.
  그 외 서술형·부호식 정답은 추출·저장·채점 전 단계에서 일관되게 거부됩니다
- **자동 구조 분석**: 연속 번호 청크를 자동 인식하고 최적 분할을 추천
- **자동 채점**: `③`·`(3)`·`3.0` vs `3`·`1,234` vs `1234` 등 표기 차이를 정규화하여 채점,
  오답/미응답 구분 리포트
- **학습 분석**: 섹션별 점수 이력, 자주 틀린 문항 TOP, "틀린 문제만 다시 풀기"

모든 데이터는 로컬 SQLite(`data/`)에 저장됩니다. 사진 업로드 시에만 Gemini API로
네트워크 호출이 발생하며, 이미지는 저장되지 않습니다.

---

## Quick Start

```bash
# 1) 의존성 설치 (Python 3.11+)
python3 -m pip install -r requirements.txt

# 2) 실행
./run.sh                    # http://127.0.0.1:8000
PORT=9000 ./run.sh          # 포트 변경

# 3) Gemini API 키 등록 (사진 등록에 필요 — https://aistudio.google.com/apikey)
#    앱 화면 오른쪽 위 칩(예: "Gemini API 키 미설정")을 클릭해 키를 입력·저장하세요.
#    키는 data/settings.json에만 저장되며, 환경 변수로도 설정할 수 있습니다:
export GEMINI_API_KEY="여기에_키_붙여넣기"        # 또는 GOOGLE_API_KEY
export GEMINI_MODEL="gemini-3.6-flash"           # 선택: 모델 변경 (기본 gemini-3.6-flash)

# 4) (선택) 데모 데이터 삽입 후 브라우저에서 확인
python3 -m scripts.seed_demo
```

API 키는 **앱 내 설정(오른쪽 위 칩 클릭)** 또는 환경 변수 중 한 방법으로 등록하면 되며,
앱에서 저장한 키가 환경 변수보다 우선합니다. 키가 없어도 앱은 실행되며, 사진 대신
**텍스트 붙여넣기** 탭으로 정답지를 등록할 수 있습니다.

### 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | — (또는 `GOOGLE_API_KEY`) | 서버 전체 폴백 키. 각 기기는 설정 화면에서 자신의 키를 등록하며, 등록된 기기 키가 우선합니다. 요청 헤더 `X-Gemini-API-Key`로도 일회 대체 가능 |
| `GEMINI_MODEL` | `gemini-3.6-flash` | 사용할 Gemini 비전 모델 |
| `GEMINI_TIMEOUT_SECS` | `60` | API 호출 타임아웃(초) |
| `GEMINI_MAX_RETRIES` | `2` | 일시적 오류 재시도 횟수 |
| `AUTO_GRADER_DATA_DIR` | `./data` | SQLite/업로드 저장 위치 |

## 테스트

```bash
python3 -m pytest tests/ -q        # 파서/세그먼터/채점기/정규화기/Gemini(mock) + API 라이프사이클
python3 scripts/make_sample_image.py /tmp/key.png   # 실제 Gemini Vision E2E용 샘플 정답지 이미지 생성
```

## Architecture

```
app/
├── main.py                  FastAPI 앱, 정적 SPA 서빙, 에러 핸들링
├── config.py                경로/업로드 제한 + Gemini 설정 (env: GEMINI_API_KEY 등)
├── db.py                    SQLite DAL (workbooks ▸ sections ▸ answer_keys ▸ attempts)
├── schemas.py               Pydantic 요청 검증 (ID 범위, 답안 길이/개수 제한)
├── routers/
│   ├── workbooks.py         워크북 CRUD, 섹션 조회, 통계
│   ├── extraction.py        POST /api/extract (사진→Gemini | 텍스트), 섹션 일괄 저장
│   └── attempts.py          채점, 이력, 오답 재도전
├── services/
│   ├── gemini.py            Gemini Vision 연동 (의미론적 그룹 Day/Chapter 인식, 객관식/숫자만 검증)
│   ├── parser.py            붙여넣기 텍스트 마커 스캔, 헤더 감지, 이슈 탐지
│   ├── normalizer.py        객관식 라벨·숫자 정규화/분류 (그 외 형식은 일관 거부)
│   ├── segmenter.py         구조 추천(chunks), 그룹 재구성
│   └── grader.py            correct/incorrect/unanswered 판정 + 유형 메타데이터
└── static/                  바닐라 JS SPA (해시 라우팅, 빌드 불필요)
docs/    PRODUCT_SPEC.md · API.md
tests/
scripts/ seed_demo.py · make_sample_image.py
```

### Extraction pipeline

```
photo ─▶ Gemini Vision (structured JSON, temp=0)  ─▶ validate_entries(객관식|숫자만) ─┐
                                                                                      ├─▶ issues(gap/dup/noise)
pasted text ─▶ parser(marker scan + glue 복원) ─▶ normalizer(객관식|숫자만) ───────────┘
                                                                                      ─▶ segmenter.recommend() ─▶ 사용자 확인 UI ─▶ 저장
```

## API 요약

전체 계약은 [`docs/API.md`](docs/API.md). 주요 엔드포인트:

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/health` | 상태 + Gemini 사용 가능 여부/모델 |
| POST/GET/DELETE | `/api/workbooks…` | 워크북 CRUD |
| POST | `/api/extract` | 사진 1장 이상(multipart `file`, 반복 가능)→Gemini Vision 또는 텍스트(form `raw_text`) → 추출 프리뷰 + 구조 추천 |
| POST | `/api/extract-text` | JSON `{raw_text}` → 동일 프리뷰 |
| POST | `/api/workbooks/{id}/sections/import` | 확정된 구조로 섹션+정답 저장 |
| GET | `/api/sections/{id}` | 문항 번호만 제공 (**정답 미포함** — 입력 중 부정행위 방지) |
| POST | `/api/attempts` | 채점 수행 + 기록 저장 |
| POST | `/api/attempts/from-misses` | 오답 번호 목록 반환(재도전용) |
| GET | `/api/workbooks/{id}/stats` | 섹션별 성적 + 자주 틀린 문항 |

## UX 개선 사항 (기본 요구사항에서 강화된 점)

1. **Human-in-the-loop 추출** — Gemini도 완벽하지 않음을 인정하고, 저장 전 편집 가능한 프리뷰
   테이블과 경고(누락/중복/판독 불가) 패널을 제공. "그럴듯한 자동화" 대신 "항상 올바른 결과".
2. **구조 추천형 설정** — Day/Chapter 헤더 자동 감지 시 근거와 함께 추천, 원클릭 확정.
   수동 드롭다운으로 5/10/20문제 단위·하나로 묶기 즉시 전환.
3. **키보드 퍼스트 채점 입력** — Enter 다음 문항 이동, 방향키 자유 이동, 실시간 진행 카운터.
4. **여러 개 붙여넣기** — 메모앱에 적어둔 `1. 3` 형태 텍스트를 그리드에 일괄 반영.
5. **오답 중심 결과 화면** — 점수 링 보조, 내 답 vs 정답 나란히, [틀린 문제만 다시 풀기] 원탭.
6. **채점 무결성** — 문항 입력 화면에는 정답을 전송하지 않고 서버에서만 대조.
7. **붙여넣기 폴백** — API 키 미설정·사진 품질 불량 시에도 제품이 죽지 않도록 텍스트 경로 유지.

## 알려진 제한 및 로드맵

- 지원 문항: **객관식(①~⑤/A~E/ㄱㄴㄷㄹ 등)과 숫자 단답형만**. 서술형·수식 정답은 추출 단계에서
  자동 제외되며 프리뷰 경고로 표시됩니다.
- 사진 등록 시 Google Gemini API 유료/무료 쿼터가 소진됩니다. 오프라인 사용은 텍스트 붙여넣기 경로를 이용하세요.
- 다중 사용자/계정, 클라우드 동기화는 의도적으로 제외(로컬 퍼스트).
- 다중 선택 객관식(`①③`)은 집합 일치로 판정 — 부분 점수는 v2.
- 이미지는 Gemini 전송 후 보존하지 않으며, DB에는 정답 문자열만 저장됩니다.
