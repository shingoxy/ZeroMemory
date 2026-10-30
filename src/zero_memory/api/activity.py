from typing import Literal

from fastapi import APIRouter, Depends, Query

from zero_memory.activity.models import (
    EpisodeInput,
    GoalInput,
    GoalPatch,
    InterestInput,
    MethodAttemptInput,
    MethodInput,
    MethodResultInput,
    MethodRevision,
    PersonaEvent,
    PersonaProposal,
    RunInput,
    SourceInput,
)
from zero_memory.activity.service import POLICY
from zero_memory.core.store import dumps
from zero_memory.schemas.models import IdentityPatch, Scope

RESOURCES = {
    "goals": "goals",
    "episodes": "episodes",
    "sources": "activity_sources",
    "artifacts": "activity_artifacts",
    "interests": "agent_interests",
    "methods": "research_methods",
    "method-attempts": "method_attempts",
    "persona-proposals": "persona_proposals",
}
Resource = Literal[
    "goals",
    "episodes",
    "sources",
    "artifacts",
    "interests",
    "methods",
    "method-attempts",
    "persona-proposals",
]


def activity_router(activity, auth):
    router = APIRouter(tags=["持续目标、经历与模拟状态"])

    def scope_of(body, p, write=False):
        scope = Scope(**{k: getattr(body, k) for k in Scope.model_fields})
        p.check(scope, write=write)
        return scope

    def selection(
        user_id: str | None = None,
        project_id: str | None = None,
        include_sensitive: bool = False,
        p=Depends(auth.principal),
    ):
        scope = Scope(user_id=user_id or p.scope.user_id, project_id=project_id or p.scope.project_id)
        p.check(scope)
        if include_sensitive:
            p.require_admin()
        return scope, p, include_sensitive

    def visible(table, ident, scope, p):
        return activity.get(table, ident, scope, include_sensitive=p.admin)

    @router.get("/v1/activity/policy")
    def policy(p=Depends(auth.principal)):
        return POLICY

    @router.get("/v1/activity/timeline")
    def timeline(
        goal_id: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=1_000_000),
        selected=Depends(selection),
    ):
        import json

        scope, p, include_sensitive = selected
        if goal_id:
            activity.get("goals", goal_id, scope, include_sensitive=include_sensitive)
        with activity.store.read() as db:
            rows = db.execute(
                "SELECT e.body FROM goal_events e JOIN goals g ON g.id=e.goal_id "
                "WHERE e.user_id=? AND e.agent_id=? AND e.project_id=?"
                + (" AND e.goal_id=?" if goal_id else "")
                + (" AND json_extract(g.body,'$.sensitivity')!='sensitive'" if not include_sensitive else "")
                + " ORDER BY e.rowid DESC LIMIT ? OFFSET ?",
                (*scope.sql(), *((goal_id,) if goal_id else ()), limit, offset),
            ).fetchall()
        return {"items": [json.loads(r[0]) for r in rows], "offset": offset}

    @router.get("/v1/activity/{resource}")
    def list_resources(
        resource: Resource,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=1_000_000),
        selected=Depends(selection),
    ):
        scope, p, include_sensitive = selected
        if resource in {"sources", "persona-proposals"}:
            p.require_admin()
        values = activity.list(
            RESOURCES[resource], scope, limit=limit, offset=offset, include_sensitive=include_sensitive
        )
        if resource == "methods":
            values = [
                activity.methods.read(scope, m["id"], include_sensitive=include_sensitive) for m in values
            ]
        return {"items": values, "offset": offset, "limit": limit}

    @router.get("/v1/activity/{resource}/{ident}")
    def detail(resource: Resource, ident: str, selected=Depends(selection)):
        scope, p, include_sensitive = selected
        if resource in {"sources", "persona-proposals"}:
            p.require_admin()
        if resource == "methods":
            return activity.methods.read(scope, ident, include_sensitive=include_sensitive)
        return activity.get(RESOURCES[resource], ident, scope, include_sensitive=include_sensitive)

    @router.post("/v1/activity/goals")
    def propose_goal(body: GoalInput, p=Depends(auth.principal)):
        return activity.propose_goal(scope_of(body, p, True), body, p.name, p.admin)

    @router.get("/v1/activity/artifacts/{ident}/content")
    def artifact_content(ident: str, selected=Depends(selection)):
        import json

        scope, p, include_sensitive = selected
        p.require_admin()
        activity.get("activity_artifacts", ident, scope, include_sensitive=include_sensitive)
        artifact = activity.verify_artifact(scope, ident)
        path = activity.store.home / "activity_artifacts" / (artifact["sha256"] + ".json")
        return {"artifact": artifact, "payload": json.loads(path.read_text(encoding="utf-8"))}

    @router.patch("/v1/activity/goals/{ident}")
    def update_goal(ident: str, body: GoalPatch, p=Depends(auth.principal)):
        scope = scope_of(body, p, True)
        visible("goals", ident, scope, p)
        return activity.update_goal(scope, ident, body, admin=p.admin)

    @router.post("/v1/activity/goals/{ident}/run")
    def run_goal(ident: str, body: RunInput, p=Depends(auth.principal)):
        p.require_admin()
        return activity.run_once(scope_of(body, p, True), ident, body)

    @router.post("/v1/activity/sources")
    def add_source(body: SourceInput, p=Depends(auth.principal)):
        p.require_admin()
        return activity.source(scope_of(body, p, True), body)

    @router.post("/v1/activity/episodes")
    def record_episode(body: EpisodeInput, p=Depends(auth.principal)):
        scope = scope_of(body, p, True)
        visible("goals", body.goal_id, scope, p)
        return activity.record_episode(scope, body, p.name)

    @router.post("/v1/activity/episodes/{ident}/propose-memory")
    def propose_memory(ident: str, body: Scope, p=Depends(auth.principal)):
        scope = scope_of(body, p, True)
        visible("episodes", ident, scope, p)
        return activity.propose_memory(scope, ident)

    @router.post("/v1/activity/interests")
    def propose_interest(body: InterestInput, p=Depends(auth.principal)):
        scope = scope_of(body, p, True)
        for ident in body.evidence_episode_ids:
            visible("episodes", ident, scope, p)
        return activity.interest(scope, body)

    @router.post("/v1/activity/interests/{ident}/approve")
    def approve_interest(ident: str, body: Scope, p=Depends(auth.principal)):
        p.require_admin()
        scope = scope_of(body, p, True)
        with activity.store.write() as db:
            value = activity.get("agent_interests", ident, scope, db=db)
            for episode_id in value["evidence_episode_ids"]:
                episode = activity.get("episodes", episode_id, scope, db=db)
                for artifact_id in episode["evidence_ids"]:
                    activity.verify_artifact(scope, artifact_id, db=db)
            value["status"] = "approved"
            activity.put(db, "agent_interests", scope, value)
            activity.store.audit(db, "interest_approve", "admin", ident)
        return value

    @router.post("/v1/activity/methods")
    def create_method(body: MethodInput, p=Depends(auth.principal)):
        p.require_admin()
        return activity.methods.create(scope_of(body, p, True), body)

    @router.patch("/v1/activity/methods/{ident}")
    def revise_method(ident: str, body: MethodRevision, p=Depends(auth.principal)):
        p.require_admin()
        return activity.methods.create(scope_of(body, p, True), body, ident)

    @router.post("/v1/activity/methods/{ident}/retire")
    def retire_method(ident: str, body: Scope, p=Depends(auth.principal)):
        p.require_admin()
        scope = scope_of(body, p, True)
        with activity.store.write() as db:
            value = activity.get("research_methods", ident, scope, db=db)
            if value["status"] == "retired":
                return value
            value.update(status="retired", version=value["version"] + 1)
            activity.put(db, "research_methods", scope, value)
            db.execute("INSERT INTO method_revisions VALUES(?,?,?)", (ident, value["version"], dumps(value)))
            activity.store.audit(db, "method_retire", "admin", ident)
        return value

    @router.get("/v1/methods/{ident}/versions")
    def method_versions(ident: str, selected=Depends(selection)):
        import json

        scope, p, include_sensitive = selected
        activity.get("research_methods", ident, scope, include_sensitive=include_sensitive)
        with activity.store.read() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM method_revisions WHERE id=? ORDER BY version DESC", (ident,)
                )
            ]

    @router.post("/v1/activity/methods/{ident}/select")
    def select_method(ident: str, body: MethodAttemptInput, p=Depends(auth.principal)):
        p.require_admin()
        return activity.methods.select(scope_of(body, p, True), ident, body.goal_id)

    @router.post("/v1/activity/method-attempts/{ident}/result")
    def method_result(ident: str, body: MethodResultInput, p=Depends(auth.principal)):
        p.require_admin()
        return activity.methods.result(scope_of(body, p, True), ident, body.success, body.reason)

    @router.post("/v1/persona/events")
    def persona_event(body: PersonaEvent, p=Depends(auth.principal)):
        p.require_admin()
        return activity.persona.advance(
            scope_of(body, p, True), body.event, body.request_id, body.expected_version, body.reason
        )

    @router.get("/v1/persona/events")
    def persona_history(limit: int = Query(50, ge=1, le=200), selected=Depends(selection)):
        import json

        scope, p, _ = selected
        p.require_admin()
        with activity.store.read() as db:
            return {
                "items": [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT body FROM persona_events WHERE user_id=? AND agent_id=? AND project_id=? ORDER BY rowid DESC LIMIT ?",
                        (*scope.sql(), limit),
                    )
                ]
            }

    @router.post("/v1/persona/proposals")
    def propose_persona(body: PersonaProposal, p=Depends(auth.principal)):
        return activity.persona.propose(scope_of(body, p, True), body, p.name)

    @router.post("/v1/persona/proposals/{ident}/approve")
    def approve_persona(ident: str, body: IdentityPatch, selected=Depends(selection)):
        scope, p, _ = selected
        p.require_admin()
        return activity.persona.review(scope, ident, body)

    @router.post("/v1/persona/proposals/{ident}/reject")
    def reject_persona(ident: str, body: Scope, p=Depends(auth.principal)):
        p.require_admin()
        return activity.persona.review(scope_of(body, p, True), ident, None)

    return router
