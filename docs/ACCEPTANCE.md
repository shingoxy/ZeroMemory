# ZeroMemory v0.1 验收报告

本文件保留原 v0.1 与迁移验收；2026-10-30 的 v0.2 增量验收见文末。

验收日期：2026-10-29（Asia/Shanghai）。下文 v0.1 基线验收只使用合成对话；文末迁移增量验收实际读取了用户提供的通用摘要包，未读取敏感包，也未向外部模型发送数据。状态定义：PASS 为实际执行通过；BLOCKED 为缺少外部条件；DEFERRED 为明确留待后续版本。

## 阶段结果

| 阶段 | 状态 | 交付和证据 |
|---|---|---|
| Phase 0 调研初始化 | PASS | 检查 Windows、Python、uv、Node、Docker/Ollama；核实 EverOS 1.5.1 与 everos-mcp 0.1.1，记录固定提交；Git、.venv、uv.lock、配置模板与架构文档已建立 |
| Phase 1 持久化核心 | PASS（SQLite 降级） | 身份版本/回滚、来源与角色核验、FTS5、修订/审计、冲突和状态生命周期；真实重启后内容保持 |
| Phase 2 历史导入与审核 | PASS（模型提炼用 mock 验证） | ZIP/JSON/JSONL、多卷、ChatGPT 分支、筛选、批次断点、重复/增量、人工引用、严格 schema 提炼与审核、相似合并 |
| Phase 3 MCP/跨模型 | PASS（mock provider） | 真正的 stdio initialize/list_tools/call_tool；只读 5 工具和可写 7 工具；两模型读取同一 ID/证据/人格版本 |
| Phase 4 Web/运维 | PASS（Windows） | 中文六页管理界面、用户头像、实际浏览器导入/审核/检索、模型状态、备份/新目录恢复；PowerShell 脚本和 wheel 构建通过 |
| Phase 5 验收修复 | PASS（本地 MVP 范围） | 全部 69 项自动测试、lint、JS 语法、合成端到端演示通过；外部依赖及平台限制见下表 |

以上不表示 EverOS 主存储、真实 LLM 或向量检索已经验收。

## 自动测试

实际执行 `scripts/Test-ZeroMemory.ps1`：

- **69 passed，0 failed，0 skipped，34.02 秒**。JUnit：`output/junit.xml`。
- Ruff 检查通过；`node --check web/app.js` 通过。
- 所有 PowerShell 脚本解析 0 错误；`Setup.ps1 -Python C:\Python314\python.exe` 锁定安装已执行通过。
- 构建 `dist/zero_memory-0.1.0-py3-none-any.whl` 成功；检查 37 个成员，头像与 Web 资源存在，没有 data/output/cache 数据文件。
- 一个第三方测试依赖告警：Starlette 提示未来推荐 httpx2 TestClient。当前固定 httpx 0.28.1 的测试全部通过；未为消除告警引入第二套 HTTP 依赖。

测试覆盖：

| 验收点 | 结果 |
|---|---|
| 10,000 条合成消息解析、重复与增量导入 | PASS |
| ChatGPT 分支、空消息、富文本/工具、时间缺失、循环图与畸形 JSON | PASS |
| ZIP 路径穿越、盘符/绝对路径、压缩炸弹、成员/上传大小限制 | PASS |
| 无 Content-Length 的分块请求大小限制 | PASS，返回 413 |
| 身份明确授权、版本竞争、重启持久化、人格边界和状态确定性 | PASS |
| 删除/撤销/编辑后 FTS 不再返回旧活动记录 | PASS |
| 用户/项目隔离、只读 token、拒绝跨作用域证据、token 撤销 | PASS |
| 旧 Assistant 内容不能标成用户亲述，人工引用只接受用户原文 | PASS |
| 证据不在原文、来源 ID/角色/作用域不匹配或提炼 schema 无效 | PASS，拒绝入事实库 |
| 新旧设备冲突、同时创建候选后再批准、过期事实退出当前检索 | PASS |
| 相似人工合并保留各个来源，结果重新审核 | PASS |
| 注入文本保持低信任数据、上下文预算、中文问题关键词召回 | PASS |
| 本地模式不触发外部网络模型请求，云端关闭拒绝发送 | PASS |
| 云端明确启用仍需本次预览 hash + consent；变更/重放拒绝 | PASS |
| 自动提炼无模型时返回 503 且任务继续等待 | PASS |
| 备份可读、清单校验、密钥/令牌不入备份、损坏恢复不覆盖 | PASS |
| EverOS 不可达的明确降级、错误配置和目录错误 | PASS |
| 实际 MCP stdio 工具发现、读取和经验提议 | PASS |

