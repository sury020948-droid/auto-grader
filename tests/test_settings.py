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

    def test_env_only_reports_env_source(self, client, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", FULL_KEY)
        from app.main import create_app

        with TestClient(create_app()) as c:
            data = c.get("/api/settings/api-key").json()
        assert data["set"] is True
        assert data["source"] == "env"
        assert FULL_KEY not in str(data)


class TestSaveApiKey:
    def test_save_and_status(self, client):
        r = client.post("/api/settings/api-key", json={"api_key": f"  {FULL_KEY}  "})
        assert r.status_code == 200
        data = r.json()
        assert data == {"set": True, "source": "app", "masked": "AIzaSy...cdef"}
        assert FULL_KEY not in str(data)

    def test_health_reflects_saved_key(self, client):
        before = client.get("/api/health").json()
        assert before["gemini_available"] is False
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        after = client.get("/api/health").json()
        assert after["gemini_available"] is True

    def test_saved_key_used_by_extract(self, client, monkeypatch):
        class FakeResponse:
            text = '{"entries": [{"number": 1, "type": "numeric", "answer": "7"}], "notes": []}'

        class FakeModels:
            def generate_content(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        from app.services import gemini as gemini_service

        monkeypatch.setattr(gemini_service, "_client", lambda: FakeClient())
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", b"\x89PNG fake", "image/png")},
        )
        assert r.status_code == 200
        assert r.json()["engine"] == "gemini-vision"

    def test_blank_rejected(self, client):
        assert client.post("/api/settings/api-key", json={"api_key": ""}).status_code == 422
        assert client.post("/api/settings/api-key", json={"api_key": "   "}).status_code == 400

    def test_too_long_rejected(self, client):
        r = client.post("/api/settings/api-key", json={"api_key": "k" * 301})
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
        monkeypatch.setenv("GOOGLE_API_KEY", "env-only-key")
        from app.main import create_app

        with TestClient(create_app()) as c:
            data = c.delete("/api/settings/api-key").json()
            assert data["set"] is True
            assert data["source"] == "env"


class TestPersistence:
    def test_survives_restart(self, client):
        client.post("/api/settings/api-key", json={"api_key": FULL_KEY})
        from app.main import create_app

        with TestClient(create_app()) as c:
            assert c.get("/api/health").json()["gemini_available"] is True
            status = c.get("/api/settings/api-key").json()
        assert status["source"] == "app"
