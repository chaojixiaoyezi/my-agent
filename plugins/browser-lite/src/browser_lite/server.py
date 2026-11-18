# LLM: 独立 stdio MCP 服务只消费宿主逐次读取上下文；标准输出仅协议，浏览器子进程的输出全部丢弃。
#   退出路径（stdin EOF、SIGTERM）必须关闭浏览器；浏览器不脱离插件进程组，宿主强制回收时一并结束。
# 模块用途: 将同源工具声明接到原 MCP 传输，并把浏览器会话的失败转成中文工具错误。

from __future__ import annotations

import json
import os
import re
import signal
import sys
from importlib.metadata import version
from pathlib import Path

from my_agent_plugin_api.workspace_read_context import (
    WORKSPACE_READ_EXTENSION,
    WORKSPACE_READ_VERSION,
    WorkspaceReadContext,
)

from .access import UrlGuard
from .declarations import declaration, fields

# 宿主在动作调用 _meta 里附观察候选事实用的扩展键（与宿主 plugin_observation.OBSERVATION_META_EXTENSION 一致）
OBSERVATION_META_EXTENSION = "my-agent/observation"
# C7 两阶段受控凭据填表与隔离会话私有 _meta 扩展
CREDENTIAL_FILL_EXTENSION = "credential_fill"
CREDENTIAL_FILL_VERSION = "1"
_CREDENTIAL_REF_RE = re.compile(r"cred_[0-9a-f]{32}\Z")
from .errors import BrowserError
from .launcher import find_browser
from .session import BrowserSession

DATA_DIR_ENV = "MY_AGENT_PLUGIN_DATA_DIR"


