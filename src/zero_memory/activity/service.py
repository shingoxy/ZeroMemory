"""Durable local goals and receipts. Model statements cannot verify their own actions."""

import json
from uuid import uuid4

from zero_memory.activity.methods import Methods
from zero_memory.activity.migrations import ENTITY_TABLES, migrate
from zero_memory.activity.persona import Persona
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import atomic_json, digest, dumps, now
from zero_memory.importers.chatgpt import timestamp
from zero_memory.schemas.models import Proposal, Search

POLICY = {
    "autonomy_enabled": False,
    "network_actions_enabled": False,
    "external_writes_enabled": False,
    "scheduler_implemented": False,
    "manual_runs_per_day": 100,
    "model_calls": 0,
    "cost_limit": 0,
    "available_actions": ["memory_review", "source_review"],
}
TRANSITIONS = {
    "proposed": {"ready", "cancelled", "waiting_approval"},
    "ready": {"running", "paused", "cancelled", "blocked"},
    "running": {"paused", "blocked", "waiting_approval", "completed", "cancelled"},
    "paused": {"ready", "cancelled", "blocked", "completed"},
    "blocked": {"ready", "paused", "cancelled"},
    "waiting_approval": {"ready", "cancelled", "paused"},
    "completed": set(),
    "cancelled": set(),
}


