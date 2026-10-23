# LLM: 计时只断言程序终态及稳定显示，不根据模型正文推断完成；受阻/未完成沿真实 local handler 保真，临时配置不访问供应商。
# 模块用途: 验证普通 CLI、TUI 和异常路径的统一结束时间、完整耗时、状态标签与去重。
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


# LLM: 墙钟和单调钟独立控制，模拟排队与墙钟回拨；原 adapter/reducer/render 均实际执行。
# 函数用途: TUI 完成摘要从接受排队算起，并成为可复制程序块和空闲状态。
@pytest.mark.parametrize("ok,interrupted", [(True, False), (False, False), (False, True)])
def test_t1_tui_terminal_includes_queued_elapsed_once(monkeypatch, ok, interrupted):
    import time

    now = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    runtime = TuiRuntime("timed")
    runtime.enqueue_prompt("request", "正文", queued=True)
    now[0] = 30
    adapter = runtime.begin_turn("request")
    now[0] = 75
    summary = TuiTurnSummary(response_text="回答", ok=ok, interrupted=interrupted)
    adapter.finalize(summary)
    adapter.finalize(summary)
    snapshot = runtime.store.snapshot()
    rows = [block for block in snapshot.stable_blocks if "用时 " in block.text]
    assert len(rows) == 1
    assert "用时 1分5秒" in rows[0].text
    assert rows[0].role != "assistant"
    assert getattr(snapshot.status, "completion", {}).get("elapsed_seconds") == 65
    frozen = rows[0].text
    now[0] = 1000
    assert runtime.store.snapshot().stable_blocks[-1].text == frozen


# LLM: 恢复观察没有原接受时钟，显示未知而不是自进入观察时从零计；不伪造服务端时间。
# 函数用途: 重连缺少可信开始时间时明确未知。
def test_t2_reattached_turn_does_not_invent_zero_elapsed():
    runtime = TuiRuntime("reattach")
    runtime.begin_turn("server-request").finalize(TuiTurnSummary())
    assert any("用时 未知" in block.text for block in runtime.store.snapshot().stable_blocks)


# LLM: 原 cmd_run 入口运行到实际 Agent 构造故障，finally 仍须有一次计时行且原异常不被吞。
# 函数用途: CLI 准备失败也有结束时间，不把错误伪装为成功。
def test_t3_run_preparation_failure_has_one_completion(monkeypatch, capsys):
    from agent_py_agent.cli import local_commands

    monkeypatch.setattr(local_commands, "make_agent", lambda args: (_ for _ in ()).throw(RuntimeError("prepare failed")))
    with pytest.raises(RuntimeError, match="prepare failed"):
        local_commands.cmd_run(SimpleNamespace())
    output = capsys.readouterr().out
    assert output.count("用时 ") == 1 and "失败于" in output


# LLM: 原 plain worker 消费一个真实 job 后被受控队列终止；异常和中断必须各自保留正确终态。
# 函数用途: 普通聊天的 finally 统一打印计时，不依赖成功返回处理函数。
@pytest.mark.parametrize("error,label", [(None, "完成"), (RuntimeError("failed"), "失败"), (InterruptedError(), "已中断"), (TimeoutError("unknown"), "未知")])
def test_t4_plain_worker_finally_has_one_completion(tmp_path, monkeypatch, capsys, error, label):
    from agent_py_agent.cli.chat_parts import plain_worker
    from agent_py_agent.cli.chat_parts.plain_state import ChatJob

    job = ChatJob(user="input", show_prompt=False, inject=[], prompt_files=[], request_id="plain-request")
    jobs = SimpleNamespace(get=iter([job]).__next__, task_done=lambda: None)
    cfg = SimpleNamespace(jobs=jobs, state_lock=threading.RLock(), pending_jobs_ref=[1], is_running_ref=[False],
        running_prompt_ref=[""], running_request_id_ref=[""], running_started_at_ref=[0.0], use_gateway=True,
        local_run_ref=[None], assistant_outputs=[], conversation_history=[], history_lock=threading.RLock(), agent=object())

    # LLM: 仅替换执行边界，不替换 finally 或打印，错误对象原样抛出。
    # 函数用途: 模拟执行结果，成功用空正文避开历史领域无关依赖。
    def process(*args):
        if error:
            raise error
        return "", False
    monkeypatch.setattr(plain_worker, "_plain_process_job", process)
    with pytest.raises(StopIteration):
        plain_worker._plain_worker(cfg)
    output = capsys.readouterr().out
    if isinstance(error, TimeoutError):
        assert "用时 " not in output and "未知" in output
    else:
        assert output.count("用时 ") == 1 and label in output


