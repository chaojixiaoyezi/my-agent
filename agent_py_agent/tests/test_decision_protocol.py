"""原生决策协议冻结、部分失败和未知用量，不访问真实供应商。"""
import copy
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_py_agent.agent.backends.decision_protocol import (
    DecisionBinding,
    DecisionInputError,
    DecisionRequest,
    decision_json,
)
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.typesafe_decision_wire import (
    parse_typesafe_response,
    typesafe_payload,
)


# LLM: 来源是测试宿主引用，不能由模型返回覆盖；fixture 不写文件或调用模型。
# 函数用途: 创建含稳定操作/策略/候选版本的测试绑定。
def binding(**overrides):
    return DecisionBinding(**{"point": "child_model", "owner_ref": "owner-a", "operation_id": "op-1",
                              "policy_revision": "p-1", "candidates_revision": "c-1",
                              "source_refs": ("message:1",), **overrides})


# LLM: 三种问题共用一次请求，候选 ID 和特殊业务选项均为宿主显式声明，不按文本推断。
# 函数用途: 返回新的原生题目数据，测试可独立修改，不共享可变 fixture。
def questions():
    return {
        "model": {"type": "choice", "instructions": {"question": "选择可用模型"},
                  "criteria": {"model-a": {"tools": ["read"]}, "need_data": "缺少所需输入容量"}},
        "priority": {"type": "score", "instructions": "整理优先级", "criteria": ["普通", "紧急"]},
        "recall": {"type": "noul", "instructions": "是否与历史有关", "criteria": {"false": "新问题"}},
    }


# LLM: 响应仅为公开 wire 格式的自有测试样本，不代表真实模型质量或供应商性能。
# 函数用途: 生成含三类答案和完整用量的可变响应。
def response():
    return {"model": "jev-resolved-test", "answers": {
        "model": {"type": "choice", "choice": "model-a", "confidence": 0.8,
                  "probabilities": {"model-a": 0.9, "need_data": 0.1}},
        "priority": {"type": "score", "score": 0.75, "confidence": 0.6,
                     "probabilities": {"0": 0.25, "1": 0.75}, "legend": {"0": "普通", "1": "紧急"}},
        "recall": {"type": "noul", "noul": 0.3}}, "usage": {"input_tokens": 120, "output_tokens": 30}}


def test_input_snapshot_and_digest_include_binding_without_uploading_it():
    state, qs = {"messages": ["材料"]}, questions()
    request = DecisionRequest(binding(), state, qs)
    original = request.payload("jev-test")
    state["messages"].append("后来变更")
    qs["model"]["criteria"].clear()
    assert request.payload("jev-test") == original
    assert set(original) == {"state", "questions", "model"}
    changed = DecisionRequest(replace(binding(), candidates_revision="c-2"), original["state"], original["questions"])
    assert changed.input_digest != request.input_digest
    original["questions"].clear()
    assert request.payload("jev-test")["questions"]
    with pytest.raises(FrozenInstanceError):
        request.input_digest = "replacement"


def test_three_primitives_and_usage_stay_separate_from_execution():
    request = DecisionRequest(binding(), ["样本"], questions())
    raw = response()
    result = parse_typesafe_response(request, "jev-test", raw)
    assert result.binding is request.binding and result.input_digest == request.input_digest
    assert result.model == "jev-resolved-test" and result.requested_model == "jev-test"
    assert [answer.value for answer in result.answers] == ["model-a", 0.75, 0.3]
    assert all(not answer.error_code for answer in result.answers)
    assert result.usage == {"input_tokens": 120, "output_tokens": 30}
    raw["usage"]["input_tokens"] = 999
    result.usage["input_tokens"] = 888
    assert result.usage["input_tokens"] == 120 and result.usage_reported


def test_missing_usage_is_unknown_not_zero():
    raw = response()
    del raw["usage"]
    result = parse_typesafe_response(DecisionRequest(binding(), "材料", questions()), "jev-test", raw)
    assert result.usage == {} and not result.usage_reported
    raw["usage"] = {"input_tokens": 0}
    result = parse_typesafe_response(DecisionRequest(binding(), "材料", questions()), "jev-test", raw)
    assert result.usage == {"input_tokens": 0} and result.usage_reported


@pytest.mark.parametrize("change", [
    {"choice": "not-an-option"}, {"choice": "need_data"}, {"type": "score"},
    {"confidence": True}, {"confidence": "0.8"}, {"confidence": 1.1},
    {"probabilities": {"model-a": 0.2, "need_data": 0.2}},
    {"probabilities": {"model-a": 0.9, "new-model": 0.1}},
])
def test_invalid_choice_is_local_error_not_other_question_failure(change):
    raw = response()
    raw["answers"]["model"].update(change)
    result = parse_typesafe_response(DecisionRequest(binding(), "材料", questions()), "jev-test", raw)
    assert result.answers[0].error_code == "invalid_answer"
    assert result.answers[0].value is None
    assert not result.answers[1].error_code and not result.answers[2].error_code
    assert result.usage["input_tokens"] == 120


