#!/usr/bin/env python3
"""Standalone doctor. DSH is diagnosed when installed but never invoked here."""
import importlib.metadata,json,shutil,sqlite3,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT))
def main():
 from app.api_contract import ROUTES,openapi
 checks=[]
 def ck(name,ok,detail=None):checks.append({'name':name,'status':'PASS' if ok else 'FAIL','detail':detail})
 ck('Python 3.10+',sys.version_info>=(3,10),sys.version)
 with tempfile.TemporaryDirectory() as t:
  c=sqlite3.connect(Path(t)/'doctor.db');c.execute('CREATE TABLE x(value TEXT)');c.execute("INSERT INTO x VALUES('ok')");c.commit();ck('actual SQLite write/read',c.execute('SELECT value FROM x').fetchone()[0]=='ok');c.close()
 packs=list((ROOT/'packs').glob('*.json'));ck('built-in business scenarios',bool(packs),[x.name for x in packs])
 ids=[r.operation_id for r in ROUTES];ck('unique actual route identifiers',len(ids)==len(set(ids)),len(ids))
 expected=json.loads((ROOT/'api/openapi.json').read_text(encoding='utf-8'));ck('OpenAPI matches actual routes',expected==openapi())
 try:sdk=importlib.metadata.version('deepseek-harness-sdk')
 except importlib.metadata.PackageNotFoundError:sdk=None
 dsh_files=all((ROOT/'dsh_runtime'/name).is_file() for name in ('worker.py','bridge.mjs','profile.patch.yml'))
 checks.append({'name':'optional local DSH SDK','status':'PASS' if sdk and shutil.which('node') and dsh_files else 'SKIP','detail':{'sdk_version':sdk,'node':shutil.which('node'),'runtime_files':dsh_files,'install':'python -m pip install -r requirements-dsh.txt'}})
 report={'ok':all(c['status']!='FAIL' for c in checks),'checks':checks,'model_required_for_this_check':False,'live_DSH_verified':False,'live_Hermes_verified':False,'live_RAG_verified':False,'version':'2.2.0-rag'}
 print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['ok'] else 1
if __name__=='__main__':sys.exit(main())
