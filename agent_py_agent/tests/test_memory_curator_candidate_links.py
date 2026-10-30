# LLM: 真实Curator租约、解析、事务及候选服务，只用离线脚本后端；范围键与提示字段须同权威合同一致，不发模型网络调用。
# 模块用途: 验证规范范围锚点、提示字段和结构化链接贯穿生产策展入口。
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_backend import (
    curator_prompt,
    fit_batch_to_input_budget,
)
from agent_py_agent.agent.memory_store.curator_inputs import attach_candidate_anchors
from agent_py_agent.agent.memory_store.curator_models import curator_response_schema
from agent_py_agent.tests.test_curator_input_budget import _batch
from agent_py_agent.tests.test_curator_observation_identity import (
    _candidate,
    _conversation,
    _output,
    _service,
)


# LLM: 后端只提出已给锚点的ID与规范scope；同时核验会话lesson/HOT和个人范围，保留真实游标及候选账本收敛断言。
# 函数用途: 在一次真实Curator运行中消费同范围锚点并链接重复经验，禁止混入其他范围候选。
@pytest.mark.parametrize("scope_type", ["personal", "session"])
@pytest.mark.parametrize("candidate_type,promotion_target", [("lesson", "lesson"), ("hot_rule", "hot")])
def test_curator_injects_same_scope_anchors_and_merges_linked_observation(
    tmp_path, scope_type, candidate_type, promotion_target,
):
    store, _thread, message = _conversation(tmp_path)
    seen = []

    # LLM: 仅作为Curator测试的离线结构化后端，不发网络请求；必须使用输入已呈现的锚点身份。
    # 类用途: 记录真实生产提示并提出同范围的重复经验观察。
    class Backend:
        name = "anchor-fixture"

        # LLM: 响应遵守当前schema并复制锚点scope；不自行授予晋升权，后续合并仍由真实候选服务验证。
        # 函数用途: 返回带link_candidate_id的脚本响应，记录本次模型实际收到的提示。
        def generate_structured(self, prompt, *, response_schema):
            seen.append(prompt)
            assert "link_candidate_id" in response_schema["properties"]["candidates"]["items"]["properties"]
            item = _candidate(message.message_id, "核实证据后再下结论（新表述）", "proof.first")
            item.update(candidate_type=candidate_type, promotion_target=promotion_target, origin="model_inferred",
                link_candidate_id=anchor.candidate_id, scope=dict(anchor.scope))
            return ModelResponse(text=json.dumps(_output(message, [item])), backend=self.name)

    curator = _service(tmp_path, Backend(), store)
    scope_key = "personal" if scope_type == "personal" else f"session:{message.thread_id}"
    anchor = curator.candidate_service.observe(CandidateObservation(candidate_type=candidate_type, content="先核实证据",
        subject_key="proof.first", scope=MemoryScope(scope_type, scope_key), origin="model_inferred",
        promotion_target=promotion_target, source_message_refs=({"message_id": "prior", "thread_id": "prior-thread"},)))
    foreign = curator.candidate_service.observe(CandidateObservation(candidate_type="lesson", content="其他项目私有经验",
        subject_key="foreign", scope=MemoryScope("project", "project:unrelated"), origin="model_inferred",
        promotion_target="lesson"))
    result = curator.run(reason="admin")
    assert result.status == "succeeded", result
    assert "candidate_anchors" in seen[0] and anchor.candidate_id in seen[0]
    payload = json.loads(seen[0].split("待提炼经历 JSON：\n", 1)[1])
    assert [(row["candidate_id"], row["scope"]["scope_key"]) for row in payload["candidate_anchors"]] == [
        (anchor.candidate_id, scope_key),
    ]
    assert foreign.candidate_id not in seen[0]
    assert curator.candidate_service.get(anchor.candidate_id).occurrence_count == 2
    assert len(curator.candidate_service.list()) == 2
    assert curator.state_store.load().per_thread_cursors == {message.thread_id: message.message_id}


