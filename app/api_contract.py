"""The runtime's ONLY HTTP API registration surface.

Routes, OpenAPI, the offline inventory and API tests are generated from this
registry. Adding a business endpoint means adding a route here, not hiding it in
server.py. Schemas describe shape; business assertions belong in the test plan.
"""
from dataclasses import dataclass
import re
from .engine import DomainError
from . import bridge

OBJECT={'type':'object','additionalProperties':True}
ARRAY={'type':'array','items':OBJECT}
ERROR={'type':'object','required':['error'],'properties':{'error':{'type':'string'},'details':{}},'additionalProperties':True}
ROUTES=[]
@dataclass
class Route:
    method:str
    path:str
    operation_id:str
    summary:str
    effect:str
    handler:object
    required:tuple=()
    properties:dict=None
    response:dict=None
    example:dict=None
    def match(self,path):
        names=re.findall(r'{(\w+)}',self.path)
        pattern=re.sub(r'\\\{\w+\\\}',r'([^/]+)',re.escape(self.path))
        m=re.fullmatch(pattern,path)
        return dict(zip(names,m.groups())) if m else None
    def public(self):
        return {'method':self.method,'path':self.path,'operationId':self.operation_id,'summary':self.summary,
                'effect':self.effect,'required':list(self.required),'request_schema':self.body_schema(),
                'response_schema':self.response or {},'example':self.example or {},
                'live_test_default':'read-only' if self.effect=='read' else 'explicit-opt-in'}
    def body_schema(self):
        return {'type':'object','properties':self.properties or {k:{} for k in self.required},
                'required':list(self.required),'additionalProperties':True}

def route(method,path,op,summary,effect='read',required=(),properties=None,response=None,example=None):
    def register(fn):
        if any((x.method==method and x.path==path) or x.operation_id==op for x in ROUTES):raise RuntimeError('Duplicate route: '+path)
        ROUTES.append(Route(method,path,op,summary,effect,fn,tuple(required),properties,response,example));return fn
    return register

def scenario(c):
    sid=c.q.get('scenario_id') or c.body.get('scenario_id')
    if sid:
        c.rt.pack(sid);return sid
    rows=c.rt.list_scenarios()
    if len(rows)==1:return rows[0]['id']
    if not rows:raise DomainError('尚未安装业务场景',409)
    raise DomainError('存在多个场景，请传 scenario_id；不会默认 materials',400)

@route('GET','/api/health','health','运行与数据库状态',response=OBJECT)
def health(c):return {'ok':True,'version':'2.2.0-rag','runtime':'Python + SQLite + optional local DSH SDK + RAG contracts','database':c.rt.store.path.name}
@route('GET','/api/app','appInfo','应用清单与默认场景',response=OBJECT)
def app_info(c):
    rows=c.rt.list_scenarios()
    return {'product':'supply-chain-agent-community','maker_enabled':False,'runtime_version':'2.2.0-rag','bridge_base':c.rt.base_url,
            'instance_id':getattr(c.rt,'instance_id','business'),'default_scenario':rows[0]['id'] if rows else None}
