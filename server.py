#!/usr/bin/env python3
"""Supply-chain workbench. Python 3.10+, SQLite, optional local DSH SDK."""
import os
import argparse
import json
import mimetypes
import re
import threading
import traceback
import webbrowser
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote
from app.runtime import Runtime
from app.agent import AgentService
from app.engine import DomainError
from app.store import dump
from app import bridge

ROOT=Path(__file__).resolve().parent
# Optional runtime storage settings; command-line arguments still take precedence.
import os
_env_file=ROOT/'.env'
if _env_file.exists():
    for _line in _env_file.read_text(encoding='utf-8').splitlines():
        if not _line.strip() or _line.lstrip().startswith('#') or '=' not in _line:continue
        _key,_value=_line.split('=',1)
        os.environ.setdefault(_key.strip(),_value.strip().strip('"').strip("'"))


class Handler(BaseHTTPRequestHandler):
    server_version='SupplyChainAgentWorkbench/2.2.1-rag'
    def log_message(self,fmt,*args):
        if '/api/agent/' not in str(args) and '/api/events' not in str(args): super().log_message(fmt,*args)
    def send(self,data,status=200,ctype='application/json; charset=utf-8',filename=None):
        body=dump(data).encode() if ctype.startswith('application/json') else data.encode() if isinstance(data,str) else data
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        if filename: self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError): pass
    def body(self):
        try: n=int(self.headers.get('Content-Length',0))
        except ValueError: raise DomainError('Content-Length 无效')
        if n>12*1024*1024: raise DomainError('P0 单次输入上限 12 MB',413)
        if not n: return {}
        try:
            result=json.loads(self.rfile.read(n))
            if not isinstance(result,dict):raise DomainError('JSON 请求体必须是对象')
            return result
        except (ValueError,UnicodeError): raise DomainError('需要有效 JSON 请求')
    def do_GET(self): self.handle_request('GET')
    def do_POST(self): self.handle_request('POST')
    def do_PATCH(self): self.handle_request('PATCH')
    def do_PUT(self): self.handle_request('PUT')
    def do_DELETE(self): self.handle_request('DELETE')
    def handle_request(self,method):
        rt=self.server.runtime; agent=self.server.agent
        try:
            u=urlparse(self.path); path=unquote(u.path); q={k:v[0] for k,v in parse_qs(u.query).items()}
            if not path.startswith('/api/'):
                if method!='GET': raise DomainError('方法不支持',405)
                name='index.html' if path in ('/','/index.html') else 'api-explorer.html' if path=='/api-docs' else path.lstrip('/')
                file=(ROOT/'web'/name).resolve()
                if not file.is_relative_to((ROOT/'web').resolve()) or not file.is_file(): raise DomainError('资源不存在',404)
                typ=mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
                self.send(file.read_bytes(),ctype=typ+('; charset=utf-8' if typ.startswith('text/') or typ=='application/javascript' else '')); return
            data=self.body() if method in ('POST','PATCH','PUT','DELETE') else {}
            from types import SimpleNamespace
            from app.api_contract import dispatch
            ctx=SimpleNamespace(rt=rt,agent=agent,method=method,path=path,q=q,body=data,headers=self.headers,p={})
            out=dispatch(ctx)
            self.send(out)
        except DomainError as ex: self.send({'error':str(ex),'details':ex.details},ex.status)
        except Exception as ex:
            traceback.print_exc(); self.send({'error':str(ex)},500)


def create_server(host='127.0.0.1',port=8765,db=None,poll_interval=1.5):
    rt=Runtime(db or os.environ.get('WORKBENCH_DB') or ROOT/'data'/'workbench.sqlite3',ROOT/'packs')
    srv=ThreadingHTTPServer((host,port),Handler); srv.daemon_threads=True
    import hashlib
    rt.instance_id=hashlib.sha256(str(rt.store.path.resolve()).encode()).hexdigest()[:16]
    rt.base_url='http://'+('127.0.0.1' if host=='0.0.0.0' else host)+':'+str(srv.server_address[1])
    srv.runtime=rt; srv.agent=AgentService(rt,poll_interval=poll_interval)
    return srv


def main():
    p=argparse.ArgumentParser(description='业务工作台模板 · Python + SQLite')
    p.add_argument('--host',default='127.0.0.1'); p.add_argument('--port',type=int,default=8765)
    p.add_argument('--db',default=os.environ.get('WORKBENCH_DB',str(ROOT/'data'/'workbench.sqlite3'))); p.add_argument('--open',action='store_true')
    args=p.parse_args(); srv=create_server(args.host,args.port,args.db)
    print('供应链 Agent 工作台：'+srv.runtime.base_url+'\n合成业务样例 · 可选本地 DSH SDK / Hermes · Ctrl+C 结束本地服务')
    if args.open: threading.Timer(.5,lambda:webbrowser.open(srv.runtime.base_url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: srv.agent.shutdown(); srv.server_close()

if __name__=='__main__': main()
