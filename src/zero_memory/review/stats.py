"""审核统计：按批次汇总通过率、驳回率与估算审核耗时，只读不写。"""

import json
from collections import Counter, defaultdict
from datetime import datetime

from zero_memory.review.queue import deferred_ids
from zero_memory.review.runs import run_of

SESSION_GAP_SECONDS = 600
DECISIONS = ("memory_approve", "memory_rejected", "memory_run_revoke")
JOURNAL_DIR = "review_queue/v03b/batches"
COUNT_KEYS = ("approved", "rejected", "batch_rejected", "run_revoked", "pending")


def review_durations(events):
    # 相邻两次人工审核操作的间隔记为后一条的审核耗时；超过会话间隔视为中断，不计
    durations, previous = {}, None
    for stamp, memory_id in events:
        current = datetime.fromisoformat(stamp)
        if previous is not None and (current - previous).total_seconds() <= SESSION_GAP_SECONDS:
            durations[memory_id] = (current - previous).total_seconds()
        previous = current
    return durations


def _outcome(memory, last):
    if memory["status"] == "pending_review":
        return "pending"
    if memory["status"] in ("approved", "superseded", "revoked"):
        return "approved"
    if last and last[0] == "memory_run_revoke":
        return "run_revoked"
    if last and last[1] == "admin_batch":
        return "batch_rejected"
    return "rejected"


def _journal(home, run_id):
    path = home / JOURNAL_DIR / f"{run_id}.json"
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items", []) if path.is_file() else None
    except (OSError, ValueError, AttributeError):
        return None
    if items is None:
        return None
    return {"requests": len(items), "statuses": dict(Counter(item.get("status") for item in items)),
            "reasons": dict(Counter(item["reason"] for item in items if item.get("reason")))}


def _summary(memories, last, durations, deferred):
    counts = Counter(_outcome(m, last.get(m["memory_id"])) for m in memories)
    reviewed = counts["approved"] + counts["rejected"] + counts["batch_rejected"]
    seconds = [durations[m["memory_id"]] for m in memories if m["memory_id"] in durations]
    return {
        "candidates": len(memories), **{key: counts[key] for key in COUNT_KEYS},
        "deferred": sum(1 for m in memories if m["status"] == "pending_review" and m["memory_id"] in deferred),
        "pass_rate": counts["approved"] / reviewed if reviewed else None,
        "reject_rate": (counts["rejected"] + counts["batch_rejected"]) / reviewed if reviewed else None,
        "avg_review_seconds": sum(seconds) / len(seconds) if seconds else None,
    }


def run_stats(store, scope):
    with store.read() as db:
        memories = [json.loads(row[0]) for row in db.execute(
            "SELECT body FROM memories WHERE user_id=? AND agent_id=? AND project_id=? "
            "AND json_extract(body,'$.review_note') LIKE 'DeepSeek __ %'", scope.sql())]
        audits = db.execute(f"SELECT time, action, actor, target FROM audit WHERE action IN "
                            f"({','.join('?' * len(DECISIONS))}) ORDER BY id", DECISIONS).fetchall()
        deferred = deferred_ids(db, scope)
    last = {target: (action, actor) for _, action, actor, target in audits}
    durations = review_durations([(stamp, target) for stamp, action, actor, target in audits
                                  if actor == "admin" and action != "memory_run_revoke"])
    groups = defaultdict(list)
    for memory in memories:
        run = run_of(memory.get("review_note"))
        if run:
            groups[(run["run_id"], run["kind"])].append(memory)
    runs = [{"run_id": run_id, "kind": kind, **_summary(items, last, durations, deferred),
             "journal": _journal(store.home, run_id)}
            for (run_id, kind), items in sorted(groups.items(), key=lambda pair: min(m["extracted_at"] for m in pair[1]))]
    return {"runs": runs, "overall": {"runs": len(runs), **_summary(memories, last, durations, deferred)}}
