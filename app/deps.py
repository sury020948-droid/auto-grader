"""Request-scoped device authentication.

Every client generates a UUIDv4 once, stores it in localStorage, and sends it
as the ``X-Device-User-Id`` header on all ``/api/*`` requests. The backend
maps that UUID to an isolated per-device user row; all workbook/section/
attempt records are scoped to it. Requests without a valid header are
rejected (401 missing / 400 malformed).
"""

import uuid
from typing import Any

from fastapi import Depends, HTTPException, Request

from . import db as dal
from .db import get_conn

DEVICE_ID_HEADER = "X-Device-User-Id"


def device_id_from_request(request: Request) -> str:
    raw = request.headers.get(DEVICE_ID_HEADER, "").strip()
    if not raw:
        raise HTTPException(
            401,
            f"{DEVICE_ID_HEADER} 헤더가 필요합니다. 브라우저에서 기기 ID를"
            " 생성해 요청에 포함해 주세요.",
        )
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise HTTPException(
            400,
            f"{DEVICE_ID_HEADER} 헤더 값이 유효한 UUID 형식이 아닙니다.",
        ) from None


def get_current_user(request: Request, conn=Depends(get_conn)) -> dict[str, Any]:
    """Resolve (and lazily create) the user bound to this device UUID."""
    device_id = device_id_from_request(request)
    return dal.get_or_create_device_user(conn, device_id)


def try_current_user(request: Request, conn=Depends(get_conn)) -> dict[str, Any] | None:
    """Like get_current_user but returns None instead of raising.

    Used by endpoints that must also serve anonymous callers
    (e.g. GET /api/health for load-balancer checks)."""
    try:
        return get_current_user(request, conn)
    except HTTPException:
        return None
