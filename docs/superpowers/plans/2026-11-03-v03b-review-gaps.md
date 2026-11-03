# v0.3b 审核缺口补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准的设计补齐 v0.3b 审核功能：失败原因码、审核队列与稍后处理、A/C 建议、批量操作、批次撤销、审核统计、敏感队列和键盘审核。

**Architecture:** 新增 `zero_memory/review/` 包。两张新表在首次写入时创建，路由 `/v1/review/*` 只允许本机 Web 管理会话访问。原有记忆模型、MCP 和客户端接口不变，唯一改动是修复敏感记忆的排除。前端新增 `web/review.js` 接管审核页。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic v2、SQLite（JSON1 / FTS5）、portalocker、httpx（仅测试 mock）、原生 JS。

**Spec:** `docs/superpowers/specs/2026-11-03-v03b-review-gaps-design.md`

## Global Constraints

- 不新增依赖。新代码路径不发起任何网络或模型调用。
- `/v1/review/*` 全部依赖 `web_admin`，客户端令牌一律返回 403。
- 原因码、审计详情和日志不得包含消息或候选的正文。
- 不修改原有表结构。新表 `review_marks`、`memory_links` 的列为 `(id, user_id, agent_id, project_id, body)`，只在首次写入时创建，读取前用 `initialized(db)` 判断。
- 常量：
  - 低优先级阈值默认 `0.5`
  - 单次上限：批量批准 `50`；批量驳回 / 稍后处理 `200`；所选 `30`
  - 抽样 `5` 条
  - 重合片段下限 `4`；每条候选目标上限 `3`
  - 会话间隔 `600` 秒
  - 方法版本 `shingle3-v1`
- 确认串：
  - 批量操作：`确认批量{驳回|稍后处理|批准} {记忆类型或「所选」} {集合哈希前 12 位}`
  - 批次撤销：`确认撤销批次 {run_id}`
- 批次编号正则：`^DeepSeek (?:批次|试点) ([0-9a-f]{64})`。
- 代码注释用中文。提交信息用英文 conventional commits，不加署名行。
- 测试只用合成数据和 `tmp_path`，不读写 `E:\ZeroMemory\data`。
- Ruff 配置：`line-length = 110`，规则 `E4,E7,E9,F,I`。
- 在分支 `feat/v03b-review-gaps` 上实施。

## Review Focus

1. **旧种子记忆缺字段**：没有批次备注、来源消息键或置信度时，队列、优先级和批次解析都不能报错。测试：Task 2 `test_queue_handles_seed_style_memories`。
2. **敏感文件损坏或编号不合法**：隔离文件损坏时，列表跳过并计数；`item_id` 含路径字符时，动作返回 422。测试：Task 7 `test_sensitive_listing_skips_malformed_files`、`test_item_id_must_be_hex`。
3. **同一确认串重复提交**：第二次提交不能重复批准，也不能返回 500。测试：Task 4 `test_batch_apply_twice_is_harmless`。
4. **撤销无效批次**：批次不存在或编号格式错误时，不得写入任何数据。测试：Task 5 `test_run_revoke_unknown_run`。
5. **新表尚未创建**：此时读取队列和建议都要正常返回空结果。测试：Task 2 `test_queue_before_tables_exist`、Task 3 `test_links_before_tables_exist`。

## File Structure

```text
src/zero_memory/review/__init__.py     空包
src/zero_memory/review/tables.py       建表、结构校验、备份清单 REVIEW_TABLES
src/zero_memory/review/runs.py         从审核备注解析批次编号
src/zero_memory/review/queue.py        审核队列、优先级、稍后处理
src/zero_memory/review/links.py        A/C 建议计算与决定
src/zero_memory/review/batch.py        按类别或所选的批量操作
src/zero_memory/review/revoke.py       按批次撤销
src/zero_memory/review/stats.py        审核统计
src/zero_memory/review/sensitive.py    敏感隔离队列
src/zero_memory/review/router.py       /v1/review/* 路由与请求模型
web/review.js                          审核页、标签页、键盘、批量与批次面板
tests/review_support.py                合成消息与候选的测试辅助
tests/test_review_{queue,links,batch,revoke,stats}.py、tests/test_sensitive_queue.py
```

需要修改的现有文件：

- `extraction/cloud_batch.py`：失败原因码与待审核上限（Task 1）
- `core/backup.py`：备份与恢复纳入新表（Task 2）
- `api/app.py`：接入路由（Task 2）；客户端检索排除敏感记忆（Task 7）
- `memory/sqlite.py`：`transition` 支持自定义审计（Task 5）
- `core/context.py`：模型上下文排除敏感记忆（Task 7）
- 前端与脚本（Task 8）：`web/app.js`、`web/index.html`、`web/style.css`、`scripts/Test-ZeroMemory.ps1`

---

### Task 1: 云端批次失败原因码与待审核上限

**Files:**
- Modify: `src/zero_memory/extraction/cloud_batch.py`
- Test: `tests/test_cloud_preview.py`

**Interfaces:**
- Produces:
  - `class ResponseTooLarge(ValueError)`
  - `reason_for(exc: Exception) -> str`：判断顺序如下
    1. `ValidationError` → `schema:<loc>:<type>`（取第一条错误）
    2. `json.JSONDecodeError` → `not_json`
    3. `ResponseTooLarge` → `response_too_large`
    4. `KeyError`、`IndexError`、`TypeError` → `missing_choices`
  - `save_candidate(...)`：无效时返回 `{"status": "invalid_output", "reason": <码>}`。原因码对应关系：
    - 外层不是 `{"memories": list}` → `bad_shape`
    - 多条候选 → `multiple_memories`
    - 条目不是对象 → `item_not_object`
    - 类型不在允许范围 → `type_not_allowed`
    - 含 `summary_links` → `summary_links`
  - `run_batch(home, manifest, confirmation, max_pending: int | None = None) -> dict`：发送前，如果作用域内 `pending_review` 数 ≥ `max_pending`，返回 `state["halted"] = "queue_limit"`，不发送任何请求。
  - `summarize(state: dict) -> dict`：返回 `{"batch_id", "counts": {...}, "reasons": {...}}`。供 `--status` 和执行结束输出使用。
  - CLI 增加 `--max-pending N`。

