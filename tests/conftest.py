import uuid

import pytest
from fastapi.testclient import TestClient

DEVICE_HEADER = "X-Device-User-Id"


@pytest.fixture()
def device_id() -> str:
    """The device UUID used by the primary `client` fixture."""
    return str(uuid.uuid4())


@pytest.fixture()
def client(tmp_path, monkeypatch, device_id):
    monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
    from app.main import create_app

    app = create_app()
    with TestClient(app, headers={DEVICE_HEADER: device_id}) as c:
        yield c


@pytest.fixture()
def other_device_client(tmp_path, monkeypatch):
    """A second device sharing the same data dir — for isolation tests."""
    monkeypatch.setenv("AUTO_GRADER_DATA_DIR", str(tmp_path / "data"))
    from app.main import create_app

    app = create_app()
    with TestClient(app, headers={DEVICE_HEADER: str(uuid.uuid4())}) as c:
        yield c
