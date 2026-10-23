# LLM: RS-07/RS-14 离线回归锁定历史 SHA、显式白名单及调用方同一实现，不读取真实凭据。
# 模块用途: 防止 helper 抽取改变上下文字节、放开证据字段或留下重复实现。
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_context import (
    decision_action_candidate,
    decision_delivery_quality,
    external_material_order,
)
from agent_py_agent.agent.backends.decision_protocol import DecisionInputError
from agent_py_agent.agent.subagents import (
    result_artifact_evidence,
    result_structured_evidence,
)
from agent_py_agent.agent.subagents.models import (
    EvidencePacket,
    SubAgentParsedOutput,
    SubAgentTask,
)

_CONTEXT_MODULES = (
    external_material_order,
    decision_action_candidate,
    decision_delivery_quality,
)
_SERIALIZERS = (
    result_structured_evidence._evidence_packet_dict,
    result_artifact_evidence._packet_payload,
)
_FULL_SHA = "7efb152c4e42d55bab3bc8da86eedd7f9774adaecced5c121aa5abe9ab5751eb"
_DEFAULT_SHA = "440309a4a738727f5f7e87ff3fde50df325902d13dd601aeab2d3874b8015055"
_PACKET_FIELDS = (
    "id",
    "claim",
    "checked_scope",
    "evidence_refs",
    "artifact_refs",
    "counter_evidence_refs",
    "confidence",
    "unresolved_risks",
    "created_at",
)


# LLM: 样本对应抽取前实际运行的 _FULL_SHA；Unicode、换行及重复工具名用于捕获编码或去重变化。
# 函数用途: 构造决策上下文样本，无文件或状态副作用。
def _params():
    return SimpleNamespace(
        request_id="req-中文",
        run_id="run-1",
        task_id="task-1",
        user_prompt="比较 A\nB 😀",
        context_scope="thread",
        task_attributes={"z": [3, None, True], "a": {"owner": "local"}},
        allowed_tools=["web_fetch", "read_file"],
        write_boundary={"roots": ["/tmp/文档"], "readonly": False},
        tool_runtime_snapshot=SimpleNamespace(
            snapshot_hash="b" * 64,
            available_tool_names=("z_tool", "a_tool", "z_tool"),
        ),
    )


# LLM: 独立编码历史投影，不调用产品 helper/decision_json，避免共同错误掩盖兼容问题。
# 函数用途: 返回样本应有的精确 UTF-8 字节，用于固定 SHA 与编码输入比对。
def _expected_context_bytes():
    return json.dumps(
        {
            "request_id": "req-中文",
            "run_id": "run-1",
            "task_id": "task-1",
            "user_prompt": "比较 A\nB 😀",
            "context_scope": "thread",
            "task_attributes": {"z": [3, None, True], "a": {"owner": "local"}},
            "allowed_tools": ["web_fetch", "read_file"],
            "write_boundary": {"roots": ["/tmp/文档"], "readonly": False},
            "snapshot_hash": "b" * 64,
            "available_tools": ["a_tool", "z_tool", "z_tool"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# LLM: Compare an independently fixed historical SHA without mutating input.
# 函数用途: 锁定原上下文编码的精确摘要。
@pytest.mark.parametrize(
    "module",
    _CONTEXT_MODULES,
    ids=lambda module: module.__name__.rsplit(".", 1)[-1],
)
def test_context_exact_historical_sha_and_input_unchanged(module):
    params = _params()
    before = deepcopy(params)

    assert hashlib.sha256(_expected_context_bytes()).hexdigest() == _FULL_SHA
    assert module._context_revision(params) == _FULL_SHA
    assert params == before


# LLM: Missing attributes and absent snapshots keep their historical defaults.
# 函数用途: 验证空上下文的精确回退摘要。
@pytest.mark.parametrize("module", _CONTEXT_MODULES)
@pytest.mark.parametrize(
    "params",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(tool_runtime_snapshot=None),
        SimpleNamespace(tool_runtime_snapshot=SimpleNamespace()),
    ],
)
def test_context_missing_attribute_fallback_is_exact(module, params):
    assert module._context_revision(params) == _DEFAULT_SHA


# LLM: Sort available tools only; duplicates and allowed order remain significant.
# 函数用途: 防止抽取时新增去重或无关排序。
@pytest.mark.parametrize("module", _CONTEXT_MODULES)
def test_context_tool_names_sorted_but_duplicates_and_allowed_order_preserved(module):
    params = _params()
    params.tool_runtime_snapshot.available_tool_names = (
        "z_tool",
        "z_tool",
        "a_tool",
    )
    assert module._context_revision(params) == _FULL_SHA

    params.tool_runtime_snapshot.available_tool_names = ("z_tool", "a_tool")
    assert module._context_revision(params) != _FULL_SHA

    params = _params()
    params.allowed_tools.reverse()
    assert module._context_revision(params) != _FULL_SHA


# LLM: Every original task/permission field participates in the local version.
# 函数用途: 验证每个上下文字段均能改变指纹。
@pytest.mark.parametrize("module", _CONTEXT_MODULES)
@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "run_id",
        "task_id",
        "user_prompt",
        "context_scope",
        "task_attributes",
        "allowed_tools",
        "write_boundary",
    ],
)
def test_context_every_bound_field_changes_sha(module, field):
    params = _params()
    setattr(params, field, "changed")

    assert module._context_revision(params) != _FULL_SHA


