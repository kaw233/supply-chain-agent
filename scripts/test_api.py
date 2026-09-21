#!/usr/bin/env python3
"""Reproducible API matrix: discover actual catalog, exercise safe paths, report skips.
--isolated uses fresh SQLite, no real Hermes credentials, no outgoing action.
--base is READ ONLY. It never sends/approves/cancels a production task.
Custom scenario cases in api/cases.json may extend isolated tests.
"""
import argparse, json, sys, tempfile, threading, time, copy
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def request(base,method,path,body=None):
    req=Request(base.rstrip('/')+path,data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),method=method,headers={'Content-Type':'application/json'})
    try:
        with urlopen(req,timeout=20) as r:return r.status,json.loads(r.read())
    except HTTPError as e:
        try:body=json.loads(e.read())
        except ValueError:body={'error':str(e)}
        return e.code,body

def pointer(obj,path):
    for bit in str(path).strip('/').split('/') if path not in ('','/') else []:
        bit=bit.replace('~1','/').replace('~0','~')
        obj=obj[int(bit)] if isinstance(obj,list) else obj[bit]
    return obj

def assertions_ok(obj,items):
    for a in items:
        try:value=pointer(obj,a.get('path',''))
        except (KeyError,IndexError,TypeError,ValueError):return False
        if 'equals' in a and value!=a['equals']:return False
        if 'min_length' in a and (not hasattr(value,'__len__') or len(value)<a['min_length']):return False
        types={'object':dict,'array':list,'string':str,'number':(int,float),'boolean':bool,'null':type(None)}
        if 'type' in a and not isinstance(value,types[a['type']]):return False
    return True

def replace_vars(value,fixtures):
    import re
    if isinstance(value,str):
        return re.sub(r'\$\{(\w+)\}',lambda m:str(fixtures.get(m[1],m[0])),value)
    if isinstance(value,list):return [replace_vars(v,fixtures) for v in value]
    if isinstance(value,dict):return {k:replace_vars(v,fixtures) for k,v in value.items()}
    return value

