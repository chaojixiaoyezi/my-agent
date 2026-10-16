"""B路复核：合并不能用另一条可学投影补猜未知引用的来源。"""
import json
from dataclasses import replace
from itertools import permutations

import pytest

from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
from agent_py_agent.tests.test_review38_contracts import env as env
from agent_py_agent.tests.test_review38_contracts import observation, thread


@pytest.mark.parametrize("bad", [None, [], "broken-source", 42])
# LLM: 两种合并顺序都核验坏类型不获得另一份投影的学习权。
# 函数用途: 覆盖来源投影的非法类型。
def test_invalid_projection_does_not_inherit_learnable_authority(bad):
    user = ls.user_dialogue_projection()
    assert not ls.projection_is_learnable(ls.merge_source_projections((user, bad)))
    assert not ls.projection_is_learnable(ls.merge_source_projections((bad, user)))


@pytest.mark.parametrize("missing", [{}, None, [], "broken-source"])
@pytest.mark.parametrize("unknown_first", [False, True])
# LLM: 不同真实消息引用经候选合并和正式晋升，未知引用不能借用已知来源。
# 函数用途: 验证完整引用集合缺来源时拒绝自动学习。
def test_unknown_new_reference_cannot_borrow_other_reference_source(env, missing, unknown_first):
    good = observation(env, thread(env, ls.user_dialogue_projection(), "good"), "约定：先核对版本。")
    unknown = observation(env, thread(env, ls.unknown_projection(), "unknown"), "约定：先核对版本。")
    unknown = replace(unknown, learning_source=missing)
    observations = (unknown, good) if unknown_first else (good, unknown)
    first = env.service.candidates.observe(observations[0])
    merged = env.service.candidates.observe(observations[1])
    assert merged.candidate_id == first.candidate_id
    assert len(merged.source_message_refs) == 2
    result = confirm_candidate(env.service, merged.candidate_id)
    assert not result.promoted, (result, merged.learning_source)
    assert env.service.long_term.all() == []


@pytest.mark.parametrize("unknown_first", [False, True])
# LLM: 完全相同的引用允许补齐缺失投影，且不能增加独立观察次数。
# 函数用途: 保留同证据补来源的合法正例。
def test_same_complete_reference_can_fill_missing_source(env, unknown_first):
    good = observation(env, thread(env, ls.user_dialogue_projection()), "约定：先核对版本。")
    unknown = replace(good, learning_source={})
    first, second = (unknown, good) if unknown_first else (good, unknown)
    saved = env.service.candidates.observe(first)
    merged = env.service.candidates.observe(second)
    assert merged.candidate_id == saved.candidate_id
    assert merged.occurrence_count == 1
    result = confirm_candidate(env.service, merged.candidate_id)
    assert result.promoted, result
    assert len(env.service.long_term.all()) == 1


@pytest.mark.parametrize("bad", [None, [], "broken-source", 42])
# LLM: 坏类型在首次观察也必须明确降为unknown，不能变成兼容人工入口的空字典。
# 函数用途: 验证坏来源不能自动晋升用户事实。
def test_initial_invalid_source_cannot_auto_promote_user_fact(env, bad):
    obs = observation(env, thread(env, ls.user_dialogue_projection()), "我喜欢先核对版本。",
                      options={"ctype": "long_term_fact", "origin": "user_explicit"})
    saved = env.service.candidates.observe(replace(obs, learning_source=bad))
    assert saved.learning_source == ls.unknown_projection()
    assert not confirm_candidate(env.service, saved.candidate_id).promoted
    assert env.service.long_term.all() == []


# LLM: 旧空来源不能靠automatic参数恢复资格；显式人工审核仍保留可用路径。
# 函数用途: 同时验证空来源自动拒绝与人工审核晋升。
def test_manual_user_fact_without_projection_keeps_existing_behavior(env):
    obs = observation(env, thread(env, ls.user_dialogue_projection()), "我喜欢先核对版本。",
                      options={"ctype": "long_term_fact", "origin": "user_explicit"})
    saved = env.service.candidates.observe(replace(obs, learning_source={}))
    assert not confirm_candidate(env.service, saved.candidate_id).promoted
    assert env.service.long_term.all() == []
    env.service.candidates.transition(saved.candidate_id, "approved", reviewer="explicit-user-review")
    assert env.service.promote(saved.candidate_id, automatic=False).promoted


