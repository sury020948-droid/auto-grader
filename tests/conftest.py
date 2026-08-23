import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
