#!/usr/bin/env python3
# LLM: 此模块只为 TUI 黑盒验收提供确定性 Anthropic 兼容响应；不得接入产品 provider 路由、读取真实密钥或保存完整请求正文。
# 模块用途: 在 loopback 上驱动 终端交互/my-agent 的固定流式、思考、工具和错误场景，隔离外部模型健康对界面验收的影响。

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_ID = "claude-sonnet-4-5-20250929"
SCENARIO_MARKERS = {
    "TUI_FIXTURE_MARKDOWN": "markdown",
    "TUI_FIXTURE_THINKING": "thinking",
    "TUI_FIXTURE_PERMISSION": "permission",
    "TUI_FIXTURE_ERROR": "error",
    "TUI_FIXTURE_TERMINAL_CONTROL": "terminal_control",
}


# LLM: FixtureConfig 只保存公开测试参数；新增字段时不得加入 token、header 或用户原文。
# 类用途: 汇总服务器的默认场景、事件节奏和脱敏审计落点。
@dataclass(frozen=True)
class FixtureConfig:
    default_scenario: str = "markdown"
    event_delay_ms: int = 20
    audit_path: Path | None = None


# LLM: FixtureServer 向 handler 暴露只读配置和串行审计锁，不维护第二份对话事实。
# 类用途: 承载确定性响应配置，并让并发请求安全追加脱敏审计行。
class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    # LLM: 构造函数只绑定 loopback listener 与公开配置；调用方负责关闭 server。
    # 函数用途: 创建可并发处理 终端交互 启动探测和消息请求的本地服务器。
    def __init__(self, address: tuple[str, int], config: FixtureConfig) -> None:
        super().__init__(address, FixtureHandler)
        self.config = config
        self.audit_lock = threading.Lock()

    # LLM: 审计只能写协议形状和计数，禁止写 header、API key、prompt 或 tool 参数正文。
    # 函数用途: 为每次请求留下可核对但不泄密的 JSONL 摘要。
    def append_audit(self, payload: dict[str, Any]) -> None:
        if self.config.audit_path is None:
            return
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        with self.audit_lock:
            self.config.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line)


