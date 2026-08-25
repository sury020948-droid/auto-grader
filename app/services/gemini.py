import json
import re
import time
from typing import Any

from .. import config
from ..errors import GeminiResponseError, GeminiUnavailableError
from .normalizer import answer_matches_type, normalize_answer

ALLOWED_TYPES = ("multiple_choice", "numeric")

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "workbook_title": {"type": "STRING"},
        "groups": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "main_category": {"type": "STRING"},
                    "sub_category": {"type": "STRING", "nullable": True},
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "number": {"type": "INTEGER"},
                                "type": {
                                    "type": "STRING",
                                    "enum": ["multiple_choice", "numeric"],
                                },
                                "answer": {"type": "STRING"},
                            },
                            "required": ["number", "type", "answer"],
                        },
                    },
                },
                "required": ["main_category", "items"],
            },
        },
        "notes": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["workbook_title", "groups"],
}

SYSTEM_PROMPT = """You are a precise answer-key reader for printed school workbooks. \
You receive one photograph of a workbook answer-key page (tables, multi-column grids, \
dense answer lists). You read it VISUALLY — you are not running OCR character scanning.

Return ONLY strict JSON matching the given schema:
{"workbook_title": "<string>", "groups": [{"main_category": "<string>",
"sub_category": "<string|null>", "items": [{"number": <int>, "type":
"<multiple_choice|numeric>", "answer": "<string>"}]}], "notes": [<string>]}

Dynamic segmentation rules (crucial):
1. NO arbitrary chunking. Never group questions by fixed counts (every 5 or 10). \
Group ONLY by the printed organizational boundaries visible on the page.
2. Semantic grouping markers: "Day 01 / Day 02", chapter titles like "01 힘과 운동", \
"Unit", "Lesson", "Test/테스트" headers. Each marker starts a new group in "groups".
3. Hierarchical text layouts: use the chapter title as "main_category" and the \
sub-test label (e.g. "수능 2점 테스트") as "sub_category". If there is no sub-test \
level, set "sub_category" to null.
4. Spanning grids: if a single table cell vertically spans multiple rows, apply that \
spanning label as "main_category" to every adjacent question-answer pair it covers, \
with "sub_category" null.
5. Scan columns and logical reading order first; never read left-to-right across \
unrelated sections.

Answer formatting (backend supports ONLY two types):
- "multiple_choice": choice labels only — circled digits ①②③ MUST be converted to \
plain integers ("1","2","3"), Latin letters A-J, or Korean jamo ㄱㄴㄷ. Multiple \
selections joined with "," (e.g. "1,3"). Never include words or explanations.
- "numeric": raw numbers only — optional leading minus sign, decimal point, thousands \
commas (e.g. "-4.5", "151", "1,234"). No units, no fractions like 1/2, no ranges, no text.
If an item's answer is anything else (a word, expression, sentence), SKIP that item \
and add one short reason to "notes".

Hard rules:
A. NEVER invent or guess answers. If unreadable, skip the item and note it.
B. "number" is the PRINTED question number: an integer from 1 to 999. The printed \
number always wins over visual position.
C. Do not merge, split, reorder, or summarize items. Output every readable item, \
placed in its correct printed group."""

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_MAX_NUMBER = 999


def available(api_key: str | None = None) -> bool:
    return bool(api_key if api_key is not None else config.GEMINI_API_KEY)


def _client(api_key: str):
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise GeminiUnavailableError(
            "google-genai 패키지가 설치되지 않았습니다. pip install google-genai"
        ) from exc
    if not api_key:
        raise GeminiUnavailableError()
    return genai.Client(
        api_key=api_key,
        http_options={"timeout": int(config.GEMINI_TIMEOUT_SECS * 1000)},
    )


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip())


