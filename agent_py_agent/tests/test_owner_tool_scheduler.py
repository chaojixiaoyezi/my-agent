# LLM: 测试只创建临时宿主与受控线程；Event/Barrier决定先后，不靠sleep猜调度。
# 模块用途: 验证同owner跨Agent额度、排队取消、公平性和嵌套身份。
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import round_execution as rounds
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.tests.test_tool_round_execution import _round_request, _success


# LLM: 同owner两个Agent走真实parallel入口；第一段占满许可后第二段不得执行handler。
# 函数用途: 暴露原per-segment线程池不共享owner额度的问题。
def test_rounds_share_owner_limit_and_cancel_queued(tmp_path):
    entered, release = Event(), Event()
    token = CancellationToken()
    observed = []

    # LLM: handler保留结果配对，只有第一段阻塞，第二段若越额度会留下可见记录。
    # 函数用途: 记录实际开始的调用。
    def execute(work):
        observed.append(work.call.call_id)
        if work.call.call_id.endswith("1"):
            entered.set()
            assert release.wait(5)
        return _success(work, "done")

    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            config=SimpleNamespace(max_parallel_tool_calls_per_owner=1))
    first = _round_request(agent=agent, params=SimpleNamespace(tool_context=[]), tool_rounds=1,
                           response=None, calls=[{"tool": "read"}], execute_one=execute, record_one=lambda _: None)
    second = _round_request(agent=agent, params=SimpleNamespace(tool_context=[], cancellation_token=token),
                            tool_rounds=1, response=None, calls=[{"tool": "other"}],
                            execute_one=execute, record_one=lambda _: None)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(rounds._execute_parallel_segment, first, first.calls, start_idx=1)
        assert entered.wait(5)
        token.cancel()
        b = pool.submit(rounds._execute_parallel_segment, second, second.calls, start_idx=1)
        release.set()
        one, two = a.result(5), b.result(5)
    assert one[0][3].result.ok
    assert two[0][3].result.error_code == "CANCELLED"
    assert len(observed) == 1


# LLM: 这些测试在模块存在后才导入新API；初始失败必须由上面的真实旧入口断言确认。
# 函数用途: 获取新调度器的公开构造接口。
def scheduler_api():
    from agent_py_agent.agent.tooling.owner_tool_scheduler import OwnerToolScheduler, ToolWorkItem
    return OwnerToolScheduler, ToolWorkItem


# LLM: FIFO是入队顺序，不要求并行完成顺序；上限1使启动顺序可确定。
# 函数用途: 验证独立Context、异常释放、队列顺序和同许可内嵌套返回。
def test_scheduler_fifo_context_exception_and_nested(tmp_path):
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 1)
    entered, release = Event(), Event()
    marker = ContextVar("test_owner_marker", default="none")
    seen = []

    # LLM: 第一项占许可并改变私有Context，后续不能继承此变更。
    # 函数用途: 构造确定性排队窗口。
    def first():
        marker.set("changed")
        entered.set()
        assert release.wait(5)
        raise ValueError("fixture")

    first_future = scheduler.submit(Item("first", first))
    assert entered.wait(5)
    futures = []
    for number in range(2):
        marker.set(str(number))
        futures.append(scheduler.submit(Item(str(number), lambda: seen.append(marker.get()))))
    release.set()
    with pytest.raises(ValueError, match="fixture"):
        first_future.result(5)
    for future in futures:
        future.result(5)
    assert seen == ["0", "1"] and marker.get() == "1"
    parent = scheduler.submit(Item("parent", lambda: scheduler.submit(Item("nested", lambda: 42)).result(1)))
    assert parent.result(5) == 42


# LLM: 真tool_program原执行器派生子调用；cap1时子handler必须在父线程内返回。
# 函数用途: 暴露nested另建池而逃出父许可的问题。
def test_nested_dispatch_runs_inline_in_owner_worker(tmp_path):
    import threading

    from agent_py_agent.tests.test_tool_program import Probe, run
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 1)
    threads = []

    # LLM: 仅记录实际handler线程，不替代原executor或权限检查。
    # 类用途: 为现有Probe提供线程观察器。
    class Observer:
        # LLM: 原Probe仅调用wait，此处不等待其它worker避免把测试写成必死锁。
        # 函数用途: 捕获真正执行子handler的线程。
        def wait(self, timeout):
            threads.append(threading.get_ident())

    # LLM: 父工作项使用规范owner根，子请求沿宿主owner_scope_root，不从参数借权。
    # 函数用途: 执行真实两子项嵌套批次。
    def parent():
        identity = threading.get_ident()
        outcome, _ = run(tmp_path, [{"id": "reads", "calls": [
            {"tool": "probe", "arguments": {"value": n}} for n in range(2)]}],
            probe=Probe(gate=Observer()), owner_scope_root=str(tmp_path))
        assert outcome.ok, outcome.output
        return identity

    parent_thread = scheduler.submit(Item("parent", parent)).result(5)
    assert threads == [parent_thread, parent_thread]


