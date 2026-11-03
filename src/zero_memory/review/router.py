"""审核接口：只允许本机 Web 管理会话访问，客户端令牌和 MCP 一律 403。"""

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import Field

from zero_memory.review import batch as review_batch
from zero_memory.review import links as review_links
from zero_memory.review import queue as review_queue
from zero_memory.review import revoke as review_revoke
from zero_memory.review import sensitive as review_sensitive
from zero_memory.review import stats as review_stats
from zero_memory.schemas.models import MemoryType, Scope

RUN_ID = r"^[0-9a-f]{64}$"


def as_scope(value) -> Scope:
    return Scope(**{k: getattr(value, k) for k in Scope.model_fields})


class DeferRequest(Scope):
    memory_ids: list[str] = Field(min_length=1, max_length=200)
    undo: bool = False


class LinkDecision(Scope):
    decision: Literal["confirm", "dismiss"]


class RevokeRequest(Scope):
    confirm: str | None = Field(default=None, max_length=200)


class SensitiveAction(Scope):
    action: Literal["keep_sensitive", "to_private", "reject", "defer"]


def review_router(store, backend, web_admin):
    router = APIRouter(prefix="/v1/review", dependencies=[Depends(web_admin)])

    @router.get("/queue")
    def queue(user_id: str = "default-user", project_id: str = "default",
              view: Literal["active", "deferred", "low_priority", "sensitive"] = "active",
              memory_type: MemoryType | None = None, source_type: str | None = Query(None, max_length=100),
              run_id: str | None = Query(None, pattern=RUN_ID), threshold: float = Query(0.5, ge=0, le=1),
              limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        scope = Scope(user_id=user_id, project_id=project_id)
        return review_queue.queue(store, scope, view=view, memory_type=memory_type, source_type=source_type,
                                  run_id=run_id, threshold=threshold, limit=limit, offset=offset)

    @router.post("/defer")
    def defer(body: DeferRequest):
        return review_queue.defer(store, as_scope(body), body.memory_ids, "admin", undo=body.undo)

    @router.post("/batch/preview")
    def batch_preview(body: review_batch.BatchRequest):
        return review_batch.preview_batch(store, backend, as_scope(body), body)

    @router.post("/batch/apply")
    def batch_apply(body: review_batch.BatchApply):
        return review_batch.apply_batch(store, backend, as_scope(body), body, body.confirm)

    @router.get("/runs")
    def runs(user_id: str = "default-user", project_id: str = "default"):
        return review_stats.run_stats(store, Scope(user_id=user_id, project_id=project_id))

    @router.post("/runs/{run_id}/revoke")
    def revoke_run(body: RevokeRequest, run_id: str = Path(pattern=RUN_ID)):
        return review_revoke.revoke_run(store, backend, as_scope(body), run_id, body.confirm)

    @router.get("/sensitive")
    def sensitive(user_id: str = "default-user", project_id: str = "default"):
        return review_sensitive.sensitive_items(store, Scope(user_id=user_id, project_id=project_id))

    @router.post("/sensitive/{item_id}")
    def sensitive_action(body: SensitiveAction, item_id: str = Path(pattern=RUN_ID)):
        return review_sensitive.act(store, backend, as_scope(body), item_id, body.action)

    @router.post("/links/suggest")
    def suggest_links(body: Scope):
        return review_links.suggest_links(store, body)

    @router.get("/links")
    def links(memory_id: str = Query(max_length=100), user_id: str = "default-user", project_id: str = "default"):
        return review_links.links_for(store, Scope(user_id=user_id, project_id=project_id), memory_id)

    @router.post("/links/{link_id}")
    def decide_link(link_id: str, body: LinkDecision):
        return review_links.decide_link(store, as_scope(body), link_id, body.decision)

    return router
