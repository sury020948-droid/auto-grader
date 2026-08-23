from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Path, UploadFile

from .. import db as dal
from ..config import ALLOWED_IMAGE_TYPES
from ..db import get_conn
from ..errors import AppError
from ..schemas import ExtractTextPayload, SectionImport
from ..services import gemini, parser, segmenter
from ..services.conflicts import detect_conflicts
from ..services.normalizer import normalize_answer

router = APIRouter(tags=["extraction"])


def _build_preview(
    entries: list[dict[str, Any]],
    headers: list[dict[str, Any]],
    issues: list[dict[str, str]],
    raw_text: str,
    engine: str,
    model: str | None = None,
):
    if not entries:
        raise AppError(
            422,
            "객관식/숫자 정답을 찾지 못했습니다. 사진이 선명한지 확인하거나"
            " '텍스트 붙여넣기'를 이용해 주세요.",
        )
    parsed = {"entries": entries, "headers": headers}
    rec = segmenter.recommend(parsed)
    out: dict[str, Any] = {
        "engine": engine,
        "raw_text": raw_text,
        "entries": entries,
        "headers": headers,
        "issues": parser.detect_issues(entries, headers) + issues,
        "recommendation": rec,
    }
    if model:
        out["model"] = model
    return out


def _gemini_preview(data: bytes, content_type: str):
    result = gemini.extract_answer_key(data, content_type)
    entries = [
        {
            "number": e["number"],
            "qtype": e["qtype"],
            "answer_display": e["answer_display"],
            "answer": e["answer"],
            "line": i,
        }
        for i, e in enumerate(result["entries"])
    ]
    issues = [
        {"kind": "noise", "message": f"Gemini: {note}"} for note in result["notes"]
    ]
    preview = _build_preview(
        entries,
        result["headers"],
        issues,
        result["raw_text"],
        engine="gemini-vision",
        model=result["model"],
    )
    preview["workbook_title"] = result["workbook_title"]
    return preview


@router.post("/extract")
async def extract(
    file: UploadFile | None = File(default=None),
    raw_text: str | None = Form(default=None),
):
    if file is not None and file.filename:
        if file.content_type not in ALLOWED_IMAGE_TYPES:
            raise AppError(415, "지원하지 않는 파일 형식입니다. JPG/PNG를 업로드해 주세요.")
        data = await file.read()
        return _gemini_preview(data, file.content_type or "")
    if raw_text is not None and raw_text.strip():
        return _paste_preview(raw_text)
    raise AppError(400, "사진 또는 텍스트 중 하나는 필요합니다.")


def _paste_preview(raw_text: str):
    parsed = parser.parse_answer_key(raw_text)
    return _build_preview(
        parsed["entries"],
        parsed["headers"],
        [i for i in parsed["issues"] if i["kind"] == "noise"],
        parsed["raw_text"],
        engine="paste",
    )


@router.post("/extract-text")
def extract_text_json(payload: ExtractTextPayload):
    return _paste_preview(payload.raw_text)


def _group_incoming(
    payload: SectionImport,
) -> list[dict[str, Any]]:
    """Segment the payload into incoming groups (label + numbers) for
    conflict checks — mirrors segmenter.build_groups but without DB access."""
    entries = [e.model_dump() for e in payload.entries]
    headers = [h.model_dump() for h in payload.headers]
    groups = segmenter.build_groups(
        entries,
        payload.structure,
        headers if (payload.structure == "headers" and headers) else None,
        payload.chunk_size,
    )
    return [
        {
            "label": str(g["label"]),
            "numbers": sorted(int(i["number"]) for i in g["items"]),
        }
        for g in groups
        if g["items"]
    ]


@router.post("/workbooks/{wid}/sections/conflicts")
def check_conflicts(
    wid: Annotated[int, Path(ge=1, le=2**63 - 1)],
    payload: SectionImport,
    conn=Depends(get_conn),
):
    """Detect existing sessions that collide with the incoming answer key."""
    wb = conn.execute("SELECT id FROM workbooks WHERE id = ?", (wid,)).fetchone()
    if not wb:
        raise AppError(404, "워크북을 찾을 수 없습니다.")
    conflicts = detect_conflicts(
        dal.list_section_numbers(conn, wid), _group_incoming(payload)
    )
    return {"conflicts": conflicts}