# LLM: Both snapshot bindings must invalidate stale decisions.
# 函数用途: 验证快照哈希及工具集合参与指纹。
@pytest.mark.parametrize("module", _CONTEXT_MODULES)
@pytest.mark.parametrize("field", ["snapshot_hash", "available_tool_names"])
def test_context_snapshot_fields_change_sha(module, field):
    params = _params()
    value = "changed" if field == "snapshot_hash" else ("other_tool",)
    setattr(params.tool_runtime_snapshot, field, value)

    assert module._context_revision(params) != _FULL_SHA


# LLM: Invalid material still raises the original bounded encoder error.
# 函数用途: 锁定非JSON、非有限数和超长材料的拒绝行为。
@pytest.mark.parametrize("module", _CONTEXT_MODULES)
@pytest.mark.parametrize(
    "invalid",
    [object(), float("nan"), "x" * 300_000],
    ids=["non_json_object", "non_finite_number", "oversized"],
)
def test_context_invalid_material_keeps_decision_input_error(module, invalid):
    params = _params()
    params.task_attributes = {"invalid": invalid}

    with pytest.raises(DecisionInputError):
        module._context_revision(params)


# LLM: Explicit malformed tool names must not be silently normalized.
# 函数用途: 保留工具集合显式为None时的原异常。
@pytest.mark.parametrize("module", _CONTEXT_MODULES)
def test_context_explicit_none_tool_names_is_not_silently_normalized(module):
    params = _params()
    params.tool_runtime_snapshot.available_tool_names = None

    with pytest.raises(TypeError):
        module._context_revision(params)


# LLM: Identity checks detect wrappers and leftover duplicate implementations.
# 函数用途: 保证三个旧入口绑定同一个共享函数。
def test_context_three_private_names_bind_the_single_shared_implementation():
    assert (
        external_material_order._context_revision
        is decision_action_candidate._context_revision
    )
    assert (
        external_material_order._context_revision
        is decision_delivery_quality._context_revision
    )

    shared = import_module(
        "agent_py_agent.agent.agent_core.tool_context.context_fingerprint"
    )
    assert external_material_order._context_revision is shared.context_fingerprint


# LLM: Observe real encoder bytes rather than imitating the implementation.
# 函数用途: 核对共享编码入口产生的原UTF8字节。
def test_context_shared_encoder_receives_exact_legacy_bytes(monkeypatch):
    shared = import_module(
        "agent_py_agent.agent.agent_core.tool_context.context_fingerprint"
    )
    encode = shared.decision_json
    seen = []

    # LLM: Delegate to the real encoder and collect its immutable output.
    # 函数用途: 捕获真实编码字节，不替换编码行为。
    def capture(value):
        encoded = encode(value)
        seen.append(encoded)
        return encoded

    monkeypatch.setattr(shared, "decision_json", capture)

    for module in _CONTEXT_MODULES:
        assert module._context_revision(_params()) == _FULL_SHA

    assert seen == [_expected_context_bytes()] * 3


# LLM: 模拟未来 dataclass 增加敏感字段；asdict/vars 全量展开必须被白名单断言发现。
# 类用途: 提供带未知秘密字段的证据包样本，仅用合成凭据。
@dataclass
class _ExtendedPacket(EvidencePacket):
    secret_token: str = "synthetic-token-do-not-export"
    credentials: str = "synthetic-credentials-do-not-export"


