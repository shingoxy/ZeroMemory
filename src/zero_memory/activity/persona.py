import json
from datetime import datetime
from uuid import uuid4

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import digest, dumps, now
from zero_memory.schemas.models import IdentityPatch


class Persona:
    def __init__(self, store, identity):
        self.store, self.identity = store, identity

    def state(self, scope, at=None):
        at = at or now()
        with self.store.read() as db:
            row = db.execute(
                "SELECT body FROM persona_states WHERE user_id=? AND agent_id=? AND project_id=?", scope.sql()
            ).fetchone()
        legacy = self.identity.state()
        traits = self.identity.get()["soul"]["traits"]
        baseline = {
            "energy": 65,
            "curiosity": traits["curiosity"],
            "patience_current": traits["patience"],
            "engagement": 50,
            "social_willingness": 50,
        }
        value = (
            json.loads(row[0])
            if row
            else {
                **legacy,
                **{k: v for k, v in baseline.items() if k not in legacy},
                "version": 1,
            }
        )
        # A pure projection; reads do not save decay or alter the legacy state file.
        elapsed = max(
            0,
            int(
                (datetime.fromisoformat(at) - datetime.fromisoformat(value["updated_at"])).total_seconds()
                // 900
            ),
        )
        for key, target in baseline.items():
            current = value.get(key, target)
            value[key] = max(0, min(100, current + max(-elapsed, min(elapsed, target - current))))
        value["mood"] = (
            "tired" if value["energy"] < 25 else "curious" if value["engagement"] >= 65 else "calm"
        )
        return {
            **value,
            "simulated": True,
            "projected_at": at,
            "rules": "状态只影响表达；不改变事实、隐私、授权或工具权限。零是虚构角色。",
        }

    def advance(self, scope, event, request_id, expected_version, reason):
        event_id = digest(dumps([*scope.sql(), request_id]))
        with self.store.write() as db:
            old = db.execute("SELECT body FROM persona_events WHERE id=?", (event_id,)).fetchone()
            if old:
                old = json.loads(old[0])
                if (old["event"], old["reason"]) != (event, reason):
                    raise ServiceError("idempotency_conflict", "同一事件键的内容已变化", 409)
                return old["after"]
            value = self.state(scope)
            if value["version"] != expected_version:
                raise ServiceError("version_conflict", "模拟状态已变化，请刷新", 409)
            if event not in {"engaged", "routine", "rest", "reset"}:
                raise ServiceError("state_event", "未知模拟事件")
            if event == "reset":
                traits = self.identity.get()["soul"]["traits"]
                value.update(
                    energy=65,
                    curiosity=traits["curiosity"],
                    patience_current=traits["patience"],
                    engagement=50,
                    social_willingness=50,
                )
            else:
                deltas = {
                    "engaged": (-2, 2, 1, 3, 1),
                    "routine": (-1, -1, -1, -2, -1),
                    "rest": (8, 0, 3, -1, 2),
                }[event]
                for key, delta in zip(
                    ("energy", "curiosity", "patience_current", "engagement", "social_willingness"), deltas
                ):
                    value[key] = min(100, max(0, value[key] + delta))
            value.update(version=value["version"] + 1, updated_at=now())
            value["mood"] = (
                "tired" if value["energy"] < 25 else "curious" if value["engagement"] >= 65 else "calm"
            )
            db.execute("INSERT OR REPLACE INTO persona_states VALUES(?,?,?,?)", (*scope.sql(), dumps(value)))
            record = {
                "event": event,
                "reason": reason,
                "source_type": "manual",
                "time": now(),
                "after": value,
            }
            db.execute(
                "INSERT INTO persona_events VALUES(?,?,?,?,?)", (event_id, *scope.sql(), dumps(record))
            )
            self.store.audit(db, "persona_event", "admin", event_id, {"event": event})
            return value

    def propose(self, scope, body, actor):
        value = {
            "id": str(uuid4()),
            "description": body.description,
            "status": "pending_review",
            "identity_version": self.identity.get()["version"],
            "created_at": now(),
            "actor": actor,
            "soul": body.soul.model_dump() if body.soul else None,
            "style": body.style.model_dump() if body.style else None,
        }
        with self.store.write() as db:
            db.execute(
                "INSERT INTO persona_proposals VALUES(?,?,?,?,?)", (value["id"], *scope.sql(), dumps(value))
            )
            self.store.audit(db, "persona_propose", actor, value["id"])
        return value

    def review(self, scope, proposal_id, patch: IdentityPatch | None):
        with self.store.lock():
            with self.store.read() as db:
                row = db.execute(
                    "SELECT body FROM persona_proposals WHERE id=? AND user_id=? AND agent_id=? AND project_id=?",
                    (proposal_id, *scope.sql()),
                ).fetchone()
            if not row:
                raise ServiceError("not_found", "人格提议不在当前作用域", 404)
            value = json.loads(row[0])
            if value["status"] != "pending_review":
                raise ServiceError("review_state", "人格提议已处理", 409)
            if patch is not None:
                if patch.expected_version != value["identity_version"]:
                    raise ServiceError("version_conflict", "原人格版本已改变，请重新提出建议", 409)
                changed = self.identity.update(patch, reason="persona_proposal:" + proposal_id)
                value.update(status="approved", applied_identity_version=changed["version"])
            else:
                value["status"] = "rejected"
            with self.store.write() as db:
                db.execute("UPDATE persona_proposals SET body=? WHERE id=?", (dumps(value), proposal_id))
                self.store.audit(db, "persona_review", "admin", proposal_id, {"status": value["status"]})
        return value
