"""Local protocol test peer, NOT Hermes and NOT a model. Never used by application.
Contracts: docs/Hermes协议说明.md. Tests verify adapters against explicit fixtures.
"""
import json,threading,time,re
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from uuid import uuid4

class Peer:
    def __init__(self,prefix='/proxy'):
        self.prefix=prefix;self.lock=threading.RLock();self.runs={};self.sessions={};self.calls=[];self.created=0;self.starts=0;self.fail_status=False;self.drop_stream=False;self.lost_submit=False
        peer=self
        class H(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*args):pass
            def out(self,d,status=200,headers=None):
                raw=(json.dumps(d,ensure_ascii=False) if not isinstance(d,str) else d).encode();self.send_response(status)
                for k,v in (headers or {}).items():self.send_header(k,v)
                self.send_header('Content-Type','text/html' if isinstance(d,str) else 'application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
                try:self.wfile.write(raw)
                except OSError:pass
            def do_GET(self):self.handle_it('GET')
            def do_POST(self):self.handle_it('POST')
            def handle_it(self,method):
                u=urlparse(self.path);path=u.path[len(peer.prefix):] if u.path.startswith(peer.prefix) else u.path;q={k:v[0] for k,v in parse_qs(u.query).items()};n=int(self.headers.get('Content-Length',0));body=json.loads(self.rfile.read(n)) if n else {}
                peer.calls.append({'method':method,'path':path,'body':body,'headers':dict(self.headers),'query':q})
                if path=='/api/auth/login':
                    return self.out({'ok':True},headers={'Set-Cookie':'hermes_session=fixture; Path=/'}) if body.get('password')=='fixture-password' else self.out({'error':'bad password'},401)
                if path=='/':return self.out('<html><script>window.config={csrfToken: "csrf-fixture"};</script></html>')
                if path.startswith('/api/'):
                    if 'hermes_session=fixture' not in self.headers.get('Cookie',''):return self.out({'error':'login required'},401)
                    if method=='POST' and self.headers.get('X-Hermes-CSRF-Token')!='csrf-fixture':return self.out({'error':'CSRF required'},403)
                if path.startswith('/v1/') and self.headers.get('Authorization')!='Bearer fixture-key':return self.out({'error':'bad key'},401)
                if path=='/v1/capabilities':return self.out({'features':{'run_submission':True,'run_status':True,'run_events_sse':True,'run_stop':True,'run_approval':True}})
                if path=='/v1/models':return self.out({'data':[{'id':'hermes-agent'}]})
                if path=='/api/sessions':return self.out({'sessions':[{'id':k,'title':v.get('title',k)} for k,v in peer.sessions.items()]})
                if path=='/api/session/new':
                    sid='s-'+uuid4().hex[:8];peer.sessions[sid]={'id':sid,'messages':[]};peer.created+=1;return self.out(peer.sessions[sid])
                if path=='/api/session':
                    s=peer.sessions.get(q.get('session_id'));return self.out(s) if s else self.out({'error':'not found'},404)
                if path=='/api/session/yolo':return self.out({'ok':True,'yolo_enabled':bool(body.get('enabled'))})
                if path in ('/api/chat/start','/v1/runs'):
                    native=path=='/v1/runs';sid=body.get('session_id') or 's-'+uuid4().hex[:8];peer.sessions.setdefault(sid,{'id':sid,'messages':[]})
                    prompt=body.get('input',body.get('message',''));rid='r-'+uuid4().hex[:8];peer.starts+=1
                    run={'id':rid,'session_id':sid,'status':'running','output':'','events':[],'approval':None,'input':None,'native':native,'prompt':prompt,'stop_calls':0}
                    peer.runs[rid]=run;peer.sessions[sid]['active_stream_id']=rid;peer.sessions[sid]['messages'].append({'role':'user','content':prompt})
                    peer.add(rid,'tool.started' if native else 'tool',{'name':'fixture_lookup','preview':'本地协议测试，不访问业务账号'})
                    threading.Thread(target=peer.perform,args=(rid,),daemon=True).start()
                    if peer.lost_submit:self.connection.shutdown(2);self.connection.close();return
                    return self.out({'run_id':rid,'session_id':sid} if native else {'stream_id':rid,'session_id':sid})
                if path=='/v1/chat/completions':return self.out({'choices':[{'message':{'content':'[协议替身] 旧接口回复'}}]})
                if path=='/api/chat/stream/status':
                    if peer.fail_status:return self.out({'error':'status temporarily unavailable'},503)
                    r=peer.runs.get(q.get('stream_id'))
                    if not r:return self.out({'active':False,'replay_available':False})
                    terminal=r['status'] in ('completed','cancelled','failed')
                    return self.out({'active':not terminal,'replay_available':True,'journal':{'session_id':r['session_id'],'run_id':r['id'],'last_seq':len(r['events']),'terminal':terminal,'terminal_state':{'completed':'done','cancelled':'cancelled','failed':'error'}.get(r['status'],r['status'])}})
                m=re.fullmatch(r'/v1/runs/([^/]+)',path)
                if m:
                    if peer.fail_status:return self.out({'error':'status temporarily unavailable'},503)
                    r=peer.runs.get(m[1]);return self.out({'run_id':r['id'],'session_id':r['session_id'],'status':r['status'],'output':r['output'],'approval':r['approval']}) if r else self.out({'error':'not found'},404)
                if path in ('/api/approval/pending','/api/clarify/pending'):
                    sid=q.get('session_id');r=next((r for r in peer.runs.values() if r['session_id']==sid and r['status'] not in ('completed','failed','cancelled')),None)
                    val=r['approval' if '/approval/' in path else 'input'] if r else None;return self.out({'pending':val,'pending_count':int(bool(val))})
                m=re.fullmatch(r'/v1/runs/([^/]+)/stop',path)
                if path=='/api/chat/cancel' or m:
                    rid=m[1] if m else q.get('stream_id');r=peer.runs.get(rid)
                    if not r:return self.out({'error':'not found'},404)
                    r['stop_calls']+=1;r['status']='stopping';r['approval']=None;r['input']=None
                    threading.Timer(.18,lambda:peer.finish(rid,'cancelled')).start()
                    return self.out({'status':'stopping','run_id':rid} if m else {'ok':True,'cancelled':True})
                m=re.fullmatch(r'/v1/runs/([^/]+)/approval',path)
                if path in ('/api/approval/respond','/api/clarify/respond') or m:
                    r=peer.runs.get(m[1]) if m else next((x for x in peer.runs.values() if x['session_id']==body.get('session_id') and x['status'] not in ('completed','cancelled','failed')),None)
                    if not r:return self.out({'error':'stale run'},409)
                    clarify='clarify' in path;pending=r['input' if clarify else 'approval'];key='clarify_id' if clarify else 'request_id' if m else 'approval_id'
                    if not pending or body.get(key)!=pending.get(key):return self.out({'error':'stale request'},409)
                    if not m and pending.get('run_id') and (body.get('run_id')!=pending['run_id'] or body.get('mirror_token')!=pending.get('_gateway_mirror_token')):return self.out({'error':'mirror identity missing'},409)
                    r['input' if clarify else 'approval']=None;r['status']='running';threading.Timer(.15,lambda:peer.finish(r['id'],'failed' if body.get('choice')=='deny' else 'completed')).start();return self.out({'ok':True,'accepted':True})
                m=re.fullmatch(r'/v1/runs/([^/]+)/events',path)
                if path=='/api/chat/stream' or m:
                    if peer.drop_stream:return self.out({'error':'event buffer missing'},404)
                    rid=m[1] if m else q.get('stream_id');r=peer.runs.get(rid)
                    if not r:return self.out({'error':'not found'},404)
                    cursor=q.get('after_event_id',self.headers.get('Last-Event-ID',''));start=int(cursor.rsplit(':',1)[1]) if ':' in cursor else 0
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();until=time.monotonic()+12
                    try:
                        while time.monotonic()<until:
                            events=list(r['events'])
                            for idx,(kind,data) in enumerate(events[start:],start+1):
                                self.wfile.write(('id: '+rid+':'+str(idx)+'\nevent: '+kind+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode());self.wfile.flush();start=idx
                            if r['status'] in ('completed','cancelled','failed'):return
                            self.wfile.write(b': heartbeat\n\n');self.wfile.flush();time.sleep(.05)
                    except OSError:pass
                    return
                return self.out({'error':'unsupported fixture endpoint '+path},404)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),H);self.server.daemon_threads=True;self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base='http://127.0.0.1:'+str(self.server.server_address[1])+prefix
    def add(self,rid,event,body):
        with self.lock:self.runs[rid]['events'].append((event,body))
    def perform(self,rid):
        time.sleep(.12);r=self.runs[rid];prompt=r['prompt'];msg=prompt.split('【用户请求】')[-1]
        if 'APPROVE' in msg or '批准测试' in msg:
            pending={'request_id':'approval-'+rid,'command':'echo protocol-fixture','description':'允许本地协议测试操作'} if r['native'] else {'approval_id':'approval-'+rid,'command':'echo protocol-fixture','description':'允许本地协议测试操作','run_id':'gateway-'+rid,'_gateway_mirror_token':'mirror-'+rid}
            r['status']='waiting_for_approval';r['approval']=pending;self.add(rid,'approval',pending);return
        if 'CLARIFY' in msg or '补充信息测试' in msg:
            r['status']='waiting_input';r['input']={'clarify_id':'clarify-'+rid,'question':'要查看哪个工厂？','options':['华东工厂','华南工厂']};self.add(rid,'clarify',r['input']);return
        if 'HOLD' in msg or '长任务测试' in msg:return
        if 'BRIDGE_FILTER' in msg or '只看高风险' in msg:
            m=re.search(r'HTTP: POST (\S+)，X-Task-Token: (\S+)，',prompt)
            if m:
                from urllib.request import Request,urlopen
                payload={'operation':'view','parameters':{'type':'set_filter','filters':{'severity':'high'}},'request_id':'fixture-view'}
                try:
                    with urlopen(Request(m[1],data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Task-Token':m[2]}),timeout=5) as x:x.read()
                except Exception as ex:r['bridge_error']=str(ex)
        self.add(rid,'message.delta' if r['native'] else 'token',({'delta':'[本地协议测试服务] 已读取业务上下文，'} if r['native'] else {'text':'[本地协议测试服务] 已读取业务上下文，'}));time.sleep(.25)
        if r['status']=='running':self.finish(rid,'completed')
    def finish(self,rid,status='completed'):
        with self.lock:
            r=self.runs[rid];r['status']=status;r['approval']=None;r['input']=None;r['output']='[本地协议测试服务，非真实 Hermes] 本轮已'+('停止' if status=='cancelled' else '结束')+'。没有发送邮件或访问飞书。'
            self.sessions[r['session_id']]['active_stream_id']=None;self.sessions[r['session_id']]['messages'].append({'role':'assistant','content':r['output']})
            self.add(rid,'tool.completed' if r['native'] else 'tool_complete',{'name':'fixture_lookup','preview':'协议结果记录','is_error':status=='failed'})
            self.add(rid,'run.'+status if r['native'] else 'done',{'output':r['output'],'session':self.sessions[r['session_id']]})
    def close(self):self.server.shutdown();self.server.server_close()
