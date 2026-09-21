# 本地 DSH SDK 接入

## 1. 接入边界

本项目直接使用本机 Python 环境中的 `deepseek-harness-sdk`，不是把 DSH 伪装成一个 HTTP Chat Completions 服务。当前锁定版本为 `0.1.5rc1`，SDK 自带匹配的 Windows runtime。

基础工作台仍可在未安装 DSH 时运行。DSH 只在 `agent.mode=dsh` 时成为必需依赖：

```powershell
cd supply-chain-agent
.\.venv\Scripts\python.exe -m pip install -r requirements-dsh.txt
.\.venv\Scripts\python.exe doctor.py
```

`doctor.py` 只检查 SDK、bundled runtime、Node 和项目 runtime 文件，不调用模型。

## 2. 配置

启动工作台，在“连接与自动任务”选择“本地 DSH SDK”，填写：

- 模型 API 地址：DeepSeek 或内部兼容地址，必须是完整 `http://` 或 `https://` URL。
- 模型名称：传给 DSH SDK 的实际模型标识。
- API Key：端点需要认证时填写；前端只返回“已保存”标记，不回显旧密钥。
- Provider：默认 `deepseek-official`。
- 工具批准：默认 `ask`；`auto` 只表示 DSH 项目工作区内工具自动允许，不替代业务动作的人工确认。

设置保存在本机工作台 SQLite 中，`data/` 不应提交到开源仓库。密钥不会进入命令行参数、任务事件或 API 响应。

## 3. 运行链路

```text
AgentService
  -> DSHAdapter（任务状态、事件、停止、审批）
  -> 每个工作台会话一个持久 Python worker
  -> deepseek-harness-sdk
  -> SDK 自带 DSH runtime
  -> bridge.mjs（流式输出、工具批准、用户问题、取消）
  -> 模型 API
```

同一工作台会话在服务进程存活期间复用同一 DSH 进程和 session，保留 DSH 原生上下文。浏览器刷新只读取任务，不重新提交。

后端重启会结束其拥有的本地 DSH 子进程。项目不会冒充原生 session resume，也不会自动重发未确认任务；下一次人工发起的新任务只会携带工作台保存的有限会话记录，并明确提示模型不得重复不确定的外部写操作。

## 4. 工具与业务动作

DSH 使用 `workspace-write` 工具策略，工作目录为本项目。它不是操作系统容器；生产部署应使用专用系统账户、最小文件权限和独立密钥。

业务数据、Case、草稿和回执仍通过任务级 `scripts/workbench_bridge.py` 访问。DSH 不应直接修改 SQLite。催交动作仍要求：

1. 工作台生成带对象版本和幂等键的草稿。
2. 用户确认当前草稿版本。
3. Agent 通过已登记的 Mock ERP MCP/CLI 工具执行 `prepare_expedite` 与 `commit_expedite`。
4. Agent 使用 `action.receipt` 登记真实外部编号；这仍标记为“工具报告，未独立核验”。

SDK profile 本身不会把任意 MCP 地址伪装成已安装工具。Mock ERP 端点 `http://127.0.0.1:8000/mcp` 仍需在实际 DSH 工具/Skill 配置中登记后再做现场联调。飞书图片由外部 DSH 定时任务与飞书 CLI Skill 拉取，工作台消费落盘或接口结果，不要求当前对话实时生成。

项目提供 `dsh_runtime/integrations.example.json` 作为 provider-neutral 登记模板：它列出 Mock ERP 的四个工具和定时飞书批次接口，但不会修改 DSH 的全局工具注册。部署时应将其中的 MCP URL 按实际 DSH/Skill 配置格式登记；工作台的 `/api/connections/test` 只报告模板存在，不把“模板存在”当成工具已注册。

## 5. 验证分层

不调用模型的本地检查：

```powershell
.\.venv\Scripts\python.exe verify_dsh.py
.\.venv\Scripts\python.exe -m unittest tests.test_dsh_runtime -v
```

显式真实模型 smoke（会产生一次 API 请求和费用）：

```powershell
.\.venv\Scripts\python.exe verify_dsh.py --live
```

只有 `--live` 返回 `live_model_verified=true`，才表示当前模型地址、模型名和密钥完成了一次真实请求。它不等于 Mock ERP MCP、飞书 CLI 或生产业务动作已联调。

当前仓库可以验证 SDK 版本、真实 runtime 配置合成、持久 worker 协议和真实 bridge 模块；在未提供模型地址与凭据时，实时模型联调状态必须保持未验证。
