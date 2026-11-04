"""验证摘要输出预算、缓存前缀和一次选区；模型调用只用本地替身。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.tool_protocol_adapter import anthropic_tool_choice
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.tests.test_compact_calibrated_candidate_gate import _calibration
from agent_py_agent.tests.test_compact_capacity_facts import _transcript_case
from agent_py_agent.tests.test_compact_request_budget import _request, _request_between_budgets


# LLM: 调用只记录实际请求选项，列表在浅拷贝后仍共用；不发 HTTP。
# 类用途: 观察压缩请求实际使用的输出上限。
class RecordingBackend:
    # LLM: 每个用例独立保存请求，主请求上限故意大于摘要上限。
    # 函数用途: 准备可观察的本地后端。
    def __init__(self, cap):
        self.max_tokens = cap
        self.calls = []

    # LLM: 保留真实辅助调用签名，返回纯文本，不实现任何工具执行。
    # 函数用途: 捕获实际下发的输出上限及工具面。
    def generate(self, prompt, *, messages=None, tools=None, tool_choice=None, on_chunk=None, request_options=None):
        cap = getattr(request_options, 'max_output_tokens', None) or self.max_tokens
        self.calls.append((cap, tools, tool_choice))
        return ModelResponse(text='已验证的事实摘要', backend='local')


# LLM: 必须验证供应商入口的实际参数，而非仅断言常量存在。
# 函数用途: 摘要封顶 4096，但不抬高较小上限或修改共享后端。
@pytest.mark.parametrize('normal,expected', [(16384, 4096), (1024, 1024)])
def test_summary_output_cap_is_request_local(normal, expected):
    request = _request('已完成的事实')
    backend = RecordingBackend(normal)
    agent = SimpleNamespace(backend=backend, config=SimpleNamespace(model_context_window_tokens=100000))
    response = budget.generate_bounded_compact_response(replace(request, agent=agent))
    assert response.text == '已验证的事实摘要'
    assert backend.calls[0][0] == expected
    assert backend.max_tokens == normal


# LLM: 冻结主请求选择策略及 schema，前台不能用另一个 auto/none 改写缓存面。
# 函数用途: 逐字段比较传入与发出的前台前缀。
@pytest.mark.parametrize('choice', [None, ToolChoice.auto('foreground'), ToolChoice.specific('read_file')])
def test_foreground_tools_choice_and_system_are_unchanged(monkeypatch, choice):
    request = replace(_request('按原样总结'), tool_choice=choice)
    original = deepcopy((request.tools, request.tool_choice, request.messages, request.system_instruction))
    calls = []
    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', lambda r: calls.append(r) or ModelResponse(text='事实', backend='local'))
    budget.generate_bounded_compact_response(request)
    assert len(calls) == 1
    assert (calls[0].tools, calls[0].messages, calls[0].system_instruction) == (original[0], original[2], original[3])
    assert anthropic_tool_choice(calls[0].tool_choice or ToolChoice.auto()) == anthropic_tool_choice(choice or ToolChoice.auto())
    if choice is not None:
        assert calls[0].tool_choice == choice
    assert (request.tools, request.tool_choice, request.messages, request.system_instruction) == original


# LLM: 请求大小处于原始无工具预算之外，但冻结校准后可容纳，禁止误切分或丢工具。
# 函数用途: 工具调用响应只触发一次缓存安全修复，并使用校准计量。
def test_tool_reply_repair_uses_calibrated_same_prefix(monkeypatch):
    base = _request('')
    request = _request_between_budgets(base, budget.compact_summary_budget(base.agent), budget.compact_cache_surface_budget(base.agent))
    calls = []

    # LLM: 第一条结构化工具块是无效摘要，不得执行；第二条提供正文。
    # 函数用途: 捕获两次摘要请求并控制响应形状。
    def generate(candidate):
        calls.append(candidate)
        return ModelResponse(text='事实' if len(calls) > 1 else '', backend='local',
                             tool_use_blocks=[] if len(calls) > 1 else [{'id': 'unused', 'name': 'read_file', 'input': {}}])

    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', generate)
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    result = budget.generate_bounded_compact_response(replace(request, messages=None), message_source=source, calibration=_calibration())
    assert result.text == '事实' and len(calls) == 2
    for candidate in calls:
        assert candidate.tools == request.tools
        assert anthropic_tool_choice(candidate.tool_choice or ToolChoice.auto()) == anthropic_tool_choice(request.tool_choice or ToolChoice.auto())
        assert candidate.messages == request.messages and candidate.system_instruction == request.system_instruction


# LLM: 闲置标志来自宿主调用，不从提示文案推断；通过真正摘要构造入口观察无工具请求。
# 函数用途: 闲置摘要允许冷请求，但不修改前台冻结面。
def test_idle_summary_drops_tools_only_for_idle_call(monkeypatch):
    surface = ConversationCompactProviderSurface(stable_prompt_prefix='稳定前缀', tools=({'name': 'read_file'},), system_instruction='原系统')
    agent = _request('').agent
    calls = []
    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', lambda r: calls.append(r) or ModelResponse(text='事实', backend='local'))
    compact._summarize(agent, '', {}, [], call=compact._CompactSummaryCall(provider_surface=surface, idle_compact=True))
    assert len(calls) == 1 and calls[0].tools == [] and calls[0].tool_choice.mode == 'none'
    assert surface.tools == ({'name': 'read_file'},) and surface.system_instruction == '原系统'


# LLM: 指令只要求事实归档，仍保留完整用户要求、未完成请求和证据要求。
# 函数用途: 防止收紧摘要时丢掉必需章节。
def test_summary_instruction_is_direct_factual_archival():
    text = compact._conversation_summary_instruction({}, custom_instructions='', cache_safe=True)
    assert 'factual archival' in text
    assert 'meta commentary' in text
    assert 'User requirements' in text and 'Pending user requests' in text
    assert 'verified results' in text and 'Do not call tools' in text


# LLM: 真正 Store、候选、投影及 CAS 不替换；较软恢复目标不能导致第二次模型摘要。
# 函数用途: 在目标水位未达到但仍可容纳时只调用一次摘要模型。
def test_split_point_is_selected_before_the_single_summary_call(tmp_path, monkeypatch):
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=6)
    agent.backend.max_tokens = 64
    calls = []
    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', lambda r: calls.append(r) or ModelResponse(text='真实已完成事实', backend='local'))
    surface = ConversationCompactProviderSurface('稳定前缀', (), '原系统')

    # LLM: 该投影器是宿主的纯计量输入；候选落在软目标与硬上限之间。
    # 函数用途: 产生可提交但仍高于恢复目标的完整下一请求大小。
    def project(view):
        return ConversationCompactProjection(7000 if view.is_candidate else 12000, object())

    result = compact.prepare_conversation_context(agent, store, thread, options=compact.ConversationCompactOptions(
        current_prompt='继续原任务', provider_surface=surface, request_projector=project))
    assert result.compacted and len(calls) == 1
    assert result.projected_tokens == 7000
    assert store.threads.load(thread.thread_id).compact_generation == 1
