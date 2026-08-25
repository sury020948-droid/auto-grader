"""Per-device auth (X-Device-User-Id header) and strict tenant isolation."""

import uuid

import pytest
from fastapi.testclient import TestClient

DEVICE_HEADER = "X-Device-User-Id"


def device_headers(device_id: str | None = None) -> dict[str, str]:
    return {DEVICE_HEADER: device_id or str(uuid.uuid4())}


class TestDeviceHeaderRequired:
    @pytest.fixture()
    def raw_client(self, tmp_path, monkeypatch):
        """Same data dir as `client`, but sends no device header by default."""
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
        from app.main import create_app

        app = create_app()
        with TestClient(app) as c:
            yield c

    def test_missing_header_rejected(self, raw_client):
        r = raw_client.get("/api/workbooks")
        assert r.status_code == 401
        assert DEVICE_HEADER in r.json()["detail"]

    @pytest.mark.parametrize("bad", ["not-a-uuid", "12345", "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"])
    def test_malformed_header_400(self, raw_client, bad):
        r = raw_client.get("/api/workbooks", headers=device_headers(bad))
        assert r.status_code == 400

    def test_blank_header_treated_as_missing(self, raw_client):
        r = raw_client.get("/api/workbooks", headers={DEVICE_HEADER: "   "})
        assert r.status_code == 401

    def test_valid_uuid_accepted(self, client):
        assert client.post("/api/workbooks", json={"title": "t"}).status_code == 201


class TestDeviceIdentity:
    def test_same_device_across_restarts_sees_same_data(
        self, client, device_id, tmp_path, monkeypatch
    ):
        wid = client.post("/api/workbooks", json={"title": "내 문제집"}).json()["id"]

        from app.main import create_app

        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
        with TestClient(create_app(), headers=device_headers(device_id)) as c2:
            books = c2.get("/api/workbooks").json()
            assert [b["id"] for b in books] == [wid]

    def test_uuid_format_normalized(self, tmp_path, monkeypatch):
        """Case/braces/hyphen variants of one UUID must map to the same user."""
        base = uuid.uuid4()
        variants = [
            str(base),
            str(base).upper(),
            f"{{{base}}}",
            str(base).replace("-", ""),
        ]
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
        from app.db import connect, get_or_create_device_user, init_db

        init_db()
        conn = connect()
        try:
            uids = {get_or_create_device_user(conn, v)["id"] for v in variants}
        finally:
            conn.close()
        assert len(uids) == 1


class TestTenantIsolation:
    def test_workbooks_invisible_across_devices(self, client, other_device_client):
        wid = client.post("/api/workbooks", json={"title": "A의 워크북"}).json()["id"]
        books_b = other_device_client.get("/api/workbooks").json()
        assert all(b["id"] != wid for b in books_b)
        assert other_device_client.get(f"/api/workbooks/{wid}").status_code == 404

    def test_cross_device_delete_blocked(self, client, other_device_client):
        wid = client.post("/api/workbooks", json={"title": "A의 워크북"}).json()["id"]
        assert other_device_client.delete(f"/api/workbooks/{wid}").status_code == 404
        # A's data still intact
        assert client.get(f"/api/workbooks/{wid}").status_code == 200

    def test_sections_and_attempts_scoped(self, client, other_device_client):
        wid = client.post("/api/workbooks", json={"title": "A의 워크북"}).json()["id"]
        pv = client.post(
            "/api/extract-text",
            json={"raw_text": "Day 01\n1. 3 2. 4 3. 1"},
        ).json()
        sid = client.post(
            f"/api/workbooks/{wid}/sections/import",
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

        assert other_device_client.get(f"/api/sections/{sid}").status_code == 404
        assert (
            other_device_client.get(f"/api/sections/{sid}/attempts").status_code
            == 404
        ) or other_device_client.get(f"/api/sections/{sid}/attempts").json() == []
        assert (
            other_device_client.get(f"/api/attempts/{att['id']}").status_code == 404
        )
        assert (
            other_device_client.post(
                "/api/attempts",
                json={"section_id": sid, "answers": {"1": "9"}},
            ).status_code
            == 404
        )

    def test_stats_scoped_across_devices(self, client, other_device_client):
        """The workbook stats endpoint (sections + top_missed) must 404 for a
        foreign device, not just omit/scope the data -- top_missed's rows now
        carry workbook_id/workbook_title/section_id, so a leak here would
        expose another user's workbook identity, not just question numbers."""
        wid = client.post("/api/workbooks", json={"title": "A의 워크북"}).json()["id"]
        pv = client.post(
            "/api/extract-text",
            json={"raw_text": "Day 01\n1. 3 2. 4 3. 1"},
        ).json()
        sid = client.post(
            f"/api/workbooks/{wid}/sections/import",
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
        client.post("/api/attempts", json={"section_id": sid, "answers": {"1": "9"}})

        assert other_device_client.get(f"/api/workbooks/{wid}/stats").status_code == 404

    def test_gemini_keys_isolated_per_device(self, client, other_device_client):
        client.post(
            "/api/settings/api-key", json={"api_key": "AIzaSyD-device-a-key-000"}
        )
        status = other_device_client.get("/api/settings/api-key").json()
        assert status["set"] is False


class TestRemovedOAuthSurface:
    def test_auth_endpoints_gone(self, client):
        for path in (
            "/api/auth/config",
            "/api/auth/me",
            "/api/auth/google/start",
            "/api/auth/dev-token",
        ):
            assert client.get(path).status_code in (404, 405), path

    def test_health_serves_anonymous_callers(self, tmp_path, monkeypatch):
        """Load balancers hit /api/health without a device header."""
        monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
        from app.main import create_app

        with TestClient(create_app()) as c:
            data = c.get("/api/health").json()
        assert data["status"] == "ok"
        assert "oauth_enabled" not in data