@route('GET','/api/scenarios','listScenarios','列出已安装业务场景',response={'type':'array','items':OBJECT})
def scenes(c):return c.rt.list_scenarios()
@route('GET','/api/capabilities','listCapabilities','当前场景可调用能力')
def caps(c):return c.rt.capabilities(scenario(c))
@route('POST','/api/capabilities/invoke','invokeCapability','业务查询、检查、草稿或 Hermes Skill 委托','conditional',('scenario_id','capability'),example={'scenario_id':'${scenario_id}','capability':'data.query'})
def invoke(c):return c.rt.invoke(c.body)
@route('GET','/api/settings','getSettings','读取脱敏连接和自动化设置')
def settings(c):return c.rt.store.public_settings()
@route('POST','/api/settings','updateSettings','修改本实例设置','local-write')
def update_settings(c):return c.rt.store.update_settings(c.body)
@route('POST','/api/connections/test','testConnection','只读探测当前 Agent 运行时；不代表模型联调','external-read')
def connection(c):return c.agent.client().capabilities()
@route('GET','/api/agent/tasks','listAgentTasks','查看原任务，不启动执行')
def tasks(c):return c.agent.list_tasks(scenario=c.q.get('scenario_id'),session=c.q.get('session_id'))
@route('POST','/api/agent/tasks','submitAgentTask','发起真实 Agent 任务','external-write',('scenario_id','message'),example={'scenario_id':'${scenario_id}','message':'只检查连接，不发送业务消息','request_id':'api-check'})
def submit(c):return c.agent.submit(c.body)
@route('GET','/api/agent/sessions','listSessions','当前业务会话')
def sessions(c):return c.agent.sessions(scenario(c))
@route('POST','/api/agent/sessions','createSession','创建本地会话，不发送对话','local-write',('scenario_id',))
def new_session(c):return {'session_id':c.agent.session(c.body['scenario_id'])}
@route('GET','/api/agent/tasks/{task_id}','getAgentTask','查询一次执行的当前事实')
def task(c):return c.agent.task(c.p['task_id'])
@route('GET','/api/agent/tasks/{task_id}/events','agentEvents','按游标读取已记录工具事件')
def events(c):return c.agent.events(c.p['task_id'],c.q.get('after',0))
@route('POST','/api/agent/tasks/{task_id}/cancel','cancelAgentTask','只停止对应 Agent 执行，数据计算继续','external-write')
def cancel(c):return c.agent.stop(c.p['task_id'])
@route('POST','/api/agent/tasks/{task_id}/reply','replyAgentRequest','回复匹配的批准或补充信息请求','external-write',('card_key',))
def reply(c):return c.agent.respond(c.p['task_id'],c.body)
@route('POST','/api/agent/tasks/{task_id}/observe','observeAgentTask','重新核对原远端任务，不重发','external-read')
def observe(c):c.agent.observe(c.p['task_id']);return c.agent.task(c.p['task_id'])
@route('POST','/api/agent/tasks/{task_id}/continue','continueAgentTask','用户明确发起后续步骤','external-write',('confirm','message'))
def cont(c):return c.agent.continue_task(c.p['task_id'],c.body)
@route('GET','/api/agent/sessions/{session_id}/messages','sessionMessages','查询会话消息')
def messages(c):return c.agent.messages(c.p['session_id'])
@route('GET','/api/agent/sessions/{session_id}/snapshot','sessionSnapshot','会话与任务一致性快照')
def snap(c):return {'messages':c.agent.messages(c.p['session_id']),'tasks':c.agent.list_tasks(session=c.p['session_id'])}
@route('GET','/api/hermes/sessions','listRemoteSessions','列出 WebUI 远端会话','external-read')
def remote_sessions(c):
    cl=c.agent.client()
    if cl.kind!='webui':raise DomainError('仅 WebUI 提供此列表')
    return cl.sessions()
