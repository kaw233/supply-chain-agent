"""Business/tool bridge. Not a second Agent; no email/Feishu skill included."""
import hashlib
import hmac
import json
from .engine import DomainError
from .store import dump, load
from .agent import TERMINAL

CATALOG={
 'describe':{'parameters':{},'effect':'read','description':'此目录；普通聊天不要求 JSON'},
 'data.query':{'parameters':{'filters':'可选 severity/owner/site/q','limit':'最多2000'},'effect':'read'},
 'data.raw':{'parameters':{},'effect':'read'},
 'data.ingest':{'parameters':{'records':'记录数组','mode':'upsert/replace','event_id':'来源事件编号'},'effect':'business_write'},
 'data.pull':{'parameters':{},'effect':'business_write','description':'读取已配置 file/url 数据源并计算'},
 'rules.run':{'parameters':{'parameters':'已声明数字参数，可选'},'effect':'compute'},
 'case.get':{'parameters':{'case_id':'明确的 Case 编号'},'effect':'read'},
 'case.feedback':{'parameters':{'case_id':'编号','note':'反馈正文','source':'来源','status':'可选'},'effect':'business_write'},
 'view':{'parameters':{'type':'set_filter/navigate/open_case','filters':'筛选对象','page':'页面','case_id':'Case编号'},'effect':'view_only'},
 'action.prepare':{'parameters':{'capability':'当前业务 actions 中的能力标识','case_ids':'明确选中的 Case 数组'},'effect':'draft','description':'只生成工作台业务确认草稿，不发送'},
 'action.receipt':{'parameters':{'action_id':'已确认动作','message_id':'消息编号','status':'submitted/failed/unknown','provider_ref':'实际外部编号','evidence':'工具回执文本或来源'},'effect':'business_write','description':'记录报告的回执；不声称独立核验外部投递'},
 'processing.get':{'parameters':{},'effect':'read','description':'当前配置、参数、映射、派生公式和 SQL/Python 处理代码'},
 'processing.preview':{'parameters':{'config':'完整当前配置及你的差异'},'effect':'draft','description':'保留验收样例，预览和试算不覆盖当前配置'},
 'processing.apply':{'parameters':{'draft_id':'试算草稿编号','authorized':'用户明确要求修改并生效时 true'},'effect':'business_write'},
 'workspace.tables':{'parameters':{'snapshot':'可选，默认本任务快照'},'effect':'read'},
 'workspace.rows':{'parameters':{'table':'表名','snapshot':'快照','cursor':'分页游标','limit':'每页最多200'},'effect':'read'},
 'workspace.lineage':{'parameters':{'table':'表名','key':'行key','snapshot':'快照'},'effect':'read'},
 'rag.describe':{'parameters':{},'effect':'read','description':'RAG 接口、适配器和联调状态'},
 'rag.search':{'parameters':{'collection_id':'RAG Collection 编号','query':'检索问题','top_k':'最多50','filters':'可选过滤器'},'effect':'read','description':'只读 RAG 检索；未配置时不返回虚假召回'},
 'rag.citation':{'parameters':{'query_id':'RAG 查询编号','chunk_id':'可选 chunk 编号'},'effect':'read','description':'读取已持久化的 RAG 引用证据'},
 'report':{'parameters':{'filters':'可选'},'effect':'read'}
}
READS={k for k,v in CATALOG.items() if v['effect']=='read'}


