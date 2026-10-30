# ZeroMemory v0.1 架构

## 当前可运行形态

```text
浏览器中文管理界面 ── REST + 本机管理会话 ──┐
Codex / Claude ── stdio MCP ── 作用域令牌 ─┤
                                         ↓
                           FastAPI ZeroMemory Service
                         Identity / Context / Review / Policy
                          ↓          ↓             ↓
                    版本化身份    MemoryBackend    导入与提炼任务
                    YAML/JSON     SQLite FTS5      原始 ZIP/JSON/JSONL
                                         ↓
                           可选 Ollama / OpenAI-compatible
```

入口 `python -m zero_memory`，默认 `127.0.0.1:8765`。Web 由同一服务提供原生 HTML/CSS/JS，运行无需 Node、前端编译或 CDN。初始头像来自用户的 `assets/zero-avatar.png`，原图保留，前端使用副本。

## 权威数据与一致性

- 降级后端的 SQLite 为记忆、修订、审计、规范化消息和任务的唯一事实源；FTS5 是事务内更新的可重建索引。v0.1 不同时向另一个系统写入相互竞争的事实。
- `identity/versions/*.json` 是提交后的不可变身份快照，`current.json` 是原子提交指针。`soul.yaml`、`style.yaml` 是当前快照的可读投影，启动时自动校正。`state.json` 独立持久化模拟状态。请通过 API/UI 编辑，直接改投影文件不会绕过版本审核。
- 所有写操作共享项目数据目录内的跨进程文件锁；数据库每个操作使用独立连接和事务。每 200 个导入事件提交一次，并在同一事务保存断点。失败的批次不会推进断点。
- 备份持有相同写锁，导出 JSONL 表、身份快照、原始文件、Markdown 阅读副本及 SHA-256 清单；恢复重新建立数据库与 FTS，不复制运行中的数据库文件。

## EverOS 集成边界

2026-10-29 核实官方 EverOS **1.5.1**，提交 **5b0979b1b7f3a0cbe235756ff098515ec490753e**；官方 `everos-mcp` **0.1.1**。没有安装或启动这些外部服务，也没有修改其表结构。

官方文档确认 `/api/v2/memory/add`、`flush`、`search`，关键词搜索需指定 `method=keyword`。`everos-mcp` 为独立 stdio/HTTP 进程，通过 HTTP 访问运行中的 EverOS；它不会替用户启动后端。官方文档同时说明自托管 `forget_session` 尚不可用，索引更新具有延迟。

`memory/everos.py` 实现真实的本地 `/health` 与 `/openapi.json` 契约探测，核对 v2 端点。**这不是已完成的 EverOS 主存储实现**。在无模型、无服务，且审核、溯源、完整删除的无损映射未经验证时，`ZERO_MEMORY_BACKEND=everos` 明确报告降级，并继续使用本地 SQLite。不会把 HTTP 接收成功说成审核持久化成功。

未来集成需实现 `MemoryBackend` 的完整事务语义，先用可读备份迁移并比对 ID/证据，再切换单一主后端；v0.1 不自动迁移、不自动上传。向量检索、EverOS reflection 和原生技能提炼未启用。

## 上下文和跨模型

上下文依次包含可信边界、当前人格和授权范围、任务、带角色及证据标记的相关已批准记忆、当前会话历史及最后一条问题。全部外部内容作为低信任消息数据，不拼成系统指令。历史以用户/assistant 原角色序列化成资料，分支导出不会被线性拼接。

预算以 Unicode 字符数加消息开销保守估计，确定性裁剪，不声称是精确 tokenizer 计数。本地检索为中文 trigram FTS 与短词字面子串搜索；分数 1 表示关键词匹配，0 表示无关键词列表，不是向量相似度。自然语言召回目前较简单。

两个 mock provider 和两份独立 scoped token 的真实 HTTP/stdio 验收验证了同一 `zero-001`、`memory_id`、证据、人格版本。模型的语言风格与事实推理质量仍由模型决定；mock 测试不能证明真实模型能力。

## 官方依据

- [EverOS 1.5.1 源码](https://github.com/EverMind-AI/EverOS/tree/5b0979b1b7f3a0cbe235756ff098515ec490753e)
- [固定提交的 MCP 文档](https://github.com/EverMind-AI/EverOS/blob/5b0979b1b7f3a0cbe235756ff098515ec490753e/docs/mcp.md)
- [EverOS-MCP 官方仓库](https://github.com/EverMind-AI/EverOS-MCP)
- [EverOS PyPI](https://pypi.org/project/everos/1.5.1/)、[everos-mcp PyPI](https://pypi.org/project/everos-mcp/0.1.1/)
