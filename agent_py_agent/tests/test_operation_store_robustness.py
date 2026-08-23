"""seq 245 反例测试：ManagedOperationStore 单连接单事务状态机（先红后修）。

对方（seq 245）复核阶段①源码后给出 5 个 P1 + reopen/lease 缺口，要求
「先补反例测试并让它们红，再重做 ManagedOperationStore 的单连接单事务
状态机；这些转绿后才可信，全量 gate 绿不能替代并发/崩溃边界」：

- P1 selector 全局 id 缓存串 owner（模块级 {id(agent): store}，旧 agent 回收
  后 id 可复用 → 新 agent 拿旧 owner 的 store）→ 必须删全局缓存，store 作为
  agent 实例字段一次性构造注入。
- P2 claim 丢调用者 attempt fence（ToolCall.attempt_id 是宿主注入的可信字段，
  但 claim 只按 run 读「当前 attempt」→ takeover 后旧 attempt 再发新工具会被
  绑到新 attempt 执行）→ expected_attempt_id 必须传进 store 同事务 CAS，
  绝不用「数据库当前是谁」替调用者补身份。
- P3 UNKNOWN 路径丢元数据 + 泄漏锁 + 跨连接 SQLITE_BUSY（mark_operation_unknown
  整段覆盖 outcome_json 抹掉 resource_scopes；外层 BEGIN IMMEDIATE 未提交时
  开第二连接写会撞锁）→ UNKNOWN + 锁释放 + mutation→DIRTY + 保留 payload
  必须同一事务。
- P4 成功 settle 非原子闭环（settle 先 commit → release_locks 再 commit →
  逐 scope mark_mutation_stable 再 commit；间隙崩溃 → SUCCEEDED 但锁仍在/
  MUTATING）→ 终态 CAS + mutation 状态 + lock delete 同事务提交。
- P5 真实资源 scope 没被锁住（绝对路径参数 continue；list 参数忽略；shell
  缺省 working_dir 补绝对路径 → 真实写根 0 锁）→ 单一权威 resolver，
  绝对路径 canonicalize 后锁、列表逐项、缺省锁 effective cwd。
- P6 reopen 不重取锁/不置 MUTATING/无 current-attempt CAS + lease renew 未接入。

helper 复用红测文件（tests/test_tool_operation_managed_gate.py）：装配原则
一致（链经 _direct_register_chain 登记、registry 经 _operation_store_for 从
agent 解析、真实 ToolExecutor 主链）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.tool_operations import (
    TOOL_OPERATION_RUNNING,
    TOOL_OPERATION_SUCCEEDED,
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationOwnershipError,
    ToolOperationReopenRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db.execution_mode import ExecutionMode
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ToolOperationAuthorityRequest,
)
from agent_py_agent.agent.runtime_db.operation_store_selector import (
    select_operation_store,
)
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.executor import (
    ToolExecutorRequest,
    _durable_operation_scopes,
    _workspace_operation_scopes,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.registry_workspace import effective_registry_cwd
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_tool_operation_managed_gate import (
    _agent,
    _direct_register_chain,
    _gate_call,
    _managed_chain,
    _mutation_rows,
    _params,
    _PathWriteTool,
    _ReadOnlyTool,
    _repo,
    _store,
    _traced_call,
)

# ============================================================== P1：selector 缓存
# 期望（修后）：select_operation_store(agent) 把 store 作为 agent 实例字段一次性
# 构造注入（composition root），无模块级 {id(agent): store} 缓存（id 复用串 owner）。
# 现状（红测）：模块级 _SELECTION/_SelectionCache 存在，store 不挂 agent。


def test_p1_selector_attaches_store_to_agent(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    store = select_operation_store(agent)
    assert getattr(agent, "_operation_store", None) is store  # 当前：未挂属性 → 红
    assert select_operation_store(agent) is store  # 幂等（同一实例字段）


def test_p1_selector_has_no_module_level_id_cache():
    import agent_py_agent.agent.runtime_db.operation_store_selector as sel

    assert not hasattr(sel, "_SELECTION")  # 当前：模块级缓存存在 → 红
    assert not hasattr(sel, "_SelectionCache")


# ============================================================== P2：调用者 attempt
# 期望（修后）：ToolCall.attempt_id（宿主注入）经请求链透传进 store，claim 同事务
# CAS current_attempt_id；非 current attempt 的调用 → 拦截（handler=0、零残留）。
# 现状（红测）：claim 按 run 的 current pointer 绑定，旧 attempt 的调用被放行执行。


def test_p2_claim_rejected_for_stale_attempt(tmp_path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-p2", task_id="task-p2")
    tool = _PathWriteTool("stale_write")
    agent = _managed_chain(repo, store, [tool], tmp_path)
    params = _params(
        run_id="run-p2",
        task_id="task-p2",
        snapshot=runtime_snapshot_for_tools({"stale_write": tool}, run_id="run-p2"),
    )
    call = _gate_call(
        run_id="run-p2",
        tool_name="stale_write",
        operation_id="tool_call:attempt-stale:call-1",
        snapshot=runtime_snapshot_for_tools({"stale_write": tool}, run_id="run-p2"),
        attempt_id="attempt-stale",  # 伪造旧 attempt：调用者可信身份不是 current pointer
    )

    execution = _traced_call(agent, params, call)

    assert not execution.result.ok  # 当前：放行执行 → 红
    assert tool.calls == 0  # handler 不得执行
    assert repo.operations_for_attempt(attempt_id) == []  # 零残留
    assert repo.operations_for_attempt("attempt-stale") == []


# ============================================================== P3：UNKNOWN 单事务
# 期望（修后）：finish(UNKNOWN) 在 ManagedOperationStore 单连接单事务内联完成
# （行 UNKNOWN + 锁删除 + mutation DIRTY + payload 保留），不调用 repo 独立方法
# （各自开连接，外层事务未提交时第二连接写撞 SQLITE_BUSY）。
# 现状（红测）：依赖 repo.mark_operation_unknown / release_locks / mark_mutation_dirty。

_THROWER = lambda *a, **k: (_ for _ in ()).throw(  # noqa: E731
    AssertionError("不得独立调用 repo 方法（必须单事务内联）")
)


def test_p3_unknown_finish_inlines_single_transaction(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-p3", task_id="task-p3")
    agent = _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    store_obj = select_operation_store(agent)
    claim = store_obj.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="owner-a",
            run_id="run-p3",
            task_id="task-p3",
            operation_id="tool_call:attempt-p3:call-1",
            tool="p3_write",
            args_hash="sha256:ab",
            idempotency_key="key-p3-1",
            idempotency_scope="business",
            idempotency_namespace="p3_write",
            holder=new_tool_operation_holder(),
            lease_expires_at=9999999999.0,
            resource_scopes=("workspace:/p3-out",),
            attempt_id=attempt_id,  # seq 248 #1：MANAGED claim 必传真实调用者 attempt
        )
    )
    assert claim.action == "execute"
    # UNKNOWN 收尾不得走 repo 独立方法：单事务内联完成后三个断言必须成立
    monkeypatch.setattr(repo, "mark_operation_unknown", _THROWER)
    monkeypatch.setattr(repo, "release_locks", _THROWER)
    monkeypatch.setattr(repo, "mark_mutation_dirty", _THROWER)
    record = store_obj.finish_tool_operation(
        ToolOperationCompletionRequest(
            owner_id="owner-a",
            run_id="run-p3",
            operation_id="tool_call:attempt-p3:call-1",
            holder_id=claim.record.holder_id,
            generation=claim.record.generation,
            status=TOOL_OPERATION_UNKNOWN,
            result={},
            error_code="",
            unknown_reason="injected_crash",
        )
    )
    assert record.status == TOOL_OPERATION_UNKNOWN  # 当前：调 repo 方法即抛 → 红
    assert record.unknown_reason == "injected_crash"
    with repo._runtime_connection() as conn:
        locks = conn.execute("SELECT COUNT(*) AS n FROM resource_locks").fetchone()
    assert locks["n"] == 0  # 锁已释放
    mutations = _mutation_rows(repo)
    assert len(mutations) == 1
    assert mutations[0]["state"] == "DIRTY"  # mutation 终态 DIRTY
    assert mutations[0]["dirty_reason"]  # 非空原因


# ============================================================== P4：settle 单事务
# 期望（修后）：finish(SUCCEEDED/FAILED) 单事务内联（终态 CAS + lock delete +
# mutation STABLE 同 commit），不调用 repo.settle_operation / release_locks /
# mark_mutation_stable 独立方法。
# 现状（红测）：settle 先 commit → 锁释放再 commit → mutation 再 commit（间隙崩溃
# → SUCCEEDED 但锁仍在/MUTATING）。


def test_p4_success_finish_inlines_single_transaction(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-p4", task_id="task-p4")
    agent = _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    store_obj = select_operation_store(agent)
    claim = store_obj.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="owner-a",
            run_id="run-p4",
            task_id="task-p4",
            operation_id="tool_call:attempt-p4:call-1",
            tool="p4_write",
            args_hash="sha256:ab",
            idempotency_key="key-p4-1",
            idempotency_scope="business",
            idempotency_namespace="p4_write",
            holder=new_tool_operation_holder(),
            lease_expires_at=9999999999.0,
            resource_scopes=("workspace:/p4-out",),
            attempt_id=attempt_id,  # seq 248 #1：MANAGED claim 必传真实调用者 attempt
        )
    )
    assert claim.action == "execute"
    monkeypatch.setattr(repo, "settle_operation", _THROWER)
    monkeypatch.setattr(repo, "release_locks", _THROWER)
    monkeypatch.setattr(repo, "mark_mutation_stable", _THROWER)
    record = store_obj.finish_tool_operation(
        ToolOperationCompletionRequest(
            owner_id="owner-a",
            run_id="run-p4",
            operation_id="tool_call:attempt-p4:call-1",
            holder_id=claim.record.holder_id,
            generation=claim.record.generation,
            status="succeeded",
            result={"ok": True},
            error_code="",
        )
    )
    assert record.status == "succeeded"  # 当前：调 repo 方法即抛 → 红
    with repo._runtime_connection() as conn:
        locks = conn.execute("SELECT COUNT(*) AS n FROM resource_locks").fetchone()
    assert locks["n"] == 0  # 终态同事务删锁
    mutations = _mutation_rows(repo)
    assert len(mutations) == 1
    assert mutations[0]["state"] == "STABLE"  # mutation 终态 STABLE


# ============================================================== P5：scope resolver
# 期望（修后）：单一权威 resolver——绝对路径参数 canonicalize 后锁；list 参数
# 逐项锁；mutating 工具命中参数全部缺失时锁 effective_registry_cwd（run_command
# 缺省 working_dir 的真实写根）。
# 现状（红测）：绝对路径 continue / list 忽略 / 缺省 0 锁。


class _WorkingDirWriteTool(_PathWriteTool):
    """缺省 working_dir 参数工具（模拟 run_command）：schema 声明 working_dir，
    policy 引用它；参数缺省时 handler 写 effective cwd（P5 缺省写根实锤）。"""

    def __init__(self, name: str = "cwd_write"):
        self.calls = 0
        self.model_spec = make_test_model_spec(
            name,
            description="write one side effect at cwd",
            input_schema={
                "type": "object",
                "properties": {
                    "working_dir": {"type": "string"},
                    "value": {"type": "integer"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            "mutating",
            resource_parameters=("working_dir",),
        )

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, "completed")


def _scope_request(root: Path, call, tools, run_id: str) -> ToolExecutorRequest:
    return ToolExecutorRequest(
        call=call,
        runtime_snapshot=runtime_snapshot_for_tools(tools, run_id=run_id),
        workspace_root=root,
        write_boundary={"allowed_write_roots": [str(root)]},
    )


def test_p5_absolute_path_argument_locks_canonical_root(tmp_path):
    tool = _PathWriteTool("abs_write")
    abs_target = tmp_path / "abs-out"
    abs_target.mkdir(parents=True, exist_ok=True)
    call = _gate_call(
        run_id="run-p5a",
        tool_name="abs_write",
        operation_id="tool_call:attempt-p5a:call-1",
        snapshot=runtime_snapshot_for_tools({"abs_write": tool}, run_id="run-p5a"),
        attempt_id="attempt-p5a",
        arguments={"path": str(abs_target / "x.txt"), "value": 1},
    )
    request = _scope_request(tmp_path, call, {"abs_write": tool}, "run-p5a")
    runtime = request.runtime_snapshot.runtime("abs_write")
    scopes = _workspace_operation_scopes(request, call, runtime)
    assert scopes == (f"workspace:{(abs_target / 'x.txt').resolve()}",)  # 当前: () → 红


def test_p5_list_argument_locks_each_item(tmp_path):
    tool = _PathWriteTool("list_write")
    call = _gate_call(
        run_id="run-p5b",
        tool_name="list_write",
        operation_id="tool_call:attempt-p5b:call-1",
        snapshot=runtime_snapshot_for_tools({"list_write": tool}, run_id="run-p5b"),
        attempt_id="attempt-p5b",
        arguments={"path": ["out/a.txt", "out/b.txt"], "value": 1},
    )
    request = _scope_request(tmp_path, call, {"list_write": tool}, "run-p5b")
    runtime = request.runtime_snapshot.runtime("list_write")
    scopes = _workspace_operation_scopes(request, call, runtime)
    assert scopes == (
        f"workspace:{(tmp_path / 'out' / 'a.txt').resolve()}",
        f"workspace:{(tmp_path / 'out' / 'b.txt').resolve()}",
    )  # 当前: () → 红


def test_p5_missing_argument_locks_effective_cwd(tmp_path):
    """run_command 缺省 working_dir → 真实写根 = effective_registry_cwd → 必须锁。"""
    tool = _WorkingDirWriteTool("cwd_write")
    call = _gate_call(
        run_id="run-p5c",
        tool_name="cwd_write",
        operation_id="tool_call:attempt-p5c:call-1",
        snapshot=runtime_snapshot_for_tools({"cwd_write": tool}, run_id="run-p5c"),
        attempt_id="attempt-p5c",
        arguments={"value": 1},  # working_dir 缺省
    )
    request = _scope_request(tmp_path, call, {"cwd_write": tool}, "run-p5c")
    runtime = request.runtime_snapshot.runtime("cwd_write")
    scopes = _workspace_operation_scopes(request, call, runtime)
    expected = effective_registry_cwd(tmp_path, request.write_boundary).resolve()
    assert scopes == (f"workspace:{expected}",)  # 当前: () → 红


def test_p5_action_policy_scopes_share_authoritative_resolver(tmp_path):
    """seq 245 P5「ActionPolicy/concurrency/operation-store 共用同一解析结果」。

    ActionPolicy 的 evidence.resource_scopes（审计记录）必须与 executor 权威
    resolver（operation-store 锁 scope）同一套：绝对路径 canonicalize 后锁
    workspace:{path}。现状：ActionPolicy 用旧文本投影 path:{raw} → 不一致 → 红。
    """
    from agent_py_agent.agent.tooling.action_policy import (
        ActionPolicy,
        ActionPolicyRequest,
    )

    tool = _PathWriteTool("ap_write")
    abs_target = tmp_path / "ap-out"
    abs_target.mkdir(parents=True, exist_ok=True)
    target = abs_target / "x.txt"
    call = _gate_call(
        run_id="run-p5d",
        tool_name="ap_write",
        operation_id="tool_call:attempt-p5d:call-1",
        snapshot=runtime_snapshot_for_tools({"ap_write": tool}, run_id="run-p5d"),
        attempt_id="attempt-p5d",
        arguments={"path": str(target), "value": 1},
    )
    snapshot = runtime_snapshot_for_tools({"ap_write": tool}, run_id="run-p5d")
    decision = ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            write_boundary={"allowed_write_roots": [str(tmp_path)]},
        )
    )
    assert decision.allowed
    scopes = decision.resource_scopes
    assert scopes == (f"workspace:{target.resolve()}",)  # 当前: ("path:{str}",) → 红


def test_p5_concurrency_scopes_share_authoritative_resolver(tmp_path):
    """seq 245 P5：concurrency 调度投影与锁同一物理根归一化。

    带 workspace_root 上下文时，out/x 与 out/../out/x 必须归一化到同一 scope
    （旧文本投影 path:out/x vs path:out/../out/x 算两个资源 → 与锁不一致 → 红）。
    规划器只在 parallel_safe + read_only（parallel_eligible）路径解析 scopes，
    故用只读工具；逐 call 判定，用两个 call 各带一个路径验证归一化。
    """
    from agent_py_agent.agent.tooling.concurrency import describe_tool_concurrency

    tool = _ReadOnlyTool("cc_write")
    snapshot = runtime_snapshot_for_tools({"cc_write": tool}, run_id="run-p5e")

    def describe(path_value: str):
        return describe_tool_concurrency(
            snapshot,
            _gate_call(
                run_id="run-p5e",
                tool_name="cc_write",
                operation_id="tool_call:attempt-p5e:call-1",
                snapshot=snapshot,
                attempt_id="attempt-p5e",
                arguments={"path": path_value},
            ),
            workspace_root=tmp_path,
            write_boundary={"allowed_write_roots": [str(tmp_path)]},
        )

    aliased = describe("out/x")
    normalized = describe("out/../out/x")
    assert len(aliased.resource_scopes) == 1  # 当前: ("path:out/x",) vs ("path:out/../out/x",) → 红
    assert normalized.resource_scopes == aliased.resource_scopes
    assert aliased.resource_scopes == (f"workspace:{(tmp_path / 'out' / 'x').resolve()}",)


# ============================================================== P6：reopen + renew
# 期望（修后）：reopen 复用 claim/start 事务（重取资源锁 + mutation MUTATING +
# current-attempt CAS）；takeover 后 reopen 拒绝；store 暴露 lease renew。
# 现状（红测）：reopen 只 UPDATE 行状态（无锁/无 mutation/无 attempt CAS），
# store 无 renew 方法。


def _unknown_operation(repo: RuntimeRepository, store_obj, run_id: str, op_id: str, *, attempt_id: str = ""):
    claim = store_obj.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="owner-a",
            run_id=run_id,
            task_id=f"task-{run_id}",
            operation_id=op_id,
            tool="p6_write",
            args_hash="sha256:ab",
            idempotency_key=f"key-{run_id}-1",
            idempotency_scope="business",
            idempotency_namespace="p6_write",
            holder=new_tool_operation_holder(),
            lease_expires_at=9999999999.0,
            resource_scopes=("workspace:/p6-out",),
            attempt_id=attempt_id,  # seq 248 #1：MANAGED claim 必传真实调用者 attempt
        )
    )
    assert claim.action == "execute"
    store_obj.finish_tool_operation(
        ToolOperationCompletionRequest(
            owner_id="owner-a",
            run_id=run_id,
            operation_id=op_id,
            holder_id=claim.record.holder_id,
            generation=claim.record.generation,
            status=TOOL_OPERATION_UNKNOWN,
            result={},
            error_code="",
            unknown_reason="injected_crash",
        )
    )
    return claim


def _reopen_request(claim, op_id: str) -> ToolOperationReopenRequest:
    return ToolOperationReopenRequest(
        owner_id="owner-a",
        run_id=claim.record.run_id,
        operation_id=op_id,
        expected_generation=claim.record.generation,
        holder=new_tool_operation_holder(),
        lease_expires_at=9999999999.0,
        source_ref="p6:reconciliation",
        reconciliation_result={"ok": False, "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN"},
    )


def test_p6_reopen_reacquires_locks_and_mutation(tmp_path):
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-p6a", task_id="task-run-p6a")
    agent = _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    store_obj = select_operation_store(agent)
    op_id = "tool_call:attempt-p6a:call-1"
    claim = _unknown_operation(repo, store_obj, "run-p6a", op_id, attempt_id=attempt_id)

    reopened = store_obj.reopen_tool_operation_after_reconciliation(
        _reopen_request(claim, op_id)
    )

    assert reopened.status == TOOL_OPERATION_RUNNING  # 当前：可行但无锁 → 下两断言红
    with repo._runtime_connection() as conn:
        locks = conn.execute(
            "SELECT canonical_scope FROM resource_locks"
        ).fetchall()
    assert [row["canonical_scope"] for row in locks] == ["workspace:/p6-out"]  # 重取锁
    mutations = _mutation_rows(repo)
    assert len(mutations) == 1
    assert mutations[0]["state"] == "MUTATING"  # 重开后 mutation 声明 MUTATING


def test_p6_reopen_rejected_after_takeover(tmp_path):
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(repo, "run-p6b", task_id="task-run-p6b")
    agent = _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    store_obj = select_operation_store(agent)
    op_id = "tool_call:attempt-p6b:call-1"
    claim = _unknown_operation(repo, store_obj, "run-p6b", op_id, attempt_id=attempt_id)
    # takeover：推进 current pointer → 旧 attempt 的 reconcile 重开必须拒绝
    repo.create_attempt(agent_run_id)

    with pytest.raises(ToolOperationOwnershipError):
        store_obj.reopen_tool_operation_after_reconciliation(
            _reopen_request(claim, op_id)
        )  # 当前：无 attempt CAS → 成功不抛 → 红


def test_p6_lease_renew_contract_present(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    store_obj = select_operation_store(agent)
    assert callable(getattr(store_obj, "renew_tool_operation_lease", None))  # 当前：无 → 红


# ============================================================== seq 248：身份/代数 fence
# 对方（seq 248）复核 seq 245 修复后给出 7 个可复现 P1，要求「先补 5 个反例：
# 旧只读 attempt、旧 gen settle、epoch bump、错 task、lease-expiry DIRTY，再修
# 底层」。
#
# - #1 只读 authority 门不比较调用者 attempt_id（takeover 后旧 attempt 调
#   read_only 工具仍 ok=True, handler_calls=1）→ 所有工具门必须传
#   call.attempt_id 并同查 run→task_run→task、current attempt/generation；
#   MANAGED 下 attempt 为空必须拒绝。
# - #2 settle/UNKNOWN CAS 缺 holder_id + tool_operation_generation（gen1
#   UNKNOWN→reopen gen2 后，gen1 旧 holder 仍可写 SUCCEEDED，结果来自
#   stale-old-worker）→ SUCCESS/FAILED/UNKNOWN 同事务校验 status、holder、
#   期望 generation、attempt/current pointer、captured workspace_epoch。
# - #3 workspace_epoch 只进 resource_locks，operation 权威行未捕获，settle 不
#   比较（claim 后 bump epoch，旧操作仍可成功 settle）→ claim_epoch 持久化到
#   行，settle/reopen/renew 与当前 epoch CAS。
# - #4 require_authority/claim 按 run_id 单查，request.task_id 未 join
#   tasks/task_runs（run 属 task-real，传 task-wrong 仍 ok）→ 一次 join 校验
#   task_id/task_run/agent_run/attempt。
# - #5 自动过期转 UNKNOWN 只写 UNKNOWN 并删锁，不把 scope mutation 置 DIRTY
#   → 同事务 UNKNOWN+删锁+所有 scope DIRTY。


def _s248_claim_request(
    *,
    run_id: str,
    op_id: str,
    task_id: str,
    holder: object,
    resource_scopes: tuple[str, ...] = ("workspace:/s248-out",),
    idempotency_key: str = "key-s248",
    attempt_id: str = "",
) -> ToolOperationClaimRequest:
    return ToolOperationClaimRequest(
        owner_id="owner-a",
        run_id=run_id,
        task_id=task_id,
        operation_id=op_id,
        tool="s248_write",
        args_hash="sha256:ab",
        idempotency_key=idempotency_key,
        idempotency_scope="business",
        idempotency_namespace="s248_write",
        holder=holder,
        lease_expires_at=9999999999.0,
        resource_scopes=resource_scopes,
        attempt_id=attempt_id,
    )


def _s248_authority(
    store_obj,
    *,
    run_id: str,
    task_id: str,
    op_id: str,
    attempt_id: str,
) -> None:
    store_obj.require_authority(
        ToolOperationAuthorityRequest(
            owner_id="owner-a",
            run_id=run_id,
            task_id=task_id,
            operation_id=op_id,
            tool_name="s248_read",
            attempt_id=attempt_id,
        )
    )


def test_s248_authority_rejects_stale_or_missing_attempt(tmp_path):
    """seq 248 #1：只读权威门必须比较调用者 attempt（旧 attempt 拒绝）。"""
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248a", task_id="task-s248a"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )

    # 当前 attempt 过门（正例）。
    _s248_authority(
        store_obj, run_id="run-s248a", task_id="task-s248a",
        op_id="tool_call:attempt-s248a:call-1", attempt_id=attempt_id,
    )
    # MANAGED 下 attempt 为空必须拒绝（主链恒注入可信 attempt_id）。
    with pytest.raises(AuthorityContextMissing):
        _s248_authority(
            store_obj, run_id="run-s248a", task_id="task-s248a",
            op_id="tool_call:attempt-s248a:call-2", attempt_id="",
        )
    # takeover 后旧 attempt 的只读调用必须拒绝（当前：只查 current 非空 → 红）。
    repo.create_attempt(agent_run_id)
    with pytest.raises(AuthorityContextMissing):
        _s248_authority(
            store_obj, run_id="run-s248a", task_id="task-s248a",
            op_id="tool_call:attempt-s248a:call-3", attempt_id=attempt_id,
        )