@route('POST','/api/hermes/bind','bindRemoteSession','绑定已存在远端会话','external-read',('scenario_id','remote_session_id'))
def bind(c):return c.agent.bind(c.body)
@route('POST','/api/bridge/{task_id}','invokeBridge','任务范围内的工作台桥接；见 x-bridge-operations','conditional',('operation',))
def bridge_call(c):return bridge.invoke(c.agent,c.p['task_id'],c.headers.get('X-Task-Token'),c.body)
@route('POST','/api/data/ingest','ingestData','写入业务输入并触发计算；自动任务视设置','conditional',('scenario_id','records'),properties={'scenario_id':{'type':'string'},'records':{'type':'array','items':OBJECT},'mode':{'type':'string','enum':['upsert','replace']},'event_id':{'type':'string'}})
def ingest(c):return c.rt.ingest(c.body['scenario_id'],c.body['records'],c.body.get('mode','upsert'),event_id=c.body.get('event_id'))
@route('POST','/api/integrations/feishu/{scenario_id}/batches','ingestFeishuBatch','接收定时飞书 CLI 产物清单；不调用飞书 API','conditional',('event_id','pulled_at','artifacts'),properties={'event_id':{'type':'string'},'pulled_at':{'type':'string'},'source':OBJECT,'artifacts':{'type':'array','items':OBJECT},'records':{'type':'array','items':OBJECT},'mode':{'type':'string','enum':['upsert','replace']}},example={'event_id':'feishu-job-20260920-0800','pulled_at':'2026-09-20T08:00:00+08:00','source':{'provider':'feishu-cli','job_id':'scheduled-job-1'},'artifacts':[{'artifact_id':'image-001','kind':'image','relative_path':'data/integrations/feishu/materials/image-001.png','sha256':'${sha256}'}]})
def ingest_feishu(c):return c.rt.ingest_feishu_batch(c.p['scenario_id'],c.body)
@route('GET','/api/integrations/feishu/{scenario_id}/artifacts','listFeishuArtifacts','查看已接收的定时飞书产物清单')
def feishu_artifacts(c):return c.rt.feishu_artifacts(c.p['scenario_id'],c.q.get('limit',50))
@route('GET','/api/rag/capabilities','ragCapabilities','RAG 适配器能力与联调状态')
def rag_capabilities(c):return c.rt.rag.capabilities()
@route('GET','/api/rag/collections','listRagCollections','列出 RAG Collection')
def rag_collections(c):return c.rt.rag.collections(c.q.get('scenario_id'))
@route('POST','/api/rag/collections','createRagCollection','创建 RAG Collection 配置','local-write',('scenario_id','id','name'),properties={'scenario_id':{'type':'string'},'id':{'type':'string'},'name':{'type':'string'},'description':{'type':'string'},'embedding':OBJECT,'vector_store':OBJECT,'reranker':OBJECT,'chunking':OBJECT})
def rag_create_collection(c):return c.rt.rag.create_collection(c.body)
@route('GET','/api/rag/collections/{collection_id}','getRagCollection','读取 RAG Collection')
def rag_collection(c):return c.rt.rag.collection(c.p['collection_id'])
@route('GET','/api/rag/collections/{collection_id}/documents','listRagDocuments','列出 RAG 文档元数据')
def rag_documents(c):return c.rt.rag.documents(c.p['collection_id'])
@route('POST','/api/rag/collections/{collection_id}/documents','registerRagDocument','登记 RAG 文档元数据','local-write',('external_id',),properties={'external_id':{'type':'string'},'title':{'type':'string'},'source_type':{'type':'string'},'source_ref':{'type':'string'},'version':{'type':'string'},'checksum':{'type':'string'},'metadata':OBJECT})
def rag_register_document(c):return c.rt.rag.register_document(c.p['collection_id'],c.body)
@route('GET','/api/rag/documents/{document_id}','getRagDocument','读取 RAG 文档元数据')
def rag_document(c):return c.rt.rag.document(c.p['document_id'])
@route('GET','/api/rag/documents/{document_id}/chunks','listRagChunks','列出已建立索引的 RAG Chunk；框架阶段为空')
def rag_chunks(c):return c.rt.rag.chunks(c.p['document_id'])
@route('POST','/api/rag/collections/{collection_id}/jobs','createRagJob','创建 RAG 计划任务；当前不执行采集或索引','local-write',('kind',),properties={'kind':{'type':'string','enum':['ingest','embed','index','rebuild']},'mode':{'type':'string','enum':['plan']},'source':OBJECT,'options':OBJECT})
def rag_create_job(c):return c.rt.rag.create_job(c.p['collection_id'],c.body)
@route('GET','/api/rag/collections/{collection_id}/jobs','listRagJobs','列出 RAG 计划任务')
def rag_jobs(c):return c.rt.rag.jobs(c.p['collection_id'])
@route('GET','/api/rag/jobs/{job_id}','getRagJob','读取 RAG 任务')
def rag_job(c):return c.rt.rag.job(c.p['job_id'])
@route('POST','/api/rag/collections/{collection_id}/query','queryRagCollection','执行 RAG 查询；未配置时不返回虚假召回','external-read',('query',),properties={'query':{'type':'string'},'top_k':{'type':'integer'},'filters':OBJECT,'rerank':{'type':'boolean'}})
def rag_query(c):return c.rt.rag.query(c.p['collection_id'],c.body)
@route('GET','/api/rag/queries/{query_id}','getRagQuery','读取 RAG 查询审计结果')
def rag_query_detail(c):return c.rt.rag.query_detail(c.p['query_id'])
@route('GET','/api/rag/queries/{query_id}/citations','getRagCitations','读取 RAG 查询引用证据')
def rag_citations(c):return c.rt.rag.citation(c.p['query_id'],c.q.get('chunk_id'))
@route('POST','/api/rag/documents/{document_id}/chunk-plan','previewRagChunks','预览确定性文本分块；不采集、不持久化','local-write',('text',),properties={'text':{'type':'string'},'chunk_size':{'type':'integer'},'overlap':{'type':'integer'}})
def rag_chunk_plan(c):return c.rt.rag.preview_chunking(c.p['document_id'],c.body)
@route('POST','/api/triggers/fire','fireTrigger','统一数据、按钮和对话检查入口','conditional',('scenario_id',))
def trigger(c):
    kind=c.body.get('type','manual')
    if kind not in ('manual','agent','data_ready'):raise DomainError('触发类型不正确')
    out=c.rt.run(c.body['scenario_id'],kind,c.body.get('parameters'))
    if kind=='data_ready' and c.body.get('event_id'):
        from .runtime import digest
        out['automatic_task']=c.agent.on_data_event(c.body['scenario_id'],digest({'scenario':c.body['scenario_id'],'event':c.body['event_id']}),out['id'])
    return out