class Activity:
    def __init__(self, store, backend, identity):
        self.store, self.backend = store, backend
        migrate(store)
        self.persona = Persona(store, identity)
        self.methods = Methods(self)
        (store.home / "activity_artifacts").mkdir(exist_ok=True)

    def get(self, table, ident, scope, *, include_sensitive=True, db=None):
        assert table in ENTITY_TABLES
        if db is None:
            with self.store.read() as connection:
                return self.get(table, ident, scope, include_sensitive=include_sensitive, db=connection)
        row = db.execute(
            f"SELECT body FROM {table} WHERE id=? AND user_id=? AND agent_id=? AND project_id=?",
            (ident, *scope.sql()),
        ).fetchone()
        if not row:
            raise ServiceError("not_found", "记录不在当前作用域", 404)
        value = json.loads(row[0])
        if not include_sensitive and value.get("sensitivity") == "sensitive":
            raise ServiceError("not_found", "记录不在当前可见范围", 404)
        return value

    def put(self, db, table, scope, value):
        assert table in ENTITY_TABLES
        db.execute(
            f"INSERT OR REPLACE INTO {table} VALUES(?,?,?,?,?)", (value["id"], *scope.sql(), dumps(value))
        )

    def list(self, table, scope, *, limit=50, offset=0, include_sensitive=False):
        assert table in ENTITY_TABLES
        with self.store.read() as db:
            rows = db.execute(
                f"SELECT body FROM {table} WHERE user_id=? AND agent_id=? AND project_id=?"
                + (
                    " AND COALESCE(json_extract(body,'$.sensitivity'),'private')!='sensitive'"
                    if not include_sensitive
                    else ""
                )
                + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (*scope.sql(), limit, offset),
            )
            return [json.loads(r[0]) for r in rows]

    def _event(self, db, scope, goal, reason, source):
        event = {
            "time": now(),
            "status": goal["status"],
            "version": goal["version"],
            "reason": reason,
            "source": source,
            "checkpoint": goal["checkpoint"],
        }
        db.execute(
            "INSERT INTO goal_events VALUES(?,?,?,?,?,?)",
            (str(uuid4()), *scope.sql(), goal["id"], dumps(event)),
        )
        self.store.audit(
            db, "goal_update", source, goal["id"], {"status": goal["status"], "version": goal["version"]}
        )

    def propose_goal(self, scope, body, actor, admin):
        value = {
            **body.model_dump(exclude=set(type(scope).model_fields)),
            "id": str(uuid4()),
            **scope.model_dump(),
            "status": "proposed",
            "version": 1,
            "checkpoint": {},
            "evidence_ids": [],
            "created_at": now(),
            "updated_at": now(),
            "approved_by_human": False,
        }
        value["due_at"] = timestamp(body.due_at)
        if not admin:
            value["owner_kind"] = "agent"
        if any(not x.strip() or len(x) > 1000 for x in value["acceptance_criteria"]):
            raise ServiceError("criteria", "完成标准必须为非空且不超过 1000 字的条目")
        with self.store.write() as db:
            self.put(db, "goals", scope, value)
            self._event(db, scope, value, body.why, actor)
        return value

    def update_goal(self, scope, ident, patch, *, admin):
        with self.store.write() as db:
            goal = self.get("goals", ident, scope, db=db)
            if patch.expected_version != goal["version"]:
                raise ServiceError("version_conflict", "目标已变化，请刷新", 409)
            status = patch.status or goal["status"]
            if not admin and status not in {goal["status"], "paused", "blocked", "waiting_approval"}:
                raise ServiceError("goal_approval", "客户端不能批准、启动或完成目标", 403)
            if status != goal["status"] and status not in TRANSITIONS[goal["status"]]:
                raise ServiceError("goal_transition", "不允许的目标状态转换", 409)
            if status == "running":
                raise ServiceError("manual_run_required", "请使用手动运行一次入口；状态修改不代表执行", 409)
            if status == "completed":
                if not admin or not patch.acceptance_confirmed or not goal["evidence_ids"]:
                    raise ServiceError("goal_evidence", "完成须由用户核对标准并确认真实产物", 409)
                for artifact_id in goal["evidence_ids"]:
                    self.verify_artifact(scope, artifact_id, goal_id=ident, db=db)
                goal["acceptance_confirmed"] = True
            if status == "ready":
                if not admin:
                    raise ServiceError("goal_approval", "须由本机用户批准目标", 403)
                goal["approved_by_human"] = True
            if len(dumps(patch.checkpoint)) > 16000:
                raise ServiceError("checkpoint_limit", "检查点过大")
            goal.update(status=status, updated_at=now(), version=goal["version"] + 1)
            if patch.next_step is not None:
                goal["next_step"] = patch.next_step
            if patch.checkpoint:
                goal["checkpoint"] = patch.checkpoint
            self.put(db, "goals", scope, goal)
            self._event(db, scope, goal, patch.reason, "admin" if admin else "client")
            return goal

    def source(self, scope, body):
        material = digest(body.text)
        ident = digest(dumps([*scope.sql(), body.source_id, material]))
        value = {
            "id": ident,
            "label": body.label,
            "text": body.text,
            "source_id": body.source_id,
            "material_hash": material,
            "created_at": now(),
            "origin": "user_supplied_text",
            "disclosure": "只核验本地内容；不证明所填网站地址的真实性",
        }
        with self.store.write() as db:
            existing = db.execute("SELECT body FROM activity_sources WHERE id=?", (ident,)).fetchone()
            if existing:
                return json.loads(existing[0])
            self.put(db, "activity_sources", scope, value)
            self.store.audit(db, "activity_source", "admin", ident)
        return value

    def record_episode(self, scope, body, actor):
        with self.store.write() as db:
            goal = self.get("goals", body.goal_id, scope, db=db)
            ident = digest(dumps([*scope.sql(), "reported", body.source_id]))
            value = {
                "id": ident,
                "goal_id": body.goal_id,
                "run_id": None,
                "occurred_at": timestamp(body.occurred_at),
                "recorded_at": now(),
                "kind": body.kind,
                "subject_kind": "agent",
                "subject_id": "zero-001",
                "source_type": "reported",
                "source_id": body.source_id,
                "observations": body.observations,
                "interpretation": body.interpretation,
                "status": "unverified",
                "executed": False,
                "evidence_ids": [],
                "origin_key": ident,
                "supersedes_id": None,
                "sensitivity": goal["sensitivity"],
                "actor": actor,
            }
            old = db.execute("SELECT body FROM episodes WHERE id=?", (ident,)).fetchone()
            if old:
                old = json.loads(old[0])
                if any(
                    old[k] != value[k]
                    for k in ("goal_id", "kind", "observations", "interpretation", "occurred_at")
                ):
                    raise ServiceError(
                        "idempotency_conflict", "相同来源的事件内容已变化，须使用新事件 ID", 409
                    )
                return old
            self.put(db, "episodes", scope, value)
            self.store.audit(db, "episode_reported", actor, ident, {"verified": False})
            return value

    def verify_artifact(self, scope, ident, *, goal_id=None, db=None):
        artifact = self.get("activity_artifacts", ident, scope, db=db)
        if goal_id and artifact["goal_id"] != goal_id:
            raise ServiceError("artifact_goal", "产物不属于此目标", 409)
        checksum = artifact["sha256"]
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ServiceError("artifact_hash", "产物校验信息无效", 409)
        path = self.store.home / "activity_artifacts" / (checksum + ".json")
        if not path.exists() or digest(path.read_bytes()) != checksum:
            raise ServiceError("artifact_integrity", "产物缺失或已变化，不能用作成功证据", 409)
        if artifact["source_type"] != "local_read_tool":
            raise ServiceError("artifact_unverified", "产物没有可信本地读取回执", 409)
        return artifact

    def run_once(self, scope, goal_id, request):
        request_hash = digest(dumps(request.model_dump()))
        run_id = digest(dumps([*scope.sql(), request.request_id]))
        with self.store.write() as db:
            old = db.execute("SELECT body FROM activity_runs WHERE id=?", (run_id,)).fetchone()
            if old:
                value = json.loads(old[0])
                if value["request_hash"] != request_hash or value["goal_id"] != goal_id:
                    raise ServiceError("idempotency_conflict", "同一运行键对应的操作已变化", 409)
                return value
            goal = self.get("goals", goal_id, scope, db=db)
            if goal["status"] != "ready" or not goal["approved_by_human"]:
                raise ServiceError("goal_not_ready", "须先人工批准或恢复目标，再手动运行", 409)
            day = now()[:10]
            count = db.execute(
                "SELECT count(*) FROM activity_runs WHERE user_id=? AND agent_id=? AND project_id=? AND substr(json_extract(body,'$.started_at'),1,10)=?",
                (*scope.sql(), day),
            ).fetchone()[0]
            if count >= POLICY["manual_runs_per_day"]:
                raise ServiceError("activity_budget", "本日手动运行预算已用完", 409)
            value = {
                "id": run_id,
                "goal_id": goal_id,
                "request_hash": request_hash,
                "started_at": now(),
                "status": "running",
                "action": request.action,
                "artifact_id": None,
                "episode_id": None,
            }
            db.execute(
                "INSERT INTO activity_runs VALUES(?,?,?,?,?,?,?)",
                (run_id, *scope.sql(), goal_id, request.request_id, dumps(value)),
            )
            goal.update(status="running", version=goal["version"] + 1, updated_at=now())
            self.put(db, "goals", scope, goal)
            self._event(db, scope, goal, "用户手动触发限定的本地只读步骤", "admin")
            running_version = goal["version"]
        try:
            if request.action == "memory_review":
                rows = self.backend.search(
                    Search(
                        **scope.model_dump(),
                        query=request.query,
                        status=None if request.include_pending else "approved",
                        exclude_sensitive=True,
                        limit=100,
                    )
                )
                observations = [
                    {k: m.get(k) for k in ("memory_id", "content", "status", "confidence", "origin_quality")}
                    for m in rows
                ]
                material = digest(dumps(observations))
                summary = f"本地只读检索返回 {len(rows)} 条授权记忆；未改变审核状态，也未核验摘要语义。"
                source_id = "memory-query:" + digest(
                    dumps([*scope.sql(), request.query, request.include_pending])
                )
            else:
                source = self.get("activity_sources", request.source_id, scope)
                if digest(source["text"]) != source["material_hash"]:
                    raise ServiceError("source_integrity", "本地资料校验失败")
                material = source["material_hash"]
                source_id = source["id"]
                observations = {
                    "label": source["label"],
                    "source_id": source["source_id"],
                    "text": source["text"][:4000],
                    "truncated": len(source["text"]) > 4000,
                    "disclosure": source["disclosure"],
                }
                summary = "已实际读取并校验用户提供的本地资料；不等于访问或验证了外部网站。"
            payload = {
                "run_id": run_id,
                "goal_id": goal_id,
                "observations": observations,
                "material_hash": material,
                "summary": summary,
                "occurred_at": now(),
            }
            checksum = digest(dumps(payload))
            with self.store.write() as db:
                current = self.get("goals", goal_id, scope, db=db)
                if current["status"] != "running" or current["version"] != running_version:
                    raise ServiceError("goal_interrupted", "目标已停止或改变，结果不写成成功经历", 409)
                atomic_json(self.store.home / "activity_artifacts" / (checksum + ".json"), payload)
                artifact_id, episode_id = str(uuid4()), str(uuid4())
                artifact = {
                    "id": artifact_id,
                    "goal_id": goal_id,
                    "run_id": run_id,
                    "version": len(goal["evidence_ids"]) + 1,
                    "sha256": checksum,
                    "material_hash": material,
                    "source_type": "local_read_tool",
                    "summary": summary,
                    "created_at": now(),
                    "sensitivity": goal["sensitivity"],
                }
                episode = {
                    "id": episode_id,
                    "goal_id": goal_id,
                    "run_id": run_id,
                    "occurred_at": payload["occurred_at"],
                    "recorded_at": now(),
                    "kind": "local_read",
                    "subject_kind": "agent",
                    "subject_id": "zero-001",
                    "source_type": "local_read_tool",
                    "source_id": run_id,
                    "origin_key": digest(dumps([source_id, material])),
                    "evidence_ids": [artifact_id],
                    "observations": summary,
                    "interpretation": "",
                    "status": "verified",
                    "executed": True,
                    "supersedes_id": None,
                    "sensitivity": goal["sensitivity"],
                }
                self.put(db, "activity_artifacts", scope, artifact)
                self.put(db, "episodes", scope, episode)
                goal.update(
                    status="paused",
                    version=goal["version"] + 1,
                    updated_at=now(),
                    evidence_ids=[*goal["evidence_ids"], artifact_id],
                    checkpoint={
                        "last_run_id": run_id,
                        "last_artifact_id": artifact_id,
                        "next": "人工检查结果后恢复或确认完成；不自动重复执行",
                    },
                )
                self.put(db, "goals", scope, goal)
                self._event(db, scope, goal, "只读步骤已执行，保存检查点等待用户", "local_read_tool")
                value.update(
                    status="completed", artifact_id=artifact_id, episode_id=episode_id, finished_at=now()
                )
                db.execute("UPDATE activity_runs SET body=? WHERE id=?", (dumps(value), run_id))
                return value
        except Exception as exc:
            with self.store.write() as db:
                current = self.get("goals", goal_id, scope, db=db)
                if current["status"] == "running" and current["version"] == running_version:
                    current.update(status="blocked", version=current["version"] + 1, updated_at=now())
                    self.put(db, "goals", scope, current)
                    self._event(db, scope, current, "本地步骤失败，需人工检查", "service")
                value.update(
                    status="failed", error=getattr(exc, "code", "local_read_failed"), finished_at=now()
                )
                db.execute("UPDATE activity_runs SET body=? WHERE id=?", (dumps(value), run_id))
            raise

    def recover(self):
        with self.store.write() as db:
            for row in db.execute(
                "SELECT * FROM goals WHERE json_extract(body,'$.status')='running'"
            ).fetchall():
                from zero_memory.schemas.models import Scope

                scope = Scope(**{k: row[k] for k in Scope.model_fields})
                goal = json.loads(row["body"])
                goal.update(status="paused", version=goal["version"] + 1, updated_at=now())
                self.put(db, "goals", scope, goal)
                self._event(db, scope, goal, "重启中断：保留检查点，必须重新检查授权后手动恢复", "recovery")
            for row in db.execute(
                "SELECT id,body FROM activity_runs WHERE json_extract(body,'$.status')='running'"
            ).fetchall():
                value = json.loads(row["body"])
                value.update(status="interrupted", finished_at=now(), error="restart_no_replay")
                db.execute("UPDATE activity_runs SET body=? WHERE id=?", (dumps(value), row["id"]))

    def interest(self, scope, body):
        with self.store.write() as db:
            topic = body.topic.strip().casefold()
            ident = digest(dumps([*scope.sql(), "agent", topic]))
            old = db.execute("SELECT body FROM agent_interests WHERE id=?", (ident,)).fetchone()
            old = json.loads(old[0]) if old else {"evidence_episode_ids": []}
            ids = list(dict.fromkeys(old["evidence_episode_ids"] + body.evidence_episode_ids))
            origins = set()
            sensitivity = "private"
            for episode_id in ids:
                episode = self.get("episodes", episode_id, scope, db=db)
                if episode["status"] != "verified" or not episode["executed"]:
                    raise ServiceError("interest_evidence", "自身兴趣只能引用实际执行且有回执的经历", 409)
                for artifact in episode["evidence_ids"]:
                    self.verify_artifact(scope, artifact, db=db)
                origins.add(episode["origin_key"])
                if episode["sensitivity"] == "sensitive":
                    sensitivity = "sensitive"
            if old.get("id") and old["independent_sources"] == len(origins):
                # Repeating a known source is not a new preference or a reason to reset review.
                return old
            value = {
                "id": ident,
                "topic": topic,
                "owner_kind": "agent",
                "evidence_episode_ids": ids,
                "independent_sources": len(origins),
                "interest_level": min(100, 20 * len(origins)),
                "confidence": min(0.95, 0.5 + 0.1 * len(origins)),
                "status": "pending_review",
                "updated_at": now(),
                "sensitivity": sensitivity,
            }
            self.put(db, "agent_interests", scope, value)
            self.store.audit(db, "interest_propose", "client", ident)
            return value

    def propose_memory(self, scope, episode_id):
        episode = self.get("episodes", episode_id, scope)
        if episode["status"] != "verified" or not episode["executed"]:
            raise ServiceError("episode_unverified", "未核实的经历不能提炼为已执行经验", 409)
        artifacts = [
            self.verify_artifact(scope, aid, goal_id=episode["goal_id"]) for aid in episode["evidence_ids"]
        ]
        goal = self.get("goals", episode["goal_id"], scope)
        return self.backend.propose(
            Proposal(
                memory_type="agent_experience",
                source_type="agent_activity",
                source_role="tool",
                inference_type="system_summary",
                content=f"目标“{goal['title']}”的本地步骤：{episode['observations']}",
                evidence=artifacts[0]["summary"],
                sensitivity=episode["sensitivity"],
                event_time=episode["occurred_at"],
                activity_goal_id=episode["goal_id"],
                activity_episode_ids=[episode_id],
                activity_artifact_ids=episode["evidence_ids"],
            ),
            scope,
            "activity_summary",
        )

    def context(self, scope):
        return {
            "goals": [
                {k: g[k] for k in ("id", "title", "status", "next_step", "version")}
                for g in self.list("goals", scope, limit=5)
            ],
            "episodes": [
                {k: e[k] for k in ("id", "goal_id", "status", "executed", "observations")}
                for e in self.list("episodes", scope, limit=3)
            ],
            "policy": POLICY,
            "notice": "目标是操作状态；未核实经历不是已完成事实。",
        }
