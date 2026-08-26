# REST API Contract v2.0

Base URL: `http://<host>:8000` · All bodies JSON unless noted. Errors: `{ "detail": string }` with proper HTTP codes.

## Authentication (per-device)

Every client generates a UUIDv4 once, stores it locally, and sends it as the
`X-Device-User-Id` header on **every** `/api/*` request. The server maps that
UUID to an isolated user row — workbooks, sections, attempts and the saved
Gemini key never cross device boundaries.

- Missing header → `401 { "detail": "..." }`
- Malformed (non-UUID) value → `400`
- First sight of a new UUID lazily creates its user row

Exception: `GET /api/health` also serves anonymous callers (load-balancer
checks) and then only reports the server-wide env fallback key.

## Gemini API key resolution (photo extraction)

Precedence per request:
1. `X-Gemini-API-Key` request header (any non-empty string)
2. The device's saved key (`/api/settings/api-key`)
3. Server-wide env fallback (`GEMINI_API_KEY`/`GOOGLE_API_KEY`)

No prefix/format validation is performed server-side; any non-empty value is
accepted and the actual Google AI API response is the source of truth for
validity (`401` with a clear message when Google rejects the key).

## Objects

```jsonc
Workbook      { "id": 1, "title": "쎈 미적분", "created_at": "...", "section_count": 12, "problem_count": 240, "latest_percent": 85.0 }

Section       { "id": 3, "workbook_id": 1, "label": "Day 01", "position": 0,
                "problem_count": 20, "attempt_count": 2, "latest_percent": 90.0, "best_percent": 95.0 }

ExtractionEntry   { "number": 1, "qtype": "multiple_choice", "answer_display": "(3)", "answer": "3", "line": 0 }
                  // qtype ∈ "multiple_choice" | "numeric" (only these two types are supported)
ExtractionHeader  { "type": "day", "label": "Day 01", "index": 1, "line": 0 }
ExtractionIssue   { "kind": "gap|duplicate|empty|noise", "message": "..." }

Recommendation { "structure": "headers",            // or "chunks"
                 "header_type": "day",              // when structure=headers
                 "groups": [ { "label": "Day 01", "numbers": [1,2] } ],
                 "chunk_size": null,                // when structure=chunks
                 "confidence": 0.92,
                 "rationale": "Day 헤더가 12개 감지되었습니다.",
                 "alternatives": [ { "structure": "chunks", "chunk_size": 10, "label": "10문제씩 나누기" },
                                   { "structure": "chunks", "chunk_size": 0, "label": "하나로 묶기" } ] }

ExtractionPreview { "engine": "gemini-vision",     // or "paste" when raw_text given
                    "model": "gemini-3.6-flash",   // present on the vision path only
                    "workbook_title": "...",       // vision path only ('' if not printed)
                    "raw_text": "...",
                    "entries": ExtractionEntry[],
                    "headers": ExtractionHeader[], // paste: marker scan · vision: semantic groups (Day/Chapter)
                    "issues": ExtractionIssue[],
                    "recommendation": Recommendation }

AttemptResult { "id": 7, "section_id": 3, "taken_at": "...",
                "total": 20, "score": 18, "percent": 90.0,
                "is_full_attempt": true,  // false for a merge/retry attempt — see below
                "results": [ { "number": 1, "qtype": "numeric", "expected": "3", "given": "3", "status": "correct" },   // correct|incorrect|unanswered
                             { "number": 2, "qtype": "multiple_choice", "expected": "1,4", "given": "4,1", "status": "correct" } ],
                "wrong_numbers": [5], "unanswered_numbers": [],
                "note"?: "..." }   // present when extra/unanswered inputs were excluded (see below)
```

## Endpoints

### System
- `GET /api/health` → `{ "status": "ok", "gemini_available": true, "model": "gemini-3.6-flash" }`
  (with `X-Device-User-Id` present, `gemini_available` reflects that device's key;
  anonymous callers get the env-fallback view only)

### Settings (Gemini API key, per device)
- `GET /api/settings/api-key` → `{ "set": true, "source": "user"|"server", "masked": "AIzaSy...cdef" }`
  (raw key is never returned; `masked`/`source` are `null` when unset)
- `POST /api/settings/api-key` body `{ "api_key": "<any non-empty string>" }` → same status object.
  Persists on the device's user row in SQLite; takes precedence over env vars.
  No format validation (`AQ...` etc. accepted). (400 blank after trim, 422 validation)
- `DELETE /api/settings/api-key` → removes the saved key and falls back to
  `GEMINI_API_KEY`/`GOOGLE_API_KEY` env vars (if any)

### Workbooks
- `GET /api/workbooks` → `Workbook[]` (ordered by created_at desc)
- `POST /api/workbooks` body `{ "title": "쎈 미적분" }` → `201 Workbook` (400 if title blank)
- `GET /api/workbooks/{wid}` → `{ ...Workbook fields, "sections": Section[] }` (404 if missing)
- `PATCH /api/workbooks/{wid}` body `{ "title": "..." }` → `200 Workbook` (400 blank after trim, 404 missing/not owned, 422 validation)
- `DELETE /api/workbooks/{wid}` → `204` (cascades sections/keys/attempts)

