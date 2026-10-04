"""M1 B8 样例插件合同用例：假宿主按第 5 节协议直接和插件进程对话，不经 Gateway。

- 三个包用仓库构建脚本打成 v8 并过读包校验（B1 规则）；
- event-watch：握手能力位、6 类事件计数、dropped_before 计入、只读面板幂等；
- rm-guard / rm-guard-node：rm 组合写法回 ask + RM_RF，普通命令 allow_as_is，apply_patch 补丁里的删除文件段回 deny + DELETE_FILE_BLOCKED；
- 截断只能更严（b8tr/trs）：arguments_truncated 为布尔 true 时才生效——片段已 deny 保持 deny（原原因码），片段本来就 ask 保留原原因码（消息补"参数还被截断了"），只有可放行才升到 ask + ARGUMENTS_TRUNCATED；false、缺失（旧宿主）或 1/"true" 这类真值照旧；
- 本机没有 node 时 Node 用例跳过；全部写入在 tmp_path，不启动产品 TUI、模型或 Gateway。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.plugin_package import inspect_plugin_package
from scripts.build_plugin_files_package import build_files_package

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "plugins"
NODE = shutil.which("node")
_REASON_CODE = re.compile(r"[A-Z0-9_]{1,40}\Z")
_VERDICTS = ("allow_as_is", "ask", "deny")
_EVENT_TYPES = ("prompt_submitted", "turn_started", "turn_ended", "tool_call_started", "tool_call_finished",
                "command_executed")
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
#   改动同步：样例插件 plugins/{rm-guard,rm-guard-node} 的 reviewGate、_TRUNCATION_CASES 表、_TRUNCATED_MESSAGE/
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