@route('GET','/api/events','businessEvents','业务事件游标')
def bus_events(c):return c.rt.events(c.q.get('scenario_id'),c.q.get('after',0),c.q.get('limit',100))
@route('GET','/api/scenarios/{scenario_id}/dashboard','dashboard','指标、图表和当前业务投影')
def dashboard(c):return c.rt.dashboard(c.p['scenario_id'],c.q)
@route('GET','/api/scenarios/{scenario_id}/records','records','明细或原始记录')
def records(c):return c.rt.raw_records(c.p['scenario_id']) if c.q.get('raw')=='1' else c.rt.rows(c.p['scenario_id'],c.q)
@route('GET','/api/scenarios/{scenario_id}/config','scenarioConfig','业务定义、处理规则与样例')
def config(c):return c.rt.pack(c.p['scenario_id'])
@route('GET','/api/scenarios/{scenario_id}/cases','cases','当前问题列表')
def cases(c):return c.rt.cases(c.p['scenario_id'])
@route('POST','/api/scenarios/{scenario_id}/pull','pullSource','读取配置的数据源并计算','conditional')
def pull(c):return c.rt.pull_source(c.p['scenario_id'])
@route('POST','/api/scenarios/{scenario_id}/drafts','draftConfig','试算配置并生成差异草稿','local-write',('config',))
def draft(c):return c.rt.draft_config(c.p['scenario_id'],c.body['config'])
@route('GET','/api/scenarios/{scenario_id}/revisions','configRevisions','配置版本历史')
def revisions(c):return c.rt.revisions(c.p['scenario_id'])
@route('POST','/api/scenarios/{scenario_id}/rollback','rollbackConfig','显式回滚配置，不撤销现实动作','local-write',('confirm','version'))
def rollback(c):
    if c.body['confirm'] is not True:raise DomainError('需要明确确认')
    return c.rt.rollback(c.p['scenario_id'],c.body['version'])
@route('POST','/api/drafts/{draft_id}/publish','publishDraft','校验后应用业务配置','local-write',('confirm',))
def publish(c):
    if c.body['confirm'] is not True:raise DomainError('需要明确确认')
    return c.rt.publish(c.p['draft_id'])
