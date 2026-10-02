"""发给 Jev 的能力候选投影：白名单字段、按转义字节从前往后截、截断计数，以及 jev_wire_bytes.v1 标定下的最坏情况。"""
import copy
import json

import pytest

from agent_py_agent.agent.backends.decision_protocol import DecisionBinding, DecisionRequest
from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body
from agent_py_agent.agent.backends.typesafe_decision_wire import (
    jev_empirical_input_bound,
    typesafe_payload,
)
from agent_py_agent.agent.capability import decision_recommendation as module
from agent_py_agent.agent.capability.decision_candidates import (
    SELECTION_INSTRUCTIONS,
    selection_questions,
)
from agent_py_agent.agent.contracts.model_call_budget import ModelCallBudgetError
from agent_py_agent.tests.test_decision_capability_consumer import provider
from agent_py_agent.tests.test_decision_capability_consumer import surface as surface  # noqa: F401
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)

_CAPS = {"name": 96, "description": 180, "when_to_use": 144}


def _wire_bytes(text: str) -> int:
    return len(json.dumps(text)) - 2


def _skill(index: int, **fields) -> dict:
    return {"kind": "skill", "ref": f"workspace:skill-{index}", "name": f"skill-{index}", "description": "",
            "when_to_use": "", "version": "v" * 64, "tools_required": [], **fields}


def _candidates(questions: dict) -> list[dict]:
    return [question["instructions"]["candidate"] for question in questions.values()]


def test_wire_candidate_keeps_only_whitelisted_fields_and_leaves_host_rows_intact():
    rows = [
        {"kind": "tool", "ref": "web_fetch", "name": "web_fetch", "description": "抓网页", "category": "web", "version": "t" * 64},
        _skill(1, description="写周报", when_to_use="周五", tools_required=["read_file"]),
        {"kind": "capability_package", "ref": "capability:story", "name": "story", "description": "分镜", "when_to_use": "故事",
         "keywords": ["分镜"], "version": "p" * 64, "tools_required": []},
        {"kind": "provider", "ref": "plugin:peek", "name": "plugin:peek", "version": "g" * 64, "tool_refs": ["peek_show"],
         "tools": [{"name": "peek_show", "description": "看目录", "version": "s" * 64}]},
    ]
    before = copy.deepcopy(rows)
    questions, _ = selection_questions(rows)
    assert rows == before, "完整行由宿主保留，投影不能改写它"
    assert _candidates(questions) == [
        {"kind": "tool", "name": "web_fetch", "description": "抓网页"},
        {"kind": "skill", "name": "skill-1", "description": "写周报", "when_to_use": "周五", "tools_required": ["read_file"]},
        {"kind": "capability_package", "name": "story", "description": "分镜", "when_to_use": "故事", "tools_required": []},
        {"kind": "provider", "name": "plugin:peek", "tools": [{"name": "peek_show", "description": "看目录"}]},
    ]
    assert all(set(q["instructions"]) <= {"candidate", "provider"} for q in questions.values()), "共用说明不再逐题重复"
    assert "provider" in questions["candidate_3"]["instructions"] and "provider" not in questions["candidate_1"]["instructions"]
    loose, projection = selection_questions([_skill(0, when_to_use=None)])
    assert _candidates(loose)[0]["when_to_use"] is None and projection["truncated"]["when_to_use"]["fields"] == 0, "非字符串原样通过"


