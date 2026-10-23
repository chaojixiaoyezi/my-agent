# LLM: 回归测试必须守住真实 runner 的身份、权限和引用，不能只以字符串变短判定正确。
# 模块用途: 验证 CTX-12 的启动上下文去重、紧凑 JSON 和原模型调用参数合同。
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner import prompts
from agent_py_agent.agent.agent_core.runner.prompt_context_summary import (
    runner_context_summary_payload,
)
from agent_py_agent.agent.agent_core.subagent.run_flow import (
    SubagentModelIteration,
    _subagent_model_run_params,
)
from agent_py_agent.agent.subagents.models import ContextManifest, SubAgentExecutionContext


# LLM: 固定长任务重复出现在 envelope 与父级资料中，权限空列表是显式禁止而非可删默认值。
# 函数用途: 创建可独立复用、无需 provider 的完整子代理上下文样本。
def runner_context(workspace):
    goal = '完整实现中文功能，保留 "引号"、换行\n与精确引用。' * 60
    return SubAgentExecutionContext(
        run_id="child-ctx12", generated_at=1.0, goal=goal, thought="先核对合同", plan=[],
        agent_name="worker-ctx12", role="worker", parent_id="parent-ctx12",
        root_id="root-ctx12", depth=2, task_dir=str(workspace / "state"),
        context_bundle_json=str(workspace / "state/context_bundle.json"),
        context_bundle_file=str(workspace / "state/CONTEXT_BUNDLE.md"),
        allowed_tools=["read_file", "write_file", "run_command"], allowed_skills=[],
        context_manifest=ContextManifest(required_read_paths=["source.md"]),
        context_packs=[{"kind": "parent_task_directive", "summary": goal}],
        write_boundary={
            "execution_cwd": str(workspace), "allowed_write_roots": [str(workspace)],
            "allowed_read_roots": [str(workspace / "source.md")], "locked_files": [],
            "forbidden_write_roots": [], "product_write_roots": [str(workspace)],
            "effective_permissions": {"network_enabled": False, "disabled_tools": []},
        },
        context_bundle={
            "gate": {"ok": True, "missing_fields": [], "blocking_reason": ""},
            "workspace_refs": {"owner_workspace_dir": str(workspace), "task_root": str(workspace / "state")},
            "conversation": {"thread_id": "thread-ctx12", "root_task_id": "root-ctx12"},
            "task_envelope": {
                "schema_version": "subagent_task_envelope.v1", "goal": goal, "role": "worker",
                "address": {"run_id": "child-ctx12", "root_id": "root-ctx12", "parent_id": "parent-ctx12", "depth": 2},
                "tool_contract": {"allowed_tools": ["read_file", "write_file", "run_command"]},
                "write_contract": {"forbidden_write_roots": [], "locked_files": []},
                "context_refs": {},
            },
            "tool_preflight": {"ok": True, "issues": [], "effective_tools": ["read_file", "write_file", "run_command"]},
            "collaboration": {"targeted_request_count": 0, "targeted_requests": [], "responder_policy": ""},
        },
    )


@pytest.fixture
def context(tmp_path):
    (tmp_path / "source.md").write_text("输入原件", encoding="utf-8")
    return runner_context(tmp_path)


def test_prepared_json_is_compact_and_round_trips_without_changing_authority(context):
    before = deepcopy(context)
    prepared = prompts.prepare_subagent_runner_prompt(context)
    payload = json.loads(prepared.context_json)
    assert "\n" not in prepared.context_json
    assert prepared.context_json == json.dumps(payload, ensure_ascii=False, separators=(",", ": "))
    assert payload["task"]["goal"] == context.goal
    assert payload["permissions"]["allowed_tools"] == context.allowed_tools
    assert payload["permissions"]["allowed_skills"] == []
    assert payload["write_boundary"] == context.write_boundary
    assert context == before


def test_summary_keeps_required_identity_scope_gate_and_input_refs(context):
    payload = runner_context_summary_payload(context)
    identity = payload["identity"]
    assert identity["run_id"] == "child-ctx12"
    assert identity["parent_id"] == identity["parent_run_id"] == "parent-ctx12"
    assert identity["root_id"] == identity["root_run_id"] == identity["task_id"] == "root-ctx12"
    assert identity["role"] == "worker" and identity["depth"] == 2
    assert payload["workspace_root"] == context.write_boundary["execution_cwd"]
    assert payload["context_gate"] == context.context_bundle["gate"]
    assert payload["input_refs"] == [str(context.write_boundary["allowed_read_roots"][0])]
    assert payload["refs"]["context_bundle_json"] == context.context_bundle_json
    assert payload["conversation"]["root_task_id"] == "root-ctx12"