def test_s248_settle_rejects_stale_generation_and_holder(tmp_path):
    """seq 248 #2：settle 必须同事务 CAS holder + tool_operation_generation。"""
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248b", task_id="task-s248b"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    holder = new_tool_operation_holder()
    claim = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248b", task_id="task-s248b",
            op_id="tool_call:attempt-s248b:call-1",
            holder=holder, attempt_id=attempt_id,
        )
    )
    assert claim.action == "execute"

    # 旧代数 settle：行是 gen1，伪造 generation=99 → 必须拒绝（当前：WHERE 无
    # generation → SUCCEEDED 成功 → 红）。
    with pytest.raises(RuntimeConflictError):
        store_obj.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-a",
                run_id="run-s248b",
                operation_id=claim.record.operation_id,
                holder_id=claim.record.holder_id,
                generation=99,
                status=TOOL_OPERATION_SUCCEEDED,
                result={"ok": True},
            )
        )
    # 伪造 holder：他人提交终态 → 必须拒绝（当前：holder 只用于删锁 → 红）。
    # 注：seq 253 #2 后，call-1 的旧代数 settle 被拒不再毒化行——call-1 的锁
    # 保持持有，call-2 必须用独立 scope（否则撞锁失败的是 claim 而非 settle）。
    claim2 = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248b", task_id="task-s248b",
            op_id="tool_call:attempt-s248b:call-2",
            holder=holder, attempt_id=attempt_id,
            idempotency_key="key-s248-b2",
            resource_scopes=("workspace:/s248-out-2",),
        )
    )
    with pytest.raises(RuntimeConflictError):
        store_obj.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-a",
                run_id="run-s248b",
                operation_id=claim2.record.operation_id,
                holder_id="stale-holder",
                generation=claim2.record.generation,
                status=TOOL_OPERATION_SUCCEEDED,
                result={"ok": True},
            )
        )


