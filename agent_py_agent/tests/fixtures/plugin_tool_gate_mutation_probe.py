"""B5 可复现变异，仅改当前解释器内存；pytest 原退出码保留，不写产品文件。"""
from __future__ import annotations

import argparse
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent))

from agent_py_agent.agent.agent_core.tool_loop import round_execution  # noqa: E402
from agent_py_agent.agent.conversation import (
    agent_tool_approval,  # noqa: E402
    tool_approval_scope,  # noqa: E402
)
from agent_py_agent.agent.gateway_parts import permission_bridge, stream_approval  # noqa: E402
from agent_py_agent.agent.plugin_events import (  # noqa: E402
    decision_ledger,
    tool_gate,
    tool_gate_review,
)
from agent_py_agent.agent.tooling import plugin_gate_policy  # noqa: E402
from agent_py_agent.agent.user_space import approval_mode  # noqa: E402

MUTATIONS = {
    "ask-as-allow": ("merge", 'status = primary.reply.verdict if primary else host_status',
                     'status = "allow" if primary and primary.reply.verdict == "ask" else (primary.reply.verdict if primary else host_status)',
                     "seven_design_combinations"),
    "not-most-restrictive": ("_review_order", '-_PLUGIN_GATE_VERDICT_RANK[review.reply.verdict]',
                            '_PLUGIN_GATE_VERDICT_RANK[review.reply.verdict]', "most_restrictive"),
    "response-order-primary": ("_review_order", 'review.target.plugin_id, review.target.declaration.id',
                               '"", ""', "most_restrictive or stable_plugin"),
    "no-redaction": ("request_payload", 'redact_sensitive_value(_without_internal_keys(call.call.arguments))',
                     '_without_internal_keys(call.call.arguments)', "full_projection_removes or redaction_precedes"),
    "internal-keys-leak": ("request_payload", '_without_internal_keys(call.call.arguments)',
                          'call.call.arguments', "full_projection_removes"),
}


# LLM: 仅修改当前解释器内存并保持pytest原退出码，定位必须唯一，不能把导入故障当业务反证。
# 函数用途: 为纯逻辑用例安装一个可复现变异，不写产品文件。
def apply_mutation(name):
    symbol, old, new, selector = MUTATIONS[name]
    owner = tool_gate.PluginToolGate if hasattr(tool_gate.PluginToolGate, symbol) else tool_gate
    original = getattr(owner, symbol)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, f"变异定位必须唯一：{name}"
    namespace = {}
    exec(compile(source.replace(old, new), f"<mutation:{name}>", "exec"), tool_gate.__dict__, namespace)
    setattr(owner, symbol, namespace[symbol])
    return selector