def run_suite(base,isolated=False):
    rows=[]; fixtures={};covered=set()
    plan_path=ROOT/'api/test-plan.json'
    plan=json.loads(plan_path.read_text(encoding='utf-8')) if plan_path.exists() else {'operations':[]}
    configured={v['operationId']:v for v in plan.get('operations',[])}
    def check(label,method,path,body=None,status=200,op=None,predicate=None):
        started=time.monotonic()
        try:
            code,result=request(base,method,path,body)
            ok=code in (status if isinstance(status,list) else [status])
            if predicate and ok:ok=bool(predicate(result))
            if ok and code==200 and op:ok=assertions_ok(result,configured.get(op,{}).get('business_assertions',[]))
            rows.append({'test':label,'operationId':op,'method':method,'path':path,'status':'PASS' if ok else 'FAIL','http_status':code,'seconds':round(time.monotonic()-started,3),'response':result if not ok else None,'coverage':'positive-request' if code==200 else 'negative-input' })
            if op:covered.add(op)
            return result if ok else None
        except Exception as e:
            rows.append({'test':label,'operationId':op,'status':'FAIL','error':str(e)});return None
    cat=check('discover all routes','GET','/api/catalog',op='apiCatalog') or {'operations':[]}
    spec=check('OpenAPI matches route inventory','GET','/api/openapi.json',op='openapiSpec') or {'paths':{}}
    inv={(r['method'],r['path']) for r in cat['operations']}
    api={(m.upper(),p) for p,x in spec['paths'].items() for m in x if m.lower() in ('get','post','put','patch','delete','options')}
    rows.append({'test':'inventory-openapi-exact-parity','status':'PASS' if inv==api and len(inv)==len(cat['operations']) else 'FAIL','operations':len(inv)})
    scenarios=check('installed scenes','GET','/api/scenarios',op='listScenarios') or []
    sid=scenarios[0]['id'] if scenarios else None
    if sid:
        fixtures['scenario_id']=sid
        cs=check('case fixtures','GET',f'/api/scenarios/{sid}/cases',op='cases') or []
        if cs:fixtures['case_id']=cs[0]['id']
        if isolated:
            s=check('create local chat only','POST','/api/agent/sessions',{'scenario_id':sid},op='createSession') or {}
            if s.get('session_id'):fixtures['session_id']=s['session_id']
            r=check('deterministic business run','POST','/api/triggers/fire',{'scenario_id':sid,'type':'manual'},op='fireTrigger') or {}
            if r.get('id'):fixtures['run_id']=r['id']
            rec=check('read full raw inputs','GET',f'/api/scenarios/{sid}/records?raw=1',op='records') or []
            if rec:check('ingest same batch; no external automation','POST','/api/data/ingest',{'scenario_id':sid,'records':rec,'mode':'replace','event_id':'api-test-fixture'},op='ingestData')
            feishu=check('receive scheduled Feishu manifest','POST',f'/api/integrations/feishu/{sid}/batches',{
                'event_id':'api-test-feishu-fixture',
                'pulled_at':'2026-09-20T08:00:00+08:00',
                'source':{'provider':'feishu-cli','job_id':'api-test'},
                'artifacts':[{'artifact_id':'api-test-image-001','kind':'image','relative_path':f'data/integrations/feishu/{sid}/api-test-image-001.png'}],
            },op='ingestFeishuBatch',predicate=lambda x:x.get('artifact_count')==1)
            if feishu:check('list scheduled Feishu artifacts','GET',f'/api/integrations/feishu/{sid}/artifacts?limit=10',op='listFeishuArtifacts',predicate=lambda x:any(a.get('artifact_id')=='api-test-image-001' for a in x.get('artifacts',[])))
            check('RAG adapters report contract-only state','GET','/api/rag/capabilities',op='ragCapabilities',predicate=lambda x:x.get('mode')=='contract-only' and x.get('live_retrieval_verified') is False)
            rag=check('create RAG collection metadata','POST','/api/rag/collections',{
                'scenario_id':sid,'id':'api-test-evidence','name':'API fixture evidence'
            },op='createRagCollection',predicate=lambda x:x.get('status')=='draft') or {}
            if rag.get('id'):
                fixtures['collection_id']=rag['id']
                check('list RAG collections','GET',f'/api/rag/collections?scenario_id={sid}',op='listRagCollections',predicate=lambda x:any(v.get('id')==rag['id'] for v in x))
                check('get RAG collection','GET',f"/api/rag/collections/{rag['id']}",op='getRagCollection')
                doc=check('register RAG document metadata only','POST',f"/api/rag/collections/{rag['id']}/documents",{
                    'external_id':'api-fixture-doc','title':'Fixture document','source_type':'external-manifest','version':'v1','checksum':'a'*64,'metadata':{'fixture':True}
                },op='registerRagDocument',predicate=lambda x:x.get('status')=='registered') or {}
                check('list RAG document metadata','GET',f"/api/rag/collections/{rag['id']}/documents",op='listRagDocuments',predicate=lambda x:len(x)==1)
                if doc.get('id'):
                    fixtures['document_id']=doc['id']
                    check('get RAG document metadata','GET',f"/api/rag/documents/{doc['id']}",op='getRagDocument')
                    check('empty RAG chunks before indexing','GET',f"/api/rag/documents/{doc['id']}/chunks",op='listRagChunks',predicate=lambda x:x==[])
                    check('preview chunks without persistence','POST',f"/api/rag/documents/{doc['id']}/chunk-plan",{'text':'fixture text '*30,'chunk_size':120,'overlap':20},op='previewRagChunks',predicate=lambda x:x.get('persisted') is False and x.get('count',0)>1)
                job=check('plan RAG job without execution','POST',f"/api/rag/collections/{rag['id']}/jobs",{'kind':'ingest','mode':'plan','source':{'type':'future-manifest'}},op='createRagJob',predicate=lambda x:x.get('result',{}).get('executed') is False) or {}
                check('list planned RAG jobs','GET',f"/api/rag/collections/{rag['id']}/jobs",op='listRagJobs',predicate=lambda x:len(x)==1)
                if job.get('id'):
                    fixtures['job_id']=job['id'];check('read planned RAG job','GET',f"/api/rag/jobs/{job['id']}",op='getRagJob')
                query=check('RAG query refuses fake retrieval','POST',f"/api/rag/collections/{rag['id']}/query",{'query':'fixture question','top_k':5},op='queryRagCollection',predicate=lambda x:x.get('status')=='not_configured' and x.get('hits')==[] and x.get('citations')==[]) or {}
                if query.get('query_id'):
                    fixtures['query_id']=query['query_id']
                    check('read RAG query audit','GET',f"/api/rag/queries/{query['query_id']}",op='getRagQuery',predicate=lambda x:x.get('status')=='not_configured')
                    check('no citation exists without retrieval','GET',f"/api/rag/queries/{query['query_id']}/citations",status=404,op='getRagCitations')
            cfg=check('read current processing definition','GET',f'/api/scenarios/{sid}/config',op='scenarioConfig')
            if cfg:
                candidate=copy.deepcopy(cfg);candidate['description']=str(candidate.get('description',''))+' [isolated API test]'
                d=check('config draft and tests','POST',f'/api/scenarios/{sid}/drafts',{'config':candidate},op='draftConfig') or {}
                if d.get('id'):
                    fixtures['draft_id']=d['id']
                    check('publish exact draft','POST',f"/api/drafts/{d['id']}/publish",{'confirm':True},op='publishDraft')
            check('idempotent operator setting','POST','/api/settings',{'operator':'API fixture'},op='updateSettings')
            if fixtures.get('case_id'):
                check('record feedback without closing risk','PATCH','/api/cases/'+fixtures['case_id'],{'feedback':'isolated API fixture'},op='updateCase')
                a=check('prepare only, never send','POST','/api/capabilities/invoke',{'scenario_id':sid,'capability':'email.send','case_ids':[fixtures['case_id']]},op='invokeCapability') or {}
                ar=a.get('action',a)
                if isinstance(ar,dict) and ar.get('id'):
                    fixtures['action_id']=ar['id'];check('edit action draft only','PATCH','/api/actions/'+ar['id'],{'messages':ar.get('messages',[])},op='editAction')
            if fixtures.get('run_id'):check('read-only calculation replay','POST','/api/runs/'+fixtures['run_id']+'/replay',{},op='replayRun')
    # New data workspace requests are actual positive-path HTTP checks over one
    # resolved immutable snapshot, not just "missing parameter -> 400" checks.
    if sid:
        w=check('resolve data snapshot','GET',f'/api/scenarios/{sid}/workspace',op='dataWorkspace') or {}
        if w.get('snapshot'):
            snap=w['snapshot']['id'];fixtures['snapshot']=snap;fixtures['table_id']='results'
            tab=check('source/intermediate/result directory','GET',f'/api/scenarios/{sid}/tables?snapshot={snap}',op='dataTables') or {}
            check('date-indexed snapshot page','GET',f'/api/scenarios/{sid}/versions?limit=3',op='dataVersions')
            check('frozen executable rule view','GET',f'/api/scenarios/{sid}/rules-view?snapshot={snap}',op='rulesView')
            page=check('paged result records','GET',f'/api/scenarios/{sid}/tables/results/rows?snapshot={snap}&limit=5',op='tableRows') or {}
            if page.get('items'):
                from urllib.parse import quote
                key=str(page['items'][0]['key']);fixtures['row_key']=key
                check('actual row lineage','GET',f'/api/scenarios/{sid}/tables/results/lineage/{quote(key,safe="")}?snapshot={snap}',op='rowLineage')
                check('quick prompt does not send','POST',f'/api/scenarios/{sid}/quick-prompt',{'snapshot':snap,'table':'results','key':key,'action':'remind'},op='quickPrompt',predicate=lambda x:x.get('auto_send') is False)
                if isolated:check('append historic opinion not data mutation','POST',f'/api/scenarios/{sid}/snapshot-notes',{'snapshot':snap,'table':'results','key':key,'note':'isolated API fixture'},op='snapshotNote')
            high=w.get('summary',{}).get('counts',{}).get('high',0)
            check('metric and drill use same snapshot/filter','GET',f'/api/scenarios/{sid}/tables/results/rows?snapshot={snap}&severity=high',predicate=lambda x:x.get('total')==high)

    # Optional additional scenario-specific fixtures. No unknown or live write is silently executed.
    cases_path=ROOT/'api/cases.json'
    extra=json.loads(cases_path.read_text(encoding='utf-8')) if cases_path.exists() else {'cases':[]}
    for item in extra.get('cases',[]):
        op=item.get('operationId');route=next((r for r in cat['operations'] if r['operationId']==op),None)
        if not route:
            rows.append({'test':item.get('name',op),'status':'FAIL','error':'case references an unregistered API operation'});continue
        if route['effect'] in ('external-write','external-read','conditional') or (not isolated and route['method']!='GET'):
            rows.append({'test':item.get('name',op),'operationId':op,'status':'SKIP','reason':'External/conditional actions require dedicated live verification; never auto-run from cases.json'});continue
        path=item.get('path',route['path'])
        for k,v in fixtures.items():path=path.replace('{'+k+'}',str(v))
        body=replace_vars(item.get('body'),fixtures)
        if '{' in path or '${' in json.dumps(body):
            rows.append({'test':item.get('name',op),'operationId':op,'status':'SKIP','reason':'Required fixture is unavailable'});continue
        result=check(item.get('name',op),route['method'],path,body,status=item.get('expected_status',200),op=op,
            predicate=lambda x,it=item:assertions_ok(x,it.get('assertions',[])))
        if result is not None:
            for name,ptr in item.get('save',{}).items():fixtures[name]=pointer(result,ptr)
    for r in cat['operations']:
        op=r['operationId'];path=r['path']
        if op in covered:continue
        import re
        missing=[v for v in re.findall(r'{(\w+)}',path) if v not in fixtures]
        for key,value in fixtures.items():path=path.replace('{'+key+'}',str(value))
        if missing:
            rows.append({'test':op,'operationId':op,'status':'SKIP','reason':'No safe fixture for '+', '.join(missing)});continue
        if r['method']=='GET' and r['effect']=='read':
            if sid:path+='?scenario_id='+sid
            check(op,r['method'],path,op=op);continue
        if isolated and r.get('required'):
            # Parameter-negative coverage is recorded as NEGATIVE, never full business coverage.
            out=check(op+' rejects missing required fields',r['method'],path,{},status=400,op=op)
            rows[-1]['coverage']='negative-input-only'
            rows.append({'test':op+' positive execution','operationId':op,'status':'SKIP','reason':'Needs explicit fixture/authorized external service; not counted as executed'});continue
        rows.append({'test':op,'operationId':op,'status':'SKIP','reason':'External effect or fixture required; no automatic live execution'})
    coverage={r['operationId']:{'positive_pass':any(x.get('operationId')==r['operationId'] and x['status']=='PASS' and x.get('http_status')==200 for x in rows),'negative_pass':any(x.get('operationId')==r['operationId'] and x['status']=='PASS' and x.get('http_status',200)>=400 for x in rows)} for r in cat['operations']}
    totals={s:sum(r['status']==s for r in rows) for s in ('PASS','FAIL','SKIP')}
    return {'mode':'isolated-local' if isolated else 'live-read-only','base_url':base,'catalog_operations':len(inv),'totals':totals,'ok':totals['FAIL']==0,'rows':rows,'operation_coverage':coverage,
      'limitations':'SKIP is not PASS. Negative validation is not a successful business operation. No live DSH/Hermes/Skills/MCP/Feishu effect was executed.'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--base');p.add_argument('--isolated',action='store_true');p.add_argument('--report',default='reports/api-report.json');args=p.parse_args()
    if not args.base and not args.isolated:p.error('choose --isolated or --base URL')
    if args.isolated:
        from server import create_server
        with tempfile.TemporaryDirectory() as td:
            s=create_server('127.0.0.1',0,str(Path(td)/'check.sqlite3'));th=threading.Thread(target=s.serve_forever,daemon=True);th.start()
            try:r=run_suite('http://127.0.0.1:'+str(s.server_port),True)
            finally:s.agent.shutdown();s.shutdown();s.server_close()
    else:r=run_suite(args.base,False)
    path=Path(args.report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:r[k] for k in ('ok','mode','catalog_operations','totals','limitations')},ensure_ascii=False))
    return 0 if r['ok'] else 1
if __name__=='__main__':sys.exit(main())
