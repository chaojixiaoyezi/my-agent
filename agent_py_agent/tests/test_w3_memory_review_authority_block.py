"""The memory review preview must not offer a confirmation that the promotion rules always refuse.

Live clone (2026-11-17): a model_inferred long-term fact waited in pending_review, the preview offered
/memory candidates confirm, and the confirmation only answered "正式内容未提交，请核对原因后重新预览" because
promotion._absolute_authority_block refuses model inference even after human review
(MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN).  The user could not learn why.
"""
from __future__ import annotations

import json

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.tests.test_memory_candidate_control import (
    memory_control,  # noqa: F401 - pytest fixture
)


def _inferred_candidate(control):
    draft = RememberTool(control.agent).execute(dict(content='用户偏好先看结论', kind='fact', origin='model_inferred',
                                                     subject_key='memory.inferred'))
    assert draft.ok and not json.loads(draft.output)['saved']
    return next(row for row in control.agent.memory_candidates.list() if row.origin == 'model_inferred')


def test_preview_of_an_always_refused_candidate_offers_no_confirm(memory_control):  # noqa: F811
    c = memory_control
    candidate = _inferred_candidate(c)
    code, result = c.send('/memory candidates preview ' + candidate.candidate_id)
    assert code == 200 and result['ok'], result
    assert result['details']['confirm_blocked_reason'] == 'MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN'
    assert 'candidates confirm' not in result['message']
    assert 'candidates reject' in result['message'] and 'MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN' in result['message']
    refused = c.send(result['details']['confirm_command'])[1]
    assert refused['details']['state'] == 'not_committed'
    assert 'MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN' in refused['message']
    assert c.agent.memory.all() == []


def test_regular_candidate_preview_still_offers_confirm(memory_control):  # noqa: F811
    c = memory_control
    code, result = c.send('/memory candidates preview ' + c.candidate.candidate_id)
    assert code == 200 and 'confirm_blocked_reason' not in result['details']
    assert result['details']['confirm_command'] in result['message']