# LLM: 消息metadata和audit的task_id共用同一项目范围合同；raw、project和旧task别名均须命中规范持久化键。
# 函数用途: 验证两类输入能匹配项目锚点，旧task范围写入归一后也不丢匹配或串入其他项目。
@pytest.mark.parametrize("source", ["message", "audit"])
@pytest.mark.parametrize("task_id", ["alpha", "project:alpha", "task:alpha"])
@pytest.mark.parametrize("candidate_scope_key", ["project:alpha", "task:alpha"])
def test_project_anchors_share_canonical_task_scope(tmp_path, source, task_id, candidate_scope_key):
    candidates = CandidateService(tmp_path / "candidates.jsonl")
    anchor = candidates.observe(CandidateObservation(
        candidate_type="lesson", content="先核项目范围再引用证据", subject_key="project.proof",
        scope=MemoryScope("project", candidate_scope_key), origin="model_inferred", promotion_target="lesson",
    ))
    foreign = candidates.observe(CandidateObservation(
        candidate_type="lesson", content="其他项目的经验", subject_key="project.foreign",
        scope=MemoryScope("project", "project:other"), origin="model_inferred", promotion_target="lesson",
    ))
    base = _batch(1, 1)
    if source == "message":
        batch = replace(base, messages=(replace(base.messages[0], metadata={"task_id": task_id}),), audit_events=())
    else:
        batch = replace(base, messages=(), audit_events=(replace(base.audit_events[0], task_id=task_id),))

    attached = attach_candidate_anchors(batch, candidates)

    assert anchor.scope["scope_key"] == "project:alpha"
    assert [(row["candidate_id"], row["scope"]["scope_key"]) for row in attached.candidate_anchors] == [
        (anchor.candidate_id, "project:alpha"),
    ]
    assert foreign.candidate_id not in {row["candidate_id"] for row in attached.candidate_anchors}
    assert batch.candidate_anchors == ()


# LLM: 会话scope的canonical化须幂等；只有当前结构化thread_id可引入对应锚点，不能凭正文或其他会话扩域。
# 函数用途: 验证裸和已带session前缀的宿主线程身份都命中同一锚点，同时排除其他会话。
@pytest.mark.parametrize("thread_id", ["current", "session:current"])
def test_session_anchor_keys_are_canonical_and_scope_isolated(tmp_path, thread_id):
    candidates = CandidateService(tmp_path / "candidates.jsonl")
    anchor = candidates.observe(CandidateObservation(
        candidate_type="lesson", content="当前会话经验", subject_key="session.current",
        scope=MemoryScope("session", "session:current"), origin="model_inferred", promotion_target="lesson",
    ))
    foreign = candidates.observe(CandidateObservation(
        candidate_type="lesson", content="其他会话经验", subject_key="session.foreign",
        scope=MemoryScope("session", "session:other"), origin="model_inferred", promotion_target="lesson",
    ))
    base = _batch(1, 0)
    batch = replace(base, messages=(replace(base.messages[0], thread_id=thread_id),))

    attached = attach_candidate_anchors(batch, candidates)

    assert [row["candidate_id"] for row in attached.candidate_anchors] == [anchor.candidate_id]
    assert foreign.candidate_id not in {row["candidate_id"] for row in attached.candidate_anchors}


# LLM: 输入锚点只提供candidate_id，不能要求模型复制输入不存在的link_candidate_id；输出映射必须写明。
# 函数用途: 锁定Curator提示中的锚点身份到输出链接字段的明确映射。
def test_curator_prompt_maps_anchor_candidate_id_to_output_link_candidate_id():
    prompt = curator_prompt(_batch(1, 0))
    instruction = next(line for line in prompt.splitlines() if line.startswith("candidate_anchors 是"))
    assert "将匹配锚点的 candidate_id 填入输出字段 link_candidate_id" in instruction
    assert "复制其link_candidate_id" not in instruction


# LLM: 候选字段清单必须同strict schema的完整字段集一致，尤其不能漏可空link_candidate_id。
# 函数用途: 对照权威schema验证提示词候选输出字段无遗漏、重复或额外字段。
def test_curator_prompt_candidate_fields_match_response_schema():
    prompt = curator_prompt(_batch(1, 0))
    declaration = next(line for line in prompt.splitlines() if line.startswith("candidates 每项"))
    fields = declaration.split("字段：", 1)[1].split("。", 1)[0].split("、")
    properties = curator_response_schema()["properties"]["candidates"]["items"]["properties"]
    assert set(fields) == set(properties)
    assert len(fields) == len(properties)


# LLM: 锚点是可省略提示，不是必须处理的新输入；低预算不能因锚点永久卡游标。
# 函数用途: 验证模型最终输入预算先缩只读锚点且保留至少一条真实消息。
def test_candidate_anchors_cannot_exhaust_curator_input_budget():
    base = _batch(1, 0)
    budget = len(curator_prompt(base)) + 100
    anchored = replace(base, candidate_anchors=tuple({"candidate_id": f"id-{i}", "content": "x" * 500} for i in range(12)))
    fitted, _warnings = fit_batch_to_input_budget(anchored, max_chars=budget)
    assert len(curator_prompt(fitted)) <= budget
    assert fitted.messages == base.messages
