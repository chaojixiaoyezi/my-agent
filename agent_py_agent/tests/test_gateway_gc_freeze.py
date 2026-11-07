"""验证 Gateway 等待循环的永久代仅在本次服务循环存活期间保留。"""
from __future__ import annotations

import gc
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.cli import gateway_process
from agent_py_agent.cli.models import GatewayRunContext


@pytest.fixture
def gateway_context(tmp_path):
    # 清除解释器或测试插件预先冻结的对象，避免把外部状态误认成本循环的行为。
    gc.unfreeze()
    identity = gateway_process.build_process_identity()
    payload = {"target_process": identity}
    stop_request = tmp_path / "gateway.stop"
    stop_request.write_text(json.dumps(payload), encoding="utf-8")
    context = GatewayRunContext(
        agent=SimpleNamespace(),
        paths=SimpleNamespace(stop_request=stop_request),
        config_path=tmp_path / "config.yaml",
        process_identity=identity,
    )
    try:
        yield context, payload
    finally:
        # 失败用例也必须释放永久代，避免影响同进程内后续测试。
        gc.unfreeze()


def test_gateway_service_loop_freezes_until_stop_request(gateway_context, monkeypatch):
    context, payload = gateway_context
    observed = []
    read_json_file = gateway_process.read_json_file
    thresholds = gc.get_threshold()

    def observe_read(path):
        observed.append(gc.get_freeze_count())
        return read_json_file(path)

    monkeypatch.setattr(gateway_process, "read_json_file", observe_read)

    report = gateway_process._run_gateway_service_loop(context)

    assert report == {"summary": "stop requested", "stop_request": payload}
    assert len(observed) == 1 and observed[0] > 0
    assert gc.get_freeze_count() == 0
    assert gc.get_threshold() == thresholds


def test_gateway_service_loop_unfreezes_after_restart(gateway_context, monkeypatch):
    context, _payload = gateway_context
    context.paths.stop_request.unlink()
    observed = []
    restart_report = {"summary": "restart requested"}

    def observe_restart(current_context, identity):
        assert current_context is context
        assert identity == context.process_identity
        observed.append(gc.get_freeze_count())
        return restart_report

    monkeypatch.setattr(gateway_process, "drain_for_requested_restart", observe_restart)

    assert gateway_process._run_gateway_service_loop(context) is restart_report
    assert len(observed) == 1 and observed[0] > 0
    assert gc.get_freeze_count() == 0


@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt])
def test_gateway_service_loop_unfreezes_after_error(
    gateway_context, monkeypatch, exception_type,
):
    context, _payload = gateway_context
    observed = []
    failure = exception_type("service loop interrupted")

    def fail_read(_path):
        observed.append(gc.get_freeze_count())
        raise failure

    monkeypatch.setattr(gateway_process, "read_json_file", fail_read)

    with pytest.raises(exception_type) as exc_info:
        gateway_process._run_gateway_service_loop(context)

    assert exc_info.value is failure
    assert len(observed) == 1 and observed[0] > 0
    assert gc.get_freeze_count() == 0
