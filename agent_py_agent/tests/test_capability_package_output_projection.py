"""归档展示适配不改包读取器冻结回执，也不能影响普通工具或失败结果。"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.capability.package_output_projection import preserve_package_page_view
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.memory_archive import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolContentBlock,
    ToolResult,
    ToolResultRef,
)
from agent_py_agent.tests.test_capability_package_discovery import package_fixture
from agent_py_agent.tests.test_capability_package_read import _reader_agent


# LLM: 用真实包读取器的原回执，单独观察派生归档视图；原JSON、信封及全部已有身份字段必须不变。
# 函数用途: 核对读取与检索在不同预览预算下均保留完整JSON和包导航。
@pytest.mark.parametrize('action', ['get', 'search'])
@pytest.mark.parametrize('preview', [0, 4000])
def test_bounded_package_view_preserves_original_receipt_and_archive_identity(action, preview):
    package = package_fixture(files={'CAPABILITY.md': b'entry' * 2000, 'methods/detail.md': b'private'})
    agent, _ = _reader_agent(package, max_chars=5000, preview_chars=preview)
    result = SkillSearchTool(agent).execute({'action': action, 'package_id': package.package_id})
    original = result.output, json.dumps(result.result_envelope, sort_keys=True)
    archive = {'output_preview': '{truncated', 'output_hash': hashlib.sha256(result.output.encode()).hexdigest(),
               'output_externalized': True, 'scoped_call_id': 'run:package', 'output_path': '/host-only/archive'}
    identity = dict(archive)
    preserve_package_page_view(result, archive)
    assert archive.get('preserve_prompt_output') is True
    assert archive.get('live_prompt_output') == result.model_visible_output()
    assert json.loads(archive['live_prompt_output'])['resource_namespace']['declared_members_only'] is True
    assert {key: archive[key] for key in identity} == identity
    assert (result.output, json.dumps(result.result_envelope, sort_keys=True)) == original


# LLM: 失败或其他工具也给完整合法包namespace，确保真实命中工具身份/状态守门而非被坏namespace提前挡住。
# 函数用途: 验证普通、无效及非授权包类型的回执不启用包页面展示适配。
@pytest.mark.parametrize('tool,ok,payload', [
    ('skill_search', True, '{bad-json'), ('skill_search', True, '[]'),
    ('skill_search', True, '{"body":"ordinary Skill"}'),
    ('skill_search', False, '{"resource_namespace":{"kind":"capability_package","declared_members_only":true,"filesystem_path":false}}'),
    ('web_fetch', True, '{"resource_namespace":{"kind":"capability_package","declared_members_only":true,"filesystem_path":false}}'),
    ('skill_search', True, '{"resource_namespace":[]}'),
])
def test_non_package_or_failed_outcomes_keep_generic_projection(tool, ok, payload):
    result = ToolHandlerOutcome(tool, ok, payload)
    record = {'output_preview': 'original', 'output_externalized': True}
    preserve_package_page_view(result, record)
    assert record == {'output_preview': 'original', 'output_externalized': True}


# LLM: 真实归档器生成硬余量外置原因，真实reducer生成最终工具投影；不mock产品判断或改小包页面。
# 函数用途: 证明适配器不能在硬余量外置后新增内联正文，也不泄漏本次物理归档引用。
@pytest.mark.parametrize('preview', [0, 40000])
def test_hard_headroom_archive_keeps_original_model_projection(tmp_path, preview):
    package = package_fixture(files={'CAPABILITY.md': b'entry' * 2000})
    agent, _ = _reader_agent(package, max_chars=5000, preview_chars=preview)
    outcome = SkillSearchTool(agent).execute({'action': 'get', 'package_id': package.package_id})
    record = externalize_tool_output_record(ExternalizeToolOutputRequest(
        root=tmp_path, tool='skill_search', call_id='hard-page', run_id='hard-run',
        output=outcome.output, ok=True, force_externalize=True, min_chars=100000, preview_chars=128,
        result_envelope=outcome.result_envelope,
    ))
    assert record['output_externalized_reason'] == 'tool_result_headroom'
    physical = str(record['output_path'])
    original_bytes, original_record = Path(physical).read_bytes(), deepcopy(record)
    result = ToolResult(
        call_id='hard-page', tool_name='skill_search', status='succeeded',
        content_blocks=(ToolContentBlock('text', text=str(record['output_preview'])),
                        ToolContentBlock('ref', ref=physical)),
        refs=(ToolResultRef(kind='tool_output', ref=physical),), handler_executed=True,
        metadata={'handler_details': outcome.result_envelope},
    )
    before = render_tool_result_for_live_prompt(result, record)
    preserve_package_page_view(outcome, record)
    after = render_tool_result_for_live_prompt(result, record)
    assert after == before
    assert record == original_record and Path(physical).read_bytes() == original_bytes
    assert 'hard-run:hard-page' in after and 'read_artifact_hint' in after
    assert physical not in after
