"""同一进度卡展示真实步骤摘要、批次与已绑定的后台执行事件。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.adapter.feishu_progress_card import render_progress_card
from agent_py_agent.agent.delivery.execution_view import project_execution_view


# LLM: 只使用宿主结构化展示字段；不让原始 arguments/output 进入卡片。
# 函数用途: 构造一条带批次和有界结果的工具进度。
def _event():
    return {'kind': 'tool_progress', 'request_id': 'request', 'progress': {
        'tool': 'scripts', 'call_id': 'call', 'phase': 'finished', 'ok': True,
        'handler_executed': True, 'duration_ms': 42,
        'display': {'connector_id': 'fixture_connector', 'endpoint_path': '/health', 'subcommand': 'status'},
        'result_excerpt': '结果摘要' * 40, 'batch_index': 2, 'batch_total': 8,
        'arguments': {'private': 'not-for-display'}, 'output': 'raw-body-not-for-display'}}


# LLM: 先核执行投影再核真实 Card 2.0，原始输出禁止通过结果摘要旁路出现。
# 函数用途: 验证名称、状态、耗时、结果节选与批次同一行展示。
def test_step_detail_result_excerpt_and_batch_survive_projection():
    view = project_execution_view([_event()], request_id='request')
    row = view['steps'][0]
    assert row['label'] == 'fixture_connector /health status'
    assert row['result_excerpt'] == ('结果摘要' * 40)[:60]
    assert row['batch_index'] == 2 and row['batch_total'] == 8
    card = render_progress_card(view)
    panel = next(el for el in card['body']['elements'] if el['tag'] == 'collapsible_panel')
    assert len(panel['elements']) == 1
    step = panel['elements'][0]
    assert step['tag'] == 'collapsible_panel' and step['expanded'] is False
    text = step['header']['title']['content'] + step['elements'][0]['text']['content']
    assert all(value in text for value in ['执行脚本', '已完成', '42ms', '/health', '批次：2/8', row['result_excerpt']])
    assert 'raw-body-not-for-display' not in json.dumps(card)
    assert 'not-for-display' not in json.dumps(card)


# LLM: 只允许显式绑定当前父请求的后台事件合流，不以名字相似合并跨请求状态。
# 函数用途: 验证后台同一身份更新只占一行且外来事件不进入卡片。
def test_background_progress_merges_into_the_same_card_and_keeps_scope():
    events = [_event(),
        {'kind': 'subagent_progress', 'parent_request_id': 'request', 'run_id': 'child', 'phase': 'started', 'label': '处理子任务'},
        {'kind': 'subagent_progress', 'parent_request_id': 'request', 'run_id': 'child', 'phase': 'completed',
         'duration_ms': 12, 'result_excerpt': '已核对'},
        {'kind': 'session_task_progress', 'parent_request_id': 'another', 'task_id': 'foreign', 'phase': 'started'}]
    view = project_execution_view(events, request_id='request')
    assert len(view['steps']) == 2
    assert view['steps'][1]['phase'] == 'completed'
    panel = next(el for el in render_progress_card(view)['body']['elements'] if el['tag'] == 'collapsible_panel')
    assert len(panel['elements']) == 2
    assert 'foreign' not in json.dumps(panel)


# LLM: 真正的 manager 轮询必须使用同一执行投影，否则模块单测通过也不能证明卡片接线。
# 函数用途: 验证嵌套工具进度与后台事件经过轮询后仍进入一个步骤批次。
def test_manager_poll_uses_enriched_view(monkeypatch):
    from agent_py_agent.agent.adapter import manager
    from agent_py_agent.agent.adapter.delivery import PendingGatewayReply
    body = {'events': [_event(), {'kind': 'subagent_progress', 'parent_request_id': 'request',
        'run_id': 'child', 'phase': 'started'}], 'next': 2}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_value, traceback):
            return False
        def read(self):
            return json.dumps(body).encode()
    monkeypatch.setattr(manager, 'open_authenticated_request', lambda request, timeout: Response())
    client = manager._GatewayReplyPollClient(12345)
    client.credentials = SimpleNamespace(headers=lambda: {})
    batch = client.poll_progress(PendingGatewayReply('request', 'feishu', 'actor', 'message'))
    assert len(batch.steps) == 2
    assert batch.steps[0]['batch_total'] == 8
    assert '/health' in batch.steps[0]['label']
