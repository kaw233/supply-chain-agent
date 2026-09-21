#!/usr/bin/env python3
"""Persistent local DSH SDK worker used by app.dsh.DSHAdapter."""
import json
import os
import queue
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen


OUT_LOCK = threading.Lock()
PENDING = {}
CONTROL = {}
READY = threading.Event()
STOP = threading.Event()
QUIT = threading.Event()
JOBS = queue.Queue()
CURRENT = {}


def emit(kind, data):
    with OUT_LOCK:
        print(
            json.dumps(
                {"kind": kind, "data": data, "run_id": CURRENT.get("task_id")},
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )


class Sink(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
            data = json.loads(body)
            if self.path == "/register":
                CONTROL.update(data)
                READY.set()
                answer = {"ok": True}
            elif self.path == "/events":
                for event in data:
                    emit(event["kind"], event["data"])
                answer = {"ok": True}
            elif self.path == "/request":
                request_id = "REQ-" + uuid.uuid4().hex
                slot = {"event": threading.Event(), "answer": None}
                PENDING[request_id] = slot
                emit("interaction.pending", {"id": request_id, **data})
                while not STOP.is_set() and not QUIT.is_set() and not slot["event"].wait(0.2):
                    pass
                if STOP.is_set() or QUIT.is_set():
                    answer = {"decision": "deny", "answers": []}
                else:
                    answer = slot["answer"] or {"answers": []}
                PENDING.pop(request_id, None)
                emit("interaction.resolved", {"id": request_id, "stopped": STOP.is_set()})
            else:
                self.send_error(404)
                return
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_error(500, str(exc))


def control(command):
    if command == "cancel":
        STOP.set()
    if command == "shutdown":
        QUIT.set()
        STOP.set()
    if command in ("cancel", "shutdown") and CONTROL.get("control_port"):
        try:
            request = Request(
                "http://127.0.0.1:" + str(CONTROL["control_port"]) + "/cancel",
                json.dumps({"session_id": CURRENT.get("session_id")}).encode(),
                {"Content-Type": "application/json"},
            )
            urlopen(request, timeout=3).close()
        except Exception:
            pass


def input_loop():
    for line in sys.stdin:
        try:
            message = json.loads(line)
            command = message.get("command")
            if command == "run":
                JOBS.put(message["job"])
            elif command == "reply":
                slot = PENDING.get(message.get("request_id"))
                if slot:
                    slot["answer"] = message.get("answer", {})
                    slot["event"].set()
            elif command in ("cancel", "shutdown"):
                control(command)
        except Exception as exc:
            emit("control.error", {"error": str(exc)})
    QUIT.set()
    JOBS.put(None)


def quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def main():
    if os.environ.get("SUPPLY_CHAIN_DSH_RUNNER") != "1":
        raise RuntimeError("Must be launched by DSHAdapter")
    first = json.loads(sys.stdin.readline())
    first_job = first.get("job", first)
    root = Path(first_job["workspace_root"]).resolve()
    runtime = Path(first_job["runtime_root"]).resolve()
    dsh_home = Path(first_job["dsh_home"]).resolve()
    if runtime.parent != root or dsh_home != (root / "data" / "dsh" / dsh_home.name).resolve():
        raise RuntimeError("DSH runtime/home must stay inside the project")
    dsh_home.mkdir(parents=True, exist_ok=True)
    patch = dsh_home / "supply-chain-runtime.patch.yml"
    template = (runtime / "profile.patch.yml").read_text(encoding="utf-8")
    patch.write_text(
        template.replace("__WORKSPACE_ROOT__", quoted(root)).replace(
            "__BRIDGE_SCRIPT__", quoted(runtime / "bridge.mjs")
        ),
        encoding="utf-8",
    )
    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    sink.daemon_threads = True
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    os.environ.update(
        SUPPLY_CHAIN_DSH_BRIDGE_URL="http://127.0.0.1:" + str(sink.server_port),
        SUPPLY_CHAIN_CONTEXT_THRESHOLD=str(first_job["context_threshold"]),
        SUPPLY_CHAIN_CONTEXT_RETAIN=str(first_job["context_retain"]),
        SUPPLY_CHAIN_APPROVAL_MODE=first_job["approval_mode"],
        DSH_TELEMETRY_DISABLED="1",
    )
    threading.Thread(target=input_loop, daemon=True).start()
    JOBS.put(first_job)

    from deepseek_harness import DeepSeekHarness
    import importlib.metadata

    last_context = None

    def notice(notification):
        nonlocal last_context
        payload = notification.payload
        if notification.method == "session.event":
            event = payload.get("event", {})
            event_type = str(event.get("type", ""))
            if event_type in ("compaction/end", "compaction/committed"):
                last_context = None
            if not any(word in event_type.lower() for word in ("thinking", "reasoning")):
                emit("session.event", {"session_id": payload.get("sessionId"), "event": event})
        elif notification.method == "session.status":
            emit("session.status", payload)

    try:
        with DeepSeekHarness(
            provider=first_job.get("provider", "deepseek-official"),
            model=first_job["model"],
            cwd=str(root),
            runtime_cwd=str(root),
            dsh_home=str(dsh_home),
            profile="sdk",
            patches=(str(patch),),
            base_url=first_job.get("model_url") or None,
            api_key=first_job.get("api_key") or None,
            initialize_timeout_seconds=60,
            request_timeout_seconds=None,
            shutdown_timeout_seconds=4,
        ) as harness:
            if not READY.wait(15):
                raise RuntimeError("DSH stream/interaction bridge did not register")
            while not QUIT.is_set():
                try:
                    turn = JOBS.get(timeout=0.5)
                except queue.Empty:
                    continue
                if turn is None:
                    break
                STOP.clear()
                CURRENT.clear()
                CURRENT.update(task_id=turn["task_id"], session_id=turn.get("session_id", first_job["session_id"]))
                message = turn["message"]
                context = turn.get("context", "")
                if context and context != last_context:
                    message += "\n\n[本任务开始时的工作台状态]\n" + context
                    last_context = context
                if turn.get("handoff"):
                    saved = json.dumps(turn.get("history", []), ensure_ascii=False)
                    message += "\n\n[进程重启说明]\n" + turn["handoff"] + "\n[已保存会话]\n" + saved[-24000:]
                emit(
                    "agent.connecting",
                    {
                        "profile": "sdk",
                        "model": turn["model"],
                        "session_id": CURRENT["session_id"],
                        "sdk_version": importlib.metadata.version("deepseek-harness-sdk"),
                        "session_mode": "persistent-local-process",
                        "sandbox_mode": "workspace-write",
                    },
                )
                try:
                    result = harness.run(message, session_id=CURRENT["session_id"], on_notification=notice)
                    try:
                        request = Request(
                            "http://127.0.0.1:" + str(CONTROL["control_port"]) + "/flush",
                            b"{}",
                            {"Content-Type": "application/json"},
                        )
                        with urlopen(request, timeout=10) as response:
                            response.read()
                    except Exception as exc:
                        emit("bridge.warning", {"error": str(exc)})
                    emit(
                        "agent.result",
                        {
                            "text": result.final_response,
                            "finish_reason": result.finish_reason,
                            "session_id": CURRENT["session_id"],
                            "cancel_requested": STOP.is_set(),
                        },
                    )
                except Exception as exc:
                    emit("agent.error", {"error": str(exc), "trace": traceback.format_exc()})
                    emit(
                        "agent.result",
                        {
                            "text": "",
                            "finish_reason": "error",
                            "error": str(exc),
                            "session_id": CURRENT["session_id"],
                        },
                    )
                    return 2
                finally:
                    CURRENT.clear()
    except Exception as exc:
        emit("agent.error", {"error": str(exc), "trace": traceback.format_exc()})
        return 2
    finally:
        QUIT.set()
        STOP.set()
        sink.shutdown()
        sink.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

