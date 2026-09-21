# API 与最小工作台桥接

以下为本包已实现接口。API 返回 JSON；错误采用非 2xx 状态和 error 字段。例子里的 id、网址和请求号需要用实际运行值替换。默认本机单实例，身份显示名由 workspace.operator 记录，不是企业 SSO。

## 1. 业务能力接口

`GET /api/capabilities?scenario_id=materials` 读取当前业务的可用入口。

`POST /api/capabilities/invoke`：

```json
{
  "scenario_id":"materials",
  "capability":"feishu.read",
  "case_ids":["实际 CASE 编号"],
  "parameters":{"query":"本次交期确认"},
  "request_id":"本次独立查询的稳定编号"
}
```

配置中 ui=query 的 Skill 返回 type=agent_task 和 task；不是本地协同数据。ui=notification 返回 type=action_draft，必要确认后再执行。data.query/rules.run/report.generate 是本地确定性业务能力，不经模型。

草稿 `GET /api/actions/{id}`；修改使用 PATCH 同一路径的 messages（仅 recipient/subject/body，不能改分组数量）。执行：

```json
{"confirm":true,"draft_revision":1}
```

POST 到 `/api/actions/{id}/execute`。确认版本、目标/正文和数据依据不匹配时会拒绝；已经提交的同一草稿不会重复派发。实际发送由 Hermes Skill，返回 action.agent_task_id 用于查看对应执行。

## 2. Agent 任务接口

```json
{
  "scenario_id":"materials",
  "message":"分析当前业务问题，并读取相关协同信息",
  "session_id":"已有会话编号；新会话时省略本字段",
  "request_id":"稳定且唯一的本次意图编号",
  "context":{"page":"overview","filters":{"severity":"high"},"case_ids":[]}
}
```

POST `/api/agent/tasks` 返回本地任务。上下文提交时冻结，不随页面后续筛选改变。GET 同一路径可按 scenario_id/session_id 列表。

| 接口 | 作用 |
|---|---|
| GET /api/agent/tasks/{id} | 当前状态、关联远端、活动、结果、有效卡片 |
| GET /api/agent/tasks/{id}/events?after=0 | 已保存事件增量，seq 用于观察游标 |
| POST /api/agent/tasks/{id}/cancel | 请求停止当前执行；不改自动化设置 |
| POST /api/agent/tasks/{id}/observe | 只恢复观察，不提交新任务 |
| POST /api/agent/tasks/{id}/continue | confirm=true + message=明确下一步 + request_id；新执行关联原任务 |
| POST /api/agent/tasks/{id}/reply | card_key + choice(once/deny)；澄清另带 answer |
| GET /api/agent/sessions?scenario_id=… | 本地会话 |
| GET /api/agent/sessions/{id}/snapshot | 消息与任务快照；页面刷新读取此处 |
| GET /api/hermes/sessions | WebUI 已有远端会话列表 |
| POST /api/hermes/bind | scenario_id、可选 session_id、remote_session_id；仅关联，不重放 |

页面状态由 SQLite 和后台对账产生。前端 1 秒读任务快照，后台接收 SSE 并约 1.5 秒核对远端；这些是当前实现默认值，不是上游网络下的保证 SLA。已断连会显示最后确认状态，不把离线视为成功。

## 3. 数据与触发

POST `/api/data/ingest`：

```json
{"scenario_id":"materials","mode":"upsert","event_id":"上游数据批次唯一编号","records":[{"id":"实际业务对象编号"}]}
```

records 必须补齐当前业务合同字段；这里只说明请求形状。可用 samples/*.json。mode=replace 替换输入，upsert 按 id 新增/更新。event_id 相同不重复派发自动任务，但仍可完成数据检查。

POST `/api/triggers/fire`：scenario_id、type(manual/agent/data_ready)、可选 parameters；data_ready 要自动派发时需 event_id。

GET `/api/scenarios/{id}/dashboard` 可按 severity、owner、site、q 筛选。GET records?raw=1 读原始数据，config 读当前配置，cases 读问题。POST pull 读取已配置 file/url 来源，失败保留原数据和错误说明。

PATCH `/api/cases/{id}` 支持反馈 note、source 和相应 status。关闭要求有效验收，风险未解除不能仅凭“已通知”关闭。

## 4. 业务处理变更

GET `/api/scenarios/{id}/config` → 修改 data_source/mapping/processor/derived/rules/actions 等 → POST `/api/scenarios/{id}/drafts`，body={config:完整配置}。

返回 validation、impact、diff；原 acceptance 改动直接拒绝。本轮业务基准若确需修改，另行明确评审，不开放给普通配置修复自动放宽。

POST `/api/drafts/{id}/publish`，confirm=true，生效并重新算。GET `/api/scenarios/{id}/revisions`；POST rollback，version 和 confirm=true。配置回滚不撤销外部动作。

## 5. 最小工作台桥接

每次 Agent 任务生成随机任务令牌，只随该轮上下文提供。请求：

```text
POST /api/bridge/{task_id}
X-Task-Token: 该任务实际令牌
Content-Type: application/json
```

```json
{"operation":"describe","parameters":{},"request_id":"read-contract-1"}
```

describe 返回当场景业务能力和可用桥接操作。更方便的同机调用：

```bash
python scripts/workbench_bridge.py --url http://127.0.0.1:8765 --task TASK_ID --token TASK_TOKEN describe
```

脚本只是标准库 HTTP 客户端，不是邮件/飞书 Skill。后续 operation 和 parameters 的 CLI 格式见 `--help`。

桥接包括 data.query/raw/ingest/pull、rules.run、case.get/feedback、view、action.prepare/receipt、processing.get/preview/apply、report。没有本地 action.send 外部执行器。

读写均绑定原任务场景。写操作需要 request_id 去重；停止/结束后不接受该轮新的写操作。普通 read 不代表外部 Skill 的统一准入审批。view 只发出明确的筛选/页面/Case 指令，无浏览器时不阻塞后台业务。工具 started/completed 事件仅记录，绝不再本地执行同名工具。

## 6. 非技术用户不需要填写这些接口

员工只需启动项目、填写 Hermes 连接、使用业务按钮或聊天。上述接口面向场景制作者、AI 和已有业务技能的可选适配。不要把这些 request_id、mirror_token 之类变成业务人员每次要手工填写的表单。