def test_s248_settle_rejects_workspace_epoch_bump(tmp_path):
    """seq 248 #3：claim_epoch 必须持久化并参与 settle CAS（bump epoch 后拒）。"""
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248c", task_id="task-s248c"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    claim = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248c", task_id="task-s248c",
            op_id="tool_call:attempt-s248c:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
        )
    )
    # bump workspace_epoch（新快照开始）：旧操作 settle 必须拒绝。注意
    # _runtime_connection 只 close 不 commit，必须显式 commit 否则 UPDATE 回滚
    # （bump 不生效 → settle 放行 → 假绿，R5 同坑）。
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET workspace_epoch = workspace_epoch + 1 "
            "WHERE agent_run_id = ?",
            (agent_run_id,),
        )
        conn.commit()
    with pytest.raises(RuntimeConflictError):
        store_obj.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-a",
                run_id="run-s248c",
                operation_id=claim.record.operation_id,
                holder_id=claim.record.holder_id,
                generation=claim.record.generation,
                status=TOOL_OPERATION_SUCCEEDED,
                result={"ok": True},
            )
        )


def test_s248_claim_and_authority_reject_wrong_task(tmp_path):
    """seq 248 #4：claim/authority 必须 join 校验 task_id（错 task 拒绝）。"""
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248d", task_id="task-s248d"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    # claim 传错 task：run 属于 task-s248d，参数声明 task-wrong → 必须拒绝
    # （当前：单查 agent_runs，task_id 只进 payload → 执行 → 红）。
    with pytest.raises(AuthorityContextMissing):
        store_obj.claim_tool_operation(
            _s248_claim_request(
                run_id="run-s248d", task_id="task-wrong",
                op_id="tool_call:attempt-s248d:call-1",
                holder=new_tool_operation_holder(), attempt_id=attempt_id,
            )
        )
    # authority 门同样必须 join 校验 task（当前：不查 → 放行 → 红）。
    with pytest.raises(AuthorityContextMissing):
        _s248_authority(
            store_obj, run_id="run-s248d", task_id="task-wrong",
            op_id="tool_call:attempt-s248d:call-2", attempt_id=attempt_id,
        )