# LLM: 精确观察第二个Future的等待入口与执行轮退出，不靠耗时猜仍有在途调用。
# 函数用途: 复现首个异常使批次提前退出。
def test_round_exception_waits_for_other_inflight(tmp_path, monkeypatch):
    from concurrent.futures import Future

    from agent_py_agent.agent.tooling import owner_tool_scheduler as module
    entered, release, decided, waiting = Barrier(2), Event(), Event(), Event()
    counter = []

    # LLM: 真实Future执行与唤醒保持，只有第二项result入口增加观察点。
    # 类用途: 判断异常处理是否等待本批未完成项。
    class ObservedFuture(Future):
        # LLM: submit串行创建Future；编号不涉及handler身份。
        # 函数用途: 识别批次第二个Future。
        def __init__(self):
            super().__init__()
            self.number = len(counter)
            counter.append(self)

        # LLM: 尚未完成的第二项调用result即证明轮次选择了等待而非直接退出。
        # 函数用途: 记录等待观察点。
        def result(self, timeout=None):
            if self.number == 1:
                waiting.set()
                decided.set()
            return super().result(timeout)

    # LLM: 第一项异常只在两handler均已进入后发生，第二项保持未完成。
    # 函数用途: 创造可复现的在途异常窗口。
    def execute(work):
        entered.wait(5)
        if work.idx == 1:
            raise ValueError("first failed")
        assert release.wait(5)
        return _success(work, "done")

    agent = SimpleNamespace(root=tmp_path, config=SimpleNamespace(max_parallel_tool_calls_per_owner=2))
    request = _round_request(agent=agent, params=SimpleNamespace(tool_context=[]), tool_rounds=1,
                            response=None, calls=[{"tool": "a"}, {"tool": "b"}],
                            execute_one=execute, record_one=lambda _: None)
    monkeypatch.setattr(module, "Future", ObservedFuture)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(rounds._execute_parallel_segment, request, request.calls, start_idx=1)
        future.add_done_callback(lambda _: decided.set())
        try:
            assert decided.wait(5)
            assert waiting.is_set(), "首个异常传播前没有等待其余在途项"
        finally:
            release.set()
        with pytest.raises(ValueError, match="first failed"):
            future.result(5)


# LLM: 等待抛异常后不实际再阻塞测试；检查下一票是否仍可入队，原缺口会使谓词为False。
# 函数用途: 复现背压异常留下无法越过的废弃票号。
def test_abandoned_submission_does_not_block_next_ticket(tmp_path, monkeypatch):
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 1)
    original = scheduler._condition.wait_for
    error = KeyboardInterrupt("queue wait")

    # LLM: 在首个背压等待点定向抛异常，不触碰handler或Future。
    # 函数用途: 模拟等待者被真实异常打断。
    def interrupt(predicate):
        raise error

    monkeypatch.setattr(scheduler._condition, "wait_for", interrupt)
    with pytest.raises(KeyboardInterrupt):
        scheduler.submit(Item("interrupted", lambda: None))

    # LLM: 有界断言代替永久等待，只有票号谓词不被废弃票卡住才进入真实wait_for。
    # 函数用途: 检查后继提交已恢复可前进性。
    def assert_ready(predicate):
        assert predicate(), "废弃提交阻塞了后续票号"
        return original(predicate)

    monkeypatch.setattr(scheduler._condition, "wait_for", assert_ready)
    assert scheduler.submit(Item("next", lambda: 42)).result(5) == 42


# LLM: 降cap不能撤销已执行项，但之后不得让超额旧worker再领取新项。
# 函数用途: 检查忙碌期间较小额度的后续启动约束。
def test_lower_cap_stops_excess_worker_from_dequeuing(tmp_path):
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 2)
    entered, release_a, release_b, queued = Barrier(3), Event(), Event(), Event()

    # LLM: 启动两个真实worker，各有独立可控退出点。
    # 函数用途: 保持降cap时已有两项在执行。
    def block(release):
        entered.wait(5)
        assert release.wait(5)

    first = scheduler.submit(Item("a", lambda: block(release_a)))
    second = scheduler.submit(Item("b", lambda: block(release_b)))
    entered.wait(5)
    pending = scheduler.submit(Item("pending", queued.set))
    Scheduler.for_owner(tmp_path, 1)
    try:
        release_a.set()
        first.result(5)
        # 等worker实际退出，不按Future完成推断许可已经归还。
        with scheduler._condition:
            assert scheduler._condition.wait_for(lambda: scheduler._active == 1 or queued.is_set(), 5)
        assert not queued.is_set(), "降cap后超额worker仍启动下一项"
    finally:
        release_a.set()
        release_b.set()
    second.result(5)
    pending.result(5)


