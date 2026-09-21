"""Thin HTTP adapters. Execution ownership stays remote; no tool replay here.
Protocol references and unsupported legacy guarantees: docs/Hermes协议说明.md.
"""
import json
import re
import threading
from http.cookiejar import CookieJar
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.parse import urlsplit, urlencode, quote
from urllib.error import HTTPError, URLError
from .engine import DomainError

class RemoteError(DomainError):
    def __init__(self, message, code=502):
        super().__init__(message, 502)
        self.remote_code = code


def visible_text(value):
    if isinstance(value, list):
        value = '\n'.join(visible_text(v.get('text', v.get('content', ''))) for v in value if isinstance(v, dict) and v.get('type', 'text') not in ('reasoning', 'thinking'))
    if not isinstance(value, str): return ''
    return re.sub(r'<think(?:ing)?>[\s\S]*?(?:</think(?:ing)?>|$)', '', value, flags=re.I).strip()


def event_stream(response):
    """SSE preserves ids and multiline payloads; comments are heartbeat observations."""
    name, parts, eid = 'message', [], ''
    for raw in response:
        line = raw.decode('utf-8', 'replace').rstrip('\r\n')
        if not line:
            if parts:
                joined = '\n'.join(parts)
                if joined == '[DONE]': yield 'transport.done', {}, eid
                else:
                    try: data = json.loads(joined)
                    except ValueError: data = {'text': joined}
                    if not isinstance(data, dict): data = {'data': data}
                    yield data.get('event', name), data, eid
            name, parts, eid = 'message', [], ''
        elif line.startswith(':'): yield 'heartbeat', {}, ''
        elif line.startswith('event:'): name = line[6:].strip()
        elif line.startswith('data:'): parts.append(line[5:].lstrip(' '))
        elif line.startswith('id:'): eid = line[3:].strip()


class HTTP:
    def __init__(self, cfg, base):
        self.cfg = dict(cfg); self.base = str(base or '').strip().rstrip('/')
        u = urlsplit(self.base)
        # Any HTTP(S) host is allowed, including public endpoints and proxy prefixes.
        if u.scheme not in ('http', 'https') or not u.netloc or u.query or u.fragment:
            raise DomainError('请填写完整 http:// 或 https:// 服务地址；可以是本机、内网或公网域名。')
        self.timeout = max(2, min(120, float(cfg.get('timeout', 15))))
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.headers = {'User-Agent': 'BusinessWorkbenchTemplate/1.2'}

    def open(self, path, body=None, headers=None, stream=False):
        h = {**self.headers, 'Accept': 'text/event-stream' if stream else 'application/json', **(headers or {})}
        data = None
        if body is not None:
            h['Content-Type'] = 'application/json'; data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        try:
            return self.opener.open(Request(self.base + path, data=data, headers=h), timeout=max(35, self.timeout) if stream else self.timeout)
        except HTTPError as ex:
            raw = ex.read(4096).decode('utf-8', 'replace')
            try: detail = json.loads(raw).get('error', raw)
            except ValueError: detail = raw[:500] or str(ex.reason)
            if isinstance(detail, dict): detail = detail.get('message', str(detail))
            raise RemoteError('远端 HTTP %s：%s' % (ex.code, str(detail)[:700]), ex.code) from ex
        except (URLError, TimeoutError, OSError) as ex:
            raise RemoteError('连接或读取失败：%s。不会自动重新提交执行。' % ex) from ex

    def request(self, path, body=None, headers=None):
        with self.open(path, body, headers) as r: raw = r.read(12 * 1024 * 1024)
        try: data = json.loads(raw)
        except ValueError as ex: raise RemoteError('端点没有返回 JSON，可能到达登录页或地址前缀不正确。') from ex
        if isinstance(data, dict) and (data.get('ok') is False or data.get('error')):
            raise RemoteError('远端拒绝请求：' + str(data.get('error') or data)[:700], 409)
        return data