def test_s248_expired_lease_unknown_marks_mutation_dirty(tmp_path):
    """seq 248 #5：自动过期转 UNKNOWN 必须同事务把 scope mutation 置 DIRTY。"""
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248e", task_id="task-s248e"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    claim = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248e", task_id="task-s248e",
            op_id="tool_call:attempt-s248e:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
        )
    )
    # 让操作「过期且持有者不可判活」：lease 改过去 + holder pid 清 0。
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT outcome_json FROM tool_operations WHERE operation_id = ?",
            (claim.record.operation_id,),
        ).fetchone()
        payload = json.loads(row["outcome_json"])
        payload["lease_expires_at"] = 1.0
        payload["holder"] = {**payload.get("holder", {}), "pid": 0}
        conn.execute(
            "UPDATE tool_operations SET outcome_json = ? WHERE operation_id = ?",
            (json.dumps(payload), claim.record.operation_id),
        )
        conn.commit()  # _runtime_connection 只 close 不 commit
    # 第二次 claim：决策 unknown（lease 过期）。
    second = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248e", task_id="task-s248e",
            op_id=claim.record.operation_id,
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
        )
    )
    assert second.action == "unknown"
    # 锁已删（绿，现状已内联）+ mutation 必须 DIRTY（当前：仍 MUTATING → 红）。
    with repo._runtime_connection() as conn:
        locks = conn.execute("SELECT * FROM resource_locks").fetchall()
    assert locks == []
    mutations = _mutation_rows(repo)
    assert len(mutations) == 1
    assert mutations[0]["state"] == "DIRTY"
    assert mutations[0]["dirty_reason"]


