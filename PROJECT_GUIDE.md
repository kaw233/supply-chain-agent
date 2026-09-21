# 数据工作区与工程合同（v2.2.1-rag）

制作时允许修改完整 Python/JS/CSS/SQL，不限两个 JSON。不得在生成场景时静默删除：同快照下钻、数据版本、已登记全表分页、规则实际定义、来源引用、Case→Hermes 快捷提示、API 目录和测试。

`pipeline` 注册处理步骤（id/kind/input/source），每步结果都保存。Python可返回 `{rows, tables}` 保留更多中间表；`_source_refs: [{table,key}]` 给出确定逐行关系。未保存的临时变量/表和未登记引用，不推测成可证实来源。

`rule_sources` 记录实际提供的规则原文、文件和章节；`rule_coverage` 映射到实际 rules/pipeline/derived 与待确认事项。不能将用户给定复杂规则缩成几条通用分类还标成“全部完成”。缺少原文应标明，不能编造文档。

`app/data_workspace.py` 固定来源、阶段结果、规则和参数；历史注释追加保存，不修改旧快照。工作区菜单右侧共用，左侧只选场景。点击统计要携带同一 snapshot 和筛选，分页不能落到最新批次。

新增API由 `app/api_contract.py` 注册，再执行 `scripts/export_api.py`；以 `scripts/test_api.py --isolated` 测试真实本地 HTTP，不把外部 Hermes 未配置的 SKIP 改成 PASS。

业务运行的邮件/飞书仍由 Hermes Skills 执行。Case捷径仅填入相应快照/对象/动作提示词，不自动发送。历史业务动作在当前事实核对后执行。

社区贡献应在独立分支完成，并保留可审阅的 Git 变更。界面修改应尽量完成实际浏览器检查；截图能力不可用时，仍须保证服务可运行并通过自动化测试，不为单次修改引入与业务无关的浏览器依赖。
