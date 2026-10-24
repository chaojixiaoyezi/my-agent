# LLM: 沿真实工具合同验证只读、完整性与窄读取；不以摘要伪造全量枚举或放宽旧断言。
# 模块用途: 锁住开放登记、分页预算、安全恢复及新增轻量摘要的公共语义。
"""自我目录合同：只读、开放登记、完整性、分页预算及安全恢复。"""
import json

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.tooling.self_map_registry import (
    SectionDescriptor,
    SectionProjection,
    SelfMapRegistry,
    map_item,
    next_read,
)
from agent_py_agent.agent.tooling.self_map_tool import SelfMapTool


@pytest.fixture
def map_case(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
                                   execution_mode='local_unmanaged', enable_self_map=False), tmp_path / 'workspace')
    sections = SelfMapRegistry()
    tool = SelfMapTool(agent, sections)
    agent.tools.register(tool)
    return agent, sections, tool


def _query(case, query, *, allowed=None):
    agent, _, tool = case
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    result = tool.execute_scoped(query, ToolInvocationContext(snapshot))
    return result, json.loads(result.output)


@pytest.mark.parametrize('query', [
    {'owner_id': 'other'}, {'limit': True}, {'section': ''}, {'item': 'x'},
    {'section': 'x', 'limit': 0}, {'section': 'x', 'cursor': '%%%'} ,
    {'section': 'x', 'item': 'a', 'limit': 1},
])
def test_invalid_query_never_reads_sources(map_case, query):
    _, registry, _ = map_case
    registry.register(SectionDescriptor('x', '测试领域', 1, lambda _: pytest.fail('reader executed')))
    result, _ = _query(map_case, query)
    assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'


def test_open_registration_schema_and_unknown_section_are_read_free(map_case):
    _, registry, tool = map_case
    registry.register(SectionDescriptor('future', '未来领域', 1, lambda _: pytest.fail('reader executed'), '公开用途'))
    assert tool.model_spec.input_schema['properties']['section']['enum'] == ['future']
    assert '公开用途' in tool.model_spec.description
    with pytest.raises(ValueError):
        registry.register(SectionDescriptor('future', '重复', 2, lambda _: None))
    result, value = _query(map_case, {'section': 'absent'})
    assert not result.ok and value['valid_sections'] == ['future']
    assert value['next_read'] == next_read('self_map', {})


def test_partial_failure_is_unknown_count_without_exception_text(map_case):
    _, registry, _ = map_case
    def unreadable(_):
        raise OSError('secret private path')
    registry.register(SectionDescriptor('partial', '来源', 1, unreadable))
    result, value = _query(map_case, {'section': 'partial'})
    assert result.ok and value['total'] is None and value['state'] == 'unavailable'
    assert value['completeness'] == 'partial' and value['errors'] == ['SELF_MAP_SOURCE_UNAVAILABLE']
    assert 'secret' not in result.output and value['count_reason']


def test_cursor_binds_public_source_scope_and_preserves_all_items(map_case):
    _, registry, _ = map_case
    state = {'source': 'revision-one'}
    rows = tuple(map_item(str(i), '条目' + str(i)) for i in range(5))
    registry.register(SectionDescriptor('pages', '分页', 1,
        lambda _: SectionProjection(rows, source=state['source'])))
    result, first = _query(map_case, {'section': 'pages', 'limit': 2})
    assert result.ok and first['returned'] == 2 and first['total'] == 5
    query = {'section': 'pages', 'limit': 2, 'cursor': first['next_cursor']}
    _, second = _query(map_case, query)
    _, third = _query(map_case, {**query, 'cursor': second['next_cursor']})
    assert [row['id'] for page in (first, second, third) for row in page['items']] == list('01234')
    assert third['next_cursor'] is None and third['remaining'] == 0
    state['source'] = 'revision-two'
    stale, recovery = _query(map_case, query)
    assert not stale.ok and stale.error_code == 'SELF_MAP_CURSOR_STALE'
    assert recovery['next_read'] == next_read('self_map', {'section': 'pages'})


