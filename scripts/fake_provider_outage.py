#!/usr/bin/env python3
"""假模型端点:模拟"额度断供→自动恢复"(资源自愈演练用,零第三方依赖)。

行为:启动后头 --outage-seconds 秒对所有 /chat/completions 请求返回 HTTP 429
rate_limit(模拟额度用满),之后返回正常 200(OpenAI 兼容,支持流式/非流式),
模拟额度刷新。所有请求逐条记账到 --log(ndjson:时刻/阶段/状态码),作为
"断供期打了几次、恢复后何时续跑"的独立证据源。

用法(隔离演练,别指生产):
  python scripts/fake_provider_outage.py --port 18497 --outage-seconds 90 \
      --log /tmp/drill/provider_requests.ndjson
把隔离部署的 api_base 指到 http://127.0.0.1:18497/v1 即可。
GET /_drill_status 可随时查当前阶段。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_START_AT = time.time()
_OUTAGE_SECONDS = 90.0
_LOG_PATH: Path | None = None
_LOG_LOCK = threading.Lock()
_COUNTS = {"outage_429": 0, "recovered_200": 0}

_RATE_LIMIT_BODY = {
    "error": {
        "type": "rate_limit_error",
        "message": "已达到 Token Plan 用量上限(演练模拟,过一会儿自动刷新) (2056)",
        "code": "rate_limit_exceeded",
    }
}

_HEALTHY_TEXT = (
    "(演练模型)本轮判读完成:已核对数据源,未发现新命中,任务继续保持监控。"
)


def _phase(now: float | None = None) -> str:
    elapsed = (now if now is not None else time.time()) - _START_AT
    return "outage" if elapsed < _OUTAGE_SECONDS else "recovered"


def _log_request(record: dict) -> None:
    if _LOG_PATH is None:
        return
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _LOG_LOCK:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认 stderr 访问日志(证据走 ndjson)
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/_drill_status":
            body = json.dumps(
                {
                    "phase": _phase(),
                    "elapsed_seconds": round(time.time() - _START_AT, 1),
                    "outage_seconds": _OUTAGE_SECONDS,
                    "counts": dict(_COUNTS),
                }
            ).encode("utf-8")
            self._send(200, "application/json", body)
            return
        self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        now = time.time()
        phase = _phase(now)
        record = {
            "at": round(now, 3),
            "elapsed": round(now - _START_AT, 1),
            "path": self.path,
            "phase": phase,
            "stream": bool(payload.get("stream")),
        }
        if phase == "outage":
            _COUNTS["outage_429"] += 1
            record["status"] = 429
            _log_request(record)
            body = json.dumps(_RATE_LIMIT_BODY, ensure_ascii=False).encode("utf-8")
            self._send(429, "application/json", body)
            return
        _COUNTS["recovered_200"] += 1
        record["status"] = 200
        _log_request(record)
        if payload.get("stream"):
            self._send_sse(_HEALTHY_TEXT, model=str(payload.get("model") or "fake-model"))
        else:
            self._send_completion(_HEALTHY_TEXT, model=str(payload.get("model") or "fake-model"))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_completion(self, text: str, *, model: str) -> None:
        body = json.dumps(
            {
                "id": "drill-cmpl",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(200, "application/json", body)

    def _send_sse(self, text: str, *, model: str) -> None:
        chunks = [
            {"id": "drill-cmpl", "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
            {"id": "drill-cmpl", "object": "chat.completion.chunk", "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
        ]
        payload = b"".join(
            b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n" for chunk in chunks
        ) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    global _START_AT, _OUTAGE_SECONDS, _LOG_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--outage-seconds", type=float, default=90.0)
    parser.add_argument("--log", default="")
    args = parser.parse_args()

    _OUTAGE_SECONDS = max(0.0, float(args.outage_seconds))
    if args.log:
        _LOG_PATH = Path(args.log).expanduser()
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _START_AT = time.time()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(
        f"[fake-provider] listening on 127.0.0.1:{args.port} "
        f"outage_seconds={_OUTAGE_SECONDS} log={args.log or '-'}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