# LLM: 真 worker/finalize 消费运输异常；仅替换执行和无关清理，不将退出观察当业务终态。
# 函数用途: TUI 断线或退出观察不显示假的失败、中断或零秒完成。
@pytest.mark.parametrize("error", [TimeoutError("unconfirmed"), InterruptedError("observation closed")])
def test_t2_tui_observation_failure_has_no_completion(monkeypatch, error):
    import queue

    from agent_py_agent.cli.chat_parts import tui_worker

    runtime = TuiRuntime("unknown")
    runtime.enqueue_prompt("original", "输入", queued=True)
    stop = threading.Event()
    jobs = queue.Queue()
    jobs.put(SimpleNamespace(request_id="original", show_prompt=False))
    cfg = SimpleNamespace(jobs=jobs, stop_event=stop, tui_runtime=runtime, use_gateway=True)
    monkeypatch.setattr(tui_worker, "_tui_prepare_gateway_job", lambda cfg, job: None)
    monkeypatch.setattr(tui_worker, "_tui_update_running_state", lambda cfg, job: None)
    monkeypatch.setattr(tui_worker, "_tui_cleanup_after_job", lambda *args: None)

    # LLM: 一次真实 worker 执行后停本测试循环，不修改服务端状态。
    # 函数用途: 抛出原运输异常并保证测试有限退出。
    def process(*args):
        stop.set()
        raise error
    monkeypatch.setattr(tui_worker, "_tui_process_job", process)
    tui_worker._tui_worker_body(cfg)
    assert not any("用时 " in row.text for row in runtime.store.snapshot().stable_blocks)
    assert not runtime.store.snapshot().status.completion


# LLM: 真实附件 job 入口只有恢复观察，无本地受理时间；不得重新启动零秒钟。
# 函数用途: 从原 outbox 接回已排队请求时显示耗时未知。
def test_t2_real_attachment_does_not_start_a_new_clock():
    import queue

    from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
    from agent_py_agent.cli.chat_parts.tui_actions import _tui_attach_gateway_job

    runtime = TuiRuntime("reconnect")
    params = SimpleNamespace(tui_runtime=runtime, state_lock=threading.RLock(),
        pending_jobs_ref_for_enqueue=[0], jobs=queue.Queue())
    _tui_attach_gateway_job(params, "input", display_text="input", gateway_request_id="canonical",
        client_message_id="original", execution_options=GatewayAskExecutionOptions())
    runtime.begin_turn("canonical").finalize(TuiTurnSummary())
    assert any("用时 未知" in row.text for row in runtime.store.snapshot().stable_blocks)


# LLM: 原普通终端 handler 接收确定的服务端终态，模拟的仅是网络和渲染边界，计时分类仍用真实函数。
# 函数用途: 无正文停止和确定失败不能被 worker 默认归为成功，正常出口不重复输出耗时。
@pytest.mark.parametrize("response,status", [({"ok": False, "status": "interrupted", "error_code": "INTERRUPTED"}, "interrupted"),
    ({"ok": False, "error_code": "MODEL_ERROR", "response": ""}, "failed"), ({"ok": True, "response": "ok"}, "completed")])