## 10,000 条性能实测

最终运行见 `output/benchmark.json`：

| 指标 | 实测 |
|---|---:|
| 首次预览 + 解析入库 | 4.364 秒 |
| 重复导入 + 单条增量 | 4.645 秒 |
| 重复导入新增 | 0 |
| 重复数 | 10,000 |
| 增量新增 | 1 |
| Python tracemalloc 峰值 | 2.348 MiB |
| OS 进程峰值 working set | 93.613 MiB |

OS 数值包含 Python、已加载测试模块及原生分配，不是纯导入增量；Python heap 数值不代表总进程内存。单个超大 ChatGPT mapping 仍按一个会话处理，不据此宣称支持任意大小文件。

## 必须的端到端演示

`python scripts/acceptance_demo.py --output output` 已执行。报告：`output/acceptance-demo.json`。

1. 导入合成会话：“我正在开发星舟项目，目标是让本地记忆可追溯。”
2. 人工引用原文并批准。无模型时没有伪装成 LLM 提炼。
3. 修改幽默参数为 67，保存人格 v2；终止第一个 OS 服务进程。
4. 确认旧监听已经关闭后，在同一端口 8951 启动新 OS 进程（真实服务 PID 62736 → 50288），读取到 v2 和同一记忆。
5. model-a 和 model-b 两个独立本机 mock provider、独立 scoped token，均读取：`agent_id=zero-001`，`memory_id=f7923bb8-9ff3-4550-ae3b-cf9cc1acf365`，相同原文证据、人格 v2。
6. 用官方 MCP Python SDK 实际运行只读和可写 stdio 进程，发现工具并调用身份、搜索、经验提议；写入结果仍是 pending_review。
7. 导出备份、恢复到全新目录，启动另一个 OS 服务，核对记忆 ID、证据和幽默 67 一致。

mock A/B 只验证上下文继承和协议行为，不代表真实推理质量，也不承诺表达风格相同。

## 浏览器实操

使用独立 Chrome/Playwright 会话，仅连接回环服务。已实际操作：文件选择、合成 JSON 预览、开始导入、完成进度、人工引用、待审核批准、中文“星舟”检索、模型配置保存、生成备份、上传该备份并恢复到随机新目录。恢复响应 `activated=false`，未覆盖当前目录。

无模型点击提炼时显示明确配置提示；控制台对应的 HTTP 503 是预期结果，没有 JavaScript 未捕获异常。初次 favicon 404 已修复。截图位于 `output/playwright/overview.png`、`memory-search.png`、`settings.png`、`restore.png`、`mobile.png`。

头像原图与前端副本 SHA-256 一致：`C0EA6674BA32838E59C3D49FDDC0203D436E599F6ED76ACB7A13A3582577FD39`。没有修改原始资产。

## 实际运行与存储

默认本机地址：`http://127.0.0.1:8765`。正式数据目录 `E:\ZeroMemory\data`；数据库 `data/memory/zero.sqlite3`。基线验收时正式目录消息/记忆均为 0；当前迁移结果见文末，云端仍关闭。合成测试均在 gitignored `output/`。

```powershell
cd E:\ZeroMemory
.\scripts\Start-ZeroMemory.ps1
# 等价命令
.\.venv\Scripts\python.exe -m zero_memory --home E:\ZeroMemory\data --port 8765
```

前台启动后 Ctrl+C 停止。后台运行时应核对真正服务进程的 PID 与命令行；Windows venv 的 python.exe 可能是等待中的启动器，不能直接把启动器 PID 当作监听 PID。不要按所有 python.exe 批量结束进程。迁移 API 验收进程已在核验后停止。

环境和占用快照见 `output/environment.json`：Windows 11，Python 3.14.4；FastAPI 0.162.0，MCP 1.30.0，Pydantic 2.14.1。`.venv` 约 101.79 MiB，缓存约 159.34 MiB，Web 含头像约 2.11 MiB，合成验收输出约 102.37 MiB。未下载任何模型权重。小型生成模型与 Embedding 的 20 GB 为规划预算，真实大小取决于用户选择，不是本次占用。

