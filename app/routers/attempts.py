from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from .. import db as dal
from ..db import get_conn
from ..deps import get_current_user
from ..schemas import AttemptCreate
from ..services.grader import grade
from ..services.normalizer import canonical_type
from ..services.sessions import merge_answers

router = APIRouter(tags=["attempts"])

ID = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _require_section(conn, sid: int, uid: int) -> dict[str, Any]:
    sec = dal.get_section(conn, sid, uid)
    if not sec:
        raise HTTPException(status_code=404, detail="섹션을 찾을 수 없습니다.")
    return sec


def serialize_attempt(
    att: dict[str, Any], session: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Shared AttemptResult shape -- reused by GET /attempts/{aid} and by
    the sessions router (POST /attempts's own response, and the open
    session's `latest_attempt`), so the object looks identical everywhere
    it appears.

    `session` is the attempt's owning sessions row, when the caller already
    has it (or can cheaply fetch it) -- it's used to derive `session_finished`
    and to surface the session's frozen `first_percent` alongside this
    submission's own score, so a retry's results screen can show both without
    a second round-trip; pass None when unknown (session_finished then reads
    False, first_percent None, rather than guessing).
    """
    results = [
        {**r, "qtype": canonical_type(r["expected"]) if r["expected"] else None}
        for r in att.get("results", [])
    ]
    return {
        "id": att["id"],
        "section_id": att["section_id"],
        "session_id": att.get("session_id"),
        "is_first_submission": bool(att.get("is_first_submission", True)),
        "submission_seq": att.get("submission_seq", 1),
        "session_finished": bool(session and session["status"] == "finished"),
        "first_percent": session["first_percent"] if session else None,
        "taken_at": att["taken_at"],
        "total": att["total"],
        "score": att["score"],
        "percent": att["percent"],
        "results": results,
        "wrong_numbers": [r["number"] for r in results if r["status"] == "incorrect"],
        "unanswered_numbers": [
            r["number"] for r in results if r["status"] == "unanswered"
        ],
    }


@router.post("/attempts", status_code=201)
def create_attempt(
    payload: AttemptCreate,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    """Grade one submission for a section, auto-detecting a retry.

    No open session for this section -> this is submission 1 of a brand new
    session: grade the given answers as-is and freeze this result onto the
    new session's first_score/first_total/first_percent.

    An open session already exists -> this is a retry: overlay the given
    answers onto the session's latest submission (services.sessions.
    merge_answers -- unchanged overlay/retract semantics from the old
    merge_attempt_id flow, now auto-driven by the open session instead of a
    client-supplied base attempt id), regrade the full merged set, and
    store it as the next submission_seq. The session's own first_* fields
    are left untouched -- only the first submission ever sets them.
    """
    uid = int(user["id"])
    _require_section(conn, payload.section_id, uid)

    keys = dal.get_keys(conn, payload.section_id, uid)
    keys_canonical = {n: c for n, (c, _) in keys.items()}
    keys_display = {n: d for n, (_, d) in keys.items()}

    open_session = dal.get_open_session(conn, payload.section_id, uid)
    prior_attempts: list[dict[str, Any]] = []
    if open_session is None:
        effective = dict(payload.answers)
    else:
        prior_attempts = dal.list_session_attempts(conn, open_session["id"])
        latest_results = prior_attempts[-1]["results"] if prior_attempts else []
        effective = merge_answers(latest_results, payload.answers)

    graded = grade(
        keys_canonical, keys_display, effective, answered_only=payload.answered_only
    )

    if open_session is None:
        session_id = dal.create_session(
            conn,
            uid,
            payload.section_id,
            graded["score"],
            graded["total"],
            graded["percent"],
        )
        is_first_submission = True
        submission_seq = 1
    else:
        session_id = open_session["id"]
        is_first_submission = False
        last_seq = prior_attempts[-1]["submission_seq"] if prior_attempts else 0
        submission_seq = last_seq + 1

    aid = dal.create_attempt(
        conn,
        uid,
        payload.section_id,
        graded["score"],
        graded["total"],
        graded["percent"],
        graded["results"],
        is_full_attempt=is_first_submission,
        session_id=session_id,
        is_first_submission=is_first_submission,
        submission_seq=submission_seq,
    )
    saved = dal.get_attempt(conn, aid, uid)
    session_row = dal.get_session(conn, session_id, uid)
    out = serialize_attempt(saved, session_row)
    notes = []
    if graded["extra_ignored"]:
        notes.append(f"목록에 없는 문항 {len(graded['extra_ignored'])}개는 무시했습니다.")
    if payload.answered_only and graded["unanswered_numbers"]:
        notes.append(f"{len(graded['unanswered_numbers'])}문항은 미응답으로 채점에서 제외했습니다.")
    if notes:
        out["note"] = " ".join(notes)
    return out


@router.get("/attempts/{aid}")
def read_attempt(
    aid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    uid = int(user["id"])
    att = dal.get_attempt(conn, aid, uid)
    if not att:
        raise HTTPException(status_code=404, detail="채점 기록을 찾을 수 없습니다.")
    session_row = (
        dal.get_session(conn, att["session_id"], uid) if att.get("session_id") else None
    )
    return serialize_attempt(att, session_row)


@router.delete("/attempts/{aid}", status_code=204)
def remove_attempt(
    aid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    if not dal.delete_attempt(conn, aid, int(user["id"])):
        raise HTTPException(status_code=404, detail="채점 기록을 찾을 수 없습니다.")