# LLM: 同根的两个独立Agent/请求同时走真实段入口，启动屏障证明达到上限而不超上限。
# 函数用途: 验证1/2/4额度的共享峰值、结果配对与逐项Context隔离。
@pytest.mark.parametrize("cap", [1, 2, 4])
def test_two_agents_rounds_share_peak_and_context(tmp_path, cap):
    from agent_py_agent.agent.tooling.owner_tool_scheduler import scheduler_for_agent
    ready, release, lock = Event(), Event(), Lock()
    marker = ContextVar("round_origin")
    active, peak, seen = [0], [0], []

    # LLM: 只有实际handler计数，不用已提交Future数冒充并发执行数。
    # 函数用途: 观察并发上限与调用者Context。
    def execute(work):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            seen.append(marker.get())
            if active[0] >= cap:
                ready.set()
        try:
            assert release.wait(5)
            marker.set("worker mutation")
            return _success(work, str(work.idx))
        finally:
            with lock:
                active[0] -= 1

    agents = [SimpleNamespace(root=tmp_path, config=SimpleNamespace(max_parallel_tool_calls_per_owner=cap))
              for _ in range(2)]
    assert scheduler_for_agent(agents[0]) is scheduler_for_agent(agents[1])

    # LLM: 两个请求有各自身份；不直接调用scheduler替代接线验证。
    # 函数用途: 从一条请求线程执行一段工具。
    def run(number):
        marker.set(str(number))
        request = _round_request(agent=agents[number], params=SimpleNamespace(tool_context=[]), tool_rounds=1,
            response=None, calls=[{"tool": "read"} for _ in range(6)], execute_one=execute, record_one=lambda _: None)
        rows = rounds._execute_parallel_segment(request, request.calls, start_idx=1)
        assert [row[1] for row in rows] == request.calls
        assert [row[0] for row in rows] == list(range(1, 7))

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, number) for number in range(2)]
        try:
            assert ready.wait(5)
            assert peak[0] == cap
        finally:
            release.set()
        for future in futures:
            future.result(5)
    assert peak[0] == cap and sorted(seen) == ["0"] * 6 + ["1"] * 6


# LLM: 不同owner的许可和队列互不耦合；复制Context到外线程也不能冒认父worker。
# 函数用途: 验证owner隔离与跨线程许可不可转移。
def test_distinct_owners_and_copied_permit_are_isolated(tmp_path):
    from contextvars import copy_context

    from agent_py_agent.agent.tooling.owner_tool_scheduler import current_owner_scheduler
    Scheduler, Item = scheduler_api()
    first, second = Scheduler.for_owner(tmp_path / "a", 1), Scheduler.for_owner(tmp_path / "b", 1)
    entered, release = Event(), Event()

    # LLM: 父许可只在原线程有效，同步复制到外线程后必须返回None。
    # 函数用途: 阻塞一个owner并验证许可线程绑定。
    def invoke():
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(copy_context().run, current_owner_scheduler).result(5) is None
        entered.set()
        assert release.wait(5)

    pending = first.submit(Item("blocked", invoke))
    try:
        assert entered.wait(5)
        assert second.submit(Item("other", lambda: 42)).result(5) == 42
    finally:
        release.set()
    pending.result(5)