def test_t1_plain_handler_preserves_terminal_status(tmp_path, monkeypatch, capsys, response, status):
    from agent_py_agent.cli.chat_parts import plain_handlers as handlers
    from agent_py_agent.cli.chat_parts.plain_state import ChatJob

    job = ChatJob(user="input", show_prompt=False, inject=[], prompt_files=[], request_id="plain")
    agent = SimpleNamespace(config=SimpleNamespace(agent_name="test", gateway_request_timeout=1))
    ctx = handlers.PlainJobContext(job, agent, SimpleNamespace(no_save=True, gateway_timeout=1),
        SimpleNamespace(terminal=tmp_path), [], lambda: "")
    monkeypatch.setattr(handlers, "check_gateway_alive", lambda paths: True)
    monkeypatch.setattr(handlers, "submit_chat_request", lambda *args, **kw: ("canonical", tmp_path / "chunk", tmp_path / "result"))
    monkeypatch.setattr(handlers, "gateway_request_activity_paths", lambda *args: ())
    monkeypatch.setattr(handlers, "poll_gateway_chunks", lambda request: response)
    monkeypatch.setattr(handlers, "_render_if_needed", lambda *args: None)
    handlers._plain_gateway_handle(ctx)
    assert getattr(job, "completion_status", "") == status
    assert "耗时" not in capsys.readouterr().out


# LLM: 秒级精度、非有限和负数都直接检查纯格式器，不把非法值伪装成零秒。
# 函数用途: 覆盖跨分钟/小时/天和小数耗时的边界。
@pytest.mark.parametrize("seconds,expected", [(0.4, "0.4秒"), (59.9, "59.9秒"), (60, "1分0秒"),
    (3600, "1小时0分0秒"), (86400, "1天0小时0分0秒"), (float("nan"), "未知"), (-1, "未知")])
def test_t2_formatter_boundaries(seconds, expected):
    from agent_py_agent.cli.turn_completion import format_elapsed

    assert format_elapsed(seconds) == expected


# LLM: 只替换真实模型/续跑与正文渲染边界，仍执行 cmd_run 的准备、结果分类和唯一收口。
# 函数用途: 正常与手动续跑中的结构化取消不能被整数退出码误写成失败，unknown 不能冒充终态。
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("status,label", [("ok", "完成于"), ("interrupted", "已中断于"), ("unknown", "状态待确认")])
def test_t1_run_confirmed_outcome_survives_exit_code(monkeypatch, capsys, resume, status, label):
    from agent_py_agent.cli import local_commands, resume_loop

    result = SimpleNamespace(runtime_status=status)
    outcome = SimpleNamespace(status="completed", final_result=result)
    monkeypatch.setattr(local_commands, "make_agent", lambda _args: object())
    monkeypatch.setattr(resume_loop, "run_with_resume", lambda *args, **kw: outcome)
    monkeypatch.setattr(resume_loop, "run_manual_resume", lambda *args, **kw: outcome)
    monkeypatch.setattr(local_commands, "print_run_result", lambda *args, **kw: print("最终正文"))
    args = SimpleNamespace(prompt="任务", resume="existing-task" if resume else "", save=False,
                           inject=[], prompt_file=[], show_prompt=False)
    assert local_commands.cmd_run(args) == (0 if status == "ok" else 2)
    output = capsys.readouterr().out
    assert "最终正文" in output and label in output
    assert output.count("用时 ") == (0 if status == "unknown" else 1)