def parse_model_json(text: str) -> dict[str, Any]:
    cleaned = _strip_fences(text or "")
    if not cleaned:
        raise GeminiResponseError("Gemini 응답이 비어 있습니다.")
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiResponseError("Gemini 응답을 JSON으로 해석할 수 없습니다.") from exc
    if not isinstance(payload, dict):
        raise GeminiResponseError("Gemini 응답이 객체가 아닙니다.")
    if not isinstance(payload.get("groups"), list):
        # Legacy/flat fallback: {"entries": [...]} -> one unnamed group.
        if isinstance(payload.get("entries"), list):
            payload = {
                "workbook_title": "",
                "groups": [
                    {"main_category": "전체", "sub_category": None, "items": payload["entries"]}
                ],
                **{k: v for k, v in payload.items() if k != "entries"},
            }
        else:
            raise GeminiResponseError("Gemini 응답에 'groups' 배열이 없습니다.")
    return payload


def _category_type(label: str) -> str:
    low = label.lower()
    if "day" in low or "일차" in label:
        return "day"
    if "unit" in low or "단원" in low:
        return "unit"
    if "lesson" in low:
        return "lesson"
    if "chapter" in low or re.search(r"\d+\s*장", label) or re.match(r"\d+\s+\S", label):
        return "chapter"
    if any(k in low or k in label for k in ("test", "테스트", "시험", "모의")):
        return "step"
    if "step" in low or "단계" in label:
        return "step"
    return "chapter"


def group_label(main_category: str, sub_category: str | None) -> str:
    main = main_category.strip() or "전체"
    sub = (sub_category or "").strip()
    return f"{main} - {sub}" if sub else main


def _validated_item(raw: Any, notes: list[str]) -> dict[str, Any] | None:
    """Filter one model item down to a valid multiple-choice / numeric entry."""
    if not isinstance(raw, dict):
        return None
    num = raw.get("number")
    qtype = raw.get("type")
    answer = raw.get("answer")
    if not isinstance(num, int) or isinstance(num, bool) or not (1 <= num <= _MAX_NUMBER):
        notes.append(f"건너뜀: 잘못된 문항 번호 {num!r}")
        return None
    if qtype not in ALLOWED_TYPES:
        notes.append(f"{num}번 건너뜀: 지원하지 않는 유형({qtype!r})")
        return None
    canonical = normalize_answer(str(answer)) if isinstance(answer, str) else ""
    if not canonical or not answer_matches_type(canonical, qtype):
        notes.append(
            f"{num}번 건너뜀: {'객관식' if qtype == 'multiple_choice' else '숫자'}"
            f" 형식 불일치 ({str(answer)[:20]!r})"
        )
        return None
    return {
        "number": num,
        "qtype": qtype,
        "answer": canonical,
        "answer_display": str(answer).strip(),
    }


