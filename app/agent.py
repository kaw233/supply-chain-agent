"""Durable, small single-process coordinator.
Browser requests never own the worker. Restart only observes remote handles.
No subagent manager, no automatic restart of a stopped execution.
"""
import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path
from .store import uid, now, dump, load
from .engine import DomainError
from .remote import adapter, RemoteError, visible_text

TERMINAL = {'completed','cancelled','failed','interrupted'}
ACTIVE = {'queued','submitting','running','waiting_approval','waiting_input','stopping','reconciling','unknown'}
NORMALIZE = {'started':'running','waiting_for_approval':'waiting_approval','done':'completed','canceled':'cancelled','error':'failed'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, remote_event TEXT, kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task,remote_event));
CREATE INDEX IF NOT EXISTS ix_agent_events_task ON agent_events(task,seq);
CREATE TABLE IF NOT EXISTS agent_intents(intent TEXT PRIMARY KEY, task TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connection_snapshots(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_receipts(event_key TEXT PRIMARY KEY, scenario TEXT NOT NULL, task TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bridge_receipts(task TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(task,request_id));
'''

class AgentService:
    def __init__(self, runtime, poll_interval=1.5):
        self.rt=runtime; self.store=runtime.store; self.poll_interval=poll_interval
        self.closed=threading.Event(); self.lock=threading.RLock(); self.clients={}; self.observers={}; self.streams={}
        self.slots=threading.Semaphore(4)
        with self.store.db() as c: c.executescript(SCHEMA)
        self.rt.agent=self
        # The ONLY startup action is to mark never-submitted work interrupted or observe handles.
        for t in self.list_tasks(raw=True):
            if t['status'] in ACTIVE:
                if t.get('mode')=='dsh':
                    self.finish(t['id'],'interrupted',error='后端重启已结束本地 DSH 进程；外部工具是否已产生效果仍需按回执核对，不自动重发。')
                elif t.get('handle'):
                    self.patch(t['id'], connection_state='reconnecting')
                    self.observe(t['id'])
                elif t.get('submission_attempted'):
                    self.patch(t['id'], status='unknown', error='服务重启前提交结果未记录；不自动重发。')
                else:
                    self.finish(t['id'],'interrupted', error='后端重启，未提交任务已中断；需要人工继续。')

    def shutdown(self):
        # Remote runs are never stopped here. Local DSH child processes are owned by
        # this service and must be reaped; unfinished tasks become interrupted on restart.
        self.closed.set()
        for client in list(self.clients.values()):
            if getattr(client,'kind',None)=='dsh' and hasattr(client,'close'):
                client.close()

    def session(self, sid, session_id=None):
        self.rt.pack(sid)
        with self.store.db() as c:
            if session_id:
                row=c.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone()
                if not row or row['scenario']!=sid: raise DomainError('会话不存在或场景不匹配',409)
                return session_id
            lid=uid('SESSION'); c.execute('INSERT INTO sessions VALUES (?,?,?)',(lid,sid,now())); return lid

    def sessions(self, sid):
        self.rt.pack(sid)
        with self.store.db() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM sessions WHERE scenario=? ORDER BY rowid DESC',(sid,))]
        for r in rows:
            tasks=self.list_tasks(session=r['id']); r['latest_task']=tasks[0] if tasks else None
            r['automatic']=bool(tasks and tasks[0].get('origin')=='automatic')
        return rows

    def messages(self, session):
        with self.store.db() as c:
            return [{'id':r['id'],'role':r['role'],'body':load(r['body']),'created_at':r['created_at']} for r in c.execute('SELECT * FROM messages WHERE session=? ORDER BY id',(session,))]

    def add_message(self, session, role, body):
        with self.store.db() as c:
            c.execute('INSERT INTO messages(session,role,body,created_at) VALUES (?,?,?,?)',(session,role,dump(body),now()))

    def raw_task(self, tid):
        with self.store.db() as c:
            row=c.execute('SELECT body FROM tasks WHERE id=?',(tid,)).fetchone()
            if not row: raise DomainError('任务不存在',404)
            return load(row[0])

    def public(self, t):
        out=copy.deepcopy({k:v for k,v in t.items() if not k.startswith('_')})
        if out.get('card'): out['card']={k:v for k,v in out['card'].items() if k!='raw'}
        return self.redact(out,t)

    def redact(self,value,t):
        token=t.get('_bridge_token','')
        def clean(x):
            if isinstance(x,str):
                if token: x=x.replace(token,'[任务凭据]')
                return x
            if isinstance(x,list): return [clean(v) for v in x]
            if isinstance(x,dict): return {k:clean(v) for k,v in x.items() if k not in ('_gateway_mirror_token','mirror_token')}
            return x
        return clean(value)

    def task(self, tid): return self.public(self.raw_task(tid))

    def list_tasks(self, scenario=None, session=None, raw=False):
        sql='SELECT body FROM tasks'; where=[]; args=[]
        if scenario: where.append('scenario=?'); args.append(scenario)
        if session: where.append('session=?'); args.append(session)
        if where: sql+=' WHERE '+' AND '.join(where)
        sql+=' ORDER BY rowid DESC LIMIT 200'
        with self.store.db() as c: ts=[load(r[0]) for r in c.execute(sql,args)]
        return ts if raw else [self.public(t) for t in ts]

    def patch(self, tid, **fields):
        with self.store.db() as c:
            t=self.raw_task(tid)
            if t['status'] in TERMINAL:
                return t  # late stream/poll observations cannot reopen cards or change terminal phase
            if t.get('stop_requested') and fields.get('status') in ('running','waiting_approval','waiting_input'):
                fields['status']='stopping'
            t.update(fields); t['revision']=t.get('revision',0)+1; t['updated_at']=now()
            c.execute('UPDATE tasks SET body=? WHERE id=?',(dump(t),tid))
            return t

    def event(self, tid, kind, body, eid=None):
        if kind in ('heartbeat','thinking','reasoning','reasoning.delta') or str(body.get('tool_name') or body.get('name') or body.get('tool') or '')=='_thinking': return False
        # Never persist model private reasoning; tool outputs shown as bounded visible summaries.
        with self.store.db() as c:
            n=c.execute('INSERT OR IGNORE INTO agent_events(task,remote_event,kind,body,created_at) VALUES (?,?,?,?,?)',(tid,eid or None,kind,dump(body),now())).rowcount
        return bool(n)

    def events(self, tid, after=0):
        self.raw_task(tid)
        with self.store.db() as c:
            return [dict(seq=r['seq'],kind=r['kind'],body=self.redact(load(r['body']),self.raw_task(tid)),created_at=r['created_at']) for r in c.execute('SELECT * FROM agent_events WHERE task=? AND seq>? ORDER BY seq LIMIT 300',(tid,int(after)))]

    def client(self, t=None):
        if t:
            key=t['_connection']
            with self.store.db() as c: cfg=load(c.execute('SELECT body FROM connection_snapshots WHERE id=?',(key,)).fetchone()[0])
        else:
            cfg=self.store.settings()['agent']; key=hashlib.sha256(dump(cfg).encode()).hexdigest()
        with self.lock:
            if key not in self.clients: self.clients[key]=adapter(cfg)
            return self.clients[key]

    def binding(self, lid, client):
        with self.store.db() as c: r=c.execute('SELECT * FROM remote_sessions WHERE local_session=?',(lid,)).fetchone()
        return r['remote_id'] if r and r['endpoint']==client.base else None

    def save_binding(self, lid, client, rid):
        if not rid: return
        with self.store.db() as c:
            other=c.execute('SELECT local_session FROM remote_sessions WHERE endpoint=? AND remote_id=? AND local_session<>?',(client.base,rid,lid)).fetchone()
            if other: raise DomainError('远端会话已经绑定另一条本地会话，请返回对应会话。',409)
            c.execute('INSERT OR REPLACE INTO remote_sessions VALUES (?,?,?,?)',(lid,client.base,rid,now()))

    def bind(self, data):
        lid=self.session(data['scenario_id'],data.get('session_id')); cl=self.client()
        if cl.kind!='webui': raise DomainError('已有远端会话选择用于 WebUI 接入。')
        with self.store.lock:
            if any(t['status'] in ACTIVE for t in self.list_tasks(session=lid)): raise DomainError('当前会话有未结束任务，不能换绑',409)
        rid=data.get('remote_session_id') or cl.create_session(); cl.session(rid)
        self.save_binding(lid,cl,rid)
        return {'session_id':lid,'remote_session_id':rid}

    def context(self, t):
        ctx=t.get('context') or {}; d=self.rt.dashboard(t['scenario'],ctx.get('filters',{}))
        selected=[]
        for cid in ctx.get('case_ids',[])[:12]:
            case=self.rt.case_detail(cid)
            if case['scenario']!=t['scenario']: raise DomainError('选中对象不属于当前业务')
            selected.append({k:v for k,v in case.items() if k not in ('events','actions')})
        return {'scenario':d['scenario']['name'],'scenario_id':t['scenario'],'version':d['scenario']['version'],
                'data_time':(d['latest_run'] or {}).get('created_at'),'metrics':d['metrics'],'view':ctx,
                'objects':[{k:v for k,v in r.items() if k!='trace'} for r in d['rows'][:16]],'selected_cases':selected,
                'action_plan':self.rt.action(ctx['action_id']) if ctx.get('action_id') else None}

    def prompt(self, t):
        base=self.store.settings()['workspace'].get('bridge_url','').rstrip('/') or getattr(self.rt,'base_url','http://127.0.0.1:8765')
        helper=Path(__file__).resolve().parent.parent/'scripts'/'workbench_bridge.py'
        return ('你是此供应链业务工作台中的 Agent。正常中文对话，不强制 JSON。可以使用当前运行时已有工具和 skills；不要派发子代理。'
                '没有技能或来源时明确说明。工具成功不等于业务问题完成。停止后不继续派发本任务操作。\n'
                '需要控制工作台、读取更多数据或修改业务处理时，使用工作台桥接，避免直接写运行核心和数据库。'
                '同机可执行下面的脚本；跨机可用 HTTP 调用相同接口（必须从远端能访问该地址）。\n'
                f'脚本：python "{helper}" --url "{base}" --task "{t["id"]}" --token "{t["_bridge_token"]}" describe\n'
                f'HTTP: POST {base}/api/bridge/{t["id"]}，X-Task-Token: {t["_bridge_token"]}，JSON: {{"operation":"describe","parameters":{{}},"request_id":"每次不同的操作编号"}}。\n'
                'describe 返回可用操作及字段。工具事件只展示，不会再执行；改变视图使用 view 操作。'
                '业务 SQL/Python、映射或公式可通过 processing.preview/apply 试算生效；明确修改指令为本次授权，不越范围、不改松验收。'
                '普通技能不要求额外通用审批；运行时工具批准按当前 DSH/Hermes 机制。邮件/飞书由已配置技能处理，未安装不伪发送。\n'
                '【本轮冻结上下文】\n'+dump(t.get('_context_snapshot',{}))+'\n【用户请求】\n'+t['message'])

    def submit(self, data):
        sid=data.get('scenario_id');
        if not sid:
            installed=self.rt.list_scenarios()
            if len(installed)!=1:raise DomainError('请明确 scenario_id；不会默认 materials',400)
            sid=installed[0]['id']
        msg=str(data.get('message','')).strip()
        if not msg or len(msg)>16000: raise DomainError('请输入 1–16000 字符的任务')
        req=str(data.get('request_id') or uid('REQ'))
        fingerprint=hashlib.sha256(dump({'scenario':sid,'message':msg,'context':data.get('context',{}),'session':data.get('session_id')}).encode()).hexdigest()
        with self.store.lock:
            with self.store.db() as c: prev=c.execute('SELECT * FROM agent_intents WHERE intent=?',(req,)).fetchone()
            if prev:
                if prev['fingerprint']!=fingerprint: raise DomainError('同一个请求编号的内容发生变化',409)
                return self.task(prev['task'])
            cfg=self.store.settings()['agent']
            endpoint_key='dsh_model_url' if cfg['mode']=='dsh' else 'base_url' if cfg['mode'] in ('hermes','legacy') else 'webui_url'
            if not cfg.get(endpoint_key):
                raise DomainError('待连接 Agent：请在连接设置填写当前模式的真实服务地址。',409)
            if cfg['mode']=='legacy' and not cfg.get('legacy_yolo_confirmed'):
                raise DomainError('旧接口没有可靠工具批准通道。请确认远端已按你的授权启用 YOLO，或使用现代 Runs/WebUI。',409)
            lid=self.session(sid,data.get('session_id'))
            if any(t['status'] in ACTIVE for t in self.list_tasks(session=lid)):
                raise DomainError('此会话仍有执行或状态待核实任务；请先停止/核对，或新建会话。',409)
            clkey=hashlib.sha256(dump(cfg).encode()).hexdigest()
            t={'id':uid('AGENT'),'scenario':sid,'session_id':lid,'request_id':req,'status':'queued','revision':1,
               'created_at':now(),'updated_at':now(),'message':msg,'context':copy.deepcopy(data.get('context',{})),
               'mode':cfg['mode'],'origin':data.get('origin','user'),'operator':self.store.settings()['workspace']['operator'],
               'continued_from':data.get('continued_from'),'handle':None,'submission_attempted':False,
               'stop_requested':False,'connection_state':'connecting','partial':'','activity':[],'outputs':[],
               'card':None,'_connection':clkey,'_bridge_token':secrets.token_urlsafe(24)}
            t['_context_snapshot']=self.context(t)
            with self.store.db() as c:
                c.execute('INSERT OR IGNORE INTO connection_snapshots VALUES (?,?)',(clkey,dump(cfg)))
                c.execute('INSERT INTO tasks VALUES (?,?,?,?,?)',(t['id'],sid,lid,dump(t),now()))
                c.execute('INSERT INTO agent_intents VALUES (?,?,?)',(req,t['id'],fingerprint))
            aid=t['context'].get('action_id')
            if aid:
                plan=self.rt.action(aid)
                plan.update(agent_task_id=t['id'],agent_status='queued')
                if plan['status'] not in ('submitted','failed','cancelled'):
                    plan['status']='executing'
                self.rt.save_action(plan)
            self.add_message(lid,'user',{'text':msg,'task_id':t['id']})
            self.event(t['id'],'task.created',{'origin':t['origin']})
        threading.Thread(target=self.start_work,args=(t['id'],),daemon=True).start()
        return self.task(t['id'])

    def start_work(self, tid):
        with self.slots:
            if self.closed.is_set(): return
            with self.store.lock:
                t=self.raw_task(tid)
                if t['status']!='queued': return
                if t.get('stop_requested'): self.finish(tid,'cancelled'); return
                self.patch(tid,status='submitting',submission_attempted=True,started_at=now())
            try:
                cl=self.client(t); prompt=self.prompt(t)
                if cl.kind=='legacy':
                    self.patch(tid,status='running',phase='旧 API 正在请求；此路径不保证远端停止/恢复。')
                    history=[]
                    for m in self.messages(t['session_id'])[:-1][-12:]:
                        history.append({'role':m['role'],'content':str(m['body'].get('text',''))[-4000:]})
                    output=cl.chat(prompt,history)
                    if self.raw_task(tid).get('stop_requested'):
                        self.finish(tid,'completed',output,error='停止请求未有原生取消协议；已取回本轮回复，不派发额外操作。')
                    else: self.finish(tid,'completed',output)
                    return
                if cl.kind=='dsh':
                    history=[]
                    history_limit=int(getattr(cl,'cfg',{}).get('history_messages',12))
                    for message in self.messages(t['session_id'])[:-1][-history_limit:]:
                        history.append({'role':message['role'],'content':str(message['body'].get('text',''))[-4000:]})
                    handle=cl.start(t,prompt,self.binding(t['session_id'],cl),history=history)
                else:
                    handle=cl.start(t,prompt,self.binding(t['session_id'],cl))
                if self.closed.is_set():
                    # Still preserve the accepted handle for the next backend to observe.
                    self.patch(tid,handle=handle,status='reconciling'); return
                self.save_binding(t['session_id'],cl,handle.get('session_id'))
                current=self.patch(tid,handle=handle,remote_session_id=handle.get('session_id'),remote_run_id=handle['run_id'],status='running',connection_state='connected',phase=('本地 DSH SDK 正在执行' if cl.kind=='dsh' else 'Hermes 正在执行'))
                self.event(tid,'remote.accepted',handle)
                if current.get('stop_requested'): self.stop(tid)
                self.observe(tid)
            except Exception as ex:
                if self.closed.is_set(): return
                # Transport failures after POST may already have side effects. Never re-POST.
                rejected=(isinstance(ex,RemoteError) and ex.remote_code in (400,401,403,404,409,422)) or (getattr(cl,'kind',None)=='dsh' and not t.get('handle'))
                if rejected: self.finish(tid,'failed',error=str(ex))
                else: self.patch(tid,status='unknown',connection_state='disconnected',error=str(ex),phase='提交结果待核实；不会自动重发')

    def observe(self, tid):
        with self.lock:
            if tid in self.observers and self.observers[tid].is_alive(): return
            th=threading.Thread(target=self.observe_loop,args=(tid,),daemon=True)
            self.observers[tid]=th; th.start()

    def observe_loop(self, tid):
        while not self.closed.is_set():
            t=self.raw_task(tid)
            if t['status'] in TERMINAL or not t.get('handle'): return
            try:
                self.reconcile(tid)
                t=self.raw_task(tid)
                if t['status'] in TERMINAL: return
                # Stream is supplementary; missing native buffer does not prevent status polling.
                with self.lock:
                    th=self.streams.get(tid)
                    if (not th or not th.is_alive()) and time.monotonic()>=t.get('_next_stream',0):
                        th=threading.Thread(target=self.read_stream,args=(tid,),daemon=True); self.streams[tid]=th; th.start()
            except Exception as ex:
                missing=isinstance(ex,RemoteError) and ex.remote_code in (404,410)
                fields={'status':'unknown'} if missing else {}
                self.patch(tid,connection_state='disconnected',phase='原任务记录不可查询，结果待核实；不重新提交' if missing else '远端连接中断；保留最后确认状态，正在核对',error=str(ex),**fields)
            self.closed.wait(self.poll_interval)

    @staticmethod
    def make_card(kind, raw):
        if not isinstance(raw,dict): return None
        rid=raw.get('request_id') if kind=='approval' and raw.get('request_id') else raw.get('approval_id') if kind=='approval' else raw.get('clarify_id')
        # Identity binds content as well, to prevent same-id mutation from confirming a new plan.
        identity={k:raw.get(k) for k in ('request_id','approval_id','clarify_id','run_id','mirror_token','_gateway_mirror_token','command','question','description','choices','options','prompt')}
        key=hashlib.sha256(dump({'kind':kind,'request':identity}).encode()).hexdigest()[:24]
        return {'kind':kind,'key':key,'request_id':str(rid or ''),'actionable':bool(rid),
                'title':raw.get('description') or raw.get('question') or ('Agent 工具批准' if kind=='approval' else '需要补充信息'),
                'detail':raw.get('command') or raw.get('prompt') or raw.get('question') or '',
                'choices':raw.get('choices',raw.get('options',[])), 'raw':raw}

    def reconcile(self, tid):
        t=self.raw_task(tid)
        if t['status'] in TERMINAL or not t.get('handle'): return self.task(tid)
        cl=self.client(t); s=cl.status(t['handle']); state=NORMALIZE.get(s['status'],s['status'])
        if s.get('session_id'):
            self.save_binding(t['session_id'],cl,s['session_id'])
            h={**t['handle'],'session_id':s['session_id']}; self.patch(tid,handle=h,remote_session_id=s['session_id'])
        if state in TERMINAL:
            output=s.get('output','') or t.get('partial','')
            if not output and cl.kind=='webui':
                try:
                    ss=cl.session(t['handle']['session_id']); msgs=ss.get('messages',[])
                    # Only use a final assistant from messages added by this run.
                    old=t['handle'].get('message_count',len(msgs))
                    for m in reversed(msgs[old:]):
                        if m.get('role')=='assistant' and not m.get('tool_calls'):
                            output=visible_text(m.get('content',m.get('text',''))); break
                except Exception: pass
            self.finish(tid,state,output,error=str(s['raw'].get('error','')))
        else:
            card=self.make_card('approval',s.get('approval')) or self.make_card('input',s.get('input'))
            if t.get('stop_requested'): card=None
            label='等待工具批准' if card and card['kind']=='approval' else '等待补充信息' if card else ('本地 DSH SDK 正在执行' if cl.kind=='dsh' else 'Hermes 正在执行')
            if state=='unknown': label='远端状态无法确认；不会重新执行'
            if t.get('stop_requested'): label='已请求停止，等待远端确认；数据处理仍然运行'
            self.patch(tid,status=state if state in ACTIVE else 'unknown',connection_state='connected',last_confirmed_at=now(),card=card,phase=label,error='')
        return self.task(tid)

    def read_stream(self, tid):
        t=self.raw_task(tid); cl=self.client(t)
        try:
            for kind,data,eid in cl.stream(t['handle'],t.get('cursor','')):
                if self.closed.is_set(): break
                current=self.raw_task(tid)
                if current['status'] in TERMINAL: break
                if kind=='heartbeat': continue
                if kind.startswith(('reasoning','thinking')) or kind=='_thinking' or (data.get('tool_name') or data.get('name') or data.get('tool'))=='_thinking': continue
                if eid and not self.event(tid,kind,data,eid): continue
                if eid: self.patch(tid,cursor=eid)
                elif kind not in ('token','message.delta','assistant.delta'): self.event(tid,kind,data)
                if kind in ('token','message.delta','assistant.delta'):
                    part=data.get('text',data.get('delta',''))
                    if isinstance(part,str): self.patch(tid,partial=(current.get('partial','')+part)[-160000:])
                elif kind in ('tool','tool_complete','tool.started','tool.completed','tool.start','tool.complete','tool.failed'):
                    self.record_tool(tid,kind,data)
                elif kind in ('approval','approval.request'):
                    card=self.make_card('approval',data.get('pending',data))
                    if card and not current.get('stop_requested'): self.patch(tid,card=card,status='waiting_approval')
                elif kind in ('clarify','clarify.request'):
                    card=self.make_card('input',data.get('pending',data))
                    if card and not current.get('stop_requested'): self.patch(tid,card=card,status='waiting_input')
                elif kind in ('done','run.completed','run.cancelled','run.failed','run.interrupted','cancelled','error'):
                    output=visible_text(data.get('output') or data.get('text') or data.get('response') or '')
                    if kind=='done':
                        for m in reversed((data.get('session') or {}).get('messages',[])):
                            if m.get('role')=='user': break
                            if m.get('role')=='assistant': output=visible_text(m.get('content',m.get('text',''))) or output; break
                    if output: self.patch(tid,partial=output)
                    # Never equate a cancellation ACK to the executor's confirmed terminal state.
                    self.reconcile(tid)
                    break
        except Exception as ex:
            if not self.closed.is_set(): self.event(tid,'stream.gap',{'message':str(ex)[:400],'effect':'仅恢复观察，不提交任务'})
        finally:
            if not self.closed.is_set(): self.patch(tid,_next_stream=time.monotonic()+5)

    def record_tool(self, tid, kind, data):
        with self.store.lock:
            t=self.raw_task(tid)
            if t['status'] in TERMINAL: return
            name=str(data.get('name') or data.get('tool_name') or data.get('tool') or '工具')
            if name=='_thinking': return
            key=str(data.get('tid') or data.get('tool_call_id') or data.get('call_id') or data.get('id') or '')
            completed=kind in ('tool_complete','tool.completed','tool.complete','tool.failed')
            status='failed' if kind=='tool.failed' or data.get('is_error') or data.get('error') else 'completed' if completed else 'running'
            activity=copy.deepcopy(t.get('activity',[]))
            match=next((x for x in reversed(activity) if (key and x.get('call_id')==key) or (not key and x['name']==name and x['status']=='running')),None)
            item={'name':name,'call_id':key,'status':status,'preview':str(data.get('preview') or data.get('result') or '')[:1200],'at':now()}
            if completed and match:
                match.update(status=status,completed_at=now(),duration=data.get('duration'))
                if item['preview']: match['preview']=item['preview']
            elif not match or not completed: activity.append(item)
            self.patch(tid,activity=activity[-100:])

    def finish(self, tid, status, output='', error=''):
        with self.store.lock:
            t=self.raw_task(tid)
            if t['status'] in TERMINAL: return self.public(t)
            result={'reply':visible_text(output) or ('本轮已停止；已发生的外部操作不会自动撤销。' if status=='cancelled' else error or '本轮已结束，完整回复不可用，请核对原端记录。'),
                    'outputs':t.get('outputs',[]),'mode':t['mode']}
            activity=copy.deepcopy(t.get('activity',[]))
            for tool in activity:
                if tool['status']=='running':
                    tool.update(status='interrupted' if status=='cancelled' else 'unknown',note='本轮已结束，未取得该工具独立的完成回执')
            t=self.patch(tid,status=status,result=result,activity=activity,card=None,completed_at=now(),last_confirmed_at=now(),connection_state='connected',error=error,phase='本轮已结束')
            self.add_message(t['session_id'],'assistant',{'text':result['reply'],**result,'task_id':tid})
            self.event(tid,'task.'+status,{'status':status,'error':error})
            if hasattr(self.rt,'agent_action_finished'): self.rt.agent_action_finished(t)
            return self.public(t)

    def stop(self, tid):
        with self.store.lock:
            t=self.raw_task(tid)
            if t['status'] in TERMINAL: return self.public(t)
            self.patch(tid,stop_requested=True,card=None,status='stopping',phase='正在停止当前 Agent 执行；不改变自动任务开关')
            self.event(tid,'stop.requested',{'operator':self.store.settings()['workspace']['operator'],'automation_unchanged':True})
            if not t.get('submission_attempted'): return self.finish(tid,'cancelled')
        if not t.get('handle'):
            if t['mode']=='legacy':
                self.patch(tid,status='unknown',phase='旧接口不能核实远端停止；已禁止本任务后续工作台操作',error='请在原端停止并核对。')
            return self.task(tid)
        try:
            out=self.client(t).stop(t['handle'])
            self.patch(tid,stop_ack=out,phase='停止请求已受理，等待远端终态')
            self.reconcile(tid)
        except Exception as ex:
            self.patch(tid,error=str(ex),connection_state='disconnected',phase='停止尚未核实，请检查远端；不自动派发后续操作')
        return self.task(tid)

    def respond(self, tid, data):
        self.reconcile(tid)  # fetch current remote request; not a cached UI approval
        with self.store.lock:
            t=self.raw_task(tid); card=t.get('card')
            if t['status'] in TERMINAL or t.get('stop_requested') or not card or card['key']!=data.get('card_key'):
                raise DomainError('这张卡片已失效、被处理或属于旧请求。已刷新状态；没有批准新的操作。',409)
            if not card['actionable']: raise DomainError('请求缺少稳定标识，请在原端处理。',409)
            choice=data.get('choice','once')
            if choice not in ('once','deny'): raise DomainError('此模板仅提交本次允许或拒绝')
            answer=str(data.get('answer','')).strip()
            if card['kind']=='input' and not answer: raise DomainError('请输入补充信息')
        result=self.client(t).reply(t['handle'],card,choice,answer)
        self.event(tid,'card.responded',{'kind':card['kind'],'request_id':card['request_id'],'choice':choice,'answer':answer,'operator':self.store.settings()['workspace']['operator'],'remote_result':result})
        self.reconcile(tid)
        return {'accepted':True,'task':self.task(tid)}

    def continue_task(self, tid, data):
        if data.get('confirm') is not True: raise DomainError('继续执行需要你明确确认。')
        t=self.raw_task(tid)
        if t['status'] not in TERMINAL:
            if t.get('handle'): self.reconcile(tid); t=self.raw_task(tid)
            if t['status'] not in TERMINAL: raise DomainError('原任务是否仍在执行尚不确定，请先在原端核对；不能重复派发。',409)
        goal=str(data.get('message','')).strip()
        if not goal: raise DomainError('请说明接下来要做的未完成步骤；不会自动重放原始指令。')
        msg=f'用户手动继续任务 {tid}，旧任务终态 {t["status"]}。只执行下面明确的新步骤，不重复已完成的外部操作。提交成功的分项绝不重发；unknown 分项先查询核对，不因这次继续而再次发送。\n已保存结果：{(t.get("result") or {}).get("reply","")[:3000]}\n后续目标：{goal}'
        return self.submit({'scenario_id':t['scenario'],'session_id':t['session_id'],'message':msg,'context':t['context'],'continued_from':tid,'request_id':data.get('request_id') or uid('CONT')})

    def on_data_event(self, sid, event_key, run_id):
        # Called only for new live ingestion; never scan historical events on startup.
        cfg=self.store.settings()['automation']
        with self.store.db() as c:
            if not c.execute('INSERT OR IGNORE INTO trigger_receipts VALUES (?,?,NULL,?)',(event_key,sid,now())).rowcount: return None
        if not cfg.get('enabled'): return None
        if cfg.get('scenario') not in ('all',sid): return None
        try:
            t=self.submit({'scenario_id':sid,'message':cfg['message'],'origin':'automatic','context':{'page':'overview','data_run_id':run_id},'request_id':'AUTO-'+event_key})
            with self.store.db() as c: c.execute('UPDATE trigger_receipts SET task=? WHERE event_key=?',(t['id'],event_key))
            return t
        except Exception as ex:
            with self.store.db() as c: self.store.event(c,sid,'automation.not_dispatched',event_key,{'error':str(ex),'auto_retry':False})
            return {'error':str(ex),'status':'not_dispatched'}