# ============================================================== seq 248 #6：
# scope 语义（path/logical 区分 + shell workspace-wide + 层级冲突）。
# 期望（修后）：logical 型参数（session_id/url/query）投影 logical 文本 scope
# 绝不 resolve 成 workspace 假写根；shell 类工具（__sandbox_write_roots）锁
# 全部 allowed_write_roots（bwrap 可写全部写根）；workspace 锁 parent/child
# 物理重叠与 hardlink 同 inode 在 INSERT 前显式判冲突（fail-fast）——
# UNIQUE(canonical_scope) 精确串拦不了这两类。


class _LogicalSessionTool(_PathWriteTool):
    """逻辑型参数工具（模拟 process_status）：session_id 是逻辑 ID 不是路径。"""

    def __init__(self, name: str = "logical_tool"):
        self.calls = 0
        self.model_spec = make_test_model_spec(
            name,
            description="operate one logical session",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "value": {"type": "integer"},
                },
                "required": ["session_id", "value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            "mutating",
            resource_parameters=("session_id",),
            resource_parameter_kinds={"session_id": "logical"},
        )

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, "completed")


def test_s248_logical_parameter_projects_text_scope(tmp_path):
    """seq 248 #6：logical 型参数投影 logical 文本 scope，绝不 resolve 成
    workspace 假写根（把逻辑 ID 当路径锁会锁错根且互不冲突）。"""
    tool = _LogicalSessionTool("logical_tool")
    call = _gate_call(
        run_id="run-s248f",
        tool_name="logical_tool",
        operation_id="tool_call:attempt-s248f:call-1",
        snapshot=runtime_snapshot_for_tools({"logical_tool": tool}, run_id="run-s248f"),
        attempt_id="attempt-s248f",
        arguments={"session_id": "bg-1-1718500000", "value": 1},
    )
    request = _scope_request(tmp_path, call, {"logical_tool": tool}, "run-s248f")
    runtime = request.runtime_snapshot.runtime("logical_tool")
    scopes = _workspace_operation_scopes(request, call, runtime)
    assert scopes == ("logical:session_id:bg-1-1718500000",)


