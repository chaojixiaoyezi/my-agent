# LLM: 只在临时服务目录验证停止请求和预算；信号/PID操作用替身，不停止真实daemon，原CLI解析仍执行。
# 模块用途: 回归适配器请求文件响应、心跳节奏以及适配器和监护者停止窗口。
from __future__ import annotations

import argparse
import json
import signal
import threading
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from agent_py_agent.cli import adapter, adapter_daemon, supervisor
from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands


# LLM: 夹具捕获信号注册与状态写入，实际等待函数和临时请求文件不替换；每例独立stop_event。
# 函数用途: 为停止等待测试准备临时目录、管理器和可观察信号处理器。
@pytest.fixture
def wait_case(tmp_path, monkeypatch):
    handlers = {}
    monkeypatch.setattr(adapter.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    writes = Mock()
    monkeypatch.setattr(adapter, "_write_adapter_state_guarded", writes)
    return SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path),
        stop_path=tmp_path / "adapter_stop.request",
        manager=SimpleNamespace(runtime_channel_statuses=lambda: [{"name": "feishu"}]),
        event=threading.Event(), handlers=handlers, writes=writes,
    )


# LLM: 已存在的请求须在第一次阻塞前消费，不靠SIGTERM或下一次五秒心跳才退出。
# 函数用途: 验证进入等待时已有停止文件会立即返回并清理文件。
def test_wait_consumes_existing_stop_request_without_waiting(wait_case, monkeypatch):
    case = wait_case
    case.stop_path.write_text("{}", encoding="utf-8")
    wait = Mock(side_effect=AssertionError("停止文件存在时不得先等待"))
    monkeypatch.setattr(case.event, "wait", wait)
    adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event)
    wait.assert_not_called()
    case.writes.assert_not_called()
    assert not case.stop_path.exists()


# LLM: 请求在一次短等待中出现须立刻收起循环；真实文件不mock，虚拟时钟避免墙钟抖动。
# 函数用途: 验证运行中发布停止请求最多经过一个0.2秒等待片。
def test_wait_observes_stop_request_within_one_short_slice(wait_case, monkeypatch):
    case = wait_case
    clock, delays = [0.0], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])

    # LLM: 只替代Event等待，推进时钟并真实写入停止文件；禁止无限循环或长等待。
    # 函数用途: 在首个等待片中发布停止请求。
    def publish(timeout):
        assert 0 < timeout <= 0.2
        assert not delays
        delays.append(timeout)
        clock[0] += timeout
        case.stop_path.write_text("{}", encoding="utf-8")
        return False

    monkeypatch.setattr(case.event, "wait", publish)
    adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event, requested_channel="feishu")
    assert delays == [0.2]
    case.writes.assert_not_called()
    assert not case.stop_path.exists()


# LLM: 轮询缩短不等于增加状态IO；原五秒心跳和requested_channel必须保留。
# 函数用途: 验证短片等待期间状态仍仅在第5秒和第10秒刷新。
def test_short_polling_preserves_state_write_interval(wait_case, monkeypatch):
    case = wait_case
    clock, delays, write_times = [0.0], [], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    case.writes.side_effect = lambda *_: write_times.append(clock[0])

    # LLM: 虚拟等待有次数上限，不能靠永不返回的替身掩盖产品忙循环。
    # 函数用途: 推进十秒正常等待后设置停止事件。
    def advance(timeout):
        assert 0 < timeout <= 0.2
        assert len(delays) < 100
        delays.append(timeout)
        clock[0] += timeout
        if clock[0] >= 10.1:
            case.event.set()
        return case.event.is_set()

    monkeypatch.setattr(case.event, "wait", advance)
    adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event, requested_channel="qq")
    assert write_times == pytest.approx([5.0, 10.0])
    assert case.writes.call_args_list == [call(case.paths, "running", {
        "channels": [{"name": "feishu"}], "requested_channel": "qq",
    })] * 2
    assert not case.stop_path.exists()


# LLM: 信号仍沿原handler设置同一event，不以文件协议取代SIGINT/SIGTERM。
# 函数用途: 验证两个原停止信号都能结束等待。
@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_wait_keeps_signal_shutdown(wait_case, monkeypatch, number):
    case = wait_case

    # LLM: 调用产品实际注册的handler，不向本机任何进程发真实信号。
    # 函数用途: 在等待中模拟收到指定停止信号。
    def receive(_timeout):
        case.handlers[number](number, None)
        return case.event.is_set()

    monkeypatch.setattr(case.event, "wait", receive)
    adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event)
    assert case.event.is_set()
    case.writes.assert_not_called()


# LLM: KeyboardInterrupt沿原合同吞掉，其它异常保留；两种退出均清理已发布停止文件。
# 函数用途: 验证等待抛错时不会遗留请求，且不改变原异常传播语义。
@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_wait_cleans_stop_request_on_interrupted_exit(wait_case, monkeypatch, error):
    case = wait_case

    # LLM: 在进入等待后发布真实请求再抛异常，确保清理发生于产品finally。
    # 函数用途: 构造等待期间被中断的部分退出现场。
    def interrupt(_timeout):
        case.stop_path.write_text("{}", encoding="utf-8")
        raise error("interrupted")

    monkeypatch.setattr(case.event, "wait", interrupt)
    if error is KeyboardInterrupt:
        adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event)
    else:
        with pytest.raises(RuntimeError, match="interrupted"):
            adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event)
    assert not case.stop_path.exists()