- [ ] **Step 1: 写失败测试**

```python
CANARY = "ZM-CANARY-7731"

def test_invalid_outputs_record_reason_codes_without_text(tmp_path, app, monkeypatch):
    # 6 条含「python / 代码」的用户消息，按调用次序返回 6 种不合格响应
    texts = [f"我用 python 脚本整理代码笔记 {CANARY} 第{i}条" for i in range(6)]
    ...  # 与 test_confirmed_topic_batch_* 相同方式建 ZIP、run(prepared(...))、prepare(home, topic="工程")
    good = lambda t: {"content": "合成候选", "evidence": t, "memory_type": "user_profile"}
    replies = ["not json", {"memories": [good(t), good(t)]}, {"memories": [{**good(t), "claim_kind": "bogus"}]},
               {"memories": [{**good(t), "memory_type": "relationship"}]},
               {"memories": [{**good(t), "summary_links": ["x"]}]}, None]  # None：响应体没有 choices
    state = cloud_batch.run_batch(home, manifest, "确认发送批次 " + manifest["preview_id"])
    assert [i["status"] for i in state["items"]] == ["invalid_output"] * 6
    assert [i["reason"] for i in state["items"]] == [
        "not_json", "multiple_memories", "schema:claim_kind:literal_error",
        "type_not_allowed", "summary_links", "missing_choices"]
    assert cloud_batch.summarize(state)["reasons"]["not_json"] == 1
    assert CANARY not in json.dumps(state)

def test_max_pending_halts_before_sending(tmp_path, app, client, monkeypatch):
    ...  # 1 条工程消息；propose(client, "合成待审核") 制造 1 条待审核
    state = cloud_batch.run_batch(home, manifest, confirmation, max_pending=1)
    assert state["halted"] == "queue_limit" and calls == []
    assert [i["status"] for i in state["items"]] == ["pending"]
```

- [ ] **Step 2: 运行确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cloud_preview.py -q -k "reason_codes or max_pending"`
Expected: FAIL（`KeyError: 'reason'` / `unexpected keyword argument 'max_pending'`）

- [ ] **Step 3: 实现上述接口**。`run_batch` 的异常分支改为 `result.update(status="invalid_output", reason=reason_for(exc))`。原因码只用字段名和 pydantic 错误类型，不取 `input`。

- [ ] **Step 4: 运行通过**：同一命令 PASS，且 `-q tests/test_cloud_preview.py` 全部通过。

- [ ] **Step 5: Commit** `feat: record reason codes for invalid DeepSeek batch outputs`

---

### Task 2: 审核表、批次解析、队列与稍后处理（含备份）

**Files:**
- Create: `src/zero_memory/review/__init__.py`、`tables.py`、`runs.py`、`queue.py`、`router.py`
- Create: `tests/review_support.py`、`tests/test_review_queue.py`
- Modify: `src/zero_memory/api/app.py`（`include_router`）、`src/zero_memory/core/backup.py`

**Interfaces:**
- Produces:
  - `tables.REVIEW_TABLES: dict[str, tuple[str, ...]]`、`initialized(db) -> bool`、`install(db) -> None`：`user_version` 不在 `(0, 2)` 时抛 `ServiceError("schema_version")`；列不匹配时抛 `ServiceError("review_schema")`。
  - `runs.run_of(note: str | None) -> dict | None`：返回 `{"run_id": str, "kind": "batch" | "pilot"}`。
  - `runs.run_like(run_id: str) -> str`：返回 `f"DeepSeek __ {run_id}%"`，供 SQL `LIKE` 使用。
  - `queue.priority(memory: dict) -> float`：`confidence × max(1, 不同来源消息键数)`。
  - `queue.queue(store, scope, *, view="active", memory_type=None, source_type=None, run_id=None, threshold=0.5, limit=30, offset=0) -> dict`。四个视图都只看 `pending_review`：

    | view | 条件 |
    | --- | --- |
    | `active` | 非敏感、未稍后处理、优先级 ≥ 阈值 |
    | `deferred` | 非敏感、已稍后处理 |
    | `low_priority` | 非敏感、未稍后处理、优先级 < 阈值 |
    | `sensitive` | `sensitivity='sensitive'` |

    排序为优先级降序，再按 `extracted_at` 降序。返回值：
    - `items`：每项是 `{**memory, "review": {...}}`，`review` 含 `priority`、`low_priority`、`deferred`、`run` 和 `links`（本任务固定为 `{"evidence": 0, "conflict": 0}`）
    - `counts`：`{"active", "deferred", "low_priority", "sensitive"}`
    - `offset`、`limit`、`threshold`
  - `queue.deferred_ids(db, scope) -> set[str]`
  - `queue.defer(store, scope, memory_ids, actor, undo=False) -> {"updated": int, "undo": bool}`：只处理待审核项，不改记忆版本。
  - `router.review_router(store, backend, web_admin) -> APIRouter`：
    - `GET /v1/review/queue`：`threshold` 取值 0–1；`limit` 取值 1–100。
    - `POST /v1/review/defer`：请求体 `DeferRequest(Scope)`，`memory_ids` 1–200 条。
  - `tests/review_support.py`：
    - `RUN_A = "a" * 64`、`RUN_B = "b" * 64`
    - `NOTE_TEXTS = ["我每天用纸质笔记整理技术方案", "我每天用纸质笔记整理技术方案和会议记录", "我以前用纸质笔记整理技术方案，现在改用平板"]`（Task 3、4 共用）
    - `seed_messages(client, texts, project_id="default") -> list[dict]`：每项含 `key`、`conversation_id`、`message_id`、`timestamp`、`text`。用 `node()` 构造 ChatGPT 会话后经 `import_data` 导入，再按 `content` 查 `messages` 表。
    - `candidate(app, message, *, run_id=RUN_A, content=None, evidence=None, confidence=0.6, memory_type="user_profile", sensitivity="private", fact_key=None, project_id="default") -> dict`：用 `SQLiteBackend(app.state.store).propose`，字段与 `cloud_batch.save_candidate` 相同；`review_note=f"DeepSeek 批次 {run_id} / cloud-pilot-1 / deepseek-flash。来源时间 …"`；`actor="deepseek_batch"`。
    - `client_token(client) -> str`
- Backup：
  - `Backups.create` 导出存在的 `REVIEW_TABLES`。
  - 恢复时，如果备份里有部分审核表，抛 `ServiceError("backup_schema", "审核表清单不完整", 422)`；表都在时先 `install` 再写入。

- [ ] **Step 1: 写失败测试**

```python
def test_queue_orders_by_priority_and_folds_low_priority(app, client):
    msgs = seed_messages(client, ["我喜欢用纸质笔记", "我习惯晚上写代码", "我偶尔去爬山"])
    high, mid = candidate(app, msgs[0], confidence=0.9), candidate(app, msgs[1], confidence=0.6)
    low = candidate(app, msgs[2], confidence=0.3)
    data = client.get("/v1/review/queue").json()
    assert [i["memory_id"] for i in data["items"]] == [high["memory_id"], mid["memory_id"]]
    assert data["counts"] == {"active": 2, "deferred": 0, "low_priority": 1, "sensitive": 0}
    assert data["items"][0]["review"]["priority"] == 0.9
    assert data["items"][0]["review"]["run"] == {"run_id": RUN_A, "kind": "batch"}
    folded = client.get("/v1/review/queue", params={"view": "low_priority"}).json()["items"]
    assert [i["memory_id"] for i in folded] == [low["memory_id"]]
    assert client.get("/v1/review/queue", params={"threshold": 0.2}).json()["counts"]["low_priority"] == 0

