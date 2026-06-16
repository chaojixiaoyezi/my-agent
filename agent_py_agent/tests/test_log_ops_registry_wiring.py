
from __future__ import annotations

"""验证 6 个 log_ops 工具被 ToolRegistry 注册、出现在 specs 与原生 tools schema 里(纯加法不破坏现有)。"""

from pathlib import Path

from agent_py_agent.agent.backends.tool_schema import tool_specs_to_anthropic_tools
from agent_py_agent.agent.tooling.log_ops.tools import LOG_OPS_TOOL_NAMES
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


def _registry(tmp_path: Path) -> ToolRegistry:
    params = ToolRegistryParams(
        workspace_root=tmp_path,
        max_chars=10_000,
        max_entries=200,
        max_matches=200,
        web_max_chars=10_000,
        http_timeout=10,
        catalog_limit=500,
        retrieval_limit=20,
        vector_search_enabled=False,
    )
    return ToolRegistry(params)


def test_all_log_ops_tools_registered(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for name in LOG_OPS_TOOL_NAMES:
        assert name in registry.tools, f"{name} 未注册"
        assert registry.tools[name].spec.category == "log_ops"


def test_log_ops_tools_in_specs_and_native_schema(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    spec_names = {spec.name for spec in registry.specs()}
    for name in LOG_OPS_TOOL_NAMES:
        assert name in spec_names

    tools = tool_specs_to_anthropic_tools(registry.specs())
    native_names = {t["name"] for t in tools}
    for name in LOG_OPS_TOOL_NAMES:
        assert name in native_names


def test_log_ops_tools_workspace_rooted(tmp_path: Path) -> None:
    # 工具的 store 根应落在 workspace 下的 .log_ops(不污染别处)。
    registry = _registry(tmp_path)
    start_tool = registry.tools["log_monitor_start"]
    store = start_tool.store({})
    assert str(store.root).startswith(str(tmp_path.resolve()))
    assert ".log_ops" in str(store.root)


# ---- 根因回归:经完整工具调用链(含 tool_manifest 门)不再 0.00s UNKNOWN_ERROR ----
# 实锤(日志运营 2 小时):log_alert_poll 声明 mutating 却漏 requires_idempotency,被 tool_manifest
# 门在 execute **之前** 判 TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING(此前未注册→兜底 UNKNOWN_ERROR
# /retryable=False)拦死,主代理误以为核心循环被永久阻塞而停手。旧的 log_ops 单测只直接 .execute()
# 绕过了门,所以漏掉。这里用 registry.execute_call 走整条链复现+守护。


def test_alert_poll_through_full_call_chain_not_unknown_error(tmp_path: Path) -> None:
    # 典型主代理文本协议调用:不带 idempotency_key(框架对 mutating 工具自动派生)。
    registry = _registry(tmp_path)
    result = registry.execute_call({"tool": "log_alert_poll", "limit": 20})
    assert result.ok is True, result.output
    assert result.error_code in (None, "")
    assert result.error_code != "UNKNOWN_ERROR"


def test_all_mutating_tools_pass_manifest_gate_no_unknown_error(tmp_path: Path) -> None:
    # 通用守护:任何 mutating/dangerous 工具都不能因漏声明 requires_idempotency 被 tool_manifest
    # 门 0.00s 拦成 UNKNOWN_ERROR。直接断言:不存在「side-effecting 却 requires_idempotency=False」
    # 的工具(那种工具每次被调用都会 0.00s UNKNOWN_ERROR,塌掉依赖它的任务循环)。
    from agent_py_agent.agent.contracts.gates.tool_manifest import (
        evaluate_tool_manifest_gate,
        tool_manifest_from_spec,
    )

    registry = _registry(tmp_path)
    offenders = []
    for name, tool in registry.tools.items():
        spec = getattr(tool, "spec", None)
        if spec is None:
            continue
        decision = evaluate_tool_manifest_gate(tool_manifest_from_spec(spec))
        if not decision.allowed:
            offenders.append((name, spec.effect, spec.requires_idempotency, decision.finding_codes))
    assert offenders == [], f"这些工具会被 tool_manifest 门 0.00s 拦成 UNKNOWN_ERROR: {offenders}"


def test_manifest_idempotency_gate_code_is_registered_and_retryable() -> None:
    # 防御纵深:即便将来又有工具 spec 配错被门拦,这个码也必须是已注册的精确码(非 UNKNOWN_ERROR)
    # 且 retryable=True(让模型换工具/换参数继续,而非 report_blocker 放弃整轮长任务)。
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    for code in (
        "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING",
        "TOOL_EFFECT_MISSING",
        "TOOL_IDEMPOTENCY_KEY_MISSING",
        "RUNTIME_GATE_DENIED",
    ):
        contract = error_contract(code)
        assert contract.code == code, f"{code} 未注册,回落成 {contract.code}"
        assert contract.retryable is True, f"{code} 应 retryable=True"