## 剩余项

| 项目 | 状态 | 原因/下一步 |
|---|---|---|
| EverOS 真实主存储与官方 MCP 联动 | BLOCKED / 未集成 | 本机无服务/模型；仅完成官方文档和契约探测。先验证审核、ID、来源及完整删除的无损映射，再迁移单一主后端 |
| Ollama 真实生成、真实 LLM 自动提炼质量 | BLOCKED | 本机未安装 Ollama/模型，没有擅自下载；当前用 mock 验证接口、schema、证据和队列 |
| Embedding/向量语义检索 | DEFERRED | v0.1 真实关键词检索可用；未伪称向量能力 |
| 真实 Codex/Claude 应用注册 | DEFERRED | 提供可复制配置，SDK 的真实 stdio 已测试；没有擅自修改用户全局 MCP 配置 |
| macOS/Linux/WSL2 实机 | BLOCKED | 提供指南与兼容入口，当前只有 Windows 实测 |
| 任意文本矛盾检测、完备敏感分类、精确 tokenizer | DEFERRED | 当前 fact_key、敏感词和字符预算是保守规则，边界已记录 |
| 物理擦除来源/旧备份/所有证据副本 | DEFERRED | 活动删除与索引清理已实现；不会自动破坏用户原始文件和备份 |

v0.2 建议顺序：真实小模型质量与中文检索验收 → EverOS 无损迁移 → 更细致的人格/情绪演化 → 用户可审核的主动反思与长期关系记忆。保持模拟身份说明、来源追踪和权限边界。

## 迁移增量验收（2026-10-29）

- 最终执行项目现有 `scripts/Test-ZeroMemory.ps1`：**93 passed，0 failed，27.84 秒**；Ruff、JavaScript 语法、真实进程重启/MCP/双 mock/恢复演示通过。记录在 `output/migration-validation.log` 和 `output/junit.xml`。保留一个既有 Starlette TestClient 依赖弃用告警。
- 10,000 条合成消息：首次解析入库 3.392 秒，重复加增量 3.498 秒；重复 10,000，增量 1，Python heap 峰值 2.348 MiB，OS working set 峰值 93.5 MiB。
- 通用包已实际接入现有 `data/memory/zero.sqlite3`：累计 56 条、待审核 56 条、批准 0 条；重复导入新增 0、重复 56、失败 0。原 UUID、置信度、来源角色和摘要标记逐条核对一致；种子均为 v1。3 条 assistant_proposal 保持外部推断。
- 初次试导为 41 成功/15 时间格式失败。补充年/月精度支持并测试后，重试新增 15/重复 41/失败 0。保留初始错误报告；没有回滚或覆盖先入库的记录，也没有把年/月编造成确切日期。
- 身份 v2 添加迁移元数据；现有 soul/style 完全保留，未确认人格建议不生效。头像内容哈希与原资产一致。导入前备份保存在本地 `data/backups/`，准确路径见私人验收报告。
- 三组真实 API 溯源查询（pending_review）：ZeroMemory 3 条、OIS 8 条、本地 7 条。示例 UUID、源行号、完整元数据见 gitignored `data/review_queue/migration-acceptance-2026-10-29.json`。
- 实际导入时阻断网络 socket；未读取/导入 Private Review 包。未发现完整聊天文件，实际原始会话/消息均为 0。本地模型未配置，因此真实模型提炼未执行；本地 mock 提炼、摘要关联、人工取代、云端预览确认及批次中途切换模型的隔离已测试。
- 在临时回环服务中核对实际 56 条待审核、未认证 401、待审核不进入模型上下文、头像正确、无模型提炼 503。临时进程已停止；默认 8765 当时被现有 TCP 连接占用，未停止占用者。需要时可用 `scripts/Start-ZeroMemory.ps1 -DataHome E:\ZeroMemory\data -Port 8766` 启动另一回环端口。
- 本次新增页面按钮只完成 JS 语法和对应 API 验证，未重新进行浏览器点击实操；原有浏览器基线记录仍见上文。

## v0.2 大肥鱼适配增量验收（2026-10-30）

当前服务为 `http://127.0.0.1:8786`，版本 0.2.0；仅回环地址。原数据目录和数据库沿用，不重新初始化、不重复导入历史种子。

