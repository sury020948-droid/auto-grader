# Smart Auto-Grader — Product Specification v1.0

> Working title: **정답기 (AnswerKey)** — "사진 한 장, 채점은 끝."

---

## 1. Problem Statement

Korean high-school students grind through printed workbooks (문제집) daily. Every self-study
session ends the same way: flip to the back, find "빠른 정답", check 20–50 answers by eye,
tally a score on paper, and throw the information away. The friction is real:

- **Slow**: manually checking 40 answers takes 3–5 minutes per session.
- **Error-prone**: eyes skip rows in dense two-column keys; wrong rows get marked.
- **Zero memory**: no record of *which* problems were missed → students re-solve blind.

Existing tools don't fit. ZipGrade / Gradescope / Quick Key are **teacher-facing** (scan stacks of
bubble sheets). Quizlet / 밀크티 are **flashcard/curation** tools with no physical-workbook loop.
Nobody serves the student↔workbook self-grading loop. That is this product's wedge.

## 2. Personas & Jobs-To-Be-Done

| Persona | JTBD |
|---|---|
| **수능 준비생 "민준"** (18, 고3) | Solves 2 workbooks/day. Wants: photo the answer key once → grade every future session in <30s → know exactly which 문제 keep failing before 재도전. |
| **내신 관리형 "서연"** (17, 고2) | School-paced workbook (Day 1~30 구성). Wants progress per Day + weakest-problem list for 시험 직전 리마인드. |
| **과외 선생님 (보조 페르소나)** | Wants to assign workbook sections and see a student's score history without grading by hand. (v1: read-only stats screen.) |

## 3. Competitive Analysis (2026 landscape)

| Product | Model | Gap we exploit |
|---|---|---|
| ZipGrade | Teacher scans bubble sheets w/ camera | Requires special answer sheets; no workbook key ingestion; teacher-centric reports |
| Gradescope | Higher-ed rubric grading | Heavyweight, instructor workflow, no Korean workbook structure |
| Quizlet / 밀크티 | Flashcards & curated content | No OCR of *your own* book; no auto-scoring of solved sets |
| 수만휘·랜스 형 앱 | LMS/video | Grading still manual |

**Differentiators:** (1) one-time key digitization from a single photo, (2) automatic structural
segmentation matching how Korean workbooks organize keys (Day/Chapter/Unit), (3) keyboard-first
answer entry that beats paper checking speed, (4) longitudinal mistake analytics.

## 4. Scope — MoSCoW

### Must have (v1 — this release)
- M1 Photo/paste → OCR → structured answer extraction (Tesseract kor+eng).
- M2 Automatic segmentation recommendation: Day/Chapter/Unit/Lesson headers or sequential chunking; user confirms/edits before save.
- M3 Editable extraction review table (fix/delete/add any row) — **OCR is never trusted blindly**.
- M4 Workbook database: titles → sections → answer keys (SQLite, fully local).
- M5 Grading session UI: grid input, Enter-to-advance keyboard flow, bulk paste, live progress.
- M6 Instant scoring: correct/incorrect/unanswered, side-by-side review, animated score ring.
- M7 History & analytics: attempts per section, score trend, top-missed problems per workbook.
- M8 "Retry only my misses" one-tap re-test.
- M9 Answer integrity: correct answers are NEVER sent to the client during entry — grading happens server-side.

### Should have (v1 if time allows)
- S1 Demo data seeding (`scripts/seed_demo.py`).
- S2 Multi-answer questions (e.g., `①③`) graded as unordered sets; alternate accepted forms (O/X, ㄱ/ㄴ).

### Won't have (v1, documented non-goals)
- Handwriting recognition of *student* solutions; camera-live scanning; cloud accounts/multi-user auth;
  iOS/Android native apps (responsive web covers phone browsers); solution explanations (해설).

## 5. Key UX Decisions (engineered improvements over base requirements)

1. **Human-in-the-loop extraction.** Base requirement said "accurately extracts all answers."
   Reality: OCR on photographed print is ~95–99% accurate, not 100%. Instead of pretending,
   the flow lands on an **editable preview table** with confidence warnings ("누락 의심 구간",
   duplicate numbers) so saving takes seconds but is always correct. Trust is a feature.
2. **Structure recommendation, not configuration.** New users shouldn't model data. The app
   proposes "이 워크북은 Day 단위로 구성된 것 같아요" with a one-tap confirm; power users can
   switch to Chapter/Unit/custom chunks via dropdown. Recommendation + rationale shown.
3. **Keyboard-first grading flow.** Checking answers on paper is thumb-travel heavy. Our grid:
   type → Enter → next field auto-focuses; arrow keys move freely; progress bar counts live;
   unanswered fields are visually distinct at submit. Target: 40 answers in under 25 seconds.
