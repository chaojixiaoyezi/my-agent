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
