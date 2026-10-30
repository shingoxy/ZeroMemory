import json

import pytest
from fastapi.testclient import TestClient

from zero_memory.api.app import create_app
from zero_memory.core.config import Settings
from zero_memory.schemas.models import Scope


@pytest.fixture
def app(tmp_path):
    return create_app(
        Settings(_env_file=None, home=tmp_path / "zero", llm_provider="none", allow_cloud=False)
    )


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-Zero-Request": "1"}) as client:
        client.get("/")
        yield client


@pytest.fixture
def scope():
    return Scope()


def import_data(client, value, filename="conversations.json", scope=None):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
    response = client.post(
        "/v1/imports",
        files={"file": (filename, data)},
        data={"scope": json.dumps((scope or Scope()).model_dump())},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    response = client.post(f"/v1/imports/{job['id']}/start", json={})
    assert response.status_code == 200, response.text
    return client.get(f"/v1/imports/{job['id']}").json()


def propose(client, content="我正在开发星舟项目", **kwargs):
    response = client.post("/v1/memories/propose", json={"content": content, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, memory, **kwargs):
    response = client.post(
        f"/v1/memories/{memory['memory_id']}/approve",
        json={**{k: memory[k] for k in Scope.model_fields}, "confirm_external": True, **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()