# LLM: Filesystem paths remain visible to policy/audit but must never enter the cross-run
# operation lease after the 会话运行时 concurrency migration.
# 函数用途: 验证普通目录锁被一次性过滤，精确控制面逻辑锁仍保留。
def test_codex_style_durable_scopes_drop_workspace_and_keep_logical():
    assert _durable_operation_scopes(
        (
            "workspace:/root",
            "workspace:/root/project",
            "logical:agent_run:run-1",
            "logical:agent_run:run-1",
        )
    ) == ("logical:agent_run:run-1",)


# LLM: This is the production ToolExecutor seam, not merely a resolver unit test. A legacy
# workspace lease may remain in an upgraded database, but a new ordinary write must not consult
# it as admission authority.
# 函数用途: 模拟旧版留下的过期父目录锁，验证新任务在子目录仍能真正进入工具 handler。
def test_tool_executor_ignores_legacy_workspace_lease(tmp_path):
    repo = _repo(tmp_path)
    _old_agent_run, old_attempt = _direct_register_chain(
        repo, "run-old-lock", task_id="task-old-lock"
    )
    _new_agent_run, new_attempt = _direct_register_chain(
        repo, "run-new-write", task_id="task-new-write"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    store_obj.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="owner-a",
            run_id="run-old-lock",
            task_id="task-old-lock",
            operation_id="tool_call:old:call-1",
            tool="legacy_write",
            args_hash="sha256:old",
            idempotency_key="legacy-workspace-lock",
            idempotency_scope="operation",
            idempotency_namespace="legacy_write",
            holder=new_tool_operation_holder(),
            lease_expires_at=9999999999.0,
            resource_scopes=(f"workspace:{tmp_path.resolve()}",),
            attempt_id=old_attempt,
        )
    )
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE resource_locks SET lease_expires_at = 1 WHERE attempt_id = ?",
            (old_attempt,),
        )
        conn.commit()

    tool = _PathWriteTool("new_write")
    snapshot = runtime_snapshot_for_tools({"new_write": tool}, run_id="run-new-write")
    agent = _managed_chain(repo, _store(tmp_path), [tool], tmp_path)
    execution = _traced_call(
        agent,
        _params(run_id="run-new-write", task_id="task-new-write", snapshot=snapshot),
        _gate_call(
            run_id="run-new-write",
            tool_name="new_write",
            operation_id="tool_call:new:call-1",
            snapshot=snapshot,
            attempt_id=new_attempt,
            arguments={"path": "child/out.txt", "value": 1},
        ),
    )

    assert execution.result.ok is True
    assert tool.calls == 1


class _SandboxWriteTool(_PathWriteTool):
    """shell 类工具（模拟 run_command）：input_policy 声明 __sandbox_write_roots，
    bwrap 沙箱可写全部 allowed_write_roots。"""

    def __init__(self, name: str = "sandbox_write"):
        self.calls = 0
        self.model_spec = make_test_model_spec(
            name,
            description="write anywhere inside sandbox write roots",
            input_schema={
                "type": "object",
                "properties": {
                    "working_dir": {"type": "string"},
                    "value": {"type": "integer"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            "dangerous",
            resource_parameters=("working_dir",),
            internal_parameters=("__sandbox_write_roots",),
        )

    # seq 253 #5：协议方法——模拟 ShellTool 语义，bwrap 可写全部
    # allowed_write_roots（与写边界同一 resolved_write_roots 解析器）。
    def effective_write_roots(self, arguments, write_boundary, workspace_root):
        _ = arguments
        from agent_py_agent.agent.tooling.write_boundary import resolved_write_roots

        return tuple(str(p) for p in resolved_write_roots(write_boundary, workspace_root))

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, "completed")


def test_s248_sandbox_shell_locks_all_allowed_write_roots(tmp_path):
    """seq 248 #6：shell 类工具不只锁 working_dir——bwrap 可写全部
    allowed_write_roots，锁必须 workspace-wide（只锁 cwd 的假安全）。"""
    tool = _SandboxWriteTool("sandbox_write")
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    call = _gate_call(
        run_id="run-s248g",
        tool_name="sandbox_write",
        operation_id="tool_call:attempt-s248g:call-1",
        snapshot=runtime_snapshot_for_tools({"sandbox_write": tool}, run_id="run-s248g"),
        attempt_id="attempt-s248g",
        arguments={"working_dir": "out", "value": 1},
    )
    request = ToolExecutorRequest(
        call=call,
        runtime_snapshot=runtime_snapshot_for_tools({"sandbox_write": tool}, run_id="run-s248g"),
        workspace_root=tmp_path,
        write_boundary={
            "allowed_write_roots": [
                str(tmp_path / "out"),
                str(tmp_path / "docs"),
            ]
        },
    )
    runtime = request.runtime_snapshot.runtime("sandbox_write")
    scopes = _workspace_operation_scopes(request, call, runtime)
    assert set(scopes) == {
        f"workspace:{(tmp_path / 'out').resolve()}",
        f"workspace:{(tmp_path / 'docs').resolve()}",
    }


def test_s248_lock_rejects_parent_child_overlap(tmp_path):
    """seq 248 #6：UNIQUE 精确串拦不了 parent/child 物理重叠——先锁父根再
    锁子路径必须 fail-fast RuntimeConflictError。WRITE-04 修订: 同一 attempt
    的父子 scope 是同一执行者合法声明(跳过), 跨 attempt/执行者重叠仍拦截——
    用第二个 run/attempt 验证冲突保护不变。"""
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248h", task_id="task-s248h"
    )
    _other_run_id, other_attempt_id = _direct_register_chain(
        repo, "run-s248h-other", task_id="task-s248h"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    parent = tmp_path.resolve()
    store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248h", task_id="task-s248h",
            op_id="tool_call:attempt-s248h:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
            resource_scopes=(f"workspace:{parent}",),
        )
    )
    # 同一 attempt 的父子 scope → 允许(WRITE-04: 同执行者并行工具合法声明)
    store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248h", task_id="task-s248h",
            op_id="tool_call:attempt-s248h:call-1b",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
            resource_scopes=(f"workspace:{parent / 'sub'}",),
            idempotency_key="key-s248-h1b",
        )
    )
    # 跨 attempt 的父子重叠 → 仍 fail-fast
    with pytest.raises(RuntimeConflictError):
        store_obj.claim_tool_operation(
            _s248_claim_request(
                run_id="run-s248h-other", task_id="task-s248h",
                op_id="tool_call:attempt-s248h-other:call-2",
                holder=new_tool_operation_holder(), attempt_id=other_attempt_id,
                resource_scopes=(f"workspace:{parent / 'sub'}",),
                idempotency_key="key-s248-h2",
            )
        )


