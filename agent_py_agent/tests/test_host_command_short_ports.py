# LLM: 假平台只验证公共短端口适配，原 SQLite/ToolExecutor/审批仍真实；不证明 C 权限、P3 或 native 退出。
# 模块用途: 验证宿主命令元数据及收尾在原短提交内，未知不伪装前置失败，Count0 和审批时限保持。
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from contextvars import copy_context

import pytest

from agent_py_agent.agent.common import request_execution_ports as ports
from agent_py_agent.agent.runtime_db import executor_liveness
from agent_py_agent.agent.runtime_db import host_command_execution as execution
from agent_py_agent.tests.test_host_command_execution import case


# LLM: 只作为 fake 平台合同，绑定原 HostCommandBinding、当前线程及活窗口；没有真实 C/lease 许可或另一份持久账。
# 类用途: 在测试中记录原同步 callback 的执行区间，不替代产品原短门。
class _PlatformSource:
    # LLM: 构造仅保存当前原 binding 和线程，不写库或启动后台工作。
    # 函数用途: 准备一次显式 fake 平台窗口。
    def __init__(self, binding):
        self.binding = binding
        self.thread = threading.current_thread()
        self.active = True
        self.inside = False
        self.phases = []

    # LLM: 只消费本次 fake 窗口；真实产品许可应由 C 原来源实现，不能用此替身放宽生产。
    # 函数用途: 为短操作执行同一同步 callback 并记下实际阶段。
    def short_operation(self, phase, operation):
        _validate(self, phase)
        self.phases.append(phase)
        self.inside = True
        try:
            return operation()
        finally:
            self.inside = False


# LLM: 测试只接受自己建立的当前源；拒绝不从 message 或角色猜 authority。
# 函数用途: 给原公共端口安装明确的 fake 平台校验器。
def _validate(source, _phase):
    if type(source) is not _PlatformSource or not source.active or source.thread is not threading.current_thread():
        raise ports.RequestExecutionContextUnavailable("fake original source unavailable")


# LLM: 显式临时 home 和 monkeypatch 恢复所有进程内 hook，不影响别的测试或真实运行。
# 函数用途: 构造原真实 Store/执行器测试环境并装配 fake 来源校验。
def _case(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ports, "_source_validator", _validate)
    monkeypatch.setattr(ports, "_context_rejection", None)
    return case(tmp_path)


# LLM: 来源由真实 binding 建立，仅在本次同步执行内有效；退出不续租或补造新的源。
# 函数用途: 生成传给宿主命令的 fake scope，同时保留测试观察引用。
def _scope(captured):
    @contextmanager
    def enter(binding):
        source = _PlatformSource(binding)
        captured.append(source)
        try:
            with ports.request_execution_ports_scope(source):
                source.context = copy_context()
                yield
        finally:
            source.active = False
    return enter


def test_managed_missing_source_keeps_original_running_debt(tmp_path, monkeypatch):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(ports, "managed_request_execution_required", lambda: True)
    monkeypatch.setattr(execution, "managed_request_execution_required", lambda: True, raising=False)
    with pytest.raises(ports.RequestExecutionContextUnavailable):
        execution.execute_host_command(repo, request, prepare)
    binding = repo.find_host_command(request)
    attempt = repo.get_attempt(binding.attempt_id)
    assert attempt["status"] == "running" and tool.calls == 0
    assert "executor" not in json.loads(attempt["metadata_json"] or "{}")
    assert execution._operation(repo, binding) is None
    assert execution._unstarted_receipt(repo, binding) is None
    assert not repo.get_task_run(binding.task_run_id)["closed_at"]


def test_metadata_and_terminal_write_run_inside_original_callback(tmp_path, monkeypatch):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    captured, writes, claims = [], [], []
    original = executor_liveness._write_executor_record

    def leaf(*args, **kwargs):
        assert captured[-1].inside
        writes.append(kwargs["starting"])
        return original(*args, **kwargs)

    def claimed(record, resources):
        assert tool.calls == 0
        assert record.operation_id == request.operation_id
        binding = captured[-1].binding
        assert record.run_id == binding.run_id
        assert repo.get_attempt(binding.attempt_id)["agent_run_id"] == binding.agent_run_id
        claims.append((record, resources))

    monkeypatch.setattr(executor_liveness, "_write_executor_record", leaf)
    result = execution.execute_host_command(repo, request, prepare,
        execution_scope=_scope(captured), on_operation_claim=claimed)
    assert result["state"] == "succeeded" and tool.calls == 1, result
    assert writes == [True, False] and len(claims) == 1
    assert captured[0].phases == ["child_executor_record", "child_executor_record", "host_command_terminal_commit"]
    assert not captured[0].active and not captured[0].inside


