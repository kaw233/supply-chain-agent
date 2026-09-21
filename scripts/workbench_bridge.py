#!/usr/bin/env python3
"""Used by a local DSH or remote Hermes terminal tool. No SDK dependency.
Example: python workbench_bridge.py --url http://127.0.0.1:8765 --task TASK --token TOKEN describe
A task-specific token and precise command are provided in that turn's workbench context.
"""
import argparse,json,sys,uuid
from urllib.request import Request,urlopen
from urllib.error import HTTPError
p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8765');p.add_argument('--task',required=True);p.add_argument('--token',required=True);p.add_argument('--request-id');p.add_argument('operation');p.add_argument('parameters',nargs='?',default='{}');args=p.parse_args()
try:
    data={'operation':args.operation,'parameters':json.loads(args.parameters),'request_id':args.request_id or 'BR-'+uuid.uuid4().hex}
    req=Request(args.url.rstrip('/')+'/api/bridge/'+args.task,json.dumps(data,ensure_ascii=False).encode(),{'Content-Type':'application/json','X-Task-Token':args.token})
    with urlopen(req,timeout=60) as r: print(r.read().decode('utf-8'))
except HTTPError as e:
    print(e.read().decode('utf-8','replace'),file=sys.stderr);sys.exit(1)
except Exception as e:
    print(str(e),file=sys.stderr);sys.exit(1)
