# LLM: 只验原失败归档之后的发布接缝；真C只持原单例/控制器，不伪造P2停止或业务Agent，索引不授恢复权。
# 模块用途: 覆盖冷C可选日志跳过、Count0原Agent路由及索引失败后原投影事实保持。
import json
from contextlib import contextmanager

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import logging as gateway_logging
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import (
    claimed_request_chunk_path,
    gateway_paths_from_root,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskParams, submit_gateway_ask
from agent_py_agent.agent.settings import AgentConfig


# LLM: 队列/claim和canonical均由原Count0 producer在C出生前落盘；无模型、业务claim、P3或运行库行，不从JSON造执行许可。
# 函数用途: 准备已经封存的真实失败请求，让测试仅观察原后段发布顺序。
def failure_case(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    recovery._ensure_recovery_dirs(paths)
    request_id, request_path, _ = submit_gateway_ask(paths, params=GatewayAskParams(prompt='合成日志回归', save=False))
    claim = claim_request(paths, request_path)
    assert claim is not None
    payload = recovery.read_json_file_report(claim.path).payload
    context = {'request_path': claim.path, 'payload': payload, 'agent': None,
               'status': 'failed', 'error_code': 'GATEWAY_PROCESSING_TIMEOUT',
               'error': '合成失败', 'event_type': 'gateway_request_processing_failed'}
    response = recovery._build_gateway_failure_response(context)
    terminal = recovery._terminal_gateway_request_payload(request_id, payload, response)
    canonical = recovery._commit_gateway_terminal_request(paths, claim.path, paths.failed, request_id, terminal)
    chunk = claimed_request_chunk_path(claim.path, request_id)
    chunk.parent.mkdir(parents=True, exist_ok=True)
    return paths, context, response, canonical, chunk


# LLM: 原C单例与真实controller绑定同代控制面，不运行服务、W或Agent factory；空临时旧域不构造停止证明。
# 函数用途: 在原租约内提供真正的冷ControlPlane，保持它没有LocalStore的产品合同。
@contextmanager
def cold_control(tmp_path, paths):
    with coordinator_guard(paths.root, 1):
        controller = prepare_coordinator_takeover(paths, 1, 'optional-index-c')
        config = AgentConfig(my_agent_home=str(tmp_path / 'cold-home'), model_backend='echo')
        control = GatewayControlPlane(config, tmp_path / 'cold-home', paths, 1, controller.c_run_id)
        assert control.controller() is controller
        yield control


# LLM: 观察原logger但不替换其实现，诊断仅保存固定operation/event和异常类型，不保存请求正文。
# 函数用途: 记录是否错误进入可选业务索引，同时确认原文件投影先于索引。
def observe_index(monkeypatch, paths, context, chunk):
    calls, errors = [], []
    original = recovery.log_gateway_payload

    def indexed(agent, payload, **kwargs):
        assert recovery.gateway_response_path(paths, context['request_path'].stem).is_file()
        assert paths.history.is_file() and chunk.is_file()
        calls.append((agent, kwargs['event_type']))
        return original(agent, payload, **kwargs)

    def report(operation, request_id, error):
        errors.append((operation, type(error).__name__, error.name if isinstance(error, AttributeError) else None))

    monkeypatch.setattr(recovery, 'log_gateway_payload', indexed)
    monkeypatch.setattr(gateway_logging, '_report_gateway_side_effect_error', report)
    return calls, errors


def test_real_cold_c_failure_publisher_keeps_projections_without_business_index(tmp_path, monkeypatch):
    paths, context, response, canonical, chunk = failure_case(tmp_path)
    original_terminal = canonical.read_bytes()
    calls, errors = observe_index(monkeypatch, paths, context, chunk)
    with cold_control(tmp_path, paths) as control:
        context['agent'] = control
        recovery._publish_gateway_failure_response(paths, context, response)
        assert context['agent'] is control
        assert {'event_types': [event for _, event in calls], 'diagnostics': errors} == {'event_types': [], 'diagnostics': []}
        assert not control.home_root.exists()
    assert canonical.read_bytes() == original_terminal
    assert recovery.read_json_file_report(recovery.gateway_response_path(paths, canonical.stem)).payload == response
    assert json.loads(chunk.read_text().splitlines()[-1])['kind'] == 'request_aborted'
    assert len(paths.history.read_text().splitlines()) == 1
    assert not (paths.root / 'admission').exists()


# LLM: 使用实际Echo Agent及原LocalStore，但不调用模型；索引仍收到完全同一Agent对象。
# 函数用途: 保护Count0业务索引与原response/history/chunks先行顺序。
def test_count0_real_agent_indexes_through_same_original_object(tmp_path, monkeypatch):
    paths, context, response, canonical, chunk = failure_case(tmp_path)
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'agent-home')), tmp_path / 'project')
    context['agent'] = agent
    calls, errors = observe_index(monkeypatch, paths, context, chunk)
    recovery._publish_gateway_failure_response(paths, context, response)
    assert calls == [(agent, 'gateway_request_processing_failed')] and errors == []
    hits = agent.local_store.search(canonical.stem, source_type='gateway_request')
    assert any(row.source_id == canonical.stem for row in hits)
    assert context['agent'] is agent