def test_defer_hides_until_undone_without_new_version(app, client):
    m = candidate(app, seed_messages(client, ["我每天早上跑步"])[0])
    assert client.post("/v1/review/defer", json={"memory_ids": [m["memory_id"]]}).json() == {"updated": 1, "undo": False}
    assert client.get("/v1/review/queue").json()["items"] == []
    [item] = client.get("/v1/review/queue", params={"view": "deferred"}).json()["items"]
    assert item["review"]["deferred"] is True and item["version"] == m["version"]
    client.post("/v1/review/defer", json={"memory_ids": [m["memory_id"]], "undo": True})
    assert len(client.get("/v1/review/queue").json()["items"]) == 1

def test_queue_handles_seed_style_memories(client):
    seed = propose(client, "手工记录的偏好")  # 无批次备注、无来源消息键
    [item] = client.get("/v1/review/queue").json()["items"]
    assert item["review"]["run"] is None and item["review"]["priority"] == seed["confidence"]

def test_sensitive_pending_only_in_sensitive_view(app, client):
    m = candidate(app, seed_messages(client, ["我的投资计划"])[0], sensitivity="sensitive")
    data = client.get("/v1/review/queue").json()
    assert data["items"] == [] and data["counts"]["sensitive"] == 1
    assert client.get("/v1/review/queue", params={"view": "sensitive"}).json()["items"][0]["memory_id"] == m["memory_id"]

def test_queue_before_tables_exist(app, client):
    with app.state.store.read() as db:
        assert not initialized(db)
    assert client.get("/v1/review/queue").json()["counts"] == {"active": 0, "deferred": 0, "low_priority": 0, "sensitive": 0}

def test_queue_is_scoped_and_web_only(app, client):
    m = candidate(app, seed_messages(client, ["别的项目"], project_id="other")[0], project_id="other")
    assert client.get("/v1/review/queue").json()["items"] == []
    assert client.get("/v1/review/queue", params={"project_id": "other"}).json()["items"][0]["memory_id"] == m["memory_id"]
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.get("/v1/review/queue", headers=headers).status_code == 403
    assert client.post("/v1/review/defer", json={"memory_ids": ["x"]}, headers=headers).status_code == 403

def test_review_tables_survive_backup_restore(app, client):
    m = candidate(app, seed_messages(client, ["我喜欢安静的咖啡馆"])[0])
    client.post("/v1/review/defer", json={"memory_ids": [m["memory_id"]]})
    ...  # 按 tests/test_security_models_backup.py 现有方式创建备份并恢复到新目录
    with Store(restored_home).read() as db:
        assert db.execute("SELECT count(*) FROM review_marks").fetchone()[0] == 1
```

- [ ] **Step 2: 运行确认失败**：`./.venv/Scripts/python.exe -m pytest tests/test_review_queue.py -q`，结果为 FAIL（404 或导入错误）。
- [ ] **Step 3: 实现上述接口，并在 `app.py` 中 `include_router(review_router(store, backend, web_admin))`**。

  实现要点：
  - 计数和筛选用 SQL：`LEFT JOIN review_marks`；优先级表达式为 `json_extract(confidence) * max(1, json_array_length(source_message_keys))`。
  - 结果在 Python 侧用 `priority()` 复算。
  - `initialized` 为假时按无标记处理。
- [ ] **Step 4: 运行通过**：同一命令 PASS；`tests/test_security_models_backup.py` 仍然通过。
- [ ] **Step 5: Commit** `feat: add review queue with priority, low-priority folding and defer`

---

### Task 3: A/C 关联与时间冲突建议

**Files:**
- Create: `src/zero_memory/review/links.py`、`tests/test_review_links.py`
- Modify: `src/zero_memory/review/router.py`、`src/zero_memory/review/queue.py`（`review.links` 改为真实计数）

**Interfaces:**
- Consumes: `_shingles` 和 `CHANGE_SIGNAL` 来自 `extraction/cloud_preview.py`；`run_of`；`install` / `initialized`。
- Produces:
  - `METHOD = "shingle3-v1"`、`MIN_OVERLAP = 4`、`MAX_TARGETS = 3`
  - `suggest_links(store, scope, actor="admin") -> {"candidates": int, "created": int, "evidence": int, "conflict": int}`
    - 候选：非敏感、待审核的 `chatgpt_export`。
    - 目标：同作用域内 `extracted_at` 更早、非敏感、状态为 `approved` 或 `pending_review`、不属于同一批次。
    - 比较：候选证据的片段与目标内容重合 ≥ 4，每条候选按重合数取前 3 个目标。
    - 分类：证据中有 `CHANGE_SIGNAL`，或事实键一致但内容不同，归为 `conflict`；其余归为 `evidence`。
    - `conflict` 附 `time_version = {"new_valid_from": 证据消息 timestamp, "old_valid_to": 同值}`。
    - `link_id = digest(dumps([source, target, METHOD]))`。已存在的不重写；`created` 只计新写入的条数。
  - `links_for(store, scope, memory_id) -> {"items": [link]}`。每个 link 含 `link_id`、`source_memory_id`、`target_memory_id`、`kind`、`status`、`signals`（`overlap`、`time_change_wording`、`fact_key_match`）、`target_status`、`time_version`、`method`、`created_at`、`decided_at`、`decided_by`。
  - `decide_link(store, scope, link_id, decision: Literal["confirm", "dismiss"], actor="admin") -> dict`：`status` 改为 `confirmed` 或 `dismissed`；只写建议表。
  - `open_conflict_ids(db, scope) -> set[str]`：`kind='conflict'` 且 `status` 为 `suggested` 或 `confirmed` 的候选 ID。
  - `link_counts(db, scope, memory_ids) -> dict[str, dict]`：只计未忽略的建议。
  - 路由：`POST /v1/review/links/suggest`（请求体 Scope）、`GET /v1/review/links?memory_id=`、`POST /v1/review/links/{link_id}`（请求体含 `decision`）。

- [ ] **Step 1: 写失败测试**

```python
# NOTE_TEXTS、RUN_A、RUN_B 来自 tests/review_support.py