def invoke(agent,tid,token,body):
    t=agent.raw_task(tid)
    if not hmac.compare_digest(str(token or ''),t['_bridge_token']): raise DomainError('桥接凭据不正确',403)
    op=body.get('operation'); params=body.get('parameters') or {}
    if op not in CATALOG: raise DomainError('未知桥接操作，先调用 describe',400)
    sid=t['scenario']; rt=agent.rt; selected_snapshot=params.get('snapshot') or t.get('context',{}).get('snapshot_id'); request_id=str(body.get('request_id') or '')
    fp=hashlib.sha256(dump({'operation':op,'parameters':params}).encode()).hexdigest()
    if op not in READS and not request_id: raise DomainError('写入/视图操作需要稳定的 request_id',400)
    # Local stop flag and invocation are serialized. Nothing here resubmits remote tools.
    with agent.store.lock:
        t=agent.raw_task(tid)
        if op not in READS and (t.get('stop_requested') or t['status'] in TERMINAL):
            raise DomainError('本轮已停止或结束，不能再派发它的后续操作；需要人工新建执行。',409)
        if request_id:
            with agent.store.db() as c: prev=c.execute('SELECT * FROM bridge_receipts WHERE task=? AND request_id=?',(tid,request_id)).fetchone()
            if prev:
                if prev['fingerprint']!=fp: raise DomainError('同一操作编号的参数发生变化',409)
                return load(prev['body'])
        if selected_snapshot and op not in READS and op!='view' and rt.workspace.resolve(sid,selected_snapshot)['id']!=rt.workspace.resolve(sid)['id'] and params.get('use_current_confirmed') is not True:
            raise DomainError('当前任务引用历史数据；先核对当前状态，再明确 use_current_confirmed 才能执行写入或准备现实动作。',409)
        if op=='describe': out={'operations':CATALOG,'capabilities':rt.capabilities(sid),'rag':rt.rag.capabilities(),'scenario':sid,'pages':['overview','data','cases','actions','rules','tasks','evidence','settings'],'sql_table':'source','python_contract':'def transform(rows, parameters): return rows','note':'business_write 是记录类型，不是对任意 Skill 的额外审批。'}
        elif op=='rag.describe': out=rt.rag.capabilities()
        elif op=='rag.search': out=rt.rag.query(params.get('collection_id',''),{'query':params.get('query',''),'top_k':params.get('top_k',5),'filters':params.get('filters',{})},scenario=sid)
        elif op=='rag.citation': out=rt.rag.citation(params.get('query_id',''),params.get('chunk_id'),scenario=sid)
        elif op=='workspace.tables':out=rt.workspace.table_list(sid,selected_snapshot)
        elif op=='workspace.rows':out=rt.workspace.rows(sid,selected_snapshot,params.get('table','results'),params)
        elif op=='workspace.lineage':out=rt.workspace.lineage(sid,selected_snapshot,params.get('table','results'),str(params.get('key')))
        elif op=='data.query' and selected_snapshot:out=rt.workspace.rows(sid,selected_snapshot,'results',params.get('filters',{})|{'limit':params.get('limit',100)})
        elif op=='data.raw' and selected_snapshot:out=rt.workspace.rows(sid,selected_snapshot,'source',params)
        elif op=='processing.get' and selected_snapshot:out=rt.workspace.rules(sid,selected_snapshot)
        elif op=='report' and selected_snapshot:out=rt.workspace.summary(sid,selected_snapshot)
        elif op=='data.query': out={'rows':rt.rows(sid,params.get('filters',{}))[:min(2000,int(params.get('limit',100)))],'source':'SQLite 当前规则投影'}
        elif op=='data.raw': out={'records':rt.raw_records(sid)}
        elif op=='data.ingest': out=rt.ingest(sid,params.get('records'),params.get('mode','upsert'),event_id=params.get('event_id'))
        elif op=='data.pull': out=rt.pull_source(sid)
        elif op=='rules.run': out=rt.run(sid,'agent',params.get('parameters'))
        elif op in ('case.get','case.feedback'):
            case=rt.case_detail(params.get('case_id'))
            if case['scenario']!=sid: raise DomainError('Case 不属于当前业务',409)
            out=case if op=='case.get' else rt.update_case(case['id'],params)
        elif op=='view':
            typ=params.get('type'); command={'type':typ}
            if typ=='set_filter': command['filters']={k:str(v) for k,v in params.get('filters',{}).items() if k in ('severity','site','owner','q')}
            elif typ=='navigate':
                if params.get('page') not in ('overview','data','cases','actions','rules','tasks','evidence','settings'): raise DomainError('页面不存在')
                command['page']=params['page']
            elif typ=='open_case':
                case=rt.case_detail(params.get('case_id'))
                if case['scenario']!=sid: raise DomainError('Case 不属于当前业务')
                command['case_id']=case['id']
            else: raise DomainError('只支持明确的页面操作，不能直接运行任意 JS')
            item={'type':'ui','command':command,'request_id':request_id}
            outputs=t.get('outputs',[])+[item]; agent.patch(tid,outputs=outputs)
            out=item
        elif op=='action.prepare':
            a=rt.prepare_action(sid,params.get('capability','email.send'),params.get('case_ids',[]),params.get('parameters',{}),origin='agent',idempotency_key=tid+':'+request_id)
            out={'type':'action_draft','action':a}
            agent.patch(tid,outputs=t.get('outputs',[])+[out])
        elif op=='action.receipt':
            a=rt.action(params.get('action_id'))
            if a['scenario']!=sid or t.get('context',{}).get('action_id')!=a['id']: raise DomainError('不是本任务的已确认业务动作',409)
            out=rt.action_receipt(a['id'],params.get('message_id'),params.get('status'),params.get('provider_ref'),params.get('evidence',''),tid)
        elif op=='processing.get': out={'config':rt.pack(sid),'source_note':'配置是可修改业务定义；运行控制核心不在本次修改范围。'}
        elif op=='processing.preview': out=rt.draft_config(sid,params.get('config',{}))
        elif op=='processing.apply':
            if params.get('authorized') is not True: raise DomainError('诊断不代表修改授权；用户明确要求修改并生效时再应用。',409)
            d=rt.get_draft(params.get('draft_id'))
            if d['scenario']!=sid: raise DomainError('草稿不属于当前场景')
            out=rt.publish(d['id'])
        elif op=='report': out=rt.invoke({'scenario_id':sid,'capability':'report.generate','parameters':params.get('filters',{})})
        if request_id:
            with agent.store.db() as c: c.execute('INSERT INTO bridge_receipts VALUES (?,?,?,?)',(tid,request_id,fp,dump(out)))
        agent.event(tid,'bridge.completed',{'operation':op,'request_id':request_id,'effect':CATALOG[op]['effect']})
        return out
