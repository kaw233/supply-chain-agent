# 业务成品完整 API 目录

HTTP 路由唯一注册文件：`app/api_contract.py`。
运行时：`GET /api/catalog`、`GET /api/openapi.json`、`/api-docs`。
默认测试：`python scripts/test_api.py --isolated --report reports/api-report.json`。
已启动服务：`python scripts/test_api.py --base http://127.0.0.1:8765 --report reports/read-only.json`。
后者只读；不将未执行的发送、批准、停止标记为通过。

| 方法 | 路径 | operationId | 副作用 | 用途 |
|---|---|---|---|---|
| GET | `/api/health` | `health` | read | 运行与数据库状态 |
| GET | `/api/app` | `appInfo` | read | 应用清单与默认场景 |
| GET | `/api/scenarios` | `listScenarios` | read | 列出已安装业务场景 |
| GET | `/api/capabilities` | `listCapabilities` | read | 当前场景可调用能力 |
| POST | `/api/capabilities/invoke` | `invokeCapability` | conditional | 业务查询、检查、草稿或 Hermes Skill 委托 |
| GET | `/api/settings` | `getSettings` | read | 读取脱敏连接和自动化设置 |
| POST | `/api/settings` | `updateSettings` | local-write | 修改本实例设置 |
| POST | `/api/connections/test` | `testConnection` | external-read | 只读探测当前 Agent 运行时；不代表模型联调 |
| GET | `/api/agent/tasks` | `listAgentTasks` | read | 查看原任务，不启动执行 |
| POST | `/api/agent/tasks` | `submitAgentTask` | external-write | 发起真实 Agent 任务 |
| GET | `/api/agent/sessions` | `listSessions` | read | 当前业务会话 |
| POST | `/api/agent/sessions` | `createSession` | local-write | 创建本地会话，不发送对话 |
| GET | `/api/agent/tasks/{task_id}` | `getAgentTask` | read | 查询一次执行的当前事实 |
| GET | `/api/agent/tasks/{task_id}/events` | `agentEvents` | read | 按游标读取已记录工具事件 |
| POST | `/api/agent/tasks/{task_id}/cancel` | `cancelAgentTask` | external-write | 只停止对应 Agent 执行，数据计算继续 |
| POST | `/api/agent/tasks/{task_id}/reply` | `replyAgentRequest` | external-write | 回复匹配的批准或补充信息请求 |
| POST | `/api/agent/tasks/{task_id}/observe` | `observeAgentTask` | external-read | 重新核对原远端任务，不重发 |
| POST | `/api/agent/tasks/{task_id}/continue` | `continueAgentTask` | external-write | 用户明确发起后续步骤 |
| GET | `/api/agent/sessions/{session_id}/messages` | `sessionMessages` | read | 查询会话消息 |
| GET | `/api/agent/sessions/{session_id}/snapshot` | `sessionSnapshot` | read | 会话与任务一致性快照 |
| GET | `/api/hermes/sessions` | `listRemoteSessions` | external-read | 列出 WebUI 远端会话 |
| POST | `/api/hermes/bind` | `bindRemoteSession` | external-read | 绑定已存在远端会话 |
| POST | `/api/bridge/{task_id}` | `invokeBridge` | conditional | 任务范围内的工作台桥接；见 x-bridge-operations |
| POST | `/api/data/ingest` | `ingestData` | conditional | 写入业务输入并触发计算；自动任务视设置 |
| POST | `/api/integrations/feishu/{scenario_id}/batches` | `ingestFeishuBatch` | conditional | 接收定时飞书 CLI 产物清单；不调用飞书 API |
| GET | `/api/integrations/feishu/{scenario_id}/artifacts` | `listFeishuArtifacts` | read | 查看已接收的定时飞书产物清单 |
| GET | `/api/rag/capabilities` | `ragCapabilities` | read | RAG 适配器能力与联调状态 |
| GET | `/api/rag/collections` | `listRagCollections` | read | 列出 RAG Collection |
| POST | `/api/rag/collections` | `createRagCollection` | local-write | 创建 RAG Collection 配置 |
| GET | `/api/rag/collections/{collection_id}` | `getRagCollection` | read | 读取 RAG Collection |
| GET | `/api/rag/collections/{collection_id}/documents` | `listRagDocuments` | read | 列出 RAG 文档元数据 |
| POST | `/api/rag/collections/{collection_id}/documents` | `registerRagDocument` | local-write | 登记 RAG 文档元数据 |
| GET | `/api/rag/documents/{document_id}` | `getRagDocument` | read | 读取 RAG 文档元数据 |
| GET | `/api/rag/documents/{document_id}/chunks` | `listRagChunks` | read | 列出已建立索引的 RAG Chunk；框架阶段为空 |
| POST | `/api/rag/collections/{collection_id}/jobs` | `createRagJob` | local-write | 创建 RAG 计划任务；当前不执行采集或索引 |
| GET | `/api/rag/collections/{collection_id}/jobs` | `listRagJobs` | read | 列出 RAG 计划任务 |
| GET | `/api/rag/jobs/{job_id}` | `getRagJob` | read | 读取 RAG 任务 |
| POST | `/api/rag/collections/{collection_id}/query` | `queryRagCollection` | external-read | 执行 RAG 查询；未配置时不返回虚假召回 |
| GET | `/api/rag/queries/{query_id}` | `getRagQuery` | read | 读取 RAG 查询审计结果 |
| GET | `/api/rag/queries/{query_id}/citations` | `getRagCitations` | read | 读取 RAG 查询引用证据 |
| POST | `/api/rag/documents/{document_id}/chunk-plan` | `previewRagChunks` | local-write | 预览确定性文本分块；不采集、不持久化 |
| POST | `/api/triggers/fire` | `fireTrigger` | conditional | 统一数据、按钮和对话检查入口 |
| GET | `/api/events` | `businessEvents` | read | 业务事件游标 |
| GET | `/api/scenarios/{scenario_id}/dashboard` | `dashboard` | read | 指标、图表和当前业务投影 |
| GET | `/api/scenarios/{scenario_id}/records` | `records` | read | 明细或原始记录 |
| GET | `/api/scenarios/{scenario_id}/config` | `scenarioConfig` | read | 业务定义、处理规则与样例 |
| GET | `/api/scenarios/{scenario_id}/cases` | `cases` | read | 当前问题列表 |
| POST | `/api/scenarios/{scenario_id}/pull` | `pullSource` | conditional | 读取配置的数据源并计算 |
| POST | `/api/scenarios/{scenario_id}/drafts` | `draftConfig` | local-write | 试算配置并生成差异草稿 |
| GET | `/api/scenarios/{scenario_id}/revisions` | `configRevisions` | read | 配置版本历史 |
| POST | `/api/scenarios/{scenario_id}/rollback` | `rollbackConfig` | local-write | 显式回滚配置，不撤销现实动作 |
| POST | `/api/drafts/{draft_id}/publish` | `publishDraft` | local-write | 校验后应用业务配置 |
| GET | `/api/drafts/{draft_id}` | `getDraft` | read | 查看固定草稿 |
| GET | `/api/cases/{case_id}` | `caseDetail` | read | 问题详情、依据和反馈 |
| PATCH | `/api/cases/{case_id}` | `updateCase` | local-write | 登记反馈或验收；不以消息发送替代关闭 |
| POST | `/api/actions/{action_id}/execute` | `executeAction` | external-write | 确认后将动作交给 Hermes Skill |
| GET | `/api/actions/{action_id}` | `getAction` | read | 读取草稿、执行和分项回执 |
| PATCH | `/api/actions/{action_id}` | `editAction` | local-write | 修改草案，旧确认不覆盖新内容 |
| POST | `/api/runs/{run_id}/replay` | `replayRun` | local-write | 只读重新计算旧快照，不重复执行动作 |
| GET | `/api/runs/{run_id}` | `getRun` | read | 输入、规则、参数和结果快照 |
| GET | `/api/schema` | `capabilitySchema` | read | 业务接口和桥接目录 |
| GET | `/api/catalog` | `apiCatalog` | read | 全部已注册 HTTP 操作及效果分类 |
| GET | `/api/openapi.json` | `openapiSpec` | read | 与运行路由同源的 OpenAPI 3.1 |
| GET | `/api/scenarios/{scenario_id}/workspace` | `dataWorkspace` | read | 固定时点的目标与筛选口径 |
| GET | `/api/scenarios/{scenario_id}/versions` | `dataVersions` | read | 按时间分页列出真实数据版本 |
| GET | `/api/scenarios/{scenario_id}/tables` | `dataTables` | read | 列出该快照全部原始表、中间表、结果表 |
| GET | `/api/scenarios/{scenario_id}/tables/{table_id}/rows` | `tableRows` | read | 固定快照与过滤，游标分页读取记录 |
| GET | `/api/scenarios/{scenario_id}/tables/{table_id}/lineage/{row_key}` | `rowLineage` | read | 结构化形成链路与当时规则 |
| GET | `/api/scenarios/{scenario_id}/rules-view` | `rulesView` | read | 实际执行的规则、参数、处理代码及原文依据 |
| POST | `/api/scenarios/{scenario_id}/quick-prompt` | `quickPrompt` | read | 准备带当前快照和对象的 Hermes 提示词；不执行 |
| POST | `/api/scenarios/{scenario_id}/snapshot-notes` | `snapshotNote` | local-write | 在历史快照上追加复盘意见，不改历史数据 |
| POST | `/api/scenarios/{scenario_id}/data-bundle` | `ingestDataBundle` | conditional | 批次接入多张来源表并运行处理 |

## 桥接子操作

HTTP 操作 `invokeBridge` 内的子操作也需测试；目录见 inventory.json 的 bridge_operations。
## 扩展规则
新增路由必须登记 operationId、输入、响应、副作用、样例；成功响应断言写入 api/test-plan.json 的 business_assertions；多步骤隔离用例写 api/cases.json（均由 test_api.py 实际读取）。
目录覆盖率、HTTP可达、业务正确性、真实Hermes联调是四个不同结果。