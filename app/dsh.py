"""Local DeepSeek Harness SDK adapter.

DSH owns the model/tool loop in a child process.  This module only translates
its durable worker protocol into the same task contract used by AgentService.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from .engine import DomainError
from .remote import RemoteError


TERMINAL = {"completed", "cancelled", "failed", "interrupted"}


def _visible(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_visible(item) for item in value)
    if not isinstance(value, dict):
        return ""
    if any(word in str(value.get("type", "")).lower() for word in ("reasoning", "thinking")):
        return ""
    if isinstance(value.get("text"), str):
        return value["text"]
    return "".join(_visible(value.get(key)) for key in ("content", "message", "result"))


def _terminate(proc, grace=3):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            proc.terminate()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


class _Worker:
    def __init__(self, root, job):
        self.session_id = "dsh-" + uuid.uuid4().hex
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.stderr = deque(maxlen=120)
        env = dict(os.environ)
        # Model routing is explicit per workbench connection snapshot. Do not let
        # ambient DSH/model credentials silently change a recorded task.
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DSH_API_KEY", "DSH_HOME"):
            env.pop(key, None)
        env.update(
            SUPPLY_CHAIN_DSH_RUNNER="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUNBUFFERED="1",
        )
        script = Path(root) / "dsh_runtime" / "worker.py"
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            cwd=str(root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for pipe, kind in ((self.proc.stdout, "stdout"), (self.proc.stderr, "stderr")):
            threading.Thread(target=self._read, args=(pipe, kind), daemon=True).start()
        job = {**job, "session_id": self.session_id}
        self.send({"command": "run", "job": job})

    def _read(self, pipe, kind):
        for line in pipe:
            if kind == "stderr":
                self.stderr.append(line.rstrip())
            self.queue.put((kind, line))
        self.queue.put((kind, None))

    def send(self, body):
        with self.lock:
            if self.proc.poll() is not None or not self.proc.stdin:
                raise RemoteError("本地 DSH 进程已经退出；不会自动重发任务。", 410)
            self.proc.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def close(self):
        if self.proc.poll() is None:
            try:
                self.send({"command": "shutdown"})
                self.proc.wait(timeout=4)
            except Exception:
                _terminate(self.proc)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass


class DSHAdapter:
    """Persistent local SDK processes, one DSH session per workbench session."""

    kind = "dsh"

    def __init__(self, cfg, root=None):
        self.cfg = dict(cfg)
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        identity = {
            "provider": self.cfg.get("dsh_provider"),
            "model": self.cfg.get("dsh_model"),
            "url": self.cfg.get("dsh_model_url"),
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
        self.base = "dsh://local-sdk/" + digest
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.workers = {}
        self.runs = {}
        self.closed = False

    def diagnostics(self):
        try:
            sdk_version = importlib.metadata.version("deepseek-harness-sdk")
        except importlib.metadata.PackageNotFoundError:
            sdk_version = None
        runtime = None
        if sdk_version:
            try:
                from deepseek_harness_runtime import resolve_bundled_launch_args

                runtime = list(resolve_bundled_launch_args())
            except Exception:
                runtime = None
        files = [
            self.root / "dsh_runtime" / "worker.py",
            self.root / "dsh_runtime" / "bridge.mjs",
            self.root / "dsh_runtime" / "profile.patch.yml",
        ]
        integration_template = self.root / "dsh_runtime" / "integrations.example.json"
        url = str(self.cfg.get("dsh_model_url") or "").strip()
        parsed = urlsplit(url)
        return {
            "sdk_version": sdk_version,
            "bundled_runtime": runtime,
            "node": shutil.which("node"),
            "runtime_files": all(path.is_file() for path in files),
            "integration_template": integration_template.is_file(),
            "model": str(self.cfg.get("dsh_model") or "").strip(),
            "model_url_configured": parsed.scheme in ("http", "https") and bool(parsed.netloc),
            "api_key_configured": bool(self.cfg.get("dsh_api_key")),
            "approval_mode": self.cfg.get("dsh_approval_mode", "ask"),
        }

    def capabilities(self):
        info = self.diagnostics()
        available = bool(
            info["sdk_version"]
            and info["bundled_runtime"]
            and info["node"]
            and info["runtime_files"]
        )
        configured = bool(info["model"] and info["model_url_configured"])
        return {
            "kind": self.kind,
            "available": available,
            "configured": configured,
            "features": {
                "local_sdk": bool(info["sdk_version"]),
                "bundled_runtime": bool(info["bundled_runtime"]),
                "persistent_session": True,
                "stream": True,
                "status": True,
                "stop": True,
                "approval": True,
                "questions": True,
                "workspace_bridge": True,
                "scheduled_feishu_manifest": True,
                "live_model_verified": False,
            },
            "diagnostics": info,
            "note": "只检查本地 DSH SDK、运行时与配置，不调用模型；真实模型需显式运行联调任务。",
        }

    def require(self):
        info = self.diagnostics()
        if not info["sdk_version"] or not info["bundled_runtime"]:
            raise RemoteError(
                "当前 Python 环境没有可用的 DSH SDK/runtime。请安装 requirements-dsh.txt 后重启服务。",
                503,
            )
        if not info["node"]:
            raise RemoteError("本机未找到 Node.js，DSH 事件与批准 bridge 无法启动。", 503)
        if not info["runtime_files"]:
            raise RemoteError("项目的 dsh_runtime 文件不完整。", 503)
        if not info["model"]:
            raise DomainError("请先配置 DSH 模型名称。", 409)
        if not info["model_url_configured"]:
            raise DomainError("请先配置完整的 DSH 模型 API 地址（http:// 或 https://）。", 409)
        if info["approval_mode"] not in ("ask", "auto"):
            raise DomainError("DSH 工具批准方式必须为 ask 或 auto。", 409)

    def _job(self, task, prompt, history, handoff):
        return {
            "task_id": task["id"],
            "workspace_root": str(self.root),
            "runtime_root": str(self.root / "dsh_runtime"),
            "dsh_home": str(self.root / "data" / "dsh" / task["session_id"]),
            "provider": self.cfg.get("dsh_provider") or "deepseek-official",
            "model": self.cfg.get("dsh_model") or "deepseek-v4-flash",
            "model_url": str(self.cfg.get("dsh_model_url") or "").strip(),
            "api_key": self.cfg.get("dsh_api_key") or None,
            "context_threshold": float(self.cfg.get("dsh_context_threshold", 0.72)),
            "context_retain": float(self.cfg.get("dsh_context_retain", 0.16)),
            "approval_mode": self.cfg.get("dsh_approval_mode", "ask"),
            "message": prompt,
            "history": history if handoff else [],
            "handoff": handoff,
        }

    def start(self, task, prompt, session_id=None, history=None):
        self.require()
        local_session = task["session_id"]
        history = list(history or [])
        with self.lock:
            if self.closed:
                raise RemoteError("DSH 适配器正在关闭。", 503)
            worker = self.workers.get(local_session)
            fresh = worker is None or worker.proc.poll() is not None
            handoff = ""
            if fresh and session_id:
                handoff = (
                    "上一个本地 DSH 进程已结束，下面是工作台保存的会话记录。"
                    "这不是原生 session resume；不要据此重复已执行的外部写操作。"
                )
            job = self._job(task, prompt, history, handoff)
            run = {
                "status": "submitting",
                "output": "",
                "error": "",
                "approval": None,
                "input": None,
                "events": [],
                "worker": None,
                "session_id": "",
            }
            self.runs[task["id"]] = run
            if fresh:
                if worker:
                    worker.close()
                worker = _Worker(self.root, job)
                self.workers[local_session] = worker
            else:
                worker.send({"command": "run", "job": {**job, "session_id": worker.session_id}})
            run.update(status="running", worker=worker, session_id=worker.session_id)
            self._append(run, "dsh.accepted", {"session_id": worker.session_id})
            threading.Thread(
                target=self._collect,
                args=(task["id"], worker),
                name="dsh-collector-" + task["id"],
                daemon=True,
            ).start()
            return {"run_id": task["id"], "session_id": worker.session_id}

    def _append(self, run, kind, data):
        seq = run["events"][-1]["seq"] + 1 if run["events"] else 1
        run["events"].append({"seq": seq, "kind": kind, "data": data})
        if len(run["events"]) > 2000:
            run["events"] = run["events"][-2000:]
        self.changed.notify_all()

    def _collect(self, run_id, worker):
        ended_streams = 0
        while True:
            try:
                channel, line = worker.queue.get(timeout=0.5)
            except queue.Empty:
                if worker.proc.poll() is None:
                    continue
                line = None
                channel = "process"
            if line is None:
                ended_streams += 1
                if worker.proc.poll() is None or ended_streams < 2:
                    continue
                with self.lock:
                    run = self.runs.get(run_id)
                    if run and run["status"] not in TERMINAL:
                        detail = "\n".join(worker.stderr)[-3000:]
                        run.update(
                            status="failed",
                            error="本地 DSH 进程异常退出；外部工具是否已产生效果仍需按回执核对。"
                            + (("\n" + detail) if detail else ""),
                        )
                        self._append(run, "run.failed", {"error": run["error"]})
                return
            if channel == "stderr":
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if event.get("run_id") not in (None, run_id):
                continue
            with self.lock:
                run = self.runs.get(run_id)
                if not run:
                    return
                self._ingest(run, event.get("kind", "dsh.event"), event.get("data") or {})
                if run["status"] in TERMINAL:
                    return

    def _ingest(self, run, kind, data):
        if kind == "assistant.stream":
            frame = data.get("frame") or {}
            chunk = frame.get("chunk") or {}
            if chunk.get("type") in ("text-delta", "text_delta"):
                text = chunk.get("text", chunk.get("delta", ""))
                if isinstance(text, str) and text:
                    self._append(run, "assistant.delta", {"text": text})
            return
        if kind == "session.event":
            event = data.get("event") or {}
            event_type = str(event.get("type") or "")
            body = event.get("data") or {}
            if any(word in event_type.lower() for word in ("reasoning", "thinking")):
                return
            if event_type == "tool/call":
                self._append(
                    run,
                    "tool.started",
                    {
                        "name": body.get("name", "DSH tool"),
                        "call_id": body.get("callId", event.get("seq")),
                        "preview": json.dumps(body.get("arguments", {}), ensure_ascii=False)[:1200],
                    },
                )
            elif event_type == "tool/result":
                message = body.get("message") or {}
                call_id = (
                    message.get("tool_call_id")
                    or message.get("toolCallId")
                    or body.get("callId")
                    or event.get("seq")
                )
                self._append(
                    run,
                    "tool.failed" if body.get("error") else "tool.completed",
                    {
                        "name": body.get("name", "DSH tool"),
                        "call_id": call_id,
                        "preview": _visible(message or body)[:1200],
                        "is_error": bool(body.get("error")),
                    },
                )
            elif event_type == "turn/end":
                self._append(run, "dsh.turn.end", {"reason": body.get("reason")})
            return
        if kind == "interaction.pending":
            request_id = str(data.get("id") or "")
            if data.get("kind") == "approval":
                pending = {
                    "request_id": request_id,
                    "description": "DSH 工具批准：" + str(data.get("tool") or "工具"),
                    "command": str(data.get("reason") or ""),
                    "tool": data.get("tool"),
                    "call_id": data.get("call_id"),
                }
                run.update(status="waiting_for_approval", approval=pending, input=None)
                self._append(run, "approval.request", pending)
            else:
                questions = data.get("questions") if isinstance(data.get("questions"), list) else []
                summary = "\n".join(
                    str(item.get("question") or item.get("prompt") or item.get("id") or "")
                    for item in questions
                    if isinstance(item, dict)
                )
                pending = {
                    "clarify_id": request_id,
                    "question": summary or "DSH 需要补充信息",
                    "questions": questions,
                }
                run.update(status="waiting_input", input=pending, approval=None)
                self._append(run, "clarify.request", pending)
            return
        if kind == "interaction.resolved":
            run.update(status="running", approval=None, input=None)
            self._append(run, "dsh.interaction.resolved", data)
            return
        if kind == "agent.result":
            error = str(data.get("error") or "")
            reason = str(data.get("finish_reason") or "").lower()
            if error or reason in ("error", "failed"):
                status = "failed"
            elif data.get("cancel_requested") or reason in ("cancelled", "canceled", "aborted"):
                status = "cancelled"
            elif reason == "interrupted":
                status = "interrupted"
            else:
                status = "completed"
            run.update(
                status=status,
                output=str(data.get("text") or ""),
                error=error,
                approval=None,
                input=None,
            )
            self._append(run, "run." + status, {"output": run["output"], "error": error})
            return
        if kind == "agent.error":
            run["error"] = str(data.get("error") or "DSH 执行失败")
            self._append(run, "dsh.error", {"error": run["error"]})
            return
        if kind in ("agent.connecting", "bridge.warning", "control.error"):
            self._append(run, "dsh." + kind, data)

    def _run(self, handle):
        run_id = str(handle.get("run_id") or "")
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                raise RemoteError(
                    "本地 DSH 运行记录不在当前服务进程中；不会自动恢复或重发。",
                    410,
                )
            return run

    def status(self, handle):
        run = self._run(handle)
        with self.lock:
            return {
                "status": run["status"],
                "output": run["output"],
                "approval": run["approval"],
                "input": run["input"],
                "session_id": run["session_id"],
                "raw": {"error": run["error"]},
            }

    def stream(self, handle, cursor=""):
        run_id = str(handle.get("run_id") or "")
        try:
            after = int(cursor or 0)
        except (TypeError, ValueError):
            after = 0
        while True:
            with self.changed:
                run = self.runs.get(run_id)
                if not run:
                    raise RemoteError("本地 DSH 流记录已不可用。", 410)
                batch = [item for item in run["events"] if item["seq"] > after]
                terminal = run["status"] in TERMINAL
                if not batch and not terminal:
                    self.changed.wait(timeout=15)
                    continue
            for item in batch:
                after = item["seq"]
                yield item["kind"], item["data"], str(item["seq"])
            if terminal:
                return

    def stop(self, handle):
        run = self._run(handle)
        with self.lock:
            if run["status"] in TERMINAL:
                return {"requested": False, "status": run["status"]}
            run["status"] = "stopping"
            run["approval"] = None
            run["input"] = None
            worker = run["worker"]
            worker.send({"command": "cancel"})
            self._append(run, "dsh.cancel.requested", {"session_id": run["session_id"]})
            return {"requested": True}

    def reply(self, handle, card, choice, answer=""):
        run = self._run(handle)
        raw = card.get("raw") or {}
        request_id = str(raw.get("request_id") or raw.get("clarify_id") or "")
        if not request_id:
            raise DomainError("DSH 交互请求缺少稳定编号。", 409)
        if card.get("kind") == "approval":
            payload = {"decision": "allow" if choice == "once" else "deny"}
        else:
            questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
            answers = []
            for index, question in enumerate(questions or [{"id": "answer"}]):
                qid = question.get("id") if isinstance(question, dict) else None
                answers.append({"id": qid or "question-" + str(index + 1), "selected": [], "custom": answer})
            payload = {"answers": answers}
        with self.lock:
            run["worker"].send({"command": "reply", "request_id": request_id, "answer": payload})
            run.update(status="running", approval=None, input=None)
            self._append(run, "dsh.interaction.replied", {"request_id": request_id})
        return {"accepted": True, "request_id": request_id}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            workers = list(self.workers.values())
        for worker in workers:
            worker.close()
