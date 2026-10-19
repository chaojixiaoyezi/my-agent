"""零结果查询事实、来源归因和 canonical 模型复查软指导回归。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.ingestion import source_adapter
from agent_py_agent.agent.ingestion.source_http import (
    SourceHttpRequest,
    public_rendered_source_request,
)
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import _filesystem_search as fs
from agent_py_agent.agent.tooling.query_evidence import (
    QueryObservation,
    query_evidence,
    query_zero_result_guidance,
)
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.agent.tooling.web_search import WebSearchProvider, WebSearchTool


@pytest.mark.parametrize('query', ['准确字段 AND 多余条件', '中文可以有效', 'missing_identifier'])
def test_local_zero_preserves_exact_conditions_and_soft_review(tmp_path, query):
    (tmp_path / 'source.txt').write_text('真实记录 value\n')
    result = fs.SearchTextTool(tmp_path, 10).execute({'query': query, 'literal': True, 'file_glob': '*.txt'})
    facts = result.result_envelope['query_evidence']
    assert facts['conditions']['query'] == query
    assert facts['conditions']['file_glob'] == '*.txt'
    assert facts['status'] == 'zero_observed' and facts['returned_count'] == 0
    assert facts['total_known'] and facts['total_count'] == 0
    assert facts['review_required'] and facts['review']['baseline_budget'] == 1
    assert not facts['review']['automatic_request']
    assert facts['source_truth'] == 'not_verified'


def test_empty_page_is_not_zero_source(tmp_path):
    (tmp_path / 'data.txt').write_text('存在\n')
    result = fs.SearchTextTool(tmp_path, 10).execute({'query': '存在', 'offset': 5, 'literal': True})
    facts = result.result_envelope['query_evidence']
    assert facts['status'] == 'empty_page'
    assert facts['observed_count'] == 1 and facts['returned_count'] == 0
    assert not facts['review_required']


def test_partial_scan_cannot_be_complete_zero(tmp_path, monkeypatch):
    (tmp_path / 'data.txt').write_text('存在\n')
    monkeypatch.setattr(fs.shutil, 'which', lambda *_: None)
    monkeypatch.setattr(fs, '_FALLBACK_SCAN_MAX_FILES', 0)
    result = fs.SearchTextTool(tmp_path, 10).execute({'query': '不存在', 'literal': True})
    facts = result.result_envelope['query_evidence']
    assert facts['status'] == 'unknown' and facts['coverage'] == 'incomplete'
    assert not facts['total_known'] and facts['total_count'] is None


def test_malformed_rg_cannot_silently_become_empty_result(tmp_path, monkeypatch):
    from io import StringIO
    (tmp_path / 'data.txt').write_text('存在\n')
    process = SimpleNamespace(stdout=StringIO('broken json\n'), poll=lambda: 0, wait=lambda **_: 0)
    monkeypatch.setattr(fs, '_start_rg_process', lambda *_: process)
    result = fs.SearchTextTool(tmp_path, 10).execute({'query': '存在', 'literal': True})
    assert result.result_envelope['search_result']['backend'] == 'python'
    assert result.result_envelope['query_evidence']['returned_count'] == 1


# LLM: provider 替身只返回声明候选，不反向定义产品的零结果语义。
# 类用途: 控制查询窗口、失败与调用次数，保证回归不发真实网络。
class Provider(WebSearchProvider):
    name = 'fake-source'
    # LLM: 替身不执行网络，只准备固定来源窗口。
    # 函数用途: 保存可控候选和调用记录。
    def __init__(self, rows=None, error=False):
        self.rows = rows or []
        self.error = error
        self.calls = []
    # LLM: 不解析自然语言来改变查询语义。
    # 函数用途: 按测试声明返回候选或失败。
    def search(self, query, limit):
        self.calls.append((query, limit))
        if self.error:
            raise RuntimeError('synthetic-secret-in-exception')
        return self.rows


def test_provider_empty_unknown_and_filters_are_actual_without_auto_baseline():
    provider = Provider()
    tool = WebSearchTool(timeout=1, providers=[provider])
    result = tool.execute({'query': '中文报警', 'limit': 2, 'allowed_domains': ['EXAMPLE.TEST']})
    facts = result.result_envelope['query_evidence']
    assert facts['conditions'] == {'query': '中文报警', 'limit': 2, 'allowed_domains': ['example.test'], 'blocked_domains': []}
    assert facts['coverage'] == 'provider_window' and not facts['total_known']
    assert facts['status'] == 'unknown' and facts['review_required']
    assert provider.calls == [('中文报警', 2)]
    assert facts['provider_observations'][0]['parser_version'] == 'unknown'


def test_provider_failure_not_normal_zero_or_secret_echo():
    provider = Provider(error=True)
    result = WebSearchTool(timeout=1, providers=[provider]).execute({'query': 'api_key=synthetic-private-key'})
    facts = result.result_envelope['query_evidence']
    assert not result.ok and facts['status'] == 'failed'
    assert not facts['review_required']
    assert 'synthetic-private-key' not in result.output
    assert 'synthetic-secret-in-exception' not in result.output
    assert len(provider.calls) == 1


def test_domain_filtered_zero_preserves_provider_observation():
    provider = Provider([{'title': 'data', 'url': 'https://outside.test/a', 'snippet': 'x'}])
    result = WebSearchTool(timeout=1, providers=[provider]).execute({'query': 'data', 'allowed_domains': ['within.test']})
    facts = result.result_envelope['query_evidence']
    assert facts['returned_count'] == 0 and facts['provider_observations'][0]['returned_count'] == 1
    assert facts['total_count'] is None and facts['conditions']['allowed_domains'] == ['within.test']


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('owner_kind', ['main', 'user', 'group'])
def test_canonical_system_override_and_all_owners_share_switch(tmp_path, native, owner_kind):
    # 此用例只核查询开关，独立的执行中沟通指导由共同启用用例覆盖。
    config = AgentConfig(prompt_files=[], assistant_progress_updates_enabled=False)
    config.query_zero_result_review_enabled = True
    home = SimpleNamespace(owner_provider='local' if owner_kind == 'main' else 'feishu', owner_kind=owner_kind, owner_id='owner')
    builder = PromptBuilder(config, tmp_path, home_paths=home)
    request = PromptBuildRequest('复查', [], system_prompt_override='子代理或后台原系统说明',
                                tools=ToolSections(native_tool_use=native), workspace_context_override='原工作区')
    prepared = builder.prepare_render_input(request)
    assert prepared.system_prompt.startswith('子代理或后台原系统说明')
    assert prepared.system_prompt.count('查询结果纪律') == 1
    assert '最多做一次' in prepared.system_prompt and 'shell/自定义脚本/MCP' in prepared.system_prompt
    config.query_zero_result_review_enabled = False
    assert builder.prepare_render_input(request).system_prompt == '子代理或后台原系统说明'


def test_source_http_actual_request_redacts_all_auth_slots():
    request = SourceHttpRequest('https://user:url-secret@example.test/search?q=中文&custom=query-secret&token=token-secret&after=7',
        'POST', {'Authorization': 'Bearer header-secret', 'X-Custom': 'header-private'},
        json.dumps({'filter': {'field': '准确键', 'value': '中文', 'slot': 'body-secret'}, 'password': 'password-secret'}).encode(),
        ({'location': 'query', 'name': 'custom', 'secret_ref': 'env:QUERY_PRIVATE'},
         {'location': 'json_body', 'path': ['filter', 'slot'], 'secret_ref': 'env:BODY_PRIVATE'}), 'cred_sensitive_ref')
    facts = public_rendered_source_request(request)
    assert facts['method'] == 'POST' and facts['query'][0] == ['q', '中文']
    assert facts['json_body']['filter']['field'] == '准确键'
    assert facts['authentication_configured']
    encoded = json.dumps(facts)
    for secret in ['url-secret', 'query-secret', 'token-secret', 'header-secret', 'header-private', 'body-secret',
                   'password-secret', 'QUERY_PRIVATE', 'BODY_PRIVATE', 'cred_sensitive_ref']:
        assert secret not in encoded


def test_pinned_parser_zero_attributed_not_source_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(source_adapter, '_invoke', lambda *_: {'records': [], 'checkpoint': {}, 'has_more': False, 'record_keys': []})
    request = SourceHttpRequest('https://example.test?category=中文&after=3', 'GET', {}, None)
    page = source_adapter.accept_source_response(owner_home=tmp_path, audit_id='audit-test',
        adapter={'path': 'adapter.py', 'sha256': 'a' * 64}, checkpoint={}, context={}, response={'unparsed': [1]},
        max_records=3, actual_request=request)
    facts = page.query_evidence
    assert facts['parser_version'] == 'a' * 64 and facts['count_unit'] == 'parser_records'
    assert facts['returned_count'] == 0 and facts['source_truth'] == 'not_verified'
    assert facts['coverage'] == 'parser_observation' and facts['status'] == 'unknown'
    assert facts['request_known'] and facts['review_required']


def test_no_protocol_cannot_fabricate_verified_query_from_stdout():
    config = SimpleNamespace(query_zero_result_review_enabled=True)
    hint = query_zero_result_guidance(config)
    assert '不能从 stdout 猜查询条件' in hint
    observation = query_evidence(QueryObservation('declared_query', {'query': 'valid'}, 0, 0, 'unknown', 'unknown'))
    assert observation['source_truth'] == 'not_verified' and observation['total_count'] is None


# LLM: 脚本化假 LLM 只发真实 native 工具调用；它按固定回放步骤复查，不把建议当自动执行保证。
# 类用途: 用原 SimpleAgent 工具循环验证初始零结果→有界基线阳性或中文零结果→未知的回放。
class QueryReplayBackend(BaseBackend):
    name = 'query-replay'
    # LLM: 回放只按固定数据步骤派工具，不能成为产品自动复查逻辑。
    # 函数用途: 保存两次查询及最终模型回复。
    def __init__(self, initial, baseline, final):
        self.queries = [initial, baseline]
        self.final = final
        self.prompts = []
        self.messages = []
    # LLM: 测试后端显式声明 native 协议，不访问供应商。
    # 函数用途: 提供原工具循环所需协议事实。
    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint='local://query-replay', model='fake', stream=False,
                                      native_supported=True, evidence='test_declared_native')
    # LLM: 检查真实原生 messages 后继续固定回放，结果仍由原工具运行器写入。
    # 函数用途: 模拟发现、初始查询、一次基线和自然收口。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.prompts.append(prompt)
        self.messages.append(json.dumps(kwargs.get("messages") or [], ensure_ascii=False, default=str))
        index = len(self.prompts) - 1
        assert '查询结果纪律' in prompt
        if index == 0:
            return ModelResponse('', self.name, tool_use_blocks=[{'id': 'discover', 'name': 'tool_search',
                'input': {'query': 'search_text'}}])
        index -= 1
        if index < len(self.queries):
            if index:
                assert 'review_required' in self.messages[-1]
            return ModelResponse('', self.name, tool_use_blocks=[{'id': f'query-{index}', 'name': 'search_text',
                'input': {'query': self.queries[index], 'path': 'source.txt', 'literal': True}}])
        return ModelResponse(self.final, self.name)


@pytest.mark.parametrize(('initial', 'baseline', 'final'), [('正确键 AND 多余条件', '正确键', '基线已找到记录'),
                                                            ('合法中文不存在', '合法中文', '当前查询仍未找到，来源是否存在未知')])
def test_fake_llm_original_tool_loop_bounded_baseline_replay(tmp_path, initial, baseline, final):
    agent = SimpleAgent(AgentConfig(enable_tools=True, prompt_files=[], memory_path='memory.jsonl'), tmp_path)
    (agent.home_paths.owner_home_dir / 'source.txt').write_text('正确键=存在\n')
    backend = QueryReplayBackend(initial, baseline, final)
    agent.backend = backend
    result = agent.run('检查现有来源，必要时一次有界复查', save=False)
    assert result.response == final and result.tool_rounds == 3
    assert len(backend.prompts) == 4
    assert 'query_evidence.v1' in '\n'.join(backend.messages)
    assert 'source_truth' in '\n'.join(backend.messages)
    if final == '基线已找到记录':
        assert '正确键=存在' in backend.messages[-1]
    else:
        assert 'zero_observed' in backend.messages[-1]


def test_live_cursor_query_uses_effective_request_not_old_probe(tmp_path):
    import time

    from agent_py_agent.agent.ingestion.puller import DrainBudget, drain_source
    requests = []
    def fetch(request):
        requests.append(request)
        return True, {'items': [], 'next_cursor': 8, 'has_more': False}, ''
    envelope = {'request': {'method': 'GET', 'cursor_binding': {'location': 'query', 'name': 'after', 'initial': 0},
                            'page_size_binding': {'location': 'query', 'name': 'take'}},
                'record_list_key': 'items', 'cursor_field': 'next_cursor', 'has_more_field': 'has_more'}
    result = drain_source(fetch, 'https://example.test?category=中文', 7,
                          DrainBudget(4, 3, time.time() + 5), source_envelope=envelope)
    facts = result.query_evidence
    assert len(requests) == 1
    assert ['after', '7'] in facts['conditions']['query']
    assert ['take', '3'] in facts['conditions']['query']
    assert ['category', '中文'] in facts['conditions']['query']
    assert facts['status'] == 'unknown' and facts['returned_count'] == 0 and not facts['total_known']


def test_poll_not_due_and_denied_http_never_fabricate_normal_zero():
    from agent_py_agent.agent.ingestion.sources import drain_poll_source
    calls = []
    def fetch(request):
        calls.append(request)
        return False, 'not authorized', 'TOOL_PERMISSION_DENIED'
    envelope = {'request': {'method': 'GET'}}
    result = drain_poll_source(fetch, 'https://example.test', 2, due=False, source_envelope=envelope)
    assert not calls and not result.query_evidence
    result = drain_poll_source(fetch, 'https://example.test', 2, due=True, source_envelope=envelope)
    assert len(calls) == 1 and result.error_code == 'TOOL_PERMISSION_DENIED' and not result.query_evidence


def test_inline_pull_projects_latest_query_into_original_receipt(monkeypatch):
    from agent_py_agent.agent.ingestion import watch_tool
    evidence = query_evidence(QueryObservation('declared_source', {'key': 'exact'}, 0, 0, 'unknown', 'pinned-parser'))
    def drain(_tool, _state, aggregate):
        aggregate['query_evidence'] = evidence
        return SimpleNamespace(candidates=[object()])
    monkeypatch.setattr(watch_tool, '_drain_and_digest', drain)
    monkeypatch.setattr(watch_tool, 'render_pull_payload', lambda *_: {'ok': True, 'action': 'pull'})
    result = watch_tool._pull_locked(object(), object(), 0)
    assert result.result_envelope['query_evidence'] == evidence
    assert json.loads(result.output)['query_evidence'] == evidence


def test_query_receipt_never_changes_source_configuration_authority():
    from agent_py_agent.agent.ingestion.source_binding import audit_source_config_version
    envelope = {'mode': 'adapter', 'record_boundary': 'adapter_records', 'request': {'method': 'GET'},
                'adapter': {'path': 'parser.py', 'sha256': 'a' * 64}}
    options = {'source_url': 'https://example.test', 'source_mode': 'adapter', 'poll_query_seconds': 1}
    before = audit_source_config_version(source_envelope=envelope, **options)
    envelope['query_evidence'] = {'returned_count': 0, 'conditions': {'after': 9}}
    envelope['query_observations'] = [{'returned_count': 10}]
    assert audit_source_config_version(source_envelope=envelope, **options) == before