# LLM: Handler 实现最小 Anthropic HTTP/SSE 表面；不得把未知路径宽松转发到外网。
# 类用途: 响应 models、count_tokens 和 messages，并按测试标记生成固定事件序列。
class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "MyAgentTuiFixture/1"
    protocol_version = "HTTP/1.1"

    # LLM: GET 只允许模型目录和单模型详情，其他路径必须明确 404。
    # 函数用途: 满足 终端交互 启动时的模型存在性检查。
    def do_GET(self) -> None:  # noqa: N802 - http.server 固定接口名
        path = self.path.split("?", 1)[0]
        if path == "/v1/models":
            self._send_json(
                200,
                {
                    "data": [_model_record()],
                    "has_more": False,
                    "first_id": MODEL_ID,
                    "last_id": MODEL_ID,
                },
            )
            self._audit("GET", path, scenario="models")
            return
        if path == f"/v1/models/{MODEL_ID}":
            self._send_json(200, _model_record())
            self._audit("GET", path, scenario="model")
            return
        self._send_json(404, _error_payload("not_found", "fixture path not found"))
        self._audit("GET", path, scenario="not_found", status=404)

    # LLM: POST 只解析有界 JSON 并路由已声明端点；畸形输入返回结构化 400，不猜测或转发。
    # 函数用途: 处理 token 估算与流式/非流式消息请求。
    def do_POST(self) -> None:  # noqa: N802 - http.server 固定接口名
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
        if payload is None:
            return
        if path == "/v1/messages/count_tokens":
            input_tokens = max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)
            self._send_json(200, {"input_tokens": input_tokens})
            self._audit("POST", path, scenario="count_tokens")
            return
        if path != "/v1/messages":
            self._send_json(404, _error_payload("not_found", "fixture path not found"))
            self._audit("POST", path, scenario="not_found", status=404)
            return
        scenario = _select_scenario(payload, self.fixture_config.default_scenario)
        if scenario == "error":
            self._audit("POST", path, payload=payload, scenario=scenario, status=429)
            self._send_json(429, _error_payload("rate_limit_error", "Deterministic fixture error"))
            return
        blocks, stop_reason = _scenario_blocks(scenario, payload)
        # 先提交脱敏审计再暴露响应终态，保证客户端读完 body 时 evidence 已经可见；
        # 否则 ThreadingHTTPServer 会让测试/录制器与 handler 尾部写账发生竞态。
        self._audit("POST", path, payload=payload, scenario=scenario)
        if bool(payload.get("stream")):
            self._send_stream(blocks, stop_reason, str(payload.get("model") or MODEL_ID))
        else:
            self._send_message(blocks, stop_reason, str(payload.get("model") or MODEL_ID))

    # LLM: 属性只做 server 子类收窄，禁止在 handler 内读取全局可变配置。
    # 函数用途: 取得当前 fixture 的不可变公开参数。
    @property
    def fixture_config(self) -> FixtureConfig:
        return self.server.config  # type: ignore[attr-defined,no-any-return]

    # LLM: JSON reader 使用 Content-Length 上限，避免测试服务被意外大请求拖垮。
    # 函数用途: 读取并验证一个最大 8 MiB 的 JSON 请求体。
    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 8 * 1024 * 1024:
            self._send_json(413, _error_payload("invalid_request_error", "request body too large"))
            return None
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _error_payload("invalid_request_error", "invalid JSON"))
            return None
        if not isinstance(value, dict):
            self._send_json(400, _error_payload("invalid_request_error", "JSON object required"))
            return None
        return value

    # LLM: 普通消息响应保持 Anthropic content block 结构，供非流式启动探测和测试复用。
    # 函数用途: 返回一次完整的确定性 assistant message。
    def _send_message(self, blocks: list[dict[str, Any]], stop_reason: str, model: str) -> None:
        output_tokens = max(1, len(json.dumps(blocks, ensure_ascii=False)) // 4)
        self._send_json(
            200,
            {
                "id": "msg_tui_fixture",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": blocks,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 32, "output_tokens": output_tokens},
            },
        )

    # LLM: SSE 严格按 block start/delta/stop 与 message terminal 排序，事件延迟只用于可见流式验收。
    # 函数用途: 把固定内容块编码成 Anthropic 兼容事件流并主动关闭连接。
    def _send_stream(self, blocks: list[dict[str, Any]], stop_reason: str, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_tui_fixture",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 32, "output_tokens": 0},
            },
        }
        self._write_sse("message_start", start)
        for index, block in enumerate(blocks):
            for event_type, event_payload in _stream_block(index, block):
                self._write_sse(event_type, event_payload)
        output_tokens = max(1, len(json.dumps(blocks, ensure_ascii=False)) // 4)
        self._write_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
        self._write_sse("message_stop", {"type": "message_stop"})
        self.close_connection = True

    # LLM: SSE writer 不缓存跨事件状态；断连只影响当前确定性请求。
    # 函数用途: 写入一个带 event/data 的 SSE 帧并按配置制造短暂可见延迟。
    def _write_sse(self, event_type: str, payload: dict[str, Any]) -> None:
        wire = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(wire.encode("utf-8"))
        self.wfile.flush()
        delay = max(0, self.fixture_config.event_delay_ms) / 1000.0
        if delay:
            time.sleep(delay)

    # LLM: JSON response 总是声明长度和 close，避免 keep-alive 让下一请求体被误解析。
    # 函数用途: 返回一个完整 JSON 响应。
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    # LLM: 审计投影只包含请求形状和计数；不得把 payload 原值写入文件。
    # 函数用途: 记录当前请求的脱敏协议摘要。
    def _audit(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        scenario: str,
        status: int = 200,
    ) -> None:
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        tools = payload.get("tools", []) if isinstance(payload, dict) else []
        self.server.append_audit(  # type: ignore[attr-defined]
            {
                "method": method,
                "path": path,
                "scenario": scenario,
                "status": status,
                "stream": bool(payload.get("stream")) if isinstance(payload, dict) else False,
                "message_count": len(messages) if isinstance(messages, list) else 0,
                "tool_count": len(tools) if isinstance(tools, list) else 0,
                "has_tool_result": _has_tool_result(payload or {}),
            }
        )


# LLM: 模型目录记录是公开 fixture 元数据，必须保持与 messages 默认 model 一致。
# 函数用途: 构造 Anthropic Models API 的单条固定记录。
def _model_record() -> dict[str, str]:
    return {
        "type": "model",
        "id": MODEL_ID,
        "display_name": "TUI Fixture Sonnet",
        "created_at": "2026-01-01T00:00:00Z",
    }


# LLM: 场景选择只属于测试数据层；产品运行时不得按 prompt 标记分支。
# 函数用途: 从固定测试标记选择响应脚本，未命中时使用显式默认值。
def _select_scenario(payload: dict[str, Any], default: str) -> str:
    visible = _message_text(payload.get("messages", []))
    latest = max(
        ((visible.rfind(marker), scenario) for marker, scenario in SCENARIO_MARKERS.items()),
        default=(-1, default),
    )
    return latest[1] if latest[0] >= 0 else default


# LLM: 文本提取只用于 fixture 场景路由，不能复用到产品状态判断。
# 函数用途: 取得消息中 text block 的有界拼接，忽略工具参数和结果正文。
def _message_text(messages: Any) -> str:
    parts: list[str] = []
    if not isinstance(messages, list):
        return ""
    for message in messages[-20:]:
        content = message.get("content") if isinstance(message, dict) else None
        parts.extend(_content_text_parts(content))
    return "\n".join(parts)


# LLM: helper 只提取显式 text block，工具和其他开放类型一律忽略。
# 函数用途: 把单条消息 content 转成最多若干个有界文本片段。
def _content_text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content[:4096]]
    if not isinstance(content, list):
        return []
    return [
        str(block.get("text", ""))[:4096]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]