4. **Bulk paste escape hatch.** Students often already typed answers into a memo app. A
   "여러 개 붙여넣기" modal parses free-form lines (`1. 3` style) into the grid instantly.
5. **Answers stay server-side during entry.** Prevents accidental self-spoofing and mirrors
   real exam integrity; also enables honest history/analytics later.
6. **Mistake-first results screen.** Wrong answers render first-class: expected vs. given,
   color-coded, with one-tap "틀린 문제만 다시 풀기". Score is secondary; learning is primary.
7. **Paste-text fallback.** If `GEMINI_API_KEY` isn't set (or photo quality fails), the same
   pipeline accepts raw text pasted from any notes app — the product degrades gracefully
   instead of dead-ending. (Also our testability strategy.)

## 6. Core User Flows

```
[F1 Key Digitization]
Library → "+ 새 워크북" → title → Extract screen
  → upload photo OR paste text → POST /api/extract
  → Review table (editable rows + issues panel + recommendation banner)
  → choose structure (recommendation preselected) → 저장
  → Workbook detail (sections appear)

[F2 Grading Session]
Workbook detail → section card "채점 시작" → GET section problem list (numbers only)
  → grid entry → 제출 → POST /api/attempts
  → Results: score ring, wrong-answer table, [틀린 문제만 다시] / [목록으로]

[F3 Review & Analytics]
Workbook detail → section stats (latest/best/attempts), sparkline of recent scores,
top-missed problems list across attempts
```

## 7. Screen Inventory & Layout Notes

1. **Library (`#/`)** — header w/ app name + OCR status chip; workbook cards (title, sections,
   last activity); create dialog. Empty state explains 3-step value prop.
2. **Extract (`#/new`, `#/wb/:id/extract-more`)** — two tabs (사진 업로드 / 텍스트 붙여넣기),
   drop-zone with preview thumbnail; after extraction: issues banner, editable table
   (#, 답, 삭제), structure selector showing recommended option first with rationale text.
3. **Workbook detail (`#/wb/:id`)** — section cards grouped by label (Day 01 …), each showing
   문항수, 최근 점수, attempts; global stats strip; rename/delete in overflow menu.
4. **Quiz (`#/sec/:id/solve`)** — responsive grid of numbered inputs; sticky footer with
   progress "12/20 · 미응답 8" + 제출 button; bulk-paste button.
5. **Results (`#/attempt/:id`)** — big score ring (SVG animation), summary chips
   (정답/오답/미응답), wrong-answer cards (번호, 내 답 vs 정답), retry-misses CTA.

Design system: Indigo `#4F46E5` accent, neutral slate surfaces, 12px radii, system font stack
(`Pretendard, -apple-system, ...` fallback chain, no webfont dependency), green/red state colors,
soft shadows, mobile-first breakpoints at 640/900px.

## 8. Edge Cases Catalogue (QA contract)

- Two-column key rows (`1.③  11.①`) and column-major ordering.
- Headers: `Day 03`, `DAY 3`, `3일차`, `Chapter 2`, `제2장`, `Unit 5`, `Lesson`, `Step`.
- Circled numerals `①–⑳`, full-width digits, `(3)`/`３` variants → canonical digits.
- Multi-select answers `①③`; comma/slash alternatives; O/X; ㄱㄴㄷ.
- Missing numbers (OCR dropped a line) → gap warning, still importable.
- Duplicate question numbers → scoped per section (Day/Chapter); warned + last-wins
  only WITHIN one section. Numbers restarting at 1 in each section are normal and never collide.
- Empty/garbage OCR output → clear error + paste-mode suggestion.
- Unanswered inputs at submit → counted incorrect, flagged separately in results.
- Opt-in "응답한 문항만 채점" mode (per-submission checkbox) → skipped questions are excluded
  from the total/percent instead of counting against the score; still flagged as unanswered
  in the results list.
- Section with N problems graded with extra user inputs → extras ignored with note.

## 9. Success Metrics (simulated QA targets)

- Extraction→save flow completes in ≤60s for a 100-question key (paste mode: ≤30s).
- Parser recall ≥95% on synthetic clean keys; zero silent corruptions (all anomalies surface as visible warnings).
- Grading round-trip <150ms server-side for 200 questions.
- Full pytest suite green incl. API lifecycle + parser edge cases above.

## 10. Architecture Summary

Local-first monolith: FastAPI + SQLite (file DB), vanilla-JS SPA served statically.
Photo extraction via the Google Gemini API (vision, structured JSON output) behind a pluggable
service interface with graceful unavailability when no API key is configured.
Only photo registration makes a network call; all student data stays in `./data/`.

```
Browser SPA ──HTTP──▶ FastAPI ─▶ services: ocr ▶ parser ▶ segmenter        (extraction)
                              │             grader                          (scoring)
                              └─▶ SQLite: workbooks ▸ sections ▸ keys ▸ attempts
```
