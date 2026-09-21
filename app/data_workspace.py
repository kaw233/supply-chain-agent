"""Immutable, indexed data snapshots. Same selector drives metrics and drill-down.
Rows are content-addressed and zlib-compressed once, then referenced by snapshot/table.
There is no per-version SQL table, OFFSET-heavy history scan or whole-table UI preload.
"""
from __future__ import annotations
import base64,copy,hashlib,json,re,sqlite3,time,zlib
from collections import Counter
from datetime import datetime,timezone
from .store import now,dump,load,uid
from .engine import DomainError

SCHEMA='''
CREATE TABLE IF NOT EXISTS dw_blobs(hash TEXT PRIMARY KEY,content BLOB NOT NULL,bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dw_versions(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,scenario TEXT NOT NULL,data_at TEXT NOT NULL,created_at TEXT NOT NULL,rule_hash TEXT NOT NULL,config_hash TEXT NOT NULL,summary TEXT NOT NULL,source_times TEXT NOT NULL,legacy INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS dw_versions_date ON dw_versions(scenario,data_at DESC,seq DESC);
CREATE TABLE IF NOT EXISTS dw_tables(snapshot TEXT NOT NULL,id TEXT NOT NULL,label TEXT NOT NULL,kind TEXT NOT NULL,row_count INTEGER NOT NULL,columns_json TEXT NOT NULL,definition TEXT NOT NULL,PRIMARY KEY(snapshot,id));
CREATE TABLE IF NOT EXISTS dw_rows(snapshot TEXT NOT NULL,table_id TEXT NOT NULL,ordinal INTEGER NOT NULL,row_key TEXT NOT NULL,hash TEXT NOT NULL,severity TEXT,owner TEXT,site TEXT,rule_id TEXT,PRIMARY KEY(snapshot,table_id,ordinal));
CREATE UNIQUE INDEX IF NOT EXISTS dw_row_key ON dw_rows(snapshot,table_id,row_key);
CREATE INDEX IF NOT EXISTS dw_row_filter ON dw_rows(snapshot,table_id,severity,ordinal);
CREATE INDEX IF NOT EXISTS dw_row_owner ON dw_rows(snapshot,table_id,owner,ordinal);
CREATE INDEX IF NOT EXISTS dw_row_site ON dw_rows(snapshot,table_id,site,ordinal);
CREATE TABLE IF NOT EXISTS dw_notes(id TEXT PRIMARY KEY,scenario TEXT NOT NULL,snapshot TEXT NOT NULL,table_id TEXT,row_key TEXT,actor TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dw_note_snapshot ON dw_notes(scenario,snapshot,created_at);
CREATE TABLE IF NOT EXISTS dw_source_tables(scenario TEXT NOT NULL,id TEXT NOT NULL,metadata TEXT NOT NULL,body_hash TEXT NOT NULL,PRIMARY KEY(scenario,id));
'''

def digest(x):return hashlib.sha256(dump(x).encode()).hexdigest()
def cursor_encode(data):return base64.urlsafe_b64encode(dump(data).encode()).decode().rstrip('=')
def cursor_decode(text):
    try:return json.loads(base64.urlsafe_b64decode(text+'='*(-len(text)%4)))
    except Exception:raise DomainError('分页游标无效',400)
def normalized_time(value):
    if not value:return ''
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt.replace(tzinfo=timezone.utc).isoformat(timespec='seconds') if dt.tzinfo is None else dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except ValueError:raise DomainError('时间应为 ISO 日期或日期时间',400)
def cap_limit(v,default=50):
    try:return max(1,min(200,int(v or default)))
    except (ValueError,TypeError):raise DomainError('page_size 必须是整数',400)

def columns_for(rows):
    keys=[];seen=set()
    for row in rows:
        for k in row:
            if k not in seen and k not in ('_source_refs',):seen.add(k);keys.append(k)
    return [{'key':k,'label':k,'type':next(('number' if isinstance(r.get(k),(int,float)) and not isinstance(r.get(k),bool) else 'object' if isinstance(r.get(k),(dict,list)) else 'text' for r in rows if r.get(k) is not None),'text')} for k in keys]

