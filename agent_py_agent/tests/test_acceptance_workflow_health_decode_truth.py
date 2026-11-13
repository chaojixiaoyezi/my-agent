"""磁盘旧账解码保真：缺失或非法的 attempt 不得被补成"已知一次"。

来源：WF-11 非作者复审发现的统计真实性缺口——采样走 WorkflowRunRecord.from_dict，
原实现 int(raw.get('attempt_count', 1)) 会把缺失、bool、浮点补成 1，使健康统计
把未知当成已知的一次尝试。本测试锁定：非法值保持未知，合法值原样保留。
"""
import pytest

from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.query_health import summarize_run_step_health

STEP_BASE = {
    'step_id': 's1', 'name': 'step', 'mode': 'tool', 'status': 'succeeded',
    'started_at': 1.0, 'ended_at': 2.0, 'duration_ms': 1000.0, 'llm_involved': False,
}

RUN_BASE = {
    'workflow_run_id': 'r1', 'workflow_id': 'w1', 'owner_id': 'o1',
    'trigger_kind': 'manual', 'status': 'succeeded',
    'started_at': 1.0, 'ended_at': 2.0, 'duration_ms': 1000.0,
}


@pytest.mark.parametrize('raw', [None, True, False, 1.5, '3', 0, -2])
def test_invalid_attempt_is_not_coerced_to_known_one(raw):
    payload = dict(STEP_BASE)
    if raw is not None:
        payload['attempt_count'] = raw
        payload['max_attempts'] = raw
    step = StepExecutionRecord.from_dict(payload)
    assert step.attempt_count is None
    assert step.max_attempts is None


@pytest.mark.parametrize('raw', [1, 3])
def test_valid_attempt_is_preserved(raw):
    step = StepExecutionRecord.from_dict({**STEP_BASE, 'attempt_count': raw, 'max_attempts': raw})
    assert step.attempt_count == raw
    assert step.max_attempts == raw


def test_decoded_unknown_attempt_reaches_health_as_unknown():
    step = StepExecutionRecord.from_dict(dict(STEP_BASE))
    run = WorkflowRunRecord.from_dict({**RUN_BASE, 'steps': [step.to_dict()]})
    row = summarize_run_step_health(run)['steps'][0]
    assert row['attempt_count'] is None
    assert row['max_attempts'] is None
    assert row['retried'] is None


def test_valid_attempt_survives_roundtrip_and_health():
    step = StepExecutionRecord.from_dict({**STEP_BASE, 'attempt_count': 2, 'max_attempts': 3})
    again = StepExecutionRecord.from_dict(step.to_dict())
    assert again.attempt_count == 2
    assert again.max_attempts == 3
    run = WorkflowRunRecord.from_dict({**RUN_BASE, 'steps': [again.to_dict()]})
    row = summarize_run_step_health(run)['steps'][0]
    assert row['attempt_count'] == 2
    assert row['max_attempts'] == 3
    assert row['retried'] is True