| 项目 | 实际结果 |
|---|---|
| 修改前基线 | 93 passed；备份 ID `9451cf5c-86eb-48ce-839b-c0b0bb082056`，隔离恢复一致 |
| 最终自动化 | **118 passed，0 failed，49.12 秒**；Ruff、app/observatory/activity JS 语法通过；一个既有 Starlette TestClient 弃用告警 |
| 原始记忆保全 | 56 条仍全部 pending_review；UUID 集合、原始 JSON、来源、修订/有效时间、原审计前缀均一致 |
| 身份/头像 | 身份 v2、人格/风格/状态文件及头像字节哈希未改变；未应用任何新的人格建议 |
| 星海 | 实际展示既有 56 条；OIS 搜索 8 条，来源摘要原始行及置信度可查，缺原文明确待核实 |
| 只读 | 重复快照、检索、详情、时间线、模拟未来/巩固/重置、浏览刷新后保全指纹一致 |
| scope / 敏感 | 不同 scope 拒绝；敏感默认隐藏；普通客户端不见待审核项；产物正文仅管理员校验后可读 |
| 目标 / 经历 | 实际新增 1 个公开资料研究目标、1 个本地只读运行、1 条 verified/executed 经历及 1 个产物；目标保持 paused 等人工验收 |
| 重启恢复 | 真实 OS 进程重启前后目标 v4、检查点和唯一经历保持；running 中断恢复/停止竞态另有合成测试，不重放工具 |
| MCP / 模型切换 | 12 只读 / 20 含提议工具，官方 SDK 实际调用；两临时 scoped 客户端读取相同实际身份/目标，令牌已撤销；模型自我批准被拒绝 |
| 方法 / 兴趣 | 合成用例验证独立来源去重、不接受模型自述、不同目标的新产物、失败、修订/撤销、证据损坏失效；生产兴趣/方法均未虚构添加 |
| 备份恢复 | 新目标和实际产物连同旧记忆在隔离目录恢复一致；v0.1 无扩展表备份前向恢复通过 |
| Web | 1600×1080 / 390×844 实测；真实星海、线索、时间线、模拟、目标、经历、人格和方法页面可用；手机横向宽度 390/390，控制台 0 错误/0 告警 |
| 发行包 | 离线构建 wheel/sdist 成功；53 / 89 个成员；含新 Web 资源和第三方声明，不含 data/output/cache/环境文件或 token |

最终 ID 集合 SHA-256 仍为 `ad4c966f27386f9eca108079efa522c4aba25e1eded530c3d65aa4d1c268fda8`。新操作记录和扩展审计为本轮合法新增，未写入统一长期记忆作为已批准事实。完整私人验收报告与实际目标/产物 ID 在 `output/v02/acceptance.json`，基线及逐阶段比较在同目录；不把私人内容写入本报告或 Git。

最新 10,000 条合成消息回归：首次解析入库 3.757 秒，重复加单条增量 3.582 秒，重复 10,000 / 增量 1，Python heap 峰值 2.344 MiB，OS working set 峰值 120.797 MiB。星海另以 1,000 条合成记录验证分页，上限 500、后端每页不超过 200；浏览器真实数据为 56 条，不宣称 10,000 节点渲染性能。

开发中修复了新增脚本的初次加载顺序、手机长回执 ID 换行、日期范围结束日边界、重复兴趣来源保持审核、同结果方法重试，以及聊天发送目标与预览的绑定。最终测试与浏览器记录均为修复后的结果。离线构建改用工程已有 uv 缓存后通过，没有下载新依赖。

未实现/未验证：后台 scheduler、网络动作/外部写入、QQ/B站/手机渠道、真实 LLM 研究与方法质量、真实长期多目标效能、真实手机设备及其他浏览器。方法复杂表单的每一种提交组合没有逐项浏览器点击，相关 API 流程和拒绝条件已测试。调度相关的免打扰/成本/提醒预算不在当前手动本地闭环中，不能描述为已启用。

参考提交与采纳边界见 [DAFEIYU_ADOPTION.md](DAFEIYU_ADOPTION.md)，使用与非破坏性开关回退见 [V02_GUIDE.md](V02_GUIDE.md)。本轮没有读取外部敏感包、调用云端模型、修改全局 MCP 配置、执行外部账号操作、push 或部署公网。