def test_suggest_classifies_evidence_and_conflict(app, client):
    msgs = seed_messages(client, NOTE_TEXTS)
    old = approve(client, candidate(app, msgs[0], run_id=RUN_B))
    a, c = candidate(app, msgs[1]), candidate(app, msgs[2])
    assert client.post("/v1/review/links/suggest", json={}).json() == {"candidates": 2, "created": 2, "evidence": 1, "conflict": 1}
    [la] = client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"]
    assert (la["kind"], la["target_memory_id"], la["status"], la["time_version"]) == ("evidence", old["memory_id"], "suggested", None)
    [lc] = client.get("/v1/review/links", params={"memory_id": c["memory_id"]}).json()["items"]
    assert lc["kind"] == "conflict" and lc["signals"]["time_change_wording"] is True
    assert lc["time_version"] == {"new_valid_from": msgs[2]["timestamp"], "old_valid_to": msgs[2]["timestamp"]}
    links = {i["memory_id"]: i["review"]["links"] for i in client.get("/v1/review/queue").json()["items"]}
    assert links == {a["memory_id"]: {"evidence": 1, "conflict": 0}, c["memory_id"]: {"evidence": 0, "conflict": 1}}

def test_suggest_skips_sensitive_other_scope_and_same_run(app, client):
    msgs = seed_messages(client, [NOTE_TEXTS[0], NOTE_TEXTS[1], NOTE_TEXTS[0] + "的模板"])
    candidate(app, msgs[0], sensitivity="sensitive")
    candidate(app, msgs[1]); candidate(app, msgs[2])        # 同一批次 RUN_A
    other = seed_messages(client, [NOTE_TEXTS[0]], project_id="other")
    candidate(app, other[0], run_id=RUN_B, project_id="other")
    assert client.post("/v1/review/links/suggest", json={}).json() == {"candidates": 2, "created": 0, "evidence": 0, "conflict": 0}

def test_suggest_is_idempotent_and_keeps_decisions(app, client):
    msgs = seed_messages(client, NOTE_TEXTS[:2])
    approve(client, candidate(app, msgs[0], run_id=RUN_B)); a = candidate(app, msgs[1])
    client.post("/v1/review/links/suggest", json={})
    [link] = client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"]
    assert client.post(f"/v1/review/links/{link['link_id']}", json={"decision": "dismiss"}).json()["status"] == "dismissed"
    assert client.post("/v1/review/links/suggest", json={}).json()["created"] == 0
    assert client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"][0]["status"] == "dismissed"

def test_confirm_evidence_link_does_not_modify_target(app, client):
    msgs = seed_messages(client, NOTE_TEXTS[:2])
    old = approve(client, candidate(app, msgs[0], run_id=RUN_B)); a = candidate(app, msgs[1])
    client.post("/v1/review/links/suggest", json={})
    [link] = client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"]
    before = SQLiteBackend(app.state.store).get(old["memory_id"], Scope()).model_dump()
    assert client.post(f"/v1/review/links/{link['link_id']}", json={"decision": "confirm"}).json()["status"] == "confirmed"
    assert SQLiteBackend(app.state.store).get(old["memory_id"], Scope()).model_dump() == before

def test_links_before_tables_exist(client):
    assert client.get("/v1/review/links", params={"memory_id": "missing"}).json() == {"items": []}
    assert client.post("/v1/review/links/suggest", json={}).json() == {"candidates": 0, "created": 0, "evidence": 0, "conflict": 0}
