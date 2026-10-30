# LLM: 任务边界只能收紧宿主限流；回归沿真实注册表和执行器调用，不替换限流裁决。
# 模块用途: 验证零值绕过、窗口与退避交集、记录保留和严格输入拒绝。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.registry_rate_limit_policy import tool_rate_limit_policy
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    execute_registry_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


# LLM: 限流记录和门禁采用同一固定时钟，不让注册表构造耗时影响短退避断言。
# 函数用途: 提供本测试文件专用的确定性秒数。
def _clock_now():
    return 1000.0


# LLM: 只替换门禁模块的时钟依赖，不改变全局系统时间、执行器或限流算法。
# 函数用途: 为每个用例冻结门禁观察点并由测试夹具自动恢复。
@pytest.fixture(autouse=True)
def _freeze_gate_clock(monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.tooling.action_policy.time", SimpleNamespace(time=_clock_now))


# LLM: 本地只读探针仍走生产校验及限流门，调用计数区分拒绝与实际进入处理器。
# 类用途: 提供没有文件或网络副作用的真实工具处理器。
class RateProbe(BaseTool):
    model_spec = make_test_model_spec("rate_probe")
    runtime_policy = make_test_runtime_policy("read_only")

    # LLM: 每次执行入口使用独立计数，不借助跨测试全局状态。
    # 函数用途: 初始化处理器实际调用次数。
    def __init__(self):
        self.calls = 0

    # LLM: 只有执行器真正放行才增加计数，不伪造门禁结果。
    # 函数用途: 返回确定性的只读成功结果。
    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome("rate_probe", True, "已执行")


# LLM: 宿主正上限与非空退避由正式类型校验，避免测试桩绕过部署合同。
# 函数用途: 构造便于辨别宿主和任务边界的冻结策略。
def _host(**changes):
    return RuntimeGuardPolicy(**{
        "tool_rate_max_calls": 12,
        "tool_rate_window_seconds": 60,
        "tool_circuit_failure_threshold": 3,
        "tool_circuit_backoff_seconds": (10, 20, 30),
        "tool_rate_max_records": 4,
        **changes,
    })


# LLM: 参数哈希取生产函数；历史记录模拟上游账本输入，不替代真实 gate。
# 函数用途: 为同一工具参数构造限流或熔断历史。
def _record(**changes):
    return {"tool_name": "rate_probe", "args_hash": tool_arguments_hash({}), **changes}


# LLM: 两个入口都携带冻结宿主策略，注册表使用正式构造与标准调用；不运行外部工具。
# 函数用途: 返回实际执行结果和处理器，验证限流发生于处理器之前。
def _execute(tmp_path, entry, host, values, records=(), probe=None):
    tool = probe if probe is not None else RateProbe()
    boundary = {"tool_rate_limit_policy": values, "tool_rate_limit_records": list(records)}
    if entry == "executor":
        result = execute_canonical_test_call(
            tmp_path, tools={"rate_probe": tool}, tool_name="rate_probe", arguments={},
            write_boundary=boundary, runtime_guard_policy=host,
        ).result
    else:
        registry = ToolRegistry(ToolRegistryParams(
            workspace_root=tmp_path, max_chars=1000, max_entries=20, max_matches=20,
            web_max_chars=1000, http_timeout=5, catalog_limit=20, retrieval_limit=3,
            vector_search_enabled=False, defer_mcp_connect=True,
            runtime_guard_policy=host, operation_store_required=False,
        ))
        try:
            registry.register(tool)
            result = execute_registry_test_call(
                registry, "rate_probe", {}, allowed_tools=["rate_probe"], write_boundary=boundary,
            )
        finally:
            registry.close_mcp_clients()
    return result, tool


# LLM: 零值和过大阈值均不能关闭或放宽宿主调用次数上限。
# 函数用途: 从两个生产入口复现原始零值绕过并断言未进入处理器。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("boundary_calls", [0, 1000])
def test_boundary_cannot_disable_or_raise_host_call_limit(tmp_path, entry, boundary_calls):
    record = _record(attempt_timestamps=[_clock_now() - 1] * 12)
    result, tool = _execute(tmp_path, entry, _host(), {"max_calls": boundary_calls}, [record])
    assert result.ok is False
    assert result.error_code == "TOOL_RATE_LIMIT_EXCEEDED"
    assert result.handler_executed is False
    assert tool.calls == 0


# LLM: task 正整数可收紧有限和无限宿主，上限交集不能变成总是保留宿主。
# 函数用途: 验证正值收紧在两个入口真正阻止超额调用。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("host_calls", [0, 12])
def test_positive_boundary_tightens_finite_and_unlimited_hosts(tmp_path, entry, host_calls):
    record = _record(attempt_timestamps=[_clock_now() - 1] * 2)
    result, tool = _execute(
        tmp_path, entry, _host(tool_rate_max_calls=host_calls), {"max_calls": 2}, [record],
    )
    assert result.error_code == "TOOL_RATE_LIMIT_EXCEEDED"
    assert result.handler_executed is False
    assert tool.calls == 0


# LLM: 统计窗口缩短或归零都会忘掉宿主有效历史，因此窗口只能取更大值。
# 函数用途: 验证早于任务短窗口但仍在宿主窗口内的记录继续阻断。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("window", [0, 1, 120])
def test_window_boundary_cannot_forget_host_attempts(tmp_path, entry, window):
    record = _record(attempt_timestamps=[_clock_now() - 30] * 12)
    result, tool = _execute(tmp_path, entry, _host(), {"window_seconds": window}, [record])
    assert result.error_code == "TOOL_RATE_LIMIT_EXCEEDED"
    assert tool.calls == 0
    finding = result.metadata["action_decision"]["evidence"]["gate"]["findings"][0]
    assert finding["evidence"]["window_seconds"] == max(60, window)


# LLM: 熔断阈值的零值表示关闭；较大正值同样不得推迟宿主触发。
# 函数用途: 验证失败阈值与退避零值不能让原本熔断的调用进入处理器。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("threshold", [0, 1000])
def test_boundary_cannot_disable_host_circuit(tmp_path, entry, threshold):
    record = _record(consecutive_failures=3, last_failure_at=_clock_now() - 1)
    result, tool = _execute(tmp_path, entry, _host(), {
        "failure_threshold": threshold, "backoff_schedule_seconds": [0],
    }, [record])
    assert result.error_code == "TOOL_CIRCUIT_OPEN"
    assert result.handler_executed is False
    assert tool.calls == 0


# LLM: 退避按绝对连续失败数求交集，末档重复且不同长度不能截短宿主长退避。
# 函数用途: 验证任务短序列与降低熔断阈值不会错位绕过宿主等待。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("failures,schedule,threshold", [
    (5, [1], 3), (3, [1], 1), (4, [0, 0, 0, 0], 1),
])
def test_backoff_intersection_uses_real_gate_index(tmp_path, entry, failures, schedule, threshold):
    host = _host(tool_circuit_backoff_seconds=(30, 1, 20))
    host_wait = (30, 1, 20)[min(failures - 3, 2)]
    record = _record(consecutive_failures=failures, last_failure_at=_clock_now() - host_wait / 2)
    result, tool = _execute(tmp_path, entry, host, {
        "failure_threshold": threshold, "backoff_schedule_seconds": schedule,
    }, [record])
    assert result.error_code == "TOOL_CIRCUIT_OPEN"
    assert result.handler_executed is False
    assert tool.calls == 0


