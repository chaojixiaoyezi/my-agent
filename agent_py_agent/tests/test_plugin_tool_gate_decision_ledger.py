# LLM: 决定事实沿真实执行器生成；本文件库替身只给组件证据，真实归档/临时库/info链由组合用例核对。
# 模块用途: 反证每门写账、白名单、批准排除和不可交互终态，不启用真实插件或Gateway。
"""B5 第 5 段：收紧征询的决定账本（plugin_gate.decided）。

只构造内存假件（假安装表、假传输、真 B2 池）+ 临时 runtime.db；不启用真实 v8、不连真实 Gateway。
字段集合与设计第9节对齐，本文件的库替身/旧B6投影只提供组件证据；
真实RuntimeRepository与当前B6 info接线在test_plugin_gate_event_combination覆盖，不能把本文件替身外推为生产验收。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.plugin_channel import PluginChannelPool
from agent_py_agent.agent.plugin_channel.pool import PluginChannelRevoked, PluginChannelTimeout
from agent_py_agent.agent.plugin_events.decision_ledger import (
    PLUGIN_GATE_DECIDED_EVENT,
    PLUGIN_GATE_DECISION_EVIDENCE_KEY,
    PLUGIN_GATE_DECISION_FIELDS,
    plugin_gate_decision_payload,
)
from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
from agent_py_agent.agent.plugin_events.tool_gate import GateCall, GateTarget
from agent_py_agent.agent.plugin_events.tool_gate_review import GateWiring, PluginGateReviewer
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)

# 照抄 B6（be00891b8 runtime_db/repository.py）的白名单与投影口径：本文件不依赖 B6 分支代码。
_B6_FIELDS = (
    "plugin_id", "version", "activation_id", "gate_id", "tool", "call_id", "operation_id", "args_hash",
    "actor", "outcome", "verdict", "reason_code", "latency_ms", "host_status", "final_status",
)


# LLM: 仅复用旧B6投影口径核组件字段，不冒充当前产品info或真实owner仓储读取。
# 函数用途: 按B6旧白名单投影内存事件行，并补事件时间。
def _b6_project(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"] or "{}")
    decision = {field: payload.get(field) for field in _B6_FIELDS}
    decision["created_at"] = float(row["created_at"] or 0.0)
    return decision


# LLM: 真实Executor调用该最小handler；执行与否以标准ToolResult事实断言，不替换执行裁决。
# 类用途: 提供一个无外部副作用的工具，核对决定进入结果metadata。
class _Probe(BaseTool):
    model_spec = make_test_model_spec("gate_probe", input_schema={"type": "object", "additionalProperties": True})
    runtime_policy = make_test_runtime_policy()

    # LLM: 只返回固定handler结果，不读真实凭据或网络；是否调用由原Executor控制。
    # 函数用途: 为允许分支提供最小成功结果。
    def execute(self, arguments):
        return ToolHandlerOutcome("gate_probe", True, "executed")


# LLM: 替换插件进程传输，不替换协议解码与B2池的身份复核。
# 类用途: 把池请求送入可编排的测试回复。
class _Transport:
    # LLM: 只保存所属假client，不建立外部连接。
    # 函数用途: 绑定测试传输的client。
    def __init__(self, client):
        self.client = client

    # LLM: 先执行真实池传来的authority_check，再调用假回复；失效身份不能被测试线路绕过。
    # 函数用途: 转发一次门征询并保留权威复核顺序。
    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        return self.client.harness.answer(self.client.row, params, timeout)


# LLM: 仅提供B2所需client合同；能力与激活来自本用例安装投影，不启用真实v8。
# 类用途: 为真实池准备隔离client与传输。
class _Client:
    # LLM: 保存本用例harness和安装行，声明能力与激活复核都走这份投影。
    # 函数用途: 初始化测试client的协议能力及激活引用。
    def __init__(self, h, row):
        self.harness, self.row = h, row
        self.capabilities = h.capabilities
        self.activation_ref = SimpleNamespace(require=self.require)

    # LLM: 只返回本用例安装行，真实池仍负责复核调用该合同。
    # 函数用途: 提供当前测试激活。
    def require(self):
        return self.row

    # LLM: 仅创建假传输，不启动进程；协议与并发仍交给真实池。
    # 函数用途: 返回本client的隔离传输。
    def start(self):
        return _Transport(self)

    # LLM: 假client没有进程资源，不操作真实Gateway或插件进程。
    # 函数用途: 实现测试client的空资源收尾合同。
    def stop(self):
        return None


# LLM: 假安装表 + 真 B2 池 + 可编排回复；只替换"插件进程"，协议与并发语义仍走真实池。
# 类用途: 组装单门或多门的隔离征询线路。
class _Harness:
    # LLM: 只构造声明/激活投影和真池，不解除B7启用门或访问真实owner。
    # 函数用途: 为指定门和回复建立组件测试环境。
    def __init__(self, answers, *, gates=("guard-rm",), arguments="full"):
        self.answers = answers if isinstance(answers, dict) else list(answers)
        self.sent = []
        self.capabilities = {"experimental": {"my-agent/tool-gate": {"versions": ["1"]}}}
        self.row = SimpleNamespace(
            manifest=SimpleNamespace(plugin_id="rm-guard", version="1.0.0",
                                     tool_gates=tuple(PluginToolGateDeclaration(id=g, tools=("gate_probe",),
                                                                                effects=(), arguments=arguments)
                                                      for g in gates)),
            activation=SimpleNamespace(activation_id="act-1"),
            enabled=True,
        )
        self.pool = PluginChannelPool(client_factory=lambda _owner, row: _Client(self, row))
        self.reviewer = PluginGateReviewer(GateWiring(
            owner=SimpleNamespace(home_dir=".", identity="local/main"), config=SimpleNamespace(plugin_tool_gate_timeout_ms=2000),
            pool=lambda: self.pool, installations=lambda owner: (self.row,)))

    # LLM: 按门ID选择预设回复或抛预设异常；不生成宿主决定、批准引用或账本行。
    # 函数用途: 为真实征询提供可复现的插件侧响应。
    def answer(self, row, params, timeout):
        gate_id = str((params or {}).get("gate_id") or "")
        if isinstance(self.answers, dict):
            item = self.answers.get(gate_id)
        else:
            item = self.answers.pop(0) if self.answers else None
        if item is None:
            return {"verdict": "allow_as_is", "reason_code": "OK"}
        if isinstance(item, BaseException):
            raise item
        return item


# LLM: 调用真实Executor且仅替插件传输，不手填ActionDecision或决定字段。
# 函数用途: 返回本次执行结果与工具探针供组件断言。
def _execute(tmp_path, harness, arguments=None):
    probe = _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", arguments if arguments is not None else {})
    execution = ToolExecutor().execute(ToolExecutorRequest(
        call, snapshot, tmp_path, operation_store_required=False,
        plugin_gate_reviewer=harness.reviewer.review))
    return execution, probe


# LLM: 只读Executor产出的专键，不把handler任意metadata或人工条目当门决定。
# 函数用途: 取本次执行结果的决定事实供断言。
def _entries(execution) -> list[dict]:
    return list(execution.result.metadata.get(PLUGIN_GATE_DECISION_EVIDENCE_KEY) or [])


# --- 字段集合与内容 ---


# LLM: 单门真实征询后核完整白名单、身份和延迟，缺任一字段都必须业务失败。
# 函数用途: 固定决定字段集合与宿主身份来源。
def test_single_gate_writes_exactly_design_field_set(tmp_path):
    h = _Harness([{"verdict": "ask", "reason_code": "RM_RF", "message": "别删"}])
    execution, _probe = _execute(tmp_path, h)
    entries = _entries(execution)
    assert len(entries) == 1
    payload = plugin_gate_decision_payload(entries[0])
    assert tuple(payload.keys()) == PLUGIN_GATE_DECISION_FIELDS
    assert payload["plugin_id"] == "rm-guard" and payload["version"] == "1.0.0"
    assert payload["activation_id"] == "act-1" and payload["gate_id"] == "guard-rm"
    assert payload["tool"] == "gate_probe" and payload["verdict"] == "ask"
    assert payload["reason_code"] == "RM_RF" and payload["outcome"] == "ok"
    assert payload["host_status"] == "allow" and payload["final_status"] == "ask"
    assert isinstance(payload["latency_ms"], int) and payload["latency_ms"] >= 0
    assert payload["call_id"] and payload["operation_id"] and payload["args_hash"] and payload["actor"] == "main"


# LLM: 用带标记消息和参数核结构化决定投影，不依赖空输入形成假脱敏通过。
# 函数用途: 反证插件正文与工具参数不能进入账本。
def test_no_message_and_no_arguments_enter_the_ledger(tmp_path):
    # 这里用ask核脱敏；deny/allow同样必须记账，另有真实组合与混合门用例钉住。
    h = _Harness([{"verdict": "ask", "reason_code": "RM_RF", "message": "SECRET-MESSAGE"}])
    execution, _probe = _execute(tmp_path, h, {"command": "ls /tmp/SECRET-ARG"})
    payload = plugin_gate_decision_payload(_entries(execution)[0])
    text = json.dumps(payload, ensure_ascii=False)
    assert "SECRET-MESSAGE" not in text and "SECRET-ARG" not in text
    assert "message" not in payload and "arguments" not in payload


# LLM: 异常和畸形回复来自传输替身，outcome仍由真实reviewer/Executor确定。
# 函数用途: 覆盖正常、畸形、错误与不可用决定的组件写账。
@pytest.mark.parametrize("outcome,answers", [
    ("ok", [{"verdict": "ask", "reason_code": "RM_RF"}]),
    ("malformed", [{"verdict": "maybe", "reason_code": "RM_RF"}]),
    ("error", [RuntimeError("boom")]),
    # 传输报撤销而安装表仍启用时，征询按设计改判 unavailable（不能当停用而放行）；
    # 真正的 revoked 只在安装快照证明该激活已消失时出现，由 _current_review 判定。
    ("unavailable", [PluginChannelRevoked("revoked")]),
])
def test_outcome_variants_reach_the_ledger(tmp_path, outcome, answers):
    h = _Harness(answers)
    execution, _probe = _execute(tmp_path, h)
    entries = _entries(execution)
    assert len(entries) == 1
    assert entries[0]["outcome"] == outcome
    assert plugin_gate_decision_payload(entries[0])["outcome"] == outcome


# LLM: 传输抛真实池超时类型，不sleep或直接伪造timeout条目。
# 函数用途: 核对超时征询保留timeout决定。
def test_timeout_outcome_is_recorded(tmp_path):
    # 传输抛池的超时异常即 timeout（不 sleep）。
    h = _Harness([PluginChannelTimeout("slow")])
    execution, _probe = _execute(tmp_path, h)
    assert _entries(execution)[0]["outcome"] == "timeout"


# LLM: 共用池缺失只影响本用例，真实reviewer应给不可用，不允许放行或虚构成功。
# 函数用途: 核对无池时仍保留不可用决定。
def test_unavailable_when_pool_is_missing(tmp_path):
    h = _Harness([])
    h.reviewer = PluginGateReviewer(GateWiring(
        owner=SimpleNamespace(home_dir=".", identity="local/main"),
        config=SimpleNamespace(plugin_tool_gate_timeout_ms=2000),
        pool=lambda: None, installations=lambda owner: (h.row,)))
    execution, _probe = _execute(tmp_path, h)
    assert _entries(execution)[0]["outcome"] == "unavailable"


# LLM: 用同一调用的两个真实review反证仅写主原因或第一门的实现。
# 函数用途: 固定多门各写一条决定的数量与门身份。
def test_multiple_gates_write_one_entry_each(tmp_path):
    # 同一调用多个门都必须留决定，不得只保主原因或第一条。
    h = _Harness({"guard-rm": {"verdict": "ask", "reason_code": "RM_RF"},
                  "guard-alt": {"verdict": "ask", "reason_code": "BLOCK"}},
                 gates=("guard-rm", "guard-alt"))
    execution, _probe = _execute(tmp_path, h)
    entries = _entries(execution)
    assert len(entries) == 2
    assert {item["gate_id"] for item in entries} == {"guard-rm", "guard-alt"}
    assert {item["reason_code"] for item in entries} == {"RM_RF", "BLOCK"}


# LLM: 非ask仍是真实征询，不能因为不进入待审批requirements而丢决定。
# 函数用途: 覆盖直接拒绝与允许的账本终态。
@pytest.mark.parametrize("verdict,final_status", [("deny", "deny"), ("allow_as_is", "allow")])
def test_nonask_gate_still_records_its_real_decision(tmp_path, verdict, final_status):
    h = _Harness([{"verdict": verdict, "reason_code": "REAL_REPLY"}])
    execution, _probe = _execute(tmp_path, h)
    entries = _entries(execution)
    assert len(entries) == 1
    assert entries[0]["verdict"] == verdict and entries[0]["final_status"] == final_status
    assert entries[0]["call_id"] == execution.call.call_id


# LLM: 三类回复一起出现时每门仍独立记账，但共享最严合并终态。
# 函数用途: 核对混合门不漏记且共同保留deny。
def test_mixed_verdicts_all_record_once_with_same_final_status(tmp_path):
    h = _Harness({"guard-rm": {"verdict": "ask", "reason_code": "ASK"},
                  "guard-alt": {"verdict": "deny", "reason_code": "DENY"},
                  "guard-ok": {"verdict": "allow_as_is", "reason_code": "OK"}},
                 gates=("guard-rm", "guard-alt", "guard-ok"))
    execution, _probe = _execute(tmp_path, h)
    entries = _entries(execution)
    assert len(entries) == 3
    assert {item["verdict"] for item in entries} == {"ask", "deny", "allow_as_is"}
    assert {item["final_status"] for item in entries} == {"deny"}
    assert len({item["gate_id"] for item in entries}) == 3


# LLM: 无交互取可信宿主字段，最终ask变为明确失败；决定终态须与B6计数口径一致。
# 函数用途: 核对真正无法审批的决定终态。
def test_unavailable_approval_terminal_is_projected_for_b6_count(tmp_path):
    h = _Harness([{"verdict": "ask", "reason_code": "CONFIRM"}])
    snapshot = runtime_snapshot_for_tools({"gate_probe": _Probe()})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, plugin_gate_reviewer=h.reviewer.review,
        trusted_run_context={"interactive": False}))
    assert execution.result.error_code == "PLUGIN_GATE_APPROVAL_UNAVAILABLE"
    assert _entries(execution)[0]["final_status"] == "PLUGIN_GATE_APPROVAL_UNAVAILABLE"


# LLM: 无交互只把最终ask投影为无法审批，混合门已有deny时仍必须按真实拒绝写账，不能污染B6计数。
# 函数用途: 固定不可交互的混合门以最严拒绝作为共同终态。
def test_noninteractive_mixed_deny_is_not_recorded_as_approval_unavailable(tmp_path):
    h = _Harness({"guard-rm": {"verdict": "ask", "reason_code": "ASK"},
                  "guard-alt": {"verdict": "deny", "reason_code": "DENY"}},
                 gates=("guard-rm", "guard-alt"))
    snapshot = runtime_snapshot_for_tools({"gate_probe": _Probe()})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, plugin_gate_reviewer=h.reviewer.review,
        trusted_run_context={"interactive": False}))
    assert execution.result.error_code == "PLUGIN_GATE_DENIED"
    assert not execution.result.handler_executed
    entries = _entries(execution)
    assert len(entries) == 2
    assert {item["verdict"] for item in entries} == {"ask", "deny"}
    assert {item["final_status"] for item in entries} == {"deny"}


# LLM: 安装未声明门时没有征询，不能为了展示有记录而写空事件。
# 函数用途: 固定无门时零决定事实。
def test_no_gate_means_no_entry(tmp_path):
    h = _Harness([])
    h.row.manifest.tool_gates = ()
    execution, _probe = _execute(tmp_path, h)
    assert _entries(execution) == []


# LLM: 精确批准跳门没有协议征询；组件构造合并事实只为独立钉住排除条件，不代真实审批接线。
# 函数用途: 反证已批准跳门不能混入决定列表。
def test_approval_applied_gate_writes_no_ledger_entry():
    # 第 4 段契约（3a/sol3 给定）：approval_applied=True 表示宿主按精确批准跳过了这个门、
    # 没有真的征询插件；这类投影绝不写 plugin_gate.decided，否则 B6 的"最近 10 次收紧决定"
    # 会混进从未发生的征询。这里直接构造合并结果，精确钉住排除条件。
    from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
    from agent_py_agent.agent.plugin_events.tool_gate import (
        GateDecision,
        GateReply,
        GateReview,
    )
    from agent_py_agent.agent.tooling.plugin_gate_policy import _decision_ledger_entries
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    snapshot = runtime_snapshot_for_tools({"gate_probe": _Probe()})
    call = canonical_test_call(snapshot, "gate_probe", {})
    gate_call = GateCall(call, "read_only", "main", True)
    approved_target = GateTarget("rm-guard", "1.0.0", "act-1",
                                 PluginToolGateDeclaration("guard-approved", ("gate_probe",), (), "none"))
    asked_target = GateTarget("rm-guard", "1.0.0", "act-1",
                              PluginToolGateDeclaration("guard-asked", ("gate_probe",), (), "none"))
    approved = GateReview(approved_target, GateReply("ask", "SHOULD_NOT_RECORD"), approval_applied=True)
    asked = GateReview(asked_target, GateReply("ask", "RM_RF"))
    merged = GateDecision("ask", approved, (approved, asked))
    entries = _decision_ledger_entries(gate_call, "allow", merged.status, merged.requirements)
    assert [item["gate_id"] for item in entries] == ["guard-asked"]
    assert all(item["gate_id"] != "guard-approved" for item in entries)


# LLM: 直接测试封闭outcome投影，非法值只能更严为error，不能扩大展示合同。
# 函数用途: 固定未知outcome的安全投影。
def test_out_of_set_outcome_is_tightened_to_error():
    # 投影必须把封闭集合外的 outcome 收紧为 error（否则展示层会读到未登记值）。直接喂投影，覆盖所有非法值。
    for value in ("weird", "", None, 7, "OK"):
        payload = plugin_gate_decision_payload({"outcome": value})
        assert payload["outcome"] == "error"


# --- 组件接缝：库替身加旧B6投影；真实库/当前info接线见组合文件 ---


# LLM: 仅替权威库合同并使用旧B6投影，不能把本内存库当真实owner或当前info接线证据。
# 类用途: 保存组件写入事件，核对原持久化writer的调用与字段。
class _Repo:
    # LLM: 只建本用例内存SQLite，SQL和时间均为测试数据，不触碰真实owner库。
    # 函数用途: 初始化组件事件表与收集列表。
    def __init__(self, tmp_path):
        self.rows: list[dict] = []
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE runtime_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, "
                        "event_type TEXT, attempt_id TEXT, agent_run_id TEXT, task_run_id TEXT, "
                        "payload_json TEXT, created_at REAL)")
        self.db.commit()

    # LLM: 返回固定测试运行归属；真实身份建立由组合用例的产品仓储另验。
    # 函数用途: 为组件writer提供最小运行映射。
    def agent_run_for_run_id(self, run_id):
        return {"agent_run_id": "run-1", "task_run_id": "task-1"}

    # LLM: 只保存writer实际传来的事件，不补决定字段；字段真假由业务断言核对。
    # 函数用途: 将一次组件事件写入内存库并收集payload。
    def append_event(self, **kwargs):
        self.db.execute("INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id, task_run_id, "
                        "payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
                        ("e%d" % (len(self.rows) + 1), kwargs["event_type"], kwargs["attempt_id"],
                         kwargs["agent_run_id"], kwargs["task_run_id"],
                         json.dumps(kwargs["payload"], ensure_ascii=False), 1000.0 + len(self.rows)))
        self.db.commit()
        self.rows.append(kwargs["payload"])

    # LLM: 按真实事件类型过滤，再走旧B6白名单投影；无事件应读回空列表。
    # 函数用途: 返回本组件库里的决定投影。
    def decisions(self):
        return [_b6_project(row) for row in self.db.execute(
            "SELECT * FROM runtime_events WHERE event_type = ? ORDER BY seq", (PLUGIN_GATE_DECIDED_EVENT,))]


# LLM: 人工归档只核组件writer接缝；决定来自真实Executor，当前info真实库另由组合覆盖。
# 函数用途: 核对canonical信封经过原持久化入口到旧B6投影。
def test_archive_record_reaches_runtime_events_and_b6_reads_it(tmp_path):
    h = _Harness({"guard-rm": {"verdict": "ask", "reason_code": "RM_RF"},
                  "guard-alt": {"verdict": "ask", "reason_code": "RM_RF"}},
                 gates=("guard-rm", "guard-alt"))
    execution, _probe = _execute(tmp_path, h)
    repo = _Repo(tmp_path)
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    archive = {"run_id": "run-1", "attempt_id": "attempt-1", "operation_id": "op-1",
               "tool_result_envelope": {PLUGIN_GATE_DECISION_EVIDENCE_KEY: _entries(execution)}}
    persist_tool_runtime_ledger(agent, archive)
    rows = repo.decisions()
    assert len(rows) == 2
    for row in rows:
        assert set(row) == set(_B6_FIELDS) | {"created_at"}
        assert row["plugin_id"] == "rm-guard" and row["tool"] == "gate_probe"
        assert row["reason_code"] == "RM_RF" and row["final_status"] == "ask"


# LLM: 不含门决定的归档不得产生plugin_gate.decided，不靠总有事件伪造接通。
# 函数用途: 反证无决定键时零决定事件。
def test_archive_without_gate_decisions_writes_no_event(tmp_path):
    # 必须修 2（ds1 初审）：传一条不含 plugin_gate_decisions 的 archive record，runtime_events 里
    # plugin_gate.decided 一行都不该有——没有门就没有决定，不能为"总有记录"写空事件。
    repo = _Repo(tmp_path)
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    archive = {"run_id": "run-1", "attempt_id": "attempt-1", "operation_id": "op-1"}
    persist_tool_runtime_ledger(agent, archive)
    rows = list(repo.db.execute(
        "SELECT * FROM runtime_events WHERE event_type = ?", (PLUGIN_GATE_DECIDED_EVENT,)))
    assert rows == []
    assert repo.decisions() == []


# LLM: 专键存在但为空也没有真实征询，不能写占位决定。
# 函数用途: 固定空决定集合不入账。
def test_empty_gate_decisions_list_also_writes_nothing(tmp_path):
    # 有键但空列表同样一条不写（executor 在无门时放的是空列表，不是缺键）。
    repo = _Repo(tmp_path)
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    archive = {"run_id": "run-1", "attempt_id": "attempt-1", "operation_id": "op-1",
               "tool_result_envelope": {PLUGIN_GATE_DECISION_EVIDENCE_KEY: []}}
    persist_tool_runtime_ledger(agent, archive)
    assert repo.decisions() == []


# LLM: 伪造顶层数据不在归档合同内，writer不能给它兼容授权或接线旁路。
# 函数用途: 钉住决定只能从canonical信封提取。
def test_top_level_decisions_cannot_replace_canonical_envelope(tmp_path):
    repo = _Repo(tmp_path)
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    persist_tool_runtime_ledger(agent, {"run_id": "run-1", "attempt_id": "attempt-1",
        PLUGIN_GATE_DECISION_EVIDENCE_KEY: [{"plugin_id": "forged-top-level"}]})
    assert repo.decisions() == []


# LLM: 归档只给宿主决定专键单独白名单入口，其他metadata仍受原白名单约束。
# 函数用途: 核对决定字段穿过原归档且无额外metadata泄漏。
def test_envelope_carries_gate_decisions_but_not_other_metadata(tmp_path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope

    h = _Harness([{"verdict": "ask", "reason_code": "RM_RF"}])
    execution, _probe = _execute(tmp_path, h)
    envelope = _compact_result_envelope(execution.result)
    assert PLUGIN_GATE_DECISION_EVIDENCE_KEY in envelope
    assert len(envelope[PLUGIN_GATE_DECISION_EVIDENCE_KEY]) == 1
    # 除该键外，envelope 仍受原白名单约束：不夹带 action_decision 等顶层 metadata。
    assert "action_decision" not in envelope
