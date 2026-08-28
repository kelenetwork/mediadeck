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


def test_root_serves_panel() -> None:
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 401
        r = client.get("/", headers=_basic())
        assert r.status_code == 200
        assert "mediadeck" in r.text and "/static/app.js" in r.text
        assert client.get("/static/app.js", headers=_basic()).status_code == 200
        assert client.get("/api/whoami", headers=_basic()).json()["user"] == "admin"


def test_emby_libraries() -> None:
    with TestClient(app) as client:
        libs = client.get("/api/emby/libraries", headers=_basic()).json()
        assert libs and libs[0]["name"] == "demo-movies"
        assert {"name", "type", "items", "locations"} <= set(libs[0])


def test_mounts_mock() -> None:
    with TestClient(app) as client:
        snap = client.get("/api/mounts", headers=_basic()).json()
        assert snap["available"] is True
        labels = {m["label"] for m in snap["data"]["mounts"]}
        assert {"media-main", "media-union"} <= labels
        bad = [m for m in snap["data"]["mounts"] if not m["alive"]]
        assert bad and bad[0]["stuck_processes"] == 2


def test_tasks_mock() -> None:
    with TestClient(app) as client:
        snap = client.get("/api/tasks", headers=_basic()).json()
        assert snap["available"] is True
        tasks = snap["data"]["tasks"]
        assert tasks
        failing = [t for t in tasks if t["failure_streak"] > 0]
        assert failing and failing[0]["last_status"] == "failed"
        required = {
            "name", "schedule", "enabled", "last_run", "last_status",
            "last_duration_ms", "exit_code", "failure_streak", "last_error",
        }
        for item in tasks:
            assert required <= set(item)


def test_storage_mock() -> None:
    with TestClient(app) as client:
        headers = _basic()
        remotes = client.get("/api/storage/remotes", headers=headers).json()
        names = {item["name"] for item in remotes}
        assert {"mock-drive", "mock-s3"} <= names
        drive = next(item for item in remotes if item["name"] == "mock-drive")
        assert drive["options"]["token"] == "***"
        added = client.post(
            "/api/storage/remotes",
            headers=headers,
            json={
                "name": "mock-extra",
                "type": "alias",
                "options": {"remote": "mock-drive"},
            },
        )
        assert added.status_code == 200
        assert added.json()["name"] == "mock-extra"
        tested = client.post("/api/storage/remotes/mock-drive/test", headers=headers)
        assert tested.status_code == 200
        assert tested.json()["ok"] is True
        mounts = client.get("/api/storage/mounts", headers=headers).json()
        by_name = {item["name"]: item for item in mounts}
        assert by_name["media-main"]["status"] == "active"
        assert by_name["media-cold"]["status"] == "inactive"
        created = client.post(
            "/api/storage/mounts",
            headers=headers,
            json={
                "name": "media-hot",
                "remote": "mock-drive",
                "remote_path": "hot",
                "target": "media-hot",
                "read_only": True,
                "allow_other": True,
            },
        )
        assert created.status_code == 200
        started = client.post("/api/storage/mounts/media-hot/start", headers=headers)
        assert started.status_code == 200
        stopped = client.post("/api/storage/mounts/media-hot/stop", headers=headers)
        assert stopped.status_code == 200
        deleted = client.delete("/api/storage/mounts/media-hot", headers=headers)
        assert deleted.status_code == 200
        remaining = {
            item["name"] for item in client.get("/api/storage/mounts", headers=headers).json()
        }
        assert "media-hot" not in remaining
        bad_name = client.post(
            "/api/storage/remotes",
            headers=headers,
            json={"name": "bad name", "type": "alias", "options": {}},
        )
        assert bad_name.status_code == 422
        bad_target = client.post(
            "/api/storage/mounts",
            headers=headers,
            json={
                "name": "escape",
                "remote": "mock-drive",
                "remote_path": "",
                "target": "../escape",
            },
        )
        assert bad_target.status_code == 422


# ---- settings center -------------------------------------------------------
def test_settings_overview_and_masking() -> None:
    with TestClient(app) as client:
        overview = client.get("/api/settings", headers=_basic()).json()
        assert overview["mock_mode"] is True
        assert {"emby", "dispatch", "nodes"} <= set(overview)
        # secrets are never returned in cleartext
        assert "api_key" not in overview["emby"]
        assert overview["dispatch"]["policy"] == "affinity"


