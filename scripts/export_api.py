#!/usr/bin/env python3
"""Export ALL registered runtime routes. No HTTP handlers live outside the registry."""
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.api_contract import ROUTES,openapi
from app import bridge

def export(root=ROOT):
    root=Path(root);out=root/'api';out.mkdir(exist_ok=True)
    inventory={'contract_version':'1.0','operations':[r.public() for r in ROUTES],'bridge_operations':bridge.CATALOG}
    for file,data in [('openapi.json',openapi()),('inventory.json',inventory)]:
        (out/file).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    plan={'version':'1.0','default_mode':'isolated','note':'External effects require explicit live setup; skip is not pass.',
          'operations':[{'operationId':r.operation_id,'effect':r.effect,
            'coverage':'automatic-read-or-required-parameter-check' if r.effect=='read' or r.required else 'fixture-or-explicit-live',
            'sample':r.example or {},'expected_status':[200],'business_assertions':[]} for r in ROUTES]}
    # User assertions are not overwritten by a fresh inventory export.
    if (out/'test-plan.json').exists():
        previous=json.loads((out/'test-plan.json').read_text(encoding='utf-8'))
        saved={x['operationId']:x for x in previous.get('operations',[])}
        for x in plan['operations']:
            old=saved.get(x['operationId'],{});x['business_assertions']=old.get('business_assertions',[])
            if old.get('sample'):x['sample']=old['sample']
        plan['retired_operations']=[v for k,v in saved.items() if k not in {x['operationId'] for x in plan['operations']}]
    (out/'test-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    if not (out/'cases.json').exists():
        cases={'version':'1.0','note':'Extra isolated/read-only tests. External effects never run automatically.',
            'cases':[{'name':'Health must report ok','operationId':'health','assertions':[{'path':'/ok','equals':True}]},
                     {'name':'List installed scenarios','operationId':'listScenarios','assertions':[{'path':'','type':'array','min_length':1}]}]}
        (out/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 业务成品完整 API 目录','','HTTP 路由唯一注册文件：`app/api_contract.py`。',
           '运行时：`GET /api/catalog`、`GET /api/openapi.json`、`/api-docs`。',
           '默认测试：`python scripts/test_api.py --isolated --report reports/api-report.json`。',
           '已启动服务：`python scripts/test_api.py --base http://127.0.0.1:8765 --report reports/read-only.json`。',
           '后者只读；不将未执行的发送、批准、停止标记为通过。','','| 方法 | 路径 | operationId | 副作用 | 用途 |','|---|---|---|---|---|']
    for r in ROUTES:lines.append(f'| {r.method} | `{r.path}` | `{r.operation_id}` | {r.effect} | {r.summary} |')
    lines+=['','## 桥接子操作','', 'HTTP 操作 `invokeBridge` 内的子操作也需测试；目录见 inventory.json 的 bridge_operations。',
            '## 扩展规则','新增路由必须登记 operationId、输入、响应、副作用、样例；成功响应断言写入 api/test-plan.json 的 business_assertions；多步骤隔离用例写 api/cases.json（均由 test_api.py 实际读取）。',
            '目录覆盖率、HTTP可达、业务正确性、真实Hermes联调是四个不同结果。']
    (out/'README.md').write_text('\n'.join(lines),encoding='utf-8')
    return inventory
if __name__=='__main__':print(json.dumps({'operations':len(export()['operations']),'directory':str(ROOT/'api')},ensure_ascii=False))