# LLM: max_records 是保护历史保留容量而不是调用额度；减小容量会驱逐应被保护的键。
# 函数用途: 验证边界不得通过记录驱逐绕过宿主限流。
@pytest.mark.parametrize("entry", ["registry", "executor"])
def test_boundary_cannot_evict_host_protection_records(tmp_path, entry):
    records = [_record(attempt_timestamps=[_clock_now() - 1] * 12),
               _record(tool_name="other_probe", args_hash="other")]
    result, tool = _execute(tmp_path, entry, _host(), {"max_records": 1}, records)
    assert result.error_code == "TOOL_RATE_LIMIT_EXCEEDED"
    assert result.handler_executed is False
    assert tool.calls == 0


# LLM: 合法无历史调用仍须成功，拒门测试不能以破坏所有调用变绿。
# 函数用途: 验证空边界和双零宿主保留正常执行语义。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("unlimited", [False, True])
def test_valid_call_still_executes(tmp_path, entry, unlimited):
    host = _host(tool_rate_max_calls=0, tool_circuit_failure_threshold=0) if unlimited else _host()
    records = [_record(attempt_timestamps=[_clock_now() - 1] * 12,
                       consecutive_failures=3, last_failure_at=_clock_now() - 1)] if unlimited else []
    result, tool = _execute(tmp_path, entry, host, {}, records)
    assert result.ok is True
    assert result.handler_executed is True
    assert tool.calls == 1


