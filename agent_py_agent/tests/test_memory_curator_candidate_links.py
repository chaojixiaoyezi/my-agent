# LLM: 真实Curator租约、解析、事务及候选服务，只用离线脚本后端，不发模型网络调用。
# 模块用途: 验证当前范围锚点和结构化链接贯穿生产策展入口。
from __future__ import annotations

import json
from dataclasses import replace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.tests.test_curator_observation_identity import (
    _candidate,
    _conversation,
    _output,
    _service,
)


# LLM: 后端只提出已给锚点的ID；每次声明相同消息已处理，验证真实游标和账本收敛。
# 函数用途: 在一次真实Curator运行中消费同范围锚点并链接重复经验。
def test_curator_injects_same_scope_anchors_and_merges_linked_observation(tmp_path):
    store, _thread, message = _conversation(tmp_path)
    seen = []

    class Backend:
        name = "anchor-fixture"

        def generate_structured(self, prompt, *, response_schema):
            seen.append(prompt)
            assert "link_candidate_id" in response_schema["properties"]["candidates"]["items"]["properties"]
            item = _candidate(message.message_id, "核实证据后再下结论（新表述）", "proof.first")
            item.update(candidate_type="lesson", promotion_target="lesson", origin="model_inferred",
                link_candidate_id=anchor.candidate_id)
            return ModelResponse(text=json.dumps(_output(message, [item])), backend=self.name)

    curator = _service(tmp_path, Backend(), store)
    anchor = curator.candidate_service.observe(CandidateObservation(candidate_type="lesson", content="先核实证据",
        subject_key="proof.first", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson", source_message_refs=({"message_id": "prior", "thread_id": "prior-thread"},)))
    foreign = curator.candidate_service.observe(CandidateObservation(candidate_type="lesson", content="其他项目私有经验",
        subject_key="foreign", scope=MemoryScope("project", "project:unrelated"), origin="model_inferred",
        promotion_target="lesson"))
    result = curator.run(reason="admin")
    assert result.status == "succeeded", result
    assert "candidate_anchors" in seen[0] and anchor.candidate_id in seen[0]
    assert foreign.candidate_id not in seen[0]
    assert curator.candidate_service.get(anchor.candidate_id).occurrence_count == 2
    assert len(curator.candidate_service.list()) == 2


# LLM: 锚点是可省略提示，不是必须处理的新输入；低预算不能因锚点永久卡游标。
# 函数用途: 验证模型最终输入预算先缩只读锚点且保留至少一条真实消息。
def test_candidate_anchors_cannot_exhaust_curator_input_budget():
    from agent_py_agent.agent.memory_store.curator_backend import (
        curator_prompt,
        fit_batch_to_input_budget,
    )
    from agent_py_agent.tests.test_curator_input_budget import _batch

    base = _batch(1, 0)
    budget = len(curator_prompt(base)) + 100
    anchored = replace(base, candidate_anchors=tuple({"candidate_id": f"id-{i}", "content": "x" * 500} for i in range(12)))
    fitted, _warnings = fit_batch_to_input_budget(anchored, max_chars=budget)
    assert len(curator_prompt(fitted)) <= budget
    assert fitted.messages == base.messages