def validate_groups(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Filter grouped Gemini output down to valid MC/numeric entries per category."""
    notes: list[str] = [str(n) for n in payload.get("notes") or [] if str(n).strip()]
    groups: list[dict[str, Any]] = []

    for raw_group in payload["groups"]:
        if not isinstance(raw_group, dict):
            continue
        items = raw_group.get("items")
        if not isinstance(items, list):
            continue
        entries: dict[int, dict[str, Any]] = {}
        for raw in items:
            entry = _validated_item(raw, notes)
            if entry is not None:
                entries[entry["number"]] = entry  # duplicates: last wins
        ordered = [entries[n] for n in sorted(entries)]
        if not ordered:
            continue
        groups.append(
            {
                "main_category": str(raw_group.get("main_category") or "").strip() or "전체",
                "sub_category": (
                    str(raw_group["sub_category"]).strip()
                    if isinstance(raw_group.get("sub_category"), str)
                    and raw_group["sub_category"].strip()
                    else None
                ),
                "entries": ordered,
            }
        )
    return groups, notes


def validate_entries(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Backward-compatible flat-list validation (single implicit group)."""
    group = {
        "main_category": "전체",
        "sub_category": None,
        "items": payload.get("entries"),
    }
    groups, notes = validate_groups({"groups": [group], "notes": payload.get("notes")})
    return (groups[0]["entries"], notes) if groups else ([], notes)


def extract_answer_key(
    image_bytes: bytes,
    content_type: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Send the photo to Gemini Vision and return validated semantic groups.

    `api_key` (the requester's own key) takes precedence over the server-wide
    env fallback so usage is isolated per user.
    """
    key = api_key if api_key is not None else config.GEMINI_API_KEY
    if not key:
        raise GeminiUnavailableError()
    if len(image_bytes) == 0:
        raise GeminiResponseError("빈 파일입니다.")
    if len(image_bytes) > config.MAX_UPLOAD_BYTES:
        raise GeminiResponseError("파일이 너무 큽니다 (최대 15MB).")
    mime = content_type if content_type in config.ALLOWED_IMAGE_TYPES else "image/jpeg"

    from google.genai import types

    client = _client(key)
    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime),
        "Read this workbook answer key photo, segment it by its printed "
        "categories, and return the JSON.",
    ]
    gen_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=config.GEMINI_TEMPERATURE,
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
        max_output_tokens=8192,
    )

    last_exc: Exception | None = None
    for attempt in range(config.GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=gen_config,
            )
            break
        except GeminiResponseError:
            raise
        except Exception as exc:  # transient network/API errors -> retry
            last_exc = exc
            if attempt < config.GEMINI_MAX_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    else:  # pragma: no cover - only reached when all retries failed
        raise GeminiResponseError(
            f"Gemini API 호출에 실패했습니다: {last_exc}"
        ) from last_exc

    if response.text is None:
        raise GeminiResponseError("Gemini가 유효한 응답을 반환하지 않았습니다.")

    payload = parse_model_json(response.text)
    groups, notes = validate_groups(payload)
    total = sum(len(g["entries"]) for g in groups)
    if not total:
        detail = "; ".join(notes[:3]) if notes else "판독된 정답이 없습니다."
        raise GeminiResponseError(
            f"사진에서 객관식/숫자 정답을 찾지 못했습니다. {detail}"
        )

    # Flatten into pipeline shape: sequential lines + one synthesized header
    # per semantic group so the existing segmenter/import flow groups by them.
    entries: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []
    line = 0
    for idx, g in enumerate(groups):
        headers.append(
            {
                "type": _category_type(g["main_category"]),
                "label": group_label(g["main_category"], g["sub_category"]),
                "index": idx,
                "line": line,
            }
        )
        for e in g["entries"]:
            entries.append({**e, "line": line})
            line += 1

    title = str(payload.get("workbook_title") or "").strip()
    return {
        "workbook_title": title,
        "groups": [
            {**g, "label": h["label"]}
            for g, h in zip(groups, headers, strict=True)
        ],
        "entries": entries,
        "headers": headers,
        "notes": notes,
        "model": config.GEMINI_MODEL,
        "raw_text": "\n".join(f'{e["number"]}. {e["answer_display"]}' for e in entries),
    }


def extract_answer_key_batch(
    images: list[tuple[bytes, str]],
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run `extract_answer_key` once per image, in order, and merge the
    results into one continuous answer key.

    Fail-fast: an error on any image (bad response, size guard, network
    failure, ...) raises immediately and aborts the rest of the batch, same
    as a single-image call today. For a single image this is an identity
    transform — see `_merge_results`.
    """
    results = [
        extract_answer_key(image_bytes, content_type, api_key=api_key)
        for image_bytes, content_type in images
    ]
    return _merge_results(results)


def _merge_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Concatenate per-image `extract_answer_key` outputs into one answer key.

    Rebases each image's entry `line` and header `index` by running totals
    from the images already merged, so line/index form one continuous
    sequence across images — the same rebasing `extract_answer_key` already
    applies across groups *within* one image, applied one level up. With a
    single result, every offset is 0, so this is a pure identity transform
    and `_merge_results([r])` reproduces `r` exactly.
    """
    entries: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    notes: list[str] = []
    title = ""
    line_offset = 0
    index_offset = 0
    for result in results:
        for e in result["entries"]:
            entries.append({**e, "line": e["line"] + line_offset})
        for h in result["headers"]:
            headers.append(
                {**h, "index": h["index"] + index_offset, "line": h["line"] + line_offset}
            )
        groups.extend(result["groups"])
        notes.extend(result["notes"])
        if not title:
            title = result["workbook_title"]
        line_offset += len(result["entries"])
        index_offset += len(result["headers"])

    return {
        "workbook_title": title,
        "groups": groups,
        "entries": entries,
        "headers": headers,
        "notes": notes,
        "model": results[0]["model"] if results else None,
        "raw_text": "\n".join(f'{e["number"]}. {e["answer_display"]}' for e in entries),
    }