# LLM: 完整投影覆盖两个真实用户来源，测试只移除首次投影，不伪造引用身份或放宽晋升器。
# 函数用途: 准备缺失、局部已知、完整补齐三次观察。
def _three_observations(env):
    a = observation(env, thread(env, ls.user_dialogue_projection(), "a"), "约定：先核对版本。")
    b = observation(env, thread(env, ls.user_dialogue_projection(), "b"), a.content)
    fields = ("evidence_refs", "source_message_refs", "source_tool_refs", "source_artifact_refs",
              "source_task_ids", "source_run_ids")
    complete = replace(a, **{name: (*getattr(a, name), *getattr(b, name)) for name in fields})
    return replace(a, learning_source={}), b, complete


@pytest.mark.parametrize("order", list(permutations(range(3))))
# LLM: 每一步都从真实JSONL恢复；同一最终引用集合不应因合并顺序永久失去合法补齐机会。
# 函数用途: 验证缺失来源能被完整同引用投影补齐且顺序无关。
def test_multistep_complete_source_repair_is_order_independent(env, order):
    observations = _three_observations(env)
    for index in order:
        saved = env.service.candidates.observe(observations[index])
        saved = env.service.candidates.get(saved.candidate_id)
    assert ls.projection_is_learnable(saved.learning_source), saved.learning_source
    result = confirm_candidate(env.service, saved.candidate_id)
    assert result.promoted, result
    assert env.service.long_term.all()[0].content == observations[0].content


@pytest.mark.parametrize("bad", [None, [], "broken-source", ls.unknown_projection(), ls.dispatch_projection()])
# LLM: 明确不可学/坏类型不是“缺失待补齐”，后来完整可学投影也不能洗掉早先否决。
# 函数用途: 确保允许多步补齐不引入来源洗白。
def test_multistep_explicit_denial_cannot_be_filled(env, bad):
    missing, known, complete = _three_observations(env)
    first = env.service.candidates.observe(replace(missing, learning_source=bad))
    env.service.candidates.observe(known)
    saved = env.service.candidates.observe(complete)
    assert saved.candidate_id == first.candidate_id
    assert not ls.projection_is_learnable(saved.learning_source)
    assert not confirm_candidate(env.service, saved.candidate_id).promoted
    assert env.service.long_term.all() == []


# LLM: 老账本里unknown没有可恢复的分项覆盖证据，升级不得猜它只是临时缺失。
# 函数用途: 验证缺覆盖字段的旧unknown候选仍保持保守拒绝。
def test_legacy_unknown_without_coverage_cannot_be_laundered(env):
    missing, known, complete = _three_observations(env)
    env.service.candidates.observe(missing)
    saved = env.service.candidates.observe(known)
    row = saved.to_record()
    row.pop("learning_source_coverage", None)
    env.service.candidates.path.write_text(json.dumps(row) + "\n")
    merged = env.service.candidates.observe(complete)
    assert not ls.projection_is_learnable(merged.learning_source)
    assert not confirm_candidate(env.service, merged.candidate_id).promoted


@pytest.mark.parametrize("bad", [None, [], {"schema_version": 1},
    {"schema_version": True, "source": {}, "covered": [], "missing": []}])
# LLM: 已知字段坏形态必须阻断仓库恢复，不能回退丢弃来源否决。
# 函数用途: 覆盖持久覆盖账本的严格解析。
def test_invalid_persisted_coverage_is_rejected(env, bad):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate

    missing, _known, _complete = _three_observations(env)
    row = env.service.candidates.observe(missing).to_record()
    row["learning_source_coverage"] = bad
    with pytest.raises(ValueError, match="learning_source_coverage"):
        MemoryCandidate.from_record(row)


# LLM: 覆盖状态只由候选仓库产生，模型不能清除missing或宣称known来源。
# 函数用途: 验证策展模型输出不能注入覆盖字段。
def test_model_cannot_declare_source_coverage():
    from agent_py_agent.agent.memory_store.curator_models import _parse_candidate_observation

    with pytest.raises(ValueError, match="host-owned"):
        _parse_candidate_observation({"learning_source_coverage": {"source": ls.user_dialogue_projection()}})