@route('GET','/api/drafts/{draft_id}','getDraft','查看固定草稿')
def get_draft(c):return c.rt.get_draft(c.p['draft_id'])
@route('GET','/api/cases/{case_id}','caseDetail','问题详情、依据和反馈')
def case_detail(c):return c.rt.case_detail(c.p['case_id'])
@route('PATCH','/api/cases/{case_id}','updateCase','登记反馈或验收；不以消息发送替代关闭','local-write')
def case_update(c):return c.rt.update_case(c.p['case_id'],c.body)
@route('POST','/api/actions/{action_id}/execute','executeAction','确认后将动作交给 Hermes Skill','external-write',('confirm','draft_revision'))
def execute(c):
    if c.body['confirm'] is not True:raise DomainError('需要明确业务确认')
    return c.rt.execute_action(c.p['action_id'],c.body['draft_revision'])
@route('GET','/api/actions/{action_id}','getAction','读取草稿、执行和分项回执')
def action(c):return c.rt.action(c.p['action_id'])
@route('PATCH','/api/actions/{action_id}','editAction','修改草案，旧确认不覆盖新内容','local-write')
def edit(c):return c.rt.edit_action(c.p['action_id'],c.body)
@route('POST','/api/runs/{run_id}/replay','replayRun','只读重新计算旧快照，不重复执行动作','local-write')
def replay(c):return c.rt.replay(c.p['run_id'])
@route('GET','/api/runs/{run_id}','getRun','输入、规则、参数和结果快照')
def run(c):return c.rt.get_run(c.p['run_id'])
@route('GET','/api/schema','capabilitySchema','业务接口和桥接目录')
def schema(c):return {'business':'/api/capabilities/invoke','agent':'/api/agent/tasks','bridge':bridge.CATALOG,'openapi':'/api/openapi.json','inventory':'/api/catalog'}
@route('GET','/api/catalog','apiCatalog','全部已注册 HTTP 操作及效果分类')
def catalog(c):return {'operations':[r.public() for r in ROUTES],'bridge_operations':bridge.CATALOG,'contract_version':'1.0'}
@route('GET','/api/openapi.json','openapiSpec','与运行路由同源的 OpenAPI 3.1')
def spec(c):return openapi()

# Response shapes maintained with the same route registration; not a business proof.
_ARRAY_OPS={'listScenarios','listActions','listRuns','cases','configRevisions','listSessions','listAgentTasks','sessionMessages'}
for _r in ROUTES:
    if _r.response is None:
        _r.response=ARRAY if _r.operation_id in _ARRAY_OPS else OBJECT
    if _r.operation_id in ('records','businessEvents','agentEvents','listRemoteSessions','invokeCapability','invokeBridge'):
        _r.response={'oneOf':[OBJECT,ARRAY]}

def resolve(method,path):
    possible=[]
    for r in ROUTES:
        p=r.match(path)
        if p is not None:
            possible.append(r.method)
            if method==r.method:return r,p
    if possible:raise DomainError('方法不支持；允许 '+', '.join(possible),405)
    raise DomainError('API 路由不存在：'+path,404)

