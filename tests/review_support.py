"""审核模块测试辅助：只生成合成消息和合成候选。"""

import json
from uuid import uuid4

from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Proposal, Scope

RUN_A, RUN_B = "a" * 64, "b" * 64
NOTE_TEXTS = ["我每天用纸质笔记整理技术方案", "我每天用纸质笔记整理技术方案和会议记录",
              "我以前用纸质笔记整理技术方案，现在改用平板"]


def seed_messages(client, texts, project_id="default"):
    # 每条文本导入为一个只含用户消息的 ChatGPT 合成会话
    scope = Scope(project_id=project_id)
    tag = uuid4().hex[:8]
    conversations = [
        {"id": f"c-{tag}-{i}", "title": "合成会话", "current_node": "u",
         "mapping": {"u": {"parent": None, "children": [], "message": {
             "author": {"role": "user"}, "create_time": 1_700_000_000 + i,
             "content": {"content_type": "text", "parts": [text]}}}}}
        for i, text in enumerate(texts)
    ]
    # conftest.import_data 启动任务时只用默认作用域，这里按作用域传查询参数
    params = {"user_id": scope.user_id, "project_id": scope.project_id}
    data = json.dumps(conversations, ensure_ascii=False).encode()
    job = client.post("/v1/imports", files={"file": ("conversations.json", data)},
                      data={"scope": json.dumps(scope.model_dump())}).json()
    assert client.post(f"/v1/imports/{job['id']}/start", json={}, params=params).status_code == 200
    assert client.get(f"/v1/imports/{job['id']}", params=params).json()["status"] == "completed"
    with client.app.state.store.read() as db:
        rows = [db.execute("SELECT key, conversation_id, message_id, timestamp FROM messages WHERE user_id=? "
                           "AND agent_id=? AND project_id=? AND content=? ORDER BY rowid DESC LIMIT 1",
                           (*scope.sql(), text)).fetchone() for text in texts]
    return [{"key": r[0], "conversation_id": r[1], "message_id": r[2], "timestamp": r[3], "text": text}
            for r, text in zip(rows, texts, strict=True)]


def candidate(app, message, *, run_id=RUN_A, content=None, evidence=None, confidence=0.6,
              memory_type="user_profile", sensitivity="private", fact_key=None, project_id="default"):
    # 字段与 cloud_batch.save_candidate 写入的候选一致
    proposal = Proposal(
        content=content or message["text"], evidence=evidence or message["text"], memory_type=memory_type,
        confidence=confidence, sensitivity=sensitivity, fact_key=fact_key, claim_kind="user_reported",
        source_type="chatgpt_export", source_conversation_id=message["conversation_id"],
        source_message_ids=[message["message_id"]], source_message_keys=[message["key"]],
        origin_quality="verbatim_export_text", inference_type="system_summary",
        review_note=f"DeepSeek 批次 {run_id} / cloud-pilot-1 / deepseek-flash。来源时间 {message['timestamp'] or '未知'}。",
    )
    return SQLiteBackend(app.state.store).propose(proposal, Scope(project_id=project_id), "deepseek_batch").model_dump()


def client_token(client):
    return client.post("/v1/clients", json={"name": "synthetic"}).json()["token"]
