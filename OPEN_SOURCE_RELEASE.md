# 开源发布检查表

## 当前结论

**状态：工程发布检查已就绪。** 项目所有者已指示按 Apache License 2.0 公开发布，并在 `UPSTREAM.md` 中记录发布声明：

- [x] 项目所有者确认并承担原始模板及全部修改的公开再发布权责任。
- [x] 根目录已包含完整的 Apache License 2.0 文本与 `NOTICE`。

## 已完成的工程准备

- [x] 使用合成的物料、交期和质量样例，不包含已知企业生产数据。
- [x] DSH 保持可选依赖；无 DSH 时基础工作台仍可运行。
- [x] RAG 明确为 `contract-only`，未配置时不生成虚假召回或引用。
- [x] 运行数据库、DSH Profile、飞书接收产物、日志和测试报告已加入忽略规则。
- [x] 增加根许可证、NOTICE、安全策略、贡献指南和第三方组件说明。
- [x] API 数量与文档同步为 72。
- [x] 文档已移除本机绝对开发路径。
- [x] 旧的 `TEMPLATE_SOURCE_MANIFEST.json` 已从公开源码候选集合排除。

## 首次公开发布流程

1. 保存与上游权利相关的内部授权记录。
2. 运行 `python scripts/release_audit.py`，要求结果为 PASS。
3. 运行 `python verify.py` 以及 `python scripts/test_api.py --isolated`。
4. 使用专用 Secret Scanner 检查完整 Git 历史和待发布内容。
5. 建立首次可审阅提交；确认 `git status --ignored` 中的运行数据均未被跟踪。
6. 使用 `git archive` 从已提交内容生成发布包，不要直接压缩开发目录。
7. 在全新临时目录解压，创建新虚拟环境，安装依赖并再次运行验证。
8. 创建带版本号的 tag/release，并在说明中列出 DSH、MCP、飞书和 RAG 的真实联调边界。

## 许可证

本项目选择 Apache License 2.0，其明确的专利授权和 NOTICE 机制适合企业集成型项目。许可证选择不覆盖未披露的第三方权利；新增依赖或复制外部代码时仍须单独审核。