@pytest.mark.parametrize("fault", ["preparation", "after-tool"])
def test_original_source_denial_never_becomes_unstarted_rejection(tmp_path, monkeypatch, fault):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    captured = []
    original = ports.RequestExecutionOutcomeUncertainError({
        "id": "original-request", "execution_attempt_id": "original-attempt",
        "lease_epoch": 9, "lease_owner": "original-lease",
    }, "host-metadata")

    def prepared(binding):
        value = prepare(binding)
        if fault == "preparation":
            raise original
        captured[-1].active = False
        return value

    expected = ports.RequestExecutionOutcomeUncertainError if fault == "preparation" else ports.RequestExecutionContextUnavailable
    with pytest.raises(expected) as caught:
        execution.execute_host_command(repo, request, prepared, execution_scope=_scope(captured))
    if fault == "preparation":
        assert caught.value is original
    binding = repo.find_host_command(request)
    assert repo.get_attempt(binding.attempt_id)["status"] == "running"
    assert execution._unstarted_receipt(repo, binding) is None
    assert not repo.get_task_run(binding.task_run_id)["closed_at"]
    assert tool.calls == (fault == "after-tool")
    assert (execution._operation(repo, binding) is not None) is (fault == "after-tool")


def test_settled_managed_replay_does_not_need_new_source_or_writer(tmp_path, monkeypatch):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    assert execution.execute_host_command(repo, request, prepare)["state"] == "succeeded"
    monkeypatch.setattr(execution, "managed_request_execution_required", lambda: True, raising=False)
    monkeypatch.setattr(ports, "managed_request_execution_required", lambda: True)

    def forbidden(*_args, **_kwargs):
        pytest.fail("已结清重放不得重新进入写门或执行区间")

    monkeypatch.setattr(execution, "gateway_short_operation", forbidden, raising=False)
    result = execution.execute_host_command(repo, request, prepare, execution_scope=forbidden)
    assert result["state"] == "succeeded" and tool.calls == 1


def test_original_approval_timeout_is_preserved_with_new_execution_scope(tmp_path, monkeypatch):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    captured, timeouts = [], []
    original = execution.resolve_host_command_approval

    def approve(*args, **kwargs):
        timeouts.append(kwargs["timeout_seconds"])
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "resolve_host_command_approval", approve)
    result = execution.execute_host_command(repo, request, prepare,
        execution_scope=_scope(captured), approval_timeout_seconds=17)
    assert result["state"] == "succeeded" and tool.calls == 1 and timeouts == [17]


def test_claim_observer_failure_preserves_original_durable_debt(tmp_path, monkeypatch):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    captured = []

    def failed_observer(_record, _resources):
        raise OSError("合成领取观察失败")

    result = execution.execute_host_command(repo, request, prepare,
        execution_scope=_scope(captured), on_operation_claim=failed_observer)
    binding = repo.find_host_command(request)
    assert result["state"] == "outcome_unknown" and tool.calls == 0
    assert execution._operation(repo, binding) is not None
    assert execution._unstarted_receipt(repo, binding) is None
    assert repo.get_attempt(binding.attempt_id)["status"] == "unknown"
    assert not repo.get_task_run(binding.task_run_id)["closed_at"]
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM resource_locks").fetchone()[0] > 0


def test_scope_exit_copied_context_cannot_write_executor_metadata(tmp_path, monkeypatch):
    repo, request, tool, prepare = _case(tmp_path, monkeypatch)
    captured = []
    assert execution.execute_host_command(repo, request, prepare,
        execution_scope=_scope(captured))["state"] == "succeeded"
    binding = repo.find_host_command(request)
    before = repo.get_attempt(binding.attempt_id)
    executor = json.loads(before["metadata_json"])["executor"]
    with pytest.raises(ports.RequestExecutionContextUnavailable):
        captured[0].context.run(executor_liveness._write_executor,
            repo, binding.run_id, binding.attempt_id, executor, starting=False)
    assert repo.get_attempt(binding.attempt_id) == before and tool.calls == 1
