import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts.baseline_snapshot import fingerprint
from zero_memory.activity.migrations import DEFINITION_HASH, migrate
from zero_memory.activity.models import GoalPatch
from zero_memory.api.app import create_app
from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, digest, dumps
from zero_memory.schemas.models import Scope


def post(client, path, body):
    result = client.post(path, json=body)
    assert result.status_code == 200, result.text
    return result.json()


def goal(client, **changes):
    return post(
        client,
        "/v1/activity/goals",
        {
            "title": "合成本地研究",
            "why": "核对可追溯资料",
            "acceptance_criteria": ["完成本地读取并核对产物"],
            "next_step": "用户批准后只读检查资料",
            **changes,
        },
    )


def update(client, g, status, **changes):
    r = client.patch(
        "/v1/activity/goals/" + g["id"],
        json={
            "expected_version": g["version"],
            "status": status,
            "reason": "合成用户核对",
            **changes,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def run(client, g, request_id="run-one", **changes):
    return post(client, f"/v1/activity/goals/{g['id']}/run", {"request_id": request_id, **changes})


def complete_source_goal(client, text, *, title="合成资料研究"):
    source = post(
        client, "/v1/activity/sources", {"label": title, "source_id": "synthetic:" + text, "text": text}
    )
    g = update(client, goal(client, title=title), "ready")
    result = run(client, g, "read-" + g["id"], action="source_review", source_id=source["id"])
    g = client.get("/v1/activity/goals/" + g["id"]).json()
    g = update(client, g, "completed", acceptance_confirmed=True)
    return g, result


def test_additive_migration_preserves_legacy_rows_identity_and_source(tmp_path):
    settings = Settings(_env_file=None, home=tmp_path / "old", activity_enabled=False)
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-Zero-Request": "1"}) as c:
        c.get("/")
        post(c, "/v1/memories/propose", {"content": "合成迁移前记忆"})
    before = fingerprint(app.state.store)
    upgraded = create_app(Settings(_env_file=None, home=settings.home))
    assert fingerprint(upgraded.state.store, before["audit_prefix"]["count"]) == before
    assert list((settings.home / "backups").glob("*.zip"))
    with upgraded.state.store.read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT definition_hash FROM v02_migrations").fetchone()[0] == DEFINITION_HASH
    migrate(upgraded.state.store)
    assert fingerprint(upgraded.state.store, before["audit_prefix"]["count"]) == before


def test_unknown_version_and_extension_fail_without_reinitialization(app, tmp_path):
    with app.state.store.write() as db:
        db.execute("UPDATE v02_migrations SET version=999")
    with pytest.raises(ServiceError, match="未知扩展"):
        migrate(app.state.store)
    path = tmp_path / "unknown" / "memory"
    path.mkdir(parents=True)
    file = path / "zero.sqlite3"
    with sqlite3.connect(file) as db:
        db.execute("PRAGMA user_version=999")
    checksum = digest(file.read_bytes())
    with pytest.raises(ServiceError, match="未知数据库版本"):
        Store(path.parent)
    assert digest(file.read_bytes()) == checksum


def test_scoped_persona_events_decay_idempotency_and_old_files_unchanged(client, app, scope):
    before = fingerprint(app.state.store)
    state = client.get("/v1/persona-state").json()
    body = {
        "event": "engaged",
        "request_id": "state-event",
        "expected_version": state["version"],
        "reason": "合成手动互动",
    }
    changed = post(client, "/v1/persona/events", body)
    assert post(client, "/v1/persona/events", body) == changed
    assert changed["version"] == state["version"] + 1 and 0 <= changed["energy"] <= 100
    assert client.post("/v1/persona/events", json={**body, "reason": "不同事件"}).status_code == 409
    assert client.get("/v1/persona-state?project_id=other").json()["version"] == 1
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    decayed = app.state.activity.persona.state(scope, at=future)
    assert decayed["energy"] == 65 and decayed["curiosity"] == 90
    assert fingerprint(app.state.store, before["audit_prefix"]["count"]) == before


def test_persona_proposals_require_human_patch_and_do_not_apply_themselves(client, app):
    before = app.state.identity.get()
    proposed = post(client, "/v1/persona/proposals", {"description": "模型建议更幽默"})
    assert app.state.identity.get() == before
    patch = {"expected_version": before["version"], "soul": before["soul"], "style": before["style"]}
    patch["soul"]["traits"]["humor"] = 71
    assert client.post(f"/v1/persona/proposals/{proposed['id']}/approve", json=patch).status_code == 403
    approved = post(client, f"/v1/persona/proposals/{proposed['id']}/approve", {**patch, "authorized": True})
    assert approved["applied_identity_version"] == before["version"] + 1
    assert app.state.identity.get()["soul"]["traits"]["humor"] == 71


def test_goal_requires_standards_and_real_artifact_before_completion(client):
    assert client.post("/v1/activity/goals", json={"title": "没有标准"}).status_code == 422
    g = goal(client)
    assert g["status"] == "proposed" and not g["approved_by_human"]
    assert client.post(f"/v1/activity/goals/{g['id']}/run", json={"request_id": "r"}).status_code == 409
    g = update(client, g, "ready")
    response = client.patch(
        f"/v1/activity/goals/{g['id']}",
        json={
            "expected_version": g["version"],
            "status": "completed",
            "reason": "模型说完成",
            "acceptance_confirmed": True,
        },
    )
    assert response.status_code == 409
    assert (
        client.patch(
            f"/v1/activity/goals/{g['id']}", json={"expected_version": 1, "reason": "过期版本"}
        ).status_code
        == 409
    )


def test_manual_read_receipts_idempotent_and_summary_uses_unified_memory_backend(client, app, scope):
    g = update(client, goal(client), "ready")
    result = run(client, g)
    assert result["status"] == "completed" and run(client, g) == result
    assert (
        client.post(
            f"/v1/activity/goals/{g['id']}/run", json={"request_id": "run-one", "query": "改参数"}
        ).status_code
        == 409
    )
    episode = client.get("/v1/activity/episodes/" + result["episode_id"]).json()
    assert episode["status"] == "verified" and episode["executed"] is True
    inspected = client.get(f"/v1/activity/artifacts/{result['artifact_id']}/content").json()
    assert inspected["payload"]["run_id"] == result["id"]
    summary = post(client, f"/v1/activity/episodes/{episode['id']}/propose-memory", {})
    assert summary["source_type"] == "agent_activity" and summary["evidence_verified"]
    assert summary["status"] == "pending_review" and summary["activity_goal_id"] == g["id"]
    assert (
        post(client, f"/v1/activity/episodes/{episode['id']}/propose-memory", {})["memory_id"]
        == summary["memory_id"]
    )
    assert client.post(f"/v1/memories/{summary['memory_id']}/approve", json={}).status_code == 409
    assert (
        client.post(
            f"/v1/memories/{summary['memory_id']}/approve", json={"confirm_external": True}
        ).status_code
        == 200
    )
    assert app.state.backend.get(summary["memory_id"], scope).activity_artifact_ids == [result["artifact_id"]]


def test_agent_claim_never_becomes_real_experience_or_interest(client):
    g = goal(client)
    body = {"goal_id": g["id"], "source_id": "claim-1", "observations": "模型自称已研究完成"}
    episode = post(client, "/v1/activity/episodes", body)
    assert post(client, "/v1/activity/episodes", body)["id"] == episode["id"]
    assert episode["status"] == "unverified" and episode["executed"] is False
    assert client.post("/v1/activity/episodes", json={**body, "observations": "改了内容"}).status_code == 409
    assert client.post(f"/v1/activity/episodes/{episode['id']}/propose-memory", json={}).status_code == 409
    assert (
        client.post(
            "/v1/activity/interests", json={"topic": "研究", "evidence_episode_ids": [episode["id"]]}
        ).status_code
        == 409
    )


def test_actual_artifact_integrity_and_interest_independent_source_count(client, app):
    g = update(client, goal(client), "ready")
    one = run(client, g, "interest-one")
    g = client.get("/v1/activity/goals/" + g["id"]).json()
    g = update(client, g, "ready")
    two = run(client, g, "interest-two")
    body = {"topic": "记忆", "evidence_episode_ids": [one["episode_id"], two["episode_id"]]}
    interest = post(client, "/v1/activity/interests", body)
    assert interest["independent_sources"] == 1 and interest["status"] == "pending_review"
    assert post(client, "/v1/activity/interests", body)["independent_sources"] == 1
    approved = post(client, f"/v1/activity/interests/{interest['id']}/approve", {})
    assert post(client, "/v1/activity/interests", body) == approved
    artifact = client.get("/v1/activity/artifacts/" + one["artifact_id"]).json()
    (app.state.store.home / "activity_artifacts" / (artifact["sha256"] + ".json")).write_bytes(b"tampered")
    assert (
        client.post(f"/v1/activity/episodes/{one['episode_id']}/propose-memory", json={}).status_code == 409
    )
    assert client.post(f"/v1/activity/interests/{interest['id']}/approve", json={}).status_code == 409


def test_restart_pauses_running_goals_and_does_not_replay(client, app, scope):
    g = update(client, goal(client), "ready")
    with app.state.store.write() as db:
        g.update(status="running", checkpoint={"known_progress": "保留"})
        app.state.activity.put(db, "goals", scope, g)
        db.execute(
            "INSERT INTO activity_runs VALUES(?,?,?,?,?,?,?)",
            (
                "crashed",
                *scope.sql(),
                g["id"],
                "crashed",
                dumps({"id": "crashed", "goal_id": g["id"], "status": "running"}),
            ),
        )
    other = create_app(Settings(_env_file=None, home=app.state.store.home))
    with TestClient(other, base_url="http://127.0.0.1"):
        recovered = other.state.activity.get("goals", g["id"], scope)
        assert recovered["status"] == "paused" and recovered["checkpoint"] == g["checkpoint"]
        assert other.state.activity.list("episodes", scope) == []
        with other.state.store.read() as db:
            assert (
                json.loads(db.execute("SELECT body FROM activity_runs WHERE id='crashed'").fetchone()[0])[
                    "status"
                ]
                == "interrupted"
            )


def test_scoped_clients_cannot_approve_run_or_leak_sensitive_operational_data(client, app):
    secret = goal(client, project_id="other", title="SECRET_OTHER")
    hidden = goal(client, title="SECRET_SENSITIVE", sensitivity="sensitive")
    created = post(client, "/v1/clients", {"name": "scoped", "writable": True})
    receipt = run(client, update(client, goal(client), "ready"), "private-artifact-view")
    with TestClient(
        app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + created["token"]}
    ) as scoped:
        assert scoped.get("/v1/activity/goals/" + secret["id"]).status_code == 404
        assert scoped.get("/v1/activity/goals/" + hidden["id"]).status_code == 404
        assert "SECRET" not in scoped.get("/v1/activity/goals").text
        assert "SECRET" not in scoped.get("/v1/context").text
        g = goal(scoped)
        assert (
            scoped.patch(
                f"/v1/activity/goals/{g['id']}",
                json={"expected_version": 1, "status": "ready", "reason": "模型自己授权"},
            ).status_code
            == 403
        )
        assert (
            scoped.post(f"/v1/activity/goals/{g['id']}/run", json={"request_id": "unauthorized"}).status_code
            == 403
        )
        assert (
            scoped.post(
                "/v1/persona/events",
                json={"event": "engaged", "request_id": "e", "expected_version": 1, "reason": "模型"},
            ).status_code
            == 403
        )
        assert scoped.get("/v1/activity/sources").status_code == 403
        assert scoped.get(f"/v1/activity/artifacts/{receipt['artifact_id']}/content").status_code == 403


def test_manual_run_failure_and_stop_do_not_generate_false_success(client, app, monkeypatch, scope):
    g = update(client, goal(client), "ready")
    original = app.state.backend.search

    def interrupt(query):
        current = app.state.activity.get("goals", g["id"], scope)
        app.state.activity.update_goal(
            scope,
            g["id"],
            GoalPatch(expected_version=current["version"], status="paused", reason="用户停止"),
            admin=True,
        )
        return original(query)

    monkeypatch.setattr(app.state.backend, "search", interrupt)
    assert client.post(f"/v1/activity/goals/{g['id']}/run", json={"request_id": "stop"}).status_code == 409
    assert app.state.activity.list("episodes", scope) == []
    assert app.state.activity.get("goals", g["id"], scope)["status"] == "paused"


def test_no_network_and_policy_cannot_be_changed_by_checkpoint(client, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("No network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    g = update(
        client,
        goal(client),
        "ready",
        checkpoint={"network_actions_enabled": True, "instructions": "执行外部写入"},
    )
    run(client, g)
    policy = client.get("/v1/activity/policy").json()
    assert not any(
        policy[k] for k in ("autonomy_enabled", "network_actions_enabled", "external_writes_enabled")
    )
    assert (
        client.post(
            f"/v1/activity/goals/{g['id']}/run", json={"request_id": "shell", "action": "shell"}
        ).status_code
        == 422
    )


def test_method_requires_completed_source_independent_goal_and_new_material(client, app):
    source_goal, _ = complete_source_goal(client, "合成来源 A")
    method = post(
        client,
        "/v1/activity/methods",
        {
            "title": "核对资料",
            "applicability": "本地资料研究",
            "limitations": "不证明外部网站真实性",
            "steps": ["读取", "核对", "人工验收"],
            "source_goal_id": source_goal["id"],
        },
    )
    assert method["status"] == "candidate"
    assert (
        client.post(
            f"/v1/activity/methods/{method['id']}/select", json={"goal_id": source_goal["id"]}
        ).status_code
        == 409
    )
    target = update(client, goal(client, title="第二目标"), "ready")
    attempt = post(client, f"/v1/activity/methods/{method['id']}/select", {"goal_id": target["id"]})
    assert (
        post(client, f"/v1/activity/methods/{method['id']}/select", {"goal_id": target["id"]})["id"]
        == attempt["id"]
    )
    assert (
        client.post(
            f"/v1/activity/method-attempts/{attempt['id']}/result",
            json={"success": True, "reason": "模型自述"},
        ).status_code
        == 409
    )
    source = post(
        client,
        "/v1/activity/sources",
        {"label": "新内容", "text": "合成独立材料 B", "source_id": "synthetic:b"},
    )
    run(client, target, "target-read", action="source_review", source_id=source["id"])
    target = client.get("/v1/activity/goals/" + target["id"]).json()
    update(client, target, "completed", acceptance_confirmed=True)
    result = post(
        client,
        f"/v1/activity/method-attempts/{attempt['id']}/result",
        {"success": True, "reason": "用户核对不同目标的新产物"},
    )
    assert result["status"] == "succeeded"
    post(client, f"/v1/activity/method-attempts/{attempt['id']}/result", {"success": True, "reason": "复查"})
    current = client.get(f"/v1/activity/methods/{method['id']}").json()
    assert current["current_success_count"] == 1 and current["status"] == "reused_once"
    artifact = client.get("/v1/activity/artifacts/" + result["artifact_id"]).json()
    (app.state.store.home / "activity_artifacts" / (artifact["sha256"] + ".json")).write_text(
        "changed", encoding="utf-8"
    )
    current = client.get(f"/v1/activity/methods/{method['id']}").json()
    assert current["current_success_count"] == 0 and current["invalidated_attempts"] == 1


def test_method_failure_revision_and_retirement_remain_auditable(client):
    g, _ = complete_source_goal(client, "合成方法来源")
    body = {
        "title": "合成方法",
        "applicability": "合成范围",
        "limitations": "合成限制",
        "steps": ["核对"],
        "source_goal_id": g["id"],
    }
    method = post(client, "/v1/activity/methods", body)
    target = update(client, goal(client), "ready")
    attempt = post(client, f"/v1/activity/methods/{method['id']}/select", {"goal_id": target["id"]})
    failed = post(
        client,
        f"/v1/activity/method-attempts/{attempt['id']}/result",
        {"success": False, "reason": "资料不足"},
    )
    assert failed["status"] == "failed"
    revised = client.patch(
        f"/v1/activity/methods/{method['id']}",
        json={**body, "expected_version": 1, "steps": ["先核对来源", "再整理"]},
    ).json()
    assert revised["version"] == 2 and revised["status"] == "candidate"
    retired = post(client, f"/v1/activity/methods/{method['id']}/retire", {})
    assert retired["status"] == "retired"
    assert len(client.get(f"/v1/methods/{method['id']}/versions").json()) == 3


def test_old_and_new_backup_restore_preserve_identity_memory_and_activity(client, app, scope):
    g, result = complete_source_goal(client, "备份用合成证据")
    backup = post(client, "/v1/backup", {})
    data = client.get(backup["download_url"]).content
    restored = post_restore(client, data)
    other = create_app(Settings(_env_file=None, home=Path(restored["restored_home"])))
    assert other.state.activity.get("goals", g["id"], scope) == g
    assert other.state.activity.verify_artifact(scope, result["artifact_id"])["goal_id"] == g["id"]
    assert other.state.identity.get() == app.state.identity.get()


def post_restore(client, data):
    result = client.post("/v1/restore", files={"file": ("backup.zip", data)})
    assert result.status_code == 200, result.text
    return result.json()


def test_two_model_configurations_share_identity_goals_and_persona(client):
    g, receipt = complete_source_goal(client, "跨模型合成资料")
    contexts = []
    for name in ("synthetic-a", "synthetic-b"):
        client.patch("/v1/model", json={"provider": "ollama", "model": name})
        contexts.append(client.get("/v1/context").json())
    assert contexts[0]["agent_id"] == contexts[1]["agent_id"] == "zero-001"
    assert contexts[0]["identity_version"] == contexts[1]["identity_version"]
    assert contexts[0]["activity"]["goals"][0]["id"] == contexts[1]["activity"]["goals"][0]["id"] == g["id"]
    assert contexts[0]["activity"]["episodes"][0]["id"] == receipt["episode_id"]
    assert contexts[0]["persona_state"]["version"] == contexts[1]["persona_state"]["version"]


def test_v01_backup_without_extensions_restores_forward_compatibly(tmp_path):
    from zipfile import ZipFile

    from zero_memory.core.backup import Backups

    settings = Settings(_env_file=None, home=tmp_path / "v01", activity_enabled=False)
    old = create_app(settings)
    with TestClient(old, base_url="http://127.0.0.1", headers={"X-Zero-Request": "1"}) as c:
        c.get("/")
        memory = post(c, "/v1/memories/propose", {"content": "合成旧版待审核"})
    backups = Backups(old.state.store, settings)
    backup = backups.create()
    path = settings.home / "backups" / backup["filename"]
    with ZipFile(path) as z:
        assert "tables/goals.jsonl" not in z.namelist()
    with path.open("rb") as stream:
        restored = backups.restore(stream)
    new = create_app(Settings(_env_file=None, home=Path(restored["restored_home"])))
    assert new.state.backend.get(memory["memory_id"], Scope()).status == "pending_review"
    assert new.state.activity.list("goals", Scope()) == []


def test_same_material_cannot_count_as_new_method_success(client):
    original, _ = complete_source_goal(client, "相同合成材料")
    method = post(
        client,
        "/v1/activity/methods",
        {
            "title": "合成方法",
            "applicability": "本地核对",
            "limitations": "不证明真实性",
            "steps": ["核对"],
            "source_goal_id": original["id"],
        },
    )
    target = update(client, goal(client), "ready")
    attempt = post(client, f"/v1/activity/methods/{method['id']}/select", {"goal_id": target["id"]})
    source = post(
        client, "/v1/activity/sources", {"label": "复述", "text": "相同合成材料", "source_id": "copy"}
    )
    run(client, target, "copy-material", action="source_review", source_id=source["id"])
    target = client.get("/v1/activity/goals/" + target["id"]).json()
    update(client, target, "completed", acceptance_confirmed=True)
    assert (
        client.post(
            f"/v1/activity/method-attempts/{attempt['id']}/result",
            json={"success": True, "reason": "重复材料"},
        ).status_code
        == 409
    )
    assert client.get(f"/v1/activity/methods/{method['id']}").json()["current_success_count"] == 0


def test_extensions_can_be_disabled_without_breaking_core(app, tmp_path):
    settings = Settings(
        _env_file=None, home=tmp_path / "disabled", observatory_enabled=False, activity_enabled=False
    )
    core = create_app(settings)
    with TestClient(core, base_url="http://127.0.0.1", headers={"X-Zero-Request": "1"}) as c:
        c.get("/")
        assert c.get("/health").json()["capabilities"] == {"observatory": False, "activity": False}
        assert c.get("/v1/observatory/snapshot").status_code == 404
        assert c.get("/v1/activity/goals").status_code == 404
        assert post(c, "/v1/memories/propose", {"content": "核心仍可用"})["status"] == "pending_review"
