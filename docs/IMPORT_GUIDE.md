# ChatGPT 与通用对话导入

## 通用迁移包（身份与摘要种子）

在工程目录运行，不需要解压，不会搜索或读取相邻的 Private Review 包：

```powershell
.\.venv\Scripts\python.exe -m zero_memory.importers.migration --bundle .\ZeroMemory_Chat_Migration_Kit_2026-10-29.zip --home .\data
```

使用现有 SQLite 数据库。先验证 ZIP 路径、成员清单、字节数和 SHA-256，再将原 ZIP 与种子 JSONL 保存在 `data/sources/<SHA-256>.*`。每条记忆保留原 UUID、置信度、claim_kind、topic、review_note 及 `source_artifact`（原文件哈希、包哈希、成员名、行号、原记录哈希）。`low` 保守映射为 `private`，原值仍在原始 JSONL 中；仅日期的时间按 UTC 零点规范化；仅年份/月的时间不补造日或时刻，event_time 留空，event_time_original 和 event_time_precision 保存原值与精度。所有原字段仍可溯源。

所有种子强制 `pending_review`、`evidence_verified=false`、`origin_quality=summary_only`，摘要证据不是逐字引文。`assistant_proposal` 保留为外部推断。记忆根据 UUID 与作用域内 dedup_key 去重；原包没有 dedup_key 时，根据作用域、内容、类别、claim_kind、角色、主题派生稳定键。重打 ZIP 不改变结果；重复导入不会重置人工批准、驳回或取代状态。同 UUID 或同键但原始内容变化时，保留已有记忆，报告 `seed_changed` 供人工核对；新来源也会留存。

已有身份的 soul/style 字段优先保留。迁移的起始日期、记忆语义、来源与未确认人格建议写入新的身份版本 `migration` 元数据，建议不会自动修改核心人格；后续手工修改也保留该元数据。头像复制到 `data/identity/assets/`，通过认证接口读取，并随身份备份。运行报告在 `data/review_queue/migration-*.json`。

## 完整聊天 JSONL 与离线命令

本包不含完整历史聊天。可使用迁移包提供、已纳入工程的离线转换器：

```powershell
.\.venv\Scripts\python.exe .\tools\convert_chatgpt_export.py --input 'D:\Private\chatgpt-export.zip' --output 'D:\Private\zero-import\chatgpt_messages.jsonl'
.\.venv\Scripts\python.exe -m zero_memory.importers --input 'D:\Private\zero-import\chatgpt_messages.jsonl' --home .\data
```

也可以在下面的导入页面选择 `chatgpt_messages.jsonl`。`record_type=conversation_message` 显式映射为 ChatGPT 来源，保留真实角色、created_at、会话标题、分支策略、dedup_key 和完整输入记录；消息按会话和真实时间检索。Assistant 消息不能进入用户提炼队列，can_be_user_fact 依照真实角色重新计算。文件重命名仍去重，同一消息内容变化保留两个版本。原始聊天无需模型即可持久化和检索：`GET /v1/messages?query=关键词`（管理员认证，按用户/项目隔离）。

转换器保留 user/assistant 文本，有 current_node 时仅输出活跃分支。需要全部分支、空消息及工具描述时，直接向 ZeroMemory 导入官方 ZIP/JSON，原有导入器会保留全部 mapping 节点。转换器不会转录附件；单个 JSON 卷在内存中转换。默认导入限制仍为 64 MiB，不表示支持任意大小导出。

官方 ZIP 优先识别 `conversations.json` 和 `conversations-000.json` 等编号聊天卷；`conversation_asset_file_names.json`、共享链接和分组元数据不作为消息解析。原 ZIP 仍整体留存在本机来源目录。大型导出应先检查 CRC、路径、展开尺寸并备份；一次性本地导入限额不改变 Web 的默认上传限制。

原文入库仅排队，不自动调用模型。配置用户指定的本地 Ollama/OpenAI-compatible 模型后，预览并分批提炼，每条候选仍需人工审核。旧摘要作为不可信数据交给提炼器，模型可返回 `summary_links`；精确事实键与文本相似度也会保守关联。不同内容标记冲突，相同内容也要求明确取代旧摘要，避免两份有效事实。批准时传入 `supersedes`，原摘要生成历史版本，原始证据和新消息引用均保留。多条摘要需先人工合并或逐条整理。

