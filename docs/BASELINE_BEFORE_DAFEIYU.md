# 大肥鱼适配前的 ZeroMemory 基线

盘点日期：2026-10-30。仅记录计数、版本和校验值，不包含私人记忆正文。未重新初始化、未调用历史导入器。

| 项目 | 实际状态 |
|---|---|
| 存储 | SQLite FTS5，当前 v0.1.0；EverOS 仍为未接入的可插拔后端候选 |
| 身份 | `data/identity/current.json` 指向 v2；头像和版本文件已纳入保全 |
| 记忆 | 56 条，pending_review 56，approved 0；1 个独立 scope |
| 类型 | episodic 17、user_profile 14、working_context 9、semantic 8、procedural 5、relationship 3 |
| 来源 | chatgpt_conversation_summary 56；摘要证据不是逐字引文 |
| 原始聊天 | 0 条消息；未提供完整导出，未读取外部敏感包 |
| 基线测试 | 项目现有脚本实际运行：93 passed，28.50 秒；一个既有 TestClient 弃用告警 |
| 接口 | FastAPI、本机认证、scope token、Context Gateway；MCP 5 个只读/7 个含提议工具 |
| Web / 模型 | 原生 HTML/CSS/JS，无 CDN；真实 LLM 未配置，不自动调用云端 |

记忆 ID 集合 SHA-256：`ad4c966f27386f9eca108079efa522c4aba25e1eded530c3d65aa4d1c268fda8`。

一致性备份 ID：`9451cf5c-86eb-48ce-839b-c0b0bb082056`。
备份 SHA-256：`e45433124804e808a67311d5915e0ee9b735401a79d48bea73ece58fadbd8e26`。
已通过现有 Backups 恢复到隔离目录，核对旧表、来源文件、身份及原审计前缀完全一致。生产库未覆盖。私人路径和逐项指纹在 gitignored `output/v02/backup-verification.json`、`output/v02/baseline.json`。改动前源码另存于 `output/v02/source-before.zip`。

## 文件变更与逐阶段验证

1. 新增 `observatory/` 适配器与 API、`web/observatory.js`：只从 MemoryBackend 分页取数，复用认证；验证真实 56 条映射、越权、只读及限量渲染。
2. 新增 `activity/` 的加法迁移、状态和目标/证据服务：新表独立，不改写现有人格或记忆；验证事务、版本、防重、恢复与模型隔离。
3. 扩展 Context/MCP、Web 操作台与备份表清单：验证多模型共用、旧备份兼容及新目标恢复。
4. 新增针对性测试、使用说明和实际验收记录：每阶段执行 `scripts/baseline_snapshot.py --compare output/v02/baseline.json`，旧记忆正文、ID、审核、来源、修订、身份/头像字节及原审计前缀必须不变；新增操作记录单独统计。

当前代码全部未跟踪/未提交，按既有脏工作区处理；不自动提交或 push。
