"""恢复分类注册表与自然语言边界的钉子测试。

钉死三件事：
1. 错误码恢复处置只来自单一策略表，未知码 fail-closed（不可自动修复/恢复）。
2. finding 显式声明的结构化恢复字段优先于推导；非协议枚举的声明被忽略，不做别名兼容。
3. 自然语言错误正文分类（classify_error）只服务展示与自检，协作 raw_* 审计字段只写不读，
   两者都不得进入任务状态、验收、重试或恢复路由。
"""

from __future__ import annotations

import ast
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


def test_missing_authority_and_uncertain_side_effects_never_auto_retry():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    expected = {
        "ACTIVE_TURN_OUTCOME_UNCERTAIN": RecoveryAction.MANUAL_REVIEW.value,
        "PARENT_CREATION_SNAPSHOT_MISSING": RecoveryAction.REPORT_BLOCKER.value,
        "PARENT_TOOL_SNAPSHOT_UNAVAILABLE": RecoveryAction.REPORT_BLOCKER.value,
    }
    for code, action in expected.items():
        contract = error_contract(code)
        assert contract.code == code
        assert contract.retryable is False
        assert contract.recommended_action == action


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


def test_tool_execution_gate_finding_codes_are_registered():
    """强制工具执行管线及其子门产出的 finding code 都必须注册。

    根因实锤(日志运营 2 小时):这些码由 GateFinding("XXX") / deny("gate","XXX") 产出,
    不是 error_code="XXX" 字面量——上面那条钉子(只扫 error_code= 字面量)抓不到它们。但工具
    被门拦时,runtime_gate_block_result 会把 decision.finding_codes[0] 当成 error_code,未注册
    照样 fallback 成 UNKNOWN_ERROR(retryable=False)误导主代理放弃整轮长任务(log_alert_poll
    被 tool_manifest 门拦成 TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING→UNKNOWN_ERROR 即此)。
    这条钉子把"门产出的 finding code 必须注册"也守住,补上扫描盲区。

    扫描范围跟随唯一 ActionPolicy 和 Provider adapter 的强制门及其直接子门，包括
    Schema、path/command/owner scope、guardrail、rate limit、effect 和 approval binding。
    不能再用只列 manifest/effect 两个文件的窄名单，否则真实门已正确拒绝，错误原因仍会在
    ToolHandlerOutcome 中静默降级成 UNKNOWN_ERROR。
    """
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    gate_files = [
        AGENT_ROOT / "tooling" / "action_policy.py",
            AGENT_ROOT / "contracts" / "gates" / "gate_pipeline.py",
            AGENT_ROOT / "contracts" / "gates" / "adapters.py",
            AGENT_ROOT / "contracts" / "gates" / "path_url_command.py",
        AGENT_ROOT / "contracts" / "gates" / "command_policy.py",
        AGENT_ROOT / "path_access_policy.py",
        AGENT_ROOT / "contracts" / "gates" / "tool_guardrail.py",
        AGENT_ROOT / "contracts" / "gates" / "tool_rate_limit.py",
            AGENT_ROOT / "contracts" / "gates" / "tool_approval_binding.py",
    ]
    used: set[str] = set()
    for path in gate_files:
        used.update(_literal_gate_codes(path))

    adapter_source = (
        AGENT_ROOT / "backends" / "tool_protocol_adapter.py"
    ).read_text(encoding="utf-8")
    used.update(re.findall(r'''["']([A-Z][A-Z0-9_]+)["']''', adapter_source))

    missing = sorted(
        code for code in used if code != "UNKNOWN_ERROR" and error_contract(code).code != code
    )
    assert not missing, (
        "这些工具执行门 finding code 未在 ERROR_CONTRACTS 注册，工具被门拦时会 fallback 成 "
        f"UNKNOWN_ERROR(retryable=False)误导模型放弃: {missing}"
    )


_ERROR_CODE_LITERAL = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def _literal_gate_codes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    codes: set[str] = set()
    for node in ast.walk(tree):
        codes.update(_gate_node_codes(node))
    return codes


def _gate_node_codes(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Call):
        index = _gate_call_code_arg_index(node)
        return _uppercase_code_literals(node.args[index]) if index is not None else set()
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return set()
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = [target.id for target in targets if isinstance(target, ast.Name)]
    return (
        _uppercase_code_literals(node.value)
        if any(name == "code" or name.endswith("_ERROR_CODES") for name in names)
        else set()
    )


def _gate_call_code_arg_index(node: ast.Call) -> int | None:
    name = _call_name(node)
    if name in {"GateFinding", "CommandPolicyFinding", "pipeline_config_decision"} and node.args:
        return 0
    if name in {"PathAccessDecision", "deny", "block", "need_approval"} and len(node.args) >= 2:
        return 1
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _uppercase_code_literals(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant)
        and isinstance(item.value, str)
        and _ERROR_CODE_LITERAL.fullmatch(item.value)
    }


def test_all_tool_effects_are_valid():
    """Literal EffectResolverPolicy defaults must use the canonical vocabulary."""

    valid_effects = {"read_only", "mutating", "dangerous"}
    invalid: dict[str, list[str]] = {}
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "EffectResolverPolicy":
                continue
            keywords = {item.arg: item.value for item in node.keywords if item.arg}
            candidate = node.args[0] if node.args else keywords.get("default_effect")
            value = _literal_string(candidate)
            if value and value not in valid_effects:
                invalid.setdefault(value, []).append(f"{path.name}:{node.lineno}")
    assert not invalid, (
        f"这些 effect 值不合法(只允许 {sorted(valid_effects)}): {invalid}"
    )


def test_all_literal_side_effect_runtime_policies_declare_idempotency_scope():
    """Static guard: every literal side-effect policy enters the operation ledger."""

    side_effects = {"mutating", "dangerous"}
    valid_scopes = {"operation", "business"}

    missing: list[str] = []
    invalid: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "ToolRuntimePolicy":
                continue
            keywords = {
                item.arg: item.value
                for item in node.keywords
                if item.arg is not None
            }
            resolver = keywords.get("effect_resolver")
            if not isinstance(resolver, ast.Call) or _call_name(resolver) != "EffectResolverPolicy":
                continue
            resolver_keywords = {
                item.arg: item.value for item in resolver.keywords if item.arg
            }
            effect_node = (
                resolver.args[0]
                if resolver.args
                else resolver_keywords.get("default_effect")
            )
            effect = _literal_string(effect_node)
            if effect not in side_effects:
                continue
            idempotency = keywords.get("idempotency_policy")
            scope = ""
            if isinstance(idempotency, ast.Call) and _call_name(idempotency) == "IdempotencyPolicy":
                idempotency_keywords = {
                    item.arg: item.value for item in idempotency.keywords if item.arg
                }
                scope_node = (
                    idempotency.args[0]
                    if idempotency.args
                    else idempotency_keywords.get("scope")
                )
                scope = _literal_string(scope_node)
            location = f"{path.name}:{node.lineno}"
            if not scope:
                missing.append(location)
            elif scope not in valid_scopes:
                invalid.append(f"{location}={scope}")
    assert not missing, f"副作用 ToolRuntimePolicy 未声明 idempotency scope: {missing}"
    assert not invalid, f"副作用 ToolRuntimePolicy 的 idempotency scope 非法: {invalid}"


def _literal_string(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""
