#!/usr/bin/env python3
"""Explicit DSH diagnostic. --live makes one real model request."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.dsh import DSHAdapter, TERMINAL
from app.store import Store


def main():
    parser = argparse.ArgumentParser(description="检查本地 DSH SDK；--live 会产生一次真实模型请求")
    parser.add_argument("--live", action="store_true", help="显式调用当前配置的真实模型")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--db", default=os.environ.get("WORKBENCH_DB", str(ROOT / "data" / "workbench.sqlite3")))
    args = parser.parse_args()
    cfg = Store(args.db).settings()["agent"]
    adapter = DSHAdapter(cfg, root=ROOT)
    report = adapter.capabilities()
    report["live_requested"] = args.live
    report["live_model_verified"] = False
    if not args.live:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["available"] else 1
    try:
        adapter.require()
        task = {"id": "DSH-SMOKE-" + str(int(time.time())), "session_id": "explicit-live-smoke"}
        handle = adapter.start(
            task,
            "这是一次用户显式发起的 DSH 连通性测试。不要调用任何工具，只回复 DSH_LIVE_OK。",
            history=[],
        )
        deadline = time.monotonic() + max(10, args.timeout)
        while time.monotonic() < deadline:
            status = adapter.status(handle)
            if status["status"] in TERMINAL:
                report["live_status"] = status["status"]
                report["live_reply"] = status["output"][:1000]
                report["live_error"] = status["raw"].get("error", "")
                report["live_model_verified"] = status["status"] == "completed" and bool(status["output"])
                break
            time.sleep(0.2)
        else:
            adapter.stop(handle)
            report["live_status"] = "timeout"
            report["live_error"] = "达到 smoke 超时，已请求停止；不会自动重试。"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["live_model_verified"] else 1
    except Exception as exc:
        report["live_status"] = "failed"
        report["live_error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        adapter.close()


if __name__ == "__main__":
    sys.exit(main())