```

- [ ] **Step 2: 运行确认失败**：`./.venv/Scripts/python.exe -m pytest tests/test_review_links.py -q`，结果为 FAIL。
- [ ] **Step 3: 实现上述接口。**
- [ ] **Step 4: 运行通过**：`tests/test_review_links.py` 和 `tests/test_review_queue.py` 均 PASS。
- [ ] **Step 5: Commit** `feat: suggest local evidence and time-conflict links for candidates`

---

### Task 4: 按类别或所选的批量操作

**Files:**
- Create: `src/zero_memory/review/batch.py`、`tests/test_review_batch.py`
- Modify: `src/zero_memory/review/router.py`

**Interfaces:**
- Consumes:
  - 来自 `queue`：`queue.priority`、`queue.deferred_ids`、`queue.defer`
  - 来自 `links`：`links.open_conflict_ids`
  - 来自 `SQLiteBackend`：`approve(..., actor="admin_batch")`、`transition(..., actor="admin_batch")`
- Produces:
  - `APPROVE_LIMIT = 50`、`BULK_LIMIT = 200`、`SELECTION_LIMIT = 30`、`SAMPLE_SIZE = 5`
  - `class BatchRequest(Scope)`：`action: Literal["reject", "defer", "approve"]`、`memory_type: MemoryType | None`、`memory_ids: list[str]`（最多 30 条）、`source_type: str | None`、`run_id: str | None`、`threshold: float = 0.5`。`memory_type` 和 `memory_ids` 必须二选一，否则返回 422。
  - `class BatchApply(BatchRequest)`：`confirm: str`
  - `preview_batch(store, backend, scope, request) -> dict`，返回 `{"action", "selection": "category" | "ids", "matched", "eligible", "skipped": {原因: 数}, "samples": [{"memory_id", "content", "evidence"}], "limit", "confirm"}`。
    - 候选范围：只看非敏感的待审核项，按优先级排序后截断到上限。
    - 批准时跳过的原因：`deferred`、`low_priority`、`conflict_suggestion`、`needs_supersede`（`conflicts` 或 `summary_links` 非空）、`unverified`、`not_user`。
    - 驳回和稍后处理只跳过 `deferred`（稍后处理本身除外）。
  - `apply_batch(store, backend, scope, request, confirm) -> {"applied": int, "errors": list[str]}`：重新计算集合，确认串不一致时抛 `ServiceError("batch_confirm", "候选集合已变化或确认串不匹配，请重新预览", 409)`。
  - 路由：`POST /v1/review/batch/preview`、`POST /v1/review/batch/apply`

- [ ] **Step 1: 写失败测试**

```python
def test_batch_needs_category_or_selection(client):
    assert client.post("/v1/review/batch/preview", json={"action": "approve"}).status_code == 422
    assert client.post("/v1/review/batch/preview", json={"action": "reject", "memory_ids": ["x"] * 31}).status_code == 422

