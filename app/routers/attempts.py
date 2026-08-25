from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from .. import db as dal
from ..db import get_conn
from ..deps import get_current_user
from ..errors import AppError
from ..schemas import AttemptCreate, FromMissesPayload
from ..services.grader import grade
from ..services.normalizer import canonical_type

router = APIRouter(tags=["attempts"])

ID = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _require_section(conn, sid: int, uid: int) -> dict[str, Any]:
    sec = dal.get_section(conn, sid, uid)
    if not sec:
        raise HTTPException(status_code=404, detail="섹션을 찾을 수 없습니다.")
    return sec


def _serialize_attempt(att: dict[str, Any]) -> dict[str, Any]:
    results = [
        {**r, "qtype": canonical_type(r["expected"]) if r["expected"] else None}
        for r in att.get("results", [])
    ]
    return {
        "id": att["id"],
        "section_id": att["section_id"],
        "taken_at": att["taken_at"],
        "total": att["total"],
        "score": att["score"],
        "percent": att["percent"],
        "is_full_attempt": bool(att.get("is_full_attempt", True)),
        "results": results,
        "wrong_numbers": [r["number"] for r in results if r["status"] == "incorrect"],
        "unanswered_numbers": [
            r["number"] for r in results if r["status"] == "unanswered"
        ],
    }


def _merged_answers(
    base: dict[str, Any], new_answers: dict[str, str]
) -> dict[str, str]:
    """Overlay retry answers onto the base attempt's given answers.

    Previously solved questions keep their original answers (and thus their
    'correct' status); only re-attempted numbers change.
    """
    merged: dict[str, str] = {}
    for r in base.get("results", []):
        given = str(r.get("given") or "").strip()
        if given:
            merged[str(r["number"])] = given
    for num, val in new_answers.items():
        if str(val).strip():
            merged[str(num)] = str(val)
        else:
            merged.pop(str(num), None)  # explicit blank = retract answer
    return merged


@router.post("/attempts", status_code=201)
def create_attempt(
    payload: AttemptCreate,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    uid = int(user["id"])
    _require_section(conn, payload.section_id, uid)

    if payload.merge_attempt_id is not None:
        base = dal.get_attempt(conn, payload.merge_attempt_id, uid)
        if not base:
            raise HTTPException(
                status_code=404,
                detail="병합할 이전 채점 기록을 찾을 수 없습니다.",
            )
        if base["section_id"] != payload.section_id:
            raise AppError(
                400, "이전 채점 기록이 다른 섹션의 것이어서 병합할 수 없습니다."
            )
        effective = _merged_answers(base, payload.answers)
    else:
        effective = dict(payload.answers)

    keys = dal.get_keys(conn, payload.section_id, uid)
    keys_canonical = {n: c for n, (c, _) in keys.items()}
    keys_display = {n: d for n, (_, d) in keys.items()}
    graded = grade(keys_canonical, keys_display, effective, answered_only=payload.answered_only)
    aid = dal.create_attempt(
        conn,
        uid,
        payload.section_id,
        graded["score"],
        graded["total"],
        graded["percent"],
        graded["results"],
        is_full_attempt=payload.merge_attempt_id is None,
    )
    saved = dal.get_attempt(conn, aid, uid)
    out = _serialize_attempt(saved)
    if payload.merge_attempt_id is not None:
        out["merged_from"] = payload.merge_attempt_id
    notes = []
    if graded["extra_ignored"]:
        notes.append(f"목록에 없는 문항 {len(graded['extra_ignored'])}개는 무시했습니다.")
    if payload.answered_only and graded["unanswered_numbers"]:
        notes.append(f"{len(graded['unanswered_numbers'])}문항은 미응답으로 채점에서 제외했습니다.")
    if notes:
        out["note"] = " ".join(notes)
    return out


@router.get("/sections/{sid}/attempts")
def read_attempts(
    sid: ID,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    _require_section(conn, sid, int(user["id"]))
    return dal.list_attempts(conn, sid, int(user["id"]))


@router.get("/attempts/{aid}")
def read_attempt(
    aid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    att = dal.get_attempt(conn, aid, int(user["id"]))
    if not att:
        raise HTTPException(status_code=404, detail="채점 기록을 찾을 수 없습니다.")
    return _serialize_attempt(att)


@router.delete("/attempts/{aid}", status_code=204)
def remove_attempt(
    aid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    if not dal.delete_attempt(conn, aid, int(user["id"])):
        raise HTTPException(status_code=404, detail="채점 기록을 찾을 수 없습니다.")


@router.post("/attempts/from-misses", status_code=201)
def retry_misses(
    payload: FromMissesPayload,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    att = dal.get_attempt(conn, payload.attempt_id, int(user["id"]))
    if not att:
        raise HTTPException(status_code=404, detail="채점 기록을 찾을 수 없습니다.")
    numbers = [r["number"] for r in att["results"] if r["status"] != "correct"]
    if not numbers:
        raise AppError(422, "다시 풀 오답이 없습니다. 모두 정답입니다!")
    return {
        "section_id": att["section_id"],
        "attempt_id": att["id"],
        "numbers": numbers,
    }
