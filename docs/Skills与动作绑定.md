# Skills 与业务动作：只由 Hermes 执行

## 1. 本轮最后修正

邮件、飞书读取/发送和未来业务工具均是 Hermes 使用的技能/工具。本包没有 smtplib 发送器、SMTP 设置、飞书租户令牌申请或开放平台请求代码。员工只需配置 Hermes 连接；业务服务账号和 Skill 的依赖放在 Hermes 一侧维护。

工作台的数据查询、确定性规则、Case、页面联动、草稿与反馈属于本地业务运行能力，仍由 Python + SQLite 完成；不是把每次图表计算也交给模型。

## 2. 普通对话不需要外层注册关卡

在右侧对 Hermes 说“读取某个飞书文档”时，可以使用已经安装的 Skill，不必先创建工作台 capability。工作台显示真实工具活动与回复；原生批准按原端要求呈现。没有证据就不写成“外部已发送”或“业务问题已解决”。

下面的 actions 仅用于把业务按钮、默认参数、草稿和执行记录接起来，不是限制 Hermes 只能使用三种工具的允许名单。

## 3. 可配置的动作绑定

这是一份说明性片段，不是独立场景包。放到既有业务定义 actions 内；直接替换在运行的业务请走配置预览/应用，或由 AI 使用同一桥接。

```json
{
  "actions": {
    "email.send": {
      "name": "协同邮件",
      "executor": "hermes_skill",
      "ui": "notification",
      "skill_hint": "",
      "instruction": "使用已安装的邮件发送 Skill 发送已确认的通知。",
      "recipient_field": "owner_email",
      "recipient_label": "收件人邮箱",
      "group_by": "owner",
      "subject": "[{scenario}] {owner} 的 {count} 项待处理事项",
      "body": "{owner}，你好：\n{items}\n请核对并反馈。"
    },
    "feishu.read": {
      "name": "飞书查证",
      "executor": "hermes_skill",
      "ui": "query",
      "skill_hint": "",
      "instruction": "使用已安装的飞书读取 Skill 查询当前业务对象相关记录，返回真实来源和时间；缺少权限或技能时明确说明。"
    }
  }
}
```

`email.send`、`feishu.read` 是这份配置中的业务名字；可换成 `supplier.lookup`、`collaboration.notify` 等，不需要修改 Python 里的 switch 分支。`skill_hint` 可以填写你们真实 Skill 名称，也可以留空；不是要求按这几个名字安装 Skill。

`ui=query` 发起真实 Hermes 查询任务。`ui=notification` 先生成业务草稿，确认后发起真实 Hermes 任务。执行器统一为 hermes_skill，后加新操作不通过本地 SMTP/飞书实现。

通知按 `group_by` 分组，可用当前字段或 `all`。目标来自明确参数 `recipient` 或 `recipient_field` 的业务数据。一个分组出现冲突/缺失目标时需补充，不自动猜测。飞书样例使用可选 `feishu_target`，未填时显示待补，不塞入假群 ID。

## 4. 同一条执行路径

按钮：选择对象 → 准备同一份计划 → 必要业务确认 → Agent 任务 → Hermes 工具/Skill → 结果。

对话：Hermes 可通过 action.prepare 准备同一份计划，返回业务确认卡；用户确认后也进入上述任务路径。普通原生对话不被强制套入这套草稿，按原端 Skill 能力处理。

本地 `/api/capabilities/invoke` 是业务操作入口，不是飞书和邮件的直连代理。对外写入的真正执行留给 Hermes，不把工具事件再次当命令执行。

## 5. 缺 Skill、失败、未知与回执

工作台不能仅凭 HTTP 200 判定某个 Skill 已安装或使用成功。发给 Hermes 的指令要求使用已有 Skill，缺少时返回未执行，不临时写直连代码替代。不完整输出就保留需核实，而不是制造回执。

动作状态和 Agent 状态独立：Agent 本轮完成，但业务消息没有确切回执，动作可为 needs_review（待核实回执）。这不是又一个卡住的 Agent，而是业务结果没有被证明。Case 更不会因此关闭。

可选结果适配：Hermes 从 Skill 得到真实接口回执后，通过本轮工作台桥接登记：

```json
{
  "operation": "action.receipt",
  "request_id": "同一次回执的稳定编号",
  "parameters": {
    "action_id": "本轮上下文中的 ACT 编号",
    "message_id": "该计划中实际消息 id",
    "status": "submitted",
    "provider_ref": "真实工具返回的外部记录编号",
    "evidence": "实际返回片段或可访问来源"
  }
}
```

回执 `submitted` 必须有外部标识；failed 和 unknown 可表达已知失败及结果不明确。记录明确标注 `independently_verified=false`，即这是工具通过桥接报告的结果，不是工作台另外验证过送达/已读。已提交分项不会被后续旧回执覆盖，也不在人工继续时重做。

此适配不要求重写 Skill，更不是新增发送代码。暂不适配也可正常与 Hermes 对话、使用 Skill；只是自动闭环最多到工具结果，不能吹成外部业务已经验收。

## 6. 停止边界

停止该 Hermes 执行不改变自动化总开关，也不停止数据处理。不再允许该轮使用工作台写接口，但已经发出的邮件不能回收。任意脱离当前执行的独立脚本，不属于本轮子代理/进程管理范围；按远端能力核对，不伪装为全部取消。

## 7. 最小现场联调

先在原 Hermes/WebUI 验证真实 Skill 能完成只读操作，再从工作台相同请求验证。确认工具名/来源真实可见、刷新不中断、原端停止能同步回来。最后使用测试收件人或测试群，确认实际目标、审批和外部回执，再启用业务自动化。

本包的 tests/protocol_peer.py 只产生明确标注的协议测试事件，不调用模型、不安装业务 Skill、不发送真实消息，不能拿测试截图当作外部联调成功。