def test_batch_approve_previews_samples_and_skips_ineligible(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码", "我每周去游泳", "我在学日语", *NOTE_TEXTS[::2]])
    ok = [candidate(app, msgs[0]), candidate(app, msgs[1])]
    client.post("/v1/review/defer", json={"memory_ids": [candidate(app, msgs[2])["memory_id"]]})
    candidate(app, msgs[3], confidence=0.3)
    approve(client, candidate(app, msgs[4], run_id=RUN_B)); candidate(app, msgs[5])
    client.post("/v1/review/links/suggest", json={})
    propose(client, "手工陈述", memory_type="user_profile")       # 证据未核验
    body = {"action": "approve", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    assert (preview["eligible"], preview["limit"]) == (2, 50)
    assert preview["skipped"] == {"deferred": 1, "low_priority": 1, "conflict_suggestion": 1, "unverified": 1}
    assert {s["memory_id"] for s in preview["samples"]} == {m["memory_id"] for m in ok}
    assert preview["confirm"].startswith("确认批量批准 user_profile ")
    bad = {**body, "confirm": "确认批量批准 user_profile 000000000000"}
    assert client.post("/v1/review/batch/apply", json=bad).status_code == 409
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json() == {"applied": 2, "errors": []}

def test_batch_apply_rejects_changed_set(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"])
    candidate(app, msgs[0])
    body = {"action": "reject", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    candidate(app, msgs[1])
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).status_code == 409

def test_batch_apply_twice_is_harmless(app, client):
    m = candidate(app, seed_messages(client, ["我喜欢纸质笔记"])[0])
    body = {"action": "approve", "memory_ids": [m["memory_id"]]}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    assert preview["confirm"].startswith("确认批量批准 所选 ")
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json()["applied"] == 1
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).status_code == 409
    assert SQLiteBackend(app.state.store).get(m["memory_id"], Scope()).version == m["version"] + 1

def test_batch_limit_takes_highest_priority(app, client, monkeypatch):
    monkeypatch.setattr(batch, "APPROVE_LIMIT", 1)
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"])
    candidate(app, msgs[0], confidence=0.7); top = candidate(app, msgs[1], confidence=0.9)
    preview = client.post("/v1/review/batch/preview", json={"action": "approve", "memory_type": "user_profile"}).json()
    assert (preview["matched"], preview["eligible"], [s["memory_id"] for s in preview["samples"]]) == (2, 1, [top["memory_id"]])

def test_batch_defer_by_category(app, client):
    for m in seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"]):
        candidate(app, m)
    body = {"action": "defer", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json()["applied"] == 2
    assert client.get("/v1/review/queue").json()["counts"]["deferred"] == 2
```

- [ ] **Step 2: 运行确认失败**：`./.venv/Scripts/python.exe -m pytest tests/test_review_batch.py -q`，结果为 FAIL。
- [ ] **Step 3: 实现上述接口。** 集合哈希为 `digest(dumps(sorted(ids)))[:12]`。动作中文名：`{"reject": "驳回", "defer": "稍后处理", "approve": "批准"}`。
- [ ] **Step 4: 运行通过**：同一命令 PASS。
- [ ] **Step 5: Commit** `feat: add category and selection batch review with preview and confirmation`

---

### Task 5: 按批次撤销

**Files:**
- Create: `src/zero_memory/review/revoke.py`、`tests/test_review_revoke.py`
- Modify: `src/zero_memory/memory/sqlite.py`（`transition`）、`src/zero_memory/review/router.py`

**Interfaces:**
- Consumes: `runs.run_like`。`sensitive.reject_run_items` 在 Task 7 接入，本任务先用返回 0 的占位实现，在 `revoke.py` 内直接调用。
- Produces:
  - `SQLiteBackend.transition(memory_id, scope, status, actor="admin", audit_action: str | None = None, details: dict | None = None)`：原有调用行为不变。
  - `revoke_run(store, backend, scope, run_id, confirm: str | None = None, actor="admin") -> dict`
    - 预演返回 `{"run_id", "pending", "approved", "other", "sensitive_items", "confirm"}`
    - 执行返回 `{"run_id", "rejected", "sensitive_rejected"}`，审计动作为 `memory_run_revoke`，详情只含 `run_id`
    - 批次没有任何记忆也没有敏感条目时，抛 `ServiceError("run_not_found", ..., 404)`
    - 确认串不符时，抛 `ServiceError("revoke_confirm", ..., 409)`
  - 路由 `POST /v1/review/runs/{run_id}/revoke`：`run_id` 用 `Path(pattern="^[0-9a-f]{64}$")` 校验；请求体 `RevokeRequest(Scope)`，含 `confirm: str | None = None`。

- [ ] **Step 1: 写失败测试**

```python
def test_run_revoke_dry_run_then_rejects_only_pending(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码", "我每周去游泳", "我在学日语"])
    p1, p2 = candidate(app, msgs[0]), candidate(app, msgs[1])
    kept = approve(client, candidate(app, msgs[2])); other = candidate(app, msgs[3], run_id=RUN_B)
    url = f"/v1/review/runs/{RUN_A}/revoke"
    dry = client.post(url, json={}).json()
    assert dry == {"run_id": RUN_A, "pending": 2, "approved": 1, "other": 0, "sensitive_items": 0,
                   "confirm": f"确认撤销批次 {RUN_A}"}
    assert client.post(url, json={"confirm": "确认撤销批次 x"}).status_code == 409
    assert client.post(url, json={"confirm": dry["confirm"]}).json() == {"run_id": RUN_A, "rejected": 2, "sensitive_rejected": 0}
    backend = SQLiteBackend(app.state.store)
    assert [backend.get(m["memory_id"], Scope()).status for m in (p1, p2, kept, other)] == [
        "rejected", "rejected", "approved", "pending_review"]
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='memory_run_revoke'").fetchone()[0] == 2

def test_run_revoke_unknown_run(app, client):
    assert client.post("/v1/review/runs/" + "c" * 64 + "/revoke", json={}).status_code == 404
    assert client.post("/v1/review/runs/not-a-run/revoke", json={}).status_code == 422
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='memory_run_revoke'").fetchone()[0] == 0
```

- [ ] **Step 2: 运行确认失败**：`./.venv/Scripts/python.exe -m pytest tests/test_review_revoke.py -q`，结果为 FAIL。
- [ ] **Step 3: 实现上述接口。**
- [ ] **Step 4: 运行通过**：本任务测试 PASS，全量 `pytest -q` 不回退。
- [ ] **Step 5: Commit** `feat: revoke pending candidates of an extraction run after a dry run`

---

### Task 6: 审核统计

**Files:**
- Create: `src/zero_memory/review/stats.py`、`tests/test_review_stats.py`
- Modify: `src/zero_memory/review/router.py`

**Interfaces:**
- Produces:
  - `SESSION_GAP_SECONDS = 600`
  - `review_durations(events: list[tuple[str, str]]) -> dict[str, float]`
    - 输入：按时间排序的 `(ISO 时间, memory_id)` 列表，只取 actor 为 `admin` 的 `memory_approve` 和 `memory_rejected`。
    - 计算：每个事件与前一事件的间隔，不超过 600 秒的才计入。
  - `run_stats(store, scope) -> {"runs": [...], "overall": {...}}`。每个批次一行，字段：
    - 标识：`run_id`、`kind`
    - 计数：`candidates`、`approved`、`rejected`、`batch_rejected`、`run_revoked`、`pending`、`deferred`
    - 比率与耗时：`pass_rate`、`reject_rate`、`avg_review_seconds`
    - 批次日志：`journal`，为 `{"requests", "statuses", "reasons"}`，没有日志时为 `None`
  - 统计口径：
    - `approved`：状态为 `approved` 或 `superseded`。
    - 驳回按该记忆最后一次相关审计归类：`memory_run_revoke` 计入 `run_revoked`；actor 为 `admin_batch` 计入 `batch_rejected`；其余计入 `rejected`。
    - `pass_rate = approved / (approved + rejected + batch_rejected)`，分母为 0 时为 `None`。
    - `journal` 读取 `home/review_queue/v03b/batches/{run_id}.json`。
  - 路由：`GET /v1/review/runs`

- [ ] **Step 1: 写失败测试**

```python
def test_review_durations_ignore_long_gaps():
    t0 = datetime(2026, 11, 3, tzinfo=UTC)
    events = [((t0 + timedelta(seconds=s)).isoformat(), f"m{i}") for i, s in enumerate([0, 30, 90, 2000, 2040])]
    assert review_durations(events) == {"m1": 30.0, "m2": 60.0, "m4": 40.0}

def test_run_stats_separate_manual_batch_and_revoke(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码", "我每周去游泳", "我在学日语"])
    approve(client, candidate(app, msgs[0]))
    r = candidate(app, msgs[1])
    client.post(f"/v1/memories/{r['memory_id']}/reject", json=Scope().model_dump())
    b = {"action": "reject", "memory_ids": [candidate(app, msgs[2])["memory_id"]]}
    client.post("/v1/review/batch/apply", json={**b, "confirm": client.post("/v1/review/batch/preview", json=b).json()["confirm"]})
    candidate(app, msgs[3])
    client.post(f"/v1/review/runs/{RUN_A}/revoke", json={"confirm": f"确认撤销批次 {RUN_A}"})
    run = next(x for x in client.get("/v1/review/runs").json()["runs"] if x["run_id"] == RUN_A)
    assert {k: run[k] for k in ("candidates", "approved", "rejected", "batch_rejected", "run_revoked", "pending")} == {
        "candidates": 4, "approved": 1, "rejected": 1, "batch_rejected": 1, "run_revoked": 1, "pending": 0}
    assert run["pass_rate"] == pytest.approx(1 / 3) and run["reject_rate"] == pytest.approx(2 / 3)

def test_run_stats_include_journal_counts(app, client):
    candidate(app, seed_messages(client, ["我喜欢纸质笔记"])[0])
    journal = app.state.store.home / "review_queue/v03b/batches" / f"{RUN_A}.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"batch_id": RUN_A, "items": [
        {"number": 1, "status": "pending_review"}, {"number": 2, "status": "invalid_output", "reason": "not_json"}]}))
    run = next(x for x in client.get("/v1/review/runs").json()["runs"] if x["run_id"] == RUN_A)
    assert run["journal"] == {"requests": 2, "statuses": {"pending_review": 1, "invalid_output": 1}, "reasons": {"not_json": 1}}
```

- [ ] **Step 2: 运行确认失败**：`./.venv/Scripts/python.exe -m pytest tests/test_review_stats.py -q`，结果为 FAIL。
- [ ] **Step 3: 实现上述接口。**
- [ ] **Step 4: 运行通过**：同一命令 PASS。
- [ ] **Step 5: Commit** `feat: report review pass rates and estimated review time per run`

---

### Task 7: 敏感队列与敏感记忆排除

**Files:**
- Create: `src/zero_memory/review/sensitive.py`、`tests/test_sensitive_queue.py`
- Modify:
  - `src/zero_memory/api/app.py`：`search_memories` 对非管理员设 `exclude_sensitive=True`
  - `src/zero_memory/core/context.py`：两处 `Search(...)` 加上 `exclude_sensitive=True`
  - `src/zero_memory/review/revoke.py`：接入 `reject_run_items`
  - `src/zero_memory/review/router.py`

**Interfaces:**
- Consumes：`SENSITIVE`（来自 `extraction/cloud_preview.py`）；`ALLOWED_TYPES`（来自 `extraction/cloud_batch.py`）；`SQLiteBackend.propose` / `approve`。
- Produces:
  - `QUEUE_DIR = "review_queue/v03b/sensitive"`、`ITEM_ID = re.compile(r"^[0-9a-f]{64}$")`
  - `sensitive_items(store, scope) -> {"items": [...], "skipped_files": int}`
    - 只列 `status == "pending_review"`、且 `source_message_key` 在当前作用域 `messages` 表中的条目。
    - 每项字段：`item_id`、`status`、`deferred`、`run_id`、`observed_at`、`source_message_key`、`source_conversation_id`、`candidate`、`rule_hits`（`SENSITIVE` 在内容和证据中命中的去重词，排序后输出）。
    - 无法解析的文件计入 `skipped_files`。
  - `act(store, backend, scope, item_id, action: Literal["keep_sensitive", "to_private", "reject", "defer"], actor="admin") -> {"item_id", "status", "memory_id", "approved"}`
    - `keep_sensitive`：以 `sensitivity="sensitive"` 调用 propose，再尝试 approve。遇到 409 时保持待审核，返回 `approved=False`。文件状态改为 `kept_sensitive`。
    - `to_private`：以 `private` 调用 propose，文件状态改为 `moved_to_review`。
    - `reject`：删除 `candidate`，写入 `candidate_sha256`，状态改为 `rejected`。
    - `defer`：写入 `deferred=True`。
    - 读改写都在 `store.lock()` 内完成，写入用 `atomic_json`。
    - propose 用的 `review_note` 为 `f"DeepSeek 批次 {run_id} / 敏感队列转入"`。
  - `reject_run_items(store, scope, run_id, actor) -> int`
  - 路由：`GET /v1/review/sensitive`、`POST /v1/review/sensitive/{item_id}`（`item_id` 用 Path 正则校验，请求体含 `action`）

- [ ] **Step 1: 写失败测试**

```python
SECRET = "ZM-CANARY-5521"

def write_item(app, message, *, run_id=RUN_A, item_id="c" * 64, content=f"合成敏感候选 {SECRET}"):
    # 字段与 cloud_batch.save_candidate 写入的敏感文件一致
    ...

def test_client_search_and_context_exclude_sensitive(client):
    m = approve(client, propose(client, f"合成敏感事实 {SECRET}", sensitivity="sensitive"))
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.post("/v1/memories/search", json={"query": "合成敏感事实"}, headers=headers).json()["items"] == []
    assert SECRET not in client.get("/v1/context", params={"query": "合成敏感事实"}, headers=headers).text
    admin = client.post("/v1/memories/search", json={"query": "合成敏感事实"}).json()["items"]
    assert [x["memory_id"] for x in admin] == [m["memory_id"]]

def test_sensitive_listing_is_scoped_with_rule_hits(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0])
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"], project_id="other")[0], item_id="d" * 64)
    data = client.get("/v1/review/sensitive").json()
    assert [i["item_id"] for i in data["items"]] == ["c" * 64] and data["items"][0]["rule_hits"] == ["投资"]