def test_s248_lock_rejects_hardlink_same_inode(tmp_path):
    """seq 248 #6：hardlink 同 inode 是同一物理文件，串不同 UNIQUE 拦不了
    ——inode 判定必须冲突（fail-fast）。WRITE-04 修订: 同一 attempt 跳过,
    跨 attempt/执行者仍拦截——用第二个 run/attempt 验证冲突保护不变。"""
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s248i", task_id="task-s248i"
    )
    _other_run_id, other_attempt_id = _direct_register_chain(
        repo, "run-s248i-other", task_id="task-s248i"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    first = (tmp_path / "h1.txt").resolve()
    second = (tmp_path / "h2.txt").resolve()
    first.write_text("same inode")
    os.link(first, second)
    store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s248i", task_id="task-s248i",
            op_id="tool_call:attempt-s248i:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
            resource_scopes=(f"workspace:{first}",),
        )
    )
    with pytest.raises(RuntimeConflictError):
        store_obj.claim_tool_operation(
            _s248_claim_request(
                run_id="run-s248i-other", task_id="task-s248i",
                op_id="tool_call:attempt-s248i-other:call-2",
                holder=new_tool_operation_holder(), attempt_id=other_attempt_id,
                resource_scopes=(f"workspace:{second}",),
                idempotency_key="key-s248-i2",
            )
        )


# ============================================================== seq 253：五个新 P1 反例
# 对方（seq 253）复核 seq 248 后给出 5 个可复现 P1，要求「先为以上 5 条补反例
# 并修底层」：
#
# - #1 completion 空 holder 仍可绕过 fence：finish_tool_operation 把 holder 传入
#   _settle_operation_via_repo，后者 `if expected_holder` 才比较（空值=跳过校验）
#   → holder 必须无条件非空且恒等比较，空值不能当「跳过校验」。
# - #2 被拒绝的旧 generation 会毒化当前操作：generation 错误先触发
#   RuntimeConflictError，随后 catch 调 _mark_settle_failure（不带
#   expected_generation）→ 有效 EXECUTING 行被改成 UNKNOWN。身份/代数冲突
#   ≠ 执行结果不明；只有当前 holder/current generation 的真实持久化故障才可
#   UNKNOWN。
# - #3 reopen 仍没做 workspace_epoch fence：reopen 查询当前 epoch 只用于新锁，
#   既不比较 payload 捕获 epoch 也不更新 → 形成「可执行、不可收口」状态。
# - #4 renew 仍没与当前 workspace_epoch 做 CAS：renew 只校验 operation 行与旧
#   lock/payload 彼此一致，没有 join agent_runs 的当前 epoch。
# - #5 scope 覆盖仍不完整：ApplyPatchTool 是 mutating + mutates_workspace 但
#   无 operation lock；ControlledExecTool 真实写边界来自 grant.path_scope，
#   只声明 cwd。走底层单一权威：工具结构化提供 effective_write_roots，
#   由写边界、operation lock、调度共用，不再加工具名特判。


def test_s253_empty_holder_settle_rejected(tmp_path):
    """seq 253 #1：completion 空 holder 必须无条件拒绝（空值≠跳过校验）。
    当前：holder_id="" + generation 正确 → 写成 SUCCEEDED → 红。"""
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s253a", task_id="task-s253a"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    claim = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s253a", task_id="task-s253a",
            op_id="tool_call:attempt-s253a:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
        )
    )
    with pytest.raises(RuntimeConflictError):
        store_obj.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-a",
                run_id="run-s253a",
                operation_id=claim.record.operation_id,
                holder_id="",  # 空 holder：不得当「跳过校验」
                generation=claim.record.generation,
                status=TOOL_OPERATION_SUCCEEDED,
                result={"ok": True},
            )
        )
    # 拒绝后权威行必须保持 EXECUTING（未被污染）；DB 内部状态码是
    # EXECUTING，与对外 record 常量（running）不同
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT status, settled_at FROM tool_operations WHERE operation_id = ?",
            (claim.record.operation_id,),
        ).fetchone()
    assert row["status"] == "EXECUTING"  # 当前：succeeded → 红
    assert int(row["settled_at"]) == 0