# LLM: 队满时提交者阻塞，Future.cancel不得执行handler；背压不额外增加worker。
# 函数用途: 验证有界队列和取消释放。
def test_bounded_queue_backpressure_and_future_cancel(tmp_path, monkeypatch):
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 1)
    entered, release, blocked = Event(), Event(), Event()
    seen = []

    # LLM: 第一项独占worker，确保后续只入队不执行。
    # 函数用途: 建立满队列窗口。
    def hold():
        entered.set()
        assert release.wait(5)

    first = scheduler.submit(Item("first", hold))
    assert entered.wait(5)
    cancelled = scheduler.submit(Item("cancelled", lambda: seen.append("cancelled")))
    later = scheduler.submit(Item("later", lambda: seen.append("later")))
    original = scheduler._condition.wait_for

    # LLM: 在真实等待前观察谓词，不修改队列容量或等待逻辑。
    # 函数用途: 证明提交者真的遭遇背压。
    def observed(predicate):
        if not predicate():
            blocked.set()
        return original(predicate)

    monkeypatch.setattr(scheduler._condition, "wait_for", observed)
    with ThreadPoolExecutor(1) as pool:
        producer = pool.submit(scheduler.submit, Item("last", lambda: seen.append("last")))
        try:
            assert blocked.wait(5)
            assert scheduler._active == 1 and len(scheduler._queue) == 2
            assert cancelled.cancel()
        finally:
            release.set()
        producer.result(5).result(5)
    first.result(5)
    later.result(5)
    assert seen == ["later", "last"]


# LLM: 提交中途异常时也要等已在途项，且等待异常不覆盖原提交异常。
# 函数用途: 核对部分提交失败清理语义。
def test_partial_submit_error_still_settles_started_items():
    from concurrent.futures import Future

    from agent_py_agent.agent.tooling.owner_tool_scheduler import execute_work_items
    waiting, decided, release = Event(), Event(), Event()
    primary = ValueError("submit failed")

    # LLM: 只有result观察器，真实Future仍控制完成/异常。
    # 类用途: 证明已提交项被等待。
    class Pending(Future):
        # LLM: 不持调度锁；记录清理入口后等待测试释放。
        # 函数用途: 观察部分提交异常后的清理。
        def result(self, timeout=None):
            waiting.set()
            decided.set()
            assert release.wait(5)
            return super().result(5)

    pending = Pending()

    # LLM: 第二次提交原样抛错，第一项Future保持在途。
    # 函数用途: 模拟提交源在部分成功后异常。
    def submit(item):
        if item == 1:
            raise primary
        return pending

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(execute_work_items, [0, 1], submit)
        future.add_done_callback(lambda _: decided.set())
        try:
            assert decided.wait(5) and waiting.is_set()
        finally:
            pending.set_exception(RuntimeError("cleanup error"))
            release.set()
        with pytest.raises(ValueError) as caught:
            future.result(5)
        assert caught.value is primary


# LLM: 串行项排队后仍可能关门/取消/compact，不能沿入队前决定直接进入handler。
# 函数用途: 复现串行worker启动点未重查屏障与过早started事件。
@pytest.mark.parametrize("reason,code", [("cancel", "CANCELLED"), ("compact", "COMPACT_REQUIRED"),
                                       ("shutdown", "HOST_SHUTDOWN")])
def test_serial_rechecks_gate_after_queue(tmp_path, monkeypatch, reason, code):
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 1)
    entered, queued, release = Event(), Event(), Event()
    switched, handlers, phases = [False], [], []
    token = CancellationToken()

    # LLM: 占满唯一额度，制造入队至开始间的屏障变化。
    # 函数用途: 暂停worker直到主线程改变屏障。
    def hold():
        entered.set()
        assert release.wait(5)

    first = scheduler.submit(Item("hold", hold))
    assert entered.wait(5)
    original = scheduler.submit

    # LLM: 标记第二项已经真实入队，不能只靠请求线程启动推断排队。
    # 函数用途: 捕获队列观察点。
    def submit(item):
        future = original(item)
        queued.set()
        return future

    monkeypatch.setattr(scheduler, "submit", submit)
    monkeypatch.setattr(rounds, "_emit_tool_progress", lambda event: phases.append(event.phase))
    monkeypatch.setattr(rounds, "_should_defer_for_compact", lambda *_: switched[0] and reason == "compact")
    monkeypatch.setattr(rounds, "_closed_admission_execution", lambda _req, call:
        rounds._synthetic_execution(call, rounds._interrupted_result(call), code)
        if switched[0] and reason == "shutdown" else None)
    agent = SimpleNamespace(root=tmp_path, config=SimpleNamespace(max_parallel_tool_calls_per_owner=1))
    request = _round_request(agent=agent, params=SimpleNamespace(tool_context=[], cancellation_token=token),
        tool_rounds=1, response=None, calls=[{"tool": "read"}],
        execute_one=lambda work: handlers.append(work) or _success(work, "ok"), record_one=lambda _: None)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(rounds._execute_serial_step, request, request.calls, 0, 0, rounds._ToolRoundProgress())
        try:
            assert queued.wait(5)
            switched[0] = True
            if reason == "cancel":
                token.cancel()
        finally:
            release.set()
        future.result(5)
    first.result(5)
    assert not handlers and "started" not in phases