# LLM: 只替换模型执行和正文显示边界，真实 local handler、时钟与摘要渲染必须保留结构化状态，正文不作成功证据。
# 函数用途: 回归 direct plain 的失败分类、受阻/未完成链路及缺省 runtime_status 兼容语义。
@pytest.mark.parametrize("runtime_fields,status,label", [
    ({"runtime_status": "failed"}, "failed", "失败于"),
    ({"runtime_status": "error"}, "failed", "失败于"),
    ({"runtime_status": "cancelled"}, "failed", "失败于"),
    ({"runtime_status": "blocked"}, "blocked", "受阻于"),
    ({"runtime_status": "unfinished"}, "unfinished", "未完成于"),
    ({"runtime_status": "ok"}, "completed", "完成于"),
    ({"runtime_status": "unknown"}, "completed", "完成于"),
    ({"runtime_status": None}, "completed", "完成于"),
    ({"runtime_status": ""}, "completed", "完成于"),
    ({"runtime_status": 1}, "completed", "完成于"),
    ({}, "completed", "完成于"),
])
def test_f7_plain_local_status_reaches_completion(monkeypatch, runtime_fields, status, label):
    from agent_py_agent.cli.chat_parts import plain_handlers as handlers
    from agent_py_agent.cli.chat_parts.plain_state import ChatJob
    from agent_py_agent.cli.turn_completion import TurnClock, render_completion

    result = SimpleNamespace(response="这是正文，不是完成证据", **runtime_fields)
    job = ChatJob(user="input", show_prompt=False, inject=[], prompt_files=[], request_id="f7-local")
    agent = SimpleNamespace(config=SimpleNamespace(agent_name="test"), run=lambda *args, **kwargs: result)
    local_run = SimpleNamespace(request_id=job.request_id, check_admission=lambda: None)
    ctx = handlers.PlainJobContext(job, agent, SimpleNamespace(no_save=True),
        SimpleNamespace(), [], lambda: "", local_run=local_run)
    monkeypatch.setattr(handlers, "_make_chunk_handler", lambda *args, **kwargs: (lambda *parts: None, [False]))
    monkeypatch.setattr(handlers, "_render_if_needed", lambda *args: None)
    monkeypatch.setattr(handlers, "_print_local_timing", lambda *args: None)
    now = [10.0]
    monkeypatch.setattr(handlers.time, "monotonic", lambda: now[0])
    clock = TurnClock(job.request_id)
    now[0] = 15.0

    assert handlers._plain_local_handle(ctx) == (result.response, False)
    assert job.completion_status == status
    completion = clock.finish(job.completion_status)
    assert completion.status == status and completion.elapsed_seconds == 5.0
    rendered = render_completion(completion)
    assert rendered.startswith(label) and "用时 5.0秒" in rendered
    if status in {"blocked", "unfinished"}:
        assert not rendered.startswith("完成于")


# LLM: 明确收口可以受阻或未完成，不代表业务成功；同一时钟仍冻结首个状态与耗时。
# 函数用途: 检查五类已确认结果均可结算，且后续完成观察不改写原结果。
@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "blocked", "unfinished"])
def test_f7_turn_clock_accepts_confirmed_outcomes(status):
    from agent_py_agent.cli.turn_completion import TurnClock

    clock = TurnClock("f7-clock", started_monotonic=None)
    completion = clock.finish(status)
    assert completion.status == status and completion.elapsed_seconds is None
    assert clock.finish("completed") is completion


# LLM: 新增两类确认结果不放宽未知状态，连接或业务结果未确认时不得生成计时终态。
# 函数用途: 保留未知状态的拒绝行为，避免扩展标签后误将任意结果视为完成。
def test_f7_turn_clock_rejects_unknown_status():
    from agent_py_agent.cli.turn_completion import TurnClock

    clock = TurnClock("f7-unknown")
    with pytest.raises(ValueError, match="未知业务状态"):
        clock.finish("unknown")
    assert clock.completion is None


# LLM: 摘要仅展示输入状态，未知值明确状态未知，不从结束时间或正文推断成功。
# 函数用途: 独立检查五类中文标签以及未知、空值和缺失状态的保守回退。
@pytest.mark.parametrize("fields,label", [
    ({"status": "completed"}, "完成于"),
    ({"status": "failed"}, "失败于"),
    ({"status": "interrupted"}, "已中断于"),
    ({"status": "blocked"}, "受阻于"),
    ({"status": "unfinished"}, "未完成于"),
    ({"status": "unknown"}, "状态未知于"),
    ({"status": None}, "状态未知于"),
    ({"status": ""}, "状态未知于"),
    ({}, "状态未知于"),
])
def test_f7_completion_labels_preserve_status(fields, label):
    from agent_py_agent.cli.turn_completion import render_completion

    rendered = render_completion({"completed_at": 1700000000.0, "elapsed_seconds": None, **fields})
    assert rendered.startswith(label) and "用时 未知" in rendered