def test_settings_emby_save_and_secret_retention() -> None:
    with TestClient(app) as client:
        saved = client.put("/api/settings/emby", headers=_basic(), json={
            "enabled": True, "url": "http://emby.test:8096/",
            "api_key": "supersecretkey123456", "timeout_seconds": 20,
        })
        assert saved.status_code == 200
        body = saved.json()
        assert body["url"] == "http://emby.test:8096"      # trailing slash normalised
        assert body["api_key_set"] is True
        assert body["api_key_masked"] == "supe********3456"
        assert "supersecretkey123456" not in saved.text     # never leaks

        # editing the URL without resubmitting the key keeps the stored secret
        again = client.put("/api/settings/emby", headers=_basic(), json={
            "url": "http://emby.test:9096", "api_key": "__KEEP__",
        }).json()
        assert again["api_key_set"] is True
        assert again["url"] == "http://emby.test:9096"

        # settings survive as a persisted document
        assert client.get("/api/settings/emby", headers=_basic()).json()["api_key_set"]


def test_settings_emby_validation() -> None:
    with TestClient(app) as client:
        bad_url = client.put("/api/settings/emby", headers=_basic(),
                             json={"url": "emby.test:8096", "api_key": "k"})
        assert bad_url.status_code == 422
        assert "http" in bad_url.json()["detail"]
        assert client.put("/api/settings/emby", headers=_basic(),
                          json={"url": "", "api_key": "k"}).status_code == 422
        # enabling without a key is rejected rather than silently half-configured
        assert client.put("/api/settings/emby", headers=_basic(), json={
            "enabled": True, "url": "http://emby.test:8096", "api_key": "",
        }).status_code == 422
        assert client.put("/api/settings/emby", headers=_basic(), json={
            "url": "http://emby.test:8096", "api_key": "k", "timeout_seconds": 999,
        }).status_code == 422


def test_settings_emby_test_connection() -> None:
    with TestClient(app) as client:
        r = client.post("/api/settings/emby/test", headers=_basic(), json={})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["server_name"] == "demo-emby"


def test_node_crud_reconfigures_scheduler() -> None:
    with TestClient(app) as client:
        created = client.post("/api/nodes", headers=_basic(), json={
            "name": "edge-1", "base_url": "https://edge1.test",
            "probe_url": "http://10.0.0.9:9800/load", "capacity": 30,
        })
        assert created.status_code == 200 and created.json()["capacity"] == 30
        names = {n["name"] for n in client.get("/api/nodes", headers=_basic()).json()}
        assert "edge-1" in names          # live scheduler picked it up, no restart

        updated = client.put("/api/nodes/edge-1", headers=_basic(),
                             json={"capacity": 50}).json()
        assert updated["capacity"] == 50 and updated["base_url"] == "https://edge1.test"

        assert client.post("/api/nodes", headers=_basic(), json={
            "name": "edge-1", "base_url": "https://x.test",
            "probe_url": "http://x.test/load"}).status_code == 422   # duplicate
        assert client.post("/api/nodes", headers=_basic(), json={
            "name": "bad name!", "base_url": "https://x.test",
            "probe_url": "http://x.test/load"}).status_code == 422
        assert client.post("/api/nodes", headers=_basic(), json={
            "name": "edge-2", "base_url": "ftp://x.test",
            "probe_url": "http://x.test/load"}).status_code == 422
        assert client.put("/api/nodes/ghost", headers=_basic(),
                          json={"capacity": 20}).status_code == 404

        assert client.delete("/api/nodes/edge-1", headers=_basic()).json()["deleted"]
        names = {n["name"] for n in client.get("/api/nodes", headers=_basic()).json()}
        assert "edge-1" not in names
        assert client.delete("/api/nodes/edge-1", headers=_basic()).status_code == 404


def test_dispatch_policy_switch() -> None:
    with TestClient(app) as client:
        saved = client.put("/api/settings/dispatch", headers=_basic(),
                           json={"policy": "least-load", "load_threshold": 0.5}).json()
        assert saved == {"policy": "least-load", "load_threshold": 0.5}
        pick = client.get("/api/dispatch/pick?path=a/b.mkv", headers=_basic()).json()
        assert pick["policy"] == "least-load"
        assert client.put("/api/settings/dispatch", headers=_basic(),
                          json={"policy": "random"}).status_code == 422
        assert client.put("/api/settings/dispatch", headers=_basic(),
                          json={"policy": "affinity", "load_threshold": 0}).status_code == 422
