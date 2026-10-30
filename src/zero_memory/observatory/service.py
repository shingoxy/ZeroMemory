"""Permission-filtered, bounded memory views. All projections are read-only."""

from collections import defaultdict
from datetime import datetime

from pydantic import Field

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import now
from zero_memory.importers.chatgpt import timestamp
from zero_memory.memory.base import MemoryBackend
from zero_memory.schemas.models import MemoryType, Scope, Search, Status

KINDS = {
    "episodic": "episode",
    "agent_experience": "episode",
    "user_profile": "preference",
    "semantic": "knowledge",
    "procedural": "method",
    "relationship": "relation",
    "working_context": "plan",
}
HALF_LIVES = {"episode": 30, "preference": 120, "knowledge": 90, "method": 180, "relation": 120, "plan": 30}


class ObservatoryQuery(Scope):
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=100, ge=1, le=500)
    cursor: int = Field(default=0, ge=0, le=1_000_000)
    memory_type: MemoryType | None = None
    status: Status | None = None
    include_sensitive: bool = False
    since: str | None = None
    until: str | None = None


class MemoryObservatoryAdapter:
    def __init__(self, backend: MemoryBackend):
        self.backend = backend

    @staticmethod
    def authorize(query, principal):
        scope = Scope(**{k: getattr(query, k) for k in Scope.model_fields})
        principal.check(scope)
        if not principal.admin and query.status not in {None, "approved"}:
            raise ServiceError("review_private", "客户端只能读取已批准记忆", 403)
        if query.include_sensitive and not principal.admin:
            raise ServiceError("sensitive_private", "仅本机管理员可明确查看敏感记忆", 403)
        return scope

    def snapshot(self, query: ObservatoryQuery, principal):
        scope = self.authorize(query, principal)
        status = query.status if principal.admin else "approved"
        since, until = timestamp(query.since), timestamp(query.until)
        if since and until and since > until:
            raise ServiceError("time_range", "起始时间不能晚于结束时间")
        records = []
        # Never scan the whole corpus: at most 501 records, in backend-supported pages.
        while len(records) < query.limit + 1:
            requested = min(200, query.limit + 1 - len(records))
            batch = self.backend.search(
                Search(
                    **scope.model_dump(),
                    query=query.query,
                    memory_type=query.memory_type,
                    status=status,
                    since=since,
                    until=until,
                    exclude_sensitive=not query.include_sensitive,
                    offset=query.cursor + len(records),
                    limit=requested,
                )
            )
            records.extend(batch)
            if len(batch) < requested:
                break
        has_more = len(records) > query.limit
        records = records[: query.limit]
        as_of = now()
        scope_name = "/".join(scope.sql())
        memories = []
        for m in records:
            kind = "plan" if m.get("claim_kind") == "user_plan" else KINDS[m["memory_type"]]
            memories.append(
                {
                    "id": m["memory_id"],
                    "memory_id": m["memory_id"],
                    **scope.model_dump(),
                    "scope": scope_name,
                    "title": m["content"][:72],
                    "text": m["content"][:4000],
                    "gist": m["content"][:500],
                    "truncated": len(m["content"]) > 4000,
                    "kind": kind,
                    "memory_type": m["memory_type"],
                    "status": m["status"],
                    "sensitivity": m["sensitivity"],
                    "confidence": m["confidence"],
                    "source_type": m["source_type"],
                    "source_role": m["source_role"],
                    "source_message_ids": m["source_message_ids"],
                    "source_message_keys": m["source_message_keys"],
                    "source_conversation_id": m["source_conversation_id"],
                    "source_artifact": m.get("source_artifact"),
                    "evidence": m["evidence"][:1500],
                    "evidence_verified": m["evidence_verified"],
                    "source_verification": "原文已定位；语义仍需审核"
                    if m["evidence_verified"]
                    else "来源待核实；摘要不是逐字证据",
                    "claim_kind": m.get("claim_kind"),
                    "origin_quality": m.get("origin_quality"),
                    "topic": m.get("topic"),
                    "created_at": m["extracted_at"],
                    "event_time": m["event_time"],
                    "event_time_original": m.get("event_time_original"),
                    "event_time_precision": m.get("event_time_precision"),
                    "valid_from": m["valid_from"],
                    "valid_to": m["valid_to"],
                    "version": m["version"],
                    "timeline_at": m["event_time"] or m["extracted_at"],
                    "time_basis": "event_time" if m["event_time"] else "extracted_at_not_event",
                    "half_life_days": HALF_LIVES[kind],
                    "stability_assumed": True,
                    "match_reason": "关键词字面匹配" if query.query.strip() else "授权作用域内分页浏览",
                }
            )
        return {
            "schema": "memory-observatory/v1",
            "extension": "zeromemory/v0.2",
            "demo": False,
            "label": "零的记忆星海 · 真实数据 · 只读",
            "as_of": as_of,
            "scope": scope.model_dump(),
            "memories": memories,
            "edges": self.edges(records),
            "count": len(memories),
            "cursor": query.cursor,
            "next_cursor": query.cursor + len(records) if has_more else None,
            "sensitive_hidden": not query.include_sensitive,
            "simulation": {
                "writeback": False,
                "parameters_assumed": True,
                "age_basis": "extraction_time",
                "confidence_unchanged": True,
            },
        }

    @staticmethod
    def edges(records):
        ids = {m["memory_id"] for m in records}
        edges, seen = [], set()

        def link(a, b, kind, label, inferred, weight):
            pair = tuple(sorted((a, b)))
            key = (*pair, kind)
            if a != b and a in ids and b in ids and key not in seen and len(edges) < 2000:
                seen.add(key)
                edges.append(
                    {
                        "source": a,
                        "target": b,
                        "kind": kind,
                        "label": label,
                        "inferred": inferred,
                        "weight": weight,
                    }
                )

        topics, sources, times = defaultdict(list), defaultdict(list), []
        for m in records:
            mid = m["memory_id"]
            if m.get("topic"):
                topics[m["topic"]].append(mid)
            if m.get("source_conversation_id"):
                sources[m["source_conversation_id"]].append(mid)
            if m["event_time"]:
                times.append((m["event_time"], mid))
            if m["status"] == "approved":
                for other in ([m["supersedes"]] if m.get("supersedes") else []) + m.get("merged_from", []):
                    link(
                        mid, other, "human_confirmed", "人工批准的取代/合并关系；不等于语义事实核验", False, 1
                    )
            for other in m.get("summary_links", []):
                link(mid, other, "source_link", "摘要与原文候选关联，仍需审核", False, 1)
        for mapping, kind, label, inferred, weight in (
            (topics, "semantic_association", "共同主题推导；不是向量相似度或事实证明", True, 0.5),
            (sources, "same_source", "同一来源会话；不表示观点一致", False, 1),
        ):
            for members in mapping.values():
                for a, b in zip(members, members[1:]):
                    link(a, b, kind, label, inferred, weight)
        times.sort()
        for (ta, a), (tb, b) in zip(times, times[1:]):
            if abs((datetime.fromisoformat(tb) - datetime.fromisoformat(ta)).days) <= 7:
                link(a, b, "time_near", "事件时间相近（7 天内）；不表示因果关系", True, 0.3)
        return edges

    def detail(self, memory_id, query, principal):
        scope = self.authorize(query, principal)
        memory = self.backend.get(memory_id, scope)
        if (not principal.admin and memory.status != "approved") or (
            memory.sensitivity == "sensitive" and not query.include_sensitive
        ):
            raise ServiceError("not_found", "记忆不在当前可见范围内", 404)
        return {
            **memory.model_dump(),
            "source_verification": "原文已定位；语义仍需审核"
            if memory.evidence_verified
            else "来源待核实；摘要不是逐字证据",
        }
