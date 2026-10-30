# 给 Codex 的任务：导入「零的记忆」迁移包

请将当前 `ZeroMemory_Chat_Migration_Kit` 作为 ZeroMemory 原工程的**增量数据迁移材料**，不是新建一套独立的记忆数据库。

1. 先阅读原有 `ZeroMemory_Codex_Master_Prompt.md` 和当前仓库的 Pydantic/数据库 Schema；对齐本包 `seed/memories_pending_review.jsonl`、`zero_identity.json`、`data_dictionary.json`。
2. 将 `zero_identity.json` 合并到零的身份配置（`agent_id=zero-001`），不得覆盖仓库中更新过或用户确认的字段。头像路径 `assets/zero-avatar.png`，复制到项目受控资源目录。
3. 导入种子记忆为 `pending_review`，保留 UUID、来源、置信度、claim_kind、摘要溯源标记；禁止未经审核直接写为 approved/事实。尤其不能把旧版 ChatGPT Assistant 的回答当作零自身经历。
4. 记忆幂等：根据 `memory_id` 和 `dedup_key` 去重；未来完整对话导入后应链接/取代摘要，不生成矛盾双份事实；仅在保留原始证据后更新版本。
5. 不自动读取或导入外部 `ZeroMemory_Private_Review` 包。只有用户确认具体内容时才按审核队列处理。
6. 增加对 `conversation_message` JSONL 的导入支持；先持久化原始会话，按真实 user/assistant role 和时间顺序建立可审计引用，然后使用用户指定的本地模型提炼记忆。未配置 LLM 时原始会话仍应完成导入并可检索。
7. 遇到 HTML、图片、音频或附件标记，不能自行虚构内容。导出 JSONL 中只包含文本；以后可以开发附件插件。
8. 控制隐私：默认不联网、不向云端发送原文、不把资料写入 Git，数据路径进 `.gitignore`；公开 API 不允许被外网无认证访问。
9. 完成后提供已导入条数、待审核条数、重复/失败条数、原始会话数，以及三组可追溯示例查询。

## 导入本包之外的完整历史记录

如果用户下载了官方 ChatGPT 数据 ZIP，先在本机执行（示例 Windows PowerShell）：

```powershell
python .\tools\convert_chatgpt_export.py --input "D:\Private\chatgpt-export.zip" --output "D:\Private\zero-import\chatgpt_messages.jsonl"
```

再通过 ZeroMemory 导入页面或导入命令读取 `chatgpt_messages.jsonl`，不要将该数据上传至 GitHub 或发给远程模型。转换工具只是处理结构，事实提炼和人工审核由 ZeroMemory 完成。

## 必须通过的测试

- 导入本包两次，记忆数不重复。
- 同一条 ChatGPT 消息中的 Assistant 断言不能默认成为用户事实。
- 包中 `assistant_proposal` 不能作为用户确认的人格设定。
- 云端模型切换不会自动发送敏感信息。
- 原始对话中的新证据与旧摘要冲突时，产生审核任务而非静默覆盖。
- 反复导入相同 ZIP，`dedup_key` 幂等。
- 附带 `tests/test_converter.py` 可以离线运行通过。
