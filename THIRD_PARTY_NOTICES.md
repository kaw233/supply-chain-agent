# 第三方组件说明

本文档列出本项目直接依赖或随源码分发的第三方组件。它不替代各组件自身的许可证文本；本项目自身代码采用根目录 `LICENSE` 所载 Apache License 2.0。

| 组件 | 使用方式 | 当前版本 | 许可证 | 来源 |
| --- | --- | --- | --- | --- |
| Model Context Protocol Python SDK (`mcp`) | Python 直接依赖，用于 Mock ERP MCP 服务 | 1.30.0 | MIT | <https://github.com/modelcontextprotocol/python-sdk> |
| DeepSeek Harness SDK (`deepseek-harness-sdk`) | 可选本地 Agent 运行时 | 0.1.5rc1 | MIT | <https://github.com/deepseek-ai/deepseek-harness> |
| Marked | 随源码分发的浏览器端 Markdown 解析器 | 4.0.19 | MIT；其 Markdown 相关声明见随附文本 | <https://github.com/markedjs/marked> |

Marked 的完整随附声明保存在 `web/vendor/marked.LICENSE.md`。Python 包的传递依赖由安装工具解析，发布二进制镜像或离线依赖包时，应对最终锁定的完整依赖集合再次执行许可证审计。

`UPSTREAM.md` 记录原始模板来源与项目所有者的发布声明。第三方组件采用 MIT 等许可证，不改变本项目自身采用的 Apache License 2.0，也不免除保留第三方声明的义务。
