"""监控投影只消费结构化引用和运行记录，不从任务正文猜依赖。"""
import json

import pytest

from agent_py_agent.agent.assets.monitor_adapter import (
    get_monitor_descriptor,
    list_monitor_descriptors,
    save_monitor_binding,
)


# LLM: 合成调度记录只用于本地投影，不创建执行器或读取真实用户资料。
# 函数用途: 生成只有必需字段的监控事实。
def _job(kind=None, identifier='flow'):
    return {'job_id': 'job48', 'name': 'Daily Monitor', 'status': 'active',
            'execution_target': {'kind': kind, 'asset_id': identifier} if kind else None}


# LLM: 目标种类与ID是唯一依赖来源；缺失目标不构造虚假引用。
# 函数用途: 验证工作流、脚本及空目标的公共引用形状。
@pytest.mark.parametrize('kind', ['workflow', 'script', None])
def test_structured_execution_target_refs(tmp_path, kind):
    result = get_monitor_descriptor(tmp_path, 'job48', _job(kind))
    assert result['refs'] == ([{'kind': kind, 'id': 'flow'}] if kind else [])
    assert result['metadata']['alias'] == 'dailymonitor'
    assert result['metadata']['upgrade_policy'] == 'pinned'
    assert not (tmp_path / 'data').exists()


# LLM: 别名仅展示，不参与授权；触发签名沿W1归一化。
# 函数用途: 验证绑定参数透传、持久化及别名回退。
def test_binding_alias_matchers_and_fallback(tmp_path):
    saved = save_monitor_binding(tmp_path, 'job48', alias=' Night\n Watch ',
        trigger_matchers={'indicator_types': ['ipv4', 'unknown'], 'intent_keywords': ['LOOK', 'look']})
    assert saved['metadata']['alias'] == 'nightwatch'
    assert saved['metadata']['trigger_matchers']['indicator_types'] == ['ipv4']
    assert saved['metadata']['trigger_matchers']['intent_keywords'] == ['look']
    assert get_monitor_descriptor(tmp_path, 'job48')['metadata'] == saved['metadata']
    assert get_monitor_descriptor(tmp_path, 'empty', {'job_id': 'empty'})['metadata']['alias'] == 'empty'
    assert len(list_monitor_descriptors(tmp_path, [_job()])) == 1


# LLM: 最后运行只来自准确job_id的调度器run，不读正文推断成功。
# 函数用途: 核验阶段、状态、耗时及有界单行摘要。
def test_last_run_projection_uses_scheduler_record(tmp_path):
    directory = tmp_path / 'data/scheduler'
    directory.mkdir(parents=True)
    (directory / 'store.json').write_text(json.dumps({'jobs': {}, 'runs': {
        'r1': {'job_id': 'job48', 'status': 'done', 'stage': 'analysis', 'duration_ms': 25,
               'response': 'line one\n' + 'x' * 200, 'finished_at': 1000},
        'r2': {'job_id': 'other', 'status': 'failed', 'finished_at': 2000}}}))
    last = get_monitor_descriptor(tmp_path, 'job48', _job())['metadata']['last_run']
    assert last == {'stage': 'analysis', 'status': 'done', 'duration_ms': 25,
                    'result_summary': ('line one ' + 'x' * 200)[:120],
                    'finished_at': '1970-01-01T00:16:40Z'}


# LLM: 错类型和未来字段按缺省处理，不让尚未合入的生产者破坏列表。
# 函数用途: 验证结构化引用的防御式读取。
def test_wrong_target_types_do_not_raise(tmp_path):
    for value in ([], 'workflow:flow', {'kind': 'future', 'asset_id': 'x'}, {'kind': 'workflow', 'asset_id': '../x'}):
        assert get_monitor_descriptor(tmp_path, 'job48', {**_job(), 'execution_target': value})['refs'] == []


# LLM: 终态原账本使用ended_at并移出活动集合，投影不能只测试自造finished_at字段。
# 函数用途: 验证归档运行的阶段、耗时和终态仍能读回，且列表只读。
def test_archived_scheduler_terminal_projection(tmp_path):
    directory = tmp_path / 'data/scheduler'
    directory.mkdir(parents=True)
    job = _job()
    (directory / 'store.json').write_text(json.dumps({'owner': {'id': 'local/main'}, 'jobs': {'job48': job}, 'runs': {}}))
    run = {'run_id': 'r1', 'job_id': 'job48', 'owner': {'id': 'local/main'}, 'status': 'done',
           'started_at': 1000, 'ended_at': 1002.5, 'response': 'observed\nresult'}
    (directory / 'history.jsonl').write_text(json.dumps(run) + '\n')
    before = {path: path.read_bytes() for path in directory.iterdir()}
    last = get_monitor_descriptor(tmp_path, 'job48')['metadata']['last_run']
    assert last == {'stage': 'finished', 'status': 'done', 'duration_ms': 2500,
                    'result_summary': 'observed result', 'finished_at': '1970-01-01T00:16:42.500000Z'}
    assert list_monitor_descriptors(tmp_path)[0]['metadata']['last_run'] == last
    assert {path: path.read_bytes() for path in directory.iterdir()} == before


# LLM: 原v13规则可没有计划；它仍是监控，不能只枚举scheduler已镜像任务。
# 函数用途: 验证v13原集合结构化依赖及同镜像job去重。
def test_v13_rules_project_refs_without_schedule(tmp_path):
    directory = tmp_path / 'data/business_v13'
    directory.mkdir(parents=True)
    (directory / 'monitor_rules.json').write_text(json.dumps({'records': {
        'rule_a': {'rule_id': 'rule_a', 'name': 'Rule A', 'enabled': False, 'schedule': None,
                   'script_id': 'collect', 'workflow_id': 'flow', 'scheduler_job_id': None}}}))
    rows = list_monitor_descriptors(tmp_path)
    assert len(rows) == 1 and rows[0]['id'] == 'rule_a'
    assert rows[0]['refs'] == [{'kind': 'workflow', 'id': 'flow'}, {'kind': 'script', 'id': 'collect'}]


# LLM: 实际监控公开投影必须可恢复为规范描述符，且解析不改原始列表或丢失引用；只使用临时目录。
# 函数用途: 防止推荐、反向依赖和拓扑读口再次因列表引用丢弃监控。
@pytest.mark.parametrize('kind', ['workflow', 'script', None])
def test_monitor_public_refs_roundtrip_as_asset_descriptor(tmp_path, kind):
    from agent_py_agent.agent.assets.asset_dependents import _descriptor
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor

    raw = get_monitor_descriptor(tmp_path, 'job48', _job(kind))
    original = json.loads(json.dumps(raw))
    descriptor = AssetDescriptor.from_dict(_descriptor(raw))
    assert descriptor.refs.to_dict() == raw['metadata']['asset_refs']
    assert AssetDescriptor.from_dict(descriptor.to_dict()) == descriptor
    assert raw == original