class NativeAPI(HTTP):
    kind = 'hermes'
    def __init__(self, cfg):
        base = str(cfg.get('base_url', '')).rstrip('/')
        if not base.endswith('/v1'): base += '/v1'
        super().__init__(cfg, base)
        if cfg.get('api_key'): self.headers['Authorization'] = 'Bearer ' + cfg['api_key']

    def capabilities(self):
        raw = self.request('/capabilities')
        f = raw.get('features', raw.get('capabilities', raw))
        return {'kind': self.kind, 'features': f, 'raw': raw,
                'note': '认证/协议读取通过不等于实际模型与工具联调通过。'}

    def start(self, task, prompt, session_id=None):
        body = {'input': prompt}
        if self.cfg.get('model') and self.cfg['model']!='hermes-agent': body['model']=self.cfg['model']
        if session_id: body['session_id'] = session_id
        data = self.request('/runs', body, {'Idempotency-Key': task['request_id']})
        rid = data.get('run_id')
        if not rid: raise RemoteError('远端已应答但没有 run_id，提交结果需核实。')
        return {'run_id': rid, 'session_id': data.get('session_id') or session_id or ''}

    def status(self, handle):
        d = self.request('/runs/' + quote(handle['run_id'], safe=''))
        return {'status': d.get('status', 'unknown'), 'output': visible_text(d.get('output', '')),
                'approval': d.get('approval'), 'input': None, 'session_id': d.get('session_id'), 'raw': d}

    def stream(self, handle, cursor=''):
        with self.open('/runs/' + quote(handle['run_id'], safe='') + '/events', stream=True) as r:
            yield from event_stream(r)

    def stop(self, handle):
        return self.request('/runs/' + quote(handle['run_id'], safe='') + '/stop', {})

    def reply(self, handle, card, choice, answer=''):
        if card['kind'] != 'approval': raise DomainError('此原生 Runs 协议未声明独立澄清接口；普通问题请直接在对话中回复。')
        rid = card['raw'].get('request_id')
        if not rid: raise DomainError('远端批准缺少 request_id，不能猜测批准对象。', 409)
        return self.request('/runs/' + quote(handle['run_id'], safe='') + '/approval', {'choice': choice, 'request_id': rid})