def test_exact_duplicate_goal_and_parent_text_use_explicit_local_refs(context):
    payload = runner_context_summary_payload(context)
    assert "goal" not in payload["task_envelope"]
    assert payload["task_envelope"]["goal_ref"] == "task.goal"
    assert payload["context_packs"][0]["summary_ref"] == "task.goal"
    assert "summary" not in payload["context_packs"][0]
    assert prompts.prepare_subagent_runner_prompt(context).context_json.count("完整实现中文功能") == 60


def test_different_task_text_is_not_deduplicated_by_prefix(context):
    other_goal = context.goal + "不同任务要求，必须保留"
    context.context_bundle["task_envelope"]["goal"] = other_goal
    parent = "父级说明" * 100
    context.context_packs = [
        {"summary": parent}, {"summary": parent}, {"summary": parent + "另有要求"},
    ]
    payload = runner_context_summary_payload(context)
    assert payload["task_envelope"]["goal"] == other_goal
    packs = payload["context_packs"]
    assert packs[1]["summary_ref"] == "context_packs[0].summary"
    assert "summary_ref" not in packs[2]


def test_only_optional_empty_sections_are_omitted(context):
    payload = runner_context_summary_payload(context)
    assert not {"collaboration", "takeover", "recovery", "pending_requests", "open_gaps", "instructions", "output_contract"} & payload.keys()
    # 空权限和写围栏不能递归裁剪；false/0 也是有效运行事实。
    assert payload["permissions"]["controlled_exec_grant_count"] == 0
    assert payload["write_boundary"]["locked_files"] == []
    assert payload["write_boundary"]["effective_permissions"]["network_enabled"] is False
    assert payload["task_envelope"]["write_contract"]["forbidden_write_roots"] == []


def test_explicit_scope_authority_and_nonfilesystem_input_refs_remain_exact(context):
    context.context_bundle.update({
        "scope": {"context_scope": "task_local", "owner_id": "owner-a", "writes_main_memory": False},
        "authority": {"source": "task-envelope", "denied_tools": []},
        "input_refs": ["artifact://exact/" + "segment/" * 50 + "source"],
    })
    payload = runner_context_summary_payload(context)
    for key in ("scope", "authority", "input_refs"):
        assert payload[key] == context.context_bundle[key]


def test_blocked_gate_and_preflight_failures_survive_slimming(context):
    gate = {"ok": False, "missing_fields": ["goal"], "blocking_reason": "missing_required_context_fields"}
    context.context_bundle["gate"] = gate
    context.context_bundle["tool_preflight"] = {
        "ok": False, "issues": [{"code": "controlled_exec_grant_missing"}], "effective_tools": [],
    }
    prepared = prompts.prepare_subagent_runner_prompt(context)
    payload = json.loads(prepared.context_json)
    assert payload["context_gate"] == gate
    assert payload["tool_preflight"]["ok"] is False
    assert payload["tool_preflight"]["issue_codes"] == ["controlled_exec_grant_missing"]
    assert "Context Gate: BLOCKED" in prepared.context_gate
    assert "不要继续执行业务实现" in prepared.context_gate


def test_exact_long_refs_and_business_contract_are_not_pruned_or_clipped(context):
    long_ref = "/state/" + "segment/" * 50 + "checkpoint.json"
    business_ref = "/product/final_report.md"
    context.context_bundle["runner_recovery_preflight"] = {
        "context_scope": "task_local", "writes_main_memory": False,
        "recovery_refs": [long_ref, "/state/output.json"], "runner_instruction": "按 checkpoint 继续",
    }
    context.context_bundle["output_contract"] = {"required_file_refs": [business_ref]}
    context.context_packs = [{"source_ref": long_ref, "summary": "来源说明"}]
    payload = runner_context_summary_payload(context)
    assert payload["recovery"]["recovery_refs"] == [long_ref]
    assert payload["recovery"]["writes_main_memory"] is False
    assert payload["context_packs"][0]["source_ref"] == long_ref
    assert payload["output_contract"]["required_file_refs"] == [business_ref]
    assert "/state/output.json" not in json.dumps(payload)


def test_nonempty_collaboration_children_and_capability_requests_stay_structured(context):
    context.pending_requests = [{"request_id": "cap-1", "needed_capability": "shell"}]
    context.context_bundle["direct_children"] = {
        "total": 1, "all_terminal": False, "items": [{
            "run_id": "grandchild-1", "status": "BLOCKED", "evidence_refs": ["artifact://exact/evidence"],
            "open_capability_requests": [{"request_id": "cap-child-1", "tools": ["run_command"]}],
        }],
    }
    context.context_bundle["collaboration"] = {"targeted_requests": [{
        "case_ref": "collaboration://case/1", "request_ref": "collaboration://request/1",
        "query_intent": {"goal": "corroborate_or_refute"},
    }]}
    payload = runner_context_summary_payload(context)
    assert payload["pending_requests"] == context.pending_requests
    child = payload["direct_children"]["items"][0]
    assert child["evidence_refs"] == ["artifact://exact/evidence"]
    assert child["open_capability_requests"][0]["tools"] == ["run_command"]
    request = payload["collaboration"]["targeted_requests"][0]
    assert request["request_ref"] == "collaboration://request/1"
    assert request["query_intent"] == {"goal": "corroborate_or_refute"}


