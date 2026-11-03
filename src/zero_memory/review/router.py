"""审核接口：只允许本机 Web 管理会话访问，客户端令牌和 MCP 一律 403。"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from zero_memory.review import links as review_links
from zero_memory.review import queue as review_queue
from zero_memory.schemas.models import MemoryType, Scope

RUN_ID = r"^[0-9a-f]{64}$"


def as_scope(value) -> Scope:
    return Scope(**{k: getattr(value, k) for k in Scope.model_fields})


class DeferRequest(Scope):
    memory_ids: list[str] = Field(min_length=1, max_length=200)
    undo: bool = False


class LinkDecision(Scope):
    decision: Literal["confirm", "dismiss"]


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
