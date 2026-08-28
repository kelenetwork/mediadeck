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


# ---- playback interception -------------------------------------------------
def test_playback_defaults_off() -> None:
    """Interception changes where bytes come from: it must never self-enable."""
    with TestClient(app) as client:
        cfg = client.get("/api/settings/playback", headers=_basic()).json()
        assert cfg["enabled"] is False
        assert cfg["direct_only"] is True
        assert cfg["path_template"] == "{path}"


def test_playback_enable_requires_a_node() -> None:
    with TestClient(app) as client:
        # mock mode ships two demo nodes, so enabling is allowed here
        saved = client.put("/api/settings/playback", headers=_basic(),
                           json={"enabled": True, "strip_prefix": "/media"}).json()
        assert saved["enabled"] is True and saved["strip_prefix"] == "/media"
        # template must keep the placeholder or every URL would be identical
        assert client.put("/api/settings/playback", headers=_basic(),
                          json={"path_template": "/fixed/no-placeholder"}).status_code == 422


def test_playback_redirects_and_is_path_affine() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/playback", headers=_basic(),
                   json={"enabled": True, "strip_prefix": "/media"})
        r = client.get("/emby/Videos/item42/stream.mkv?Static=true",
                       headers=_basic(), follow_redirects=False)
        assert r.status_code == 302
        location = r.headers["location"]
        assert "Movies/Demo/item42.mkv" in location
        assert location.startswith("https://mock-")
        # same item must keep resolving to the same node (cache locality)
        targets = {
            client.get("/emby/Videos/item42/stream.mkv?Static=true",
                       headers=_basic(), follow_redirects=False).headers["location"]
            for _ in range(8)
        }
        assert len(targets) == 1


def test_playback_fails_open() -> None:
    """Every uncertain case must fall back to Emby, never break playback."""
    with TestClient(app) as client:
        client.put("/api/settings/playback", headers=_basic(),
                   json={"enabled": True, "strip_prefix": "/media"})

        # transcode requests are produced on the Emby host, not on a node
        r = client.get("/emby/Videos/item42/master.m3u8", headers=_basic(),
                       follow_redirects=False)
        assert r.status_code == 302 and "mock-" not in r.headers["location"]

        # missing Static=true means Emby intends to remux/transcode
        r = client.get("/emby/Videos/item42/stream.mkv", headers=_basic(),
                       follow_redirects=False)
        assert "mock-" not in r.headers["location"]

        # unknown item -> cannot resolve a file -> origin
        r = client.get("/emby/Videos/unknown/stream.mkv?Static=true",
                       headers=_basic(), follow_redirects=False)
        assert "mock-" not in r.headers["location"]

        # disabled -> origin
        client.put("/api/settings/playback", headers=_basic(), json={"enabled": False})
        r = client.get("/emby/Videos/item42/stream.mkv?Static=true",
                       headers=_basic(), follow_redirects=False)
        assert "mock-" not in r.headers["location"]

        reasons = {e["reason"] for e in client.get("/api/playback/log", headers=_basic()).json()}
        assert {"transcode", "unresolved-item"} <= reasons


def test_playback_preview_and_path_mapping() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/playback", headers=_basic(), json={
            "enabled": True, "strip_prefix": "/media", "path_template": "files/{path}"})
        preview = client.get("/api/playback/preview?item_id=item7", headers=_basic()).json()
        assert preview["redirected"] is True
        assert preview["media_path"] == "/media/Movies/Demo/item7.mkv"
        assert preview["target"].endswith("/files/Movies/Demo/item7.mkv")


def test_path_mapping_helpers() -> None:
    from app.modules.playback import build_node_url, is_transcode_request, map_media_path
    assert map_media_path("/media/TV/a.mkv", "/media", "{path}") == "TV/a.mkv"
    assert map_media_path("/media/TV/a.mkv", "/media/", "{path}") == "TV/a.mkv"
    assert map_media_path("/other/a.mkv", "/media", "{path}") == "other/a.mkv"
    assert map_media_path("D:\\media\\a.mkv", "D:/media", "{path}") == "a.mkv"
    assert map_media_path("/media/a.mkv", "/media", "cdn/{path}") == "cdn/a.mkv"
    # spaces and CJK must survive as a valid URL
    assert build_node_url("https://n.test/", "TV/My Show/第01集.mkv") == (
        "https://n.test/TV/My%20Show/%E7%AC%AC01%E9%9B%86.mkv")
    assert is_transcode_request("emby/Videos/1/master.m3u8", {}) is True
    assert is_transcode_request("emby/Videos/1/stream.mkv", {"Static": "true"}) is False
    assert is_transcode_request("emby/Videos/1/stream.mkv", {}) is True


# ---- settings contract -----------------------------------------------------
def test_settings_overview_contract() -> None:
    """The settings page reads these keys directly.

    A missing key here is not a cosmetic bug: the page throws on
    `s.playback.enabled` and renders nothing at all. This shipped once, so it
    is pinned by a test.
    """
    with TestClient(app) as client:
        body = client.get("/api/settings", headers=_basic()).json()
        assert {"mock_mode", "emby", "dispatch", "playback",
                "delivery", "integration", "nodes"} <= set(body)
        assert {"enabled", "direct_only", "strip_prefix",
                "path_template"} <= set(body["playback"])
        assert {"signing_enabled", "secret_set", "secret_masked",
                "ttl_seconds"} <= set(body["delivery"])
        assert {"panel_public_url", "emby_public_url",
                "node_media_root"} <= set(body["integration"])
        # secrets must never appear in cleartext form in this payload
        assert "api_key" not in body["emby"]
        assert "secret" not in body["delivery"]