def test_s253_stale_generation_settle_keeps_executing(tmp_path):
    """seq 253 #2：代数冲突被拒 ≠ 结果不明——拒绝后有效 EXECUTING 行必须保持
    EXECUTING，不能被 _mark_settle_failure 改成 UNKNOWN。当前：finish 带
    gen=当前+99 → 抛 RuntimeConflictError 后行被标 UNKNOWN → 红。"""
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s253b", task_id="task-s253b"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    claim = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s253b", task_id="task-s253b",
            op_id="tool_call:attempt-s253b:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
        )
    )
    with pytest.raises(RuntimeConflictError):
        store_obj.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-a",
                run_id="run-s253b",
                operation_id=claim.record.operation_id,
                holder_id=claim.record.holder_id,
                generation=claim.record.generation + 99,  # 旧/伪造代数
                status=TOOL_OPERATION_SUCCEEDED,
                result={"ok": True},
            )
        )
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT status, settled_at FROM tool_operations WHERE operation_id = ?",
            (claim.record.operation_id,),
        ).fetchone()
    assert row["status"] == "EXECUTING"  # 当前：UNKNOWN → 红
    assert int(row["settled_at"]) == 0
    # 真 holder + 当前代数仍可正常收口（行未被毒化）
    record = store_obj.finish_tool_operation(
        ToolOperationCompletionRequest(
            owner_id="owner-a",
            run_id="run-s253b",
            operation_id=claim.record.operation_id,
            holder_id=claim.record.holder_id,
            generation=claim.record.generation,
            status=TOOL_OPERATION_SUCCEEDED,
            result={"ok": True},
        )
    )
    assert record.status == TOOL_OPERATION_SUCCEEDED


def test_s253_reopen_rejects_workspace_epoch_bump(tmp_path):
    """seq 253 #3：reopen 必须对当前 workspace_epoch 做 fence——bump 后旧
    UNKNOWN 操作不能 reopen（否则可执行、不可收口）。当前：epoch 1→2 后仍
    成功 reopen 为 running/gen2 → 红。"""
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s253c", task_id="task-run-s253c"  # _unknown_operation 内部用 f"task-{run_id}"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    op_id = "tool_call:attempt-s253c:call-1"
    claim = _unknown_operation(repo, store_obj, "run-s253c", op_id, attempt_id=attempt_id)
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET workspace_epoch = workspace_epoch + 1 "
            "WHERE agent_run_id = ?",
            (agent_run_id,),
        )
        conn.commit()
    with pytest.raises(RuntimeConflictError):
        store_obj.reopen_tool_operation_after_reconciliation(
            _reopen_request(claim, op_id)
        )  # 当前：epoch 不符仍成功 reopen → 红


def test_s253_renew_rejects_workspace_epoch_bump(tmp_path):
    """seq 253 #4：renew 必须 join agent_runs 当前 workspace_epoch 做 CAS——
    bump 后旧操作续租必须拒绝。当前：renew 只校验行与旧锁/旧 payload 一致，
    epoch 1→2 后仍续租成功 → 红。"""
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-s253d", task_id="task-s253d"
    )
    store_obj = select_operation_store(
        _agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path)
    )
    claim = store_obj.claim_tool_operation(
        _s248_claim_request(
            run_id="run-s253d", task_id="task-s253d",
            op_id="tool_call:attempt-s253d:call-1",
            holder=new_tool_operation_holder(), attempt_id=attempt_id,
        )
    )
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET workspace_epoch = workspace_epoch + 1 "
            "WHERE agent_run_id = ?",
            (agent_run_id,),
        )
        conn.commit()
    with pytest.raises(RuntimeConflictError):
        store_obj.renew_tool_operation_lease(
            operation_id=claim.record.operation_id,
            holder_id=claim.record.holder_id,
            lease_expires_at=9999999999.0,
            owner_id="owner-a",
            run_id="run-s253d",
        )  # 当前：epoch 不符仍续租成功 → 红


def test_s253_apply_patch_locks_patch_target_files(tmp_path):
    """seq 253 #5：ApplyPatchTool mutating + mutates_workspace，patch 的目标
    文件必须上 operation lock（多文件 patch 逐目标锁）。当前：无 resource_scopes
    → 0 锁 → 红。"""
    from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool

    tool = ApplyPatchTool(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: notes.txt\n"
        "-old\n"
        "+new\n"
        "*** Update File: config.ini\n"
        "-a=1\n"
        "+a=2\n"
        "*** End Patch\n"
    )
    call = _gate_call(
        run_id="run-s253e",
        tool_name="apply_patch",
        operation_id="tool_call:attempt-s253e:call-1",
        snapshot=runtime_snapshot_for_tools({"apply_patch": tool}, run_id="run-s253e"),
        attempt_id="attempt-s253e",
        arguments={"patch": patch},
    )
    request = ToolExecutorRequest(
        call=call,
        runtime_snapshot=runtime_snapshot_for_tools(
            {"apply_patch": tool}, run_id="run-s253e"
        ),
        workspace_root=tmp_path,
    )
    runtime = request.runtime_snapshot.runtime("apply_patch")
    scopes = _workspace_operation_scopes(request, call, runtime)
    assert set(scopes) == {
        f"workspace:{(tmp_path / 'notes.txt').resolve()}",
        f"workspace:{(tmp_path / 'config.ini').resolve()}",
    }  # 当前：空 → 红


def test_s253_controlled_exec_locks_grant_write_roots(tmp_path):
    """seq 253 #5：ControlledExecTool 真实写边界来自授权 grant.path_scope
    （cwd 之外可写授权根），只声明 cwd 参数锁不住 grant 写根。当前：只有
    workspace:cwd 一把锁 → 红。"""
    from agent_py_agent.agent.tooling.controlled_exec import ControlledExecTool

    tool = ControlledExecTool()
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    call = _gate_call(
        run_id="run-s253f",
        tool_name="controlled_exec",
        operation_id="tool_call:attempt-s253f:call-1",
        snapshot=runtime_snapshot_for_tools(
            {"controlled_exec": tool}, run_id="run-s253f"
        ),
        attempt_id="attempt-s253f",
        arguments={"apply": True, "command": "ls", "cwd": "out"},
    )
    request = ToolExecutorRequest(
        call=call,
        runtime_snapshot=runtime_snapshot_for_tools(
            {"controlled_exec": tool}, run_id="run-s253f"
        ),
        workspace_root=tmp_path,
        write_boundary={
            "controlled_exec_grants": [
                {
                    "grant_id": "grant-s253-1",
                    "command_allowlist": ["ls"],
                    "path_scope": ["out", "docs"],
                }
            ]
        },
    )
    runtime = request.runtime_snapshot.runtime("controlled_exec")
    scopes = _workspace_operation_scopes(request, call, runtime)
    assert set(scopes) == {
        f"workspace:{(tmp_path / 'out').resolve()}",
        f"workspace:{(tmp_path / 'docs').resolve()}",
    }  # 当前：只有 workspace:cwd(out) → 红