def test_system_owns_common_discipline_but_contract_keeps_unique_tool_fallback(context):
    system = prompts.subagent_runner_system_prompt(context)
    prepared = prompts.prepare_subagent_runner_prompt(context)
    for invariant in (
        "不是顶层 root 主代理", "直接父级给你的当前 goal 是本轮完整工作边界",
        "有效测试不得仅为变绿而删除、跳过、放宽断言", "不要编造工具结果",
        "按本层分工由你或受委派下级通过真实工具产出", "先在普通 assistant 回复中直接回答或确认用户",
        "不要使用上下文之外的 skill/tool",
    ):
        assert invariant in system
    # 共用执行/反编造只在 system 定义；工具缺失的可交付退路仍在任务合同里。
    assert "持续执行纪律：在当前回合能继续推进时" not in system
    assert "不要假装你已经执行过命令" not in prepared.execution_contract
    assert "检查失败就继续修复" not in prepared.execution_contract
    assert "不要因为不能自己运行 pytest 就提交 capability_request" in prepared.execution_contract
    assert "应写出产物和测试建议" in prepared.execution_contract
    assert "宿主不会替你执行或裁定一套额外的机器验收" in prepared.execution_contract


def test_original_child_model_params_receive_the_same_slim_prompt_and_authority(context):
    prompt = prompts._build_subagent_runner_prompt(context)
    current = SimpleNamespace(injection="task-local", history_seed=[], compact_context=None)
    params = _subagent_model_run_params(
        context=context, prompt=prompt, attempt_id="attempt-ctx12", task_attributes={},
        iteration=SubagentModelIteration(current, [], [], None, []),
    )
    assert params.root_user_prompt == prompt
    assert params.run_id == context.run_id and params.task_id == context.root_id
    assert params.allowed_tools == context.allowed_tools
    assert params.write_boundary == context.write_boundary
    assert params.context_scope == "task_local"
    assert params.system_prompt_override == prompts.subagent_runner_system_prompt(context)
    payload = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert payload["task"]["goal"] == context.goal
    assert payload["context_gate"]["ok"] is True


@pytest.mark.parametrize("ordinary_fields", [0, 12])
def test_runner_prompt_duplicate_refs_only_target_retained_fields(context, ordinary_fields):
    import re

    shared = "与当前子任务不同的父级说明"
    context.context_packs = [
        {**{f"field_{i}": i for i in range(ordinary_fields)}, "summary": shared},
        {"summary": shared, "handoff_note": shared},
        {"summary": context.goal},
    ]
    before = deepcopy(context.context_packs)
    payload = runner_context_summary_payload(context)
    for pack in payload["context_packs"]:
        for key, ref in pack.items():
            if key not in {"summary_ref", "handoff_note_ref"}:
                continue
            target = payload
            for name, index in re.findall(r"([a-z_]+)|\[(\d+)\]", ref):
                target = target[int(index)] if index else target[name]
            assert target in {shared, context.goal}
    assert context.context_packs == before


def test_runner_prompt_keeps_real_shared_pack_path_and_ref_and_late_nested_refs(context):
    from agent_py_agent.agent.agent_core.orchestration.shared_context import (
        shared_context_packs_from_archive,
    )

    path = "/state/" + "segment/" * 50 + "source.md"
    ref = "read-artifact:" + "segment/" * 50 + "call-read"
    packs = shared_context_packs_from_archive([{
        "tool": "read_file", "ok": True, "parameters": {"path": path},
        "output_preview": "准确来源", "output_size_bytes": 12, "scoped_call_id": ref,
    }])
    assert packs[0]["path"] == path and packs[0]["ref"] == ref
    context.context_packs = packs
    context.context_bundle["takeover"] = {"metadata": {
        **{f"field_{i}": i for i in range(8)}, "path": path, "ref": ref,
    }}
    payload = runner_context_summary_payload(context)
    assert payload["context_packs"][0]["path"] == path
    assert payload["context_packs"][0]["ref"] == ref
    assert payload["takeover"]["metadata"]["path"] == path
    assert payload["takeover"]["metadata"]["ref"] == ref
