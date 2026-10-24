# LLM: 使用真实上下文计量入口与同步屏障，不访问模型网络或持久宿主状态。
# 模块用途: 验证请求局部预算single-flight、失败重试与副本隔离。
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from agent_py_agent.agent.agent_core.model import context_pressure
from agent_py_agent.tests.test_model_latency_q2 import _context_case


# LLM: 可落盘state不能承载memo；现有热缓存行为保持，私有holder取代旧state数字缓存。
# 函数用途: 暴露旧计量缓存仍写入归档state的问题。
def test_estimate_memo_is_not_archive_state():
    agent, params = _context_case()
    context_pressure._model_visible_context_components(agent, params, "prompt")
    assert "_last_estimate_cache" not in params.live_archive_state
    assert json.dumps(params.live_archive_state) == "{}"


# LLM: producer和waiter均报到后才释放计算，断言真正合流而非恰好热命中。
# 函数用途: 用16真实线程检查冷miss单飞以及异常传播/重试。
@pytest.mark.parametrize("fail", [False, True])
def test_same_key_singleflight_and_failure_retry(monkeypatch, fail):
    from concurrent.futures import Future

    from agent_py_agent.agent.agent_core.model import context_estimate_cache as cache
    agent, params = _context_case()
    start, ready, release, lock = Barrier(16), Event(), Event(), Lock()
    participants, calculations = [], []
    should_fail = [fail]
    original = context_pressure.projected_model_context_components
    error = ValueError("fixture failure")

    # LLM: 计算者和等待者各报到一次，既不过早释放也不靠时间推断是否等待。
    # 函数用途: 统计进入单飞计算/等待的线程数。
    def arrive():
        with lock:
            participants.append(1)
            if len(participants) == 16:
                ready.set()

    # LLM: 替换的是Future观察点，实际等待和异常传播仍由原Future完成。
    # 类用途: 给测试提供同键等待者已加入的证据。
    class ObservedFuture(Future):
        # LLM: 只对waiter计数；不持memo锁等待。
        # 函数用途: 报到后进入标准Future等待。
        def result(self, timeout=None):
            arrive()
            return super().result(timeout)

    # LLM: 保留真实组件计算，只在其前加入可控冷miss窗口。
    # 函数用途: 统计昂贵计量实际次数。
    def measured(*args, **kwargs):
        calculations.append(1)
        arrive()
        assert release.wait(5)
        if should_fail[0]:
            raise error
        return original(*args, **kwargs)

    # LLM: 各线程同时调用原计量入口。
    # 函数用途: 启动同键估算。
    def invoke():
        start.wait(5)
        return context_pressure._model_visible_context_components(agent, params, "prompt")

    monkeypatch.setattr(cache, "Future", ObservedFuture)
    monkeypatch.setattr(context_pressure, "projected_model_context_components", measured)
    with ThreadPoolExecutor(16) as pool:
        futures = [pool.submit(invoke) for _ in range(16)]
        try:
            assert ready.wait(5)
        finally:
            release.set()
        if fail:
            assert_same_failure(futures, error)
        else:
            values = [future.result(5) for future in futures]
            values[0][2]["pollution"] = 1
            assert all("pollution" not in value[2] for value in values[1:])
    assert len(calculations) == 1
    should_fail[0] = False
    assert context_pressure._model_visible_context_components(agent, params, "prompt")[1] > 0
    assert len(calculations) == 1 + int(fail)


# LLM: 逐个读取真实Future的错误对象，断言同一次producer失败传播给全部等待者。
# 函数用途: 检查单飞失败身份且避免测试逻辑过深嵌套。
def assert_same_failure(futures, error):
    for future in futures:
        with pytest.raises(ValueError) as caught:
            future.result(5)
        assert caught.value is error


# LLM: 不同键计算须能并行；旧键晚完成只允许造成miss，不得让新键命中旧值。
# 函数用途: 检查跨key锁范围与捕获key的发布。
def test_different_keys_compute_concurrently_and_late_old_key_is_safe():
    from agent_py_agent.agent.agent_core.model.context_estimate_cache import ContextEstimateMemo
    memo, entered, release = ContextEstimateMemo(), Event(), Event()

    # LLM: A计算未结束时B必须独立完成，不按耗时推断锁未覆盖计算。
    # 函数用途: 延迟旧key发布。
    def older():
        entered.set()
        assert release.wait(5)
        return "native", 1, {"A": 1}, "a"

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(memo.evaluate, "A", older)
        try:
            assert entered.wait(5)
            result = pool.submit(memo.evaluate, "B", lambda: ("native", 2, {"B": 2}, "b")).result(5)
            assert result == ("native", 2, {"B": 2}, "b")
        finally:
            release.set()
        assert first.result(5)[3] == "a"
    assert memo.evaluate("B", lambda: ("native", 2, {"B": 2}, "b")) == result