@pytest.mark.parametrize("field", sorted(_CAPS))
def test_text_is_cut_to_the_longest_prefix_within_escaped_byte_cap(field):
    cap = _CAPS[field]
    exact = "汉" * (cap // 6) + "a" * (cap % 6)
    over = exact + "b"
    mixed = "a" * (cap - 5) + "汉字"  # 汉字转义 6 字节：前缀放不下第一个汉字就停在 ASCII 段
    emoji = "😀" * (cap // 12 + 1)  # 补充平面字符转义成代理对，12 字节
    rows = [_skill(i, **{field: text}) for i, text in enumerate((exact, over, mixed, emoji))]
    questions, projection = selection_questions(rows)
    kept = [candidate[field] for candidate in _candidates(questions)]
    assert kept[0] == exact and _wire_bytes(exact) == cap, "恰好等于上限的文字原样保留"
    assert kept[1] == exact
    assert kept[2] == "a" * (cap - 5)
    assert kept[3] == "😀" * (cap // 12)
    assert all(text.startswith(cut) and _wire_bytes(cut) <= cap for text, cut in zip((exact, over, mixed, emoji), kept))
    removed = sum(_wire_bytes(text) - _wire_bytes(cut) for text, cut in zip((exact, over, mixed, emoji), kept))
    assert projection["truncated"][field] == {"fields": 3, "bytes": removed}
    others = {key: value for key, value in projection["truncated"].items() if key != field}
    assert all(counts == {"fields": 0, "bytes": 0} for counts in others.values())
    assert projection["schema"] == "jev_candidate_projection.v1" and projection["caps_bytes"] == _CAPS


def test_provider_member_tools_are_capped_and_counted_like_any_other_field():
    long = "插件工具说明" * 10
    row = {"kind": "provider", "ref": "plugin:p", "name": "plugin:p", "version": "g" * 64, "tool_refs": ["p_a", "p_b"],
           "tools": [{"name": "p_a", "description": long, "version": "a"}, {"name": "p_b", "description": "短", "version": "b"}]}
    questions, projection = selection_questions([row])
    tools = _candidates(questions)[0]["tools"]
    assert tools == [{"name": "p_a", "description": long[:30]}, {"name": "p_b", "description": "短"}]
    assert projection["truncated"]["description"] == {"fields": 1, "bytes": _wire_bytes(long) - 180}


# 标定依据：53 题、state 撑到 4096 字节上限、每题三段文字按上限填满汉字、ref 很长也不上线时，经验上界仍不超过 57,600。
def test_worst_case_53_questions_fit_jev_wire_bytes_v1_and_54_are_still_refused():
    text = {field: "满" * (cap // 6) for field, cap in _CAPS.items()}
    rows = [_skill(i, ref="workspace:" + "r" * 200, **text) for i in range(53)]
    questions, projection = selection_questions(rows)
    assert projection["truncated"] == {field: {"fields": 0, "bytes": 0} for field in _CAPS}
    state = {"query": "", "instructions": SELECTION_INSTRUCTIONS}
    state["query"] = "长" * ((4096 - len(gateway_request_body(state))) // 6)
    assert 4096 - 6 < len(gateway_request_body(state)) <= 4096
    binding = DecisionBinding("skill_tool", "owner", "operation", "policy", "candidate")
    payload = typesafe_payload(DecisionRequest(binding, state, questions), "jev-1.13.0")
    bound = jev_empirical_input_bound(gateway_request_body(payload), payload, point="skill_tool")
    assert bound.questions == 53 and bound.tokens <= 57_600, bound
    more, _ = selection_questions(rows + [_skill(53, **text)])
    bigger = typesafe_payload(DecisionRequest(binding, state, more), "jev-1.13.0")
    with pytest.raises(ModelCallBudgetError, match="input_bound_out_of_calibration"):
        jev_empirical_input_bound(gateway_request_body(bigger), bigger, point="skill_tool")


def test_real_request_sends_shared_instructions_once_and_keeps_host_only_keys_local(surface, monkeypatch):  # noqa: F811
    calls = provider(monkeypatch)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.finding.endswith("applied"), result.finding
    payload = calls[0].payload("jev")
    assert payload["state"]["instructions"] == SELECTION_INSTRUCTIONS
    assert not {"candidates", "candidate_projection"} & set(payload["state"])
    assert all("ref" not in question["instructions"]["candidate"] for question in payload["questions"].values())
    body = json.dumps(payload, ensure_ascii=False)
    assert body.count(SELECTION_INSTRUCTIONS["question"]) == body.count(SELECTION_INSTRUCTIONS["boundary"]) == 1
    projection = result.observation["candidate_projection"]
    assert projection["schema"] == "jev_candidate_projection.v1"
    texts = [(field, value) for question in payload["questions"].values()
             for item in [question["instructions"]["candidate"], *question["instructions"]["candidate"].get("tools", [])]
             for field, value in item.items() if field in _CAPS]
    assert texts and all(_wire_bytes(value) <= _CAPS[field] for field, value in texts), "上线的每段文字都在字段上限内"
    assert all(type(n) is int and n >= 0 for counts in projection["truncated"].values() for n in counts.values())