EXECUTION_MUTATIONS = {
    "parameter-origin": (plugin_gate_policy, "tighten_plugin_decision",
        'request.call_origin == "host_command"',
        '(request.call_origin == "host_command" or call.arguments.get("call_origin") == "host_command" or call.arguments.get("__call_origin") == "host_command")',
        "forged or origin", "test_plugin_tool_gate_execution.py"),
    "timeout-as-allow": (tool_gate, "_failure_reply", 'GateReply("ask", code)',
        'GateReply("allow_as_is", code)', "timeout or budget", "test_plugin_tool_gate_execution.py"),
    "response-arguments": (tool_gate_review.PluginGateReviewer, "_exchange",
        'return replace(PluginToolGate.decode_reply(job.target, result), latency_ms=_latency(job.started))',
        'if isinstance(result, dict) and isinstance(result.get("arguments"), dict):\n    job.call.call.arguments.clear()\n    job.call.call.arguments.update(result["arguments"])\nreturn replace(PluginToolGate.decode_reply(job.target, result), latency_ms=_latency(job.started))',
        "extra_response_arguments", "test_plugin_tool_gate_execution.py"),
    "closed-pool-is-revoked": (tool_gate_review, "_current_review", 'if review.outcome == "revoked":',
        'if review.outcome == "unused":', "closed_channel", "test_plugin_tool_gate_execution.py"),
    "non-ok-as-permission": (tool_gate.GateReview, "__post_init__", 'if self.outcome != "ok":',
        'if False and self.outcome != "ok":', "non_ok_construction", "test_plugin_tool_gate_logic.py"),
    "direct-message-unwashed": (tool_gate.GateReply, "__post_init__", 'clean_gate_message(self.message)',
        'self.message', "direct_reply", "test_plugin_tool_gate_logic.py"),
    "gateway-wait-auto-bypass": (permission_bridge, "wait_for_gateway_permission_decision",
        'forced = plugin_gate_required(request)',
        'forced = False', "gateway_wait_plugin_gate", "test_plugin_gate_approval.py"),
    "agent-wait-auto-bypass": (agent_tool_approval, "wait_for_agent_tool_approval",
        'forced = plugin_gate_required(handle.request)',
        'forced = False', "agent_wait_plugin_gate", "test_plugin_gate_approval.py"),
    "gateway-request-bypass": (stream_approval.StreamApproval, "request", 'forced = plugin_gate_required(request)',
        'forced = False', "gateway_plugin_request", "test_plugin_gate_approval.py"),
    "agent-request-bypass": (agent_tool_approval.AgentToolApprovalSinkMixin, "request_permission",
        'forced = plugin_gate_required(request)', 'forced = False',
        "agent_plugin_request", "test_plugin_gate_approval.py"),
    "autonomous-provider-bypass": (approval_mode, "autonomous_tool_decision", 'if plugin_gate_required(request):',
        'if False and plugin_gate_required(request):', "autonomous_provider", "test_plugin_gate_approval.py"),
    "tool-name-gate-guess": (stream_approval.StreamApproval, "request", 'forced = plugin_gate_required(request)',
        'forced = request.tool_name == "run_command"', "gateway_plugin_request", "test_plugin_gate_approval.py"),
    "truncated-as-complete": (tool_gate.PluginToolGate, "request_payload",
        'len(_json_text(sanitized)) > MAX_PLUGIN_GATE_ARGUMENT_CHARS', 'False',
        "actual_truncation", "test_plugin_tool_gate_execution.py"),
    "executor-reference-missing": (plugin_gate_policy, "plugin_gate_approval_request", 'if not reference:',
        'if True:', "reference_reaches", "test_plugin_tool_gate_execution.py"),
    "child-attempt-unbound": (agent_tool_approval, "tool_approval_scope",
        'execution_attempt_id=str(task.runner_active_attempt_id or "")', 'execution_attempt_id=""',
        "child_old_plugin_approval", "test_plugin_gate_consumers.py"),
    "canonical-child-gate-bypass": (plugin_gate_policy, "tighten_plugin_decision",
        'if host.status == "deny" or request.call_origin == "host_command" or request.plugin_gate_reviewer is None:',
        'if request.runtime_snapshot.owner_type == "subagent" or host.status == "deny" or request.call_origin == "host_command" or request.plugin_gate_reviewer is None:',
        "canonical_consumers_keep and subagent", "test_plugin_gate_consumers.py"),
    "reapproval-call-only": (tool_gate, "_approved_call_matches",
        'for key in ("operation_id", "call_id", "idempotency_key", "args_hash")', 'for key in ("call_id",)',
        "each_of_six", "test_plugin_gate_reapproval.py"),
    "reapproval-target-ignored": (tool_gate, "_approved_target_matches",
        'return all(isinstance(item.get(key), str) and bool(item[key]) and item[key] == value',
        'return all(isinstance(item.get(key), str) and bool(item[key])',
        "six_reference or current_installation or canonical_new_identity or only_approved_gate", "test_plugin_gate_reapproval.py"),
    "reapproval-status-ignored": (plugin_gate_policy, "_approved_gate_reference",
        'if not isinstance(action, dict) or action.get("status") != "APPROVED":', 'if not isinstance(action, dict):',
        "unapproved_malformed", "test_plugin_gate_reapproval.py"),
    "reapproval-version-stale": (tool_gate_review, "_identity",
        'target.plugin_id, target.version, target.activation_id, target.declaration.id',
        'target.plugin_id, target.activation_id, target.declaration.id',
        "fresh_installation_change", "test_plugin_gate_reapproval.py"),
    "rejection-unexplained": (round_execution, "plugin_gate_rejection_message",
        'return _confirmation_prefix(execution.decision) + message if execution.decision.evidence.get("plugin_gate_ref") else message',
        'return message', "canonical_user_rejection", "test_plugin_gate_reapproval.py"),
    "reapproval-false-applied": (round_execution, "_with_applied_approval_fact",
        'not execution.decision.allowed or ', '', "change_while_waiting", "test_plugin_gate_reapproval.py"),
    "ledger-latency-missing": (decision_ledger, "plugin_gate_decision_payload",
        'payload[key] = max(0, int(value or 0)) if isinstance(value, (int, float)) else 0',
        'continue', "single_gate_writes_exactly_design_field_set", "test_plugin_tool_gate_decision_ledger.py"),
    "ledger-first-gate-only": (plugin_gate_policy, "_decision_ledger_entries",
        'for item in reviews if not item.approval_applied',
        'for item in reviews[:1] if not item.approval_applied',
        "multiple_gates_write_one_entry_each", "test_plugin_tool_gate_decision_ledger.py"),
    "ledger-applied-recorded": (plugin_gate_policy, "_decision_ledger_entries",
        'for item in reviews if not item.approval_applied', 'for item in reviews',
        "approval_applied_gate_writes_no_ledger_entry", "test_plugin_tool_gate_decision_ledger.py"),
    "resolve-skips-record-check": (agent_tool_approval, "resolve_agent_tool_approval",
        'if report.load_error is not None or not _record_matches(',
        'if report.load_error is not None or False and _record_matches(',
        "resolve_rechecks_claim_or_attempt", "test_plugin_gate_consumers.py"),
    "wait-scope-valid-off": (agent_tool_approval, "wait_for_agent_tool_approval",
        'if not handle.scope_valid():', 'if False and not handle.scope_valid():',
        "invalid_scope_wait", "test_plugin_gate_consumers.py"),
    "reapproval-plugin-id-ignored": (tool_gate, "_approved_target_matches",
        '"plugin_id": target.plugin_id, "version": target.version,', '"version": target.version,',
        "same_activation_only_changed_plugin_id", "test_plugin_gate_reapproval.py"),
    "ledger-only-ask-evidence": (plugin_gate_policy, "tighten_plugin_decision",
        'if reviews:', 'if merged.requirements:',
        "decision_visible_in_real_plugins_info", "test_plugin_gate_event_combination.py"),
    "ledger-wrong-archive-level": (decision_ledger, "plugin_gate_decisions_from_archive",
        'value = envelope.get(PLUGIN_GATE_DECISION_EVIDENCE_KEY)',
        'value = archive_record.get(PLUGIN_GATE_DECISION_EVIDENCE_KEY)',
        "decision_visible_in_real_plugins_info", "test_plugin_gate_event_combination.py"),
    "ledger-noninteractive-deny-misclassified": (plugin_gate_policy, "tighten_plugin_decision",
        'if merged.status == "ask" and merged.requirements', 'if merged.requirements',
        "noninteractive_mixed_deny", "test_plugin_tool_gate_decision_ledger.py"),
}


