import json

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import digest


def verify_activity_proposal(db, store, proposal, scope):
    if (
        not proposal.activity_goal_id
        or not proposal.activity_episode_ids
        or not proposal.activity_artifact_ids
        or proposal.source_role != "tool"
        or proposal.inference_type != "system_summary"
        or proposal.source_message_ids
        or proposal.source_message_keys
        or proposal.source_conversation_id
    ):
        raise ServiceError("activity_provenance", "自身经历须引用真实本地回执，不能冒充导入消息")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"goals", "episodes", "activity_artifacts"}.issubset(tables):
        raise ServiceError("activity_provenance", "未启用经历存储")

    def read(table, ident):
        row = db.execute(
            f"SELECT body FROM {table} WHERE id=? AND user_id=? AND agent_id=? AND project_id=?",
            (ident, *scope.sql()),
        ).fetchone()
        if not row:
            raise ServiceError("activity_provenance", "经历或产物不在授权范围")
        return json.loads(row[0])

    read("goals", proposal.activity_goal_id)
    evidence_ids = set()
    for ident in proposal.activity_episode_ids:
        episode = read("episodes", ident)
        if (
            episode["goal_id"] != proposal.activity_goal_id
            or episode["status"] != "verified"
            or not episode["executed"]
            or episode["source_type"] != "local_read_tool"
        ):
            raise ServiceError("activity_provenance", "经历没有实际工具执行证据")
        evidence_ids.update(episode["evidence_ids"])
    if set(proposal.activity_artifact_ids) != evidence_ids:
        raise ServiceError("activity_provenance", "经历和产物引用不一致")
    summaries = []
    for ident in evidence_ids:
        artifact = read("activity_artifacts", ident)
        checksum = artifact["sha256"]
        if (
            len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
            or artifact["goal_id"] != proposal.activity_goal_id
            or artifact["source_type"] != "local_read_tool"
        ):
            raise ServiceError("activity_provenance", "产物来源无效")
        path = store.home / "activity_artifacts" / (checksum + ".json")
        if not path.exists() or digest(path.read_bytes()) != checksum:
            raise ServiceError("activity_provenance", "产物缺失或校验失败")
        summaries.append(artifact["summary"])
    if proposal.evidence not in summaries:
        raise ServiceError("activity_provenance", "证据须为真实回执中的原文")
    return True
