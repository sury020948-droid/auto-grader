import json

import pytest
from fastapi.testclient import TestClient

FULL_KEY = "AIzaSyD-fake-key-1234567890abcdef"


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


class TestApiKeyStatus:
    def test_unset_reports_false(self, client):
        r = client.get("/api/settings/api-key")
        assert r.status_code == 200
        assert r.json() == {"set": False, "source": None, "masked": None}

    def test_env_only_reports_server_source(self, client, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", FULL_KEY)
        data = client.get("/api/settings/api-key").json()
        assert data["set"] is True
        assert data["source"] == "server"
        assert FULL_KEY not in str(data)


class TestSaveApiKey:
    def test_save_and_status(self, client):
        r = client.post(
            "/api/settings/api-key", json={"api_key": f"  {FULL_KEY}  "}
        )
        assert r.status_code == 200
        data = r.json()
        assert data == {"set": True, "source": "user", "masked": "AIzaSy...cdef"}
        assert FULL_KEY not in str(data)

    def test_user_key_beats_server_env(self, client, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyD-server-wide-fallback-key")
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        data = client.get("/api/settings/api-key").json()
        assert data["source"] == "user"
        assert "cdef" in data["masked"]

    def test_health_reflects_saved_key(self, client):
        before = client.get("/api/health").json()
        assert before["gemini_available"] is False
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        after = client.get("/api/health").json()
        assert after["gemini_available"] is True

    def test_saved_key_used_by_extract(self, client, monkeypatch):
        payload = {
            "workbook_title": "",
            "groups": [
                {
                    "main_category": "Day 01",
                    "sub_category": None,
                    "items": [{"number": 1, "type": "numeric", "answer": "7"}],
                }
            ],
            "notes": [],
        }

        class FakeResponse:
            text = json.dumps(payload)

        class FakeModels:
            def __init__(self):
                self.seen_keys = []

            def generate_content(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        from app.services import gemini as gemini_service

        holder = FakeClient()
        captured = {}

        def fake_client(key):
            captured["key"] = key
            return holder

        monkeypatch.setattr(gemini_service, "_client", fake_client)
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", b"\x89PNG fake", "image/png")},
        )
        assert r.status_code == 200
        assert r.json()["engine"] == "gemini-vision"
        assert captured["key"] == FULL_KEY  # user's own key reached the SDK

    def test_header_override_used_for_extract(self, client, monkeypatch):
        payload = {
            "workbook_title": "",
            "groups": [
                {
                    "main_category": "Day 01",
                    "items": [{"number": 1, "type": "numeric", "answer": "7"}],
                }
            ],
            "notes": [],
        }

        class FakeResponse:
            text = json.dumps(payload)

        class FakeModels:
            def generate_content(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            models = FakeModels()

        from app.services import gemini as gemini_service

        captured = {}

        def fake_client(key):
            captured["k"] = key
            return FakeClient()

        monkeypatch.setattr(gemini_service, "_client", fake_client)
        header_key = "AIzaSyD-header-supplied-key-000000"
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", b"\x89PNG fake", "image/png")},
            headers={"X-Gemini-Api-Key": header_key},
        )
        assert r.status_code == 200
        assert captured["k"] == header_key

    def test_accepts_non_aiza_format(self, client):
        """Any non-empty string is saved — Google's API validates the key."""
        key = "AQAbcd123notAGoogleKey"
        r = client.post("/api/settings/api-key", json={"api_key": key})
        assert r.status_code == 200
        data = r.json()
        assert data == {"set": True, "source": "user", "masked": "AQAbcd...eKey"}
        assert key in str(data) or data["masked"]

    def test_header_override_accepts_non_aiza(self, client, monkeypatch):
        payload = {
            "workbook_title": "",
            "groups": [
                {
                    "main_category": "Day 01",
                    "items": [{"number": 1, "type": "numeric", "answer": "7"}],
                }
            ],
            "notes": [],
        }

        class FakeResponse:
            text = json.dumps(payload)

        class FakeModels:
            def generate_content(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            models = FakeModels()

        from app.services import gemini as gemini_service

        captured = {}

        def fake_client(key):
            captured["k"] = key
            return FakeClient()

        monkeypatch.setattr(gemini_service, "_client", fake_client)
        header_key = "AQ-header-supplied-oauth-style-key-000000"
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", b"\x89PNG fake", "image/png")},
            headers={"X-Gemini-Api-Key": header_key},
        )
        assert r.status_code == 200
        assert captured["k"] == header_key

    def test_blank_rejected(self, client):
        assert (
            client.post("/api/settings/api-key", json={"api_key": ""}).status_code
            == 422
        )
        assert (
            client.post("/api/settings/api-key", json={"api_key": "   "}).status_code
            == 400
        )

    def test_too_long_rejected(self, client):
        r = client.post("/api/settings/api-key", json={"api_key": "AIza" + "k" * 301})
        assert r.status_code == 422


class TestDeleteApiKey:
    def test_delete_reverts_to_unset(self, client):
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        r = client.delete("/api/settings/api-key")
        assert r.status_code == 200
        assert r.json()["set"] is False
        assert client.get("/api/health").json()["gemini_available"] is False

    def test_delete_falls_back_to_env(self, client, monkeypatch):
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyD-env-only-key-1234567890")
        r = client.delete("/api/settings/api-key")
        assert r.status_code == 200
        assert r.json()["set"] is True
        assert r.json()["source"] == "server"


class TestPersistence:
    def test_survives_restart(self, client, device_id):
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        from app.main import create_app

        with TestClient(
            create_app(), headers={"X-Device-User-Id": device_id}
        ) as c:
            assert c.get("/api/health").json()["gemini_available"] is True
            status = c.get("/api/settings/api-key").json()
        assert status["source"] == "user"

    def test_keys_are_isolated_per_device(self, client, other_device_client):
        """Two devices must never see or use each other's Gemini keys."""
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})

        status = other_device_client.get("/api/settings/api-key").json()
        assert status["set"] is False  # B does not see A's key
        health = other_device_client.get("/api/health").json()
        assert health["gemini_available"] is False  # nor its usage
        # ...while the owning device still does
        assert client.get("/api/settings/api-key").json()["set"] is True
        assert client.get("/api/health").json()["gemini_available"] is True
