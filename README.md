# 供应链 Agent 工作台 v2.2.0-rag · Apache-2.0 开源版

> 本项目采用 [Apache License 2.0](LICENSE) 开源。当前实现边界、第三方组件和发布验证见 [`OPEN_SOURCE_RELEASE.md`](OPEN_SOURCE_RELEASE.md) 与 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

自带物料、交期、质量三个合成业务样例。基础工作台不需要生成器、Git、DSH、Docker或旧 ZIP；选择 Agent 模式时可直接使用本机 `deepseek-harness-sdk`，并保留 Hermes 兼容适配。外部业务操作始终由实际 Agent Tools / Skills 执行。

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS/Linux: ./.venv/bin/python -m pip install -r requirements.txt
python server.py --open
python doctor.py
python verify.py
```

启用本地 DSH：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dsh.txt
.\.venv\Scripts\python.exe server.py --open
```

默认地址为 <http://127.0.0.1:8765>。必要时可运行 `python server.py --host 0.0.0.0 --port 8765 --db /持久磁盘/workbench.sqlite3`。存储默认位于 `data/workbench.sqlite3`，也可复制 `.env.example` 为 `.env` 并设置 `WORKBENCH_DB`；启动参数优先。不要把 `.env`、`data/` 或 `reports/` 提交到仓库。

左侧切场景；右侧公共导航切目标、数据、Case、规则、证据等，旁边是 Agent。数据版本按钮选择日期范围、加载更多并固定历史。点击统计可下钻同一快照；明细区可选择所有已登记原始/中间/结果表，分页、横向滚动、行追溯和复盘；Case 快捷按钮仅填入 Agent 输入，不自动发送。

代码含 `app/data_workspace.py`、`web/data-workspace.js/css`、72 个 HTTP 接口统一注册与 API 测试。`rules-view` 显示当时实际配置和登记的规则原文，而不是新的 AI 摘要。完整追溯以显式 `_source_refs` 为准，没有记录的 Python 内部中间结果不猜测。

`doctor.py` 检查 Python、真实 SQLite、API 导出和可选 DSH SDK，不调用模型。`verify.py` 运行单元测试和真实隔离 HTTP 接口检查，报告明确保留外部 SKIP。`verify_dsh.py --live` 才会显式调用一次当前配置的真实模型。

详见 `docs/数据工作区与数据版本.md`、`docs/API合同与验收分层.md`。本模板为已提供样例的可运行基座，不宣称已验证未提供的企业正式规则，也没有真实发送邮件或飞书消息。

`business-v1.2` 仅作为旧场景兼容 ID 保留，不表示运行时依赖旧 ZIP 或外部生成平台。

## 安全与贡献

本仓库只包含合成业务样例，不应提交企业数据、模型密钥、飞书凭据、运行数据库或日志。DSH 的 `workspace-write` 不是操作系统沙箱，生产部署必须使用专用账户、最小文件权限和独立密钥。

提交变更前请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 和 [`SECURITY.md`](SECURITY.md)，并运行 `python verify.py`。第三方组件及许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 社区版缺料催交闭环

物料场景已增加“供应商催交”动作。系统使用现有确定性规则识别缺料，为选定 Case 生成带对象版本和幂等键的结构化草稿，人工确认后由 DSH/Hermes 调用独立 Mock ERP MCP 服务。

MCP 服务基于官方 Model Context Protocol Python SDK 的 FastMCP Streamable HTTP 传输，实现 `prepare_expedite`、`commit_expedite`、`rollback_expedite` 和 `get_operation_status`，并使用 SQLite 保存幂等操作状态。运行说明和当前边界见 `docs/缺料催交基础场景.md`。

本地 DSH 的进程、配置、批准、重启边界和验证命令见 `docs/DSH本地SDK接入.md`。没有实际模型地址与密钥时，仓库不会声称真实模型已经联调。

定时飞书图片由外部 DSH 任务和飞书 CLI Skill 拉取；本工作台提供脱敏的 `scheduled-feishu-cli-v1` 批次接收合同，不实时生成图片，也不内置飞书 API。合同、幂等和敏感字段约束见 `docs/缺料催交基础场景.md` 与 `dsh_runtime/integrations.example.json`。

## RAG 框架

项目已包含供应链证据 RAG 的接口与审计框架：Collection、文档元数据、Chunk 预览、计划任务、Query、Hit/Citation 表结构，以及 DSH Bridge。当前默认适配器为 `none`，未收集真实文档，也未调用 Embedding、向量库或 Reranker；查询会明确返回 `not_configured`，不会生成虚假召回。实现边界和后续接入步骤见 `docs/RAG框架与实施步骤.md`。