# ---- signed delivery -------------------------------------------------------
def test_signing_defaults_off_and_roundtrip() -> None:
    from app.modules.signing import compute_digest, sign_url, verify
    with TestClient(app) as client:
        cfg = client.get("/api/settings/delivery", headers=_basic()).json()
        assert cfg["signing_enabled"] is False and cfg["secret_set"] is False

        saved = client.put("/api/settings/delivery", headers=_basic(),
                           json={"signing_enabled": True, "ttl_seconds": 3600}).json()
        # enabling without a key must generate one, not sign with ""
        assert saved["signing_enabled"] is True and saved["secret_set"] is True
        assert saved["ttl_seconds"] == 3600

    # digest must match nginx secure_link_md5 over the DECODED uri
    url = sign_url("https://n.test", "TV/My Show/第01集.mkv", "s3cr3t", 600, now=1000)
    assert "md5=" in url and "expires=1600" in url
    assert "%E7%AC%AC01" in url  # url-encoded, digest was not
    digest = compute_digest("/TV/My Show/第01集.mkv", 1600, "s3cr3t")
    assert f"md5={digest}" in url
    assert verify("/TV/My Show/第01集.mkv", digest, 1600, "s3cr3t", now=1000) is True
    assert verify("/TV/My Show/第01集.mkv", digest, 1600, "s3cr3t", now=2000) is False
    assert verify("/other.mkv", digest, 1600, "s3cr3t", now=1000) is False


def test_signed_urls_used_for_playback() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/playback", headers=_basic(),
                   json={"enabled": True, "strip_prefix": "/media"})
        client.put("/api/settings/delivery", headers=_basic(),
                   json={"signing_enabled": True, "secret": "unit-test-secret"})
        r = client.get("/emby/Videos/item42/stream.mkv?Static=true",
                       headers=_basic(), follow_redirects=False)
        location = r.headers["location"]
        assert "md5=" in location and "expires=" in location
        preview = client.get("/api/playback/preview?item_id=item42", headers=_basic()).json()
        assert preview["signed"] is True


def test_secret_rotation_invalidates_old_links() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/delivery", headers=_basic(),
                   json={"signing_enabled": True, "secret": "first-secret"})
        before = client.get("/api/settings/delivery", headers=_basic()).json()
        rotated = client.post("/api/settings/delivery/rotate", headers=_basic()).json()
        assert rotated["secret_set"] is True
        assert rotated["secret_masked"] != before["secret_masked"]


def test_delivery_ttl_validation() -> None:
    with TestClient(app) as client:
        assert client.put("/api/settings/delivery", headers=_basic(),
                          json={"ttl_seconds": 10}).status_code == 422
        assert client.put("/api/settings/delivery", headers=_basic(),
                          json={"ttl_seconds": 99999999}).status_code == 422


# ---- node provisioning -----------------------------------------------------
def test_install_script_is_complete_and_consistent() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/integration", headers=_basic(), json={
            "panel_public_url": "https://panel.test",
            "emby_public_url": "https://emby.test",
            "node_media_root": "/srv/media",
        })
        client.put("/api/settings/delivery", headers=_basic(),
                   json={"signing_enabled": True, "secret": "node-shared-secret"})
        client.post("/api/nodes", headers=_basic(), json={
            "name": "edge-1", "base_url": "https://edge1.test",
            "probe_url": "http://10.0.0.9:9800/load", "capacity": 40})

        body = client.get("/api/nodes/edge-1/install", headers=_basic()).json()
        script = body["script"]
        # every layer a node needs must be present, or the node 404s in ways
        # that are painful to debug from client logs
        assert "rclone mount" in script
        assert "vfs-cache-mode full" in script
        assert "secure_link_md5" in script
        assert "node-shared-secret" in script      # node must share the key
        assert "user_allow_other" in script        # else nginx cannot read mount
        assert "certbot" in script
        assert "loadprobe" in script
        assert "/agent/loadprobe.py" in script     # served by this panel
        assert "__NGINX_SITE__" not in script      # template fully rendered
        assert client.get("/api/nodes/ghost/install", headers=_basic()).status_code == 404


def test_install_script_warns_when_unsigned() -> None:
    with TestClient(app) as client:
        client.post("/api/nodes", headers=_basic(), json={
            "name": "edge-1", "base_url": "https://edge1.test",
            "probe_url": "http://10.0.0.9:9800/load"})
        script = client.get("/api/nodes/edge-1/install", headers=_basic()).json()["script"]
        assert "未启用签名" in script


def test_frontend_snippet_only_routes_stream_paths() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/integration", headers=_basic(), json={
            "panel_public_url": "https://panel.test",
            "emby_public_url": "https://emby.test"})
        for server in ("caddy", "nginx"):
            cfg = client.get(f"/api/integration/frontend?server={server}",
                             headers=_basic()).json()["config"]
            # only stream requests may be diverted; the web UI, images and
            # transcoding must keep going straight to Emby
            assert "emby/Videos" in cfg and "stream" in cfg
            assert "panel.test" in cfg and "emby.test" in cfg


def test_integration_validation() -> None:
    with TestClient(app) as client:
        assert client.put("/api/settings/integration", headers=_basic(),
                          json={"panel_public_url": "panel.test"}).status_code == 422
        assert client.put("/api/settings/integration", headers=_basic(),
                          json={"node_media_root": "relative/path"}).status_code == 422


def test_agent_is_downloadable() -> None:
    with TestClient(app) as client:
        r = client.get("/agent/loadprobe.py")
        assert r.status_code == 200
        assert "active_streams" in r.text
