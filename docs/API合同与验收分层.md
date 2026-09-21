# 成品API合同和批量测试

实际HTTP路由注册源为 `app/api_contract.py`，`server.py` 请求分发也使用它。导出不是第二份手工清单。

| 文件或入口 | 内容 |
| --- | --- |
| api/inventory.json 或 /api/catalog | 72 个实际 HTTP 操作及桥接目录 |
| api/openapi.json 或 /api/openapi.json | OpenAPI3.1，与当前注册路由一致 |
| api/test-plan.json、api/cases.json | 测试策略、用例与业务断言 |
| /api-docs | 离线资源的接口阅读页 |
| scripts/test_api.py | 本地真实HTTP检查，不靠模型描述成功 |

```bash
python scripts/export_api.py
python scripts/test_api.py --isolated --report reports/api-report.json
python -m unittest discover -s tests -v
# 对已启动实例默认只读；路径参数见本成品说明
python scripts/test_api.py --base http://127.0.0.1:8765 --report reports/read-only.json
```

新数据工作区的查询必须保持同一 `snapshot`。版本查询支持from/to/cursor/limit；行查询支持snapshot/table/cursor/limit、severity/owner/site/rule_id/risk和行键q。快捷提示POST不发外部动作；snapshot-notes仅追加复盘。

批量报告区分正例成功、参数负例、SKIP 和 FAIL。某接口正确返回 404/400 只证明拒绝路径，不当作业务成功。当前隔离检查结果以本次生成的 `reports/api-report.json` 为准；目前登记 72 个操作。缺真实 Hermes 执行号、凭据或生产外部动作的接口明确跳过，不补造回执。

可运行预览不等于“已验收交付”；发布前仍要通过本地关键检查。缺截图和宿主机无关的 pip 冲突是警告，不阻止预览。独立发布包必须在全新虚拟环境安装声明依赖并复验，不能用开发机环境掩盖漏依赖。HTTP 200 不证明全部业务规则正确。