# LLM: 精确拒绝本例停止文件unlink；已有异常不能被清理错误盖掉，正常退出清理失败仍必须暴露。
# 函数用途: 验证请求文件删不掉时保留原始异常或如实报告删除失败。
@pytest.mark.parametrize("has_primary", [True, False])
def test_wait_cleanup_failure_preserves_primary_or_reports_error(wait_case, monkeypatch, caplog, has_primary):
    case = wait_case
    primary = RuntimeError("original wait failure")
    original_unlink = type(case.stop_path).unlink

    # LLM: 只拦指定临时停止文件，其它路径沿真实unlink，避免改变测试框架清理行为。
    # 函数用途: 注入停止请求清理的权限错误。
    def deny(path, *args, **kwargs):
        if path == case.stop_path:
            raise PermissionError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(case.stop_path), "unlink", deny)
    if has_primary:
        # LLM: 请求在等待中出现后抛原异常，确保finally面对的是确实存在但删不掉的文件。
        # 函数用途: 构造主体失败与文件清理失败同时发生的现场。
        def fail_wait(_timeout):
            case.stop_path.write_text("{}", encoding="utf-8")
            raise primary

        monkeypatch.setattr(case.event, "wait", fail_wait)
        with pytest.raises(RuntimeError) as caught:
            adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event)
        assert caught.value is primary
        assert "adapter_stop_request_cleanup_failed" in caplog.text
    else:
        case.stop_path.write_text("{}", encoding="utf-8")
        case.event.set()
        # 调用方已在处理另一个异常，仍不能把它错当成本循环的失败而吞掉清理错误。
        try:
            raise RuntimeError("unrelated caller failure")
        except RuntimeError:
            with pytest.raises(PermissionError, match="cleanup denied"):
                adapter._wait_for_adapter_shutdown(case.manager, case.paths, case.event)
        assert case.stop_path.exists()


# LLM: 停止入口写真实临时请求；PID等待、信号及PID删除用spy，不触碰生产进程。
# 函数用途: 验证正常停止保留用户预算，SIGTERM后给15秒且失败不伪报成功。
@pytest.mark.parametrize("outcomes,code", [([True], 0), ([False, True], 0), ([False, False], 1)])
def test_adapter_stop_budgets_and_outcomes(tmp_path, monkeypatch, outcomes, code):
    pid_file = tmp_path / "adapter.pid"
    options = adapter._adapter_options_from_args(SimpleNamespace(timeout=2.5))
    request = adapter_daemon.AdapterStopRequest(options, SimpleNamespace(root=tmp_path), pid_file, 12345)
    wait, terminate, remove = Mock(side_effect=outcomes), Mock(), Mock()
    monkeypatch.setattr(adapter_daemon, "wait_for_pid_exit", wait)
    monkeypatch.setattr(adapter_daemon, "terminate_pid", terminate)
    monkeypatch.setattr(adapter_daemon, "remove_pid_file_if_owned", remove)
    assert adapter_daemon.stop_adapter_daemon(request) == code
    assert json.loads((tmp_path / "adapter_stop.request").read_text())["reason"] == "user request"
    assert wait.call_args_list == [call(12345, timeout=2.5)] + ([call(12345, timeout=15.0)] if not outcomes[0] else [])
    if outcomes[0]:
        terminate.assert_not_called()
    else:
        terminate.assert_called_once_with(12345)
    if code == 0:
        remove.assert_called_once_with(pid_file)
    else:
        remove.assert_not_called()


# LLM: 使用真实argparse到handler接线；省略timeout传None后由handler采用25秒，显式非零覆盖保持。
# 函数用途: 验证supervisor-stop默认/显式预算与成功失败返回码，不停止真实服务。
@pytest.mark.parametrize("timeout_args,budget", [([], 25.0), (["--timeout", "0"], 25.0),
    (["--timeout", "10"], 10.0), (["--timeout", "30.5"], 30.5)])
@pytest.mark.parametrize("stopped,code", [(True, 0), (False, 2)])
def test_supervisor_stop_parser_to_handler_budget(monkeypatch, timeout_args, budget, stopped, code):
    parser = argparse.ArgumentParser()
    add_gateway_subcommands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["gateway", "supervisor-stop", *timeout_args])
    agent, paths = object(), object()
    make_agent, gateway_paths, stop = Mock(return_value=agent), Mock(return_value=paths), Mock(return_value=stopped)
    monkeypatch.setattr(supervisor, "make_agent", make_agent)
    monkeypatch.setattr(supervisor, "gateway_paths", gateway_paths)
    monkeypatch.setattr(supervisor, "stop_supervisor", stop)
    assert args.func is supervisor.cmd_supervisor_stop
    assert args.func(args) == code
    make_agent.assert_called_once_with(args)
    gateway_paths.assert_called_once_with(agent)
    stop.assert_called_once_with(paths, timeout=budget)


# LLM: 旧代PID停止请求在新代启动清理后延迟落盘时，应被新代忽略并清理，仅同代PID或信号才触发退出。
# 函数用途: 回归R6-1跨代停止标记不误杀新代适配器进程。
def test_delayed_old_generation_stop_request_does_not_exit_new_generation(tmp_path):
    import os

    gpaths = SimpleNamespace(root=tmp_path)
    adapter_daemon._write_stop_request(gpaths, pid=os.getpid() + 99999)
    stop_path = adapter_daemon.adapter_stop_path(tmp_path)
    assert stop_path.exists()
    assert adapter._stop_request_matches_current_process(stop_path) is False
    assert not stop_path.exists()
    adapter_daemon._write_stop_request(gpaths, pid=os.getpid())
    assert adapter._stop_request_matches_current_process(stop_path) is True
