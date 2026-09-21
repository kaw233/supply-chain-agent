#!/usr/bin/env python3
"""Standalone template test entry. Never sends actual email/Feishu or calls a model."""
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
checks=[]
for label,args in [('environment',[sys.executable,'doctor.py']),('business unit tests',[sys.executable,'-m','unittest','discover','-s','tests','-v']),('actual isolated HTTP API',[sys.executable,'scripts/test_api.py','--isolated','--report','reports/api-report.json'])]:
 p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=120,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
 checks.append({'name':label,'status':'PASS' if p.returncode==0 else 'FAIL','output':p.stdout+p.stderr});print(checks[-1]['status'],label)
r={'ok':all(c['status']=='PASS' for c in checks),'checks':checks,'live_DSH_verified':False,'live_Hermes_verified':False,'external_SKIPs_are_not_PASS':True};out=ROOT/'reports/verify.json';out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(r,ensure_ascii=False,indent=2));print(out);sys.exit(0 if r['ok'] else 1)
