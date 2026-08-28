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


def test_history_and_dispatch_log() -> None:
    with TestClient(app) as client:
        # generate one real dispatch (recorded) via the 302 edge
        client.get("/stream/demo/file.mkv", follow_redirects=False)
        log = client.get("/api/dispatch/log", headers=_basic()).json()
        assert log and log[-1]["node"] in {"mock-a", "mock-b"}
        assert log[-1]["context"] == "demo/file.mkv"
        # dry-run pick must NOT be recorded
        before = len(client.get("/api/dispatch/log", headers=_basic()).json())
        client.get("/api/dispatch/pick", headers=_basic())
        after = len(client.get("/api/dispatch/log", headers=_basic()).json())
        assert after == before
        # unknown node history -> 404
        assert client.get("/api/nodes/nope/history", headers=_basic()).status_code == 404
        # known node history endpoint works (may be empty before first probe)
        r = client.get("/api/nodes/mock-a/history", headers=_basic())
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_emby_user_management() -> None:
    with TestClient(app) as client:
        created = client.post("/api/emby/users", headers=_basic(),
                              json={"name": "new-demo"}).json()
        uid = created["Id"]
        assert created["Name"] == "new-demo"
        assert client.post(f"/api/emby/users/{uid}/disable", headers=_basic()).json()["disabled"]
        users = {u["Id"]: u for u in client.get("/api/emby/users", headers=_basic()).json()}
        assert users[uid]["Policy"]["IsDisabled"] is True
        assert client.post(f"/api/emby/users/{uid}/enable", headers=_basic()).json()["disabled"] is False
        assert client.post(f"/api/emby/users/{uid}/password", headers=_basic(),
                           json={"new_password": "secret123"}).json()["ok"]
        r = client.post(f"/api/emby/users/{uid}/policy", headers=_basic(),
                        json={"SimultaneousStreamLimit": 2, "NotAllowed": "x"})
        assert r.json()["ok"]
        users = {u["Id"]: u for u in client.get("/api/emby/users", headers=_basic()).json()}
        assert users[uid]["Policy"]["SimultaneousStreamLimit"] == 2
        assert "NotAllowed" not in users[uid]["Policy"]
        # unknown user -> 404; empty policy patch -> 422
        assert client.post("/api/emby/users/nope/disable", headers=_basic()).status_code == 404
        assert client.post(f"/api/emby/users/{uid}/policy", headers=_basic(),
                           json={"Whatever": 1}).status_code == 422


def test_import_lane_lifecycle() -> None:
    with TestClient(app) as client:
        job = client.post("/api/imports", headers=_basic(),
                          json={"kind": "drive-link", "source_ref": "mock://share/abc",
                                "category": "tv"}).json()
        jid = job["id"]
        assert job["state"] == "running"
        # progress advances on each refresh until done
        last = 0.0
        for _ in range(6):
            j = client.get(f"/api/imports/{jid}", headers=_basic()).json()
            assert j["progress"] >= last
            last = j["progress"]
            if j["state"] == "done":
                break
        assert j["state"] == "done" and j["items_done"] == j["items_total"] == 5
        # done job is not cancellable
        assert client.post(f"/api/imports/{jid}/cancel", headers=_basic()).status_code == 409
        # validation errors
        assert client.post("/api/imports", headers=_basic(),
                           json={"kind": "nope", "source_ref": "x"}).status_code == 422
        assert client.post("/api/imports", headers=_basic(),
                           json={"kind": "cloud-drive", "source_ref": "  "}).status_code == 422
        assert client.get("/api/imports/zzz", headers=_basic()).status_code == 404
        # list + filter
        cancellable = client.post("/api/imports", headers=_basic(),
                                  json={"kind": "cloud-drive", "source_ref": "mock://folder/1"}).json()
        assert client.post(f"/api/imports/{cancellable['id']}/cancel", headers=_basic()).json()["cancelled"]
        failed = client.get("/api/imports?state=failed", headers=_basic()).json()
        assert any(j["id"] == cancellable["id"] for j in failed)


def test_updater_mock() -> None:
    with TestClient(app) as client:
        v = client.get("/api/update/version", headers=_basic()).json()
        assert v["version"].startswith("v0.0.0")
        chk = client.get("/api/update/check", headers=_basic()).json()
        assert chk["ok"] and chk["update_available"]
        r = client.post("/api/update/apply", headers=_basic(), json={})
        assert r.json()["started"] and r.json()["target"] == "v0.0.1"


def test_semver_helpers() -> None:
    from app.modules.updater import latest_tag, semver_key
    assert semver_key("v1.2.3") == (1, 2, 3)
    assert semver_key("v1.2") is None and semver_key("junk") is None
    assert latest_tag(["v0.1.0", "v0.10.0", "v0.2.9", "nope"]) == "v0.10.0"
    assert latest_tag(["nope"]) is None