# LLM: tool_result 检测只读结构化 block type，禁止匹配正文词语。
# 函数用途: 判断 permission 场景是否已执行过 fixture Bash 工具。
def _has_tool_result(payload: dict[str, Any]) -> bool:
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return False
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            return True
    return False


# LLM: 场景块只生成公开固定内容；新增场景必须在 Feature Spec/测试矩阵登记。
# 函数用途: 构造 Markdown、thinking 或权限工具调用的确定性 content blocks。
def _scenario_blocks(scenario: str, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    if scenario == "terminal_control":
        return (
            [
                {
                    "type": "text",
                    "text": (
                        "CONTROL_START\x1b[2J\x1b[999;999H"
                        "\x1b]0;fixture-owned\x07"
                        "\x1b]8;;https://example.invalid\x1b\\link\x1b]8;;\x1b\\"
                        "\x1b]52;c;c2VjcmV0\x07"
                        "\x1bPfixture-dcs\x1b\\\x1b_fixture-apc\x1b\\"
                        "\x1b^fixture-pm\x1b\\\x9b31m\x9dtitle\x9cCONTROL_END"
                    ),
                }
            ],
            "end_turn",
        )
    if scenario == "thinking":
        return (
            [
                {"type": "thinking", "thinking": "先核对结构，再给出结果。", "signature": "fixture"},
                {"type": "text", "text": "## 结果\n\n思考与正文是两个稳定消息块。"},
            ],
            "end_turn",
        )
    if scenario == "permission" and not _has_tool_result(payload):
        return (
            [
                {
                    "type": "tool_use",
                    "id": "toolu_tui_fixture",
                    "name": "Bash",
                    "input": {"command": "printf 'fixture-tool-ok\\n'", "description": "输出固定测试文本"},
                }
            ],
            "tool_use",
        )
    if scenario == "permission":
        return ([{"type": "text", "text": "工具已返回，当前回合在同一会话中继续并完成。"}], "end_turn")
    return (
        [
            {
                "type": "text",
                "text": (
                    "## Fixture 标题\n\n"
                    "- 中文宽字符：你好，终端\n"
                    "- **粗体**、`inline_code()` 与 [链接](https://example.com)\n\n"
                    "> 这是引用行。\n\n"
                    "```python\nprint(\"fixture\")\n```\n\n"
                    "| 列 A | 列 B |\n|---|---|\n| 1 | 二 |"
                ),
            }
        ],
        "end_turn",
    )


# LLM: block 编码器只接受本模块生成的已知 content block，未知类型必须失败而非静默转文字。
# 函数用途: 将一个完整 content block 拆成 Anthropic 的 start/delta/stop 事件。
def _stream_block(index: int, block: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    block_type = block.get("type")
    if block_type == "text":
        yield "content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }
        for chunk in _text_chunks(str(block.get("text", "")), 24):
            yield "content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": chunk},
            }
    elif block_type == "thinking":
        yield "content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        }
        for chunk in _text_chunks(str(block.get("thinking", "")), 16):
            yield "content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "thinking_delta", "thinking": chunk},
            }
        yield "content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": str(block.get("signature", "fixture"))},
        }
    elif block_type == "tool_use":
        yield "content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": str(block["id"]),
                "name": str(block["name"]),
                "input": {},
            },
        }
        yield "content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)},
        }
    else:
        raise ValueError(f"unsupported fixture block type: {block_type!r}")
    yield "content_block_stop", {"type": "content_block_stop", "index": index}


