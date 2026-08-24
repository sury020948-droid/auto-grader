"""Auth tokens, login flow states, and strict tenant isolation."""

import pytest
from fastapi.testclient import TestClient

from app import config
from app.services import auth_tokens as at


@pytest.fixture(autouse=True)
def _oauth_off(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)


class TestTokenService:
    def test_roundtrip(self):
        token = at.issue_token(42)
        assert at.verify_token(token) == 42

    def test_tampered_payload_rejected(self):
        token = at.issue_token(42)
        body, sig = token.split(".")
        assert at.verify_token(f"{body}x.{sig}") is None

    def test_wrong_secret_rejected(self, monkeypatch):
        token = at.issue_token(7)
        monkeypatch.setenv("SESSION_SECRET", "another-secret")
        assert at.verify_token(token) is None

    def test_expired_rejected(self):
        token = at.issue_token(7, ttl_secs=-10)
        assert at.verify_token(token) is None

    def test_garbage_rejected(self):
        for bad in ("", "abc", "a.b.c", ".."):
            assert at.verify_token(bad) is None


class TestLocalMode:
    def test_me_reports_local_user(self, client):
        data = client.get("/api/auth/me").json()
        assert data["oauth_enabled"] is False
        assert data["name"] == "로컬 사용자"

    def test_dev_token_authenticates(self, client):
        r = client.post("/api/auth/dev-token")
        assert r.status_code == 200
        token = r.json()["token"]
        res = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert res["id"] == r.json()["user"]["id"]

    def test_config_endpoint(self, client):
        assert client.get("/api/auth/config").json() == {"oauth_enabled": False}


class TestOAuthRequiredMode:
    @pytest.fixture()
    def oauth_client(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "d2"))
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
        from app.main import create_app

        app = create_app()
        with TestClient(app) as c:
            yield c

    def test_unauthenticated_401(self, oauth_client):
        assert oauth_client.get("/api/workbooks").status_code == 401
        assert oauth_client.get("/api/auth/me").status_code == 401

    def test_invalid_bearer_401(self, oauth_client):
        r = oauth_client.get(
            "/api/workbooks", headers={"Authorization": "Bearer nope"}
        )
        assert r.status_code == 401

    def test_valid_token_passes(self, oauth_client):
        # seed a user directly then present a valid token
        from app.db import connect

        conn = connect()
        uid = int(
            conn.execute(
                "INSERT INTO users(google_sub, email) VALUES ('g-x', 'x@t')"
            ).lastrowid
        )
        conn.commit()
        conn.close()
        r = oauth_client.get(
            "/api/workbooks",
            headers={"Authorization": f"Bearer {at.issue_token(uid)}"},
        )
        assert r.status_code == 200


class TestTenantIsolation:
    @pytest.fixture()
    def two_user_apps(self, client):
        """User A uses the default local user via `client`; user B overrides."""
        from app.db import connect
        from app.deps import get_current_user
        from app.main import create_app

        wid_a = client.post("/api/workbooks", json={"title": "A의 워크북"}).json()["id"]

        conn = connect()
        uid_b = int(
            conn.execute(
                "INSERT INTO users(google_sub, email) VALUES ('g-b', 'b@t')"
            ).lastrowid
        )
        conn.commit()
        conn.close()

        app_b = create_app()
        app_b.dependency_overrides[get_current_user] = lambda: {
            "id": uid_b,
            "gemini_api_key": "",
        }
        return wid_a, TestClient(app_b)

    def test_workbooks_invisible_across_users(self, client, two_user_apps):
        wid_a, client_b = two_user_apps
        books_b = client_b.get("/api/workbooks").json()
        assert all(b["id"] != wid_a for b in books_b)
        assert client_b.get(f"/api/workbooks/{wid_a}").status_code == 404

    def test_cross_user_delete_blocked(self, client, two_user_apps):
        wid_a, client_b = two_user_apps
        assert client_b.delete(f"/api/workbooks/{wid_a}").status_code == 404
        # A's data still intact
        assert client.get(f"/api/workbooks/{wid_a}").status_code == 200

    def test_sections_and_attempts_scoped(self, client, two_user_apps):
        wid_a, client_b = two_user_apps
        pv = client.post(
            "/api/extract-text",
            json={"raw_text": "Day 01\n1. 3 2. 4 3. 1"},
        ).json()
        sid = client.post(
            f"/api/workbooks/{wid_a}/sections/import",
            json={
                "structure": "headers",
                "header_type": "day",
                "entries": [
                    {"number": e["number"], "answer": e["answer"], "line": e["line"]}
                    for e in pv["entries"]
                ],
                "headers": pv["headers"],
            },
        ).json()["sections"][0]["id"]
        att = client.post(
            "/api/attempts", json={"section_id": sid, "answers": {"1": "3"}}
        ).json()

        assert client_b.get(f"/api/sections/{sid}").status_code == 404
        assert (
            client_b.get(f"/api/sections/{sid}/attempts").status_code == 404
        ) or client_b.get(f"/api/sections/{sid}/attempts").json() == []
        assert client_b.get(f"/api/attempts/{att['id']}").status_code == 404
        assert (
            client_b.post(
                "/api/attempts",
                json={"section_id": sid, "answers": {"1": "9"}},
            ).status_code
            == 404
        )

        _ = config  # imported for parity with other modules under test
