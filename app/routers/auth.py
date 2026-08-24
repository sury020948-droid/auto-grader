import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import db as dal
from ..db import get_conn
from ..deps import ensure_local_user, get_current_user, oauth_enabled
from ..services import auth_tokens

router = APIRouter(tags=["auth"])


def _user_public(user: dict) -> dict:
    return {
        "id": user["id"],
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
    }


@router.get("/auth/config")
def auth_config():
    return {"oauth_enabled": oauth_enabled()}


@router.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    data = _user_public(user)
    data["oauth_enabled"] = oauth_enabled()
    return data


@router.get("/auth/google/start")
def google_start(request: Request):
    if not oauth_enabled():
        return RedirectResponse("/?auth_error=not_configured")
    origin = str(request.query_params.get("origin") or request.base_url).rstrip("/")
    state = auth_tokens.make_state()
    resp = RedirectResponse(auth_tokens.build_google_auth_url(origin, state))
    resp.set_cookie(
        "ag_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, conn=Depends(get_conn)):
    if not oauth_enabled():
        return RedirectResponse("/#auth=failed")

    params = request.query_params
    err = params.get("error")
    code = params.get("code")
    state = params.get("state", "")
    cookie_state = request.cookies.get("ag_oauth_state", "")
    origin = str(params.get("origin") or request.base_url).rstrip("/")
    redirect_target = urllib.parse.quote(origin + "/", safe="")

    if err or not code or not auth_tokens.verify_state(state, cookie_state):
        return RedirectResponse(f"/#auth=failed&next={redirect_target}")

    claims = auth_tokens.exchange_code(code, origin)
    if not claims or not claims.get("sub"):
        return RedirectResponse(f"/#auth=failed&next={redirect_target}")

    user = dal.upsert_google_user(
        conn,
        sub=str(claims["sub"]),
        email=str(claims.get("email") or ""),
        name=str(claims.get("name") or ""),
        picture=str(claims.get("picture") or ""),
    )
    token = auth_tokens.issue_token(int(user["id"]))
    resp = RedirectResponse(f"/#token={token}")
    resp.delete_cookie("ag_oauth_state")
    return resp


@router.post("/auth/dev-token")
def dev_token(conn=Depends(get_conn)):
    """Issue a token for the shared local user — only when OAuth is OFF.

    Exists so tooling/tests can act as the local user without Google setup.
    """
    if oauth_enabled():
        raise HTTPException(403, "OAuth가 설정된 환경에서는 사용할 수 없습니다.")
    user = ensure_local_user(conn)
    return {"token": auth_tokens.issue_token(int(user["id"])), "user": _user_public(user)}