# LLM: 文本分片按 Python 字符边界，仅用于制造稳定可见流式节奏，不参与产品 token 计量。
# 函数用途: 把固定文本切成非空的确定长度片段。
def _text_chunks(text: str, size: int) -> Iterable[str]:
    for start in range(0, len(text), max(1, size)):
        yield text[start : start + max(1, size)]


# LLM: 错误响应保持 Anthropic error 信封，错误文案只是测试显示数据而非状态权威。
# 函数用途: 构造固定的 API 错误响应。
def _error_payload(error_type: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


# LLM: 工厂函数供 CLI 和 pytest 共用，默认只允许 loopback 地址。
# 函数用途: 创建尚未启动的 fixture server，端口可传 0 让系统分配。
def make_server(host: str, port: int, config: FixtureConfig) -> FixtureServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("TUI fixture server 只允许监听 loopback")
    return FixtureServer((host, port), config)


# LLM: CLI 参数只控制公开场景/延迟/落点，不接受或读取认证秘密。
# 函数用途: 解析 fixture server 的命令行参数。
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动确定性 TUI Anthropic 协议 fixture")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18788)
    parser.add_argument(
        "--default-scenario",
        choices=("markdown", "thinking", "permission", "error", "terminal_control"),
        default="markdown",
    )
    parser.add_argument("--event-delay-ms", type=int, default=20)
    parser.add_argument("--audit-path", type=Path)
    parser.add_argument("--ready-file", type=Path)
    return parser.parse_args()


# LLM: 主入口只启动 loopback server，ready 文件仅写公开 host/port/pid-free 元数据。
# 函数用途: 运行服务直到收到中断，并在退出时可靠关闭监听器。
def main() -> int:
    args = _parse_args()
    config = FixtureConfig(
        default_scenario=args.default_scenario,
        event_delay_ms=max(0, args.event_delay_ms),
        audit_path=args.audit_path,
    )
    server = make_server(args.host, args.port, config)
    host, port = server.server_address[:2]
    if args.ready_file is not None:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(
            json.dumps({"host": host, "port": port}, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({"ready": True, "host": host, "port": port}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
