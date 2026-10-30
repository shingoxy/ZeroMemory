from typing import Literal

from fastapi import APIRouter, Depends, Query

from zero_memory.observatory.service import ObservatoryQuery
from zero_memory.schemas.models import MemoryType, Status


def observatory_router(adapter, auth):
    router = APIRouter(prefix="/v1/observatory", tags=["记忆星海（只读）"])

    def selection(
        user_id: str | None = None,
        project_id: str | None = None,
        agent_id: Literal["zero-001"] = "zero-001",
        query: str = Query("", max_length=500),
        limit: int = Query(100, ge=1, le=500),
        cursor: int = Query(0, ge=0, le=1_000_000),
        memory_type: MemoryType | None = None,
        status: Status | None = None,
        include_sensitive: bool = False,
        since: str | None = None,
        until: str | None = None,
        p=Depends(auth.principal),
    ):
        value = ObservatoryQuery(
            user_id=user_id or p.scope.user_id,
            project_id=project_id or p.scope.project_id,
            agent_id=agent_id,
            query=query,
            limit=limit,
            cursor=cursor,
            memory_type=memory_type,
            status=status,
            include_sensitive=include_sensitive,
            since=since,
            until=until,
        )
        return value, p

    @router.get("/snapshot")
    def snapshot(selected=Depends(selection)):
        return adapter.snapshot(*selected)

    @router.get("/timeline")
    def timeline(selected=Depends(selection)):
        result = adapter.snapshot(*selected)
        result["memories"].sort(key=lambda m: (m["timeline_at"], m["id"]))
        result["timeline_scope"] = "当前授权分页，不代表全库完整时间线"
        return result

    @router.get("/memories/{memory_id}")
    def detail(memory_id: str, selected=Depends(selection)):
        return adapter.detail(memory_id, *selected)

    @router.post("/search")
    def search(body: ObservatoryQuery, p=Depends(auth.principal)):
        return adapter.snapshot(body, p)

    return router