# LLM: 多行变异保持原缩进，仅替换真实函数内存；独立pytest回执必须业务失败且errors为零。
# 函数用途: 安装执行/审批链变异并返回原测试选择器。
def apply_execution_mutation(name):
    owner, symbol, old, new, selector, _filename = EXECUTION_MUTATIONS[name]
    original = getattr(owner, symbol)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, f"变异定位必须唯一：{name}"
    # 多行替换保持命中行的原缩进；只改内存，不改产品源文件。
    indent = next(line[:len(line) - len(line.lstrip())] for line in source.splitlines() if old in line)
    replacement = new.replace("\n", "\n" + indent)
    module = inspect.getmodule(original)
    namespace = {}
    exec(compile(source.replace(old, replacement), f"<mutation:{name}>", "exec"), module.__dict__, namespace)
    setattr(owner, symbol, namespace[symbol])
    return selector


# LLM: 每个进程只装一个变异，pytest显式列文件；输出和XML由原pytest生成，不包装失败为成功。
# 函数用途: 运行可复现的单项变异并透传退出码，不修改产品源码。
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mutation", choices=(*MUTATIONS, *EXECUTION_MUTATIONS))
    parser.add_argument("--junitxml", default="")
    parser.add_argument("--basetemp", default="")
    options = parser.parse_args()
    execution = options.mutation in EXECUTION_MUTATIONS
    selector = apply_execution_mutation(options.mutation) if execution else apply_mutation(options.mutation)
    filename = EXECUTION_MUTATIONS[options.mutation][-1] if execution else "test_plugin_tool_gate_logic.py"
    args = [str(ROOT / "tests" / filename), "-q", "--tb=short", "-p", "no:cacheprovider",
            "-o", "addopts=", "-k", selector]
    if options.basetemp:
        args.extend(("--basetemp", options.basetemp))
    if options.junitxml:
        args.extend(("--junitxml", options.junitxml))
    return pytest.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
