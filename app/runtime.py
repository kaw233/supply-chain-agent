"""Scenario runtime: ingestion → deterministic rules → cases → actions → evidence."""
import csv
import io
import json
import hashlib
import math
import copy
import difflib
import re
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .store import Store,now,uid,dump,load
from .engine import DomainError,evaluate,validate_pack,format_text
from .processing import process

RANK={'high':0,'medium':1,'unknown':2,'normal':3}
LABELS={'high':'高风险','medium':'需关注','unknown':'数据待补充','normal':'正常'}

def digest(x): return hashlib.sha256(dump(x).encode()).hexdigest()[:24]

def public_config(config):
    x=copy.deepcopy(config); x.pop('demo_records',None); return x

class Runtime:
    def __init__(self,db_path,pack_dir,recover=True):
        self.agent=None
        self.store=Store(db_path); self.pack_dir=Path(pack_dir)
        from .rag import RAGService
        self.rag=RAGService(self)
        from .data_workspace import DataWorkspace
        self.workspace=DataWorkspace(self)
        if recover: self.store.recover()
        with self.store.db() as c: empty=c.execute('SELECT COUNT(*) FROM scenarios').fetchone()[0]==0
        if empty: self.seed()
        else: self.workspace.import_legacy()
    def pack(self,sid):
        with self.store.db() as c:
            row=c.execute('SELECT * FROM scenarios WHERE id=?',(sid,)).fetchone()
            if not row: raise DomainError('场景不存在：'+str(sid),404)
            return load(row['config'])
    def scenario_row(self,sid):
        with self.store.db() as c:
            r=c.execute('SELECT * FROM scenarios WHERE id=?',(sid,)).fetchone()
            if not r: raise DomainError('场景不存在',404)
            return dict(r)
    def seed(self):
        for path in sorted(self.pack_dir.glob('*.json')):
            p=json.loads(path.read_text(encoding='utf-8')); self.install(p,seed=True)
            original=copy.deepcopy(p.get('demo_records',[]))
            # These are actual calculations over explicitly synthetic historical snapshots.
            for day in range(6,0,-1):
                rows=copy.deepcopy(original)
                for i,row in enumerate(rows):
                    if i%4==0:
                        if 'stock' in row and row['stock'] is not None: row['stock']=max(0,row['stock']-day*5)
                        if 'delay_days' in row: row['delay_days']+=max(0,day-2)
                        if 'defective' in row: row['defective']+=day
                self.ingest(p['id'],rows,'replace',run=False)
                stamp=(datetime.now(timezone.utc)-timedelta(days=day)).replace(hour=1,minute=0,second=0,microsecond=0).isoformat()
                self.run(p['id'],trigger='demo_history',historical_at=stamp)
            self.ingest(p['id'],original,'replace',run=False)
            self.run(p['id'],trigger='initial_demo')
    def install(self,p,seed=False):
        report=validate_pack(p)
        if not report['ok']: raise DomainError('场景包校验未通过',422,report)
        p=copy.deepcopy(p); sid=p['id']; records=p.pop('demo_records',[])
        p.setdefault('template','operations'); p.setdefault('version',1)
        with self.store.db() as c:
            existing=c.execute('SELECT id FROM scenarios WHERE id=?',(sid,)).fetchone()
            if existing: raise DomainError('同名场景已经存在；请更改 id 创建新实例，或使用配置发布升级。',409)
            c.execute('INSERT INTO scenarios(id,config,updated_at) VALUES (?,?,?)',(sid,dump(p),now()))
            c.execute('INSERT INTO revisions(scenario,version,body,created_at) VALUES (?,?,?,?)',(sid,p['version'],dump(p),now()))
            self.store.event(c,sid,'package.installed',sid,{'name':p['name'],'version':p['version'],'validation':report})
        if records: self.ingest(sid,records,'replace',run=not seed)
        elif not seed: self.run(sid,trigger='package_import')
        return {'id':sid,'validation':report,'records':len(records)}
    def list_scenarios(self):
        with self.store.db() as c:
            rows=c.execute('SELECT * FROM scenarios ORDER BY CASE id WHEN "materials" THEN 0 WHEN "delivery" THEN 1 ELSE 2 END,id').fetchall()
        out=[]
        for r in rows:
            p=load(r['config']); d=self.dashboard(p['id'])
            out.append({'id':p['id'],'name':p['name'],'subtitle':p.get('subtitle',''),'template':p.get('template','operations'),'version':p['version'],'description':p.get('description',''),'metrics':d['metrics'],'revision':r['revision']})
        return out
    def ingest(self,sid,records,mode='upsert',run=True,event_id=None):
        p=self.pack(sid)
        if mode not in ('upsert','replace'): raise DomainError('mode 必须为 upsert 或 replace')
        if not isinstance(records,list) or len(records)>20000: raise DomainError('records 必须是最多 20,000 行的列表')
        id_source=p.get('mapping',{}).get('id','id'); seen=set()
        for r in records:
            if not isinstance(r,dict) or not str(r.get(id_source,'')).strip(): raise DomainError('每条记录都需要稳定的业务 id')
            key=str(r[id_source]); r[id_source]=key
            if key in seen: raise DomainError('本批数据存在重复 id：'+key)
            seen.add(key)
        with self.store.db() as c:
            if mode=='replace': c.execute('DELETE FROM records WHERE scenario=?',(sid,))
            for r in records:
                c.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(scenario,id) DO UPDATE SET body=excluded.body',(sid,str(r[id_source]),dump(r)))
            c.execute('UPDATE scenarios SET source_revision=source_revision+1,updated_at=? WHERE id=?',(now(),sid))
            self.store.event(c,sid,'data.ready',sid,{'rows':len(records),'mode':mode,'event_id':event_id})
        event_key=digest({'scenario':sid,'event':event_id}) if event_id else digest({'scenario':sid,'mode':mode,'records':records})
        result={'rows':len(records),'mode':mode,'event_key':event_key}
        if run and p.get('triggers',{}).get('data_ready',True): result['run']=self.run(sid,trigger='data_ready')
        if run and self.agent and result.get('run'):
            result['automatic_task']=self.agent.on_data_event(sid,event_key,result['run']['id'])
        return result
    def ingest_bundle(self,sid,data):
        with self.store.lock:
            tables=data.get('tables',[]);main=data.get('main_table','source')
            table=next((x for x in tables if x.get('id')==main),None)
            if table is None:raise DomainError('main_table 未包含在 tables',400)
            self.workspace.set_sources(sid,tables)
            return self.ingest(sid,table['rows'],data.get('mode','replace'),event_id=data.get('event_id'))

    def ingest_feishu_batch(self, sid, data):
        """Accept a scheduled Feishu CLI manifest; never calls Feishu itself.

        The scheduler owns authentication, image download and OCR.  The
        workbench stores only a bounded, secret-free manifest and optionally
        feeds already-normalized records into the deterministic pipeline.
        """
        self.pack(sid)
        if not isinstance(data, dict):
            raise DomainError('飞书批次必须是 JSON 对象', 400)
        event_id = str(data.get('event_id') or '').strip()
        if not event_id or len(event_id) > 200:
            raise DomainError('飞书批次需要稳定的 event_id', 400)
        pulled_at = str(data.get('pulled_at') or '').strip()
        if not pulled_at or len(pulled_at) > 80:
            raise DomainError('飞书批次需要 pulled_at 时间', 400)
        source = data.get('source') or {}
        if not isinstance(source, dict):
            raise DomainError('飞书 source 必须是对象', 400)
        artifacts = data.get('artifacts') or []
        if not isinstance(artifacts, list) or len(artifacts) > 200:
            raise DomainError('artifacts 必须是最多 200 项的列表', 400)
        clean = []
        seen = set()
        for item in artifacts:
            if not isinstance(item, dict):
                raise DomainError('每个飞书附件必须是对象', 400)
            artifact_id = str(item.get('artifact_id') or item.get('id') or '').strip()
            if not artifact_id or len(artifact_id) > 200 or artifact_id in seen:
                raise DomainError('飞书附件需要批次内唯一的 artifact_id', 400)
            seen.add(artifact_id)
            kind = str(item.get('kind') or 'image').strip()
            if kind not in ('image', 'document', 'table', 'other'):
                raise DomainError('飞书附件 kind 只能是 image/document/table/other', 400)
            uri = str(item.get('uri') or '').strip()
            if len(uri) > 2000 or re.search(r'(access_token|tenant_access_token|authorization|cookie|secret|token)\s*[:=]', uri, re.I):
                raise DomainError('飞书附件 uri 不得包含凭据', 400)
            relative_path = str(item.get('relative_path') or '').strip().replace('\\', '/')
            if relative_path:
                parts = relative_path.split('/')
                if relative_path.startswith('/') or ':' in relative_path or '..' in parts:
                    raise DomainError('飞书附件 relative_path 必须是工作区内相对路径', 400)
                expected_prefix = f'data/integrations/feishu/{sid}/'
                if not relative_path.startswith(expected_prefix):
                    raise DomainError('飞书附件 relative_path 必须位于 data/integrations/feishu/{scenario_id}/', 400)
            sha256 = str(item.get('sha256') or '').strip().lower()
            if sha256 and not re.fullmatch(r'[0-9a-f]{64}', sha256):
                raise DomainError('飞书附件 sha256 必须是 64 位十六进制摘要', 400)
            metadata = item.get('metadata') or {}
            if not isinstance(metadata, dict):
                raise DomainError('飞书附件 metadata 必须是对象', 400)
            def contains_secret(value):
                if isinstance(value, dict):
                    for key, nested in value.items():
                        if re.search(r'(token|secret|password|cookie|authorization|credential)', str(key), re.I):
                            return True
                        if contains_secret(nested):
                            return True
                elif isinstance(value, list):
                    return any(contains_secret(x) for x in value)
                return False
            if contains_secret(metadata):
                raise DomainError('飞书附件 metadata 不得包含 token、secret、cookie 或凭据字段', 400)
            clean.append({
                'artifact_id': artifact_id,
                'kind': kind,
                'title': str(item.get('title') or '')[:300],
                'uri': uri,
                'relative_path': relative_path,
                'sha256': sha256,
                'captured_at': str(item.get('captured_at') or '')[:80],
                'metadata': copy.deepcopy(metadata),
            })
        records = data.get('records')
        if records is not None and (not isinstance(records, list) or len(records) > 20000):
            raise DomainError('飞书批次 records 必须是最多 20,000 行的列表', 400)
        with self.store.db() as c:
            previous = c.execute('SELECT body FROM feishu_batches WHERE scenario=? AND event_id=?', (sid, event_id)).fetchone()
        if previous:
            result = load(previous['body'])
            result['reused'] = True
            return result
        # Detect an artifact-id collision before touching the deterministic data
        # pipeline. A rejected manifest must not leave a partial business run.
        with self.store.db() as c:
            for artifact in clean:
                old = c.execute('SELECT body FROM feishu_artifacts WHERE scenario=? AND artifact_id=?', (sid, artifact['artifact_id'])).fetchone()
                if old and load(old['body']) != artifact:
                    raise DomainError('artifact_id 已存在但内容发生变化；请使用新的稳定编号', 409)
        data_result = None
        if records is not None:
            data_result = self.ingest(sid, records, data.get('mode', 'upsert'), event_id=event_id)
        body = {
            'scenario_id': sid,
            'event_id': event_id,
            'pulled_at': pulled_at,
            'source': {k: str(v)[:500] for k, v in source.items() if k in ('provider', 'job_id', 'skill', 'space', 'folder')},
            'artifacts': clean,
            'artifact_count': len(clean),
            'records_count': len(records or []),
            'data_result': data_result,
            'received_at': now(),
            'contract': 'scheduled-feishu-cli-v1',
            'reused': False,
        }
        with self.store.db() as c:
            c.execute('INSERT INTO feishu_batches VALUES (?,?,?,?)', (sid, event_id, dump(body), now()))
            for artifact in clean:
                c.execute('INSERT OR IGNORE INTO feishu_artifacts VALUES (?,?,?,?,?)', (sid, artifact['artifact_id'], event_id, dump(artifact), now()))
            self.store.event(c, sid, 'feishu.batch.received', event_id, {
                'event_id': event_id, 'artifact_count': len(clean), 'records_count': len(records or []),
                'provider': body['source'].get('provider', 'scheduled-cli'),
            })
        return body

    def feishu_artifacts(self, sid, limit=50):
        self.pack(sid)
        try:
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            raise DomainError('limit 必须是整数', 400)
        with self.store.db() as c:
            batches = [load(r['body']) for r in c.execute('SELECT body FROM feishu_batches WHERE scenario=? ORDER BY created_at DESC LIMIT ?', (sid, limit))]
            artifacts = [load(r['body']) | {'event_id': r['event_id'], 'received_at': r['created_at']} for r in c.execute('SELECT body,event_id,created_at FROM feishu_artifacts WHERE scenario=? ORDER BY created_at DESC LIMIT ?', (sid, limit))]
        return {'scenario_id': sid, 'batches': batches, 'artifacts': artifacts, 'contract': 'scheduled-feishu-cli-v1'}
    def pull_source(self,sid):
        p=self.pack(sid); cfg=p.get('data_source') or {}; kind=cfg.get('kind','manual'); location=str(cfg.get('location','')).strip()
        if kind=='manual': raise DomainError('当前使用接口写入 / 文件导入；请导入数据，或先保存文件 / URL 数据源。',409)
        try:
            if kind=='file':
                path=Path(location).expanduser()
                if not path.is_absolute(): path=self.pack_dir.parent/path
                if path.stat().st_size>12*1024*1024: raise DomainError('数据源超过 12 MB')
                text=path.read_text(encoding='utf-8-sig')
            elif kind=='url':
                if urlsplit(location).scheme not in ('http','https'): raise DomainError('来源需要 HTTP(S) 网址')
                with urlopen(Request(location,headers={'Accept':'application/json,text/csv'}),timeout=15) as response:
                    raw=response.read(12*1024*1024+1)
                if len(raw)>12*1024*1024: raise DomainError('数据源超过 12 MB')
                text=raw.decode('utf-8-sig')
            else: raise DomainError('未知数据源类型')
            if urlsplit(location).path.lower().endswith('.csv'): rows=list(csv.DictReader(io.StringIO(text)))
            else:
                obj=json.loads(text); rows=obj if isinstance(obj,list) else obj.get('records')
            result=self.ingest(sid,rows,'replace'); result['source']=location
            return result
        except Exception as ex:
            with self.store.db() as c: self.store.event(c,sid,'data.error',sid,{'error':str(ex),'source':location,'phase':'source_read'})
            if isinstance(ex,DomainError): raise
            raise DomainError('数据来源读取失败；保留上一次投影：'+str(ex),422) from ex

    def raw_records(self,sid):
        self.pack(sid)
        with self.store.db() as c: return [load(r[0]) for r in c.execute('SELECT body FROM records WHERE scenario=? ORDER BY id',(sid,))]
    def run(self,sid,trigger='manual',parameters=None,historical_at=None):
        with self.store.lock:
            p=self.pack(sid); params={**p.get('parameters',{}),**(parameters or {})}
            if parameters:
                if set(parameters)-set(p.get('parameters',{})): raise DomainError('本次参数包含未知字段')
                if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in parameters.values()): raise DomainError('运行参数必须是数字')
            gate={'manual':'manual','agent':'agent','data_ready':'data_ready'}.get(trigger)
            if gate and not p.get('triggers',{}).get(gate,True): raise DomainError('该触发入口已停用',409)
            raw=self.raw_records(sid); cooked_pack={**p,'parameters':params}
            stages=[];sources=self.workspace.sources(sid)
            try: processed=process(cooked_pack,raw,capture=stages,sources=sources)
            except Exception as ex:
                with self.store.db() as c: self.store.event(c,sid,'data.error',sid,{'error':str(ex),'phase':'processing'})
                raise
            run_id=uid('RUN'); stamp=historical_at or now(); started=now()
            results=[evaluate(p,r,params) for r in processed]
            counts=Counter(r['severity'] for r in results); source=self.scenario_row(sid)
            summary={'id':run_id,'scenario':sid,'trigger':trigger,'status':'completed','created_at':stamp,'executed_at':started,'synthetic_history':bool(historical_at),'pack_version':p['version'],'source_revision':source['source_revision'],'parameters':params,'total':len(results),'counts':dict(counts),'normal_rate':round(counts['normal']/max(len(results),1)*100,1),'impact':round(sum(r.get('impact',0) or 0 for r in results if r['severity']!='normal'),2),'steps':[{'label':'读取数据','count':len(raw),'status':'completed'},{'label':'语义映射','count':len(p['fields']),'status':'completed'},{'label':'规则计算','count':len(p['rules']),'status':'completed'},{'label':'感知信号','count':len(results)-counts['normal'],'status':'completed'},{'label':'Case 投影','count':len(results)-counts['normal'],'status':'completed'}]}
            new_cases=[]
            with self.store.db() as c:
                if not historical_at:
                    c.execute('DELETE FROM results WHERE scenario=?',(sid,))
                    existing={r['object_id']:(r['id'],load(r['body'])) for r in c.execute('SELECT * FROM cases WHERE scenario=?',(sid,))}
                    present=set()
                    for row in results:
                        oid=str(row['id']); present.add(oid); row['run_id']=run_id; row['fingerprint']=digest({k:v for k,v in row.items() if k not in ('trace','run_id','fingerprint')})
                        cid,case=existing.get(oid,(uid('CASE'),None))
                        if row['severity']!='normal':
                            is_new=case is None
                            if is_new:
                                case={'id':cid,'scenario':sid,'object_id':oid,'title':format_text(p.get('case',{}).get('title','{name}'),row),'status':'open','created_at':stamp,'feedback':[],'source_present':True}; new_cases.append(cid)
                            elif case['status']=='closed':
                                case['status']='open'; case['reopened_at']=stamp
                            elif case['status']=='ready_to_close': case['status']='in_progress'
                            case.update(site=row.get('site',''),severity=row['severity'],reason=row['reason'],owner=row.get(p.get('case',{}).get('owner_field','owner'),'未分配'),updated_at=stamp,run_id=run_id,source_present=True)
                        elif case:
                            case.update(severity='normal',reason=row['reason'],updated_at=stamp,run_id=run_id,source_present=True)
                            if case['status']!='closed': case['status']='ready_to_close'
                        if case:
                            c.execute('INSERT INTO cases VALUES (?,?,?,?) ON CONFLICT(scenario,object_id) DO UPDATE SET body=excluded.body',(cid,sid,oid,dump(case)))
                            row['case_id']=cid
                        c.execute('INSERT INTO results VALUES (?,?,?,?)',(sid,oid,dump(row),run_id))
                    for oid,(cid,case) in existing.items():
                        if oid not in present and case['status']!='closed':
                            case.update(severity='unknown',reason='源对象已从本批数据中移除，不能据此认定业务问题已解决。',source_present=False,updated_at=stamp,run_id=run_id)
                            c.execute('UPDATE cases SET body=? WHERE id=?',(dump(case),cid))
                summary['new_cases']=len(new_cases)
                mapped=[]
                for record in processed:
                    item=dict(record)
                    for target,origin in p.get('mapping',{}).items():item[target]=record.get(origin)
                    mapped.append(item)
                stages.append({'id':'mapped','label':'语义映射结果','kind':'intermediate','rows':mapped,'mapping':p.get('mapping',{}),'inputs':[stages[-1]['id']]})
                stages.append({'id':'results','label':'业务分类与计算结果','kind':'result','rows':results,'derived':p.get('derived',[]),'rules':p.get('rules',[]),'inputs':['mapped']})
                frozen_cases=[load(x[0]) for x in c.execute('SELECT body FROM cases WHERE scenario=?',(sid,))] if not historical_at else [{'id':r.get('case_id','HIST-'+str(r['id'])),'object_id':r['id'],'severity':r['severity'],'reason':r['reason'],'owner':r.get('owner'),'site':r.get('site'),'status':'snapshot_risk'} for r in results if r['severity']!='normal']
                stages.append({'id':'cases','label':'当次 Case 状态','kind':'business','rows':frozen_cases,'inputs':['results']})
                self.workspace.capture(c,run_id,sid,summary,p,stages,[{k:v for k,v in x.items() if k!='rows'} for x in sources])
                snapshot={'summary':summary,'workspace_snapshot':run_id}
                c.execute('INSERT INTO runs VALUES (?,?,?,?)',(run_id,sid,dump(snapshot),stamp))
                self.store.event(c,sid,'rules.completed',run_id,summary)
            if new_cases and p.get('triggers',{}).get('auto_prepare') and not historical_at:
                try: summary['auto_action']=self.prepare_action(sid,p['triggers'].get('auto_capability','email.send'),new_cases,origin='trigger')['id']
                except DomainError as e:
                    with self.store.db() as c: self.store.event(c,sid,'action.prepare_failed',run_id,{'error':str(e)})
            return summary
    def rows(self,sid,filters=None):
        self.pack(sid)
        with self.store.db() as c: rows=[load(r[0]) for r in c.execute('SELECT body FROM results WHERE scenario=?',(sid,))]
        f=filters or {}
        for key in ('severity','owner','site'):
            val=f.get(key)
            if val and val!='all': rows=[r for r in rows if str(r.get(key))==str(val)]
        q=str(f.get('q','')).strip().lower()
        if q: rows=[r for r in rows if q in ' '.join(str(v) for k,v in r.items() if k!='trace').lower()]
        return sorted(rows,key=lambda r:(RANK.get(r['severity'],9),str(r['id'])))
    def cases(self,sid):
        with self.store.db() as c: rows=[load(r[0]) for r in c.execute('SELECT body FROM cases WHERE scenario=?',(sid,))]
        return sorted(rows,key=lambda r:(r['status']=='closed',RANK.get(r['severity'],9),r['object_id']))
    def case_detail(self,cid):
        with self.store.db() as c:
            r=c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone()
            if not r: raise DomainError('Case 不存在',404)
            case=load(r['body']); obj=c.execute('SELECT body FROM results WHERE scenario=? AND id=?',(case['scenario'],case['object_id'])).fetchone()
            case['record']=load(obj[0]) if obj else None
            actions=[load(a[0]) for a in c.execute('SELECT body FROM actions WHERE scenario=? ORDER BY created_at DESC,rowid DESC',(case['scenario'],))]
            case['actions']=[a for a in actions if cid in a.get('case_ids',[])]
            case['events']=[{'kind':r['kind'],'time':r['created_at'],'body':load(r['body'])} for r in c.execute('SELECT * FROM events WHERE entity=? ORDER BY seq DESC LIMIT 30',(cid,))]
            return case
    def update_case(self,cid,data):
        case=self.case_detail(cid); case.pop('record',None); case.pop('actions',None); case.pop('events',None)
        status=data.get('status')
        if status:
            if status not in ('open','in_progress','waiting','ready_to_close','closed'): raise DomainError('未知 Case 状态')
            if status in ('closed','ready_to_close') and (case['severity']!='normal' or not case.get('source_present',True)): raise DomainError('当前风险尚未解除。请修正数据并重跑规则，不能以发送成功代替业务验收。',409)
            case['status']=status
        if 'owner' in data and str(data['owner']).strip(): case['owner']=str(data['owner']).strip()
        if data.get('note'):
            case.setdefault('feedback',[]).append({'id':uid('FB'),'text':str(data['note'])[:4000],'source':str(data.get('source','人工登记'))[:300],'created_at':now()})
        case['updated_at']=now()
        with self.store.db() as c:
            c.execute('UPDATE cases SET body=? WHERE id=?',(dump(case),cid))
            self.store.event(c,case['scenario'],'case.updated',cid,{'status':case['status'],'note':data.get('note',''),'source':data.get('source','')})
        return self.case_detail(cid)
    def dashboard(self,sid,filters=None):
        p=self.pack(sid); rows=self.rows(sid,filters); cases=self.cases(sid); ids={r['id'] for r in rows}
        f=filters or {}
        def missing_matches(x):
            if x.get('source_present',True): return False
            if any(f.get(k) not in (None,'','all',str(x.get(k,''))) for k in ('severity','owner','site')): return False
            q=str(f.get('q','')).strip().lower()
            return not q or q in (x['object_id']+' '+x['title']+' '+x.get('owner','')).lower()
        cases=[x for x in cases if x['object_id'] in ids or missing_matches(x)]
        counts=Counter(r['severity'] for r in rows); active=sum(x['status']!='closed' for x in cases)
        with self.store.db() as c:
            rr=c.execute('SELECT body FROM runs WHERE scenario=? ORDER BY created_at DESC,rowid DESC LIMIT 20',(sid,)).fetchall()
            events=[{'seq':e['seq'],'kind':e['kind'],'entity':e['entity'],'body':load(e['body']),'time':e['created_at']} for e in c.execute('SELECT * FROM events WHERE scenario=? ORDER BY seq DESC LIMIT 12',(sid,))]
            actions=[load(a[0]) for a in c.execute('SELECT body FROM actions WHERE scenario=? ORDER BY created_at DESC,rowid DESC',(sid,))]
        runs=[load(r[0])['summary'] for r in rr]
        byowner=defaultdict(Counter); bysite=defaultdict(Counter)
        for row in rows:
            byowner[row.get('owner','未分配')][row['severity']]+=1
            bysite[row.get('site','未分配')][row['severity']]+=1
        allrows=self.rows(sid)
        with self.store.db() as c:
            hr=c.execute("SELECT kind,body,created_at FROM events WHERE scenario=? AND kind IN ('data.error','rules.completed') ORDER BY seq DESC LIMIT 1",(sid,)).fetchone()
        health={'error':load(hr['body']).get('error',''),'at':hr['created_at']} if hr and hr['kind']=='data.error' else {}
        return {'source_health':health,'scenario':public_config(p),'metrics':{'total':len(rows),'normal':counts['normal'],'high':counts['high'],'medium':counts['medium'],'unknown':counts['unknown'],'normal_rate':round(counts['normal']/max(len(rows),1)*100,1),'active_cases':active,'impact':round(sum(r.get('impact',0) or 0 for r in rows if r['severity']!='normal'),2),'pending_actions':sum(a['status']=='draft' for a in actions),'processed':sum(a['status']=='submitted' for a in actions),'closed':sum(x['status']=='closed' for x in cases)},'counts':dict(counts),'rows':rows,'cases':cases,'history':list(reversed(runs[:14])),'latest_run':runs[0] if runs else None,'events':events,'workload':[{'name':k,**dict(v)} for k,v in byowner.items()],'sites':[{'name':k,**dict(v)} for k,v in bysite.items()],'filters':{'owners':sorted({r.get('owner','') for r in allrows}),'sites':sorted({r.get('site','') for r in allrows})},'actions':actions[:30]}
    def get_run(self,rid):
        with self.store.db() as c:
            r=c.execute('SELECT body FROM runs WHERE id=?',(rid,)).fetchone()
            if not r: raise DomainError('运行记录不存在',404)
            snap=load(r[0])
            if snap.get('workspace_snapshot'):
                v=c.execute('SELECT * FROM dw_versions WHERE id=?',(rid,)).fetchone()
                if not v:raise DomainError('快照目录缺失',500)
                def table(name):
                    return [self.workspace.blob(c,x['hash']) for x in c.execute('SELECT hash FROM dw_rows WHERE snapshot=? AND table_id=? ORDER BY ordinal',(rid,name))]
                snap.update(config=self.workspace.blob(c,v['config_hash']),input=table('source'),results=table('results'))
                snap['sources']=[{'id':t['id'],'rows':table(t['id']),**load(t['definition']).get('source',{})} for t in c.execute("SELECT * FROM dw_tables WHERE snapshot=? AND kind='source' AND id!='source'",(rid,))]
            return snap
    def replay(self,rid):
        snap=self.get_run(rid); s=snap['summary']; results=[evaluate(snap['config'],r,s['parameters']) for r in process({**snap['config'],'parameters':s['parameters']},snap['input'],sources=snap.get('sources',[]))]
        exclude={'run_id','fingerprint','case_id'}
        strip=lambda x:{k:v for k,v in x.items() if k not in exclude}
        same=[strip(r) for r in results]==[strip(r) for r in snap['results']]
        report={'ok':same,'run_id':rid,'total':len(results),'pack_version':s['pack_version'],'detail':'使用已保存的输入快照、参数与规则版本重新计算；不会执行外部动作。'}
        with self.store.db() as c: self.store.event(c,s['scenario'],'replay.completed',rid,report)
        return report
    def events(self,sid=None,after=0,limit=100):
        with self.store.db() as c:
            sql='SELECT * FROM events WHERE seq>?'; args=[int(after)]
            if sid: sql+=' AND scenario=?'; args.append(sid)
            sql+=' ORDER BY seq DESC LIMIT ?'; args.append(min(int(limit),500))
            return [{'seq':r['seq'],'scenario':r['scenario'],'kind':r['kind'],'entity':r['entity'],'body':load(r['body']),'time':r['created_at']} for r in c.execute(sql,args)]
    def prepare_action(self,sid,capability,case_ids,params=None,origin='ui',idempotency_key=None):
        params=params or {}; p=self.pack(sid)
        if p.get('actions',{}).get(capability,{}).get('ui','notification')!='notification': raise DomainError('该能力不是通知草稿类型')
        if capability not in p.get('actions',{}): raise DomainError('场景没有绑定该动作')
        if not isinstance(case_ids,list) or not case_ids: raise DomainError('请明确选择至少一个 Case，不会默认发送全域。')
        case_ids=list(dict.fromkeys(case_ids)); plan_fingerprint=digest({'scenario':sid,'capability':capability,'case_ids':case_ids,'params':params})
        with self.store.lock:
            if idempotency_key:
                with self.store.db() as c: prev=c.execute('SELECT body FROM actions WHERE idempotency_key=?',(idempotency_key,)).fetchone()
                if prev:
                    a=load(prev[0])
                    if a['request_fingerprint']!=plan_fingerprint: raise DomainError('同一幂等键已用于不同请求',409)
                    return a
            details=[self.case_detail(cid) for cid in case_ids]
            if any(x['scenario']!=sid for x in details): raise DomainError('所选 Case 不属于当前场景')
            if any(not x.get('record') or x['status']=='closed' for x in details): raise DomainError('已关闭或数据缺失的 Case 不能发送新通知')
            binding=p['actions'][capability]; groupkey=binding.get('group_by','owner')
            action_id=uid('ACT')
            groups=defaultdict(list)
            for x in details: groups[str(x['record'].get(groupkey,'未分配')) if groupkey!='all' else 'all'].append(x)
            messages=[]
            for index,(key,items) in enumerate(groups.items()):
                owner=items[0]['owner'] if len({x['owner'] for x in items})==1 else '相关责任人'
                values={'scene':p['name'],'owner':owner,'count':len(items),'items':'\n'.join(f"- {x['object_id']} {x['title']}：{x['reason']}" for x in items)}
                recipient_field=binding.get('recipient_field','')
                targets={str(x['record'].get(recipient_field) or '').strip() for x in items} if recipient_field else set()
                # The binding decides the recipient field. No made-up chat ID or provider fallback.
                recipient=str(params.get('recipient') or (next(iter(targets)) if len(targets)==1 else '')).strip()
                message={'id':f'M{index+1}','recipient':recipient,'subject':format_text(binding.get('subject','[{scene}] 待处理事项'),values),'body':format_text(binding.get('body','{items}'),values),'case_ids':[x['id'] for x in items],'status':'pending'}
                if binding.get('operation_type')=='expedite':
                    if len(items)!=1: raise DomainError('催交操作必须按业务对象单独生成',409)
                    row=items[0]['record']; shortage=row.get('shortage')
                    if not isinstance(shortage,(int,float)) or shortage<=0: raise DomainError('只能对存在确定缺口的对象准备催交',409)
                    message['payload']={
                        'idempotency_key':f'{action_id}:M{index+1}',
                        'material_id':items[0]['object_id'],
                        'material_name':items[0]['title'],
                        'supplier':recipient,
                        'shortage_qty':shortage,
                        'due_days':row.get('due_days'),
                        'expected_object_version':row['fingerprint'],
                        'reason':items[0]['reason'],
                    }
                messages.append(message)
            a={'id':action_id,'scenario':sid,'capability':capability,'name':binding.get('name',capability),'binding':copy.deepcopy(binding),'case_ids':case_ids,'messages':messages,'status':'draft','draft_revision':1,'origin':origin,'mode':'hermes','pack_version':p['version'],'record_fingerprints':{x['object_id']:x['record']['fingerprint'] for x in details},'request_fingerprint':plan_fingerprint,'created_at':now(),'updated_at':now(),'blocked':any(not m['recipient'] for m in messages)}
            with self.store.db() as c:
                c.execute('INSERT INTO actions VALUES (?,?,?,?,?)',(a['id'],sid,dump(a),idempotency_key,now()))
                self.store.event(c,sid,'action.drafted',a['id'],{'capability':capability,'messages':len(messages),'cases':len(case_ids),'mode':a['mode'],'blocked':a['blocked']})
            return a
    def action(self,aid):
        with self.store.db() as c:
            r=c.execute('SELECT body FROM actions WHERE id=?',(aid,)).fetchone()
            if not r: raise DomainError('动作不存在',404)
            return load(r[0])
    def save_action(self,a):
        a['updated_at']=now()
        with self.store.db() as c: c.execute('UPDATE actions SET body=? WHERE id=?',(dump(a),a['id']))
    def edit_action(self,aid,changes):
        with self.store.lock:
            a=self.action(aid)
            if a['status']!='draft': raise DomainError('只有草稿可编辑',409)
            if changes.get('cancel'):
                a['status']='cancelled'
            else:
                incoming=changes.get('messages',[])
                if len(incoming)!=len(a['messages']): raise DomainError('编辑不能改变分组数量')
                for old,new in zip(a['messages'],incoming):
                    if a.get('binding',{}).get('operation_type')=='expedite' and str(new.get('recipient',old['recipient'])).strip()!=old['recipient']:
                        raise DomainError('结构化催交的供应商来自业务数据；请修改源数据后重新生成草稿',409)
                    for key in ('recipient','subject','body'): old[key]=str(new.get(key,old[key]))[:20000]
                a['blocked']=any(not m['recipient'].strip() for m in a['messages'])
            a['draft_revision']=a.get('draft_revision',1)+1
            self.save_action(a); return a
    def execute_action(self,aid,expected_revision=None):
        """A business-confirmed plan is delegated once to the configured real Agent."""
        if not self.agent: raise DomainError('Agent 任务服务尚未初始化',503)
        with self.store.lock:
            a=self.action(aid)
            if a['status']!='draft': return a
            if expected_revision is None or int(expected_revision)!=a.get('draft_revision',1):
                raise DomainError('确认需要对应当前草稿版本；请重新打开预览。',409)
            p=self.pack(a['scenario']); rows={r['id']:r for r in self.rows(a['scenario'])}
            if p['version']!=a['pack_version']: raise DomainError('场景配置已变化，请重新生成草稿',409)
            for oid,fp in a['record_fingerprints'].items():
                if oid not in rows or rows[oid].get('fingerprint')!=fp: raise DomainError('预览后的数据已变化，请重新生成草稿',409)
            if a['blocked']: raise DomainError('请补齐明确收件人，不会让 AI 猜测。',409)
            a['confirmation']={'revision':a.get('draft_revision',1),'operator':self.store.settings()['workspace']['operator'],'at':now(),'content_hash':digest(a['messages'])}
            a['status']='dispatching'; self.save_action(a)
        binding=a.get('binding') or p['actions'][a['capability']]
        prompt=(f"执行业务工作台已确认动作 {aid}，能力 {a['capability']}。只处理以下确切目标和正文，不扩大范围、不重复发送。\n"
                +self.skill_instruction(binding)+"\n"
                "每条操作后可通过工作台桥接 action.receipt 登记 message_id、status(submitted/failed/unknown)、provider_ref 和 evidence。"
                "这里的回执登记是可选结果适配，不要求你重写 Skill，也不是另一条发送通道。"
                "提交成功不等于已送达或业务完成。\n"+dump(a['messages']))
        try:
            t=self.agent.submit({'scenario_id':a['scenario'],'message':prompt,'request_id':'ACTION-'+aid,'context':{'action_id':aid,'case_ids':a['case_ids']},'origin':'business_action'})
            with self.store.lock:
                a=self.action(aid); a['agent_task_id']=t['id']
                if a['status']=='dispatching': a['status']='executing'
                self.save_action(a)
            return a
        except Exception as ex:
            with self.store.lock:
                a=self.action(aid); a.update(status='draft',error=str(ex)); self.save_action(a)
            raise

    def action_receipt(self,aid,message_id,status,provider_ref,evidence,task_id):
        with self.store.lock:
            a=self.action(aid)
            if a.get('agent_task_id') not in (None,task_id): raise DomainError('执行回执不属于当前动作',409)
            if a['status'] not in ('dispatching','executing','needs_review','partial'): raise DomainError('动作不在可登记执行结果的状态',409)
            m=next((m for m in a['messages'] if m['id']==message_id),None)
            if not m: raise DomainError('消息条目标识错误',404)
            if status not in ('submitted','failed','unknown'): raise DomainError('回执状态只能为 submitted / failed / unknown')
            if status=='submitted' and not str(provider_ref or '').strip(): raise DomainError('提交回执必须附外部记录编号')
            receipt={'status':status,'provider_ref':provider_ref,'evidence':str(evidence)[:4000],'reported_by':'Agent tool bridge','reported_at':now(),'independently_verified':False}
            if m['status']=='submitted': return a
            m.update(status=status,receipt=receipt)
            states={x['status'] for x in a['messages']}
            a['status']=next(iter(states)) if len(states)==1 and 'pending' not in states else 'partial'
            self.save_action(a)
            with self.store.db() as c: self.store.event(c,a['scenario'],'action.receipt',aid,receipt)
            return a

    def agent_action_finished(self,t):
        aid=t.get('context',{}).get('action_id')
        if not aid: return
        with self.store.lock:
            a=self.action(aid)
            if a.get('agent_task_id') not in (None,t['id']): return
            if a['status'] in ('dispatching','executing','unknown','partial'):
                pending=[m for m in a['messages'] if m['status']=='pending']
                for m in pending:
                    m['status']='unknown'
                # A stopped chat is not proof that a message was never sent.
                states={m['status'] for m in a['messages']}
                if states=={'submitted'}: a['status']='submitted'
                elif 'submitted' in states: a['status']='partial'
                else: a['status']='needs_review'
                a.update(agent_task_id=t['id'],agent_status=t['status'],error='Agent 本轮已结束；无确切回执的分项结果待核实，不自动重试。')
                self.save_action(a)

    @staticmethod
    def skill_instruction(binding):
        hint=str(binding.get('skill_hint') or '').strip()
        return (str(binding.get('instruction') or '使用合适的已安装 Skill 完成本次操作。')
                +(f' 优先使用用户指定的 Skill：{hint}。' if hint else ' Skill 名称未限定，由当前 Agent 使用实际已安装的合适技能。')
                +' 不要临时编写 SMTP/飞书直连接口来代替缺少的 Skill；缺技能或授权时明确报告未执行，不得伪造成功或回执。')

    def capabilities(self,sid):
        common=[{'id':'data.query','name':'业务数据查询','type':'read','description':'SQLite 规则投影，无需 Agent'},
                {'id':'rules.run','name':'确定性检查','type':'compute','description':'Python 规则计算，不调用模型'},
                {'id':'report.generate','name':'运行摘要','type':'read','description':'固定业务快照摘要，可交由 Agent 深入分析'}]
        for key,b in self.pack(sid).get('actions',{}).items():
            common.append({'id':key,'name':b.get('name',key),'type':'write' if b.get('ui','notification')=='notification' else 'read',
                'executor':'hermes_skill','confirmation':b.get('ui','notification')=='notification',
                'description':b.get('instruction','使用已安装的 Skill')+' 执行方：当前 Agent。', 'skill_hint':b.get('skill_hint','')})
        return common

    def invoke(self,data):
        sid=data.get('scenario_id');
        if not sid:
            installed=self.list_scenarios()
            if len(installed)!=1:raise DomainError('请明确 scenario_id；不会默认 materials',400)
            sid=installed[0]['id']
        cap=data.get('capability'); params=data.get('parameters',{})
        binding=self.pack(sid).get('actions',{}).get(cap)
        if binding:
            if binding.get('executor','hermes_skill')!='hermes_skill': raise DomainError('此模板的外部业务操作由 Agent Tools / Skills 执行')
            if binding.get('ui','notification')=='notification':
                return {'type':'action_draft','action':self.prepare_action(sid,cap,data.get('case_ids',[]),params,data.get('origin','api'),data.get('idempotency_key'))}
            if not self.agent: raise DomainError('Agent 任务服务尚未初始化',503)
            case_ids=list(data.get('case_ids') or ([params['case_id']] if params.get('case_id') else []))
            task=self.agent.submit({'scenario_id':sid,'message':self.skill_instruction(binding)+'\n本次业务参数：'+dump(params),
                'context':{'case_ids':case_ids,'capability':cap,'parameters':copy.deepcopy(params)},
                'origin':'business_read','request_id':data.get('request_id') or uid('SKILL')})
            return {'type':'agent_task','task':task}
        if cap=='data.query': return {'type':'query_result','result':{'rows':self.rows(sid,params),'source':'SQLite / latest rule projection'}}
        if cap=='report.generate':
            d=self.dashboard(sid,params); m=d['metrics']; txt=f"# {d['scenario']['name']} · 业务运行摘要\n\n生成时间：{now()}\n\n数据为演示样本或用户导入数据；不推断真实收益。\n\n当前范围共有 {m['total']} 个对象，正常 {m['normal']} 个、高风险 {m['high']} 个、需关注 {m['medium']} 个、数据待补充 {m['unknown']} 个。\n\n未关闭 Case：{m['active_cases']}。通知任务成功不代表业务问题解决。\n\n## 优先关注\n"+'\n'.join('- '+r['id']+' '+r['name']+'：'+r['reason'] for r in d['rows'][:8])
            with self.store.db() as c: self.store.event(c,sid,'report.generated',sid,{'markdown':txt})
            return {'type':'report','result':{'markdown':txt}}
        if cap=='rules.run': return {'type':'run','result':self.run(sid,trigger=data.get('origin','manual') if data.get('origin') in ('agent','manual') else 'manual',parameters=params)}
        raise DomainError('未知能力：'+str(cap))
    def draft_config(self,sid,config):
        old=self.pack(sid); config=copy.deepcopy(config)
        if config.get('id')!=sid: raise DomainError('升级配置不能更改场景 id')
        config['version']=old['version']+1
        # Generator cannot silently relax acceptance in an ordinary config change.
        if config.get('acceptance')!=old.get('acceptance'): raise DomainError('本入口不允许修改验收基准；业务基准更新需另行明确评审，本入口不会放宽验收')
        report=validate_pack(config)
        before=self.rows(sid); raw=self.raw_records(sid); after=[evaluate(config,r) for r in process(config,raw)] if report['ok'] else []
        previous={r['id']:r for r in before}
        changes=[{'id':r['id'],'from':previous.get(r['id'],{}).get('severity','unknown'),'to':r['severity']} for r in after if previous.get(r['id'],{}).get('severity')!=r['severity']]
        d={'id':uid('DRAFT'),'scenario':sid,'base_version':old['version'],'config':config,'validation':report,'impact':{'changed':len(changes),'examples':changes[:20],'before':dict(Counter(r['severity'] for r in before)),'after':dict(Counter(r['severity'] for r in after))},'status':'draft','created_at':now(), 'diff':'\n'.join(difflib.unified_diff(json.dumps(old,ensure_ascii=False,indent=2).splitlines(),json.dumps(config,ensure_ascii=False,indent=2).splitlines(),fromfile='当前业务定义',tofile='修改后业务定义',lineterm=''))}
        with self.store.db() as c: c.execute('INSERT INTO drafts VALUES (?,?,?,?)',(d['id'],sid,dump(d),now()))
        return d
    def get_draft(self,did):
        with self.store.db() as c:
            r=c.execute('SELECT body FROM drafts WHERE id=?',(did,)).fetchone()
            if not r: raise DomainError('配置草稿不存在',404)
            return load(r[0])
    def publish(self,did):
        with self.store.lock:
            d=self.get_draft(did); sid=d['scenario']; p=self.pack(sid)
            if d['status']=='published': return d
            if p['version']!=d['base_version']: raise DomainError('基础版本已经变化，请重新生成草稿',409)
            v=validate_pack(d['config'])
            if not v['ok']: raise DomainError('验收未通过，不能发布',422,v)
            process(d['config'],self.raw_records(sid))  # validate live input before committing new code
            with self.store.db() as c:
                c.execute('UPDATE scenarios SET config=?,revision=revision+1,updated_at=? WHERE id=?',(dump(d['config']),now(),sid))
                c.execute('INSERT INTO revisions(scenario,version,body,created_at) VALUES (?,?,?,?)',(sid,d['config']['version'],dump(d['config']),now()))
                d['status']='published'; d['published_at']=now()
                c.execute('UPDATE drafts SET body=? WHERE id=?',(dump(d),did)); self.store.event(c,sid,'package.published',did,{'version':d['config']['version']})
            d['run']=self.run(sid,trigger='config_published'); return d
    def revisions(self,sid):
        with self.store.db() as c: return [{'version':r['version'],'config':load(r['body']),'created_at':r['created_at']} for r in c.execute('SELECT * FROM revisions WHERE scenario=? ORDER BY id DESC',(sid,))]
    def rollback(self,sid,version):
        target=next((r for r in self.revisions(sid) if r['version']==int(version)),None)
        if not target: raise DomainError('版本不存在',404)
        return self.publish(self.draft_config(sid,target['config'])['id'])