def test_sensitive_listing_skips_malformed_files(app, client):
    folder = app.state.store.home / "review_queue/v03b/sensitive"
    folder.mkdir(parents=True)
    (folder / ("e" * 64 + ".json")).write_text("{broken", encoding="utf-8")
    assert client.get("/v1/review/sensitive").json() == {"items": [], "skipped_files": 1}

def test_item_id_must_be_hex(client):
    assert client.post("/v1/review/sensitive/" + "z" * 64, json={"action": "reject"}).status_code == 422

def test_keep_sensitive_approves_and_hides_from_clients(app, client):
    msg = seed_messages(client, ["我的投资计划是每月定投"])[0]
    write_item(app, msg, content="每月定投")
    result = client.post("/v1/review/sensitive/" + "c" * 64, json={"action": "keep_sensitive"}).json()
    assert (result["status"], result["approved"]) == ("kept_sensitive", True)
    memory = SQLiteBackend(app.state.store).get(result["memory_id"], Scope())
    assert (memory.sensitivity, memory.status) == ("sensitive", "approved")
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.post("/v1/memories/search", json={"query": "定投"}, headers=headers).json()["items"] == []
    assert client.get("/v1/review/sensitive").json()["items"] == []

def test_to_private_moves_into_regular_queue(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0], content="每月定投")
    result = client.post("/v1/review/sensitive/" + "c" * 64, json={"action": "to_private"}).json()
    [item] = client.get("/v1/review/queue").json()["items"]
    assert (result["status"], item["memory_id"], item["sensitivity"]) == ("moved_to_review", result["memory_id"], "private")

def test_reject_strips_candidate_text(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0])
    client.post("/v1/review/sensitive/" + "c" * 64, json={"action": "reject"})
    text = (app.state.store.home / "review_queue/v03b/sensitive" / ("c" * 64 + ".json")).read_text(encoding="utf-8")
    assert SECRET not in text and '"candidate_sha256"' in text and '"rejected"' in text