# Data workspace endpoints are real routes, not separate static documentation.
@route('GET','/api/scenarios/{scenario_id}/workspace','dataWorkspace','固定时点的目标与筛选口径')
def dw_summary(c):return c.rt.workspace.summary(c.p['scenario_id'],c.q.get('snapshot'))
@route('GET','/api/scenarios/{scenario_id}/versions','dataVersions','按时间分页列出真实数据版本')
def dw_versions(c):return c.rt.workspace.versions(c.p['scenario_id'],c.q)
@route('GET','/api/scenarios/{scenario_id}/tables','dataTables','列出该快照全部原始表、中间表、结果表')
def dw_tables(c):return c.rt.workspace.table_list(c.p['scenario_id'],c.q.get('snapshot'))
@route('GET','/api/scenarios/{scenario_id}/tables/{table_id}/rows','tableRows','固定快照与过滤，游标分页读取记录')
def dw_rows(c):return c.rt.workspace.rows(c.p['scenario_id'],c.q.get('snapshot'),c.p['table_id'],c.q)
@route('GET','/api/scenarios/{scenario_id}/tables/{table_id}/lineage/{row_key}','rowLineage','结构化形成链路与当时规则')
def dw_lineage(c):return c.rt.workspace.lineage(c.p['scenario_id'],c.q.get('snapshot'),c.p['table_id'],c.p['row_key'])
@route('GET','/api/scenarios/{scenario_id}/rules-view','rulesView','实际执行的规则、参数、处理代码及原文依据')
def dw_rules(c):return c.rt.workspace.rules(c.p['scenario_id'],c.q.get('snapshot'))
@route('POST','/api/scenarios/{scenario_id}/quick-prompt','quickPrompt','准备带当前快照和对象的 Hermes 提示词；不执行','read',('key',))
def dw_prompt(c):return c.rt.workspace.prompt(c.p['scenario_id'],c.body)
@route('POST','/api/scenarios/{scenario_id}/snapshot-notes','snapshotNote','在历史快照上追加复盘意见，不改历史数据','local-write',('snapshot','note'))
def dw_note(c):return c.rt.workspace.note(c.p['scenario_id'],c.body)
@route('POST','/api/scenarios/{scenario_id}/data-bundle','ingestDataBundle','批次接入多张来源表并运行处理','conditional',('tables',))
def dw_bundle(c):return c.rt.ingest_bundle(c.p['scenario_id'],c.body)

def dispatch(c):
    r,c.p=resolve(c.method,c.path)
    for k in r.required:
        if k not in c.body:raise DomainError('缺少参数：'+k,400)
    import os
    if os.environ.get('GENERATOR_PREVIEW')=='1' and r.effect in ('external-read','external-write'):
        raise DomainError('制作预览不调用真实 Hermes / Skills；请在独立成品中联调',409)
    if os.environ.get('GENERATOR_PREVIEW')=='1':
        if r.operation_id=='updateSettings':
            raise DomainError('预览不保存真实服务凭据',409)
        if r.operation_id=='invokeCapability' and c.body.get('capability') not in ('data.query','rules.run','report.generate'):
            raise DomainError('预览不派发真实 Hermes/Skills',409)
        if r.operation_id=='invokeBridge':raise DomainError('预览不接收远端业务执行桥接',409)
    return r.handler(c)

def openapi():
    paths={}
    for r in ROUTES:
        op={'operationId':r.operation_id,'summary':r.summary,'tags':[r.effect],
            'x-effect':r.effect,'responses':{'200':{'description':'成功；业务完成以回执和验收为准','content':{'application/json':{'schema':r.response or {}}}},
              '400':{'description':'参数或业务约束','content':{'application/json':{'schema':ERROR}}},
              '404':{'description':'对象不存在'},'409':{'description':'当前状态不允许'}}}
        names=re.findall(r'{(\w+)}',r.path)
        op['parameters']=[{'name':n,'in':'path','required':True,'schema':{'type':'string'}} for n in names]
        if r.method=='GET':
            op['parameters'] += [{'name':k,'in':'query','required':False,'schema':{'type':'string'}} for k in ('scenario_id','after','limit','session_id','raw','severity','site','owner','q','snapshot','cursor','from','to','risk','rule_id')]
        else:op['requestBody']={'required':bool(r.required),'content':{'application/json':{'schema':r.body_schema(),'example':r.example or {}}}}
        if r.operation_id=='invokeBridge':op['x-bridge-operations']=bridge.CATALOG
        paths.setdefault(r.path,{})[r.method.lower()]=op
    return {'openapi':'3.1.0','info':{'title':'Supply Chain Agent Workbench API','version':'2.2.0-rag'},'paths':paths,
            'x-note':'目录完整性不等于业务正确性。外部写入测试需明确授权。默认批量测试使用隔离库。'}