def _resolve_label_conflicts(
    conn, wid: int, label: str, res_map: dict[str, Any]
) -> tuple[str | None, int | None]:
    """Return (final_label, overwrite_target_id) for one incoming group."""
    res = res_map.get(label)
    if res is None:
        return label, None
    if res.action == "skip_incoming":
        return None, None
    if res.action == "overwrite":
        target_id = res.target_section_id
        if target_id is not None:
            target = conn.execute(
                "SELECT id FROM sections WHERE id = ? AND workbook_id = ?",
                (target_id, wid),
            ).fetchone()
            if not target:
                raise AppError(404, f"'{label}'과(와) 충돌한 기존 섹션을 찾을 수 없습니다.")
            return label, int(target_id)
        return label, None  # fall through to plain append when target missing
    # keep_both: rename the incoming version so both survive
    return dal.next_unique_label(conn, wid, label), None


@router.post("/workbooks/{wid}/sections/import", status_code=201)
def import_sections(
    wid: Annotated[int, Path(ge=1, le=2**63 - 1)], payload: SectionImport, conn=Depends(get_conn)):
    wb = conn.execute("SELECT id FROM workbooks WHERE id = ?", (wid,)).fetchone()
    if not wb:
        raise AppError(404, "워크북을 찾을 수 없습니다.")

    entries = [e.model_dump() for e in payload.entries]
    headers = [h.model_dump() for h in payload.headers]

    if payload.structure == "headers" and not headers:
        raise AppError(400, "헤더 정보가 필요합니다.")

    res_map: dict[str, Any] = {r.incoming_label: r for r in payload.resolutions}

    groups = segmenter.build_groups(
        entries,
        payload.structure,
        headers if headers else None,
        payload.chunk_size,
    )
    groups = [g for g in groups if g["items"]]
    if not groups:
        raise AppError(422, "저장할 정답 데이터가 없습니다.")

    max_pos_row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS p FROM sections WHERE workbook_id = ?",
        (wid,),
    ).fetchone()
    next_pos = int(max_pos_row["p"]) + 1

    sections = []
    used_overwrite_targets: set[int] = set()
    for g in groups:
        raw_label = str(g["label"])
        final_label, overwrite_sid = _resolve_label_conflicts(
            conn, wid, raw_label, res_map
        )
        if final_label is None:  # skip_incoming
            continue

        items = []
        seen: set[int] = set()
        for item in g["items"]:
            num = int(item["number"])
            if num in seen:
                continue
            seen.add(num)
            canon = normalize_answer(item.get("answer", ""))
            if not canon:
                raise AppError(
                    422,
                    f"{num}번 정답 '{str(item.get('answer', ''))[:20]}'은(는)"
                    " 객관식/숫자 형식이 아닙니다.",
                )
            display = str(item.get("answer_display") or item.get("answer") or "")
            items.append((num, canon, display))

        did_overwrite = False
        if overwrite_sid is not None and overwrite_sid not in used_overwrite_targets:
            dal.replace_section_keys(conn, overwrite_sid, final_label, items)
            sid = overwrite_sid
            used_overwrite_targets.add(sid)
            did_overwrite = True
        else:
            cur = conn.execute(
                "INSERT INTO sections(workbook_id, label, position) VALUES (?, ?, ?)",
                (wid, final_label, next_pos),
            )
            next_pos += 1
            sid = int(cur.lastrowid)
            dal.insert_keys(conn, sid, items)

        sections.append(
            {
                "id": sid,
                "workbook_id": wid,
                "label": final_label,
                "problem_count": len(items),
                "overwritten": did_overwrite,
            }
        )

    if not sections:
        raise AppError(422, "저장할 섹션이 없습니다. 모든 그룹이 폐기되었습니다.")
    return {"sections": sections}