class DataWorkspace:
    def __init__(self,rt):
        self.rt=rt;self.store=rt.store
        with self.store.db() as c:c.executescript(SCHEMA)
    def put_blob(self,c,obj):
        raw=dump(obj).encode();h=hashlib.sha256(raw).hexdigest()
        c.execute('INSERT OR IGNORE INTO dw_blobs VALUES(?,?,?)',(h,zlib.compress(raw,4),len(raw)))
        return h
    def blob(self,c,h):
        row=c.execute('SELECT content FROM dw_blobs WHERE hash=?',(h,)).fetchone()
        if row is None:raise DomainError('快照内容缺失：'+str(h),500)
        return json.loads(zlib.decompress(row[0]))
    def capture(self,c,run_id,sid,summary,config,tables,source_times=None,legacy=False):
        if c.execute('SELECT 1 FROM dw_versions WHERE id=?',(run_id,)).fetchone():return
        cfg_hash=self.put_blob(c,config);rule_hash=digest({k:config.get(k) for k in ('mapping','parameters','processor','pipeline','derived','rules','data_sources')})
        c.execute('INSERT INTO dw_versions(id,scenario,data_at,created_at,rule_hash,config_hash,summary,source_times,legacy) VALUES(?,?,?,?,?,?,?,?,?)',
          (run_id,sid,normalized_time(summary['created_at']),normalized_time(summary.get('executed_at',now())),rule_hash,cfg_hash,dump(summary),dump(source_times or []),int(legacy)))
        labels={f['key']:f.get('label',f['key']) for f in config.get('fields',[])}
        labels.update({f['key']:f.get('label',f['key']) for f in config.get('derived',[])})
        for table in tables:
            rows=table['rows'];cols=columns_for(rows)
            for col in cols:col['label']=labels.get(col['key'],col['label'])
            definition={k:v for k,v in table.items() if k not in ('rows','id','label','kind')}
            definition.setdefault('lineage_mode','explicit' if any('_source_refs' in r for r in rows) else 'table-level')
            c.execute('INSERT INTO dw_tables VALUES(?,?,?,?,?,?,?)',(run_id,table['id'],table.get('label',table['id']),table.get('kind','intermediate'),len(rows),dump(cols),dump(definition)))
            used=set();packed=[]
            for i,row in enumerate(rows):
                refs=row.get('_source_refs',[])
                if not isinstance(refs,list) or any(not isinstance(x,dict) or 'table' not in x or 'key' not in x for x in refs):raise DomainError('逐行来源 _source_refs 需为 {table,key} 列表；不能保存不完整引用',422)
                key=str(row.get('id',row.get(config.get('mapping',{}).get('id','id'),i)))
                if key in used:key=f'{key}#{i}'
                used.add(key);h=self.put_blob(c,row)
                packed.append((run_id,table['id'],i,key,h,str(row.get('severity','')),str(row.get('owner','')),str(row.get('site','')),str(row.get('rule_id',''))))
            c.executemany('INSERT INTO dw_rows VALUES(?,?,?,?,?,?,?,?,?)',packed)
    def import_legacy(self):
        """One-time conversion of genuinely stored old snapshots; never invent lost intermediates."""
        with self.store.db() as c:
            rows=c.execute('SELECT r.* FROM runs r LEFT JOIN dw_versions v ON v.id=r.id WHERE v.id IS NULL ORDER BY r.created_at').fetchall()
            for r in rows:
                old=load(r['body']);p=old.get('config');summary=old.get('summary')
                if not p or not summary or 'results' not in old:continue
                tables=[{'id':'source','label':'原始输入（历史已保存）','kind':'source','rows':old.get('input',[]),'lineage_mode':'legacy-source'},
                        {'id':'results','label':'计算结果','kind':'result','rows':old['results'],'lineage_mode':'legacy-no-intermediate','missing':'旧版没有保存中间表，不能重建为原执行事实'}]
                self.capture(c,r['id'],r['scenario'],summary,p,tables,legacy=True)
    def resolve(self,sid,snapshot=None):
        self.rt.pack(sid)
        with self.store.db() as c:
            if snapshot and snapshot!='latest':r=c.execute('SELECT * FROM dw_versions WHERE scenario=? AND id=?',(sid,snapshot)).fetchone()
            else:r=c.execute('SELECT * FROM dw_versions WHERE scenario=? ORDER BY data_at DESC,seq DESC LIMIT 1',(sid,)).fetchone()
            if not r:raise DomainError('此场景没有对应数据快照',404)
            return dict(r)
    def versions(self,sid,q):
        self.rt.pack(sid);limit=cap_limit(q.get('limit'),30);where=['scenario=?'];args=[sid]
        for name,op in [('from','>='),('to','<=')]:
            if q.get(name):where.append(f'data_at {op} ?');args.append(normalized_time(q[name]))
        if q.get('cursor'):
            token=cursor_decode(q['cursor'])
            if token.get('scope')!=[sid,q.get('from',''),q.get('to','')]:raise DomainError('游标不属于当前时间筛选',400)
            where.append('(data_at<? OR (data_at=? AND seq<?))');args += [token['time'],token['time'],token['seq']]
        with self.store.db() as c:rows=c.execute('SELECT seq,id,data_at,created_at,summary,rule_hash,legacy FROM dw_versions WHERE '+' AND '.join(where)+' ORDER BY data_at DESC,seq DESC LIMIT ?',args+[limit+1]).fetchall()
        page=[dict(r)|{'summary':load(r['summary'])} for r in rows[:limit]]
        next_cursor=None
        if len(rows)>limit:
            last=page[-1];next_cursor=cursor_encode({'scope':[sid,q.get('from',''),q.get('to','')],'time':last['data_at'],'seq':last['seq']})
        return {'items':page,'next_cursor':next_cursor,'page_size':limit}
    def table_list(self,sid,snapshot):
        v=self.resolve(sid,snapshot)
        with self.store.db() as c:rows=c.execute('SELECT * FROM dw_tables WHERE snapshot=? ORDER BY rowid',(v['id'],)).fetchall()
        return {'snapshot':self.meta(v),'tables':[dict(r)|{'columns':load(r['columns_json']),'definition':load(r['definition'])} for r in rows]}
    @staticmethod
    def meta(v):return {k:v[k] for k in ('id','scenario','data_at','created_at','rule_hash','legacy')}
    def _filter(self,table,q):
        clauses=[];args=[]
        for key in ('severity','owner','site','rule_id'):
            value=q.get(key)
            if value and value!='all':clauses.append(key+'=?');args.append(str(value))
        if q.get('risk')=='1':clauses.append("severity<>'normal'")
        if q.get('q'):clauses.append('instr(lower(row_key),lower(?))>0');args.append(str(q['q']))
        return clauses,args
    def rows(self,sid,snapshot,table,q):
        v=self.resolve(sid,snapshot);limit=cap_limit(q.get('limit'));where=['snapshot=?','table_id=?'];args=[v['id'],table];extra,values=self._filter(table,q);where+=extra;args+=values
        scope=digest([sid,v['id'],table,{k:q.get(k) for k in ('severity','owner','site','rule_id','risk','q')}]);start=-1
        if q.get('cursor'):
            token=cursor_decode(q['cursor'])
            if token.get('scope')!=scope:raise DomainError('游标已过期，请从当前快照和筛选首页开始',409)
            start=int(token['ordinal'])
        with self.store.db() as c:
            definition=c.execute('SELECT * FROM dw_tables WHERE snapshot=? AND id=?',(v['id'],table)).fetchone()
            if not definition:raise DomainError('快照中没有这张表',404)
            total=c.execute('SELECT count(*) FROM dw_rows WHERE '+' AND '.join(where),args).fetchone()[0]
            refs=c.execute('SELECT ordinal,row_key,hash FROM dw_rows WHERE '+' AND '.join(where)+' AND ordinal>? ORDER BY ordinal LIMIT ?',args+[start,limit+1]).fetchall()
            items=[{'key':r['row_key'],'ordinal':r['ordinal'],'data':self.blob(c,r['hash'])} for r in refs[:limit]]
        next_cursor=cursor_encode({'scope':scope,'ordinal':refs[limit-1]['ordinal']}) if len(refs)>limit else None
        return {'snapshot':self.meta(v),'table':table,'columns':load(definition['columns_json']),'items':items,'total':total,'next_cursor':next_cursor,'page_size':limit,'filters':{k:q[k] for k in ('severity','owner','site','rule_id','risk','q') if q.get(k)}}
    def summary(self,sid,snapshot=None):
        v=self.resolve(sid,snapshot)
        with self.store.db() as c:
            config=self.blob(c,v['config_hash']);summary=load(v['summary']);latest=c.execute('SELECT id,data_at FROM dw_versions WHERE scenario=? ORDER BY data_at DESC,seq DESC LIMIT 1',(sid,)).fetchone()
            groups=[dict(r) for r in c.execute("SELECT owner,site,severity,count(*) AS count FROM dw_rows WHERE snapshot=? AND table_id='results' GROUP BY owner,site,severity",(v['id'],))]
            last_error=c.execute("SELECT body,created_at FROM events WHERE scenario=? AND kind='data.error' ORDER BY seq DESC LIMIT 1",(sid,)).fetchone()
        source_health={'status':'last_update_failed','error':load(last_error['body']),'at':last_error['created_at']} if last_error and last_error['created_at']>=v['created_at'] else {'status':'last_success_available'}
        return {'source_health':source_health,'snapshot':self.meta(v),'latest':dict(latest),'summary':summary,'groups':groups,'scenario':config,'source_times':load(v['source_times']),
                'drill_contract':{'table':'results','snapshot':v['id'],'filter_keys':['severity','owner','site','rule_id','risk']},'history_is_immutable':True}
    def rules(self,sid,snapshot):
        v=self.resolve(sid,snapshot)
        with self.store.db() as c:p=self.blob(c,v['config_hash']);summary=load(v['summary'])
        return {'snapshot':self.meta(v),'config_hash':v['config_hash'],'rule_hash':v['rule_hash'],'config':p,'effective_parameters':summary.get('parameters',p.get('parameters',{})),
                'execution_order':['来源与数据合同','预处理步骤','字段映射','派生公式（顺序）','分类（首个命中）'],
                'source_documents':p.get('rule_sources',[]),'source_coverage':p.get('rule_coverage',[]),
                'coverage_note':'仅展示已提供/登记的业务原文与映射。未提供或未登记的正式规则不能被系统声明为已完整实施。'}
    def lineage(self,sid,snapshot,table,key):
        v=self.resolve(sid,snapshot)
        with self.store.db() as c:
            ref=c.execute('SELECT hash FROM dw_rows WHERE snapshot=? AND table_id=? AND row_key=?',(v['id'],table,str(key))).fetchone()
            if not ref:raise DomainError('行不存在于所选快照',404)
            row=self.blob(c,ref['hash']);cfg=self.blob(c,v['config_hash']);table_row=c.execute('SELECT * FROM dw_tables WHERE snapshot=? AND id=?',(v['id'],table)).fetchone();definition=load(table_row['definition'])
            refs=row.get('_source_refs',[]);mode='explicit'
            if table=='cases' and row.get('object_id'):refs=[{'table':'results','key':str(row['object_id'])}];mode='case-object-link'
            if not refs:
                # An unchanged id alone is NOT proof that arbitrary SQL/Python used just that input row.
                if table in ('mapped','calculated','results') and not cfg.get('pipeline') and cfg.get('processor',{}).get('kind','identity')=='identity':
                    refs=[{'table':'source','key':str(row.get('id',key))}];mode='identity-mapping'
                else:mode='table-level-only'
            sources=[]
            for source in refs:
                found=c.execute('SELECT hash FROM dw_rows WHERE snapshot=? AND table_id=? AND row_key=?',(v['id'],source.get('table'),str(source.get('key')))).fetchone()
                sources.append({**source,'row':self.blob(c,found['hash']) if found else None,'resolved':bool(found)})
            # Follow only explicit links (or the deterministic identity mapping).
            # Cap this on-demand explanation, never materialise the whole dataset.
            nodes=[];edges=[];warnings=[];visited=set()
            def follow(tab,rkey,path,depth=0):
                pair=(str(tab),str(rkey))
                if pair in path:warnings.append('来源引用出现循环：'+str(pair));return
                if pair in visited:return
                if depth>16 or len(nodes)>=256:warnings.append('该行来源链超过单次展示预算，请沿具体来源继续查看');return
                record=c.execute('SELECT hash FROM dw_rows WHERE snapshot=? AND table_id=? AND row_key=?',(v['id'],pair[0],pair[1])).fetchone()
                meta=c.execute('SELECT kind,definition FROM dw_tables WHERE snapshot=? AND id=?',(v['id'],pair[0])).fetchone()
                if not record or not meta:warnings.append('引用的来源行未保存在快照：'+str(pair));return
                visited.add(pair);value=self.blob(c,record['hash']);spec=load(meta['definition'])
                nodes.append({'table':pair[0],'key':pair[1],'kind':meta['kind'],'row':value,'stage':spec})
                links=value.get('_source_refs',[])
                if pair[0]=='cases' and value.get('object_id'):links=[{'table':'results','key':str(value['object_id'])}]
                if not links and pair[0] in ('mapped','calculated','results') and not cfg.get('pipeline') and cfg.get('processor',{}).get('kind','identity')=='identity':links=[{'table':'source','key':str(value.get('id',rkey))}]
                if not links:
                    if meta['kind']!='source':warnings.append('处理表 '+pair[0]+' 没有记录逐行输入，不能推测为唯一来源')
                    return
                for link in links:
                    if not isinstance(link,dict) or 'table' not in link or 'key' not in link:warnings.append('来源引用格式不完整：'+str(pair));continue
                    source=(str(link['table']),str(link['key']));edges.append({'from':{'table':source[0],'key':source[1]},'to':{'table':pair[0],'key':pair[1]}});follow(*source,path|{pair},depth+1)
            follow(table,str(key),set())
            notes=[dict(n) for n in c.execute('SELECT * FROM dw_notes WHERE snapshot=? AND table_id=? AND row_key=? ORDER BY created_at',(v['id'],table,str(key)))]
        return {'snapshot':self.meta(v),'table':table,'key':str(key),'row':row,'stage':definition,'sources':sources,'lineage_mode':mode,
                'rule_trace':row.get('trace',next((x.get('row',{}).get('trace',[]) for x in sources if x.get('row')),[])),'effective_parameters':load(v['summary']).get('parameters',{}),'mapping':cfg.get('mapping',{}),'notes':notes,
                'source_chain':{'nodes':nodes,'edges':edges,'complete':not warnings},'limitations':list(dict.fromkeys(warnings))}
    def note(self,sid,data):
        v=self.resolve(sid,data.get('snapshot'));note=str(data.get('note','')).strip()
        if not note:raise DomainError('请填写复盘意见',400)
        if data.get('table'):
            with self.store.db() as c:
                if not c.execute('SELECT 1 FROM dw_tables WHERE snapshot=? AND id=?',(v['id'],data['table'])).fetchone():raise DomainError('快照中不存在此表',404)
                if data.get('key') is not None and not c.execute('SELECT 1 FROM dw_rows WHERE snapshot=? AND table_id=? AND row_key=?',(v['id'],data['table'],str(data['key']))).fetchone():raise DomainError('快照中不存在此行',404)
        record={'id':uid('NOTE'),'scenario':sid,'snapshot':v['id'],'table_id':data.get('table'),'row_key':str(data['key']) if data.get('key') is not None else None,'actor':str(data.get('actor') or self.store.settings()['workspace']['operator']),'note':note,'created_at':now()}
        with self.store.db() as c:
            c.execute('INSERT INTO dw_notes VALUES(?,?,?,?,?,?,?,?)',tuple(record.values()));self.store.event(c,sid,'snapshot.annotated',v['id'],record)
        return record
    def prompt(self,sid,data):
        line=self.lineage(sid,data.get('snapshot'),data.get('table','results'),str(data['key']))
        action=data.get('action','explain');instruction={'explain':'解释这条数据如何形成，逐项引用可核验的原始行、处理代码、参数和规则命中。缺失链路明确说明，不要推测成事实。','remind':'准备催办建议或通知草稿，先核对当前实际状态和收件人。不要把历史快照风险当成当前事实；发送必须经当前确认并由 Hermes Skill 执行。','assign':'核对这个对象当前负责人，提出指派建议；不要在未确认对象和权限时写回。','claim':'核对当前任务是否仍可认领，提出认领方案，先说明将改变哪些业务记录。'}.get(action,str(action))
        return {'text':instruction+'\n\n当前依据是固定历史/当前快照 '+line['snapshot']['id']+'，数据时间 '+line['snapshot']['data_at']+'。\n'+dump(line),
                'context':{'scenario_id':sid,'snapshot_id':line['snapshot']['id'],'table':line['table'],'row_key':line['key'],'historical':self.resolve(sid)['id']!=line['snapshot']['id']},'auto_send':False}
    def set_sources(self,sid,tables):
        self.rt.pack(sid)
        if not isinstance(tables,list):raise DomainError('tables 应为列表',400)
        names=[t.get('id') for t in tables if isinstance(t,dict)]
        if len(names)!=len(tables) or len(set(names))!=len(names):raise DomainError('来源表 ID 不能重复',400)
        with self.store.db() as c:
            for table in tables:
                name=table.get('id');rows=table.get('rows')
                if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',str(name)) or name in ('results','mapped','calculated','cases'):raise DomainError('输入表 id 非法或使用保留名',400)
                if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):raise DomainError('来源表 rows 必须为对象列表',400)
                h=self.put_blob(c,rows);meta={k:v for k,v in table.items() if k!='rows'};meta.setdefault('ingested_at',now())
                c.execute('INSERT INTO dw_source_tables VALUES(?,?,?,?) ON CONFLICT(scenario,id) DO UPDATE SET metadata=excluded.metadata,body_hash=excluded.body_hash',(sid,name,dump(meta),h))
        return {'tables':len(tables),'rows':sum(len(x['rows']) for x in tables)}
    def sources(self,sid):
        with self.store.db() as c:
            return [load(r['metadata'])|{'rows':self.blob(c,r['body_hash'])} for r in c.execute('SELECT * FROM dw_source_tables WHERE scenario=? ORDER BY id',(sid,)).fetchall()]
