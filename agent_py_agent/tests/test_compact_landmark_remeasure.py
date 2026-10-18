# LLM: 仅纯计量/原render/原硬门与受控异常；不调模型/服务、不构造关闭或权限证明，全部行为须两OS包pyc运行。
# 模块用途: 验证局部备份估算不能跳过完整重测，保护材料不丢，不变小/仍超限/中断保持原拒绝。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    projected_model_context_components,
)
from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestProjection
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_landmarks import (
    LandmarkOptions,
    landmark_source,
    render_landmarked_summary,
    semantic_summary_text,
)
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.tests.test_compact_landmarks import _Rebuild


def test_original_render_and_full_projection_remeasure_keep_protected_material():
    semantic, trailing = '必须保留的语义摘要。', '\n必须保留的机械尾文。'
    rows = [MessageLogEntry(message_id='source-1', thread_id='t', role='user', content='原始要求。' * 500)]
    source = landmark_source('', rows)
    options = LandmarkOptions(landmark_budget_tokens=1200)
    rendered = render_landmarked_summary(semantic, source, options)
    rebuild = compact._LandmarkRebuild(semantic, source, trailing, options, rendered.used_tokens)
    protected = (MessageLogEntry(message_id='tail-1', thread_id='t', role='user', content='保留的当前输入。'),)
    facts = RuntimeFactsTurn(text='保留的结构化状态', source='fixture_state')
    evidence = {'created': ['synthetic.txt']}
    views = []

    def project(view):
        views.append(view)
        assert view.messages == protected and view.operation_evidence == evidence
        assert semantic_summary_text(view.summary).startswith(semantic)
        assert view.summary.endswith(trailing)
        projection = ToolLoopRequestProjection(
            status='ready', provider_prompt='稳定主请求', system_instruction='稳定系统指令', tools=[],
            messages=[{'role': 'user', 'content': view.summary}, {'role': 'assistant', 'content': view.summary},
                      {'role': 'user', 'content': protected[0].content + facts.text}],
        )
        tokens, _ = projected_model_context_components(projection)
        return ConversationCompactProjection(tokens, projection, (facts,))

    request = SimpleNamespace(interrupt_check=None, request_projector=project,
                              thread=SimpleNamespace(thread_id='t', compact_generation=0),
                              policy=SimpleNamespace(trigger_tokens=20_000), tool_source=None, calibration=None)
    tail = compact._CandidateTail(protected, evidence)
    initial = compact._measure_candidate(request, tail, rendered.text + trailing)
    minimum = rebuild.minimum_tokens()
    ceiling = initial.tokens - rebuild.used_tokens + minimum
    limits = compact._CandidateLimits(target=max(1, initial.tokens - 2 * rebuild.used_tokens), ceiling=ceiling)
    fitted = compact._fit_landmarks_to_target(
        limits, initial, [rebuild], lambda summary: compact._measure_candidate(request, tail, summary),
    )
    assert len(views) == 2
    assert fitted.tokens < ceiling <= initial.tokens
    assert fitted.projection.retained_ir_history == initial.projection.retained_ir_history == (facts,)
    assert views[0].thread_id == views[1].thread_id and views[0].compact_generation == views[1].compact_generation
    assert evidence == {'created': ['synthetic.txt']} and rows[0].content == '原始要求。' * 500


@pytest.mark.parametrize('actual_tokens', [1200, 1300])
def test_full_remeasure_not_strictly_smaller_keeps_original_object(actual_tokens):
    initial = compact._MeasuredSummary('original', None, 1200)
    rebuild = _Rebuild(100)
    calls = []

    def remeasure(summary):
        calls.append(summary)
        return compact._MeasuredSummary(summary, None, actual_tokens)

    fitted = compact._fit_landmarks_to_target(compact._CandidateLimits(1000, 1500), initial, [rebuild], remeasure)
    assert fitted is initial and calls == ['smaller-0'] and rebuild.budgets == [-100]


def test_smaller_full_remeasurement_still_at_ceiling_is_rejected_by_original_gate():
    rebuild = _Rebuild(100)
    calls = []

    def remeasure(summary):
        calls.append(summary)
        return compact._MeasuredSummary(summary, None, 1500)

    fitted = compact._fit_landmarks_to_target(
        compact._CandidateLimits(1000, 1500), compact._MeasuredSummary('original', None, 2000), [rebuild], remeasure,
    )
    candidate = compact._CompactCandidate(fitted.summary, {}, (), (), fitted.tokens)
    rejected = compact._RejectedCandidates(SimpleNamespace(tool_source=None))
    assert fitted.tokens == 1500 and calls == ['smaller-0'] and rebuild.budgets == [-900]
    assert rejected.reject_if_over(candidate, 1500) is True
    assert rejected.count == 1 and rejected.smallest.candidate_tokens == 1500


@pytest.mark.parametrize('error', [InterruptedError('controlled interruption'), ToolCancelled('controlled cancellation'),
                                  ConversationCompactError('controlled projection failure', code='COMPACT_REQUEST_PROJECTION_UNKNOWN')])
def test_remeasurement_interruption_and_typed_failure_propagate_exactly_once(error):
    calls = []
    rebuild = _Rebuild(100)

    def remeasure(summary):
        calls.append(summary)
        raise error

    with pytest.raises(type(error)) as caught:
        compact._fit_landmarks_to_target(
            compact._CandidateLimits(1000, 1500), compact._MeasuredSummary('original', None, 2000), [rebuild], remeasure,
        )
    assert caught.value is error and calls == ['smaller-0'] and rebuild.budgets == [-900]