def test_need_data_choice_remains_exact_candidate_not_runtime_failure():
    raw = response()
    raw["answers"]["model"].update(choice="need_data", probabilities={"model-a": 0.1, "need_data": 0.9})
    del raw["answers"]["recall"]
    result = parse_typesafe_response(DecisionRequest(binding(), "材料", questions()), "jev-test", raw)
    assert result.answers[0].value == "need_data" and not result.answers[0].error_code
    assert result.answers[2].error_code == "missing_answer"


@pytest.mark.parametrize("change", [{"score": 1.5}, {"score": 0.3}, {"legend": {"0": "缺少等级"}},
                                    {"probabilities": {"0": True, "1": 0.0}}])
def test_invalid_score_is_not_accepted(change):
    raw = response()
    raw["answers"]["priority"].update(change)
    result = parse_typesafe_response(DecisionRequest(binding(), {}, questions()), "jev-test", raw)
    assert result.answers[1].error_code == "invalid_answer"


@pytest.mark.parametrize("raw", [None, [], {"answers": {}}, {"model": "jev", "answers": {"unknown": {}}},
                                    {"model": "jev", "answers": {}, "usage": {"input_tokens": -1}},
                                    {"model": "jev", "answers": {}, "usage": {"input_tokens": True}},
                                    {"model": "jev", "answers": {}, "usage": None}])
def test_unbound_envelope_or_invalid_usage_is_rejected(raw):
    with pytest.raises(ProviderResponseError):
        parse_typesafe_response(DecisionRequest(binding(), {}, questions()), "jev-test", raw)


@pytest.mark.parametrize("question", [
    {"type": "unknown", "instructions": "测试"},
    {"type": "choice", "instructions": "测试", "criteria": {}},
    {"type": "choice", "instructions": "测试", "criteria": {str(i): None for i in range(256)}},
    {"type": "choice", "instructions": "测试", "criteria": {"a": 1}},
    {"type": "score", "instructions": "测试", "criteria": ["唯一等级"]},
    {"type": "score", "instructions": "测试", "criteria": ["等级"] * 11},
    {"type": "noul", "instructions": "测试", "criteria": {"maybe": "未知"}},
    {"type": "noul", "instructions": "测试", "extra": "未支持"},
])
def test_wire_input_rejected_before_network(question):
    request = DecisionRequest(binding(), {}, {"q": question})
    with pytest.raises(DecisionInputError):
        typesafe_payload(request, "jev-test")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "非字符串字段"}, b"bytes", (1, 2), "x" * 262145])
def test_input_rejects_non_json_and_excessive_size(value):
    with pytest.raises(DecisionInputError):
        DecisionRequest(binding(), value, questions())


def test_input_bounds_depth_cycle_nodes_and_aggregate_strings():
    cycle = []
    cycle.append(cycle)
    deep = "leaf"
    for _ in range(20):
        deep = [deep]
    for value in (cycle, deep, [0] * 17000, ["x" * 1000] * 300, [10 ** 1000] * 1024):
        with pytest.raises(DecisionInputError):
            decision_json(value)
    pristine = copy.deepcopy(questions())
    decision_json(pristine)
    assert pristine == questions()


@pytest.mark.parametrize("value", [[10 ** 1000] * 1024, ["x" * 1000] * 300], ids=["integers", "strings"])
def test_aggregate_limit_rejects_before_encoder_allocation(value, monkeypatch):
    from agent_py_agent.agent.backends import decision_protocol

    calls = []
    original = decision_protocol.json.dumps

    def encode(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(decision_protocol.json, "dumps", encode)
    with pytest.raises(DecisionInputError):
        decision_json(value)
    assert calls == []


@pytest.mark.parametrize("value", [True, "0.5", 1.2, -0.1])
def test_bad_noul_is_local_to_question(value):
    raw = response()
    raw["answers"]["recall"]["noul"] = value
    result = parse_typesafe_response(DecisionRequest(binding(), {}, questions()), "jev-test", raw)
    assert result.answers[2].error_code == "invalid_answer"
    assert not result.answers[0].error_code


@pytest.mark.parametrize("constant", [float("nan"), float("inf")])
def test_nonfinite_response_rejected_as_invalid_transport_document(constant):
    raw = response()
    raw["answers"]["recall"]["noul"] = constant
    with pytest.raises(ProviderResponseError):
        parse_typesafe_response(DecisionRequest(binding(), {}, questions()), "jev-test", raw)