# LLM: 缺省、零值与正值各自有精确语义，不用真假值猜测或强制转换。
# 函数用途: 检查字段交集、宿主不变及任务退避末档重复使用。
def test_policy_intersection_and_inputs_remain_immutable():
    host = _host()
    values = {"max_calls": 0, "failure_threshold": 8, "window_seconds": 0,
              "max_records": 1, "backoff_schedule_seconds": [0, 40]}
    policy = tool_rate_limit_policy({"tool_rate_limit_policy": values}, host)
    assert (policy.max_calls, policy.failure_threshold, policy.window_seconds, policy.max_records) == (12, 3, 60, 4)
    assert policy.backoff_schedule_seconds == (10, 40, 40)
    assert host.tool_circuit_backoff_seconds == (10, 20, 30)
    assert values["backoff_schedule_seconds"] == [0, 40]
    larger = tool_rate_limit_policy({"tool_rate_limit_policy": {"max_records": 8}}, host)
    assert larger.max_records == 8
    assert tool_rate_limit_policy(None, host) == tool_rate_limit_policy({}, host)


# LLM: 非整数、真假值、负数、非有限数字和部分损坏序列必须整体验证失败，不能静默跳过。
# 函数用途: 锁定所有边界字段的严格类型与范围。
@pytest.mark.parametrize("values", [
    {"max_calls": True}, {"max_calls": "0"}, {"max_calls": 1.5}, {"max_calls": -1},
    {"max_calls": None}, {"failure_threshold": False}, {"failure_threshold": -1},
    {"window_seconds": False}, {"window_seconds": "0"}, {"window_seconds": -1},
    {"window_seconds": float("nan")}, {"window_seconds": float("inf")},
    {"window_seconds": 10 ** 1000},
    {"max_records": 0}, {"max_records": 1.5}, {"max_records": True},
    {"backoff_schedule_seconds": []}, {"backoff_schedule_seconds": "1,2"},
    {"backoff_schedule_seconds": [1, "bad", 2]}, {"backoff_schedule_seconds": [False]},
    {"backoff_schedule_seconds": [-1]}, {"backoff_schedule_seconds": [float("nan")]},
    {"backoff_schedule_seconds": [float("inf")]}, {"unknown": 1},
])
def test_policy_boundary_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        tool_rate_limit_policy({"tool_rate_limit_policy": values}, _host())


# LLM: 显式提供错误容器不等于缺省；损坏的策略节点不能被默默当成空策略。
# 函数用途: 检查边界节点结构的严格拒绝。
@pytest.mark.parametrize("boundary", [[], "bad", {"tool_rate_limit_policy": None},
                                            {"tool_rate_limit_policy": []}])
def test_policy_boundary_rejects_invalid_structure(boundary):
    with pytest.raises(ValueError):
        tool_rate_limit_policy(boundary, _host())


