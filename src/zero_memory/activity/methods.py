import json
from uuid import uuid4

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import dumps, now


class Methods:
    def __init__(self, activity):
        self.activity, self.store = activity, activity.store

    def create(self, scope, body, method_id=None):
        with self.store.write() as db:
            goal = self.activity.get("goals", body.source_goal_id, scope, db=db)
            if (
                goal["status"] != "completed"
                or not goal.get("acceptance_confirmed")
                or not goal["evidence_ids"]
            ):
                raise ServiceError("method_source", "方法必须来自人工验收且有产物的已完成目标", 409)
            artifact = self.activity.verify_artifact(
                scope, goal["evidence_ids"][-1], goal_id=goal["id"], db=db
            )
            if any(not s.strip() or len(s) > 2000 for s in body.steps):
                raise ServiceError("method_steps", "步骤须为非空且不超过 2000 字的文本")
            old = self.activity.get("research_methods", method_id, scope, db=db) if method_id else None
            if old and body.expected_version != old["version"]:
                raise ServiceError("version_conflict", "方法已变化，请刷新", 409)
            value = {
                "id": method_id or str(uuid4()),
                "title": body.title,
                "applicability": body.applicability,
                "limitations": body.limitations,
                "steps": body.steps,
                "source_goal_id": goal["id"],
                "source_artifact_id": artifact["id"],
                "source_artifact_hash": artifact["sha256"],
                "source_material_hash": artifact["material_hash"],
                "source_artifact_version": artifact["version"],
                "version": old["version"] + 1 if old else 1,
                "status": "candidate",
                "updated_at": now(),
                "sensitivity": goal["sensitivity"],
                "disclosure": "方法文本是建议，不给予执行权限",
            }
            self.activity.put(db, "research_methods", scope, value)
            db.execute(
                "INSERT INTO method_revisions VALUES(?,?,?)", (value["id"], value["version"], dumps(value))
            )
            self.store.audit(db, "method_revise" if old else "method_propose", "admin", value["id"])
        return value

    def _source(self, scope, method):
        goal = self.activity.get("goals", method["source_goal_id"], scope)
        if goal["status"] != "completed":
            raise ServiceError("method_source", "来源目标不再有效", 409)
        artifact = self.activity.verify_artifact(scope, method["source_artifact_id"], goal_id=goal["id"])
        if artifact["sha256"] != method["source_artifact_hash"]:
            raise ServiceError("method_source", "来源产物版本或哈希已变化", 409)
        return artifact

    def select(self, scope, method_id, goal_id):
        with self.store.write() as db:
            method = self.activity.get("research_methods", method_id, scope, db=db)
            goal = self.activity.get("goals", goal_id, scope, db=db)
            if method["status"] == "retired" or goal["status"] not in {"ready", "running"}:
                raise ServiceError("method_not_ready", "目标须已批准，方法须有效", 409)
            if goal_id == method["source_goal_id"]:
                raise ServiceError("method_self_reuse", "来源目标不能算作自身的复用成功", 409)
            self._source(scope, method)
            row = db.execute(
                "SELECT body FROM method_attempts WHERE user_id=? AND agent_id=? AND project_id=? AND json_extract(body,'$.goal_id')=?",
                (*scope.sql(), goal_id),
            ).fetchone()
            if row:
                old = json.loads(row[0])
                if old["method_id"] != method_id or old["method_version"] != method["version"]:
                    raise ServiceError("attempt_exists", "每个目标只能保留一次方法尝试", 409)
                return old
            value = {
                "id": str(uuid4()),
                "method_id": method_id,
                "method_version": method["version"],
                "goal_id": goal_id,
                "started_at": now(),
                "status": "pending",
                "reason": "",
                "baseline_artifact_version": len(goal["evidence_ids"]),
                "baseline_material_hashes": [
                    self.activity.verify_artifact(scope, aid, db=db)["material_hash"]
                    for aid in goal["evidence_ids"]
                ],
                "artifact_id": None,
                "sensitivity": "sensitive"
                if "sensitive" in {goal["sensitivity"], method["sensitivity"]}
                else "private",
            }
            self.activity.put(db, "method_attempts", scope, value)
            self.store.audit(db, "method_select", "admin", value["id"])
            return value

    def _validate_result(self, scope, attempt, method):
        self._source(scope, method)
        goal = self.activity.get("goals", attempt["goal_id"], scope)
        if goal["status"] != "completed" or not goal.get("acceptance_confirmed"):
            raise ServiceError("method_result", "目标尚未人工验收完成", 409)
        if goal["id"] == method["source_goal_id"] or not goal["evidence_ids"]:
            raise ServiceError("method_result", "缺少独立目标和新产物", 409)
        artifact_id = attempt["artifact_id"] or goal["evidence_ids"][-1]
        artifact = self.activity.verify_artifact(scope, artifact_id, goal_id=goal["id"])
        if (
            artifact["version"] <= attempt["baseline_artifact_version"]
            or artifact["created_at"] < attempt["started_at"]
            or artifact["material_hash"] == method["source_material_hash"]
            or artifact["material_hash"] in attempt["baseline_material_hashes"]
        ):
            raise ServiceError("method_result", "旧产物、重复材料或尝试前的结果不能算作复用成功", 409)
        return artifact

    def result(self, scope, attempt_id, success, reason):
        with self.store.write() as db:
            attempt = self.activity.get("method_attempts", attempt_id, scope, db=db)
            row = db.execute(
                "SELECT body FROM method_revisions WHERE id=? AND version=?",
                (attempt["method_id"], attempt["method_version"]),
            ).fetchone()
            method = json.loads(row[0])
            desired = "succeeded" if success else "failed"
            if attempt["status"] != "pending" and attempt["status"] != desired:
                raise ServiceError("attempt_recorded", "已有结果不能由重复声明覆盖", 409)
            if success:
                artifact = self._validate_result(scope, attempt, method)
                if attempt["status"] == desired:
                    return attempt
                attempt.update(artifact_id=artifact["id"], artifact_hash=artifact["sha256"])
            elif attempt["status"] == desired:
                return attempt
            attempt.update(status=desired, reason=reason, checked_at=now())
            self.activity.put(db, "method_attempts", scope, attempt)
            self.store.audit(db, "method_result", "admin", attempt_id, {"status": desired})
            return attempt

    def read(self, scope, method_id, *, include_sensitive=False):
        method = self.activity.get("research_methods", method_id, scope, include_sensitive=include_sensitive)
        with self.store.read() as db:
            rows = db.execute(
                "SELECT body FROM method_attempts WHERE user_id=? AND agent_id=? AND project_id=? AND json_extract(body,'$.method_id')=? AND json_extract(body,'$.method_version')=? AND json_extract(body,'$.status')='succeeded'",
                (*scope.sql(), method_id, method["version"]),
            ).fetchall()
        goals, invalid = set(), 0
        for row in rows:
            attempt = json.loads(row[0])
            try:
                artifact = self._validate_result(scope, attempt, method)
                if artifact["sha256"] != attempt["artifact_hash"]:
                    raise ServiceError("artifact_integrity", "产物已改变")
                goals.add(attempt["goal_id"])
            except ServiceError:
                invalid += 1
        state = "validated" if len(goals) >= 2 else "reused_once" if goals else "candidate"
        return {
            **method,
            "current_success_count": len(goals),
            "invalidated_attempts": invalid,
            "evidence_rechecked": True,
            "status": "retired" if method["status"] == "retired" else state,
        }
