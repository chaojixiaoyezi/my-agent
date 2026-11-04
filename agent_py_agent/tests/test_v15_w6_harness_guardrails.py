"""W6真实运行接缝；临时存储、合成观察与替身发送者，不访问外网。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import AccessTarget, AssetDescriptor, DataSourceSpec
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.source_manifest import save_manifest
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.workflows import datasource_runtime as runtime
from agent_py_agent.agent.workflows.models import ScriptExecutionResult
from agent_py_agent.tests.test_v15_w6_query_health import observation, report


# LLM: Store真实落盘但连接是无端点的custom源，查询观察由当前测试明确提供。
# 函数用途: 创建一个临时多源脚本及其清单。
def setup_script(home, *, declared=('events-a', 'events-b'), available=('events-a', 'events-b'),
                 manifest_bound=True, method='GET', idempotent=False, code=None):
    store = DatasourceStore(home)
    for identity in available:
        store.save_datasource({'datasource_id': identity, 'kind': 'custom', 'title': identity})
    sources = tuple(DataSourceSpec(identity, 'custom', query_scope={'access': 'read'}) for identity in declared)
    metadata = {'source_system': 'events'} if manifest_bound else {}
    metadata['retry_policy'] = {'idempotent': idempotent}
    ScriptStore(home).save_script(AssetDescriptor('probe', 'script', '事件查询', data_sources=sources,
        access_targets=(AccessTarget('event-api', 'http', 'https://example.invalid', method=method),),
        metadata=metadata), code or 'print("{}")')
    if manifest_bound:
        save_manifest(home, 'events', [{'source_id': identity, 'core': False,
            'credential_ref': 'cref-a', 'endpoint_ref': 'endpoint-a'} for identity in declared])
    return home


# LLM: 只构造结构化结果，不伪装真实外部查询观察。
# 函数用途: 为运行编排测试准备一次脚本结果。
def result(source_report=None, *, error_code=''):
    return ScriptExecutionResult('probe', not error_code, 0 if not error_code else 1,
        1.0, 0.0, 0.001, {'source_report': source_report} if source_report is not None else {}, '', '', error_code)


class CountingRunner:
    """替换进程边界，仅验证宿主的状态编排和预算，不声称原生沙箱已验。"""

    # LLM: 每次调用返回新的测试结果，计数反映实际重试。
    # 函数用途: 保存可复现的结果序列。
    def __init__(self, results, *, clock=None):
        self.results, self.calls, self.clock = results, [], clock

    # LLM: 参数签名保持真实runner的命名边界，不接收额外任意参数。
    # 函数用途: 消费一次合成脚本结果并记录剩余预算。
    def run_script(self, script_id, inputs=None, *, timeout_seconds=None, override_data_sources=None,
                   network_access=None, record_summary=True):
        self.calls.append({'timeout_seconds': timeout_seconds, 'record_summary': record_summary})
        if self.clock is not None:
            self.clock[0] += 0.7
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


# LLM: 真实预检有一源可用时才放行，缺失项仍进入明确降级回执。
# 函数用途: 验证多源隔离不会丢掉健康数据。
def test_manifest_multisource_missing_source_proceeds_with_degraded_receipt(tmp_path):
    home = setup_script(tmp_path, available=('events-a',))
    runner = CountingRunner([result(report([observation()]))])
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert value.ok and len(runner.calls) == 1
    assert value.output_data['source_report']['sources'][0]['hit_count'] == 1
    health = value.source_check['query_health']
    assert health['classification'] == 'PARTIAL_SOURCE_FAILURE'
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]
    assert not health['analysis_blocked'] and health['healthy_sources_queried'] == 1


# LLM: 新模式不改变单源、无清单或全部源缺失的旧硬门。
# 函数用途: 验证不能扩大预检放行范围。
@pytest.mark.parametrize('declared,available,bound', [
    (('events-a',), (), True), (('events-a', 'events-b'), (), True),
    (('events-a', 'events-b'), ('events-a',), False),
])
def test_partial_mode_does_not_bypass_anchor_hard_stops(tmp_path, declared, available, bound):
    home = setup_script(tmp_path, declared=declared, available=available, manifest_bound=bound)
    runner = CountingRunner([result(report([observation()]))])
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert not value.ok and value.error_code == 'DATASOURCE_MISSING'
    assert runner.calls == []


# LLM: 红灯回执每次保留，但当前Owner同一对引用的发送限一次/24小时。
# 函数用途: 验证首次、窗口内及窗口边界后的通知。
def test_red_light_receipt_always_present_but_im_is_deduplicated(tmp_path):
    home = setup_script(tmp_path)
    raw = report([observation(status='auth_expired'), observation('events-b')])
    notices = []
    with runtime.bind_asset_im_notifier(home, notices.append):
        for instant, count in [(1000000, 1), (1000001, 1), (1086400, 2)]:
            value = runtime.run_script_with_datasources(home, 'probe', runner=CountingRunner([result(raw)]), now=instant)
            assert 'query_health' in value.source_check
            assert len(value.source_check['query_health']['auth_alerts']) == 1
            assert len(notices) == count
    assert all(n['event_type'] == 'AUTH_OR_ENDPOINT_EXPIRED' for n in notices)


# LLM: 健康账写失败只使去重变为尽力，不能覆盖脚本执行和警告事实。
# 函数用途: 验证模拟IO错误下仍有红灯回执与通知尝试。
def test_health_state_io_failure_does_not_fail_run(tmp_path, monkeypatch):
    home, notices = setup_script(tmp_path), []

    # LLM: 仅破坏健康账，不改变原脚本或源观察。
    # 函数用途: 模拟健康账IO失败。
    def unavailable(owner_home, source_system, source_id, credential_ref, *, now, window_hours=24):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(runtime, 'should_emit_alert', unavailable, raising=False)
    raw = report([observation(status='auth_expired'), observation('events-b')])
    with runtime.bind_asset_im_notifier(home, notices.append):
        value = runtime.run_script_with_datasources(home, 'probe', runner=CountingRunner([result(raw)]), now=1000000)
    assert value.ok and 'query_health' in value.source_check
    assert len(value.source_check['query_health']['auth_alerts']) == 1 and len(notices) == 1


# LLM: GET脚本可自动重试，但最多两次；最终成功保留中间尝试账。
# 函数用途: 验证同一入口恢复后不再带旧失败分类。
def test_readonly_retry_succeeds_and_logs_two_retries(tmp_path, monkeypatch):
    home = setup_script(tmp_path)
    runner = CountingRunner([result(error_code='ECONNRESET'), result(error_code='ETIMEDOUT'),
        result(report([observation(), observation('events-b')]))])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert len(runner.calls) == 3
    health = value.source_check['query_health']
    assert value.ok and health['classification'] == 'SUCCESS_WITH_HITS' and not health['analysis_blocked']
    assert [row['error_code'] for row in health['retries']] == ['ECONNRESET', 'ETIMEDOUT']
    assert [row['attempt'] for row in health['retries']] == [1, 2]
    assert all(row['delay_ms'] >= 0 for row in health['retries'])
    assert runner.calls[0]['timeout_seconds'] >= runner.calls[-1]['timeout_seconds'] > 0


# LLM: 以真实源status触发重试，不依赖进程error_code才能恢复。
# 函数用途: 验证报告内瞬态错误和两次上限。
def test_transient_source_status_retries_at_most_twice(tmp_path, monkeypatch):
    home = setup_script(tmp_path)
    runner = CountingRunner([result(report([observation(status='transient_error', hits=0)]))])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert len(runner.calls) == 3
    assert value.source_check['query_health']['analysis_blocked']
    assert len(value.source_check['query_health']['retries']) == 2


# LLM: POST默认不重放；只有真布尔幂等标记才允许恢复。
# 函数用途: 验证有副作用目标的重试前置门。
@pytest.mark.parametrize('idempotent,count', [(False, 1), (True, 3)])
def test_non_idempotent_script_never_retries(tmp_path, monkeypatch, idempotent, count):
    home = setup_script(tmp_path, method='POST', idempotent=idempotent)
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert len(runner.calls) == count
    assert 'query_health' in value.source_check and value.source_check['query_health']['analysis_blocked']


# LLM: 预算包含前一次执行和退避；不为下一次重置完整超时。
# 函数用途: 验证剩余预算不足时停止重试。
def test_remaining_budget_prevents_retry(tmp_path, monkeypatch):
    home, clock = setup_script(tmp_path), [0.0]
    runner = CountingRunner([result(error_code='SCRIPT_TIMEOUT')], clock=clock)
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0.0,
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    monkeypatch.setattr(runtime, 'jittered_backoff', lambda attempt, *, base_delay, max_delay: 0.5, raising=False)
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner, timeout_seconds=1)
    assert len(runner.calls) == 1
    assert value.source_check['query_health']['analysis_blocked'] and clock[0] <= 1


# LLM: 测试真实工具公开JSON投影，确认guidance确实进入模型可见字段。
# 函数用途: 验证缺报告不能被当作健康零命中。
def test_missing_report_guidance_is_rendered_by_scripts_tool(tmp_path):
    home = setup_script(tmp_path)
    tool = ScriptsTool(home, runner=CountingRunner([result()]))
    outcome = tool.execute({'action': 'run', 'script_id': 'probe'})
    value = json.loads(outcome.output)
    assert 'query_health' in value['source_check']
    health = value['source_check']['query_health']
    assert health['analysis_blocked'] and health['block_reason'] == 'SOURCE_REPORT_MISSING'
    assert '不得' in health['guidance'] and '\n' not in health['guidance']


# LLM: 内存调试仍返回健康裁决，但沿原约定禁止通知与健康账落盘。
# 函数用途: 验证record_summary=False没有新增持久副作用。
def test_debug_receipt_does_not_write_alert_state(tmp_path):
    home, notices = setup_script(tmp_path), []
    raw = report([observation(status='auth_expired'), observation('events-b')])
    with runtime.bind_asset_im_notifier(home, notices.append):
        value = runtime.run_script_with_datasources(home, 'probe', runner=CountingRunner([result(raw)]),
            options={'record_summary': False}, now=1000000)
    assert 'query_health' in value.source_check and value.source_check['query_health']['auth_alerts']
    assert not notices and not (home / 'data/source_manifests/events.health.json').exists()


# LLM: 不替换runner，真实本机子进程打印固定合成报告，零外部查询。
# 函数用途: 验证多源降级完整穿过原prepare、SDK快照、进程和清理链。
def test_real_local_script_runs_with_one_declared_source_unavailable(tmp_path):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner

    raw = report([observation()])
    code = 'print(' + repr(json.dumps({'source_report': raw})) + ')'
    home = setup_script(tmp_path, available=('events-a',), code=code)
    value = runtime.run_script_with_datasources(home, 'probe',
        runner=ScriptRunner(home, use_sandbox=False, restrict_to_roots=True))
    assert value.ok, value.stderr_preview
    health = value.source_check['query_health']
    assert health['classification'] == 'PARTIAL_SOURCE_FAILURE' and health['healthy_sources_queried'] == 1
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]
    assert not list((home / 'tmp/script_exec').iterdir())


# LLM: 已观察到的预检失败不能被脚本自报ok洗成健康。
# 函数用途: 验证缺失源始终列入降级，失败不贡献命中。
def test_preflight_missing_source_cannot_be_overridden_by_report(tmp_path):
    home = setup_script(tmp_path, available=('events-a',))
    runner = CountingRunner([result(report([observation(), observation('events-b')]))])
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert value.ok
    health = value.source_check['query_health']
    assert health['healthy_sources_queried'] == 1
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]


# LLM: partial模式仅豁免部分缺失；没有缺失时原DATASOURCE_EMPTY硬门保持。
# 函数用途: 验证不能顺带放开原空源预检。
def test_empty_source_without_missing_keeps_original_preflight_stop(tmp_path):
    home = setup_script(tmp_path)
    empty = home / 'empty.json'
    empty.write_text('')
    store = DatasourceStore(home)
    spec = store.get_datasource_spec('events-b')
    store.save_datasource({'datasource_id': 'events-b', 'kind': 'custom', 'title': 'empty',
        'options': {'path': str(empty)}}, expected_revision=spec.revision)
    runner = CountingRunner([result(report([observation(), observation('events-b')]))])
    value = runtime.run_script_with_datasources(home, 'probe', runner=runner)
    assert not value.ok and value.error_code == 'DATASOURCE_EMPTY'
    assert not runner.calls
