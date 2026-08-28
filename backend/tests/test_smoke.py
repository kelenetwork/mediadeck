"""Smoke tests: panel boots fully in mock mode with zero credentials."""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from app.main import app


def _basic(user: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_healthz() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_nodes_and_dispatch() -> None:
    with TestClient(app) as client:
        nodes = client.get("/api/nodes", headers=_basic()).json()
        assert len(nodes) == 2
        pick = client.get("/api/dispatch/pick", headers=_basic())
        assert pick.status_code == 200
        assert pick.json()["node"] in {"mock-a", "mock-b"}
        r = client.get("/stream/some/file.mkv", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].endswith("/some/file.mkv")


def test_disable_enable() -> None:
    with TestClient(app) as client:
        assert client.post("/api/nodes/mock-a/disable", headers=_basic()).json()["disabled"]
        for n in client.get("/api/nodes", headers=_basic()).json():
            if n["name"] == "mock-a":
                assert n["manually_disabled"] is True
        assert client.post("/api/nodes/mock-a/enable", headers=_basic()).json()["disabled"] is False


def test_auth_required() -> None:
    with TestClient(app) as client:
        assert client.get("/api/nodes").status_code == 401
        assert client.get("/api/nodes", headers=_basic(password="wrong")).status_code == 401


def test_emby_mock() -> None:
    with TestClient(app) as client:
        users = client.get("/api/emby/users", headers=_basic()).json()
        assert any(u["Name"] == "demo-user-1" for u in users)
        sessions = client.get("/api/emby/sessions", headers=_basic()).json()
        assert sessions and "BitrateMbps" in sessions[0]


def test_pipeline_mock() -> None:
    with TestClient(app) as client:
        snap = client.get("/api/pipeline", headers=_basic()).json()
        assert snap["available"] is True
        names = {q["name"] for q in snap["data"]["queues"]}
        assert "staging" in names and "upload-lanes" in names
