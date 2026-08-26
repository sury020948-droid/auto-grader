from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from .. import db as dal
from ..db import get_conn
from ..deps import get_current_user
from ..services.sessions import compute_breakdown
from .attempts import serialize_attempt

router = APIRouter(tags=["sessions"])

ID = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _require_section(conn, sid: int, uid: int) -> dict[str, Any]:
    sec = dal.get_section(conn, sid, uid)
    if not sec:
        raise HTTPException(status_code=404, detail="섹션을 찾을 수 없습니다.")
    return sec


def _serialize_session(sess: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": sess["id"],
        "section_id": sess["section_id"],
        "status": sess["status"],
        "started_at": sess["started_at"],
        "finished_at": sess["finished_at"],
        "first_score": sess["first_score"],
        "first_total": sess["first_total"],
        "first_percent": sess["first_percent"],
    }


@router.get("/sections/{sid}/session")
def read_open_session(
    sid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    """The section's currently in-progress session, if any -- the quiz
    screen calls this on entry to auto-resume: derive which numbers still
    need retrying (status != correct) and what was answered last time
    (given) straight from `latest_attempt.results`, no client-side retry
    bookkeeping needed.

    Deliberately disjoint from GET /sessions/{id}: a *finished* session's
    detail is only ever served there, never here.
    """
    uid = int(user["id"])
    _require_section(conn, sid, uid)
    sess = dal.get_open_session(conn, sid, uid)
    if not sess:
        raise HTTPException(status_code=404, detail="진행 중인 채점 세션이 없습니다.")
    atts = dal.list_session_attempts(conn, sess["id"])
    latest = atts[-1] if atts else None
    return {
        "session_id": sess["id"],
        "section_id": sess["section_id"],
        "started_at": sess["started_at"],
        "submission_count": len(atts),
        "latest_attempt": serialize_attempt(latest, sess) if latest else None,
    }


@router.post("/sessions/{sess_id}/finish")
def finish_session(
    sess_id: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    """채점 끝내기 -- idempotent: finishing an already-finished session just
    returns its (unchanged) summary rather than erroring."""
    sess = dal.finish_session(conn, sess_id, int(user["id"]))
    if not sess:
        raise HTTPException(status_code=404, detail="채점 세션을 찾을 수 없습니다.")
    return _serialize_session(sess)


@router.get("/sections/{sid}/sessions")
def read_finished_sessions(
    sid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    """One history entry = one finished session -- replaces the removed
    GET /sections/{sid}/attempts. Each row carries only its frozen
    first-submission score; open sessions never appear here."""
    uid = int(user["id"])
    _require_section(conn, sid, uid)
    return [_serialize_session(s) for s in dal.list_finished_sessions(conn, sid, uid)]


@router.get("/sessions/{sess_id}")
def read_session_detail(
    sess_id: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    """Full detail for one finished session -- 404s on an in-progress
    session too (owned or not): its live state is only ever served by
    GET /sections/{sid}/session, never here. Reused both for clicking a
    past history entry and for the screen "채점 끝내기" itself lands on."""
    uid = int(user["id"])
    sess = dal.get_session(conn, sess_id, uid)
    if not sess or sess["status"] != "finished":
        raise HTTPException(status_code=404, detail="채점 세션을 찾을 수 없습니다.")
    atts = dal.list_session_attempts(conn, sess["id"])
    first = next((a for a in atts if a["is_first_submission"]), None)
    keys = dal.get_keys(conn, sess["section_id"], uid)
    breakdown = compute_breakdown(sorted(keys), atts)
    return {
        **_serialize_session(sess),
        "submission_count": len(atts),
        "first_results": serialize_attempt(first, sess)["results"] if first else [],
        "breakdown": breakdown,
    }
