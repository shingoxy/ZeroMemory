# 数据模型与生命周期

## 记忆

`Memory` 的严格 Pydantic schema 定义于 `src/zero_memory/schemas/models.py`。完整字段包括：

`memory_id`、`agent_id`、`user_id`、`project_id`、`memory_type`、`content`、`source_type`、`source_conversation_id`、`source_message_ids`、`source_message_keys`、`source_role`、`evidence`、`event_time`、`extracted_at`、`valid_from`、`valid_to`、`confidence`、`status`、`sensitivity`、`inference_type`、`version`、`supersedes`、`fact_key`、`conflicts`、`duplicate_candidates`、`evidence_verified`、`merged_from`、`merged_evidence`。

七类记忆：用户画像、Agent 经验、关系、事件、知识、方法、工作上下文。来源分别区分手工陈述、ChatGPT 导出、通用导入、MCP 客户端及本地聊天。`user_statement`、`third_party_statement`、`assistant_inference`、`system_summary` 不等价。MCP 写入强制记为外部 assistant 推断。

`source_message_keys` 精确指向某个不可变消息版本，同一 message ID 被编辑后原版本仍可追溯。证据须为其中某条消息的连续原文片段；角色、消息 ID、会话 ID 和作用域均须匹配。`evidence_verified=true` 表示完成这些结构校验，并不保证摘要语义真实。

## 审核和版本

```text
提议 → pending_review → 人工批准 → approved
                  └→ rejected
approved → 编辑 → pending_review（新修订）
approved → revoked（保留历史，从活动检索移除）
approved → superseded（较新事实取代，保留有效时间与 supersedes 链）
任意活动记录 → delete（清理记录、修订与 FTS，审计仅保留 ID）
```

当前检索只返回 `approved` 且在有效期内的记录。管理界面可按状态查看历史。人工编辑需 `expected_version` 防止覆盖他人新修订；撤回历史内容也生成新的待审核版本。自动合并只用于精确重复的同来源提议；相似内容提供候选供人工确认。人工合并生成新的待审核记忆，`merged_evidence` 保留来源，批准前不改变旧记录。

同一 `fact_key` 出现新值会标记冲突；批准时再次校验，防止同时创建的候选绕过冲突。指定 `supersedes` 后较早事件不得取代已知较新事件。缺失时间不会伪造历史发生时间，审核时开始有效。

## 会话与任务

- `sources/<sha256>.json|jsonl|zip`：原始文件，不执行、不请求其中 URL。
- `messages`：按作用域、会话 ID、稳定消息 ID、完整内容/元数据哈希去重。规范化记录包含角色、时间、节点 ID、父节点、children 元数据及原始消息对象。
- `conversations`：标题、当前节点、会话创建/修改时间。所有分支保留，不冒充单一线性会话。
- `imports`：预览、选择条件、扫描断点、成功/重复/失败统计与可读错误。
- `extraction_tasks`：`waiting_llm`、`skipped_sensitive`、`completed`、`failed`。人工引用不会伪装成 LLM 任务完成。
- `revisions`：记忆每次修订的完整内容。`audit`：时间、动作、操作者、目标 ID、必要元数据，不记录密钥或整段对话。
- `clients`：客户端令牌的 SHA-256 和授权范围，明文只创建时返回。
- `chat_messages`：本机聊天的作用域、session ID、角色和内容，仅成功模型调用后保存。

## 备份与遗忘

便携 ZIP 含 JSONL、身份、来源、Markdown、校验清单；数据库和 FTS 在新目录重建。备份不含模型密钥、管理员令牌、客户端令牌或模型启用配置。原始来源和备份可能包含已删记忆；删除一个记忆不是对所有磁盘副本的密码学擦除。合并记忆中显式保存的证据副本也需单独审核清理。详见 SECURITY。

## 迁移扩展（2026-10-29）

沿用现有 memories/revisions/audit/messages/imports 表，不建立另一份事实数据库。Memory/Proposal 增加 `claim_kind`、`origin_quality`、`topic`、`review_note`、`dedup_key`、`source_artifact` 和 `summary_links`。旧行缺失这些字段时使用默认值，备份/恢复与历史撤回兼容旧记录。摘要来源类型为 `chatgpt_conversation_summary`，混合角色为 `mixed`；不能伪装为已验证逐字证据。

`source_artifact` 引用 `sources` 内的内容寻址 JSONL 行及 ZIP 哈希，原始字段留在只读证据文件；规范化值与原值可比对。`summary_links` 关联同一作用域的摘要，只有原始用户证据通过验证的候选才能关联。未人工确认取代关系时，不改动摘要状态或版本。

`event_time_original` 和 `event_time_precision` 保留种子的原始时间文本与 year/month/day/instant 精度。只有年或月时 event_time 为 null，不虚构确切日期；原始 JSONL 同时保留输入值。

## v0.2 操作状态扩展

只新增 `v02_migrations`、`goals`、`goal_events`、`episodes`、`activity_sources`、`activity_artifacts`、`activity_runs`、`agent_interests`、`research_methods`、`method_revisions`、`method_attempts`、`persona_states`、`persona_events`、`persona_proposals`。scope 由服务端复用既有认证校验；正文使用严格输入模型校验后的 JSON，原有表不回填、不重写。

`PRAGMA user_version` 从已知的 0 迁至 2，扩展自身版本为 1 并记录定义哈希。未知版本或列布局不匹配即拒绝；加法 DDL 与版本标记在同一事务。已有非空库启用扩展前先备份。关闭扩展保留表和证据，不自动降库版本。

目标每次变化递增版本，保存原因、操作者和检查点；完成要求实际产物及用户确认。运行 request_id 在 scope 内唯一，重试返回原回执。重启只暂停 running 并记录 interrupted，不自动重发工具。事件陈述不具有实际执行资格；真实本地工具生成的 verified/executed 经历才能引用有文件 SHA-256 的产物。

自身兴趣按独立 origin_key 计证据，重复来源不会增加等级或重置已有审核。方法有不可覆盖修订，尝试在目标内唯一，成功须在不同目标上有尝试之后的新产物；回查文件、源版本和材料哈希，失效证据不计当前成功次数。方法仅为操作建议。

Memory 增加 `source_type=agent_activity` 及 `activity_goal_id/activity_episode_ids/activity_artifact_ids` 引用。此类提议先核验同 scope 的目标、真实经历及产物，再由统一后端保存为待审核记忆；不复制目标状态作为第二份权威。仍须人工确认后才能用于长期召回。旧记录缺失字段时使用默认值，原始已存 JSON 不改写。

新模拟状态保存在 `persona_states`，`identity/state.json` 作为保留的旧基线不改写；事件变更有审计，读取衰减为纯投影。人格提议单独排队，只有明确的人格补丁授权才由既有 Identity 版本机制应用。