class WebUI(HTTP):
    kind = 'webui'
    def __init__(self, cfg):
        super().__init__(cfg, cfg.get('webui_url'))
        self.ready = False; self.auth_lock = threading.Lock()

    def authenticate(self):
        with self.auth_lock:
            if self.ready: return
            if self.cfg.get('webui_password'):
                HTTP.request(self, '/api/auth/login', {'password': self.cfg['webui_password']})
            with self.open('/') as r: html = r.read(2 * 1024 * 1024).decode('utf-8', 'replace')
            m = re.search(r'(?:[\"\']?csrfToken[\"\']?)\s*:\s*("(?:[^"\\]|\\.)*"|null)', html)
            if m:
                token = json.loads(m.group(1))
                if token: self.headers['X-Hermes-CSRF-Token'] = token
            self.ready = True

    def request(self, path, body=None, headers=None):
        self.authenticate()
        try: return HTTP.request(self, path, body, headers)
        except RemoteError as ex:
            if ex.remote_code in (401,403): self.ready = False
            # No automatic retry of POST even after re-authentication.
            raise

    def capabilities(self):
        self.request('/api/sessions')
        features = {'chat': True, 'password_login': True, 'status': False, 'approval': False, 'clarify': False}
        for key, path in [('status','/api/chat/stream/status?stream_id=__capability_probe__'), ('approval','/api/approval/pending?session_id=__capability_probe__'), ('clarify','/api/clarify/pending?session_id=__capability_probe__')]:
            try: self.request(path); features[key] = True
            except RemoteError: pass
        return {'kind': self.kind, 'features': features, 'note': '只读协议探测，不发起模型任务，不更改远端。停止能力需要真实任务另行验证。'}

    def sessions(self):
        d = self.request('/api/sessions')
        return d.get('sessions', d.get('items', [])) if isinstance(d, dict) else d

    def session(self, sid):
        d = self.request('/api/session?' + urlencode({'session_id': sid}))
        return d.get('session', d)

    def create_session(self):
        body = {'worktree': False}
        for k in ('workspace','profile','model'):
            v = self.cfg.get('webui_' + k)
            if v: body[k] = v
        d = self.request('/api/session/new', body); d = d.get('session', d)
        sid = d.get('session_id') or d.get('id')
        if not sid: raise RemoteError('WebUI 新建会话没有返回 session_id。')
        return sid

    def start(self, task, prompt, session_id=None):
        sid = session_id or self.create_session()
        existing = self.session(sid)
        if existing.get('active_stream_id'):
            raise RemoteError('这个 WebUI 会话已经有活动执行。请等待它结束或另建会话。', 409)
        if self.cfg.get('webui_yolo'):
            enabled = self.request('/api/session/yolo', {'session_id': sid, 'enabled': True})
            if enabled.get('yolo_enabled') is not True:
                raise RemoteError('旧路径请求 YOLO，但远端没有确认已启用；未提交任务。', 409)
        d = self.request('/api/chat/start', {'session_id': sid, 'message': prompt})
        if not d.get('stream_id'): raise RemoteError('WebUI 已应答但缺少 stream_id，不能自动重发。')
        return {'run_id': d['stream_id'], 'stream_id': d['stream_id'], 'session_id': sid, 'message_count': len(existing.get('messages',[]))}

    def pending(self, handle, kind):
        path = '/api/approval/pending' if kind == 'approval' else '/api/clarify/pending'
        try: return self.request(path + '?' + urlencode({'session_id': handle['session_id']})).get('pending')
        except RemoteError as ex:
            if ex.remote_code == 404: return None
            raise

    def status(self, handle):
        d = self.request('/api/chat/stream/status?' + urlencode({'stream_id': handle['run_id']}))
        j = d.get('journal') or {}
        state = str(j.get('terminal_state') or '').lower()
        if d.get('active'): state = 'running'
        elif j.get('terminal'):
            state = {'done':'completed','error':'failed','stopped':'cancelled','canceled':'cancelled','run.completed':'completed','run.failed':'failed','run.cancelled':'cancelled','run.interrupted':'interrupted'}.get(state, state)
            if state not in ('completed','failed','cancelled','interrupted'): state = 'unknown'
        else: state = 'unknown'
        ap = self.pending(handle, 'approval') if state == 'running' else None
        ip = self.pending(handle, 'input') if state == 'running' else None
        if ap: state = 'waiting_for_approval'
        elif ip: state = 'waiting_input'
        return {'status': state, 'approval': ap, 'input': ip, 'output': '', 'raw': d, 'session_id': handle['session_id']}

    def stream(self, handle, cursor=''):
        self.authenticate()
        q = {'stream_id':handle['run_id'], 'replay':'1'}
        if cursor: q['after_event_id'] = cursor
        with self.open('/api/chat/stream?' + urlencode(q), headers={'Last-Event-ID':cursor} if cursor else None, stream=True) as r:
            yield from event_stream(r)

    def stop(self, handle):
        return self.request('/api/chat/cancel?' + urlencode({'stream_id':handle['run_id']}))

    def reply(self, handle, card, choice, answer=''):
        raw = card['raw']; body = {'session_id':handle['session_id']}
        if card['kind'] == 'input':
            if not raw.get('clarify_id'): raise DomainError('澄清请求缺少 clarify_id，不能回复未知请求。',409)
            body.update(clarify_id=raw['clarify_id'], response=answer)
            return self.request('/api/clarify/respond',body)
        if not raw.get('approval_id'): raise DomainError('批准请求缺少 approval_id，不能猜测批准对象。',409)
        body.update(choice=choice,approval_id=raw['approval_id'])
        if raw.get('run_id'): body['run_id'] = raw['run_id']
        token = raw.get('mirror_token') or raw.get('_gateway_mirror_token')
        if token: body['mirror_token'] = token
        if body.get('run_id') and not body.get('mirror_token'):
            raise DomainError('Gateway 批准镜像缺少 mirror_token；请在原 WebUI 处理或升级兼容版本。',409)
        return self.request('/api/approval/respond',body)


class LegacyAPI(NativeAPI):
    """Explicit compatibility only. No invented stop/run-state protocol."""
    kind = 'legacy'
    def capabilities(self):
        self.request('/models')
        return {'kind':'legacy','features':{'chat':True,'status':False,'stop':False,'approval':False},
                'note':'旧 Chat Completions：需远端按用户授权配置 YOLO；该接口不提供可核实的停止和恢复。推荐改用 Runs 或 WebUI。'}
    def chat(self, prompt, history):
        d = self.request('/chat/completions',{'model':self.cfg.get('model','hermes-agent'),'messages':history+[{'role':'user','content':prompt}],'stream':False})
        return visible_text(d['choices'][0]['message']['content'])


def adapter(cfg):
    mode=cfg.get('mode','webui')
    if mode=='dsh':
        from .dsh import DSHAdapter
        return DSHAdapter(cfg)
    return {'hermes':NativeAPI,'webui':WebUI,'legacy':LegacyAPI}[mode](cfg)