# LLM: key=None不得合流/覆盖有效热结果；原计算字典修改也不能污染冻结值。
# 函数用途: 验证无法证明可缓存时的保守路径。
def test_uncacheable_keys_and_component_snapshots():
    from agent_py_agent.agent.agent_core.model.context_estimate_cache import ContextEstimateMemo
    memo, barrier, source = ContextEstimateMemo(), Barrier(2), {"kept": 1}
    memo.evaluate("warm", lambda: ("native", 1, source, "same"))
    source["changed"] = 2

    # LLM: 两计算须同时进入，证明None键没有等待同一Future。
    # 函数用途: 生成不可缓存估算。
    def compute():
        barrier.wait(5)
        return "text", 3, {"uncached": 3}, "none"

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(memo.evaluate, None, compute) for _ in range(2)]
        assert all(future.result(5)[3] == "none" for future in futures)
    assert memo.evaluate("warm", lambda: pytest.fail("None计算覆盖热键"))[2] == {"kept": 1}
    with pytest.raises(RuntimeError, match="自等待"):
        memo.evaluate("reentrant", lambda: memo.evaluate("reentrant", compute))
    assert memo.evaluate("reentrant", lambda: ("native", 4, {}, "retry"))[1] == 4


# LLM: 真实runtime字段default_factory按请求独立，兼容Namespace并发初始化也只能有一个holder。
# 函数用途: 核对memo所有权及初始化竞争。
def test_runtime_field_and_legacy_initialization_are_private():
    from dataclasses import fields
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.model.context_estimate_cache import estimate_memo_for
    field = next(item for item in fields(ToolLoopExecuteParams) if item.name == "_context_estimate_memo")
    assert not field.repr and not field.compare and field.default_factory() is not field.default_factory()
    params, barrier = SimpleNamespace(live_archive_state={}), Barrier(16)

    # LLM: 只让初始化入口同时竞争，不用计时窗口凑race。
    # 函数用途: 取得旧替身的同一私有holder。
    def invoke():
        barrier.wait(5)
        return estimate_memo_for(params)

    with ThreadPoolExecutor(16) as pool:
        results = [future.result(5) for future in [pool.submit(invoke) for _ in range(16)]]
    assert all(result is results[0] for result in results) and params.live_archive_state == {}


# LLM: 沿原完整键做真实miss回归，覆盖指导/压缩/media和scope而非只测试holder字符串键。
# 函数用途: 验证消息和估算器任一变化不会复用旧组件。
@pytest.mark.parametrize("change", ["compact", "guidance", "media", "classifier", "request", "run"])
def test_complete_context_key_invalidations(monkeypatch, change):
    agent, params = _context_case()
    calls = []
    original = context_pressure.projected_model_context_components

    # LLM: 真实组件计算保留，仅记录昂贵函数入口。
    # 函数用途: 观察key变化后的实际重算。
    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(context_pressure, "projected_model_context_components", measured)
    context_pressure._model_visible_context_components(agent, params, "prompt")
    context_pressure._model_visible_context_components(agent, params, "prompt")
    assert len(calls) == 1
    reserve = context_pressure.media_token_reserve()
    classify = context_pressure.classify_nontext_content
    mutations = {
        "compact": params.provider_history_messages.clear,
        "guidance": lambda: params.tool_context.append("宿主新增指导"),
        "media": lambda: monkeypatch.setattr(context_pressure, "media_token_reserve", lambda: reserve + 1),
        "classifier": lambda: monkeypatch.setattr(context_pressure, "classify_nontext_content", lambda value: classify(value)),
        "request": lambda: setattr(params, "request_id", "other"),
        "run": lambda: setattr(params, "run_id", "other"),
    }
    mutations[change]()
    context_pressure._model_visible_context_components(agent, params, "prompt")
    assert len(calls) == 2
