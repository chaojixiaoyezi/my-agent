"""B 切片：唯一 ToolOperationCoordinator 门红测（对方 seq 219 矩阵 a-l + 221/228/231 修正）。

修订记录（对方 seq 231 + seq 235 复核后的契约修订）：
- 显式 ExecutionMode：权威字段挂 agent.subagents（对齐生产 SubAgentManager：
  subagents.runtime_db + subagents.execution_mode，manager.py:61/363-371，
  authority_fence.py:55 读取路径），值用 ExecutionMode 枚举成员；subagents
  恒存在（repo 可为 None），不再出现「repo=None 时无 subagents」形态。
- store 选择 seam（seq 235 问题 2）：_operation_store_for(agent) 表达目标 seam
  （生产落地点 runtime_db/execution_mode.select_operation_store）——MANAGED →
  runtime.db adapter，LOCAL_UNMANAGED → LocalStore，同一执行只选一次。
  生产 seam 未实现 → 测试态 MANAGED 暂落 local（保持双写等红点），
  test_b2 显式钉住「MANAGED 必须解析到 runtime.db adapter」红点；
  实现后自动切生产 seam（try-import），测试不自己决定 store 的权威来源。
- direct_register_chain：a3/c/d/e2/f/k/l 用 record_run_creation 直接建链，不再调
  authority_context 预建；a2 故意不建链。测试内零引用
  authority_context/open_authority_operation/close_authority_operation。
- c/d/f/f2/h 走真实主链（_traced_call → registry → ToolExecutor）+ MANAGED。
- c/h/l 的 ToolCall.attempt_id 一律用 _direct_register_chain 真实返回值（seq 235
  问题 3：严格 fence 实现后，测试伪造 attempt 会因不匹配而掩盖目标断言）。
- f 保留 runtime.db 原语版（settle current-pointer 原子校验，G4 补已实现，绿防
  回归；「兄弟」= 同 AgentRun 新 attempt，注释已修正）；f2 新增真实 ToolExecutor
  屏障集成（seq 235 问题 6）：旧 handler 运行中 takeover → 旧 settle 被拒 →
  旧执行不得向模型报 success；真正 sibling AgentRun 同时正常完成。
- d 拆分两个正交状态（seq 235 问题 4）：tool_operations.status == "UNKNOWN"
  （终态）；resource_mutations 对 canonical scope 的 state == "DIRTY" +
  dirty_reason 非空（DIRTY 属于 mutation 表，不是 operation 终态）；
  同 effect_key 重放不重跑 handler（handler_calls 计数）。
- e 保留 LocalStore 绿版 + 新增 e2 MANAGED replay（runtime.db 账本 effect_key 幂等行数）。
- h 锁冲突策略统一为 fail-fast（对齐 acquire_locks：UNIQUE(canonical_scope) 冲突
  → 事务回滚 → RuntimeConflictError，不等待串行）；同根三类路径：原始串 /
  ../ 别名归一化 / symlink 指向同根（seq 235 问题 5）；不同物理根可并行。
- k 更名 ask→approved-binding 两次调用（非真实异步 suspend，seq 235 问题 6）；
  补「ask 悬挂期间另一合法 mutating 操作能完成」断言（不占 EXECUTING 不占锁）。
- l 缺 authority handler=0 / 有 authority op=0（attempt_id 用真实返回值）。
- seq 238 第三轮复核修订：a1/a2/a3/d 全部改真实 ToolExecutor 主链（ToolRegistry +
  _PathWriteTool + runtime_snapshot + _traced_call），删除 _GateTools 桩（绕过唯一
  coordinator/真实 ToolSpec.effect/resource_parameters/store selector/result 封装）；
  全文件只传 ExecutionMode 枚举，_manager_mode 遇非枚举抛错（fail-closed，不静默默认
  MANAGED）；新增 test_b3 生产装配口 caller 测试（agent/core.py:_build_tool_registry
  唯一装配口必须经 selector 选 store，core.py:846 现写死 agent.local_store → 红）；
  seam 签名统一为 select_operation_store(agent)，建议落专门模块
  runtime_db/operation_store_selector.py（不再挤 execution_mode.py）；
  h3 改双线程 + 双 started 事件证明不同物理根真并行（双方 handler 都进入后才 release）；
  f2 补紧断言：takeover 后旧 operation=UNKNOWN、旧 mutation=DIRTY（不得 publish）；
  d 补紧断言：重放后 runtime.db 仍唯一 + LocalStore=0（防「返回失败但账本双写/新建行」）。
- seq 241 最终裁决修订：try-import 统一到专用模块 runtime_db/operation_store_selector.py
  （模式解析与 store 构造/缓存分开）；_operation_store_for 顺序改为「先严格校验
  ExecutionMode → 生产 selector 存在则无论 MANAGED/LOCAL 都经它 → 仅红测阶段才
  回落 local」（不先短路 LOCAL，否则生产 selector 的 LOCAL 写错会假绿）；
  b2 补非法模式值用例断言 fail-closed 抛错（覆盖生产 selector 自身校验）。

现状（红测前，每一条都会失败/会失败部分）：
- 双份 operation：ToolExecutor 内部 execute_tool_operation 落 local_storage 幂等账本
  （operation_store=agent.local_store，registry.py:760），外层 authority_fence
  open/close 又在 runtime.db 记一份（无幂等/无锁）→ c 断言 local 0 条必失败。
- authority_context 缺 run 链时懒建（record_run_creation）→ a2 期望
  TOOL_AUTHORITY_CONTEXT_MISSING + 不建链，现在 handler=1 + 建链。
- authority_context 无库 → None 放行 → a1/l 期望 handler=0 现在 handler=1。
- open_authority_operation 空 agent_run_id/attempt_id → ("", "") 放行 → a3 必失败。
- close_authority_operation 吞 settle 异常（settle_operation 无 fence 参数）→
  d 期望非 ok + UNKNOWN 语义现在仍 ok。
- runtime.db 无 effect_key 幂等（create_tool_operation 每次 INSERT 新行）→
  e2 期望同 effect_key 只落一行现在两行。
- coordinator 未接资源锁（acquire_locks 生产零调用）→ h 期望第二操作被锁拒现在两次都执行。
- 外层 gate 不分 read-only：read-only 工具 MANAGED 下也被 open → l 期望 operation 行=0 现在 1 行。
- 审批悬挂（ask）在 decide 层直接拒绝、gate 在其后 → k 现状已绿（防回归）。
- f 原语层（settle current-pointer 校验）G4 补已实现 → f 现状绿（防回归）。

矩阵映射：
a = test_a1/test_a2/test_a3（MANAGED 缺每种权威字段 → 统一 TOOL_AUTHORITY_CONTEXT_MISSING）
b = test_b_explicit_local_keeps_local_only
c = test_c_managed_mutating_writes_only_runtime_db
d = test_d_settle_crash_marks_unknown
e = test_e_same_effect_key_replays_persisted_result（LocalStore 绿）+ test_e2_managed_effect_key_single_row（红）
f = test_f_takeover_prevents_stale_settle
g = test_g_approval_deny_blocks_handler
h = test_h_same_write_root_locked_across_tools
i = 解析器层正文命令/未闭合工具块由 F10/G5 既有测试覆盖；"合法 native 只执行一次" = e/e2
j = 普通中文真实 LLM（真机验证，非单测）
k = test_k_approval_pending_holds_no_lease_or_lock
l = test_l_managed_readonly_requires_authority
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
)
from agent_py_agent.agent.contracts.idempotency import operation_idempotency_key
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.runtime_db.execution_mode import ExecutionMode
from agent_py_agent.agent.runtime_db.repository import (
    RuntimeConflictError,
    RuntimeRepository,
)
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolProtocolSnapshot,
    ToolResult,
)
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)

# 目标生产 seam（B 切片实现落地点，seq 238/241 定版）：专用模块
# runtime_db/operation_store_selector.py 提供 select_operation_store(agent)：
# MANAGED → ManagedOperationStore(agent.subagents.runtime_db)；LOCAL_UNMANAGED →
# agent.local_store。agent 同时拥有 local_store 与 subagents.runtime_db
# （SubAgentManager 没有 local_store，所以签名为 agent 而非 manager）；模式
# 解析（execution_mode.py）与 store 构造/缓存（operation_store_selector.py）
# 分开，不揉回一个模块。
# 红测阶段未实现 → _operation_store_for 走测试态回落 local（保持双写红点）；
# test_b2 显式钉住「MANAGED 必须解析到 runtime.db adapter」+ 非法模式抛错；
# test_b3 钉住生产装配口 _build_tool_registry（core.py:846）必须经 selector。
# 实现后 try-import 自动生效：无论 MANAGED/LOCAL 都先经生产 selector（seq 241：
# 不先短路 LOCAL 分支，否则生产 selector 的 LOCAL 写错会假绿）。
try:
    from agent_py_agent.agent.runtime_db.operation_store_selector import (  # type: ignore[attr-defined]
        select_operation_store as _production_select_operation_store,
    )
except ImportError:
    _production_select_operation_store = None

# ------------------------------------------------------------------- helpers


def _repo(tmp_path) -> RuntimeRepository:
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "local.db", enable_fts=False)


def _direct_register_chain(
    repo: RuntimeRepository,
    run_id: str,
    *,
    task_id: str = "",
    owner_id: str = "owner-a",
) -> tuple[str, str]:
    """不经 authority_context 直接建权威链（record_run_creation），返回 (agent_run_id, attempt_id)。

    契约修订（seq 231）：测试不得用 authority_context 的懒建路径预建链；
    a2 故意不建链，其余测试用本 fixture 显式登记 run。
    """
    record = repo.record_run_creation(
        owner_id=owner_id,
        goal=f"gate goal {run_id}",
        conversation_task_id=task_id or f"task-{run_id}",
        thread_id=f"thread-{run_id}",
        run_id=run_id,
        role="assistant",
    )
    return str(record["agent_run_id"]), str(record["attempt_id"])


def _agent(
    repo: RuntimeRepository | None,
    tools: object,
    store: LocalStore | None = None,
    root: Path | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.MANAGED,
) -> SimpleNamespace:
    """agent 桩。权威字段挂 subagents（对齐生产 SubAgentManager 形态）。

    - subagents.runtime_db：权威库（None = 无权威库，但 subagents 恒存在，
      对齐生产 manager 构造形态——authority_context 读取路径
      authority_fence.py:55）。
    - subagents.execution_mode：执行模式枚举（manager.py:61 + _attach_runtime_db
      resolve，execution_mode.py:23）。
    实现后主链按 subagents.execution_mode 选 OperationStore；现状主链不读 →
    现状行为不变（红点保持）。
    """
    return SimpleNamespace(
        tools=tools,
        subagents=SimpleNamespace(runtime_db=repo, execution_mode=execution_mode),
        local_store=store,
        root=root,
    )


def _manager_mode(agent: SimpleNamespace) -> ExecutionMode:
    """读取 agent 桩的权威执行模式（生产读取点 = subagents.execution_mode）。

    seq 238 问题 2：遇非 ExecutionMode 枚举必须抛错（fail-closed）——字符串
    拼错被测试悄悄吞掉 = 不是 fail-closed，会掩盖实现接线错误。
    """
    mode = getattr(agent.subagents, "execution_mode", None)
    if not isinstance(mode, ExecutionMode):
        raise TypeError(
            f"execution_mode 必须是 ExecutionMode 枚举成员，got {mode!r}"
        )
    return mode


def _operation_store_for(agent: SimpleNamespace) -> object:
    """目标 store 选择 seam（seq 241 定版顺序，签名 select_operation_store(agent)）。

    顺序必须：先严格校验 ExecutionMode（非枚举 → 抛错）→ 生产 selector 存在时
    无论 MANAGED/LOCAL 都调用它 → 仅红测阶段 selector 未实现才回落 local 保持
    红点。绝不能先短路 LOCAL 分支：否则生产 selector 的 LOCAL 分支写错时
    b/b2 会假绿，LOCAL 路径接错位置测不出来。
    """
    mode = _manager_mode(agent)  # 非枚举 → TypeError（fail-closed，seq 238 问题 2）
    if _production_select_operation_store is not None:
        return _production_select_operation_store(agent)
    # 红测阶段：selector 未实现 → 测试态回落（MANAGED/LOCAL 均落 local 保持
    # c/e2/h 双写等红点），由 test_b2/test_b3 钉住实现后必须解析到 runtime.db。
    _ = mode
    return getattr(agent, "local_store", None)


def _params(run_id: str = "run-gate", task_id: str = "task-gate", snapshot: object = None) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        tool_runtime_snapshot=snapshot,
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary={},
        task_attributes={},
        context_scope="default",
        request_id="request-1",
        run_id=run_id,
        task_id=task_id,
        run_scope=None,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=ToolProtocolSnapshot(
            run_id,
            "text",
            ProviderToolCapability(
                provider="test",
                endpoint="local://test",
                model="fake",
                stream=False,
                native_supported=False,
                evidence="managed_gate_fixture",
            ),
        ),
    )


def _gate_call(
    *,
    run_id: str = "run-gate",
    tool_name: str = "write_file",
    operation_id: str = "op-gate-1",
    snapshot: object = None,
    arguments: dict[str, object] | None = None,
    attempt_id: str = "attempt-gate-1",
) -> ToolCall:
    if snapshot is not None:
        runtime = snapshot.runtime(tool_name)
        schema_hash = runtime.model_spec.schema_hash
    else:
        schema_hash = "sha256:" + "0" * 64
    call_arguments = arguments if arguments is not None else {
        "path": "out/report.md",
        "value": 1,
    }
    return ToolCall(
        call_id="call-gate-1",
        tool_name=tool_name,
        arguments=call_arguments,
        source_protocol="native",
        schema_hash=schema_hash,
        run_id=run_id,
        turn_id="turn-1",
        attempt_id=attempt_id,
        operation_id=operation_id,
        idempotency_key=operation_idempotency_key(
            attempt_id, operation_id
        ),
    )


def _traced_call(
    agent: SimpleNamespace,
    params: ToolLoopExecuteParams,
    call: ToolCall,
):
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )
    return execute_traced_tool_call(request)


def _registry(
    root: Path,
    store: object,
    tools: list[BaseTool],
    *,
    owner_id: str = "owner-a",
) -> ToolRegistry:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            operation_store=store,
            operation_store_required=True,
            operation_owner_id=owner_id,
        )
    )
    for tool in tools:
        registry.register(tool)
    return registry


def _managed_chain(
    repo: RuntimeRepository,
    store: LocalStore,
    tools: list[BaseTool],
    root: Path,
):
    """MANAGED 主链装配：agent 桩 + registry（executor 内部账本 store 经
    _operation_store_for 从 agent 解析，测试不自己决定 store 权威来源）。"""
    agent = _agent(repo=repo, tools=None, store=store, root=root)
    agent.tools = _registry(root, _operation_store_for(agent), tools)
    return agent


def _tool_registry_agent_stub(
    repo: RuntimeRepository | None,
    store: LocalStore,
    root: Path,
) -> SimpleNamespace:
    """生产装配口 caller 测试用的 agent 桩：覆盖 _build_tool_registry
    （core.py:786）的全部读取面（seq 238 问题 3：直接证明生产装配函数实际
    使用 selector，不是测试自己造 registry 的旁路）。"""
    return SimpleNamespace(
        effective_workspace_root=root,
        effective_workspace_roots=[root],
        owner_policy=SimpleNamespace(
            max_disk_mb=0, load_errors=[], disabled_tools=[]
        ),
        home_paths=SimpleNamespace(
            owner_id="owner-test",
            owner_provider="local",
            owner_kind="main",
            owner_home_dir=str(root),
        ),
        channel_registry=SimpleNamespace(),
        current_skill_snapshot=lambda: None,
        memory=SimpleNamespace(runtime_snapshot=lambda: None),
        persona_repository=SimpleNamespace(runtime_snapshot=lambda: None),
        scheduler_service=SimpleNamespace(runtime_snapshot=lambda: None),
        runtime_guard_policy=None,
        local_store=store,
        _current_run_params=None,
        subagents=SimpleNamespace(runtime_db=repo, execution_mode=ExecutionMode.MANAGED),
    )


class _PathWriteTool(BaseTool):
    """mutating 工具：声明 path 资源参数（h 红测的资源锁 scope 来源）。"""

    def __init__(
        self,
        name: str,
        *,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        policy: ToolRuntimePolicy | None = None,
    ):
        self.calls = 0
        self.started = started
        self.release = release
        self.model_spec = make_test_model_spec(
            name,
            description="write one side effect at path",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "value": {"type": "integer"},
                },
                "required": ["path", "value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = policy or make_test_runtime_policy(
            "mutating",
            resource_parameters=("path",),
        )

    def execute(self, params):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=10)
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            f"completed:{params['value']}",
        )


class _ReadOnlyTool(BaseTool):
    def __init__(self, name: str = "peek_file"):
        self.calls = 0
        self.model_spec = make_test_model_spec(
            name,
            description="read one file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            "read_only",
            resource_parameters=("path",),
        )

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, f"read:{params['path']}")


class _DangerousTool(_PathWriteTool):
    """dangerous 工具：无 approval binding 时 policy.decide 必须 ask 拒绝。"""

    def __init__(self, name: str = "danger_write"):
        super().__init__(name, policy=make_test_runtime_policy("dangerous"))


def _lock_rows(repo: RuntimeRepository) -> list:
    with repo._runtime_connection() as conn:
        return conn.execute("SELECT * FROM resource_locks").fetchall()


def _mutation_rows(repo: RuntimeRepository) -> list:
    with repo._runtime_connection() as conn:
        return conn.execute("SELECT * FROM resource_mutations").fetchall()


# ============================================================== a：缺权威上下文
# 期望（实现后）：MANAGED（agent.subagents.execution_mode=ExecutionMode.MANAGED）
# 缺 repo/run/attempt 任一 → 统一 TOOL_AUTHORITY_CONTEXT_MISSING + handler=0 + 不懒建链。
# 现状：缺 repo → None 放行 handler=1；缺 run → 懒建链 handler=1；attempt 空 →
# open ("","") 放行 handler=1。


def test_a1_managed_without_repo_blocks_handler(tmp_path):
    """a-1：显式 MANAGED 但无权威库（缺 repo）→ handler=0 + TOOL_AUTHORITY_CONTEXT_MISSING。

    seq 238 问题 1：真实 ToolExecutor 主链（ToolRegistry + _PathWriteTool +
    runtime_snapshot + _traced_call），不模拟 execute_tool。
    """
    repo = None
    store = _store(tmp_path)
    tool = _PathWriteTool("managed_write_a1")
    agent = _managed_chain(repo, store, [tool], tmp_path)
    assert isinstance(agent.tools, ToolRegistry)  # 证据：真实注册表主链
    snap = runtime_snapshot_for_tools({"managed_write_a1": tool}, run_id="run-a1")
    params = _params(run_id="run-a1", task_id="task-a1", snapshot=snap)
    call = _gate_call(run_id="run-a1", tool_name="managed_write_a1", snapshot=snap)

    execution = _traced_call(agent, params, call)

    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_AUTHORITY_CONTEXT_MISSING"
    assert not execution.result.handler_executed
    assert tool.calls == 0


def test_a2_managed_without_run_chain_blocks_handler(tmp_path):
    """a-2：显式 MANAGED 有 repo 但 run 未登记 → handler=0 + 不懒建链（无残留）。"""
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    tool = _PathWriteTool("managed_write_a2")
    agent = _managed_chain(repo, store, [tool], tmp_path)
    assert isinstance(agent.tools, ToolRegistry)  # 证据：真实注册表主链
    snap = runtime_snapshot_for_tools({"managed_write_a2": tool}, run_id="run-a2")
    params = _params(run_id="run-a2", task_id="task-a2", snapshot=snap)
    call = _gate_call(run_id="run-a2", tool_name="managed_write_a2", snapshot=snap)

    execution = _traced_call(agent, params, call)

    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_AUTHORITY_CONTEXT_MISSING"
    assert not execution.result.handler_executed
    assert tool.calls == 0
    assert repo.agent_run_for_run_id("run-a2") is None  # 不就地登记


def test_a3_managed_empty_attempt_blocks_handler(tmp_path):
    """a-3：run 链存在但 current attempt 缺失 → handler=0（open 空 id 不放行）。"""
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-a3", task_id="task-a3")
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET current_attempt_id = '' WHERE run_id = ?",
            ("run-a3",),
        )
        conn.commit()  # _runtime_connection 退出只 close 不提交（schema.py:461），
        # 不 commit 则 UPDATE 回滚、「空 current attempt」场景从未生效
    tool = _PathWriteTool("managed_write_a3")
    agent = _managed_chain(repo, store, [tool], tmp_path)
    assert isinstance(agent.tools, ToolRegistry)  # 证据：真实注册表主链
    snap = runtime_snapshot_for_tools({"managed_write_a3": tool}, run_id="run-a3")
    params = _params(run_id="run-a3", task_id="task-a3", snapshot=snap)
    call = _gate_call(
        run_id="run-a3", tool_name="managed_write_a3", snapshot=snap,
        attempt_id=attempt_id,  # 真实返回值：strict fence 后伪造 attempt 会被拒
    )

    execution = _traced_call(agent, params, call)

    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_AUTHORITY_CONTEXT_MISSING"
    assert not execution.result.handler_executed
    assert tool.calls == 0


# ============================================================== b：LOCAL 只落一份
# 期望（实现后）：显式 LOCAL_UNMANAGED 主链（agent 桩 + registry + seam 解析
# store）→ 只落 local 账本一份，runtime.db 零行（无权威库，LOCAL 不放行双写）。
# 现状（绿，防回归）：主链 authority_context 读到 subagents.runtime_db=None →
# 无外层行；executor 内部 execute_tool_operation 落 LocalStore 恰一条。


def test_b_explicit_local_keeps_local_only(tmp_path):
    store = _store(tmp_path)
    tool = _PathWriteTool("local_write")
    registry = _registry(tmp_path, store, [tool])
    agent = _agent(
        repo=None,
        tools=registry,
        store=store,
        root=tmp_path,
        execution_mode=ExecutionMode.LOCAL_UNMANAGED,
    )
    snap = runtime_snapshot_for_tools({"local_write": tool}, run_id="run-b")
    params = _params(run_id="run-b", task_id="task-b", snapshot=snap)
    call = _gate_call(
        run_id="run-b",
        tool_name="local_write",
        operation_id="tool_call:attempt-b:call-b",
        snapshot=snap,
    )

    execution = _traced_call(agent, params, call)

    assert execution.result.ok
    assert tool.calls == 1
    rows = store.list_tool_operations(run_id="run-b")
    assert len(rows) == 1
    assert rows[0].status == "succeeded"  # LocalStore 原样返回小写 status


def test_b2_store_selector_seam(tmp_path):
    """store 选择 seam：MANAGED manager → runtime.db adapter；LOCAL manager →
    LocalStore；同一执行只选一次（幂等）。

    现状（红）：生产 seam select_operation_store 未实现，测试态 MANAGED 落
    local → 断言「MANAGED 必须解析到 runtime.db adapter」失败。
    实现后：经生产 seam → ManagedOperationStore 实例（非 local）→ 绿。
    """
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    managed_agent = _agent(repo=repo, tools=object(), store=store, root=tmp_path)
    local_agent = _agent(
        repo=None,
        tools=object(),
        store=store,
        root=tmp_path,
        execution_mode=ExecutionMode.LOCAL_UNMANAGED,
    )

    managed_store = _operation_store_for(managed_agent)
    # 红：MANAGED 必须解析到 runtime.db adapter，不得是 local 账本（不双写源头）。
    assert managed_store is not store
    # 同一执行只选一次：同一 manager 重复解析返回同一实例（选择缓存/幂等）。
    assert _operation_store_for(managed_agent) is managed_store

    assert _operation_store_for(local_agent) is store  # LOCAL → LocalStore

    # fail-closed（seq 241）：非法模式值（非枚举）→ selector 必须抛错，
    # 不得静默默认 MANAGED（覆盖生产 selector 自身的校验，不只是测试 helper）。
    bad_agent = _agent(
        repo=None,
        tools=object(),
        store=store,
        root=tmp_path,
        execution_mode="MANAGED",  # type: ignore[arg-type]  # 故意传非法值
    )
    with pytest.raises(TypeError):
        _operation_store_for(bad_agent)


def test_b3_production_assembly_uses_selector(tmp_path):
    """生产装配口 caller 测试（seq 238 问题 3）：agent/core.py:_build_tool_registry
    ——SimpleAgent 构造 ToolRegistry 的唯一装配入口——必须经 selector 选 store，
    且 ToolRegistry.operation_store 就是同一选中实例。

    现状（红）：core.py:846 写死 operation_store=agent.local_store →
    registry.operation_store is store，selector 从未被生产装配调用。
    实现后：_build_tool_registry 调 select_operation_store(agent) → MANAGED 解析
    到 runtime.db adapter（非 local）→ 两断言转绿。
    """
    from agent_py_agent.agent.core import _build_tool_registry
    from agent_py_agent.agent.settings.config import AgentConfig

    repo = _repo(tmp_path)
    store = _store(tmp_path)
    agent = _tool_registry_agent_stub(repo, store, tmp_path)

    registry = _build_tool_registry(agent, AgentConfig())

    # 证据：唯一装配口实际使用 selector（MANAGED → runtime.db adapter，绝不 local）。
    assert registry.operation_store is not store
    if _production_select_operation_store is not None:
        # 实现后：装配口选中的实例 = selector 实例（同一选中实例，不另起炉灶）。
        assert registry.operation_store is _production_select_operation_store(agent)


# ============================================================== c：双账本合一
# 期望（实现后）：MANAGED mutating 成功 → settle 后 runtime.db 恰一条 SUCCEEDED
# + local 账本零条（绝不双写）。
# 现状：双写 —— runtime.db 一条（fence）+ local 一条（executor 内部）。


def test_c_managed_mutating_writes_only_runtime_db(tmp_path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-c", task_id="task-c")
    tool = _PathWriteTool("managed_write")
    agent = _managed_chain(repo, store, [tool], tmp_path)  # 显式 MANAGED（默认）
    params = _params(
        run_id="run-c",
        task_id="task-c",
        snapshot=runtime_snapshot_for_tools({"managed_write": tool}, run_id="run-c"),
    )
    call = _gate_call(
        run_id="run-c",
        tool_name="managed_write",
        operation_id="tool_call:attempt-c:call-c",
        snapshot=runtime_snapshot_for_tools({"managed_write": tool}, run_id="run-c"),
        attempt_id=attempt_id,  # 真实返回值：严格 fence 后伪造 attempt 会被拒
    )

    execution = _traced_call(agent, params, call)

    assert execution.result.ok  # settle 后才 success
    assert tool.calls == 1
    row = repo.agent_run_for_run_id("run-c")
    assert row is not None
    current_attempt_id = row["current_attempt_id"]
    assert current_attempt_id == attempt_id
    ops = repo.operations_for_attempt(attempt_id)
    assert len(ops) == 1  # runtime.db 恰一条
    assert ops[0]["status"] == "SUCCEEDED"  # settle 完成后才是终态
    assert ops[0]["operation_type"] == "managed_write"
    local_rows = store.list_tool_operations(run_id="run-c")
    assert local_rows == []  # 绝不双写


# ============================================================== d：settle 崩溃
# 期望（实现后）：handler 已执行但 settle 崩溃 → 非 ok（UNKNOWN 语义）+
# handler_executed=True；tool_operations.status == "UNKNOWN"（终态）；可写资源
# mutation 行 state == "DIRTY" + dirty_reason 非空（DIRTY 属 mutation 表，
# 不是 operation 终态）；同 effect_key 重放不重跑 handler。
# 现状：close_authority_operation 吞异常 → 仍 ok；行滞留 EXECUTING；
# mutation 表零行；重放再跑 handler。


def test_d_settle_crash_marks_unknown(tmp_path, monkeypatch):
    """d：真实 MANAGED store 上注入 settle 崩溃（seq 238 问题 1：不再用桩模拟）。

    现状红点：close_authority_operation 吞 sqlite3.Error → 结果仍 ok、行滞留
    EXECUTING、mutation 零行、executor 内部 local 账本 1 行、重放重跑 handler。
    注（seq 241 实现要求）：当前经 repo.settle_operation 注入复现旧外层路径；
    ManagedOperationStore adapter 建成后，注入点迁移到唯一 OperationStore 边界
    （store.finish/settle），不得长期绑死 Repository 方法。
    """
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-d", task_id="task-d")
    tool = _PathWriteTool("managed_write_d")
    agent = _managed_chain(repo, store, [tool], tmp_path)
    assert isinstance(agent.tools, ToolRegistry)  # 证据：真实注册表主链
    snap = runtime_snapshot_for_tools({"managed_write_d": tool}, run_id="run-d")
    params = _params(run_id="run-d", task_id="task-d", snapshot=snap)
    call = _gate_call(
        run_id="run-d",
        tool_name="managed_write_d",
        operation_id="tool_call:attempt-d:call-d",
        snapshot=snap,
        attempt_id=attempt_id,  # 真实返回值
    )

    def _crash_settle(*args, **kwargs):
        raise sqlite3.OperationalError("simulated settle crash")

    # 注入点 = 唯一 OperationStore 边界（_settle_operation_via_repo，seq 241 d 迁移：
    # 不得长期绑死 Repository 方法）。settle 是 store 内部原语，外层只能经 store
    # 观察，故打模块级 settle 原语而非 repo.settle_operation。
    import agent_py_agent.agent.runtime_db.managed_operation_store as _mos

    monkeypatch.setattr(_mos, "_settle_operation_via_repo", _crash_settle)
    execution = _traced_call(agent, params, call)

    assert tool.calls == 1  # handler 已执行
    assert execution.result.handler_executed
    assert not execution.result.ok  # settle 失败必须如实反映，不能吞成成功
    assert "UNKNOWN" in (execution.result.error_code or "")
    ops = repo.operations_for_attempt(attempt_id)
    assert len(ops) == 1
    # 两个正交状态：operation 终态 UNKNOWN（不可能是 DIRTY）；DIRTY 只属于
    # mutation 表。EXECUTING 滞留 = 崩溃未被标记（现状红）。
    assert ops[0]["status"] == "UNKNOWN"
    # 可写资源 mutation 如实标 DIRTY + 非空原因（现状生产零写 mutation → 红）。
    mutations = _mutation_rows(repo)
    assert len(mutations) == 1
    assert mutations[0]["state"] == "DIRTY"
    assert mutations[0]["dirty_reason"]

    # 重放：同 effect_key 不得重跑 handler（先 reconcile UNKNOWN，不重放副作用）。
    second = _traced_call(agent, params, call)
    assert tool.calls == 1
    assert not second.result.ok  # 仍如实失败，不吞
    # 紧断言（seq 238）：重放后 runtime.db 仍唯一 + LocalStore=0
    # （防「返回失败但账本又双写/新建一行」）。
    ops_after = repo.operations_for_attempt(attempt_id)
    assert len(ops_after) == 1
    assert ops_after[0]["status"] == "UNKNOWN"
    local_rows = store.list_tool_operations(run_id="run-d")
    assert local_rows == []  # 绝不双写


# ============================================================== e：幂等重放
# 期望/现状（LocalStore，绿）：同 effect_key（operation_id+attempt）成功重放返回
# 持久结果，handler 不重跑（覆盖 i 后半「合法 native 只执行一次」）。
# e2（红）：MANAGED 主链同 effect_key 在 runtime.db 权威账本也只落一行
# （现状 create_tool_operation 无幂等键 → 每次新 INSERT → 两行）。


def test_e_same_effect_key_replays_persisted_result(tmp_path):
    store = _store(tmp_path)
    tool = _PathWriteTool("replay_write")
    operation_id = "tool_call:attempt-e:call-e"
    idempotency_key = operation_idempotency_key("attempt-e", operation_id)

    first = execute_canonical_test_call(
        tmp_path,
        tools={"replay_write": tool},
        tool_name="replay_write",
        arguments={"path": "out/x", "value": 1},
        run_id="run-e",
        attempt_id="attempt-e",
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        operation_store=store,
        operation_store_required=True,
        write_boundary={},
    )
    second = execute_canonical_test_call(
        tmp_path,
        tools={"replay_write": tool},
        tool_name="replay_write",
        arguments={"path": "out/x", "value": 1},
        run_id="run-e",
        attempt_id="attempt-e",
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        operation_store=store,
        operation_store_required=True,
        write_boundary={},
    )

    assert first.result.ok and second.result.ok
    assert second.result.output == first.result.output
    assert tool.calls == 1  # 重放不重跑 handler


def test_e2_managed_effect_key_single_row(tmp_path):
    """e-2：MANAGED 主链同 effect_key（operation_id+attempt）→ runtime.db 权威账本
    只落一行（effect_key 幂等），不因每次调用新建操作行。"""
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-e2", task_id="task-e2")
    tool = _PathWriteTool("managed_replay")
    agent = _managed_chain(repo, store, [tool], tmp_path)  # 显式 MANAGED（默认）
    params = _params(
        run_id="run-e2",
        task_id="task-e2",
        snapshot=runtime_snapshot_for_tools({"managed_replay": tool}, run_id="run-e2"),
    )
    call = _gate_call(
        run_id="run-e2",
        tool_name="managed_replay",
        operation_id="tool_call:attempt-e2:call-e2",
        snapshot=runtime_snapshot_for_tools({"managed_replay": tool}, run_id="run-e2"),
        attempt_id=attempt_id,
    )

    first = _traced_call(agent, params, call)
    second = _traced_call(agent, params, call)

    assert first.result.ok and second.result.ok
    assert tool.calls == 1  # 重放不重跑 handler
    ops = repo.operations_for_attempt(attempt_id)
    assert len(ops) == 1  # 同 effect_key 只落一行（现状 create 无幂等 → 两行，红）
    assert ops[0]["status"] == "SUCCEEDED"


# ============================================================== f：takeover 后不可 settle
# 期望/现状（原语层 G4 补已实现，绿防回归）：settle 的 current-pointer 原子校验
# 在 UPDATE WHERE 里（operations.py:561 docstring）→ 旧 attempt 的 settle 被拒
# （行保持 EXECUTING 或由 takeover 转 UNKNOWN），同 AgentRun 的新 attempt
# （非兄弟 AgentRun）不受影响。主链 close 侧的重校验接线由 f2/d/c 覆盖。


def test_f_takeover_prevents_stale_settle(tmp_path):
    repo = _repo(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(repo, "run-f", task_id="task-f")

    operation_id = repo.create_tool_operation(
        agent_run_id=agent_run_id, attempt_id=attempt_id, operation_type="write_file"
    )["operation_id"]
    repo.mark_operation_executing(
        operation_id,
        agent_run_id=agent_run_id,
        attempt_id=attempt_id,
        tool_operation_generation=repo.get_operation(operation_id)["tool_operation_generation"],
    )
    assert repo.get_operation(operation_id)["status"] == "EXECUTING"

    repo.create_attempt(agent_run_id)  # takeover：推进 current pointer
    # settle 必须重校验 current pointer：G4 补实现为 fail-closed（rowcount=0 → raise）。
    rejected = False
    try:
        repo.settle_operation(operation_id, outcome="SUCCEEDED", details={"source": "tool_loop"})
    except RuntimeConflictError:
        rejected = True
    assert rejected  # 旧 attempt 的 settle 被拒
    assert repo.get_operation(operation_id)["status"] != "SUCCEEDED"

    # 同 AgentRun 新 attempt（takeover 产物）不受影响：可正常 begin/settle。
    fresh_attempt_id = repo.agent_run_for_run_id("run-f")["current_attempt_id"]
    fresh_op = repo.create_tool_operation(
        agent_run_id=agent_run_id, attempt_id=fresh_attempt_id, operation_type="write_file"
    )["operation_id"]
    repo.mark_operation_executing(
        fresh_op,
        agent_run_id=agent_run_id,
        attempt_id=fresh_attempt_id,
        tool_operation_generation=repo.get_operation(fresh_op)["tool_operation_generation"],
    )
    repo.settle_operation(fresh_op, outcome="SUCCEEDED", details={"source": "tool_loop"})
    assert repo.get_operation(fresh_op)["status"] == "SUCCEEDED"


def test_f2_takeover_mid_execution_blocks_stale_success(tmp_path):
    """f 主链集成（seq 235 问题 6）：真实 ToolExecutor 屏障——旧 handler 运行中
    takeover → 旧 settle 被拒 → 旧执行不得向模型报 success；真正 sibling
    AgentRun（run-f2b）同时正常完成，不被旧 attempt 的拒绝波及。

    现状（红）：close_authority_operation 吞 RuntimeConflictError → 旧执行仍
    向模型报 ok；实现后 settle 被拒必须如实反映（非 ok）。
    """
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    agent_run_id, attempt_id = _direct_register_chain(repo, "run-f2a", task_id="task-f2a")
    _direct_register_chain(repo, "run-f2b", task_id="task-f2b")
    started = threading.Event()
    release = threading.Event()
    tool = _PathWriteTool("takeover_write", started=started, release=release)
    agent = _managed_chain(repo, store, [tool], tmp_path)
    snap_a = runtime_snapshot_for_tools({"takeover_write": tool}, run_id="run-f2a")
    params_a = _params(run_id="run-f2a", task_id="task-f2a", snapshot=snap_a)
    call_a = _gate_call(
        run_id="run-f2a",
        tool_name="takeover_write",
        operation_id="op-f2a-1",
        snapshot=snap_a,
        attempt_id=attempt_id,
    )
    outcome: dict[str, object] = {}

    def run_old_attempt():
        outcome["execution"] = _traced_call(agent, params_a, call_a)

    thread_old = threading.Thread(target=run_old_attempt, name="f2-old-attempt")
    try:
        thread_old.start()
        assert started.wait(timeout=10)  # 旧 handler 已启动、未 settle
        repo.create_attempt(agent_run_id)  # takeover：推进 current pointer
        release.set()
        thread_old.join(timeout=10)
    finally:
        release.set()  # 屏障：无论结果如何都放行旧 handler，防线程悬挂污染
        thread_old.join(timeout=10)

    assert not thread_old.is_alive()
    stale = outcome["execution"]
    assert not stale.result.ok  # 红：takeover 后旧 settle 被拒，不得向模型报 success
    # 紧断言（seq 238）：旧 attempt 的账本状态如实——operation 终态 UNKNOWN
    # （现状 close 吞 RuntimeConflictError → 滞留 EXECUTING，红）、mutation 标
    # DIRTY（现状零行，红）、不得 publish（防旧执行向模型报 success 后仍发布）。
    ops = repo.operations_for_attempt(attempt_id)
    assert len(ops) == 1
    assert ops[0]["status"] == "UNKNOWN"
    mutations = _mutation_rows(repo)
    assert len(mutations) == 1
    assert mutations[0]["state"] == "DIRTY"
    assert mutations[0]["dirty_reason"]
    assert repo.publishes_for_attempt(attempt_id) == []  # 旧 attempt 不得 publish

    # sibling AgentRun 不受影响：run-f2b 正常完成。
    snap_b = runtime_snapshot_for_tools({"takeover_write": tool}, run_id="run-f2b")
    params_b = _params(run_id="run-f2b", task_id="task-f2b", snapshot=snap_b)
    attempt_b = repo.agent_run_for_run_id("run-f2b")["current_attempt_id"]
    call_b = _gate_call(
        run_id="run-f2b",
        tool_name="takeover_write",
        operation_id="op-f2b-1",
        snapshot=snap_b,
        attempt_id=attempt_b,
    )
    fresh = _traced_call(agent, params_b, call_b)
    assert fresh.result.ok  # sibling 正常完成（现状绿，防回归）


# ============================================================== g：审批/参数拒绝
# 期望/现状：approval deny → handler=0（防回归，现状绿）。


def test_g_approval_deny_blocks_handler(tmp_path):
    tool = _DangerousTool()
    execution = execute_canonical_test_call(
        tmp_path,
        tools={"danger_write": tool},
        tool_name="danger_write",
        arguments={"path": "out/x", "value": 1},
        run_id="run-g",
        attempt_id="attempt-g",
        operation_id="tool_call:attempt-g:call-g",
        idempotency_key=operation_idempotency_key("attempt-g", "tool_call:attempt-g:call-g"),
        operation_store=_store(tmp_path),
        operation_store_required=True,
        write_boundary={},
    )

    assert not execution.result.ok
    assert tool.calls == 0
    assert not execution.result.handler_executed


# ============================================================== h：资源锁
# 期望（实现后）：锁冲突策略 = fail-fast（对齐 acquire_locks：单事务逐条
# INSERT，UNIQUE(canonical_scope) 冲突 → 事务回滚 → RuntimeConflictError →
# 第二操作 handler=0，不等待串行）。同一物理写根即使 tool name / 相对路径
# 别名 / symlink 不同也归一化到同一 canonical scope → 被拒；不同物理根可
# 并行。资源 scope 由 resource_parameters + args 解析（coordinator 接线）。
# 现状：coordinator 无锁接线 → 两次都执行（红）。


def test_h_same_write_root_locked_across_tools(tmp_path):
    """同物理写根 fail-fast：原始串 out/x vs 别名 out/../out/x，不同工具名、
    不同 run/attempt → 第二操作被资源锁拒绝 handler=0；第一操作正常完成。"""
    root = tmp_path / "h1"
    root.mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "out" / "x").write_text("x", encoding="utf-8")
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _direct_register_chain(repo, "run-h1", task_id="task-h1")
    _direct_register_chain(repo, "run-h2", task_id="task-h2")
    started = threading.Event()
    release = threading.Event()
    write_a = _PathWriteTool("write_a", started=started, release=release)
    write_b = _PathWriteTool("write_b")
    agent = _managed_chain(repo, store, [write_a, write_b], root)
    snap_a = runtime_snapshot_for_tools(
        {"write_a": write_a, "write_b": write_b}, run_id="run-h1"
    )
    snap_b = runtime_snapshot_for_tools(
        {"write_a": write_a, "write_b": write_b}, run_id="run-h2"
    )
    params_a = _params(run_id="run-h1", task_id="task-h1", snapshot=snap_a)
    params_b = _params(run_id="run-h2", task_id="task-h2", snapshot=snap_b)
    attempt_a = repo.agent_run_for_run_id("run-h1")["current_attempt_id"]
    attempt_b = repo.agent_run_for_run_id("run-h2")["current_attempt_id"]
    call_a = _gate_call(
        run_id="run-h1",
        tool_name="write_a",
        operation_id="tool_call:attempt-h1:call-h1",
        snapshot=snap_a,
        arguments={"path": "out/x", "value": 1},
        attempt_id=attempt_a,
    )
    outcome_holder: dict[str, object] = {}

    def run_a():
        outcome_holder["a"] = _traced_call(agent, params_a, call_a)

    thread_a = threading.Thread(target=run_a, name="h-write-a")
    try:
        thread_a.start()
        assert started.wait(timeout=10)  # A handler 已启动、未 settle（锁应被持有）

        for alias in ("out/x", "out/../out/x"):
            # 同一物理写根（原始串 / ../ 别名归一化）→ fail-fast 拒绝。
            call_b = _gate_call(
                run_id="run-h2",
                tool_name="write_b",
                operation_id=f"tool_call:attempt-h2:call-h2:{alias.replace('/', '-')}",
                snapshot=snap_b,
                arguments={"path": alias, "value": 2},
                attempt_id=attempt_b,
            )
            b = _traced_call(agent, params_b, call_b)
            assert b.result.ok is False  # 同物理根并发必须被拒（现状无锁 → ok=True，红）
            assert write_b.calls == 0
            assert not b.result.handler_executed
    finally:
        release.set()  # 屏障：无论 B 结果如何都放行 A，防线程悬挂污染后续测试
        thread_a.join(timeout=10)

    assert not thread_a.is_alive()
    assert write_a.calls == 1  # A 正常完成


def test_h2_symlink_same_physical_root_locked(tmp_path):
    """symlink 指向同一物理根仍冲突（canonical physical root 归一化）。"""
    root = tmp_path / "h2"
    root.mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "out" / "x").write_text("x", encoding="utf-8")
    (root / "alias").mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(root / "out", root / "alias" / "linked")
    except OSError:
        pytest.skip("当前平台无 symlink 权限")  # type: ignore[name-defined]
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _direct_register_chain(repo, "run-h2-1", task_id="task-h2-1")
    _direct_register_chain(repo, "run-h2-2", task_id="task-h2-2")
    started = threading.Event()
    release = threading.Event()
    write_a = _PathWriteTool("write_a", started=started, release=release)
    write_b = _PathWriteTool("write_b")
    agent = _managed_chain(repo, store, [write_a, write_b], root)
    snap_a = runtime_snapshot_for_tools(
        {"write_a": write_a, "write_b": write_b}, run_id="run-h2-1"
    )
    snap_b = runtime_snapshot_for_tools(
        {"write_a": write_a, "write_b": write_b}, run_id="run-h2-2"
    )
    params_a = _params(run_id="run-h2-1", task_id="task-h2-1", snapshot=snap_a)
    params_b = _params(run_id="run-h2-2", task_id="task-h2-2", snapshot=snap_b)
    attempt_a = repo.agent_run_for_run_id("run-h2-1")["current_attempt_id"]
    attempt_b = repo.agent_run_for_run_id("run-h2-2")["current_attempt_id"]
    call_a = _gate_call(
        run_id="run-h2-1",
        tool_name="write_a",
        operation_id="tool_call:attempt-h2-1:call-h1",
        snapshot=snap_a,
        arguments={"path": "out/x", "value": 1},
        attempt_id=attempt_a,
    )
    outcome_holder: dict[str, object] = {}

    def run_a():
        outcome_holder["a"] = _traced_call(agent, params_a, call_a)

    thread_a = threading.Thread(target=run_a, name="h2-write-a")
    try:
        thread_a.start()
        assert started.wait(timeout=10)

        # B 经 symlink（alias/linked/x 指向 out/x）→ 同一物理根 → fail-fast 拒绝。
        call_b = _gate_call(
            run_id="run-h2-2",
            tool_name="write_b",
            operation_id="tool_call:attempt-h2-2:call-h2",
            snapshot=snap_b,
            arguments={"path": "alias/linked/x", "value": 2},
            attempt_id=attempt_b,
        )
        b = _traced_call(agent, params_b, call_b)
        assert b.result.ok is False  # symlink 归一化后同根必须被拒（现状无锁 → 红）
        assert write_b.calls == 0
    finally:
        release.set()
        thread_a.join(timeout=10)

    assert not thread_a.is_alive()
    assert write_a.calls == 1


def test_h3_different_physical_roots_run_in_parallel(tmp_path):
    """不同物理写根真并行（seq 238 问题 4）：双线程 + 双 started 事件——双方
    handler 都进入（同时在执行中）后才 release，证明不同 canonical scope 并行
    不被阻塞；若按顺序先后执行则无法证明「并行」（现状绿，防回归）。"""
    root = tmp_path / "h3"
    root.mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _direct_register_chain(repo, "run-h3-1", task_id="task-h3-1")
    _direct_register_chain(repo, "run-h3-2", task_id="task-h3-2")
    started_a, started_b = threading.Event(), threading.Event()
    release_a, release_b = threading.Event(), threading.Event()
    write_a = _PathWriteTool("write_a", started=started_a, release=release_a)
    write_b = _PathWriteTool("write_b", started=started_b, release=release_b)
    agent = _managed_chain(repo, store, [write_a, write_b], root)
    snap_a = runtime_snapshot_for_tools(
        {"write_a": write_a, "write_b": write_b}, run_id="run-h3-1"
    )
    snap_b = runtime_snapshot_for_tools(
        {"write_a": write_a, "write_b": write_b}, run_id="run-h3-2"
    )
    params_a = _params(run_id="run-h3-1", task_id="task-h3-1", snapshot=snap_a)
    params_b = _params(run_id="run-h3-2", task_id="task-h3-2", snapshot=snap_b)
    attempt_a = repo.agent_run_for_run_id("run-h3-1")["current_attempt_id"]
    attempt_b = repo.agent_run_for_run_id("run-h3-2")["current_attempt_id"]
    outcome_a: dict[str, object] = {}
    outcome_b: dict[str, object] = {}

    def run_a():
        outcome_a["execution"] = _traced_call(
            agent,
            params_a,
            _gate_call(
                run_id="run-h3-1",
                tool_name="write_a",
                operation_id="tool_call:attempt-h3-1:call-a",
                snapshot=snap_a,
                arguments={"path": "out/x", "value": 1},
                attempt_id=attempt_a,
            ),
        )

    def run_b():
        outcome_b["execution"] = _traced_call(
            agent,
            params_b,
            _gate_call(
                run_id="run-h3-2",
                tool_name="write_b",
                operation_id="tool_call:attempt-h3-2:call-b",
                snapshot=snap_b,
                arguments={"path": "out/y", "value": 2},  # 不同物理根
                attempt_id=attempt_b,
            ),
        )

    thread_a = threading.Thread(target=run_a, name="h3-write-a")
    thread_b = threading.Thread(target=run_b, name="h3-write-b")
    try:
        thread_a.start()
        thread_b.start()
        # 双方 handler 都进入后（同时在执行中）才 release → 证明真并行。
        assert started_a.wait(timeout=10)
        assert started_b.wait(timeout=10)
    finally:
        release_a.set()
        release_b.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    a = outcome_a["execution"]
    b = outcome_b["execution"]
    assert a.result.ok
    assert b.result.ok
    assert write_a.calls == 1
    assert write_b.calls == 1


# ============================================================== k：审批悬挂不持锁
# 期望/现状（防回归，现状绿）：ask（悬挂）期间无 EXECUTING 行、无资源锁行
# （decide 拒绝先于 pre_handler_gate，open 未发生）；期间另一合法 mutating
# 操作能正常完成（不占锁不卡他人）；resume（注入 APPROVED binding，与
# execute_approved_registry_test_call 同机制）后同 call 正常执行且 settle 干净。
# 注（seq 235 问题 6）：ask→approved-binding 为两次同步调用，非真实异步
# suspend；「不占锁」由第三段（其他操作完成）表达。


def test_k_approval_ask_then_approved_binding(tmp_path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-k", task_id="task-k")
    dangerous = _DangerousTool()
    reader = _ReadOnlyTool()
    other = _PathWriteTool("managed_write")
    agent = _managed_chain(repo, store, [dangerous, reader, other], tmp_path)
    snap = runtime_snapshot_for_tools(
        {"danger_write": dangerous, "peek_file": reader, "managed_write": other},
        run_id="run-k",
    )
    params = _params(run_id="run-k", task_id="task-k", snapshot=snap)

    dangerous_call = _gate_call(
        run_id="run-k",
        tool_name="danger_write",
        operation_id="op-k-danger",
        snapshot=snap,
        attempt_id=attempt_id,
    )

    # 1) ask：无 binding → ask 拒绝 → 无 EXECUTING 行、无资源锁占用。
    execution = _traced_call(agent, params, dangerous_call)

    assert not execution.result.ok
    assert execution.decision.status == "ask"
    assert dangerous.calls == 0
    assert repo.operations_for_attempt(attempt_id) == []  # 无 EXECUTING 残留
    assert _lock_rows(repo) == []  # 无资源锁占用

    # 2) 悬挂期间另一合法 mutating 操作能完成（不占锁不卡他人）。
    other_call = _gate_call(
        run_id="run-k",
        tool_name="managed_write",
        operation_id="op-k-other",
        snapshot=snap,
        attempt_id=attempt_id,
    )
    other_execution = _traced_call(agent, params, other_call)
    assert other_execution.result.ok
    assert other.calls == 1

    # 3) resume：approval_request 生成 APPROVED binding（与 harness 同机制）→ 批准执行。
    assert execution.decision.approval_request is not None
    binding: dict[str, object] = {
        **dict(execution.decision.approval_request),
        "approval_id": "approval-k",
        "status": "APPROVED",
    }
    params.write_boundary["approved_actions"] = [binding]
    resumed = _traced_call(agent, params, dangerous_call)

    assert resumed.result.ok
    assert dangerous.calls == 1
    ops = repo.operations_for_attempt(attempt_id)
    assert all(op["status"] != "EXECUTING" for op in ops)  # settle 后无 EXECUTING 残留
    assert _lock_rows(repo) == []


# ============================================================== l：read-only 不绕门
# 期望（实现后）：read-only 只跳过副作用 operation，不绕过 coordinator：
# 缺 authority → handler=0；有 authority → 正常执行且 operation 行数=0。
# 现状：缺 authority 放行（handler=1）；有 authority 时外层 gate 也给 read-only
# 开行（ops=1）。


def test_l_managed_readonly_requires_authority(tmp_path):
    store = _store(tmp_path)
    reader = _ReadOnlyTool()
    snap = runtime_snapshot_for_tools({"peek_file": reader}, run_id="run-l")
    params = _params(run_id="run-l", task_id="task-l", snapshot=snap)

    # 缺 authority（无 repo）→ 显式 MANAGED 下仍必须拦截（read-only 只跳过
    # 副作用 operation，不绕过 authority）。
    agent_no_repo = _agent(
        repo=None,
        tools=None,
        store=store,
        root=tmp_path,
        execution_mode=ExecutionMode.MANAGED,
    )
    # registry 的 store 经 _operation_store_for 从 agent 解析（对齐 _managed_chain
    # 装配原则：测试不自己决定 store 权威来源）。MANAGED + 无 repo →
    # selector → ManagedOperationStore(None) → require_authority 拒绝，而非
    # 手传 LocalStore 把「无 repo」条件绕过。
    agent_no_repo.tools = _registry(
        tmp_path, _operation_store_for(agent_no_repo), [reader]
    )
    blocked = _traced_call(
        agent_no_repo,
        params,
        _gate_call(
            run_id="run-l",
            tool_name="peek_file",
            snapshot=snap,
            arguments={"path": "out/report.md"},
        ),
    )
    assert not blocked.result.ok
    assert blocked.result.error_code == "TOOL_AUTHORITY_CONTEXT_MISSING"
    assert not blocked.result.handler_executed
    assert reader.calls == 0

    # 有 authority → 正常执行，且 read-only 不建副作用 operation（行数=0）。
    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(repo, "run-l", task_id="task-l")
    agent = _managed_chain(repo, store, [reader], tmp_path)
    executed = _traced_call(
        agent,
        params,
        _gate_call(
            run_id="run-l",
            tool_name="peek_file",
            snapshot=snap,
            arguments={"path": "out/report.md"},
            attempt_id=attempt_id,  # 真实返回值：严格 fence 后伪造 attempt 会被拒
        ),
    )
    assert executed.result.ok
    assert reader.calls == 1
    current_attempt_id = repo.agent_run_for_run_id("run-l")["current_attempt_id"]
    assert current_attempt_id == attempt_id
    assert repo.operations_for_attempt(attempt_id) == []


def test_n_long_handler_renews_lease(tmp_path, monkeypatch):
    """seq 258 #1 反例：长 handler 运行期间 renew_tool_operation_lease 必须被生产调用。

    修复前：renew_tool_operation_lease 全仓零生产调用者——invoke 同步阻塞期间
    lease 自然过期 → 另一请求可把 EXECUTING 行标 UNKNOWN 并释放锁（竞态抢锁）。
    修复后：invoke 外包续租守护线程，按 lease 过半前滚动续租。
    """
    import threading
    import time as _time

    from agent_py_agent.agent.tooling import tool_operation_coordinator as coord

    # lease = max(_MINIMUM, timeout + _GRACE) → 1s，让 handler 阻塞期远超 lease
    monkeypatch.setattr(coord, "_MINIMUM_LEASE_SECONDS", 1)
    monkeypatch.setattr(coord, "_LEASE_GRACE_SECONDS", 0)

    repo = _repo(tmp_path)
    _agent_run_id, attempt_id = _direct_register_chain(
        repo, "run-renew", task_id="task-renew"
    )
    tool = _PathWriteTool("renew_long")
    agent = _managed_chain(repo, _store(tmp_path), [tool], tmp_path)
    store = _operation_store_for(agent)
    assert type(store).__name__ == "ManagedOperationStore"  # MANAGED 解析到 runtime.db

    renew_count = {"n": 0}
    original_renew = store.renew_tool_operation_lease

    def counting_renew(**kwargs):
        renew_count["n"] += 1
        return original_renew(**kwargs)

    store.renew_tool_operation_lease = counting_renew

    snap = runtime_snapshot_for_tools({"renew_long": tool}, run_id="run-renew")
    params = _params(run_id="run-renew", task_id="task-renew", snapshot=snap)
    call = _gate_call(
        run_id="run-renew",
        tool_name="renew_long",
        snapshot=snap,
        attempt_id=attempt_id,
    )

    tool.started = threading.Event()
    tool.release = threading.Event()
    errors: list[Exception] = []

    def run_call() -> None:
        try:
            _traced_call(agent, params, call)
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    thread = threading.Thread(target=run_call, daemon=True)
    thread.start()
    assert tool.started.wait(timeout=10)
    _time.sleep(2.5)  # 远超 1s lease：修复前此处零续租
    tool.release.set()
    thread.join(timeout=10)
    assert not errors
    assert renew_count["n"] >= 1  # 修复前 0 → 红


# LLM: WRITE-03(2026-08-15 真机): claim 阶段 RuntimeConflictError(执行权/资源锁冲突)
# 必须映射为可重试冲突码, 不能误报成 TOOL_OPERATION_STORE_UNAVAILABLE(存储故障)。
# 函数用途: 验证执行权冲突返回 BUSY_CONFLICT 且 handler 未执行。
def test_claim_runtime_conflict_maps_to_busy_conflict(tmp_path):
    from agent_py_agent.agent.runtime_db.managed_operation_store import RuntimeConflictError
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolOperationExecutionRequest,
        execute_tool_operation,
    )

    class _FakeStore:
        def claim_tool_operation(self, request):
            raise RuntimeConflictError("执行权锁冲突: 另一执行者持有")

        def finish_tool_operation(self, request):
            raise AssertionError("不应走到 finish")

    outcome = execute_tool_operation(
        ToolOperationExecutionRequest(
            store=_FakeStore(),
            store_required=True,
            owner_id="local/main",
            run_id="run-1",
            task_id="",
            operation_id="op-1",
            tool_name="run_command",
            args_hash="h",
            idempotency_key="k",
            idempotency_scope="operation",
            idempotency_namespace="n",
            timeout_seconds=10,
            invoke=lambda: (_ for _ in ()).throw(AssertionError("不应执行")),
            attempt_id="att-1",
            resource_scopes=(),
        )
    )
    assert outcome.error_code == "TOOL_OPERATION_BUSY_CONFLICT"
    assert outcome.handler_executed is False


# LLM: WRITE-04(2026-08-15 真机): 同一 attempt 声明父子 workspace scope
# (cwd=task_root + 写根=work/output)是合法资源声明, 不应被重叠检测判为冲突;
# 跨 attempt 的重叠仍必须拦截(并发保护)。
# 函数用途: 验证同 attempt 父子 scope 可共存, 跨 attempt 仍冲突。
def test_same_attempt_parent_child_scopes_coexist(tmp_path):
    import sqlite3

    from agent_py_agent.agent.runtime_db.managed_operation_store import (
        RuntimeConflictError,
        _check_workspace_overlap_in_tx,
        _insert_lock_in_tx,
    )
    from agent_py_agent.agent.tooling.tool_operation_coordinator import new_tool_operation_holder

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE resource_locks (canonical_scope TEXT UNIQUE, attempt_id TEXT)")
    db.execute("BEGIN IMMEDIATE")
    root = str(tmp_path / "task")
    work = str(tmp_path / "task" / "work")
    holder = new_tool_operation_holder()
    # 同 attempt 先插父 scope 再插子 scope → 不冲突
    db.execute("INSERT INTO resource_locks(canonical_scope, attempt_id) VALUES(?,?)", (f"workspace:{root}", "att-1"))
    _check_workspace_overlap_in_tx(db, f"workspace:{work}", attempt_id="att-1")
    db.execute("INSERT INTO resource_locks(canonical_scope, attempt_id) VALUES(?,?)", (f"workspace:{work}", "att-1"))
    db.execute("COMMIT")
    # 跨 attempt 重叠 → 冲突
    db.execute("BEGIN IMMEDIATE")
    try:
        _check_workspace_overlap_in_tx(db, f"workspace:{work}", attempt_id="att-2")
        raised = False
    except RuntimeConflictError:
        raised = True
    db.execute("ROLLBACK")
    assert raised


# LLM: WRITE-04 回归——同 attempt 内 run_command 的 cwd(task_root)+写根(work)不再自撞。
# 函数用途: 端到端验证 claim 带父子 scopes 成功。
def test_claim_with_parent_child_scopes_succeeds(tmp_path):
    from agent_py_agent.agent.runtime_db.managed_operation_store import (
        ManagedOperationStore,
        ToolOperationClaimRequest,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.tooling.tool_operation_coordinator import new_tool_operation_holder

    repo = RuntimeRepository(str(tmp_path / "runtime.db"))
    run_id = "run-write04"
    agent_run_id = "agentrun-write04"
    task_run_id = "taskrun-write04"
    attempt_id = "attempt-write04"
    import time

    now = time.time()
    with repo._runtime_connection() as conn:
        conn.execute(
            "INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (task_run_id, run_id, "created", now, now),
        )
        conn.execute(
            "INSERT INTO agent_runs(agent_run_id, task_run_id, run_id, role, status, current_attempt_id, "
            "current_attempt_generation, workspace_epoch, created_at, updated_at) VALUES(?,?,?,?,?,?,1,1,?,?)",
            (agent_run_id, task_run_id, run_id, "main", "created", attempt_id, now, now),
        )
        conn.execute(
            "INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at) VALUES(?,?,1,'running',?)",
            (attempt_id, agent_run_id, now),
        )
        conn.commit()
    store = ManagedOperationStore(repo)
    task_root = str(tmp_path / "task")
    work = str(tmp_path / "task" / "work")
    claim = store.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="local/main",
            run_id=run_id,
            task_id="",
            operation_id="op-write04",
            tool="run_command",
            args_hash="h",
            idempotency_key="k",
            idempotency_scope="operation",
            idempotency_namespace="n",
            holder=new_tool_operation_holder(),
            lease_expires_at=now + 3600,
            resource_scopes=(f"workspace:{task_root}", f"workspace:{work}", f"workspace:{work}/output"),
            attempt_id=attempt_id,
        )
    )
    assert str(getattr(claim.record, "operation_id", "") or "") == "op-write04"


# LLM: S-D1(2026-08-20 SUB-D 真机): workspace 锁冲突是瞬态并发语义——
# 高频派工/并行创建子代理时, 前一个操作几秒内释放。claim 必须做有界短重试,
# 不能一冲突就返回 BUSY_CONFLICT 让 create_subagents 整批失败(b1_1 实锤)。
# 函数用途: 验证冲突一次后重试成功, handler 正常执行。
def test_claim_conflict_retries_then_succeeds(tmp_path):
    from agent_py_agent.agent.runtime_db.managed_operation_store import RuntimeConflictError
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolOperationExecutionRequest,
        execute_tool_operation,
    )

    calls = {"claim": 0, "invoke": 0, "finish": 0}

    class _FakeStore:
        def claim_tool_operation(self, request):
            calls["claim"] += 1
            if calls["claim"] == 1:
                raise RuntimeConflictError("执行权锁冲突: 另一执行者持有")
            from agent_py_agent.agent.local_storage import ToolOperationClaim, ToolOperationRecord

            return ToolOperationClaim(
                action="execute",
                record=ToolOperationRecord(
                    owner_id=request.owner_id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    operation_id=request.operation_id,
                    tool=getattr(request, "tool_name", None) or getattr(request, "tool", ""),
                    args_hash=request.args_hash,
                    idempotency_key=request.idempotency_key,
                    idempotency_scope=request.idempotency_scope,
                    idempotency_namespace=request.idempotency_namespace,
                    status="RUNNING",
                    holder_id="h",
                    holder_host="test",
                    holder_pid=1,
                    holder_process_start_token="tok",
                    generation=1,
                    lease_expires_at=2.0,
                ),
            )

        def finish_tool_operation(self, request):
            calls["finish"] += 1
            return {}

    outcome = execute_tool_operation(
        ToolOperationExecutionRequest(
            store=_FakeStore(),
            store_required=True,
            owner_id="local/main",
            run_id="run-1",
            task_id="",
            operation_id="op-1",
            tool_name="run_command",
            args_hash="h",
            idempotency_key="k",
            idempotency_scope="operation",
            idempotency_namespace="n",
            timeout_seconds=10,
            invoke=lambda: (calls.__setitem__("invoke", calls["invoke"] + 1)
                            or ToolHandlerOutcome("run_command", True, "ok")),
            attempt_id="att-1",
            resource_scopes=(),
        )
    )
    assert calls["claim"] == 2, calls
    assert calls["invoke"] == 1, calls
    assert outcome.ok is True
    assert outcome.error_code == ""


# LLM: S-D1 边界: 重试耗尽仍冲突才返回 BUSY_CONFLICT(有界重试不无限等待)。
# 函数用途: 验证始终冲突时最终仍映射 BUSY_CONFLICT 且重试次数有界。
def test_claim_conflict_retries_exhausted_returns_busy(tmp_path):
    from agent_py_agent.agent.runtime_db.managed_operation_store import RuntimeConflictError
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolOperationExecutionRequest,
        execute_tool_operation,
    )

    calls = {"claim": 0}

    class _FakeStore:
        def claim_tool_operation(self, request):
            calls["claim"] += 1
            raise RuntimeConflictError("持续冲突")

        def finish_tool_operation(self, request):
            raise AssertionError("不应走到 finish")

    outcome = execute_tool_operation(
        ToolOperationExecutionRequest(
            store=_FakeStore(),
            store_required=True,
            owner_id="local/main",
            run_id="run-1",
            task_id="",
            operation_id="op-1",
            tool_name="run_command",
            args_hash="h",
            idempotency_key="k",
            idempotency_scope="operation",
            idempotency_namespace="n",
            timeout_seconds=10,
            invoke=lambda: (_ for _ in ()).throw(AssertionError("不应执行")),
            attempt_id="att-1",
            resource_scopes=(),
        )
    )
    assert calls["claim"] == 4, calls  # 初始 1 次 + 3 次重试
    assert outcome.error_code == "TOOL_OPERATION_BUSY_CONFLICT"
    assert outcome.handler_executed is False