# LLM: 线程启动失败必须归还计数并结清Future，不能留下永久在途与票号缺口。
# 函数用途: 验证系统资源错误后的调度器仍可再用。
def test_worker_start_failure_finishes_future_and_recovers(tmp_path, monkeypatch):
    import threading
    Scheduler, Item = scheduler_api()
    scheduler = Scheduler.for_owner(tmp_path, 1)
    error = RuntimeError("fixture thread start failed")

    # LLM: 只在Thread.start边界抛错，不伪造Future或handler结果。
    # 函数用途: 模拟操作系统拒绝创建worker。
    def fail(_thread):
        raise error

    with monkeypatch.context() as context:
        context.setattr(threading.Thread, "start", fail)
        future = scheduler.submit(Item("failure", lambda: pytest.fail("启动失败仍执行handler")))
        with pytest.raises(RuntimeError) as caught:
            future.result(5)
        assert caught.value is error and scheduler._active == 0 and not scheduler._queue
    assert scheduler.submit(Item("recovered", lambda: 42)).result(5) == 42


# LLM: 串行工具可能晋升_current_run_task_workspace；cap=0与cap>0都必须把晋升后的路径传给后继调用和轮后读取方。
# 函数用途: 锁定P1-1串行工具工作区晋升不回滚，及P2-1并发started不重复提交模型说明。
@pytest.mark.parametrize("cap", [0, 1])
def test_serial_workspace_promotion_and_concurrent_commit_dedupe(tmp_path, cap):
    import json
    import sys
    import threading

    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
    from agent_py_agent.agent.runtime_context import ThreadLocalAgentAttribute

    # LLM: 使用真实ThreadLocalAgentAttribute验证跨线程/本线程属性保持。
    # 类用途: 提供带线程局部工作区属性的轻量宿主替身。
    class HostAgent:
        _current_run_task_workspace = ThreadLocalAgentAttribute("_current_run_task_workspace")

        # LLM: 绑定owner根与并发上限配置。
        # 函数用途: 初始化测试宿主。
        def __init__(self):
            self.home_paths = SimpleNamespace(owner_home_dir=tmp_path)
            self.config = SimpleNamespace(max_parallel_tool_calls_per_owner=cap, sqlite_connection_reuse=False)

    agent = HostAgent()
    agent._current_run_task_workspace = str(tmp_path)
    promoted_root = str(tmp_path / "runs" / "promoted-work")
    seen_workspaces = []

    # LLM: 第一条调用模拟write_file晋升工作区，第二条验证进入时即看到晋升结果。
    # 函数用途: 记录每次串行工具进入时的线程局部工作区。
    def execute(work):
        seen_workspaces.append(getattr(agent, "_current_run_task_workspace", None))
        if work.idx == 1:
            agent._current_run_task_workspace = promoted_root
        return _success(work, "ok")

    request = _round_request(
        agent=agent,
        params=SimpleNamespace(tool_context=[], task_attributes={}),
        tool_rounds=1,
        response=None,
        calls=[{"tool": "write_file"}, {"tool": "write_file"}],
        execute_one=execute,
        record_one=lambda _: None,
    )
    progress = rounds._ToolRoundProgress()
    rounds._execute_serial_step(request, request.calls, 0, 0, progress)
    rounds._execute_serial_step(request, request.calls, 1, 1, progress)
    assert seen_workspaces == [str(tmp_path), promoted_root]
    assert agent._current_run_task_workspace == promoted_root

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        chunk_path = tmp_path / "stream.jsonl"
        writer = BufferedChunkStreamWriter(chunk_path, rich_transcript=True, flush_chars=1, delivery_channel="feishu")
        writer.set_verbose_level("stream")
        barrier = threading.Barrier(8)
        payload = {
            "schema": "assistant_messages.v1",
            "boundary": "tools",
            "final_text": "",
            "source": {"call_id": "model-call-1", "purpose": "main"},
            "messages": [
                {
                    "text": "并发工具启动说明",
                    "phase": "commentary",
                    "protocol": "responses",
                    "message_id": "m1",
                    "output_index": 0,
                    "source_type": "message",
                }
            ],
        }
        threads = [
            threading.Thread(target=lambda: (barrier.wait(5), writer.commit_model_response(payload)))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        writer.close()
        rows = [json.loads(line) for line in chunk_path.read_text(encoding="utf-8").splitlines()]
        assert sum(1 for row in rows if row.get("kind") == "assistant_commentary") == 1
    finally:
        sys.setswitchinterval(old_interval)