这不是完备的语义矛盾识别器：没有模型时只能进行事实键和文本相似度检查；模型每条消息最多看到 10 条相近摘要，漏关联可在提议/编辑 API 的 `summary_links` 中人工补充。模型切到云端不会发送数据，每批发送仍须匹配完整预览并明确确认；默认 allow_cloud=false。

## 页面导入步骤

1. 在 ChatGPT 中导出数据，将 ZIP 保存到本机。不要把导出文件放入源码或提交 Git。
2. 启动 ZeroMemory，打开“导入对话”。先选定右上角用户和项目。
3. 选择 ZIP、`conversations.json`、分卷 JSON 或 JSONL，点击“解析并预览”。此步将原文件留存到本机 sources 隔离区并统计，**还不写入规范化会话或记忆**。
4. 检查会话、消息、大小、预计新增数。可勾选会话及 UTC 日期范围，默认跳过敏感内容的自动提炼。
5. 点击“开始 / 继续离线导入”。任务每 200 个事件提交并更新断点。失败或服务重启后可继续，同一选择条件保持不变；改变筛选请重新预览。
6. 无模型时，原始用户消息可点击“引用原文为候选”；转到“审核工作台”查看证据、编辑并批准。
7. 有本地模型时，在“模型与系统”配置 Ollama，再预览并处理最多 10 条消息的一批提炼任务。结果仍需审核。云端默认关闭，开启后每一批还必须预览并确认发送。

## 通用 JSONL 示例（完全虚构）

```json
{"role":"user","content":"我正在开发星舟项目。","timestamp":"2026-10-29T09:00:00+08:00","conversation_id":"synthetic-c1","message_id":"synthetic-u1"}
{"role":"assistant","content":"这是合成测试回复。","conversation_id":"synthetic-c1","message_id":"synthetic-a1"}
```

也接受消息数组、`{messages:[...]}` 会话、`{conversations:[...]}` 包装，以及 ChatGPT `mapping` 会话/会话数组。ZIP 支持 `conversations*.json` 多卷和 JSONL；存在 conversations 文件时不会误把 user.json 等账户元数据当消息。附件和工具对象保留为惰性描述，不下载附件、不运行工具。

## 限制与安全

- 默认上传 64 MiB，ZIP 单成员 128 MiB、展开总计 256 MiB、最多 1000 项、最大压缩比 100（1 MiB 以下小成员另行容忍），拒绝绝对路径、盘符、`..`、链接、加密和重复成员。
- 服务同时限制无 Content-Length 的分块请求，JSON API 默认 2 MiB。JSON/JSONL 逐记录读取；一个 ChatGPT conversation 的 mapping 在内存中处理，超大单一会话仍受文件限制，不是任意大小的流式图引擎。
- 中文短词使用字面子串查询，不依赖 Embedding。自然语言问题可先使用明确关键词检索。
- 缺失时间保留 null；启用日期筛选时排除缺失时间的消息。时区缺失的通用输入按 UTC，来源原字段仍保留。
- 同一稳定 ID 和内容重复导入新增 0；同 ID 内容变化保留新旧两个版本。没有 ID 时以位置和内容构造稳定替代 ID，因此任意重排的无 ID 文件不能保证全局去重。
- 进度中的“待提炼”为该次导入时排队数；当前队列状态以系统页面统计为准。
- 敏感词跳过是保守启发式，不是完备的隐私分类器。所有提炼候选均待审核；原始敏感内容仍保存在本地来源。

## 合成验收

`python scripts/acceptance_demo.py --output output` 会产生独立合成数据目录，完成导入、引用、审核、真实进程重启、双 mock 模型、MCP 及新目录恢复；不会读入个人聊天。

## 审核工作台与批次工具

审核工作台分为待审核、稍后处理、低优先级、敏感队列和提炼批次五个视图。批量操作须先选类别或勾选候选，预览数量与抽样证据后再确认执行；敏感候选和带时间冲突建议的候选不参与批量批准。按批次撤销只驳回仍待审核的候选，先显示预演计数，再输入确认串执行。

云端批次执行时可加 `--max-pending N`，待审核数达到 N 时暂停，不发送请求。失败的请求记录原因码，如 `multiple_memories`、`schema:<字段>:<错误类型>`，只含字段名和错误类型，不含正文；`--status` 按状态和原因分别计数。
