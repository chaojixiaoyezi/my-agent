"""Jev 2：recall 点位请求体的单一来源与缩小输入（任务 7）。

来源：dev 2026-09-28 派活任务 7（从 my-agent-1 转来）。背景：前台点位平时超时约 22%，
中位耗时约 2.3 秒，每次输入约 7.5K token。做法与 my-agent-4 修 curator 的原则一致：
一次请求里同一份材料只放一次，能用结构化元数据代替正文的就代替；不做缓存。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store import decision_recall as dr
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)


def _record(index: int, *, body: str = "记忆正文", kind: str = "fact") -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=f"mem-{index}", version=1, kind=kind, role="user", content=body,
        source="conversation", created_at=1790000000.0, updated_at=1790000000.0,
        expires_at=None, attributes={"scope_type": "personal", "scope_key": "personal"},
    )


def _material(agent, records):
    request = SimpleNamespace(user_prompt="帮我看看这个项目的配置怎么改", task_attributes=None)
    scope = SimpleNamespace(keys=[["personal", "personal"]])
    return dr._material(agent, request, records, scope)


def _chars(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def test_recall_request_omits_metadata_the_decision_never_reads(tmp_path):
    """决策只按正文和来源身份排优先级；时间戳与来源渠道不提供增量信息，不该占用请求预算。"""
    state, questions, _revision = _material(_agent(tmp_path), [_record(i) for i in range(3)])
    for row in state["memories"]:
        assert set(row) == {"entry_id", "kind", "content", "attributes"}, (
            f"recall 请求里出现了决策用不到的字段: {sorted(row)}"
        )


def test_recall_request_keeps_attributes_because_scope_binding_compares_them(tmp_path):
    """attributes 参与本轮绑定校验（scope 变化必须能被比对成 stale），缩小输入不能把它一起删掉。"""
    state, _questions, _revision = _material(_agent(tmp_path), [_record(i) for i in range(3)])
    assert all(row["attributes"] == {"scope_type": "personal", "scope_key": "personal"}
               for row in state["memories"])


def test_recall_need_data_note_is_not_repeated_per_question(tmp_path):
    """need_data 的长说明每题都一样，放一份在 state；题内只留必须逐题的 required_refs 与 entry_id。"""
    state, questions, _revision = _material(_agent(tmp_path), [_record(i) for i in range(4)])
    assert "need_data_note" in state
    for key, question in questions.items():
        need_data = question["criteria"]["need_data"]
        assert set(need_data) == {"required_refs"}, f"{key} 仍带着逐题重复的 need_data 说明"
        # 身份引用必须逐题保留：它指向本条记忆，不能共享。
        assert need_data["required_refs"] == [
            {"kind": "memory_source_ref", "ref": f"memory:{question['instructions']['entry_id']}"}
        ]


def test_recall_choice_criteria_keeps_full_option_set_for_every_question(tmp_path):
    """criteria 是决策协议的必需字段（模型和假后端都靠它选答案），形状与选项必须原样保留。"""
    state, questions, _revision = _material(_agent(tmp_path), [_record(i) for i in range(3)])
    expected = {"first", "normal", "later", "not_needed", "no_match", "abstain", "need_data"}
    assert len(state["memories"]) == 3 and questions
    for question in questions.values():
        assert set(question["criteria"]) == expected
        assert question["type"] == "choice"


def test_recall_short_content_is_sent_unchanged(tmp_path):
    """正常长度的记忆原样送：安全上限只防超长条目，不做一刀切截断（dev 定的口径）。"""
    body = "短正文" * 100  # 300 字符 < 800
    state, _questions, _revision = _material(_agent(tmp_path), [_record(0, body=body)])
    row = state["memories"][0]
    assert row["content"] == body
    assert "content_truncated" not in row


def test_recall_overlong_content_is_capped_with_a_structured_marker(tmp_path):
    """超长条目才截断，且必须带结构化标记让模型知道这条被截了、原长多少。"""
    from agent_py_agent.agent.conversation import decision_point_limits as limits

    body = "长正文" * 1000  # 3000 字符 > 800
    state, _questions, _revision = _material(_agent(tmp_path), [_record(0, body=body)])
    row = state["memories"][0]
    assert len(row["content"]) == limits.RECALL_CONTENT_MAX_CHARS
    assert row["content_truncated"] is True
    assert row["content_original_chars"] == len(body)


def test_recall_content_cap_is_the_frozen_named_constant(tmp_path):
    """上限必须来自具名常量（不新增用户参数），钉住值防止被顺手改掉。"""
    from agent_py_agent.agent.conversation import decision_point_limits as limits

    assert limits.RECALL_CONTENT_MAX_CHARS == 800


def test_recall_request_shrinks_compared_with_the_metadata_carrying_form(tmp_path):
    """钉住缩小输入的实际收益：去掉元数据后，同一批材料的请求体必须明显更小。"""
    records = [_record(i, body="这是一条比较长的记忆正文。" * 20) for i in range(6)]
    state, questions, _revision = _material(_agent(tmp_path), records)

    shrunken = _chars({"state": state, "questions": questions})
    # 用同一批记录的完整视图重建"改前"形态，比较同一批材料下的体积差。
    views = [dr._record_view(record) for record in records]
    before_state = {**state, "memories": views}
    before_state.pop("need_data_note", None)
    original = _chars({"state": before_state, "questions": questions})

    assert shrunken < original, "缩小输入没有生效"
    saved_ratio = (original - shrunken) / original
    assert saved_ratio > 0.05, f"收益过小（{saved_ratio:.1%}），元数据可能没被真正去掉"