### Extraction
- `POST /api/extract` — multipart/form-data only
  - `file=<image jpg/png/webp>` **or** form field `raw_text=1. 3 2. 4 ...`
  - `file` may be repeated 1..N times in the same request (same field name,
    one part per photo) to register an answer key spread across multiple
    photos in one call — each image is sent to Gemini Vision and the results
    are merged in upload order into a single `ExtractionPreview`; a lone
    `file` part behaves exactly like the single-image case always has.
    `headers[].index`/`.line` and `entries[].line` form one continuous
    sequence spanning all uploaded images, the same way they already span
    multiple printed sections found within one image.
  - Fail-fast: an error on any one image (unreadable, no valid entries, ...)
    fails the whole request — no partial/silent-drop result.
  - Photo → Gemini Vision structured extraction (multiple-choice + numeric answers only)
  - → `200 ExtractionPreview` (400 no input, or more than `MAX_EXTRACT_IMAGES`
    (10) files in one request; 415 bad file type on any file; 503
    `GEMINI_API_KEY` missing; 502 Gemini call/parse failure or no valid
    entries found on any file; 422 nothing parsed from text)
- `POST /api/extract-text` body `{ "raw_text": "1. 3 2. 4 ..." }` → same `ExtractionPreview`
- `POST /api/workbooks/{wid}/sections/conflicts` — same body as `sections/import`
  (resolutions ignored) → `{ "conflicts": [ { "incoming_label": "Day 01",
  "incoming_numbers": [1,2], "existing_section": { "id": 5, "label": "Day 01", "numbers": [...] },
  "overlapping_numbers": [1,2], "same_label": true } ] }`.
  A conflict fires when labels are related (normalized equality or containment)
  AND ranges overlap — OR labels are exactly equal regardless of numbers.
- `POST /api/workbooks/{wid}/sections/import`
  ```jsonc
  { "structure": "headers",            // or "chunks"
    "header_type": "day",              // required iff headers
    "chunk_size": null,
    "entries": [ { "number": 1, "answer": "3", "line": 0 } ],   // line = source line (needed for header grouping)
    "headers": [ { "type": "day", "label": "Day 01", "index": 1, "line": 0 } ],  // required iff headers
    "resolutions"?: [                  // duplicate conflict handling per group
      { "incoming_label": "Day 01", "action": "overwrite", "target_section_id": 5 },
      { "incoming_label": "Day 02", "action": "keep_both" },   // server renames "Day 02 (2)"
      { "incoming_label": "Day 03", "action": "skip_incoming" } ] }
  ```
  Server re-segments from entries+headers (or chunk_size) and **rejects any answer that is not
  a valid multiple-choice label or a number** (`422` naming the offending problem number).
  Groups without a resolution are appended as new sections (legacy behavior).
  `overwrite` replaces the target section's label+keys in place (position & attempt history kept);
  `keep_both` appends the incoming group under a unique name; `skip_incoming` discards it.
  If every group is skipped → `422`. → `201 { "sections": Section[] }` (`overwritten: true` on replaced ones)

### Sections & Attempts
- `GET /api/sections/{sid}` → `{ "id": 3, "label": "Day 01", "workbook_id": 1, "workbook_title": "...", "numbers": [1,...,20] }`
  (**never includes answers** — integrity requirement)
- `DELETE /api/sections/{sid}` → `204`. Deletes that session only; its answer keys and
  attempts cascade, sibling sections/workbook stay untouched (404 if missing)
- `POST /api/attempts` body `{ "section_id": 3, "answers": { "1": "3", "2": "" },
  "merge_attempt_id"?: 7, "answered_only"?: false }` → `201 AttemptResult`
  (404 unknown section; empty answers allowed → all unanswered.
  With `merge_attempt_id`, the base attempt's given answers are overlaid by the new
  ones so previously solved questions keep their correct status — retry flow.
  Response gains `"merged_from": 7` when used; base attempt history is untouched.
  A `merge_attempt_id` retry is saved with `is_full_attempt: false` — it remains
  fully fetchable via `GET /attempts/{aid}`, but is excluded from
  `GET /sections/{sid}/attempts`, `attempt_count`, `latest_percent`, `best_percent`,
  and `top_missed`, so retrying wrong questions only never counts as a new official
  attempt or moves best/recent stats. Ordinary attempts (no `merge_attempt_id`) are
  always `is_full_attempt: true`.
  With `answered_only: true` (default `false`), blank/skipped questions are excluded
  from `total` and `percent` instead of counting against the score — `results` still
  lists them with `status: "unanswered"` and they still appear in `unanswered_numbers`,
  they just no longer shrink the percentage. `score` is unaffected either way, since
  unanswered questions were never counted as correct.)
- `GET /api/sections/{sid}/attempts` → `AttemptSummary[]` (`{ id, taken_at, score, total, percent }`, desc)
- `GET /api/attempts/{aid}` → full `AttemptResult` (review after grading)
- `DELETE /api/attempts/{aid}` → `204`

### Stats & Utility
- `GET /api/workbooks/{wid}/stats` → `{ "sections": [ { "section_id":3, "label":"Day 01", "attempt_count":2, "latest_percent":90.0, "best_percent":95.0 } ], "top_missed": [ { "number": 7, "count": 3, "section_label": "Day 02", "section_id": 4, "workbook_id": 1, "workbook_title": "..." } ] }`
  (`top_missed` is scoped to `{wid}` only — misses from the caller's other workbooks never leak in)
- `POST /api/attempts/from-misses` body `{ "attempt_id": 7 }` → `201 { "section_id": 3, "numbers": [5,9] }`
  (returns problem list for a retry-only-misses session; graded via normal POST /api/attempts restricted to those numbers)
