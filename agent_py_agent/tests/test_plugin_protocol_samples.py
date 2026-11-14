"""非 Python 插件样例、协议合同与联合收紧/观察链集成用例。

合并自原 5 份样例与协议组合测试：
- 跨语言工作区读取一致性（plugins/sdk/conformance/workspace_read_check.json）与 hello-node / hello-go 宿主链路；
- M1 B8 样例协议合同（event-watch、rm-guard、rm-guard-node、arguments_truncated 只能更严）；
- M1 联合组件测试（真实样例 stdio + Gateway 请求执行路径 + PluginEventHub + PluginDisplayService）；
- B5 收紧执行器联合链路（真实 rm-guard 征询、审批桥、归档、runtime_events、/plugins info）；
- B5+B4+B8 组合链（真实 rm-guard 协议、ToolRegistry 与归档写入决定账本）。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import core, plugin_management
from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.contracts.gates.command_policy import evaluate_command_policy
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import http_handlers, plugin_panels_http
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    execute_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.recovery import _finish_gateway_conversation_claim
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.plugin_channel import PluginChannelPool
from agent_py_agent.agent.plugin_display.service import (
    DisplayWiring,
    PanelQuery,
    PluginDisplayService,
)
from agent_py_agent.agent.plugin_entry import host_platform_tag
from agent_py_agent.agent.plugin_events.declarations import PLUGIN_EVENT_TYPES
from agent_py_agent.agent.plugin_events.hub import EventHubWiring, PluginEventHub
from agent_py_agent.agent.plugin_events.points import EventPointContext
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.executor import ToolExecutorRequest
from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPStdioClient
from agent_py_agent.agent.tooling.mcp_protocol import unwrap_jsonrpc
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)
from agent_py_agent.agent.workspace_read_context import WorkspaceReadContext
from agent_py_agent.tests import test_plugin_event_e2e as _event_e2e
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.plugin_enable_fixtures import confirm_legacy_enable, preview_legacy_enable
from agent_py_agent.tests.test_plugin_event_display import decisions, info_message
from agent_py_agent.tests.test_plugin_event_e2e import (
    _FIELDS,
    _Backend,
    _prepare_case,
    _reply_with_user_denial,
)
from agent_py_agent.tests.test_plugin_event_gateway import _Handler, gateway
from agent_py_agent.tests.test_plugin_event_hub import _ManualExecutor
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_tool_gate_execution import _Harness
from scripts.build_plugin_files_package import build_files_package

_FIXTURES = (gateway,)
ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "agent_py_agent" / "tests" / "plugin_samples" / "fixtures"
VECTORS = ROOT / "plugins" / "sdk" / "conformance" / "workspace_read_check.json"
NODE = shutil.which("node")
GO = shutil.which("go")
_PUBLISH = plugin_panels_http.publish_plugin_event

_REASON_CODE = re.compile(r"[A-Z0-9_]{1,40}\Z")
_VERDICTS = ("allow_as_is", "ask", "deny")
_EVENT_TYPES = (
    "prompt_submitted",
    "turn_started",
    "turn_ended",
    "tool_call_started",
    "tool_call_finished",
    "command_executed",
)
# 与两个收紧样例共用的一组输入：前六种是删除整目录写法（含大写 R 变体，b8t 初审补强），后四种不是。
_REVIEW_CASES = (
    ("rm -rf build", "ask", "RM_RF"),
    ("rm -fr build", "ask", "RM_RF"),
    ("rm -r -f build", "ask", "RM_RF"),
    ("rm --recursive --force build", "ask", "RM_RF"),
    ("sudo rm -rf /tmp/build", "ask", "RM_RF"),
    ("rm -Rf build", "ask", "RM_RF"),
    ("ls -la", "allow_as_is", "NO_MATCH"),
    ("rm notes.txt", "allow_as_is", "NO_MATCH"),
    ("rm -f notes.txt", "allow_as_is", "NO_MATCH"),
    ("grep -rf pattern .", "allow_as_is", "NO_MATCH"),
)
# 与两个收紧样例共用的补丁删除门输入：只认行首的 `*** Delete File: ` 段头（与宿主解析同款，b8dg）；
# 正文行里的同样字样（加号行、上下文行）不算删除；空路径删除段按参数不可用处理。
_DELETE_PATCH_CASES = (
    ("*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch\n", "deny", "DELETE_FILE_BLOCKED"),
    ("*** Begin Patch\n*** Update File: notes.txt\n-old\n+new\n*** End Patch\n", "allow_as_is", "NO_MATCH"),
    ("*** Begin Patch\n*** Add File: notes.txt\n+hello\n*** End Patch\n", "allow_as_is", "NO_MATCH"),
    ("*** Begin Patch\n*** Update File: notes.txt\n-old\n+new\n*** Delete File: obsolete.txt\n*** End Patch\n",
     "deny", "DELETE_FILE_BLOCKED"),
    ("*** Begin Patch\n*** Update File: notes.txt\n-old\n+*** Delete File: not-a-header.txt\n*** End Patch\n",
     "allow_as_is", "NO_MATCH"),
    ("*** Begin Patch\n*** Update File: notes.txt\n *** Delete File: context-line\n*** End Patch\n",
     "allow_as_is", "NO_MATCH"),
    ("*** Begin Patch\r\n*** Delete File: obsolete.txt\r\n*** End Patch\r\n", "deny", "DELETE_FILE_BLOCKED"),
    ("*** Begin Patch\r*** Delete File: obsolete.txt\r*** End Patch\r", "deny", "DELETE_FILE_BLOCKED"),
    ("*** Begin Patch\n*** Delete File: \n*** End Patch\n", "ask", "ARGUMENTS_UNAVAILABLE"),
)
# b8tr/trs：截断只能更严——先按看到的片段判（plain），再看截断标记：片段已 deny 保持 deny（原原因码）；
# 片段本来就 ask 保留原原因码、消息补半句"参数还被截断了"；只有片段可放行才升到 ask + ARGUMENTS_TRUNCATED；
# 字段缺失、显式 false、以及 1/"true" 这类真值都完全照旧（plain，标记只认布尔 true）。
_TRUNCATION_CASES = (
    ("guard-rm", "run_command", {"command": "ls -la"},
     ("allow_as_is", "NO_MATCH"), ("ask", "ARGUMENTS_TRUNCATED")),
    ("guard-rm", "run_command", {"command": "rm -rf build"},
     ("ask", "RM_RF"), ("ask", "RM_RF")),
    ("guard-delete", "apply_patch", {"patch": "*** Begin Patch\n*** Update File: notes.txt\n-old\n+new\n*** End Patch\n"},
     ("allow_as_is", "NO_MATCH"), ("ask", "ARGUMENTS_TRUNCATED")),
    ("guard-delete", "apply_patch", {"patch": "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch\n"},
     ("deny", "DELETE_FILE_BLOCKED"), ("deny", "DELETE_FILE_BLOCKED")),
    ("guard-rm", "write_file", {"path": "x"},
     ("deny", "OUT_OF_SCOPE"), ("deny", "OUT_OF_SCOPE")),
)
_TRUNCATED_MESSAGE = "参数太长被截断，看不全，先确认一次"
_TRUNCATED_NOTE = "；参数还被截断了"

_DELETE_PATCH = '*** Begin Patch\n*** Delete File: disposable.txt\n*** End Patch\n'
_EXPECTED_FIELDS = {
    'plugin_id', 'version', 'activation_id', 'gate_id', 'tool', 'call_id', 'operation_id',
    'args_hash', 'actor', 'outcome', 'verdict', 'reason_code', 'latency_ms', 'host_status', 'final_status',
}
# 宿主放行、只有 rm-guard 要求确认的差集命令（无删除副作用）。shellwrap 以后 sh -c 包一层的 rm -rf 由宿主硬拒，
# 不能再当差集用；改动时先确认 evaluate_command_policy(...).allowed 仍为真、插件仍回 ask/RM_RF。
_GUARD_ONLY_COMMAND = 'echo rm -rf build'


# ---- 跨语言读取一致性与 hello-node / hello-go 宿主链路 ----

# 函数用途: 把用例里所有字符串中的 {root} 换成目录树根。
def _substitute(value, root: str):
    if isinstance(value, str):
        return value.replace("{root}", root)
    if isinstance(value, list):
        return [_substitute(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, root) for key, item in value.items()}
    return value


# LLM: 目录树只建在 tmp_path；根取真实路径，与宿主总是下发规范真实路径一致（macOS 的 /var 是链接）。
# 函数用途: 按用例建文件与符号链接，返回 (根, 已替换 {root} 的用例)。
def _materialize(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    root = os.path.realpath(root)
    raw = json.loads(VECTORS.read_text(encoding="utf-8"))
    for relative in raw["tree"]["files"]:
        target = Path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    for relative, link in raw["tree"]["symlinks"].items():
        target = Path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link.replace("{root}", root), target)
    return root, _substitute(raw, root)


# 函数用途: 由有效用例得到期望裁决列表。
def _expected(vectors):
    return [{"allowed": item["allowed"], "code": item["code"]} for item in vectors["valid_cases"]]


# 函数用途: 按 base/set/drop 生成一份无效上下文。
def _invalid_payload(vectors, item):
    payload = copy.deepcopy(vectors["contexts"][item["base"]])
    payload.update(item.get("set", {}))
    payload.pop(item.get("drop", ""), None)
    return payload


def test_python_reference_decides_every_conformance_case(tmp_path):
    _, vectors = _materialize(tmp_path)
    contexts = {name: WorkspaceReadContext.from_payload(payload) for name, payload in vectors["contexts"].items()}
    actual = []
    for item in vectors["valid_cases"]:
        decision = contexts[item["context"]].check(Path(item["path"]))
        actual.append({"allowed": decision.allowed, "code": decision.code})
    mismatches = [(case, want, got) for case, want, got in zip(vectors["valid_cases"], _expected(vectors), actual) if want != got]
    assert not mismatches
    for item in vectors["invalid_contexts"]:
        with pytest.raises(ValueError):
            WorkspaceReadContext.from_payload(_invalid_payload(vectors, item))


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_node_port_matches_every_conformance_case(tmp_path):
    root, vectors = _materialize(tmp_path)
    runner = SAMPLES / "hello-node" / "conformance.js"
    output = subprocess.run([NODE, str(runner), str(VECTORS), root], capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(output.stdout)
    mismatches = [(case, want, got) for case, want, got in zip(vectors["valid_cases"], _expected(vectors), result["valid"])
                  if want != got]
    assert len(result["valid"]) == len(vectors["valid_cases"]) and not mismatches
    assert result["invalid"] == [True] * len(vectors["invalid_contexts"])


# LLM: 样例真实环境/MCP 在明确 wide 下回归，确认走原管理链；不改产品默认，不只回填旧码。
# 函数用途: 安装样例并按完整命令/授权请求确认，返回完整预览供调用方核对嵌套运行事实。
def _install_and_confirm(tmp_path, package):
    service, _ = manager(tmp_path, legacy_sandbox_default=False)
    result = service.command(f'/plugins install "{package}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result
    plugin_id = service.installations.snapshot()[0].manifest.plugin_id
    first = preview_legacy_enable(service, plugin_id)
    enabled = confirm_legacy_enable(service, first)
    assert enabled["state"] == "succeeded", enabled
    entry = service.installations.snapshot()[0]
    assert entry.enabled and entry.activation.phase == "active"
    assert entry.activation_id == first["details"]["confirmation"]["activation_id"]
    return service, plugin_id, first["details"]["confirmation"]


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_node_sample_runs_through_host_and_honors_read_context(tmp_path):
    project = SAMPLES / "hello-node"
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, project, tmp_path / "hello-node.zip")
    service, plugin_id, confirmation = _install_and_confirm(tmp_path, package)
    assert confirmation["mode"] == "wide" and confirmation["runtime"]["interpreter"]["path"] == os.path.realpath(NODE)
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        greeting = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "hello"), {"name": "测试"},
                                          request_id="hello")
        assert greeting["state"] == "succeeded" and "来自 Node.js" in json.dumps(greeting, ensure_ascii=False), greeting
        (tmp_path / "notes.txt").write_text("工作区里的笔记", encoding="utf-8")
        read = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "read_text"), {"path": "notes.txt"},
                                      request_id="read")
        assert read["state"] == "succeeded" and "工作区里的笔记" in json.dumps(read, ensure_ascii=False), read
        outside = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "read_text"),
                                         {"path": str(ROOT / "README.md")}, request_id="outside")
        assert "PATH_READ_SCOPE_BLOCKED" in json.dumps(outside, ensure_ascii=False), outside
    finally:
        registry.close_mcp_clients()


@pytest.mark.skipif(GO is None, reason="本机没有 go")
def test_go_sample_builds_and_runs_through_host(tmp_path):
    project = SAMPLES / "hello-go"
    staging = tmp_path / "build"
    environment = {**os.environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod"}
    subprocess.run([GO, "build", "-trimpath", "-o", str(staging / "bin" / "hello-go"), "."], cwd=project, env=environment,
                   capture_output=True, text=True, timeout=300, check=True)
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, staging, tmp_path / "hello-go.zip", (host_platform_tag(),))
    service, plugin_id, confirmation = _install_and_confirm(tmp_path, package)
    assert confirmation["mode"] == "wide"
    assert confirmation["runtime"]["platform"] == host_platform_tag() and confirmation["runtime"]["files"][0]["executable"] is True
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        greeting = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "hello"), {"name": "测试"},
                                          request_id="hello")
        assert greeting["state"] == "succeeded" and "来自 Go 程序" in json.dumps(greeting, ensure_ascii=False), greeting
    finally:
        registry.close_mcp_clients()


# ---- M1 B8 样例插件直接协议合同 ----

# LLM: 最小假宿主：一行一帧 JSON-RPC，只做协议对话，不模拟宿主审批、合并或沙箱；stdout 同步读一行。
# 类用途: 与插件进程的 stdin/stdout 逐条对话。
class _FakeHost:
    # 函数用途: 绑定一个已启动的插件进程。
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._seq = 0

    # 函数用途: 发一条请求并读取一行响应。
    def call(self, method: str, params: dict | None = None) -> dict:
        self._seq += 1
        frame: dict = {"jsonrpc": "2.0", "id": self._seq, "method": method}
        if params is not None:
            frame["params"] = params
        self._write(frame)
        line = self._process.stdout.readline()
        assert line, f"插件没有响应：{self._process.stderr.read()}"
        return json.loads(line)

    # 函数用途: 发一条通知（没有 id），宿主侧不等待响应。
    def notify(self, method: str, params: dict | None = None) -> None:
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._write(frame)

    # 函数用途: 关闭输入并等待进程自然结束，退出码非零即失败。
    def close(self) -> None:
        self._process.stdin.close()
        assert self._process.wait(timeout=15) == 0

    # 函数用途: 写一帧并刷新。
    def _write(self, frame: dict) -> None:
        self._process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self._process.stdin.flush()


# 函数用途: 按样例目录启动 Python 插件进程（当前解释器运行随包脚本）。
def _start_python(project: Path) -> _FakeHost:
    process = subprocess.Popen([sys.executable, "src/server.py"], cwd=project, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    return _FakeHost(process)


# 函数用途: 按样例目录启动 Node 插件进程（PATH 上的 node 运行随包脚本）。
def _start_node(project: Path) -> _FakeHost:
    process = subprocess.Popen([NODE, "src/server.js"], cwd=project, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    return _FakeHost(process)


# 函数用途: 用仓库构建脚本把样例目录打成 v8 包并返回读包结果。
def _build_sample(tmp_path: Path, name: str):
    project = SAMPLES / name
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, project, tmp_path / f"{name}.zip")
    return inspect_plugin_package(package.read_bytes())


# 函数用途: 构造一条最小观察事件（字段取第 6 节公共字段）。
def _event(name: str, index: int, dropped: object) -> dict:
    return {"event_id": f"ev-{index}", "type": name, "seq": index, "occurred_at": 1759480000.0,
            "dropped_before": dropped, "channel": "tui", "thread_ref": "thread-ref", "actor": "main", "facts": {}}


# 函数用途: 把面板表格的行转成 {事件类型: (收到, 合并丢弃)}。
def _rows(table: dict) -> dict:
    return {row[0]: tuple(row[1:]) for row in table["rows"]}


# 函数用途: 构造一条收紧征询；arguments 为 None 时不带参数，arguments_truncated 为 None 时不带截断标记（模拟旧宿主）；
#   arguments_truncated 收 object 以便测试 1、"true" 这类真值（实现必须只认布尔 true）。
def _review_params(gate_id: str, tool: str, arguments: dict | None, *, arguments_truncated: object | None = None) -> dict:
    call = {"call_id": "call-1", "tool": tool, "effect": "dangerous", "actor": "main",
            "interactive": True, "args_hash": "hash-1"}
    if arguments is not None:
        call["arguments"] = arguments
    if arguments_truncated is not None:
        call["arguments_truncated"] = arguments_truncated
    return {"gate_id": gate_id, "call": call}


# LLM: 回复格式按 B5 合同：只有三种结果、原因码合法、消息可选且不超长、不含控制字符。
# 函数用途: 断言一条收紧回复符合第 8 节的格式。
def _assert_reply_shape(reply: dict) -> None:
    assert reply["verdict"] in _VERDICTS
    assert _REASON_CODE.fullmatch(reply["reason_code"])
    message = reply.get("message", "")
    assert isinstance(message, str) and len(message) <= 80
    assert all(ord(char) >= 32 for char in message)


# 函数用途: 对一组已启动的收紧样例跑完全部输入，断言裁决、格式和边界行为。
def _assert_review_behavior(host: _FakeHost) -> None:
    for command, verdict, reason in _REVIEW_CASES:
        reply = host.call("my-agent/tool-gate.review", _review_params("guard-rm", "run_command", {"command": command}))
        assert (reply["result"]["verdict"], reply["result"]["reason_code"]) == (verdict, reason), command
        _assert_reply_shape(reply["result"])
    for patch, verdict, reason in _DELETE_PATCH_CASES:
        reply = host.call("my-agent/tool-gate.review", _review_params("guard-delete", "apply_patch", {"patch": patch}))
        assert (reply["result"]["verdict"], reply["result"]["reason_code"]) == (verdict, reason), patch
        _assert_reply_shape(reply["result"])
    for payload in (None, {"note": "没有补丁字段"}, {"patch": 7}):
        reply = host.call("my-agent/tool-gate.review", _review_params("guard-delete", "apply_patch", payload))["result"]
        assert (reply["verdict"], reply["reason_code"]) == ("ask", "ARGUMENTS_UNAVAILABLE"), payload
        _assert_reply_shape(reply)
    old_tool = host.call("my-agent/tool-gate.review", _review_params("guard-delete", "delete_file", None))["result"]
    assert (old_tool["verdict"], old_tool["reason_code"]) == ("deny", "OUT_OF_SCOPE")
    missing = host.call("my-agent/tool-gate.review", _review_params("guard-rm", "run_command", None))["result"]
    assert (missing["verdict"], missing["reason_code"]) == ("ask", "ARGUMENTS_UNAVAILABLE")
    outside = host.call("my-agent/tool-gate.review", _review_params("guard-rm", "write_file", {"path": "x"}))
    assert (outside["result"]["verdict"], outside["result"]["reason_code"]) == ("deny", "OUT_OF_SCOPE")
    extra = _review_params("guard-rm", "run_command", {"command": "rm -rf x", "note": "附加字段"})
    extra["ignored"] = True
    extra["call"]["ignored"] = 1
    reply = host.call("my-agent/tool-gate.review", extra)["result"]
    assert (reply["verdict"], reply["reason_code"]) == ("ask", "RM_RF")
    _assert_truncation_behavior(host)


# LLM: B8 样例的截断合同验收，与 B5 的 tool_gate 语义同源（trs/b8tr 定）：标记只认布尔 true——false、缺失（旧宿主）
#   和 1/"true" 这类真值一律照原判定走；true 时只能更严——片段已 deny 保持 deny（原原因码，如 guard-delete 的
#   DELETE_FILE_BLOCKED）、片段本来就 ask 保留原原因码并把消息补成"…；参数还被截断了"、只有片段可放行才升到
#   ask + ARGUMENTS_TRUNCATED。每条都同时校验回复形状（裁决枚举、原因码格式、消息长度与控制字符）和两条消息文案。
#   改动同步：样例插件 {rm-guard,rm-guard-node} 的 reviewGate、_TRUNCATION_CASES 表、_TRUNCATED_MESSAGE/
#   _TRUNCATED_NOTE 文案，以及技能模板 templates/{python,node} 的同名写法（test_write_my_agent_plugin_skill.py 覆盖后者）。
# 函数用途: 断言截断只能更严——片段已 deny 保持、ask 保留原原因码、可放行才升 ask；false/缺失/非布尔真值走原判定。
def _assert_truncation_behavior(host: _FakeHost) -> None:
    for gate_id, tool, payload, plain, truncated in _TRUNCATION_CASES:
        missing = host.call("my-agent/tool-gate.review", _review_params(gate_id, tool, payload))["result"]
        assert (missing["verdict"], missing["reason_code"]) == plain, payload
        explicit_false = host.call("my-agent/tool-gate.review",
                                   _review_params(gate_id, tool, payload, arguments_truncated=False))["result"]
        assert (explicit_false["verdict"], explicit_false["reason_code"]) == plain, payload
        flagged = host.call("my-agent/tool-gate.review",
                            _review_params(gate_id, tool, payload, arguments_truncated=True))["result"]
        assert (flagged["verdict"], flagged["reason_code"]) == truncated, payload
        _assert_reply_shape(flagged)
        _assert_truncation_message(flagged, truncated, payload)
        _assert_non_boolean_flags(host, (gate_id, tool, payload, plain))


# 函数用途: 断言截断时消息——可放行升 ask 用统一文案；保留原原因码的 ask 以"参数还被截断了"收尾。
def _assert_truncation_message(flagged: dict, truncated: tuple, payload: dict) -> None:
    if truncated == ("ask", "ARGUMENTS_TRUNCATED"):
        assert flagged["message"] == _TRUNCATED_MESSAGE, payload
    elif truncated[0] == "ask":
        assert flagged["message"].endswith(_TRUNCATED_NOTE), payload


# 函数用途: 断言 1、"true" 这类非布尔真值不被当成截断标记，照原判定走（标记只认布尔 true）。
def _assert_non_boolean_flags(host: _FakeHost, case: tuple) -> None:
    gate_id, tool, payload, plain = case[:4]
    for odd in (1, "true"):
        odd_reply = host.call("my-agent/tool-gate.review",
                              _review_params(gate_id, tool, payload, arguments_truncated=odd))["result"]
        assert (odd_reply["verdict"], odd_reply["reason_code"]) == plain, (payload, odd)


def test_three_samples_build_and_pass_v8_validation(tmp_path):
    watch = _build_sample(tmp_path, "event-watch").manifest
    assert watch.to_payload()["schema_version"] == "plugin_package.v8"
    assert [event.type for event in watch.events] == list(_EVENT_TYPES)
    assert all(event.content == "none" for event in watch.events)
    assert watch.tool_gates == () and watch.permissions.network is False
    assert [panel.id for panel in watch.panels] == ["watch"]
    for name in ("rm-guard", "rm-guard-node"):
        manifest = _build_sample(tmp_path, name).manifest
        assert manifest.to_payload()["tool_gates"] == [
            {"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "full"},
            {"id": "guard-delete", "tools": ["apply_patch"], "effects": [], "arguments": "full"},
        ]
        assert manifest.events == () and manifest.permissions.network is False
        assert manifest.entry is not None and manifest.entry.kind == "interpreter"


def test_event_watch_counts_all_six_event_types_and_dropped(tmp_path):
    host = _start_python(SAMPLES / "event-watch")
    try:
        experimental = host.call("initialize", {})["result"]["capabilities"]["experimental"]
        # 精确比较能力位集合：多声明一个没实现的能力位也要被抓到（b8t 初审补强）。
        assert set(experimental) == {"my-agent/events", "my-agent/display"}
        assert experimental["my-agent/events"] == {"versions": ["1"]}
        assert experimental["my-agent/display"] == {"versions": ["1"]}
        first = [_event(name, index, dropped) for index, (name, dropped) in
                 enumerate(zip(_EVENT_TYPES, (0, 0, 0, 3, 0, 1)), 1)]
        assert host.call("my-agent/events.observe", {"events": first})["result"] == {}
        second = [_event("prompt_submitted", 7, 5),
                  {**_event("prompt_submitted", 8, 0), "content": "内部正文不该出现在面板"},
                  _event("future_event_2027", 9, 2), {**_event("turn_started", 10, 0), "extra": {"nested": [1]}},
                  _event("command_executed", 11, "9")]
        assert host.call("my-agent/events.observe", {"events": second})["result"] == {}
        table = host.call("my-agent/display.render", {"panel": "watch", "topics": {}})["result"]["display"]
        assert table["columns"] == ["事件类型", "收到", "合并丢弃"]
        assert set(table) == {"columns", "rows"}
        assert "内部正文" not in json.dumps(table, ensure_ascii=False)
        assert _rows(table) == {
            "prompt_submitted": (3, 5), "turn_started": (2, 0), "turn_ended": (1, 0),
            "tool_call_started": (1, 3), "tool_call_finished": (1, 0), "command_executed": (2, 1), "其它": (1, 2),
        }
        again = host.call("my-agent/display.render", {"panel": "watch", "topics": {}})["result"]["display"]
        assert again == table
        host.call("my-agent/events.observe", {"events": [_event("prompt_submitted", 12, 0)]})
        after = host.call("my-agent/display.render", {"panel": "watch", "topics": {}})["result"]["display"]
        assert _rows(after)["prompt_submitted"] == (4, 5)
        host.notify("my-agent/events.observe", {"events": [_event("turn_ended", 13, 0)]})
        assert host.call("ping", {})["result"] == {}
    finally:
        host.close()


def test_rm_guard_reviews_rm_combinations_and_delete(tmp_path):
    host = _start_python(SAMPLES / "rm-guard")
    try:
        experimental = host.call("initialize", {})["result"]["capabilities"]["experimental"]
        # 精确比较能力位集合：多声明一个没实现的能力位也要被抓到（b8t 初审补强）。
        assert set(experimental) == {"my-agent/tool-gate"}
        assert experimental["my-agent/tool-gate"] == {"versions": ["1"]}
        _assert_review_behavior(host)
    finally:
        host.close()


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_rm_guard_node_matches_python_behavior(tmp_path):
    host = _start_node(SAMPLES / "rm-guard-node")
    try:
        experimental = host.call("initialize", {})["result"]["capabilities"]["experimental"]
        # 精确比较能力位集合：多声明一个没实现的能力位也要被抓到（b8t 初审补强）。
        assert set(experimental) == {"my-agent/tool-gate"}
        assert experimental["my-agent/tool-gate"] == {"versions": ["1"]}
        _assert_review_behavior(host)
    finally:
        host.close()


# ---- M1 联合用例：真实样例 stdio + Gateway 请求路径 + 观察/展示 ----

# LLM: 仅适配真实样例 stdio；不伪造握手或计数；帧限额与超时有界，EOF 自然退出，不 kill/ps/嵌套沙箱。
# 类用途: 让 B2 池可和临时样例进程对话；不代替产品 PluginMCPClient 的启用与资源管理验收。
class _SampleClient:
    # LLM: 构造不启动进程，激活只是测试冻结记录；不得把它写入真实安装表。
    # 函数用途: 保存临时样例与协议往返记录，供池和计数断言使用。
    def __init__(self, project, installation):
        self.project = project
        self.installation = installation
        self.activation_ref = SimpleNamespace(require=lambda: installation)
        self.capabilities = {}
        self.calls = []
        self.responses = []
        self.process = None
        self.identifier = 0
        self.buffer = b''

    # LLM: 只启动 tmp_path 样例；用生产 MCPStdioClient._handshake 核协议，不走产品安装/沙箱启动。
    # 函数用途: 建立真实 stdio 连接；握手数据从样例进程原样返回，不手工组能力。
    def start(self):
        entry = self.installation.manifest.entry
        executable = sys.executable if entry.interpreter == 'python3' else shutil.which(entry.interpreter)
        assert executable, entry.interpreter
        self.process = subprocess.Popen([executable, entry.command, *entry.args], cwd=self.project,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'HOME': str(self.project), 'PYTHONDONTWRITEBYTECODE': '1'})
        handshake = MCPStdioClient(MCPServerConfig(name=self.installation.manifest.plugin_id,
            command=executable, connect_timeout=3))
        handshake._handshake(self)
        assert self.server_info['name'] == self.installation.manifest.plugin_id
        return self

    # LLM: 握手通知沿刚初始化的同一管道发送，通知不等待响应，不产工具或审批事件。
    # 函数用途: 让生产握手发送 initialized 帧，不复制宿主的握手裁定。
    def notify(self, method, params):
        frame = {'jsonrpc': '2.0', 'method': method, 'params': params}
        self.process.stdin.write((json.dumps(frame) + '\n').encode())
        self.process.stdin.flush()

    # LLM: before_send 由真池调用；此处仍执行原代次复查，用宿主协议解包真实响应，记录的是发出的结构化帧。
    # 函数用途: 在调用方给定期限内完成一帧请求；能力位或方法拼错不能靠假响应蒙混通过。
    def request(self, method, params, **options):
        authority_check = options.get('authority_check')
        if authority_check is not None:
            authority_check()
        self.identifier += 1
        frame = {'jsonrpc': '2.0', 'id': self.identifier, 'method': method, 'params': params}
        self.process.stdin.write((json.dumps(frame) + '\n').encode())
        self.process.stdin.flush()
        self.calls.append((method, json.loads(json.dumps(params))))
        response = self._response(time.monotonic() + options['timeout'])
        assert response['jsonrpc'] == '2.0' and response['id'] == self.identifier
        result = unwrap_jsonrpc(self.installation.manifest.plugin_id, response, method)
        self.responses.append((method, json.loads(json.dumps(result))))
        return result

    # LLM: fd 的读取有界，不做无限 readline；输入错误留给原协议解包或明确断言。
    # 函数用途: 在期限内收取真实样例的一行 JSON，超时或提前 EOF 直接失败。
    def _response(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                assert selector.select(max(0, deadline - time.monotonic())), '样例 stdio 响应超时'
                data = os.read(self.process.stdout.fileno(), 131072)
                assert data, '样例 stdio 提前 EOF'
                self.buffer += data
                assert len(self.buffer) <= 131072
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    # LLM: EOF 自然退出后才置空句柄，池吞清理异常时 fixture 仍能发现未确认；不发终止信号。
    # 函数用途: 关闭本测试 stdin 并核退出码与 stderr，重复关闭已确认对象无副作用。
    def stop(self):
        if self.process is None:
            return
        process = self.process
        if not process.stdin.closed:
            process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b''
        process.stdout.close()
        process.stderr.close()
        self.process = None


# LLM: 复制只在 pytest tmp_path 内；默认字节来自真实样例，不修改仓库样例。
#   B2 按 owner/activation_id 复用连接，不同插件的临时激活必须唯一，不能让收紧连接冒充观察连接。
# 函数用途: 用 B1 实际构建/读包校验恢复 manifest；冻结每个样例独立的临时激活供共用池复核。
def _sample(tmp_path, name):
    project = tmp_path / name
    shutil.copytree(SAMPLES / name, project)
    declaration = json.loads((project / 'declaration.json').read_text())
    package = build_files_package(declaration, project, tmp_path / f'{name}.zip')
    manifest = inspect_plugin_package(package.read_bytes()).manifest
    assert manifest.to_payload()['schema_version'] == 'plugin_package.v8'
    row = SimpleNamespace(manifest=manifest,
        activation=SimpleNamespace(activation_id=f'joint-{manifest.plugin_id}-activation'), enabled=True)
    return project, row


@pytest.fixture
def watch(tmp_path):
    project, row = _sample(tmp_path, 'event-watch')
    clients = []
    projects = {row.manifest.plugin_id: project}

    def create(_owner, installation):
        client = _SampleClient(projects[installation.manifest.plugin_id], installation)
        clients.append(client)
        return client

    pool = PluginChannelPool(client_factory=create)
    executor = _ManualExecutor()
    hub = PluginEventHub(wiring=EventHubWiring(installations=lambda _owner: (row,), executor=executor, pool=pool))
    display_jobs = _ManualExecutor()
    display = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: (row,),
        executor=display_jobs, pool=pool))
    bundle = SimpleNamespace(project=project, row=row, projects=projects, clients=clients, pool=pool, hub=hub,
        executor=executor, display=display, display_jobs=display_jobs)
    try:
        yield bundle
    finally:
        display.close()
        hub.close()
        pool.close()
        # 池会吞 stop 异常；由测试单独核自然退出，不能把未清理误写为成功。
        assert all(client.process is None for client in clients)


def _wire_gateway(gateway, watch, monkeypatch):
    agent, _paths, server, _observed = gateway
    monkeypatch.setattr(type(agent.config), 'plugin_events_enabled', True, raising=False)
    server.plugin_event_hub = watch.hub
    server.plugin_channel_pool = watch.pool
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', _PUBLISH)
    return _prepare_case(agent, monkeypatch, 'normal')[0]


# LLM: 不调用真实 request worker/claim；入队后手动复制为 processing，不能将此 helper 的结果外推到自动队列消费。
# 函数用途: 运行隔离 handler 与原执行路径，让真实样例收到事件；只用于联合组件测试。
def _normal_turn(gateway, tool):
    agent, paths, server, _observed = gateway
    handler = _Handler({'prompt': 'prompt-private-marker', 'conversation_id': 'joint-thread'})
    http_handlers.handle_ask(handler, server, lambda: 'joint-request')
    assert handler.replies[-1][0] == 202
    request_id = handler.replies[-1][1]['request_id']
    request = json.loads((paths.inbox / f'{request_id}.json').read_text())
    request.update(status='processing', turn_phase='open', execution_attempt_id='joint-attempt')
    processing = paths.processing / f'{request_id}.json'
    processing.write_text(json.dumps(request))
    result = _handle_gateway_request(agent, processing)
    assert result['ok'], result
    assert tool.executions == 1 and agent.backend.calls == 2
    _control(gateway, 'joint-control')


def _control(gateway, message_id):
    agent, paths, _server, _observed = gateway
    command = parse_conversation_control('/verbose on', reject_unknown_slash=True)
    receipt = execute_gateway_control_operation(agent, paths, command,
        GatewayControlScope(user_id='admin', channel='chat', conversation_id='joint-thread',
            metadata={'message_id': message_id}), command_text='/verbose on')
    assert receipt.state == 'completed'
    return receipt


def _owner(gateway):
    return SimpleNamespace(home_dir=gateway[0].home_paths.owner_home_dir)


def _panel(watch, owner):
    # 每次用新观察线程键绕过展示缓存，证明实际 render 只读，而非两次读了同一缓存。
    query = PanelQuery(owner, str(owner.home_dir), f'view-{len(watch.clients[0].calls)}', {},
        ((watch.row.manifest.plugin_id, watch.row.manifest.panels[0].id),))
    watch.display.panels(query)
    watch.display_jobs.run_all()
    panel = watch.display.panels(query)[0]
    assert panel['state'] == 'ready', panel
    method, raw = watch.clients[0].responses[-1]
    assert method == 'my-agent/display.render' and set(raw) == {'display'}
    assert set(raw['display']) == {'columns', 'rows'}
    assert all(len(row) == 3 and type(row[1]) is int and type(row[2]) is int for row in raw['display']['rows'])
    assert set(panel['display']) == {'kind', 'columns', 'rows', 'truncated'}
    assert panel['display']['kind'] == 'table' and panel['display']['truncated'] is False
    return panel['display']


def _counts(table):
    assert len(table['columns']) == 3
    assert len(table['rows']) == len(PLUGIN_EVENT_TYPES)
    return {row[0]: (int(row[1]), int(row[2])) for row in table['rows']}


def _delivered(watch):
    assert len(watch.clients) == 1
    return [event for method, params in watch.clients[0].calls
        if method == 'my-agent/events.observe' for event in params['events']]


def _assert_counts(watch, owner, events):
    received = Counter(event['type'] for event in events)
    dropped = Counter()
    for event in events:
        dropped[event['type']] += event['dropped_before']
    expected = {name: (received[name], dropped[name]) for name in PLUGIN_EVENT_TYPES}
    assert _counts(_panel(watch, owner)) == expected
    stats = watch.hub.stats(str(owner.home_dir))[watch.row.manifest.plugin_id]
    assert set(stats) == set(PLUGIN_EVENT_TYPES)
    for name, counts in expected.items():
        assert (stats[name]['delivered'], stats[name]['coalesced']) == counts
        assert stats[name]['failed'] == stats[name]['unavailable'] == 0


def _files(project):
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project.rglob('*') if path.is_file()}


def test_event_watch_real_gateway_six_counts_and_readonly(gateway, watch, monkeypatch):
    tool = _wire_gateway(gateway, watch, monkeypatch)
    before_files = _files(watch.project)
    _normal_turn(gateway, tool)
    watch.executor.run_all()
    client = watch.clients[0]
    assert client.capabilities['experimental'] == {
        'my-agent/events': {'versions': ['1']}, 'my-agent/display': {'versions': ['1']}}
    events = _delivered(watch)
    assert Counter(event['type'] for event in events) == Counter(dict.fromkeys(PLUGIN_EVENT_TYPES, 1))
    public = {'event_id', 'type', 'seq', 'occurred_at', 'dropped_before', 'channel', 'thread_ref',
              'channel_conversation_ref', 'actor', 'facts'}
    assert all(set(event) == public for event in events)
    assert all(set(event['facts']) == _FIELDS[event['type']] for event in events)
    assert all(event['dropped_before'] == 0 for event in events)
    assert 'private-marker' not in json.dumps(events)
    owner = _owner(gateway)
    _assert_counts(watch, owner, events)
    before_stats = watch.hub.stats(str(owner.home_dir))
    first = _panel(watch, owner)
    first['rows'][0][1] = '999'
    assert _counts(_panel(watch, owner)) == dict.fromkeys(PLUGIN_EVENT_TYPES, (1, 0))
    assert watch.hub.stats(str(owner.home_dir)) == before_stats
    assert _files(watch.project) == before_files
    assert tool.executions == 1
    assert sum(method == 'my-agent/display.render' for method, _ in client.calls) == 3
    assert len(watch.pool.connections) == 1


def test_event_watch_counts_hub_generated_dropped_before(gateway, watch, monkeypatch):
    tool = _wire_gateway(gateway, watch, monkeypatch)
    _normal_turn(gateway, tool)
    watch.executor.run_all()  # 先建立真实槽；启用前的历史不能冒充 dropped_before。
    receipts = [_control(gateway, f'coalesce-{index}') for index in range(3)]
    assert len({receipt.operation_id for receipt in receipts}) == 3
    watch.executor.run_all()
    events = _delivered(watch)
    commands = [event for event in events if event['type'] == 'command_executed']
    assert [event['dropped_before'] for event in commands] == [0, 2]
    assert commands[-1]['facts']['operation_id'] == receipts[-1].operation_id
    _assert_counts(watch, _owner(gateway), events)
    assert _counts(_panel(watch, _owner(gateway)))['command_executed'] == (2, 2)


def test_rm_guard_handshake_and_b1_manifest_only(tmp_path, gateway):
    project, row = _sample(tmp_path, 'rm-guard')
    assert row.manifest.to_payload()['tool_gates'] == json.loads((project / 'declaration.json').read_text())['tool_gates']
    gate = next(gate for gate in row.manifest.tool_gates if gate.id == 'guard-rm')
    assert (gate.tools, gate.effects, gate.arguments) == (('run_command',), (), 'full')
    assert 'run_command' in gateway[0].tools.tools
    client = _SampleClient(project, row)
    try:
        client.start()
        assert client.capabilities['experimental'] == {'my-agent/tool-gate': {'versions': ['1']}}
    finally:
        client.stop()


def test_node_event_watch_handshake_when_sample_exists(tmp_path):
    if shutil.which('node') is None:
        pytest.skip('本机没有 node')
    if not (SAMPLES / 'event-watch-node' / 'declaration.json').is_file():
        pytest.skip('仓库没有 Node event-watch 样例；rm-guard-node 不能冒充事件观察样例')
    project, row = _sample(tmp_path, 'event-watch-node')
    client = _SampleClient(project, row)
    try:
        client.start()
        assert client.capabilities['experimental'] == {
            'my-agent/events': {'versions': ['1']}, 'my-agent/display': {'versions': ['1']}}
    finally:
        client.stop()


# ---- B5 执行器联合链路：真实 rm-guard 征询、审批桥、归档与 /plugins info ----

# LLM: 用相同工具名/字符串 schema 进入真实执行器，保留宿主先验裁决；探针没有 Shell/删除副作用，避免反证真的删数据。
# 类用途: 只数 handler 进入次数，严格证明前置拒绝时是零执行，而不是执行后报错。
class _GuardCounter(BaseTool):
    # LLM: 只在隔离注册表装配计数工具，保留原宿主裁决，不获得真实 Shell 或文件删除能力。
    # 函数用途: 按原工具名构造没有业务副作用的 handler 探针。
    def __init__(self, name, field):
        self.executions = 0
        self.model_spec = ToolModelSpec(name, '隔离计数探针，不执行命令或删文件', {
            'type': 'object', 'properties': {field: {'type': 'string'}},
            'required': [field], 'additionalProperties': False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(default_effect='mutating'),
            approval_policy=ApprovalPolicy('never'), idempotency_policy=IdempotencyPolicy('operation'))

    # LLM: 进入次数只证明 handler 开始；返回值沿真实归档链记录，不执行参数里的命令或补丁。
    # 函数用途: 记录唯一执行器是否真的进入 handler。
    def execute(self, arguments):
        self.executions += 1
        return ToolHandlerOutcome(self.model_spec.name, True, 'counted')


# LLM: 联合结果固定成小对象，所有身份来自原执行路径；不手工编审批或写 runtime_events。
# 类用途: 把一次真实回合的执行回执、用户事件和实际征询帧带回断言。
@dataclass(frozen=True)
class _Case:
    execution: object
    permissions: list
    request: dict
    reply: dict


# LLM: 一次联合回合只在这四项上不同：跑哪个工具、什么参数、用户怎么答、客户端有没有声明可交互审批。
#   打包成小对象是为了不把参数堆到执行函数上，也避免每个出口各写一份几乎一样的驱动代码。
#   四项都是结构化事实（工具名/参数/决定词/能力布尔），不从文案或模型输出推断。
# 类用途: 描述一次真实工具回合要跑什么、用户如何作答。
@dataclass(frozen=True)
class _RunSpec:
    name: str
    arguments: dict
    decision: str = 'denied'
    interactive: bool = True


# LLM: 当前17j的B5字段是必备合同，缺失就失败；安装表/启动适配不代表产品启用，不给数据接口加生产旁路。
# 函数用途: 将原样 rm-guard 与观察样例接到同一个 B2 池，使用本 pytest 临时 owner。
@pytest.fixture
def guard_joint(tmp_path, gateway, watch, monkeypatch):
    assert 'plugin_gate_reviewer' in ToolExecutorRequest.__dataclass_fields__, '真实17j缺少B5执行器接线'
    from agent_py_agent.agent.plugin_events import tool_gate_review

    agent, _paths, server, _events = gateway
    project, row = _sample(tmp_path, 'rm-guard')
    watch.projects[row.manifest.plugin_id] = project
    monkeypatch.setattr(type(agent.config), 'plugin_events_enabled', True, raising=False)
    server.plugin_event_hub, server.plugin_channel_pool = watch.hub, watch.pool
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', _PUBLISH)
    monkeypatch.setattr(tool_gate_review, 'enabled_gate_installations', lambda _owner: (row,))
    agent.tools.plugin_gate_reviewer = core._build_plugin_gate_reviewer(agent, agent.config)
    assert callable(agent.tools.plugin_gate_reviewer)
    agent.tools.approval_mode_reader = lambda: 'auto'
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    yield SimpleNamespace(gateway=gateway, watch=watch, row=row, owner=owner, project=project)


# LLM: 探针原样转交生产归档函数；不能造 ToolExecution 或补写丢失的门决定来让联合测试变绿。
# 函数用途: 收集真实工具轮的 canonical 回执。
def _capture_executions(monkeypatch):
    recorded = []
    original = round_execution._record_execution

    # LLM: 原样转交归档参数和返回值，不手造成功回执或门决定。
    # 函数用途: 记录真实工具轮已产生的执行对象，随后继续原归档。
    def capture(*args):
        recorded.append(args[-1])
        return original(*args)

    monkeypatch.setattr(round_execution, '_record_execution', capture)
    return recorded


# LLM: 只换"用户选了哪个选项"这一处；发布、等待、binding 核验、执行器、归档一律走 e2e 那条真实路径，
#   所以批准、拒绝、不可用的差别只在宿主写进决策文件的那一个决定词，不会引入第二套审批通路。
# 函数用途: 按 spec.decision 让真实审批链收到批准、拒绝或不可用。
def _reply_with_user_decision(monkeypatch, client_events, decision):
    if decision == 'denied':
        _reply_with_user_denial(monkeypatch, client_events)
        return
    original = _event_e2e.write_gateway_permission_decision

    # 函数用途: 把 e2e 助手写下的拒绝换成 spec 指定的决定词，其余参数原样透传。
    def decide(chunk_path, request, _decision):
        return original(chunk_path, request, ToolApprovalDecision(request.permission_id, decision))

    monkeypatch.setattr(_event_e2e, 'write_gateway_permission_decision', decide)
    _reply_with_user_denial(monkeypatch, client_events)


# LLM: handler 与 execute_one/审批/归档不替换；只在客户端写决定文件，inbox 到 processing 明确手动推进。
#   批准时 handler 会真的进入（计数探针），拒绝与无法审批时必须是零执行——这两个期望值都来自 spec，不是放宽带过。
# 函数用途: 用离线模型提交一个真实工具回合，读回真实插件响应和 canonical 结果。
def _run_case(bundle, monkeypatch, spec):
    agent, paths, server, _events = bundle.gateway
    tool = _GuardCounter(spec.name, next(iter(spec.arguments)))
    # 仅在隔离注册表的装配期换成无副作用 handler，随后仍经原 register 校验与冻结。
    monkeypatch.delitem(agent.tools.tools, spec.name)
    agent.tools.register(tool)
    agent.backend = _Backend(spec.name, spec.arguments)
    recorded, permissions = _capture_executions(monkeypatch), []
    _reply_with_user_decision(monkeypatch, permissions, spec.decision)
    # 同一个 owner 里跑第二轮时客户端会累积帧；先记基线，只断言本轮新增的那一次征询。
    before_sent, before_replies = _review_frame_counts(bundle)
    # 不声明 tool_approval 就是宿主事实上的"这里没人能当场审批"（与 TUI/已绑定管理员 IM 之外一致）。
    capabilities = {'tool_approval': True} if spec.interactive else {}
    handler = _Handler({'prompt': 'joint-gate-private-marker', 'conversation_id': 'joint-gate-thread',
                        'client_capabilities': capabilities})
    http_handlers.handle_ask(handler, server, lambda: 'joint-gate-request')
    assert handler.replies[-1][0] == 202
    request_id = handler.replies[-1][1]['request_id']
    queued = json.loads((paths.inbox / f'{request_id}.json').read_text())
    queued.update(status='processing', turn_phase='open', execution_attempt_id='joint-gate-attempt')
    processing = paths.processing / f'{request_id}.json'
    processing.write_text(json.dumps(queued))
    response = _handle_gateway_request(agent, processing)
    # 直接调请求处理函数不经 worker 的终态迁移：按产品同一个收尾函数结束原请求的会话执行车道，否则同会话下一轮要等租约约
    # 90 秒过期才领得到（GitHub 上按 60 秒超时判失败；CI 修复，2026-10-07）。
    _finish_gateway_conversation_claim(json.loads(processing.read_text()), request_id, agent.conversation_store)
    assert response['ok'], response
    ran = spec.decision == 'approved' and spec.interactive
    assert tool.executions == int(ran) and agent.backend.calls == 2
    assert len(recorded) == 1
    execution = recorded[0]
    assert execution.call.arguments == spec.arguments
    assert execution.result.handler_executed is ran and execution.result.ok is ran
    bundle.watch.executor.run_all()
    clients = [client for client in bundle.watch.clients if client.installation.manifest.plugin_id == 'rm-guard']
    assert len(clients) == 1, {'error_code': execution.result.error_code,
                               'reason_codes': execution.decision.reason_codes}
    client = clients[0]
    sent, replies = _review_frames(client)
    sent, replies = sent[before_sent:], replies[before_replies:]
    assert len(sent) == len(replies) == 1
    return _Case(execution, permissions, sent[0], replies[0])


# LLM: 客户端跨轮累积帧，断言"这一轮恰好一次征询"必须减掉本轮之前的数量；身份只按协议方法名筛。
# 函数用途: 取出一个插件客户端收到的（征询请求, 征询应答）帧。
def _review_frames(client):
    sent = [value for method, value in client.calls if method == 'my-agent/tool-gate.review']
    replies = [value for method, value in client.responses if method == 'my-agent/tool-gate.review']
    return sent, replies


# 函数用途: 取当前 rm-guard 客户端已有的征询帧数，作为本轮增量断言的基线（没有客户端时是 0）。
def _review_frame_counts(bundle):
    client = next((item for item in bundle.watch.clients
                   if item.installation.manifest.plugin_id == 'rm-guard'), None)
    if client is None:
        return 0, 0
    sent, replies = _review_frames(client)
    return len(sent), len(replies)


# LLM: 只从event-watch目标客户端读取真实B3帧，不能用另一个插件的事件凑正向集合；未进入handler不能有工具事件。
# 函数用途: 核对本次隔离回合在目标插件留下三个生命周期事件，而没有工具开始或完成事件。
def _assert_no_tool_events(bundle):
    clients = [client for client in bundle.watch.clients
               if client.installation.manifest.plugin_id == bundle.watch.row.manifest.plugin_id]
    assert len(clients) == 1
    events = [event for method, params in clients[0].calls
              if method == 'my-agent/events.observe' for event in params['events']]
    assert len(events) == 3 and len({event['thread_ref'] for event in events}) == 1
    assert {event['type'] for event in events} == {'prompt_submitted', 'turn_started', 'turn_ended'}, (
        bundle.watch.hub.stats(str(bundle.owner.home_dir)))
    assert not any(event['type'] in {'tool_call_started', 'tool_call_finished'} for event in events)
    _assert_distinct_connections(bundle)


# LLM: B2的连接身份是owner/activation_id；两个插件必须各自握手并收到各自协议，不能用第二池避开碰撞。
# 函数用途: 从真实共用池和客户端帧证明观察与收紧连接分别归属两个激活。
def _assert_distinct_connections(bundle):
    rows = (bundle.row, bundle.watch.row)
    expected = {(str(bundle.owner.home_dir), row.activation.activation_id) for row in rows}
    assert len(expected) == 2
    assert set(bundle.watch.pool.connections) == expected
    assert {client.installation.manifest.plugin_id for client in bundle.watch.clients} == {
        row.manifest.plugin_id for row in rows}
    assert len(bundle.watch.clients) == 2
    assert all(item['failed'] == item['unavailable'] == 0 for types in
        bundle.watch.hub.stats(str(bundle.owner.home_dir)).values() for item in types.values())


# LLM: runtime.db 已由真实回合创建；只读查询不能创建表或编行，门决定必须由产品归档链写入。
# 函数用途: 读本临时 owner 的实际 plugin_gate.decided 事件。
def _gate_rows(bundle):
    path = plugin_management.runtime_db_path(bundle.owner.home_dir)
    assert path.is_file()
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM runtime_events WHERE event_type='plugin_gate.decided' ORDER BY seq")]


# LLM: 只替换本实例的安装 snapshot，B6 命令解析/版本检查/查询/渲染均保留；不调用安装启用或写真实安装表。
# 函数用途: 用实际 /plugins info 从同一临时 runtime.db 读最近决定。
def _plugins_info(bundle):
    context = plugin_management.PluginManagementContext(
        owner=bundle.owner, actor_id='local-agent', channel='chat', conversation_id='joint-gate-thread',
        threads=None, workspace=bundle.owner.home_dir,
        path_policy=plugin_management.PathAccessPolicy.from_values(mode='normal'),
        is_admin=False, enabled=True, event_hub=bundle.watch.hub)
    service = plugin_management.PluginManagement(context)
    row = SimpleNamespace(**vars(bundle.row), revision=1, installation_ref='a' * 64,
                          activation_id=bundle.row.activation.activation_id)
    service.installations = SimpleNamespace(snapshot=lambda: (row,))
    catalog = service.catalog()
    reply = service.command('/plugins info rm-guard', revision=catalog.revision, request_id='joint-info')
    assert reply['ok'], reply
    return reply['message']


# LLM: 用真实发出的结构化 call 字段判传输与截断，不替换 PluginToolGate 投影或接受完整参数被改写。
#   interactive 是宿主事实：客户端声明了 tool_approval 才是 True；不可交互时插件应当看到 False。
# 函数用途: 核对门命中、真实调用身份、可交互事实和四千字预算。
def _assert_query(case, gate_id, truncated=False, interactive=True):
    facts = case.request['call']
    assert case.request['gate_id'] == gate_id
    assert set(facts) == {'call_id', 'tool', 'effect', 'actor', 'interactive', 'args_hash',
                          'arguments', 'arguments_truncated'}
    assert facts['tool'] == case.execution.call.tool_name
    assert facts['call_id'] == case.execution.call.call_id
    assert facts['args_hash'] == case.execution.call.args_hash
    assert (facts['effect'], facts['actor'], facts['interactive']) == ('mutating', 'main', interactive)
    assert facts['arguments_truncated'] is truncated
    assert len(json.dumps(facts['arguments'], ensure_ascii=False, separators=(',', ':'))) <= 4000


# LLM: 审批必须实际发布/消费原 binding，不能因 auto 或旧授权省去插件要求的本次确认。
# 函数用途: 核用户拒绝与仅本次选项，确保 handler 前被停住。
def _assert_confirmation_denied(case):
    assert case.execution.result.error_code == 'APPROVAL_REJECTED'
    assert case.execution.decision.status == 'deny'
    assert [event['kind'] for event in case.permissions] == ['permission_requested', 'permission_resolved']
    permission, resolved = case.permissions
    assert resolved['decision'] == 'denied'
    assert resolved['permission_id'] == permission['permission']['permission_id']
    assert [item['decision'] for item in permission['permission']['options']] == ['approved', 'denied']
    assert permission['permission']['options'][0]['label'] == '仅本次'
    reference = json.loads(permission['permission']['binding']['plugin_gate_ref'])
    assert reference['call_id'] == case.execution.call.call_id
    assert reference['args_hash'] == case.execution.call.args_hash


# LLM: 先真实查询 SQL 和 /plugins info，再检查同一次征询唯一行；不补写缺失事件、不把复制 B6 投影当真实命令读取。
#   b5fix9b 之后 ask 后无论批准、拒绝还是无法审批都必须留下恰好一行（此前 ask 后重建 canonical 结果会丢门决定），
#   所以这里不再有"空账本"的豁免分支：读到 0 行就是真实失败。
# 函数用途: 对齐归档写账、最近决定、原因码及安全白名单。
def _assert_ledger_and_info(bundle, case):
    rows, text = _gate_rows(bundle), _plugins_info(bundle)
    assert len(rows) == 1, {'gate_reply': case.reply, 'runtime_events': rows, 'plugins_info': text}
    payload = json.loads(rows[0]['payload_json'])
    assert set(payload) == _EXPECTED_FIELDS
    assert payload['plugin_id'] == bundle.row.manifest.plugin_id
    assert payload['version'] == bundle.row.manifest.version
    assert payload['activation_id'] == bundle.row.activation.activation_id
    assert payload['gate_id'] == case.request['gate_id']
    assert payload['call_id'] == case.execution.call.call_id
    assert payload['operation_id'] == case.execution.call.operation_id
    assert payload['args_hash'] == case.execution.call.args_hash
    assert (payload['tool'], payload['actor'], payload['outcome']) == (case.execution.call.tool_name, 'main', 'ok')
    assert (payload['verdict'], payload['reason_code'], payload['host_status'], payload['final_status']) == (
        case.reply['verdict'], case.reply['reason_code'], 'allow', case.reply['verdict'])
    assert rows[0]['attempt_id'] and rows[0]['agent_run_id']
    assert type(payload['latency_ms']) is int and payload['latency_ms'] >= 0
    assert 'private-marker' not in rows[0]['payload_json']
    # 文案只用于可见展示断言，不参与执行裁决。
    assert '最近收紧决定' in text and case.reply['reason_code'] in text
    assert f"结论 {case.reply['verdict']}" in text


def test_rm_rf_forces_confirmation_and_user_denial_never_enters_handler(guard_joint, monkeypatch):
    # 裸rm仍由宿主硬拒；只在无副作用计数handler上选现有规则的差集，不放宽宿主策略。
    # shellwrap 起 sh -c 包一层的删除也由宿主硬拒，差集改用 echo：宿主放行（无删除），
    # rm-guard 的文字规则（独立 rm 词 + 递归 + 强制）仍要求确认——测的是插件收紧链路，不是删除本身。
    assert evaluate_command_policy('rm -rf build', allow_shell_operators=True).finding_codes == (
        'COMMAND_DESTRUCTIVE_DELETE_BLOCKED',)
    command = _GUARD_ONLY_COMMAND
    assert evaluate_command_policy(command, allow_shell_operators=True).allowed
    case = _run_case(guard_joint, monkeypatch, _RunSpec('run_command', {'command': command}))
    _assert_query(case, 'guard-rm')
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    _assert_confirmation_denied(case)
    _assert_no_tool_events(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


def test_apply_patch_delete_is_denied_without_approval_handler_or_tool_events(guard_joint, monkeypatch):
    case = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': _DELETE_PATCH}))
    _assert_query(case, 'guard-delete')
    assert case.reply['verdict'] == 'deny' and case.reply['reason_code'] == 'DELETE_FILE_BLOCKED'
    assert case.execution.result.error_code == 'PLUGIN_GATE_DENIED'
    assert case.execution.decision.status == 'deny' and case.permissions == []
    _assert_no_tool_events(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


def test_truncated_patch_without_visible_delete_requires_confirmation(guard_joint, monkeypatch):
    patch = '*** Begin Patch\n*** Add File: padded.txt\n+' + 'x' * 5000 + '\n*** Delete File: disposable.txt\n*** End Patch\n'
    case = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': patch}))
    _assert_query(case, 'guard-delete', True)
    assert not any(line.startswith('*** Delete File: ') for line in case.request['call']['arguments']['patch'].splitlines())
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'ARGUMENTS_TRUNCATED'
    _assert_confirmation_denied(case)
    _assert_no_tool_events(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


def test_truncated_patch_with_visible_delete_remains_denied(guard_joint, monkeypatch):
    patch = '*** Begin Patch\n*** Delete File: disposable.txt\n*** Add File: padded.txt\n+' + 'x' * 5000 + '\n*** End Patch\n'
    case = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': patch}))
    _assert_query(case, 'guard-delete', True)
    assert any(line.startswith('*** Delete File: ') for line in case.request['call']['arguments']['patch'].splitlines())
    _assert_no_tool_events(guard_joint)
    assert (case.reply['verdict'], case.reply['reason_code']) == ('deny', 'DELETE_FILE_BLOCKED')
    assert case.execution.result.error_code == 'PLUGIN_GATE_DENIED' and case.permissions == []
    _assert_ledger_and_info(guard_joint, case)


# --- jb6b：ask 之后三个出口的真实账本（b5fix9b 已把门决定条目接到审批出口） ---


# LLM: 批准出口与拒绝出口的差别只在用户选了哪个选项：批准会真的重跑并进入 handler，但第一次真实征询的
#   条目必须仍然留在账本里（9b 实测原来批准路径也是空账本）。用零副作用计数探针证明"真跑了"。
# 函数用途: 断言批准后 handler 真执行，且账本与 /plugins info 仍读到那一次征询。
def test_rm_rf_confirmation_approved_runs_handler_and_keeps_ledger(guard_joint, monkeypatch):
    command = _GUARD_ONLY_COMMAND
    case = _run_case(guard_joint, monkeypatch, _RunSpec('run_command', {'command': command}, decision='approved'))
    _assert_query(case, 'guard-rm')
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    assert case.execution.result.ok is True and case.execution.result.handler_executed is True
    assert [event['kind'] for event in case.permissions] == ['permission_requested', 'permission_resolved']
    assert case.permissions[1]['decision'] == 'approved'
    # 批准后 handler 真的跑了，所以观察插件这次应当同时看到工具开始与结束（与拒绝路径的零工具事件互为反证）。
    _assert_tool_events_visible(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


# LLM: 与 _assert_no_tool_events 配对：handler 真的执行过时，观察插件必须看到工具开始与结束两类事件。
#   只读目标插件的真实 B3 帧与连接归属，不解析文案；缺任何一类都说明观察链断了。
#   注意（已核实，交 3a/9b 判定，不在本用例断言范围）：turn 事件的 thread_ref 来自
#   `conversation.channel_conversation_id`，tool 事件的 thread_ref 来自 task attribute
#   `conversation_thread_id`，两者在本回合不同；所以这里只断言工具事件内部一致，不断言跨族相等。
# 函数用途: 核对本次回合在目标插件留下五个生命周期事件（含工具开始/结束）。
def _assert_tool_events_visible(bundle):
    clients = [client for client in bundle.watch.clients
               if client.installation.manifest.plugin_id == bundle.watch.row.manifest.plugin_id]
    assert len(clients) == 1
    events = [event for method, params in clients[0].calls
              if method == 'my-agent/events.observe' for event in params['events']]
    assert {event['type'] for event in events} == {
        'prompt_submitted', 'turn_started', 'turn_ended', 'tool_call_started', 'tool_call_finished'}, (
        bundle.watch.hub.stats(str(bundle.owner.home_dir)))
    tool_events = [event for event in events if event['type'].startswith('tool_call_')]
    assert len(tool_events) == 2 and len({event['thread_ref'] for event in tool_events}) == 1
    assert all(event['thread_ref'] for event in events), '每个事件的会话引用都不许为空'
    _assert_distinct_connections(bundle)


# LLM: 无法审批是宿主结构化事实（这里没人能当场审批），不是模型猜的：客户端不声明 tool_approval 时，
#   插件 ask 必须变成明确拒绝，账本里那条征询的 final_status 要投影成 PLUGIN_GATE_APPROVAL_UNAVAILABLE，
#   否则 B6 的"无法审批"计数会漏掉所有审批阶段才判定的情况。handler 必须零执行。
# 函数用途: 断言非交互请求下 ask 收紧为不可审批、账本终态与计数都对。
def test_rm_rf_confirmation_unavailable_projects_unavailable_final_status(guard_joint, monkeypatch):
    command = _GUARD_ONLY_COMMAND
    case = _run_case(guard_joint, monkeypatch, _RunSpec('run_command', {'command': command}, interactive=False))
    _assert_query(case, 'guard-rm', interactive=False)
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    assert case.execution.result.error_code == 'PLUGIN_GATE_APPROVAL_UNAVAILABLE'
    assert case.execution.decision.status == 'deny' and case.permissions == []
    _assert_no_tool_events(guard_joint)

    rows, text = _gate_rows(guard_joint), _plugins_info(guard_joint)
    assert len(rows) == 1, {'runtime_events': rows, 'plugins_info': text}
    payload = json.loads(rows[0]['payload_json'])
    assert (payload['verdict'], payload['final_status']) == ('ask', 'PLUGIN_GATE_APPROVAL_UNAVAILABLE')
    assert payload['reason_code'] == 'RM_RF'
    assert '无法审批：1 次' in text


# LLM: 另一条"无法审批"入口：执行器当时看到可交互（客户端声明了 tool_approval，所以插件收到 ask），
#   但审批消费者实际答不了（返回 unavailable）。这时终态投影只能发生在审批阶段——执行器那次投影看不到，
#   所以这条专门钉 b5fix9b 的 unavailable 承接；漏了它 B6 的计数会漏掉整类"审批时才判定"的情况。
# 函数用途: 断言审批阶段判定不可用时，账本终态仍投影成不可审批。
def test_rm_rf_confirmation_consumer_unavailable_projects_unavailable_final_status(guard_joint, monkeypatch):
    command = _GUARD_ONLY_COMMAND
    case = _run_case(guard_joint, monkeypatch,
                     _RunSpec('run_command', {'command': command}, decision='unavailable'))
    _assert_query(case, 'guard-rm')  # 客户端声明了 tool_approval，所以插件看到的是可交互
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    assert case.execution.result.error_code == 'PLUGIN_GATE_APPROVAL_UNAVAILABLE'
    assert case.execution.decision.status == 'deny'
    _assert_no_tool_events(guard_joint)

    rows, text = _gate_rows(guard_joint), _plugins_info(guard_joint)
    assert len(rows) == 1, {'runtime_events': rows, 'plugins_info': text}
    payload = json.loads(rows[0]['payload_json'])
    assert (payload['verdict'], payload['final_status']) == ('ask', 'PLUGIN_GATE_APPROVAL_UNAVAILABLE')
    assert '无法审批：1 次' in text


# LLM: 同一 owner 多次征询后，"最近决定"必须新到旧。判据只用结构化事实：库里 seq 递增、B6 按 seq 倒序取，
#   所以最新那条的原因码在渲染文本里必须排在旧的那条之前；不靠时间文案（同秒会撞）。
# 函数用途: 断言同 owner 多行最近决定按新到旧渲染。
def test_recent_decisions_are_rendered_newest_first(guard_joint, monkeypatch):
    older = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': _DELETE_PATCH}))
    newer = _run_case(guard_joint, monkeypatch,
                      _RunSpec('run_command', {'command': _GUARD_ONLY_COMMAND}))
    assert older.reply['reason_code'] == 'DELETE_FILE_BLOCKED'
    assert newer.reply['reason_code'] == 'RM_RF'

    rows, text = _gate_rows(guard_joint), _plugins_info(guard_joint)
    assert len(rows) == 2, {'runtime_events': rows, 'plugins_info': text}
    assert rows[0]['seq'] < rows[1]['seq'], '库里 seq 必须递增（写入顺序）'
    # 离线夹具两轮共用同一个 provider call_id（event-native-call），所以"两次是不同征询"要按门身份判：
    # 去重键含 gate_id，两次命中不同门就必须是两行，不能被合并成一条。
    gate_ids = [json.loads(row['payload_json'])['gate_id'] for row in rows]
    assert sorted(gate_ids) == ['guard-delete', 'guard-rm'], gate_ids

    older_at, newer_at = text.index('原因码 DELETE_FILE_BLOCKED'), text.index('原因码 RM_RF')
    assert newer_at < older_at, f'最近决定要从新到旧；实际文本：{text}'
    assert '最近收紧决定（最多 10 次，新到旧）' in text


# ---- B5+B4+B8 组合链：真实 rm-guard 协议、注册表、执行器与决定写账 ----

# LLM: 仅临时安装v8样例，不启用、不解除B7关闭门；协议函数从样例源码原样加载。
# 函数用途: 给组合用例准备真实样例声明与服务，所有写入只落pytest临时目录。
def _installed_sample(tmp_path):
    (tmp_path / "owner").mkdir()
    service, _ = manager(tmp_path / "owner")
    project = SAMPLES / "rm-guard"
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, project, tmp_path / "rm-guard.zip")
    result = service.command(f'/plugins install "{package}"', revision=service.catalog().revision,
                             request_id="install-rm-guard")
    assert result["state"] == "succeeded", result
    manifest = PluginInstallStore(service.context.owner).snapshot()[0].manifest
    spec = importlib.util.spec_from_file_location("combination_rm_guard", project / "src" / "server.py")
    sample = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sample)
    return service, manifest, sample


# LLM: 只经产品仓储建立临时运行身份，不写runtime_events；决定必须来自原归档/持久化链。
# 函数用途: 为组合调用准备真实owner库与可归档的宿主投影，写入均在测试目录。
def _ledger_host(service, root):
    owner_home = service.context.owner.home_dir
    repo = RuntimeRepository(runtime_db_path(owner_home))
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    repo.create_agent_run(task_run_id=task_run["task_run_id"], run_id="run-combination")
    return SimpleNamespace(root=root, config=AgentConfig(),
        home_paths=SimpleNamespace(owner_home_dir=owner_home), subagents=SimpleNamespace(runtime_db=repo))


# LLM: 复用生产的archive_tool_call_record→persist_tool_runtime_ledger两段，不构造决定字段或直接append_event。
# 函数用途: 将真实执行结果沿原归档入口保存，供B6查询核对接缝。
def _persist_execution(agent, execution):
    params = SimpleNamespace(request_id="combination", task_id="", run_id=execution.call.run_id,
        attempt_id=execution.call.attempt_id, run_scope=None, context_scope="default",
        task_attributes={}, live_archive_state={})
    record = ToolCallRecordParams(params, 1, 1, execution.call, execution.result, execution.states)
    archive = archive_tool_call_record(agent, record)
    persist_tool_runtime_ledger(agent, archive)
    return archive


# LLM: 返回安装样例的隔离通道投影，真实安装仍停用；协议裁决来自样例，不构造决定账本。
# 函数用途: 单独准备真实样例协议的假传输，避免组合fixture混入协议装配细节。
def _sample_gate_harness(service, manifest, sample):
    harness = _Harness()
    harness.owner = service.context.owner
    # 激活是测试投影，真实安装仍停用，B7 关闭门不解除。
    harness.rows = [SimpleNamespace(manifest=manifest, enabled=True,
                                    activation=SimpleNamespace(activation_id="act-combination"))]
    harness.capabilities = sample.handle({"jsonrpc": "2.0", "id": 1,
                                          "method": "initialize"})["result"]["capabilities"]
    harness.responses = []

    # LLM: 决定由真实样例协议产出，测试只收集回复，不手填宿主决定或账本。
    # 函数用途: 转发一次隔离门征询并保存样例回执。
    def answer(_row, params, _timeout):
        result = sample.handle({"jsonrpc": "2.0", "id": 2,
            "method": "my-agent/tool-gate.review", "params": params})["result"]
        harness.responses.append(result)
        return result

    harness.answer = answer
    return harness


# LLM: 共用执行链和B4观察保持真实；只替换激活表/通道，不手插决定账本或越过原handler。
# 函数用途: 执行一次临时补丁并记录handler和事件，结束后关闭本用例的假通道池。
@pytest.fixture
def delete_review(tmp_path, monkeypatch, request):
    service, manifest, sample = _installed_sample(tmp_path)
    harness = _sample_gate_harness(service, manifest, sample)
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "obsolete.txt"
    target.write_text("必须保留\n", encoding="utf-8")
    registry = ToolRegistry(ToolRegistryParams(root, 100, 10, 10, 100, 1, 10, 10, False,
        operation_store_required=False, approval_mode_reader=lambda: "auto",
        plugin_gate_reviewer=harness.reviewer.review))
    snapshot = registry.runtime_snapshot(run_id="run-combination")
    patch_tool = snapshot.runtime("apply_patch").handler
    executed, events = [], []
    original = patch_tool.execute

    # LLM: 记录真正进入handler的参数后调用原实现，拒绝分支不应触达这里。
    # 函数用途: 给组合用例留下实际handler执行证据。
    def execute(arguments):
        executed.append(dict(arguments))
        return original(arguments)

    monkeypatch.setattr(patch_tool, "execute", execute)
    patch = "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch\n"
    if getattr(request, "param", "delete") == "update":
        patch = "*** Begin Patch\n*** Update File: obsolete.txt\n-必须保留\n+通过更新\n*** End Patch\n"
    call = canonical_test_call(snapshot, "apply_patch", {"patch": patch})
    context = EventPointContext(SimpleNamespace(plugin_events_enabled=True), events.append,
                                "local", "isolated-thread", "main")
    execution = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot,
        trusted_run_context={"plugin_event_context": context, "interactive": False})
    archive = _persist_execution(_ledger_host(service, root), execution)
    try:
        yield SimpleNamespace(service=service, harness=harness, call=call, execution=execution,
                              executed=executed, events=events, target=target, archive=archive)
    finally:
        harness.pool.close()


# LLM: 结构化拒绝、handler执行与事件共同反证，不能只断言错误码或靠关闭观察开关过关。
# 函数用途: 核对样例已经拒绝且文件与真实工具观察都没有被触碰。
def _assert_denied_before_handler(review):
    execution = review.execution
    assert execution.decision.status == "deny"
    assert execution.result.error_code == "PLUGIN_GATE_DENIED"
    assert execution.result.handler_executed is False
    assert execution.result.effect_outcome == "not_started"
    assert review.executed == []
    assert review.events == []  # 无 started，也无 finished；观察开关已开，不能靠禁用伪造零事件。
    assert review.target.read_text(encoding="utf-8") == "必须保留\n"
    assert len(review.harness.sent) == 1
    sent = review.harness.sent[0][2]
    assert sent["gate_id"] == "guard-delete"
    assert sent["call"]["tool"] == "apply_patch"
    # plugin_requirements 是待用户确认的门集合，不含直接 deny；原协议回执才是这里的裁决观察点。
    assert review.harness.responses == [{"verdict": "deny", "reason_code": "DELETE_FILE_BLOCKED",
                                         "message": "补丁要删除文件，拒绝"}]


def test_rm_guard_delete_rejected_without_handler_or_tool_events(delete_review):
    _assert_denied_before_handler(delete_review)


# LLM: 允许正对照必须真实写文件并发成对B4事件，再核原链写出的allow决定，不能靠观察关闭形成假绿。
# 函数用途: 核对门放行后的真实handler、工具事件与决定写账。
@pytest.mark.parametrize("delete_review", ["update"], indirect=True)
def test_allowed_update_runs_real_handler_and_emits_both_tool_events(delete_review):
    assert delete_review.execution.result.ok
    assert delete_review.execution.result.handler_executed is True
    assert len(delete_review.executed) == 1
    assert [event.type for event in delete_review.events] == ["tool_call_started", "tool_call_finished"]
    assert delete_review.events[0].facts["call_id"] == delete_review.call.call_id
    assert delete_review.events[1].facts["handler_executed"] is True
    assert delete_review.target.read_text(encoding="utf-8") == "通过更新\n"
    rows = decisions(delete_review.service, plugin_id="rm-guard")
    assert len(rows) == 1 and rows[0]["verdict"] == "allow_as_is"
    assert rows[0]["call_id"] == delete_review.call.call_id


def test_rm_guard_delete_decision_visible_in_real_plugins_info(delete_review):
    _assert_denied_before_handler(delete_review)
    rows = decisions(delete_review.service, plugin_id="rm-guard")
    assert len(rows) == 1
    assert rows[0]["call_id"] == delete_review.call.call_id
    assert rows[0]["tool"] == "apply_patch"
    assert rows[0]["final_status"] == "deny"
    assert rows[0]["reason_code"] == "DELETE_FILE_BLOCKED"
    assert "原因码 DELETE_FILE_BLOCKED" in info_message(delete_review.service, plugin_id="rm-guard")