# LLM: 仅注入原实际LocalStore的附属索引I/O失败；canonical不重建、模型不重跑，原response/history幂等保持。
# 函数用途: 证明索引失败不推翻已发布事实，重放继续使用原Agent与归档。
def test_optional_index_io_failure_does_not_replay_committed_execution(tmp_path, monkeypatch):
    paths, context, response, canonical, chunk = failure_case(tmp_path)
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'agent-home')), tmp_path / 'project')
    context['agent'] = agent
    calls, errors = observe_index(monkeypatch, paths, context, chunk)
    terminal_bytes = canonical.read_bytes()
    original = agent.local_store.log_record

    def failed_index(**kwargs):
        raise OSError('synthetic optional index failure')

    with monkeypatch.context() as m:
        m.setattr(agent.local_store, 'log_record', failed_index)
        recovery._publish_gateway_failure_response(paths, context, response)
    response_path = recovery.gateway_response_path(paths, canonical.stem)
    published = response_path.read_bytes(), paths.history.read_bytes()
    assert errors == [('log_gateway_payload', 'OSError', None)]
    assert canonical.read_bytes() == terminal_bytes
    assert agent.local_store.log_record == original
    recovery._publish_gateway_failure_response(paths, context, response)
    assert calls == [(agent, 'gateway_request_processing_failed')] * 2
    assert canonical.read_bytes() == terminal_bytes
    assert (response_path.read_bytes(), paths.history.read_bytes()) == published
    assert not context['request_path'].exists() and not list(paths.inbox.glob('*.json'))
    assert all(json.loads(line)['kind'] == 'request_aborted' for line in chunk.read_text().splitlines())


# LLM: response已写但history失败时仍保留同一个canonical；重放只补原投影，不调用Agent或替代恢复裁决。
# 函数用途: 覆盖冷C发布中断后原事实和异常对象保持，下一次仅修复派生输出。
def test_cold_partial_projection_retry_keeps_original_terminal(tmp_path, monkeypatch):
    import pytest

    paths, context, response, canonical, chunk = failure_case(tmp_path)
    terminal_bytes = canonical.read_bytes()
    failure = OSError('synthetic history interruption')

    def interrupted(*args):
        raise failure

    with cold_control(tmp_path, paths) as control:
        context['agent'] = control
        with monkeypatch.context() as m:
            m.setattr(recovery, 'append_gateway_history_once', interrupted)
            with pytest.raises(OSError) as actual:
                recovery._publish_gateway_failure_response(paths, context, response)
        assert actual.value is failure
        response_path = recovery.gateway_response_path(paths, canonical.stem)
        response_bytes = response_path.read_bytes()
        assert not paths.history.exists() and not chunk.exists()
        calls, errors = observe_index(monkeypatch, paths, context, chunk)
        recovery._publish_gateway_failure_response(paths, context, response)
        assert response_path.read_bytes() == response_bytes
        assert canonical.read_bytes() == terminal_bytes
        assert context['agent'] is control
        assert {'event_types': [event for _, event in calls], 'diagnostics': errors} == {'event_types': [], 'diagnostics': []}
        assert len(paths.history.read_text().splitlines()) == 1
        assert json.loads(chunk.read_text().splitlines()[-1])['kind'] == 'request_aborted'