def test_run_revoke_rejects_sensitive_items_of_run(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0])
    url = f"/v1/review/runs/{RUN_A}/revoke"
    assert client.post(url, json={}).json()["sensitive_items"] == 1
    assert client.post(url, json={"confirm": f"确认撤销批次 {RUN_A}"}).json()["sensitive_rejected"] == 1

REVIEW_ROUTES = [("GET", "/v1/review/queue"), ("POST", "/v1/review/defer"), ("POST", "/v1/review/batch/preview"),
                 ("POST", "/v1/review/batch/apply"), ("GET", "/v1/review/runs"),
                 ("POST", f"/v1/review/runs/{RUN_A}/revoke"), ("POST", "/v1/review/links/suggest"),
                 ("GET", "/v1/review/links"), ("POST", "/v1/review/links/" + "a" * 64),
                 ("GET", "/v1/review/sensitive"), ("POST", "/v1/review/sensitive/" + "c" * 64)]

@pytest.mark.parametrize(("method", "url"), REVIEW_ROUTES)
def test_all_review_routes_reject_client_tokens(client, method, url):
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.request(method, url, json={}, headers=headers).status_code == 403
```

- [ ] **Step 2: 运行确认失败**：`./.venv/Scripts/python.exe -m pytest tests/test_sensitive_queue.py -q`，结果为 FAIL。
- [ ] **Step 3: 实现上述接口并完成两处排除修复。**
- [ ] **Step 4: 运行通过**：本任务测试 PASS，全量 `pytest -q` 全部通过。
- [ ] **Step 5: Commit** `feat: add sensitive review queue and keep sensitive memories out of client context`

---

### Task 8: 审核页前端与键盘

**Files:**
- Create: `web/review.js`
- Modify: `web/app.js`、`web/index.html`、`web/style.css`、`scripts/Test-ZeroMemory.ps1`

**Interfaces:**
- Consumes：
  - `app.js` 的全局函数：`api`、`esc`、`qs`、`bind`、`notify`、`renderMemory`、`bindMemoryActions`、`approveOrReject`、`showDetail`、`types`、`statuses`
  - 全局变量：`currentItems`、`pageCleanup`、`reviewMode`
  - Task 2–7 的接口
- Produces：
  - `async function reviewPage()`：顶部标签页依次为「待审核」「稍后处理 (N)」「低优先级 (N)」「敏感队列 (N)」「提炼批次」。保留 `?review=chatgpt_export` 和 `?review=all` 的入口行为，「只看早期 DeepSeek 试点」改为批次筛选。
  - `function reviewKeys(event)`：快捷键按设计 4.8。在 `input`、`textarea`、`select` 中或 `dialog[open]` 时不响应。在 `pageCleanup` 中移除监听。
  - 批量弹窗：显示 `matched`、`eligible`、`skipped`、`samples`，二次确认后调用 `apply`。「批准所选」改为 `memory_ids` 预览。
  - 「提炼批次」面板：统计表、失败原因、「撤销待审核候选」（先预演再确认）、「重新计算关联建议」。
  - 来源详情：「对照旧记忆」改为读取 `/v1/review/links`，提供确认和忽略按钮，C 类显示时间版本建议。
  - 敏感标签页：醒目提示条，显示规则命中词，逐条提供四个动作，没有批量入口。
  - `app.js`：`navigate` 映射 `review: () => reviewPage()`，删除 `memoryPage` 中仅审核用的分支。`index.html` 增加 `review.js`。`Test-ZeroMemory.ps1` 增加 `node --check web/raw-history.js` 和 `web/review.js`。

- [ ] **Step 1: 实现上述前端。**
- [ ] **Step 2: 语法检查**：`node --check web/app.js && node --check web/review.js && node --check web/raw-history.js`，全部无输出。
- [ ] **Step 3: 浏览器验收（合成数据）**
  - 准备环境：在临时数据目录用 `tests/review_support.py` 的辅助函数写入合成候选，加上 1 个敏感文件和 1 份批次日志，然后在 8799 端口启动服务。
  - 用内置浏览器逐项核对：
    - 标签页计数正确
    - `J` / `K` / `A` / `R` / `L` / `X` / `?` 生效，在输入框内不触发
    - 批量预览弹窗和二次确认
    - 批次撤销的预演与确认
    - A/C 的确认与忽略
    - 敏感队列的四个动作
    - 1600×1080 和 390×844 两种尺寸下都没有横向滚动
    - 控制台 0 错误
- [ ] **Step 4: Commit** `feat: add review workbench tabs, batch dialogs and keyboard shortcuts`

---

### Task 9: 文档、全量回归与整体审查

**Files:** `README.md`、`docs/HANDOFF.md`、`docs/IMPORT_GUIDE.md`（敏感队列和批次命令）、本计划的勾选状态

- [ ] **Step 1: 更新文档**
  - README：功能表的审核相关行、开发进度、未完成清单
  - HANDOFF：接手操作改为新入口
- [ ] **Step 2: 全量回归**：运行 `./scripts/Test-ZeroMemory.ps1`，要求 Ruff、四个 JS 语法检查、全部测试和合成端到端验收都通过，并记录测试数。
- [ ] **Step 3: 整体代码审查**：派一个独立的审查者审查 `main...feat/v03b-review-gaps` 的完整 diff，修复 CRITICAL / HIGH 问题后重跑 Step 2。
- [ ] **Step 4: Commit** `docs: describe the v0.3b review workbench`

---

### Task 10（闸门，需你另行同意）: 生产上线与最终报告

- [ ] **Step 1: 备份**：对 `data/` 做一致性备份，在隔离目录恢复验证，并记录 `backup_id`。
- [ ] **Step 2: 合并并重启**：合并分支，重启 8786 服务，确认健康检查正常、版本无误。
- [ ] **Step 3: 计算关联建议**：调用一次 `POST /v1/review/links/suggest`，只写建议表；随后复核原有记忆的指纹不变。
- [ ] **Step 4: 写最终报告**：`docs/EXTRACTION_V03B_REPORT.md`，内容只含计数和哈希，覆盖任务书第 7 节各项。
- [ ] **Step 5: 提交并按你的指示推送。**