def test_default_page_fits_actual_budget_and_never_claims_partial_empty(map_case):
    agent, registry, _ = map_case
    agent.config.tool_output_preview_chars = 1800
    registry.register(SectionDescriptor('many', '目录', 1, lambda _: SectionProjection(
        tuple(map_item(str(i), '方法' + str(i), fields={'summary': '用途' * 100}) for i in range(90)))))
    result, value = _query(map_case, {'section': 'many'})
    assert result.ok and len(result.output) <= 1800
    assert value['returned'] > 0 and value['next_cursor'] and value['total'] == 90
    registry.register(SectionDescriptor('unknown', '未知', 2, lambda _: SectionProjection(
        (), 'partial', 'not_observed', count_known=False)))
    _, unknown = _query(map_case, {'section': 'unknown'})
    assert unknown['state'] == 'not_observed' and unknown['total'] is None


def test_detail_suggestion_respects_scope_and_never_executes_or_loads(map_case, monkeypatch):
    agent, registry, _ = map_case
    monkeypatch.setattr(agent.tools.tools['read_file'], 'execute', lambda _: pytest.fail('read executed'))
    registry.register(SectionDescriptor('entry', '入口', 1, lambda _: SectionProjection((
        map_item('read', '读取', fields={'next_read': next_read('read_file', {'path': 'example.txt'})}),
        map_item('write', '写入', fields={'next_read': next_read('write_file', {'path': 'x', 'content': 'y'})}),
    ))))
    _, read = _query(map_case, {'section': 'entry', 'item': 'read'}, allowed=['self_map'])
    assert read['item']['next_read']['tool'] == 'self_map'
    _, write = _query(map_case, {'section': 'entry', 'item': 'write'})
    assert write['item']['next_read']['tool'] == 'self_map'
    assert not getattr(agent, '_current_run_params', None)


# LLM: summary明确给出计数时不要求物化items；普通四参数注册仍沿原读口。
# 函数用途: 总览走轻量读取且不把未枚举条目显示为空。
def test_summary_reader_uses_proven_count_without_full_reader(map_case):
    _, registry, _ = map_case
    registry.register(SectionDescriptor('skills', '方法', 1, lambda _: pytest.fail('full reader'),
        summary_reader=lambda _: SectionProjection((), summary_count=7, summary_families={'future': 7})))
    result, value = _query(map_case, {})
    assert result.ok
    row = value['sections'][0]
    assert row['count'] == 7 and row['state'] != 'empty'
    assert '条目0' not in row['summary'] and '当前为空' not in row['summary']
    assert value['capability_families']['skills'] == {'future': 7}


# LLM: 不完整来源即使传了计数也不当完整库存；分类缺少证明保持null。
# 函数用途: 防止summary空items变成假零或假空分类。
def test_partial_summary_keeps_unknown_count_and_families(map_case):
    _, registry, _ = map_case
    registry.register(SectionDescriptor('skills', '方法', 1, lambda _: SectionProjection(()),
        summary_reader=lambda _: SectionProjection((), 'partial', summary_count=8)))
    result, value = _query(map_case, {})
    assert result.ok and value['sections'][0]['count'] is None
    assert value['capability_families']['skills'] is None


# LLM: 精确详情读者结果为空不能回退到完整列表扩大范围。
# 函数用途: 准确item只调用窄读取器，未找到保持参数错误。
def test_item_reader_never_falls_back_to_full_projection(map_case):
    _, registry, _ = map_case
    registry.register(SectionDescriptor('x', '窄入口', 1, lambda _: pytest.fail('full reader'),
        item_reader=lambda _, identity: SectionProjection((map_item(identity, identity),)) if identity == 'one' else None))
    result, value = _query(map_case, {'section': 'x', 'item': 'one'})
    assert result.ok and value['item']['id'] == 'one'
    result, _ = _query(map_case, {'section': 'x', 'item': 'missing'})
    assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'
