"""恢复分类注册表与自然语言边界的钉子测试。

钉死三件事：
1. 错误码恢复处置只来自单一策略表，未知码 fail-closed（不可自动修复/恢复）。
2. finding 显式声明的结构化恢复字段优先于推导；非协议枚举的声明被忽略，不做别名兼容。
3. 自然语言错误正文分类（classify_error）只服务展示与自检，协作 raw_* 审计字段只写不读，
   两者都不得进入任务状态、验收、重试或恢复路由。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_py_agent.agent.contracts.recovery import (
    RecoveryAction,
    RecoveryEnvelopeRequest,
    action_status,
    code_policy,
    hard_stop_code,
    recovering_code,
    recovery_category,
    recovery_envelope_from_gate_payload,
    repairable_code,
)
from agent_py_agent.agent.contracts.state_machine import (
    RunStateFacts,
    can_closeout,
    can_dispatch,
    can_repair,
    recovery_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent_py_agent" / "agent"


def test_unknown_code_fails_closed_to_blocked():
    """未知错误码不能被推导成可修复/可恢复，必须落到 blocked + REPORT_BLOCKER。"""
    code = "ZZZ_TOTALLY_UNKNOWN_FAMILY"
    assert not repairable_code(code)
    assert not recovering_code(code)
    assert not hard_stop_code(code)
    assert recovery_category(code) == "contract"
    assert action_status("NEED_REPAIR", code) == "blocked"
    envelope = recovery_envelope_from_gate_payload(
        RecoveryEnvelopeRequest(
            gate="test_gate",
            allowed=False,
            status="NEED_REPAIR",
            findings=[{"code": code, "severity": "P1", "message": ""}],
        )
    )
    assert envelope is not None
    assert envelope.status == "blocked"
    assert not envelope.can_auto_repair
    assert envelope.actions[0]["recommended_action"] == RecoveryAction.REPORT_BLOCKER.value


def test_exact_code_policy_wins_over_family_prefix():
    """精确码策略优先于家族前缀（同前缀下行为可分化）。"""
    # TARGET_COVERAGE_MISSING 精确码 -> continue；同家族其它码 -> 家族策略
    assert code_policy("TARGET_COVERAGE_MISSING").repair_action == RecoveryAction.CONTINUE.value
    assert code_policy("TARGET_COVERAGE_OTHER").repair_action == ""
    # STATE_CHECKSUM_MISMATCH 是硬停；STATE_TRANSITION_* 可修复；裸 STATE_* 既非修复也非恢复
    assert hard_stop_code("STATE_CHECKSUM_MISMATCH")
    assert repairable_code("STATE_TRANSITION_INVALID")
    assert not repairable_code("STATE_SOMETHING_ELSE")


def test_declared_recovery_action_overrides_derivation():
    """finding 显式声明的 recommended_action（当前协议枚举值）优先于注册表推导。"""
    envelope = recovery_envelope_from_gate_payload(
        RecoveryEnvelopeRequest(
            gate="test_gate",
            allowed=False,
            status="NEED_REPAIR",
            findings=[{
                "code": "ARTIFACT_MISSING",
                "severity": "P1",
                "message": "",
                "recommended_action": RecoveryAction.RERUN_ACCEPTANCE_AFTER_REPAIR.value,
                "category": "state",
            }],
        )
    )
    assert envelope is not None
    action = envelope.actions[0]
    assert action["recommended_action"] == RecoveryAction.RERUN_ACCEPTANCE_AFTER_REPAIR.value
    assert action["category"] == "state"


def test_declared_action_outside_protocol_enum_is_ignored():
    """声明值不在当前协议枚举内时按未声明处理（不做大小写/别名兼容）。"""
    envelope = recovery_envelope_from_gate_payload(
        RecoveryEnvelopeRequest(
            gate="test_gate",
            allowed=False,
            status="NEED_REPAIR",
            findings=[{
                "code": "ARTIFACT_MISSING",
                "severity": "P1",
                "message": "",
                "recommended_action": "Repair_Artifact_Against_Findings",  # 大小写变体不是协议值
            }],
        )
    )
    assert envelope is not None
    assert (
        envelope.actions[0]["recommended_action"]
        == RecoveryAction.REPAIR_ARTIFACT_AGAINST_FINDINGS.value
    )


def test_state_predicates_fail_closed_on_unknown_status():
    """未知状态值不是'可修复的 BLOCKED'：三个谓词全 False，恢复决策走 MANUAL_REVIEW。"""
    facts = RunStateFacts(status="totally_unknown_status", attempts=0, max_attempts=3)
    assert not can_dispatch(facts)
    assert not can_repair(facts)
    assert not can_closeout(facts)
    decision = recovery_decision(facts)
    assert decision.action == RecoveryAction.MANUAL_REVIEW.value
    assert not decision.allow_new_run


def test_known_status_predicates_unchanged():
    """协议内状态行为不受 fail-closed 收紧影响。"""
    assert can_dispatch(RunStateFacts(status="PENDING"))
    assert can_repair(RunStateFacts(status="BLOCKED", attempts=0, max_attempts=3))
    assert can_closeout(RunStateFacts(status="DONE", verification_status="VERIFIED"))


def _production_files() -> list[Path]:
    return [
        p for p in AGENT_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def test_classify_error_only_used_by_taxonomy_self_checks():
    """错误正文分类只能服务展示/自检；不得被状态、恢复、派工、验收模块消费。"""
    allowed = {
        AGENT_ROOT / "contracts" / "error_taxonomy.py",
        AGENT_ROOT / "contracts" / "main_agent_foundation_runner.py",
        AGENT_ROOT / "contracts" / "e2e_matrix_runner.py",
        AGENT_ROOT / "contracts" / "main_agent.py",
    }
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in _production_files()
        if p not in allowed and re.search(r"\bclassify_error\b", p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"classify_error 出现在状态相关模块: {offenders}"


def test_raw_collaboration_status_metadata_is_write_only():
    """raw_case_status / raw_request_status 是审计字段：生产代码只允许写入与清理，禁止读取参与决策。"""
    allowed_writers = {
        AGENT_ROOT / "collaboration" / "request_status.py",  # 写入定义
        AGENT_ROOT / "collaboration" / "store.py",           # 状态显式迁移时清理
    }
    pattern = re.compile(r"""\.get\(\s*['"](raw_case_status|raw_request_status)['"]""")
    offenders = []
    for p in _production_files():
        text = p.read_text(encoding="utf-8")
        if p not in allowed_writers and ("raw_case_status" in text or "raw_request_status" in text):
            offenders.append(str(p.relative_to(REPO_ROOT)))
        elif pattern.search(text):
            offenders.append(str(p.relative_to(REPO_ROOT)) + " (reads raw_*)")
    assert offenders == [], f"raw 状态审计字段被消费: {offenders}"


def test_all_used_error_codes_are_registered():
    """所有 error_code="XXX" 字面量必须在 ERROR_CONTRACTS 注册。

    未注册的码经 error_contract() 会 fallback 成 UNKNOWN_ERROR
    (category=unknown / retryable=False / recommended_action=report_blocker)，
    把"模型自己可改正的工具调用格式错误"误导成"放弃并报阻塞"，而不是"修正格式重试"——
    这是 草草完成 / 幻觉归因 的底座诱因之一(deepseek-v4-pro 实测在并行工具调用里暴露:
    TOOL_CALL_MARKER_MALFORMED 此前未注册 → 显示 UNKNOWN_ERROR)。

    这条钉子守住"用了就必须注册"，杜绝再次回归。
    """
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    pattern = re.compile(r"""error_code\s*=\s*["']([A-Z_]+)["']""")
    used: set[str] = set()
    for path in _production_files():
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            used.add(match.group(1))
    # error_contract(code).code == code 当且仅当精确命中注册表(未命中则返回 UNKNOWN_ERROR)
    missing = sorted(
        code for code in used if code != "UNKNOWN_ERROR" and error_contract(code).code != code
    )
    assert not missing, (
        "这些 error_code 在生产代码中使用但未在 ERROR_CONTRACTS 注册，"
        f"会 fallback 成 UNKNOWN_ERROR(误导模型放弃而非修正重试): {missing}"
    )


def test_all_tool_effects_are_valid():
    """所有工具 spec 的 effect 必须是合法值(read_only/mutating/dangerous)。

    无效 effect(如曾经的 effect="write")会被 tool_manifest gate 判 TOOL_MANIFEST_EFFECT_INVALID
    → DENY，工具【从未能执行】(create_skill/remember 曾因此对强模型完全不可用,直到 effect
    改 mutating + requires_idempotency=True 才修通)。现有工具单测直接调 execute 绕过了 gate，
    抓不到这类 spec 错误——这条钉子用静态扫描守住"effect 写对值"。
    """
    from agent_py_agent.agent.contracts.gates.tool_manifest import VALID_TOOL_EFFECTS

    pattern = re.compile(r"""effect\s*=\s*["']([a-z_]+)["']""")
    invalid: dict[str, list[str]] = {}
    for path in _production_files():
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            value = match.group(1)
            if value not in VALID_TOOL_EFFECTS:
                invalid.setdefault(value, []).append(path.name)
    assert not invalid, (
        f"这些 effect 值不合法(只允许 {sorted(VALID_TOOL_EFFECTS)}),"
        f"会被 tool_manifest gate 拦死导致工具不可用: {invalid}"
    )
