# 本模板的不可误改项

先读 README.md、docs/Hermes协议说明.md 和 docs/Skills与动作绑定.md。这里是业务工作台模板，不是 Studio 或生成平台。

- 默认真实 Hermes，支持原生 Runs 与 nesquena/hermes-webui；没有地址或能力就如实报错。
- 邮件/飞书都由 Hermes 使用已有 Skills。不得添加本地 SMTP/飞书直连、假群、模拟发送兜底。actions 是业务绑定，不是技能注册门。
- 刷新只恢复观察，不 POST start。停止/失败的任务必须人工继续；终态不可被迟到事件复活。
- 停止当前 Hermes 不关自动任务开关、不暂停 Case、不停数据计算。开关由用户单独控制。
- 不做子代理树/通用后台进程管理。外部已发生动作不假称能回滚。
- 普通对话不强制 JSON。工作台桥接只处理具体业务操作，工具事件不二次执行。
- 允许改业务配置/SQL/Python/数据来源；保留版本、试算和原验收，不能偷偷改松验收。
- 可用任意合法 HTTP(S) 地址；同机默认，跨机可达性如实说明，不把 localhost 当成远端能访问。
- 默认 Python >=3.10 标准库；确需业务依赖可安装到项目 .venv。DSH 仅允许作为 `requirements-dsh.txt` 中的可选本地 Agent 运行时，不得变成基础工作台的强制依赖；不要增加生成器、Redis或多级IAM依赖。
- 修改后执行 `python -m unittest discover -s tests -v`。浏览器测试是开发选项，不能把协议测试对端当作真实账号联调。

## 项目工程制作

在生成器的项目工作副本中，DSH可以修改本工程的前端、后端、模板组件和测试，不限于scene.json。保持业务语义和上述运行约束；全局母版不属于本任务可写目标。所有HTTP操作必须登记于app/api_contract.py，执行scripts/export_api.py导出api/目录，并运行scripts/test_api.py --isolated。普通最终说明不强制JSON，技术故障先读取代码、修复并测试。
