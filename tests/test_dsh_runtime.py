"""DSH process contracts with an explicit fake SDK. No model call is made."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

from app.dsh import DSHAdapter
from app.engine import DomainError
from app.agent import AgentService
from app.runtime import Runtime
from app.store import Store, dump, now


FAKE_SDK = r'''
import json,os,threading
from pathlib import Path
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,urlopen
class DeepSeekHarness:
 def __init__(self,**options):self.options=options;self.cwd=Path(options['cwd'])
 def __enter__(self):
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    self.rfile.read(int(self.headers.get('Content-Length',0)));raw=b'{"ok":true}';self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=self.server.serve_forever,daemon=True).start()
  request=Request(os.environ['SUPPLY_CHAIN_DSH_BRIDGE_URL']+'/register',json.dumps({'control_port':self.server.server_port}).encode(),{'Content-Type':'application/json'})
  with urlopen(request) as response:response.read()
  return self
 def run(self,message,session_id,on_notification):
  with (self.cwd/'fake-dsh-requests.jsonl').open('a',encoding='utf-8') as output:output.write(json.dumps({'message':message,'session_id':session_id,'pid':os.getpid(),'base_url':self.options.get('base_url')})+'\n')
  on_notification(SimpleNamespace(method='session.event',payload={'sessionId':session_id,'event':{'type':'tool/call','data':{'callId':'CALL-1','name':'fixture_tool','arguments':{'safe':True}}}}))
  on_notification(SimpleNamespace(method='session.event',payload={'sessionId':session_id,'event':{'type':'tool/result','data':{'message':{'tool_call_id':'CALL-1','content':[{'type':'text','text':'fixture result'}]}}}}))
  return SimpleNamespace(final_response='DSH FIXTURE RESULT',finish_reason='completed')
 def __exit__(self,*args):self.server.shutdown();self.server.server_close()
'''


class DSHSettingsTests(unittest.TestCase):
    def test_dsh_settings_validate_and_redact_key(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "settings.sqlite3")
            public = store.update_settings(
                {
                    "agent": {
                        "mode": "dsh",
                        "dsh_model_url": "http://127.0.0.1:9000/v1",
                        "dsh_model": "fixture-model",
                        "dsh_api_key": "secret-value",
                        "dsh_context_threshold": 0.7,
                        "dsh_context_retain": 0.15,
                    }
                }
            )
            self.assertEqual(public["agent"]["dsh_api_key"], "")
            self.assertTrue(public["agent"]["has_dsh_api_key"])
            self.assertEqual(store.settings()["agent"]["dsh_api_key"], "secret-value")

    def test_invalid_dsh_context_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "settings.sqlite3")
            with self.assertRaises(DomainError):
                store.update_settings(
                    {"agent": {"dsh_context_threshold": 0.4, "dsh_context_retain": 0.5}}
                )


class DSHWorkerContractTests(unittest.TestCase):
    def test_backend_restart_marks_owned_dsh_run_interrupted_without_resend(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Path(temp) / "runtime.sqlite3", ROOT / "packs")
            task = {
                "id": "DSH-LOST-RUN",
                "scenario": "materials",
                "session_id": "SESSION-1",
                "status": "running",
                "mode": "dsh",
                "revision": 1,
                "message": "fixture",
                "activity": [],
                "outputs": [],
                "handle": {"run_id": "DSH-LOST-RUN", "session_id": "old-local-process"},
                "created_at": now(),
                "updated_at": now(),
                "_bridge_token": "fixture-token",
            }
            with runtime.store.db() as connection:
                connection.execute(
                    "INSERT INTO sessions VALUES (?,?,?)", ("SESSION-1", "materials", now())
                )
                connection.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?)",
                    (task["id"], task["scenario"], task["session_id"], dump(task), now()),
                )
            service = AgentService(runtime, poll_interval=0.01)
            try:
                recovered = service.task(task["id"])
                self.assertEqual(recovered["status"], "interrupted")
                self.assertIn("不自动重发", recovered["error"])
            finally:
                service.shutdown()

    def wait_terminal(self, adapter, handle):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            status = adapter.status(handle)
            if status["status"] in ("completed", "failed", "cancelled", "interrupted"):
                return status
            time.sleep(0.05)
        self.fail("fake DSH worker did not finish")

    def test_persistent_worker_and_normalized_tool_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            shutil.copytree(ROOT / "dsh_runtime", root / "dsh_runtime")
            fixture = Path(temp) / "fixture-sdk"
            fixture.mkdir()
            (fixture / "deepseek_harness.py").write_text(FAKE_SDK, encoding="utf-8")
            dist = fixture / "deepseek_harness_sdk-0.1.5rc1.dist-info"
            dist.mkdir()
            (dist / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: deepseek-harness-sdk\nVersion: 0.1.5rc1\n",
                encoding="utf-8",
            )
            cfg = {
                "dsh_provider": "deepseek-official",
                "dsh_model": "fixture-model",
                "dsh_model_url": "http://127.0.0.1:1/v1",
                "dsh_api_key": "fixture-only",
                "dsh_approval_mode": "ask",
                "dsh_context_threshold": 0.72,
                "dsh_context_retain": 0.16,
            }
            adapter = DSHAdapter(cfg, root=root)
            adapter.require = lambda: None
            pythonpath = str(fixture) + os.pathsep + os.environ.get("PYTHONPATH", "")
            try:
                with patch.dict(os.environ, {"PYTHONPATH": pythonpath}):
                    first = adapter.start(
                        {"id": "RUN-1", "session_id": "LOCAL-1"}, "first", history=[]
                    )
                    first_status = self.wait_terminal(adapter, first)
                    second = adapter.start(
                        {"id": "RUN-2", "session_id": "LOCAL-1"},
                        "second",
                        session_id=first["session_id"],
                        history=[{"role": "assistant", "content": first_status["output"]}],
                    )
                    second_status = self.wait_terminal(adapter, second)
                self.assertEqual(first_status["output"], "DSH FIXTURE RESULT")
                self.assertEqual(second_status["status"], "completed")
                calls = [
                    json.loads(line)
                    for line in (root / "fake-dsh-requests.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(len(calls), 2)
                self.assertEqual(len({row["pid"] for row in calls}), 1)
                self.assertEqual(len({row["session_id"] for row in calls}), 1)
                events = list(adapter.stream(first))
                kinds = {kind for kind, _data, _event_id in events}
                self.assertTrue({"tool.started", "tool.completed", "run.completed"} <= kinds)
            finally:
                adapter.close()

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional without DSH")
    def test_real_bridge_module_contract(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests" / "dsh_bridge_contract.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"ok":true', result.stdout)


if __name__ == "__main__":
    unittest.main()
