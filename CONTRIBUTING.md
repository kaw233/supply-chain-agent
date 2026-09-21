# 贡献指南

感谢参与供应链 Agent 工作台。本项目采用 Apache License 2.0；提交贡献即表示贡献者有权按该许可证提供相应内容。

## 开发环境

需要 Python 3.10 或更高版本。基础工作台不依赖 DSH；本地 DSH SDK 是可选运行时。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py --open
```

macOS/Linux 请将解释器路径替换为 `./.venv/bin/python`。

## 变更要求

- 只提交合成或已明确获准公开的数据，不提交企业数据、客户名称、模型密钥、Cookie、运行数据库、日志或报告。
- DSH 必须保持可选；基础工作台不能因未安装 DSH 而无法启动。
- 不增加本地 SMTP 或飞书直连作为兜底。外部业务动作仍由真实 Agent Tools / Skills 执行。
- 新增 HTTP 操作必须登记在 `app/api_contract.py`，然后重新导出 `api/`。
- 外部操作缺少真实环境时应标记为 SKIP 或未验证，不能用模拟回执宣称联调成功。
- RAG 未配置真实 Provider 时必须返回 `not_configured`，不能制造 Hit、分数或 Citation。
- 新增第三方代码或依赖时，必须同步更新 `THIRD_PARTY_NOTICES.md` 并确认许可证兼容。

## 验证

```powershell
.\.venv\Scripts\python.exe scripts\export_api.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\test_api.py --isolated
.\.venv\Scripts\python.exe verify.py
.\.venv\Scripts\python.exe scripts\release_audit.py
```

`release_audit.py` 检查许可证、公开文档、运行产物隔离和本机路径残留，公开发布前不得跳过。

## 提交说明

一个提交尽量只处理一个主题。说明中写清业务边界、验证结果和仍为 SKIP/未联调的部分。贡献者必须确认所提交内容为本人原创，或已获得与本项目最终许可证兼容的再发布许可。
