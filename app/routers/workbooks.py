from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from .. import db as dal
from ..db import get_conn
from ..deps import get_current_user
from ..errors import AppError
from ..schemas import WorkbookCreate, WorkbookUpdate

router = APIRouter(tags=["workbooks"])

ID = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _require_workbook(conn, wid: int, uid: int) -> dict[str, Any]:
    wb = dal.get_workbook_summary(conn, wid, uid)
    if not wb:
        raise HTTPException(status_code=404, detail="워크북을 찾을 수 없습니다.")
    return wb


@router.get("/workbooks")
def read_workbooks(
    user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    return dal.list_workbooks(conn, int(user["id"]))


@router.post("/workbooks", status_code=201)
def create_workbook(
    payload: WorkbookCreate,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    title = payload.title.strip()
    if not title:
        raise AppError(400, "제목을 입력해 주세요.")
    wid = dal.create_workbook(conn, int(user["id"]), title)
    return {"id": wid, "title": title, "section_count": 0}


@router.get("/workbooks/{wid}")
def read_workbook(
    wid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    wb = dal.get_workbook_summary(conn, wid, int(user["id"]))
    if not wb:
        raise HTTPException(status_code=404, detail="워크북을 찾을 수 없습니다.")
    wb["sections"] = dal.list_sections(conn, wid, int(user["id"]))
    return wb


@router.patch("/workbooks/{wid}")
def rename_workbook(
    wid: ID,
    payload: WorkbookUpdate,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    title = payload.title.strip()
    if not title:
        raise AppError(400, "제목을 입력해 주세요.")
    uid = int(user["id"])
    wb = _require_workbook(conn, wid, uid)
    dal.update_workbook_title(conn, wid, uid, title)
    wb["title"] = title
    return wb


@router.delete("/workbooks/{wid}", status_code=204)
def remove_workbook(
    wid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    _require_workbook(conn, wid, int(user["id"]))
    dal.delete_workbook(conn, wid, int(user["id"]))


@router.get("/sections/{sid}")
def read_section(
    sid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    sec = dal.get_section(conn, sid, int(user["id"]))
    if not sec:
        raise HTTPException(status_code=404, detail="섹션을 찾을 수 없습니다.")
    wb = dal.get_workbook(conn, sec["workbook_id"], int(user["id"]))
    keys = dal.get_keys(conn, sid, int(user["id"]))
    return {
        "id": sid,
        "label": sec["label"],
        "workbook_id": sec["workbook_id"],
        "workbook_title": wb["title"] if wb else "",
        "numbers": sorted(keys),
    }


@router.delete("/sections/{sid}", status_code=204)
def remove_section(
    sid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    """Delete a single owned session (section) — its keys and attempts cascade,
    sibling sections and the workbook stay untouched."""
    if not dal.delete_section(conn, sid, int(user["id"])):
        raise HTTPException(status_code=404, detail="섹션을 찾을 수 없습니다.")


@router.get("/workbooks/{wid}/stats")
def workbook_stats(
    wid: ID, user: dict = Depends(get_current_user), conn=Depends(get_conn)
):
    _require_workbook(conn, wid, int(user["id"]))
    uid = int(user["id"])
    sections = []
    for s in dal.list_sections(conn, wid, uid):
        sections.append(
            {
                "section_id": s["id"],
                "label": s["label"],
                "attempt_count": s["attempt_count"],
                "latest_percent": s["latest_percent"],
                "best_percent": s["best_percent"],
            }
        )
    return {"sections": sections, "top_missed": dal.top_missed(conn, wid, uid)}