# LLM: 对象只持有静态声明、已验证设置和唯一浏览器会话；不缓存调用权限，也不拥有宿主安装或激活状态。
# 类用途: 处理标准 MCP 初始化、工具目录及逐次浏览器调用。
class BrowserServer:
    # LLM: 设置只从宿主注入的 MY_AGENT_PLUGIN_SETTINGS 读取，坏配置明确启动失败；数据目录缺失或非绝对路径时
    #   不回退 cwd，推迟到 open 时返回明确错误。有副作用：会话会启动空闲回收线程。
    # 函数用途: 加载同源声明、工具索引、设置和浏览器会话。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.settings = fields(json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}")),
                               self.declaration["settings_schema"])
        data_dir = os.environ.get(DATA_DIR_ENV, "")
        self.session = BrowserSession(self.settings, Path(data_dir) if data_dir and Path(data_dir).is_absolute() else None)
        self.initialized = False

    # LLM: 协议错误用 JSON-RPC error；业务错误是 isError 工具结果，不能输出本地 traceback 或配置。
    # 函数用途: 分派单条协议请求，通知无需响应。
    def handle(self, request: object) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(None, -32600, "请求格式无效。")
        if "id" not in request:
            return None
        request_id, method = request["id"], request["method"]
        if type(request_id) not in (int, str):
            return self.error(None, -32600, "请求编号无效。")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "参数格式无效。")
        if method == "initialize":
            self.initialized = True
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "browser-lite", "version": version("my-agent-browser-lite")},
                "capabilities": {
                    "tools": {},
                    "experimental": {
                        WORKSPACE_READ_EXTENSION: {"versions": [WORKSPACE_READ_VERSION]},
                        CREDENTIAL_FILL_EXTENSION: {"versions": [CREDENTIAL_FILL_VERSION]},
                    },
                },
            }
        elif not self.initialized:
            return self.error(request_id, -32000, "请先初始化连接。")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
                                for tool in self.tools.values()]}
        elif method == "tools/call":
            result = self.call(params)
        else:
            return self.error(request_id, -32601, "不支持此方法。")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # LLM: 每个工具先确认浏览器可执行文件存在（找不到即“浏览器不可用”，不假装成功）；除 close 外都要求宿主读取上下文，
    #   权限与两阶段凭据上下文只从 _meta 取得，arguments 不能冒充上下文。
    # 函数用途: 验证参数和上下文后执行浏览器动作，所有失败都转成中文错误结果。
    def call(self, params: dict) -> dict:
        try:
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                raise BrowserError("UNKNOWN_TOOL", "工具不存在。")
            metadata = params.get("_meta")
            raw_cred = metadata.get(CREDENTIAL_FILL_EXTENSION) if isinstance(metadata, dict) else None
            if isinstance(raw_cred, dict) and raw_cred.get("session_revoked") is True:
                self.session.revoke_scope(raw_cred)
            arguments = fields(params.get("arguments", {}), self.tools[name]["input_schema"])
            credential_meta = self._credential_meta(name, arguments, metadata)
            find_browser(self.settings["chrome_path"])
            if name == "close":
                return self.result(self.session.close_browser(credential_meta), False)
            if not isinstance(metadata, dict) or WORKSPACE_READ_EXTENSION not in metadata:
                raise BrowserError("MISSING_CONTEXT", "缺少宿主逐次工作区读取上下文。")
            context = WorkspaceReadContext.from_payload(metadata[WORKSPACE_READ_EXTENSION])
            guard = UrlGuard(context, self.settings["allowed_hosts"])
            return self.result(
                self.session.run(
                    name,
                    arguments,
                    guard,
                    observation=self._observation(name, arguments, metadata),
                    credential_meta=credential_meta,
                ),
                False,
            )
        except BrowserError as exc:
            return self.result({"code": exc.code, "message": str(exc), **exc.extra}, True)
        except OSError:
            return self.result({"code": "IO_FAILED", "message": "浏览器或 profile 目录读写失败，请稍后重试。"}, True)
        except (ValueError, TypeError, KeyError):
            return self.result({"code": "INVALID_CONTEXT", "message": "宿主工作区上下文或浏览器响应无效。"}, True)

    # 函数用途: 强制 fill 参数组合互斥（value 与 credential_ref+field 严格二选一）并核对私有 _meta.credential_fill。
    def _credential_meta(self, name: str, arguments: dict, metadata: object) -> dict | None:
        raw = metadata.get(CREDENTIAL_FILL_EXTENSION) if isinstance(metadata, dict) else None
        if raw is not None and not isinstance(raw, dict):
            raise BrowserError("INVALID_CONTEXT", "宿主凭据上下文格式无效。")
        if name != "fill":
            if isinstance(raw, dict) and raw.get("phase") not in (None, "", "session"):
                raise BrowserError("INVALID_CONTEXT", "非 fill 工具不接受两阶段填表上下文。")
            return raw
        has_value = "value" in arguments
        has_ref = "credential_ref" in arguments
        has_field = "field" in arguments
        if has_value and (has_ref or has_field):
            raise BrowserError(
                "INVALID_ARGUMENTS",
                "参数互斥冲突：value 与 credential_ref/field 严格二选一，不能同时传入。",
                conflicting=["value", *(k for k in ("credential_ref", "field") if k in arguments)],
                side_effect="not_applied",
            )
        if has_value:
            if isinstance(raw, dict) and raw.get("phase") in ("inspect", "apply"):
                raise BrowserError("INVALID_CONTEXT", "普通 value 填表不能携带宿主凭据填表阶段。", side_effect="not_applied")
            return raw
        if has_ref and not has_field:
            raise BrowserError(
                "INVALID_ARGUMENTS",
                "缺少参数 field：使用 credential_ref 填表时必须同时提供 field（常规账号密码卡为 'username' 或 'secret'）。",
                missing="field",
                side_effect="not_applied",
            )
        if has_field and not has_ref:
            raise BrowserError(
                "INVALID_ARGUMENTS",
                "缺少参数 credential_ref：传入 field 时必须同时提供有效的 credential_ref（cred_<32位十六进制>），普通文本请改用 value。",
                missing="credential_ref",
                side_effect="not_applied",
            )
        if not has_ref and not has_field:
            raise BrowserError(
                "INVALID_ARGUMENTS",
                "缺少填写内容参数：请提供 value（普通文本，允许空串），或同时提供 credential_ref 与 field。",
                missing="value_or_credential_ref",
                side_effect="not_applied",
            )
        if not _CREDENTIAL_REF_RE.fullmatch(arguments["credential_ref"]):
            raise BrowserError(
                "INVALID_ARGUMENTS",
                "参数 credential_ref 格式无效，应为 cred_<32位小写十六进制>。",
                field="credential_ref",
                side_effect="not_applied",
            )
        if not isinstance(raw, dict) or raw.get("phase") not in ("inspect", "apply"):
            raise BrowserError(
                "MISSING_CONTEXT",
                "凭据填表只能由宿主经受控两阶段 _meta.credential_fill 调用。",
                side_effect="not_applied",
            )
        return raw

    # LLM: 只有声明了 observation_ref 的工具、且模型填了该参数时才要求 _meta 带宿主观察上下文；候选事实只从 _meta 取，
    #   arguments 里的 candidate_id 本身不解析（宿主已复核并换成了 key 与代次）。没填参数返回 None，沿 selector 路径。
    # 函数用途: 取本次动作调用的观察候选上下文。
    def _observation(self, name: str, arguments: dict, metadata: dict) -> dict | None:
        ref = self.tools[name].get("observation_ref")
        if not isinstance(ref, dict) or not arguments.get(ref.get("param")):
            return None
        observation = metadata.get(OBSERVATION_META_EXTENSION)
        if not isinstance(observation, dict):
            raise BrowserError("MISSING_CONTEXT", "候选调用缺少宿主观察上下文，请直接用 selector 或重新 read。")
        return observation

    # LLM: 结果使用原 MCP 文本结构，正文为结构化 JSON（保留中文便于阅读）；观察候选与候选错误只进 structuredContent、不进正文，
    #   宿主会把它改写成模型可见的有界投影；不伪装自定义 TUI 面板。
    # 函数用途: 编码正常或错误的工具返回值。
    @staticmethod
    def result(value: dict, error: bool) -> dict:
        structured = {key: value[key] for key in ("my_agent_observation", "my_agent_observation_error") if key in value}
        body = {key: item for key, item in value.items() if key not in structured}
        encoded = {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": error}
        if structured:
            encoded["structuredContent"] = structured
        return encoded

    # LLM: 这里只生成协议响应，不输出调试堆栈或输入正文。
    # 函数用途: 创建标准 JSON-RPC 错误。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: SIGTERM 转成 SystemExit，让 main 的 finally 关闭浏览器；只在主线程注册。
# 函数用途: 处理终止信号。
def _terminate(_signum, _frame) -> None:
    raise SystemExit(0)


# LLM: 单条输入有界；超长行关闭本进程交由宿主记录真实退出。无论正常 EOF、SIGTERM 还是异常，finally 都关闭浏览器。
# 函数用途: 运行插件标准输入输出入口，每条正常请求即时刷新响应。
def main() -> int:
    try:
        server = BrowserServer()
    except (ValueError, TypeError, BrowserError):
        print("插件设置无效。", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _terminate)
    try:
        while line := sys.stdin.buffer.readline(131073):
            if len(line) > 131072:
                print("插件请求超过读取上限。", file=sys.stderr)
                return 2
            try:
                response = server.handle(json.loads(line))
            except (ValueError, UnicodeError, RecursionError):
                response = server.error(None, -32700, "请求 JSON 无效。")
            if response is not None:
                print(json.dumps(response, ensure_ascii=True), flush=True)
        return 0
    finally:
        # 关闭期间不再被第二个 SIGTERM 打断，避免浏览器停到一半
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        server.session.shutdown()