# LLM: Unknown fields must not escape; preserve accepted fields and list identity.
# 函数用途: 核对白名单、值与引用语义。
@pytest.mark.parametrize("serialize", _SERIALIZERS)
def test_evidence_packet_whitelist_values_and_reference_identity(serialize):
    packet = _ExtendedPacket(
        id="evpkt-1",
        claim="已验证的结论",
        checked_scope="unit",
        evidence_refs=["evidence://1"],
        artifact_refs=["artifact://1"],
        counter_evidence_refs=["evidence://counter"],
        confidence=0.75,
        unresolved_risks=["尚需外部复核"],
        created_at=123.5,
    )
    packet.unrecognized = {"raw_secret": "synthetic-unknown-do-not-export"}
    before = deepcopy(packet.__dict__)

    payload = serialize(packet)

    assert tuple(payload) == _PACKET_FIELDS
    assert payload == {
        "id": "evpkt-1",
        "claim": "已验证的结论",
        "checked_scope": "unit",
        "evidence_refs": ["evidence://1"],
        "artifact_refs": ["artifact://1"],
        "counter_evidence_refs": ["evidence://counter"],
        "confidence": 0.75,
        "unresolved_risks": ["尚需外部复核"],
        "created_at": 123.5,
    }
    for field in (
        "evidence_refs",
        "artifact_refs",
        "counter_evidence_refs",
        "unresolved_risks",
    ):
        assert payload[field] is getattr(packet, field)

    assert packet.__dict__ == before
    assert serialize(packet) is not payload

    encoded = json.dumps(payload, ensure_ascii=False)
    for secret in (
        packet.secret_token,
        packet.credentials,
        packet.unrecognized["raw_secret"],
    ):
        assert secret not in encoded


# LLM: Default fields remain explicit in the existing payload contract.
# 函数用途: 验证默认字段不会在去重时消失。
@pytest.mark.parametrize("serialize", _SERIALIZERS)
def test_evidence_packet_defaults_are_not_omitted(serialize):
    assert serialize(EvidencePacket()) == {
        "id": "",
        "claim": "",
        "checked_scope": "",
        "evidence_refs": [],
        "artifact_refs": [],
        "counter_evidence_refs": [],
        "confidence": 0.0,
        "unresolved_risks": [],
        "created_at": 0.0,
    }


# LLM: Both evidence producers must directly reuse the explicit whitelist.
# 函数用途: 保证两个旧序列化入口绑定同一个共享函数。
def test_evidence_packet_two_private_names_bind_the_single_shared_implementation():
    assert (
        result_structured_evidence._evidence_packet_dict
        is result_artifact_evidence._packet_payload
    )

    shared = import_module(
        "agent_py_agent.agent.subagents.evidence_packet_serializer"
    )
    assert (
        result_structured_evidence._evidence_packet_dict
        is shared.serialize_evidence_packet
    )


# LLM: Exercise both real producer paths with synthetic data only.
# 函数用途: 回归结构化证据接收与真实产物兜底链路。
def test_evidence_processing_and_artifact_fallback_keep_whitelisted_output(tmp_path):
    task = SubAgentTask(id="run-1", goal="检查产物", thought="", plan=[])
    item = {
        "id": "evpkt-explicit",
        "claim": "完成检查",
        "evidence_refs": ["evidence://1"],
        "confidence": "invalid",
        "secret_token": "synthetic-parsed-secret",
    }
    parsed = SubAgentParsedOutput(found=True, ok=True, evidence_packets=[item])

    explicit = result_structured_evidence.process_evidence_packets(
        parsed, task, 123.5
    )

    assert len(explicit) == 1
    assert tuple(explicit[0]) == _PACKET_FIELDS
    assert explicit[0]["confidence"] == 0.0
    assert explicit[0]["created_at"] == 123.5
    assert task.evidence_refs == ["evidence://1"]
    assert "synthetic-parsed-secret" not in json.dumps(explicit)

    product = tmp_path / "产物.txt"
    product.write_text("已有产物", encoding="utf-8")
    artifacts = [
        {
            "path": str(product),
            "summary": "测试产物",
            "secret_token": "synthetic-artifact-secret",
        }
    ]
    fallback = result_artifact_evidence.merge_artifact_evidence(
        task, artifacts, [], 124.5
    )

    assert len(fallback) == 1
    assert tuple(fallback[0]) == _PACKET_FIELDS
    assert fallback[0]["claim"] == "artifact produced: 测试产物"
    assert fallback[0]["checked_scope"] == "runner_artifacts"
    assert fallback[0]["artifact_refs"] == [str(product)]
    assert fallback[0]["confidence"] == 0.5
    assert fallback[0]["created_at"] == 124.5
    assert task.artifact_refs == [str(product)]
    assert fallback[0]["artifact_refs"] is task.evidence_packets[-1].artifact_refs
    assert "synthetic-artifact-secret" not in json.dumps(fallback)

    count = len(task.evidence_packets)
    assert (
        result_artifact_evidence.merge_artifact_evidence(
            task, artifacts, explicit, 125.5
        )
        is explicit
    )
    assert len(task.evidence_packets) == count
