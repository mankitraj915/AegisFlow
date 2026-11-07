import pytest
from fastapi.testclient import TestClient

from aegisflow import runtime
from aegisflow.api import create_app
from aegisflow.db import connect


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry backoff delays instead of sleeping. Patches the default
    runtime only, so a simulated world keeps its own simulated sleep."""
    calls = []
    monkeypatch.setattr(runtime.current, "sleep", calls.append)
    return calls


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))
