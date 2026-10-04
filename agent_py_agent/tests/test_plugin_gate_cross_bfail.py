"""B5×B7 跨件：收紧征询遇上插件进程慢/起不来时会怎样（不依赖 B7 已接线）。

只走真实链路：真 B2 池（启动、单在途、发送前后代次核对都是真的）、真 PluginGateReviewer、
真 PluginToolGate.merge、真 ToolExecutor。被替换的只有"插件进程"这一层（假 client/transport）
以及需要精确控制时机的延迟/失败注入。

三条：
1. 冷启动撞预算：首次 request 超过 plugin_tool_gate_timeout_ms → timeout → ask，不放行。
2. 运行中沙箱起不来：连接启动失败 → ask，账本 plugin_gate.decided 留痕。
3. 进程被杀后下一次征询：连接已建、transport 失效抛连接类错误 → 退避 → ask。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.plugin_channel import PluginChannelPool, PluginChannelStartFailed
from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
from agent_py_agent.agent.plugin_events.tool_gate import (
    GateCall,
    GateReply,
    GateReview,
    GateTarget,
    PluginToolGate,
)
from agent_py_agent.agent.plugin_events.tool_gate_review import GateWiring, PluginGateReviewer
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


class _Probe(BaseTool):
    model_spec = make_test_model_spec("gate_probe", input_schema={"type": "object", "additionalProperties": True})
    runtime_policy = make_test_runtime_policy()

    def __init__(self):
        self.executed = []

    def execute(self, arguments):
        self.executed.append(arguments)
        return ToolHandlerOutcome("gate_probe", True, "executed")


# LLM: 传输替身保留池的真实启动/单在途语义，只把"插件进程"换成可编排的假实现；
#   killed 用来模拟"连接还在池里、但进程已经没了"——下一次请求直接报连接类错误。
class _Transport:
    def __init__(self, client):
        self.client = client

    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        client = self.client
        if client.killed:
            raise OSError("plugin process is gone")
        client.harness.sent.append((client.row.activation.activation_id, method, params, timeout))
        if client.harness.delay_seconds:
            time.sleep(client.harness.delay_seconds)
        return client.harness.answer(client.row, params, timeout)


class _Client:
    def __init__(self, h, row):
        self.harness, self.row, self.killed = h, row, False
        self.capabilities = h.capabilities
        self.activation_ref = SimpleNamespace(require=self.require)

    def require(self):
        current = next((item for item in self.harness.rows
                        if item.manifest.plugin_id == self.row.manifest.plugin_id), None)
        if current is None or current.activation != self.row.activation:
            raise ValueError("revoked")
        return current

    def start(self):
        if self.harness.start_error is not None:
            raise self.harness.start_error
        return _Transport(self)

    def stop(self):
        return None


# LLM: 假安装表 + 真 B2 池；config 用真实 AgentConfig 字段名，预算从同一条读取路径来。
# 类用途: 组装一条可编排的隔离征询线路与真实执行器。
class _Harness:
    def __init__(self, *, timeout_ms=200, delay_seconds=0.0, start_error=None):
        self.sent = []
        self.capabilities = {"experimental": {"my-agent/tool-gate": {"versions": ["1"]}}}
        self.delay_seconds = delay_seconds
        self.start_error = start_error
        self.rows = [self.row()]
        self.owner = SimpleNamespace(home_dir=Path("isolated-owner"), identity="local/main")
        self.config = SimpleNamespace(plugin_tool_gate_timeout_ms=timeout_ms)
        self.pool = PluginChannelPool(client_factory=lambda _owner, row: _Client(self, row))
        self.reviewer = PluginGateReviewer(GateWiring(
            self.owner, self.config, lambda: self.pool, lambda _owner: tuple(self.rows)))

    @staticmethod
    def row(plugin="guard", activation="act-1"):
        gate = PluginToolGateDeclaration("check", ("gate_probe",), (), "none")
        return SimpleNamespace(manifest=SimpleNamespace(plugin_id=plugin, version="1.0.0", tool_gates=(gate,)),
                               enabled=True, activation=SimpleNamespace(activation_id=activation))

    def answer(self, row, params, timeout):
        del row, params, timeout
        return {"verdict": "allow_as_is", "reason_code": "OK"}

    # 函数用途: 造一次真实 canonical 调用及快照，供 reviewer 或执行器使用。
    def call(self, tmp_path):
        probe = _Probe()
        snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
        return canonical_test_call(snapshot, "gate_probe", {}), probe, snapshot

    # 函数用途: 把一次 canonical 调用包成 reviewer 需要的 GateCall。
    def gate_call(self, tmp_path):
        call, probe, snapshot = self.call(tmp_path)
        return GateCall(call=call, effect="read_only", actor="main", interactive=True), probe, snapshot


# LLM: 走真实执行器与真实 reviewer，不手填 ActionDecision 或合并结果。
# 函数用途: 用真实链路执行一次受门控制的调用并返回三件事实。
def _execute(tmp_path, harness):
    call, probe, snapshot = harness.call(tmp_path)
    execution = ToolExecutor().execute(ToolExecutorRequest(
        call, snapshot, tmp_path, operation_store_required=False,
        plugin_gate_reviewer=harness.reviewer.review))
    return execution, probe


# --- 1. 冷启动撞预算 ---


# LLM: 合并层不得比宿主更松：插件回 allow_as_is、宿主原本 ask 时，最终必须仍是 ask。
#   这条钉住的是 merge 的比较方向（m2 变异就是把它反过来）。
# 函数用途: 断言插件放行不会把宿主的 ask 降级成 allow。
def test_plugin_allow_never_relaxes_host_ask():
    h = _Harness()
    gate_call, _probe, _snapshot = h.gate_call(Path("/tmp"))

    row = h.rows[0]
    declaration = row.manifest.tool_gates[0]
    target = GateTarget(row.manifest.plugin_id, row.manifest.version, row.activation.activation_id, declaration)
    review = GateReview(target, GateReply("allow_as_is", "OK"), "ok")
    assert PluginToolGate.merge("ask", (review,)).status == "ask"
    # 反向对照：宿主 allow + 插件 allow 才是 allow（证明上面的 ask 不是恒真判据）。
    assert PluginToolGate.merge("allow", (review,)).status == "allow"
    assert gate_call.call.tool_name == "gate_probe"


# LLM: 冷启动（首次 request）比预算还慢时，征询必须记 timeout、合并成 ask，handler 一次都不能跑。
#   预算走 plugin_tool_gate_timeout_ms 的同一条读取路径（config 字段名就是 AgentConfig 那个）。
# 函数用途: 断言冷启动超预算不会让工具被放行。
def test_cold_start_over_budget_times_out_and_never_allows(tmp_path):
    h = _Harness(timeout_ms=200, delay_seconds=0.6)
    execution, probe = _execute(tmp_path, h)

    assert execution.decision.status == "ask"
    requirements = execution.decision.evidence["plugin_requirements"]
    assert [item["reason_code"] for item in requirements] == ["PLUGIN_GATE_TIMEOUT"]
    assert probe.executed == [] and execution.result.handler_executed is False
    # 账本记的是 timeout 事实，不是批准。
    decisions = execution.result.metadata.get("plugin_gate_decisions") or []
    assert [item["outcome"] for item in decisions] == ["timeout"]
    assert decisions[0]["final_status"] == "ask"


# LLM: 预算是"一次调用总时间"，不是每次重试各给一份；慢启动一律落 timeout 而不是 ok。
# 函数用途: 直接从 reviewer 取征询事实，核对 timeout 与 ask。
def test_budget_covers_slow_start(tmp_path):
    h = _Harness(timeout_ms=200, delay_seconds=0.6)
    gate_call, _probe, _snapshot = h.gate_call(tmp_path)
    reviews = h.reviewer.review(gate_call)
    assert [item.outcome for item in reviews] == ["timeout"]
    assert reviews[0].reply.verdict == "ask"


# --- 2. 运行中沙箱起不来 ---


# LLM: 沙箱起不来时池把启动失败包成 PluginChannelStartFailed（连接级故障）；征询侧必须变成 ask，
#   既不放行也不当成"没有插件"。真 Seatbelt/bwrap 由 3a 在沙箱外核，这里只钉征询侧结论。
# 函数用途: 断言启动失败收紧为 ask 且账本留痕。
def test_start_failure_is_ask_and_ledgered(tmp_path):
    h = _Harness(start_error=PluginChannelStartFailed("插件连接启动失败"))
    execution, probe = _execute(tmp_path, h)

    assert execution.decision.status == "ask"
    requirements = execution.decision.evidence["plugin_requirements"]
    # 照代码现状：启动失败落在通用 error 分支，原因码是 PLUGIN_GATE_* 一族。
    assert requirements[0]["reason_code"].startswith("PLUGIN_GATE_")
    assert probe.executed == [] and execution.result.handler_executed is False
    decisions = execution.result.metadata.get("plugin_gate_decisions") or []
    assert decisions and decisions[0]["outcome"] != "ok"
    assert decisions[0]["final_status"] == "ask"


# --- 3. 进程被杀后下一次征询 ---


# LLM: 连接已建、transport 发布过（进程被杀前确实跑过一轮），之后 transport 抛连接类错误；
#   池按连接级故障退避，征询侧收成 ask，不沿用上一次的 allow 结论。
# 函数用途: 断言进程被杀后的下一次征询仍要问本人，且连接被标记退避。
def test_next_review_after_process_killed_is_ask(tmp_path):
    h = _Harness()
    gate_call, _probe, _snapshot = h.gate_call(tmp_path)

    first = h.reviewer.review(gate_call)
    assert [item.outcome for item in first] == ["ok"]
    assert [item.reply.verdict for item in first] == ["allow_as_is"]

    for connection in tuple(h.pool._connections.values()):
        connection.transport.client.killed = True

    second = h.reviewer.review(gate_call)
    assert [item.outcome for item in second] != ["ok"]
    assert second[0].reply.verdict == "ask"
    assert any(connection.backoff_until > 0 for connection in h.pool._connections.values())


# LLM: 被杀之后要走真实执行器：第一次（进程活着）插件答 allow_as_is 所以放行，
#   进程被杀后同一条链的**下一次**执行必须回到 ask 且 handler 未执行。
# 函数用途: 断言进程被杀后的执行器仍要求确认。
def test_executor_after_kill_still_requires_user(tmp_path):
    h = _Harness()
    first, probe = _execute(tmp_path, h)
    assert first.decision.status == "allow"
    assert len(probe.executed) == 1

    for connection in tuple(h.pool._connections.values()):
        connection.transport.client.killed = True

    second, probe2 = _execute(tmp_path, h)
    assert second.decision.status == "ask"
    assert probe2.executed == [] and second.result.handler_executed is False
    requirements = second.decision.evidence["plugin_requirements"]
    assert requirements[0]["reason_code"].startswith("PLUGIN_GATE_")