# LLM: 非法任务输入在真实入口也必须先于处理器失败，异常不能变成成功执行。
# 函数用途: 验证严格校验不只存在于直接辅助函数调用。
@pytest.mark.parametrize("entry", ["registry", "executor"])
def test_invalid_boundary_fails_before_handler(tmp_path, entry):
    probe = RateProbe()
    with pytest.raises(ValueError):
        _execute(tmp_path, entry, _host(), {"max_calls": True}, probe=probe)
    assert probe.calls == 0


# LLM: 任务较小正阈值既能提前宿主熔断，也能启用宿主原本关闭的保护。
# 函数用途: 从两个入口核对有限和零宿主失败阈值的正值收紧。
@pytest.mark.parametrize("entry", ["registry", "executor"])
@pytest.mark.parametrize("host_threshold", [0, 3])
def test_positive_boundary_tightens_circuit(tmp_path, entry, host_threshold):
    record = _record(consecutive_failures=2, last_failure_at=_clock_now() - 1)
    result, probe = _execute(
        tmp_path, entry, _host(tool_circuit_failure_threshold=host_threshold),
        {"failure_threshold": 2}, [record],
    )
    assert result.error_code == "TOOL_CIRCUIT_OPEN"
    assert result.handler_executed is False
    assert probe.calls == 0


# LLM: 大阈值差不能生成十亿个退避档位；紧凑末档仍须覆盖所有未来宿主等待。
# 函数用途: 用真实 gate 索引验证退避合并有界且没有缩短保护。
def test_large_threshold_shift_keeps_bounded_protective_schedule():
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import backoff_seconds

    host = _host(tool_circuit_failure_threshold=10 ** 9, tool_circuit_backoff_seconds=(30, 1, 20))
    merged = tool_rate_limit_policy({"tool_rate_limit_policy": {
        "failure_threshold": 1, "backoff_schedule_seconds": [0],
    }}, host)
    assert len(merged.backoff_schedule_seconds) <= 2 * len(host.tool_circuit_backoff_seconds)
    for offset, wait in enumerate(host.tool_circuit_backoff_seconds):
        assert backoff_seconds(merged, host.tool_circuit_failure_threshold + offset) >= wait


@pytest.mark.parametrize("entry", ["registry", "executor"])
def test_empty_boundary_record_cannot_shadow_persisted_runtime_ledger_history(tmp_path, entry):
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )

    args_hash = tool_arguments_hash({})
    persisted_records = [
        SimpleNamespace(
            tool="rate_probe",
            args_hash=args_hash,
            created_at=_clock_now() - 1,
            updated_at=_clock_now() - 1,
            status="failed",
        )
        for _ in range(3)
    ]
    store = SimpleNamespace(list_runtime_gate_ledger=lambda **_kwargs: persisted_records)
    agent = SimpleNamespace(local_store=store, owner_policy=None, home_paths=None, config=None)
    raw_boundary = {
        "tool_rate_limit_policy": {"failure_threshold": 3, "backoff_schedule_seconds": [30]},
        "tool_rate_limit_records": [
            {
                "tool_name": "rate_probe",
                "args_hash": args_hash,
                "attempt_timestamps": [],
                "consecutive_failures": 0,
                "total_failures": 0,
                "last_failure_at": 0.0,
                "last_success_at": 0.0,
            }
        ],
    }
    merged_boundary = write_boundary_with_runtime_ledger(
        agent,
        SimpleNamespace(write_boundary=raw_boundary, run_id="run-rate-shadow"),
    )
    result, probe = _execute(
        tmp_path,
        entry,
        _host(tool_circuit_failure_threshold=3, tool_circuit_backoff_seconds=(30,)),
        merged_boundary["tool_rate_limit_policy"],
        merged_boundary["tool_rate_limit_records"],
    )
    assert result.ok is False
    assert result.error_code == "TOOL_CIRCUIT_OPEN"
    assert result.handler_executed is False
    assert probe.calls == 0
