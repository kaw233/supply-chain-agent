"""Host-trusted SQL/Python business pipeline. Every declared stage is captured.
Python can return rows or {rows, tables}; opaque hidden intermediates are NOT invented.
Only explicit _source_refs are treated as exact multi-table row provenance.
"""
import ast,copy,json,os,re,sqlite3,subprocess,sys
from .engine import DomainError

def _table_name(name):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',str(name)):raise DomainError('处理表名称不合法：'+str(name),422)
    return name

def process(pack,records,capture=None,sources=None):
    capture=capture if capture is not None else []
    tables={'source':copy.deepcopy(records)}
    for table in sources or []:tables[_table_name(table['id'])]=copy.deepcopy(table['rows'])
    # Base source is always exactly the records used for processing; named sources are supplementary inputs.
    tables['source']=copy.deepcopy(records)
    for name,rows in tables.items():
        meta=next(({k:v for k,v in x.items() if k!='rows'} for x in (sources or []) if x['id']==name),{})
        capture.append({'id':name,'label':meta.get('label',name),'kind':'source','rows':copy.deepcopy(rows),'source':meta or pack.get('data_source',{'kind':'manual'}),'lineage_mode':'original'})
    cfg=pack.get('processor') or {'kind':'identity'}
    stages=pack.get('pipeline') or [dict(cfg,id='processed',input='source')]
    current=records
    for i,stage in enumerate(stages):
        name=_table_name(stage.get('id','stage_'+str(i+1)));kind=stage.get('kind','identity');input_id=stage.get('input','source' if i==0 else stages[i-1].get('id','stage_'+str(i)))
        if name in tables:raise DomainError('处理阶段不能覆盖已有表名：'+name,422)
        if input_id not in tables:raise DomainError('处理阶段输入表不存在：'+str(input_id),422)
        current=tables[input_id];source=stage.get('source','')
        if kind=='identity':result=copy.deepcopy(current);extra=[]
        elif kind=='sql':
            db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
            try:
                q=lambda s:'"'+str(s).replace('"','""')+'"'
                for table_id,rows in tables.items():
                    keys=sorted({k for row in rows for k in row}) or ['id']
                    db.execute('CREATE TABLE '+q(table_id)+' ('+','.join(q(k) for k in keys)+')')
                    db.executemany('INSERT INTO '+q(table_id)+' VALUES ('+','.join('?' for _ in keys)+')',[[json.dumps(row.get(k),ensure_ascii=False) if isinstance(row.get(k),(dict,list)) else row.get(k) for k in keys] for row in rows])
                db.commit();db.execute('PRAGMA query_only=ON');ticks=[0]
                def budget():ticks[0]+=1;return 1 if ticks[0]>int(os.getenv('WORKBENCH_SQL_TICKS','10000')) else 0
                db.set_progress_handler(budget,1000)
                result=[dict(r) for r in db.execute(source)];extra=[]
                for row in result:
                    if isinstance(row.get('_source_refs'),str):
                        try:row['_source_refs']=json.loads(row['_source_refs'])
                        except ValueError:row.pop('_source_refs',None)
            except sqlite3.Error as ex:raise DomainError('SQL 阶段 '+name+' 失败：'+str(ex),422) from ex
            finally:db.close()
        elif kind=='python':
            try:ast.parse(source)
            except (SyntaxError,TypeError) as ex:raise DomainError('Python 阶段 '+name+' 语法错误：'+str(ex),422)
            driver='''import json,sys,contextlib,inspect
x=json.load(sys.stdin);ns={}
with contextlib.redirect_stdout(sys.stderr):
 exec(compile(x['source'],'business_processor.py','exec'),ns)
 f=ns.get('transform')
 if not callable(f):raise ValueError('需要定义 transform(rows, parameters) 或 transform(rows, parameters, tables)')
 out=f(x['records'],x['parameters'],x['tables']) if len(inspect.signature(f).parameters)>=3 else f(x['records'],x['parameters'])
json.dump(out,sys.stdout,ensure_ascii=False,allow_nan=False)'''
            try:
                proc=subprocess.run([sys.executable,'-I','-c',driver],input=json.dumps({'source':source,'records':current,'parameters':pack.get('parameters',{}),'tables':tables},ensure_ascii=False),text=True,encoding='utf8',capture_output=True,timeout=float(os.getenv('WORKBENCH_PROCESSOR_TIMEOUT','30')))
                if proc.returncode:raise DomainError('Python 阶段 '+name+' 失败：'+proc.stderr[-5000:],422)
                obj=json.loads(proc.stdout)
                if isinstance(obj,dict) and 'rows' in obj:
                    result=obj['rows'];extra=[{'id':k,'rows':v} for k,v in obj.get('tables',{}).items()]
                else:result=obj;extra=[]
            except subprocess.TimeoutExpired as ex:raise DomainError('Python 阶段 '+name+' 超过配置时限，原投影保留',422) from ex
            except (ValueError,OSError) as ex:raise DomainError('Python 阶段 '+name+' 输出不合法：'+str(ex),422) from ex
        else:raise DomainError('处理类型为 identity / sql / python',422)
        if not isinstance(result,list) or any(not isinstance(x,dict) for x in result):raise DomainError('阶段 '+name+' 必须返回对象列表',422)
        for item in extra:
            tid=_table_name(item['id']);rows=item['rows']
            if tid in tables or tid==name:raise DomainError('中间表重复：'+tid,422)
            if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):raise DomainError('中间表需为对象列表',422)
            tables[tid]=rows;capture.append({'id':tid,'label':tid,'kind':'intermediate','rows':copy.deepcopy(rows),'producer':name,'inputs':[input_id],'lineage_mode':'explicit' if any('_source_refs' in r for r in rows) else 'table-level'})
        tables[name]=result;capture.append({'id':name,'label':stage.get('label',name),'kind':'intermediate','rows':copy.deepcopy(result),'inputs':stage.get('inputs',[input_id]),'processor':stage,'lineage_mode':'identity' if kind=='identity' else 'explicit' if any('_source_refs' in r for r in result) else 'table-level'})
        current=result
    idkey=pack.get('mapping',{}).get('id','id');ids=[str(r.get(idkey,'')) for r in current]
    if any(not x for x in ids) or len(ids)!=len(set(ids)):raise DomainError('处理输出缺少稳定 id 或重复 id；请在合并/聚合阶段生成业务主键',422)
    return current
