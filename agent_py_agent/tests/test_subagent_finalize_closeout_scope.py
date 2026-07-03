from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

# 防回归(收尾一公里·编队 6/6 收尾崩实锤): 子代理 runner 的最终 finalize 曾把
# context_scope 硬编码回 "default",聚合门按主代理规则认领【根任务 id】→ 编队兄弟
# 全被当成"自己未完成的孩子"→ SUBAGENTS_UNFINISHED 打回;打回响应还会【整体替换】
# final_response,把模型真实输出的 [SUBAGENT_RESULT] 清掉 → 假 structured_output_parse_error。
# 三把钉子:①finalize 参数透传真实 scope;②task_local 已交结果块则收尾轮不重跑
# closeout 替换;③子代理/内部轮 ok 收口不得退休主任务(同 task_id)的循环提醒。

from agent_py_agent.agent.agent_core._finalization_service import (
    FinalizationService,
    _subagent_result_block_present,
    _tool_loop_params_from_finalize_context,
)
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
    evaluate_subagent_aggregation_gate,
)
from agent_py_agent.agent.agent_core.runtime.progress_policy_retirement import (
    retire_task_progress_policies_on_closeout,
)

_RESULT_BLOCK = '[SUBAGENT_RESULT]{"status":"DONE","summary":"done"}[/SUBAGENT_RESULT]'


def _finalize_ctx(*, context_scope: str, response_text: str) -> FinalizeContext:
    return FinalizeContext(
        user_prompt="goal",
        final_prompt="prompt",
        final_response=SimpleNamespace(text=response_text, backend="test"),
        memories=[],
        executed_tools=[],
        archive_tool_calls=[],
        routed_context=None,
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id="req-x",
        run_id="subagent-run-1",
        task_id="req_root_task",
        source="subagent_runner",
        do_save=False,
        task_attributes=None,
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
        context_scope=context_scope,
    )


def test_finalize_params_preserve_context_scope():
    # ①曾硬编码 "default":task_local 必须原样到达 closeout 参数,聚合门才能按
    # 子代理规则做兄弟隔离。
    params = _tool_loop_params_from_finalize_context(
        _finalize_ctx(context_scope="task_local", response_text="x")
    )
    assert params.context_scope == "task_local"


def test_result_block_present_skips_recloseout_for_task_local():
    # ②task_local + 已有结果块 → 收尾轮不重跑 closeout(整个 ctx 原样返回,
    # agent=None 都不该被碰——早退才是正确行为)。
    ctx = _finalize_ctx(context_scope="task_local", response_text=_RESULT_BLOCK)
    assert _subagent_result_block_present(ctx) is True
    service = FinalizationService(None)
    assert service._with_final_delivery_closeout_if_ready(ctx) is ctx


def test_result_block_broken_json_still_owned_by_runner_chain():
    # 坏块(found=True, ok=False)也归 runner 修复链:拿模型真实输出尾部去修,
    # 而不是被 rework 响应清掉后拿噪声去修。
    ctx = _finalize_ctx(context_scope="task_local", response_text='[SUBAGENT_RESULT]{"broken')
    assert _subagent_result_block_present(ctx) is True


def test_main_agent_response_with_block_text_not_skipped():
    # 主代理轮(default)不受影响:即使响应文本里出现同款块,也照常走收口门。
    ctx = _finalize_ctx(context_scope="default", response_text=_RESULT_BLOCK)
    assert _subagent_result_block_present(ctx) is False


def test_task_local_without_block_does_not_skip():
    # task_local 但没交结果块 → 不早退,仍可走(正确 scope 的)closeout 兜底。
    ctx = _finalize_ctx(context_scope="task_local", response_text="没有结果块的自由文本")
    assert _subagent_result_block_present(ctx) is False


# ---- 聚合门 × 真实目录命名(补既有钉子盲区:task_root 目录名=根任务 id) ----
# 既有 test_fleet_sibling_not_counted_as_own_child 的 task_root 是随机 tmp 名,
# 兄弟 parent_id 恰好对不上目录名,掩盖了 scope 丢失 bug。生产中目录名就是根
# 请求 id,default scope 会认领它 → 必须用 task_local 才能隔离兄弟。

_ROOT_ID = "req_root_task"


def _realistic_fleet_root(tmp_path: Path) -> Path:
    task_root = tmp_path / _ROOT_ID
    agents = task_root / "work" / "agents"
    for run_id, status in (("subagent-A", "DONE"), ("subagent-B", "RUNNING")):
        d = agents / run_id
        d.mkdir(parents=True, exist_ok=True)
        payload = {"id": run_id, "run_id": run_id, "status": status, "parent_id": _ROOT_ID}
        (d / "canonical_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return task_root


def _closeout(task_root: Path, run_id: str, context_scope: str):
    return SimpleNamespace(
        params=SimpleNamespace(
            run_id=run_id,
            context_scope=context_scope,
            task_attributes={"run_workspace": {"task_root": str(task_root)}},
        ),
        agent=None,
    )


def test_task_local_scope_isolates_siblings_under_realistic_root_name(tmp_path):
    # 子代理收口(task_local):兄弟 RUNNING 且 parent_id=根 id=目录名 → 仍放行。
    root = _realistic_fleet_root(tmp_path)
    decision = evaluate_subagent_aggregation_gate(_closeout(root, "subagent-A", "task_local"))
    assert decision.allowed, decision.to_dict()


def test_default_scope_under_realistic_root_name_claims_fleet(tmp_path):
    # 主代理收口(default):同样目录下必须照旧拦住未终态编队(语义不回退)。
    root = _realistic_fleet_root(tmp_path)
    decision = evaluate_subagent_aggregation_gate(_closeout(root, "bg-main-thread-1", "default"))
    assert not decision.allowed
    assert any(f.code == "SUBAGENTS_UNFINISHED" for f in decision.findings)


# ---- ③收口退休提醒的 scope 门 ----


class _FakeStore:
    def __init__(self):
        self.disabled: list[str] = []
        self._policies = [SimpleNamespace(policy_id="p1", task_id="req_root_task")]

    def list_progress_policies(self, enabled_only=True):
        return list(self._policies)

    def disable_progress_policy(self, policy_id: str):
        self.disabled.append(policy_id)


def _retire_params(context_scope: str):
    return SimpleNamespace(task_id="req_root_task", context_scope=context_scope)


def test_task_local_ok_closeout_does_not_retire_parent_policies():
    # 编队第一个子代理干净收口(task_id=根任务 id)→ 不得退休主任务的监督提醒。
    store = _FakeStore()
    agent = SimpleNamespace(conversation_store=store)
    retire_task_progress_policies_on_closeout(agent, _retire_params("task_local"), {"ok": True})
    assert store.disabled == []


def test_default_scope_ok_closeout_still_retires():
    # 主代理自己收口 → 照旧退休(原语义不回退)。
    store = _FakeStore()
    agent = SimpleNamespace(conversation_store=store)
    retire_task_progress_policies_on_closeout(agent, _retire_params("default"), {"ok": True})
    assert store.disabled == ["p1"]
