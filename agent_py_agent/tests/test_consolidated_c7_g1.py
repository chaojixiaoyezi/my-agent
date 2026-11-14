"""Consolidated test shard module: test_consolidated_c7_g1.py.

Sources:
- agent_py_agent/tests/test_rework_r9_managed_generation_admission.py
- agent_py_agent/tests/test_rework_sqlite_private_files_locks.py
- agent_py_agent/tests/test_sm_r2_channel_ab_delivery.py
- agent_py_agent/tests/test_sm_r2_lifecycle_rollback_legacy.py
- agent_py_agent/tests/test_sm_r2_scope_projection_paging.py
- agent_py_agent/tests/test_sm_rework_carrier_source.py
- agent_py_agent/tests/test_sm_rework_config_scope.py

[test_rework_sqlite_private_files_locks.py] R4-P0-02：LocalStore 建连不得对已存在的库、-wal、-shm 开关 FD。
[test_sm_r2_lifecycle_rollback_legacy.py] R2 生命周期闭环回归测试：回滚历史采纳保留、遗留记录补全绑定与打击纪元严格相等。
[test_sm_r2_scope_projection_paging.py] P1-05 回归测试：JsonlMemory.scope_pairs 多页有界续扫、降级收敛、失效重启与向后兼容。
[test_sm_rework_carrier_source.py] 候选载体请求的共享格式、来源重验和真实队列往返。
[test_sm_rework_config_scope.py] 验证全局配置仅在主会话开放，受限运行仍保留决策设置分流。
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from agent_py_agent.agent import runtime_context
from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.capability import skill_learning_store as store_module
from agent_py_agent.agent.capability import skill_service
from agent_py_agent.agent.capability.pack_verifier_runner import PackVerificationResult
from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool
from agent_py_agent.agent.capability.skill_learning import (
    CODE_INEFFECTIVE_RECORDED,
    CODE_INEFFECTIVE_UNATTRIBUTED,
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_host_feedback import observe_pack_verification
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    maintain_learning,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
    revert_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import (
    REQUEST_SCHEMA_VERSION,
    SkillLearningStore,
    request_key,
    skill_content_sha256,
)
from agent_py_agent.agent.capability.skill_learning_validation import (
    default_machine_promotion_verifier,
)
from agent_py_agent.agent.capability.skill_learning_versions import freeze_learned_snapshot
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_usage_ledger import (
    learning_usage_binding,
    record_skill_usage_event,
)
from agent_py_agent.agent.capability.skill_usage_receipts import (
    has_body_delivery_receipt,
    read_usage_aggregate,
    record_exact_negative,
    record_exact_use,
)
from agent_py_agent.agent.common.managed_owner_generation import (
    ManagedOwnerGeneration,
    ManagedOwnerGenerationError,
    bind_managed_owner_generation,
    current_managed_owner_generation,
    require_agent_managed_owner_generation,
    require_managed_owner_generation,
)
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.learning_source import subagent_projection
from agent_py_agent.agent.gateway_parts.managed_owner_runtime import prepare_managed_owner_runtime
from agent_py_agent.agent.local_storage import LocalStore, schema
from agent_py_agent.agent.memory_store import (
    _jsonl_indexing as indexing_module,
)
from agent_py_agent.agent.memory_store import (
    jsonl as jsonl_module,
)
from agent_py_agent.agent.memory_store._jsonl_indexing import bounded_authority_records
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import (
    CandidateService,
    CandidateStoreCorruptError,
)
from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
from agent_py_agent.agent.memory_store.jsonl import (
    _AUTHORITY_SCAN_MAX_BYTES,
    _AUTHORITY_SCAN_MAX_RECORDS,
    JsonlMemory,
    MemoryRecord,
)
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    long_term_record_matches_scope,
    runtime_long_term_scope,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.live_switches import refresh_live_switches, register_live_config
from agent_py_agent.agent.settings.parameter_changes import ledger_path, parameter_history
from agent_py_agent.agent.subagents.role_templates import active_model_subagent_tools
from agent_py_agent.agent.tooling import user_config_tool as module
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_skill_learning import BODY, _FakeBackend, _output
from agent_py_agent.tests.test_sm_w3_intake_runtime import _finalize_ctx
from agent_py_agent.tests.test_sm_w8_promotion_curator import _observe_tool_lesson, _setup_owner
from agent_py_agent.tests.test_sm_w9_feedback_awareness import (
    _activate_main_turn,
    _make_real_agent,
    _publish_skill,
    _record_delivery,
)
from agent_py_agent.tests.test_sm_w10_closed_loop import _ineffective_output

# =============================================================================
# Source: agent_py_agent/tests/test_rework_r9_managed_generation_admission.py
# LLM: 真实受管Agent与原账号/认证/治理存储；每个关口只观察是否在新效果前拒绝，替身仅记录本应未发生的下游调用。
# 模块用途: 验证受管原代次的只收窄绑定，以及停用/修订/安全/治理变化在各实际效果关口的拒绝与已接纳结果的保留。
# =============================================================================

ALICE = OwnerIdentity.local_user("alice")
CHANGES = {"disable": "USER_DISABLED", "revision": "USER_REVISION_CHANGED",
           "security": "MANAGED_OWNER_SECURITY_CHANGED", "policy": "MANAGED_OWNER_POLICY_CHANGED"}


@pytest.fixture
def host(tmp_path):
    root = (tmp_path / "host").resolve()
    lifecycle = LocalUserLifecycle(LocalUserStore(root))
    lifecycle.create("alice", idempotency_key="managed_alice_0001")
    agent = prepare_managed_owner_runtime(root, ALICE, host_options={})
    return SimpleNamespace(root=root, lifecycle=lifecycle, agent=agent, generation=agent._managed_owner_generation,
                           home=LocalUserStore(root).owner_home("alice"), tmp=tmp_path)


# LLM: 四类变化均走原管理员/认证入口；revision经停用再启用，账号仍启用但修订已前进。
# 函数用途: 对受管用户施加一种权威变化并返回预期拒绝码。
def _change(host, kind):
    if kind in {"disable", "revision"}:
        host.lifecycle.disable("alice", idempotency_key="disable_alice_0001")
    if kind == "revision":
        host.lifecycle.enable("alice", idempotency_key="enable_alice_00001")
    if kind == "security":
        from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService

        TotpSSOAuthService(host.root).update_sso_config({"protocol": "oidc", "issuer_url": "https://idp.example.test",
                                                         "client_id": "managed-client",
                                                         "redirect_uri": "https://host.example.test/callback"})
    if kind == "policy":
        ManagedUserGovernanceService(host.root).set_policy("alice", {"persona_mode": "admin_locked"}, expected_revision=0)
    return CHANGES[kind]


def _code(call):
    with pytest.raises(ManagedOwnerGenerationError) as caught:
        call()
    return caught.value.code


class _Tool:
    def __init__(self, effect):
        from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
        from agent_py_agent.tests._tool_runtime_harness import (
            make_test_model_spec,
            make_test_runtime_policy,
        )

        outer = self

        class Tool(BaseTool):
            model_spec = make_test_model_spec(f"managed_{effect}_tool", description="test-only managed tool")
            runtime_policy = make_test_runtime_policy(effect)

            def execute(self, params):
                outer.executions += 1
                return ToolHandlerOutcome(self.model_spec.name, True, "executed")

        self.executions, self.tool = 0, Tool()

    def run(self, root, *, write_boundary=None):
        from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

        return execute_canonical_test_call(root, tools={self.tool.model_spec.name: self.tool},
                                           tool_name=self.tool.model_spec.name, arguments={},
                                           write_boundary=write_boundary)


class _RecordingBackend(BaseBackend):
    name = "recording"

    def __init__(self, on_first_call, tool_input=None):
        self.calls, self.on_first_call, self.tool_input = 0, on_first_call, tool_input

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(provider="recording", endpoint="local://recording", model="", stream=False,
                                      native_supported=True, evidence="test_backend_declares_native_tools",
                                      observed_at=_utc_now_iso())

    # LLM: 第一次调用期间施加权威变化，模拟供应商等待中途的停用/改密/改策略；不发网络。
    # 函数用途: 返回已接纳的模型结果，可选附带一个原生工具调用。
    def generate(self, prompt, on_chunk=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            self.on_first_call()
            if self.tool_input is not None:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                    {"id": "call-1", "name": "write_file", "input": self.tool_input}])
        return ModelResponse(text="managed work finished", backend=self.name)


def test_generation_is_object_bound_and_cannot_be_rebuilt(host):
    generation = host.generation
    assert require_managed_owner_generation("unbound") is None and current_managed_owner_generation() is None
    with pytest.raises(ValueError):
        require_managed_owner_generation("")
    copies = [copy.copy(generation), copy.deepcopy(generation), pickle.loads(pickle.dumps(generation)),
              replace(generation), ManagedOwnerGeneration(**{item.name: getattr(generation, item.name) for item in fields(generation)})]
    for forged in copies:
        assert forged is not generation and forged.key == generation.key
        assert _code(lambda: bind_managed_owner_generation(forged).__enter__()) == "MANAGED_OWNER_GENERATION_INVALID"
        assert _code(lambda: require_agent_managed_owner_generation(
            SimpleNamespace(_managed_owner_generation=forged), "forged")) == "MANAGED_OWNER_GENERATION_INVALID"
    serialized = json.loads(json.dumps({item.name: str(getattr(generation, item.name)) for item in fields(generation)}))
    assert _code(lambda: bind_managed_owner_generation(serialized).__enter__()) == "MANAGED_OWNER_GENERATION_INVALID"
    other = prepare_managed_owner_runtime(host.root, ALICE, host_options={})._managed_owner_generation
    with bind_managed_owner_generation(generation) as bound:
        assert bound is generation and require_managed_owner_generation("bound") is generation
        with bind_managed_owner_generation(generation):
            assert current_managed_owner_generation() is generation
        assert _code(lambda: bind_managed_owner_generation(other).__enter__()) == "MANAGED_OWNER_GENERATION_CHANGED"
    assert current_managed_owner_generation() is None


@pytest.mark.parametrize("kind", sorted(CHANGES))
def test_authority_change_denies_each_new_effect_barrier(host, monkeypatch, kind):
    from agent_py_agent.agent.agent_core import tool_model_generation
    from agent_py_agent.agent.agent_core.runner import worker as runner_worker
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.backends import gateway_helpers
    from agent_py_agent.agent.backends.provider_headers import provider_runtime_scope
    from agent_py_agent.agent.gateway_parts.request_entry_admission import require_request_execution
    from agent_py_agent.agent.workflows import script_runner
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner

    agent, generation = host.agent, host.generation
    params = SimpleNamespace(task_attributes={}, thread_id="managed-barrier")
    ScriptStore(host.home.home_dir).save_script(AssetDescriptor("hello", "script", "Hello"),
                                                "import json\nprint(json.dumps({'ran': True}))\n")
    launched = []
    original_execute = script_runner._execute
    monkeypatch.setattr(script_runner, "_execute", lambda *args: launched.append(1) or original_execute(*args))
    runner = ScriptRunner(host.home.home_dir, use_sandbox=False, strict_sandbox=False, restrict_to_roots=True)
    tool = _Tool("read_only")
    with bind_managed_owner_generation(generation):
        child = runner_worker.RunSubagentWorkerParams(config=AgentConfig(), root=host.tmp, run_id="run-managed",
                                                      instruction="child work", dry_run=False, max_cards=1,
                                                      probe=False, retry_reason="")
        with provider_runtime_scope(agent, params):
            assert require_request_execution(agent=agent) is None
        assert runner.run_script("hello").ok and launched == [1]
        assert tool.run(host.tmp).result.handler_executed is True and tool.executions == 1
    assert child.managed_owner_generation is generation
    assert runner_worker.RunSubagentWorkerParams(config=AgentConfig(), root=host.tmp, run_id="r", instruction="x",
                                                 dry_run=False, max_cards=1, probe=False,
                                                 retry_reason="").managed_owner_generation is None

    expected = _change(host, kind)
    side_effects = []
    monkeypatch.setattr(model_selection, "mark_subagent_business_request_submitted", lambda *a, **k: side_effects.append("model"))
    monkeypatch.setattr(gateway_helpers, "_bounded_connect_timeout", lambda *a, **k: side_effects.append("connect"))
    monkeypatch.setattr(runner_worker, "_build_worker_agent", lambda *a, **k: side_effects.append("child"))

    @contextmanager
    def provider_scope():
        with provider_runtime_scope(agent, params):
            yield

    codes = {
        "provider_runtime": _code(lambda: provider_scope().__enter__()),
        "model_generation": _code(lambda: tool_model_generation._mark_call_before_send(
            None, "prompt", SimpleNamespace(agent=agent, params=None, call_id="call-1"))),
        "request_admission": _code(lambda: require_request_execution(agent=agent)),
    }
    with bind_managed_owner_generation(generation):
        codes["gateway_provider_call"] = _code(lambda: gateway_helpers._gateway_urlopen_unlimited(object(), object()))
        script = runner.run_script("hello")
        codes["native_script_launch"] = script.error_code
        execution = tool.run(host.tmp)
        codes["tool_invocation"] = execution.result.reported_error_code
        assert execution.result.error_code == "TOOL_EXECUTION_DENIED"
        assert execution.result.handler_executed is False and tool.executions == 1
    refusal = runner_worker._run_subagent_worker(child)
    codes["child_worker"] = refusal.runner_last_error
    assert (refusal.ok, refusal.status) == (False, "PENDING")
    assert codes == dict.fromkeys(codes, expected)
    assert side_effects == [] and launched == [1] and not script.ok
    assert script.effect_outcome == "not_started"


@pytest.mark.parametrize("kind", ["disable", "policy"])
def test_authority_change_during_waits_blocks_the_next_effect(host, monkeypatch, kind):
    from agent_py_agent.agent.conversation import auxiliary_model_call
    from agent_py_agent.agent.llm_scale import hot_path

    expected = CHANGES[kind]

    @contextmanager
    def slot_wait(*_args, **_kwargs):
        _change(host, kind)
        yield

    monkeypatch.setattr(hot_path, "global_llm_admission_slot", slot_wait)
    generated = []
    request = SimpleNamespace(agent=host.agent, purpose="managed-test", task_attributes={}, thread_id="aux")
    call = SimpleNamespace(ledger=None, call_id="aux-1", timeouts=None)
    assert _code(lambda: auxiliary_model_call._invoke_auxiliary_generate(
        request, lambda *a, **k: generated.append(1), call)) == expected
    assert generated == []


def test_authority_change_during_approval_wait_denies_before_handler(host):
    tool = _Tool("dangerous")
    with bind_managed_owner_generation(host.generation):
        first = tool.run(host.tmp)
        assert first.result.status == "approval_required" and first.decision.approval_request
        expected = _change(host, "security")
        binding = {**dict(first.decision.approval_request), "approval_id": "approval-managed", "status": "APPROVED"}
        second = tool.run(host.tmp, write_boundary={"approved_actions": [binding]})
    assert second.decision.evidence["approval_applied"] is True
    assert (second.result.error_code, second.result.reported_error_code) == ("TOOL_EXECUTION_DENIED", expected)
    assert second.result.handler_executed is False
    assert "approved" not in second.states and tool.executions == 0


def _scheduler(agent):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=agent.delivery_service)
    return BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})


@pytest.mark.parametrize("kind", sorted(CHANGES))
def test_change_during_model_wait_blocks_tool_and_next_model_call(host, kind):
    target = host.home.workspace_dir / "managed-output.txt"
    agent = host.agent
    backend = agent.backend = _RecordingBackend(lambda: _change(host, kind),
                                                tool_input={"path": str(target), "content": "should not exist"})
    dispatched = ManagedUserGovernanceService(host.root).dispatch_task("alice", "Write the report", title="Report")
    with pytest.raises(ManagedOwnerGenerationError) as caught:
        _scheduler(agent).tick_thread(dispatched["thread_id"])
    assert caught.value.code == CHANGES[kind]
    assert backend.calls == 1 and not target.exists()
    store = agent.conversation_store
    assert store.tasks.load(dispatched["task_id"]).status == "active"
    assert [wake.root_task_id for wake in store.wakes.pending()] == [dispatched["task_id"]]


def test_admitted_result_and_selected_model_survive_later_change(host):
    from agent_py_agent.agent.gateway_parts.managed_owner_runtime import ManagedOwnerPool
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    agent = host.agent
    dispatched = ManagedUserGovernanceService(host.root).dispatch_task("alice", "Finish quietly", title="Quiet")
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "anthropic_compatible", "model_name": "thread-model", "api_base": "https://example.test/anthropic",
        "api_key": "thread-secret", "model_context_window_tokens": 96000}})
    execute_model_profile_operation(agent, "select", {"profile_id": profile_id}, thread_id=dispatched["thread_id"])
    assert require_agent_managed_owner_generation(agent, "after_thread_selection") is host.generation
    assert agent.conversation_store.threads.require(dispatched["thread_id"]).model_profile_id == profile_id

    other = ManagedUserGovernanceService(host.root).dispatch_task("alice", "Commit this result", title="Commit")
    backend = agent.backend = _RecordingBackend(lambda: _change(host, "policy"))
    reports = _scheduler(agent).tick_thread(other["thread_id"])
    store = agent.conversation_store
    assert backend.calls == 1 and [report.task_status for report in reports] == ["completed"]
    assert store.tasks.load(other["task_id"]).status == "completed"
    assert [m.content for m in store.messages.recent(other["thread_id"]) if m.role == "assistant" and m.content] == [
        "managed work finished"]
    pool = ManagedOwnerPool(host.root, host_options={}, max_agents=2)
    with pool.acquire_pinned(ALICE) as fresh:
        assert fresh._managed_owner_generation is not host.generation
    assert pool.release_idle(0) == [fresh] and pool.gauges() == {"cached": 0, "building": 0, "pinned": 0}


# =============================================================================
# Source: agent_py_agent/tests/test_rework_sqlite_private_files_locks.py
# =============================================================================

# 外部进程：write 以 BEGIN IMMEDIATE 写一行，read 返回全部行与完整性检查；锁冲突按 SQLite 原文回报，不等待。
_FOREIGN = """
import json, sqlite3, sys
db, action = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db, timeout=0, isolation_level=None)
try:
    if action == "write":
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO items(value) VALUES (?)", (sys.argv[3],))
        conn.execute("COMMIT")
        result = {"ok": True}
    else:
        rows = [row[0] for row in conn.execute("SELECT value FROM items ORDER BY id")]
        result = {"ok": True, "rows": rows, "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0]}
except sqlite3.OperationalError as exc:
    result = {"ok": False, "error": str(exc)}
finally:
    conn.close()
print(json.dumps(result))
"""


# LLM: 只在测试临时目录建库；items 表由测试自建，不改 LocalStore 生产 schema。
# 函数用途: 创建关闭 FTS 的真实 LocalStore 并建好测试表。
def _store(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    conn = store._connect()
    try:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    return store


# LLM: 子进程以 -I 只加载标准库 sqlite3，不导入 my-agent 代码，也不读写 HOME。
# 函数用途: 在另一个真实进程里对同一库执行一次读或写，返回其 JSON 结果。
def _foreign(db, action, value=""):
    completed = subprocess.run([sys.executable, "-I", "-c", _FOREIGN, str(db), action, value],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# LLM: 路径拼法与生产 _private_sqlite_files 相同，只计算不访问文件。
# 函数用途: 返回库及其 WAL、SHM 三个路径。
def _files(db):
    return [db.with_name(db.name + suffix) for suffix in ("", "-wal", "-shm")]


# LLM: 持写事务期间再建一条 LocalStore 连接；外部进程必须仍拿不到写锁，提交后只看到持有者的行。
# 函数用途: 证明建连不释放活连接的 WAL 写锁，外部提交不能插进持有者的写事务。
def test_second_connect_keeps_the_live_write_lock(tmp_path):
    store = _store(tmp_path)
    holder = store._connect()
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO items(value) VALUES ('holder')")
        store._connect().close()
        assert _foreign(store.db_path, "write", "foreign") == {"ok": False, "error": "database is locked"}
        holder.commit()
        assert _foreign(store.db_path, "read") == {"ok": True, "rows": ["holder"], "integrity": "ok"}
    finally:
        holder.close()


# LLM: 活连接读过一次后另建连接；外部进程开读关不能自认最后一个连接而 checkpoint 并删除 sidecar。
# 函数用途: 证明活连接的共享锁与 DMS 锁仍在，两边随后的提交都不丢。
def test_foreign_open_close_keeps_the_live_wal_and_shm(tmp_path):
    store = _store(tmp_path)
    live = store._connect()
    try:
        assert live.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        store._connect().close()
        assert _foreign(store.db_path, "read") == {"ok": True, "rows": [], "integrity": "ok"}
        assert [path.name for path in _files(store.db_path) if not path.exists()] == []
        live.execute("INSERT INTO items(value) VALUES ('live')")
        live.commit()
        assert _foreign(store.db_path, "write", "foreign") == {"ok": True}
    finally:
        live.close()
    assert _foreign(store.db_path, "read") == {"ok": True, "rows": ["live", "foreign"], "integrity": "ok"}


# LLM: umask 022 下新库与 sidecar 仍是 0600，旧 0644 库被收紧；出生临时名不残留，链接库路径不跟随也不建目标。
# 函数用途: 保住原私有权限合同，并确认新的出生方式只发表普通文件。
@pytest.mark.parametrize("link_target", ["existing", "dangling"])
def test_private_modes_birth_cleanup_and_symlink_rejection(tmp_path, link_target):
    previous = os.umask(0o022)
    try:
        store = LocalStore(tmp_path / "fresh" / "local.db", enable_fts=False)
        conn = store._connect()
        try:
            assert {path.name: stat.S_IMODE(os.lstat(path).st_mode) for path in _files(store.db_path)} == {
                "local.db": 0o600, "local.db-wal": 0o600, "local.db-shm": 0o600}
        finally:
            conn.close()
        legacy = tmp_path / "legacy" / "local.db"
        legacy.parent.mkdir()
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE legacy(value TEXT)")
        conn.commit()
        conn.close()
        os.chmod(legacy, 0o644)
        LocalStore(legacy, enable_fts=False)
        assert stat.S_IMODE(os.lstat(legacy).st_mode) == 0o600
    finally:
        os.umask(previous)
    assert [path for path in tmp_path.rglob("*") if path.name.endswith(".birth")] == []
    target = legacy if link_target == "existing" else tmp_path / "missing.db"
    link = tmp_path / "linked" / "local.db"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(ValueError, match="SQLite storage must be a regular file"):
        LocalStore(link, enable_fts=False)
    assert os.path.lexists(target) == (link_target == "existing")


# LLM: 只记录 schema 模块所见的 os.open；SQLite 在 C 层自己的打开不经过这里，第二次建连不能碰活文件。
# 函数用途: 直接锁定根因：已存在的库、-wal、-shm 上不再由 Python 开关 FD。
def test_second_connect_opens_no_python_fd_on_live_files(tmp_path, monkeypatch):
    store = _store(tmp_path)
    first = store._connect()
    opened = []
    real_open = schema.os.open

    # LLM: 原样转交真实 os.open，只追加目标路径，不改变打开结果或异常。
    # 函数用途: 记录第二次建连期间 Python 层打开过的路径。
    def recording_open(path, flags, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    try:
        assert all(path.exists() for path in _files(store.db_path))
        monkeypatch.setattr(schema.os, "open", recording_open)
        store._connect().close()
    finally:
        first.close()
    assert set(opened).isdisjoint(os.fspath(path) for path in _files(store.db_path))


# LLM: 仅模拟 Seatbelt 的 file-link 拒绝；真实 LocalStore 仍须守住私有权限、链接拒绝与活库零 Python FD。
# 函数用途: 验证硬链接不可用时新库能安全出生，后续建连不会碰掉 SQLite 的锁。
def test_private_birth_falls_back_cleanly_when_hardlink_denied(tmp_path, monkeypatch):
    denied = []
    opened = []
    real_open = schema.os.open

    # LLM: 记录真实出生尝试后抛出与 Seatbelt 相同的 EPERM，不代替文件创建或数据库连接。
    # 函数用途: 让硬链接发表稳定触发权限拒绝。
    def deny_link(birth, path):
        denied.append((birth, path))
        raise PermissionError(1, "Operation not permitted")

    # LLM: 原样调用 os.open，只在后续建连的有界观察窗记录 Python 层文件打开。
    # 函数用途: 检查已有数据库建连没有额外打开任何 Python 文件描述符。
    def recording_open(path, flags, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(schema.os, "link", deny_link)
    previous = os.umask(0o022)
    try:
        store = LocalStore(tmp_path / "seatbelt" / "local.db", enable_fts=False)
        live = store._connect()
        try:
            assert {path.name: stat.S_IMODE(os.lstat(path).st_mode) for path in _files(store.db_path)} == {
                "local.db": 0o600, "local.db-wal": 0o600, "local.db-shm": 0o600}
            with monkeypatch.context() as connect_patch:
                connect_patch.setattr(schema.os, "open", recording_open)
                store._connect().close()
                store._connect().close()
            assert opened == []
        finally:
            live.close()
        assert len(denied) == 1
        assert denied[0][0].name.endswith(".birth")
        assert denied[0][1] == store.db_path
        assert list(tmp_path.rglob("*.birth")) == []

        for link_target in ("existing", "dangling"):
            target = tmp_path / f"{link_target}.db"
            if link_target == "existing":
                target.write_bytes(b"must remain untouched")
                target.chmod(0o644)
            link = tmp_path / link_target / "local.db"
            link.parent.mkdir()
            link.symlink_to(target)
            with pytest.raises(ValueError, match="^SQLite storage must be a regular file$"):
                LocalStore(link, enable_fts=False)
            assert link.is_symlink()
            assert os.path.lexists(target) == (link_target == "existing")
            if link_target == "existing":
                assert target.read_bytes() == b"must remain untouched"
                assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert len(denied) == 1
        assert list(tmp_path.rglob("*.birth")) == []
    finally:
        os.umask(previous)


# LLM: 在 link 被拒前模拟其它进程发表主路径；回退的 O_EXCL 不得覆盖普通文件或跟随链接。
# 函数用途: 验证回退创建遇到并发路径时保留原 inode，并继续拒绝已有和悬空链接。
@pytest.mark.parametrize("racing_path", ["regular", "existing_symlink", "dangling_symlink"])
def test_private_birth_fallback_preserves_racing_path(tmp_path, monkeypatch, racing_path):
    db = tmp_path / "seatbelt-race" / "local.db"
    target = tmp_path / "target.db"
    if racing_path == "existing_symlink":
        target.write_bytes(b"must remain untouched")
        target.chmod(0o644)
    published = []

    # LLM: 只在尚不存在的主路径模拟一次外进程发表；拒绝 link 后仍由生产回退与 lstat 做裁决。
    # 函数用途: 在初次不存在检查与直接创建之间放入竞争文件或链接。
    def publish_then_deny_link(birth, path):
        if racing_path == "regular":
            path.touch(mode=0o600)
        else:
            path.symlink_to(target)
        published.append(os.lstat(path))
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(schema.os, "link", publish_then_deny_link)
    if racing_path == "regular":
        store = LocalStore(db, enable_fts=False)
        conn = store._connect()
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in _files(db))
        finally:
            conn.close()
    else:
        with pytest.raises(ValueError, match="^SQLite storage must be a regular file$"):
            LocalStore(db, enable_fts=False)
        assert db.is_symlink()
        assert os.path.lexists(target) == (racing_path == "existing_symlink")
        if racing_path == "existing_symlink":
            assert target.read_bytes() == b"must remain untouched"
            assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert len(published) == 1
    current = os.lstat(db)
    assert (current.st_dev, current.st_ino) == (published[0].st_dev, published[0].st_ino)
    assert list(tmp_path.rglob("*.birth")) == []


# =============================================================================
# Source: agent_py_agent/tests/test_sm_r2_channel_ab_delivery.py
# =============================================================================

def _refresh_snapshot(agent) -> None:
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)


def _main_thread(agent):
    return agent.conversation_store.threads.get_or_create(
        {
            "channel": "local",
            "channel_conversation_id": "main",
            "owner_id": str(agent.home_paths.owner_id),
        }
    )


def _skill_hurt_count(store, record) -> int:
    maint = dict(record.maintenance)
    agg = read_usage_aggregate(
        store._runtime_repository(),
        incarnation_id=str(maint["incarnation_id"]),
        activation_id=str(maint["activation_id"]),
        content_sha256=record.sha256,
    )
    return int(agg["hurt_count"]) if agg else 0


def _record_host_delivery(
    agent,
    record,
    *,
    thread_id: str,
    task_id: str,
    request_id: str,
    event: str = "host_injected",
    kind: str = "skill",
    body_chars: int = 320,
    incarnation_id: str | None = None,
    activation_id: str | None = None,
    content_sha256: str | None = None,
) -> None:
    maint = dict(record.maintenance)
    record_skill_usage_event(
        agent,
        event=event,
        skill_id=f"owner:{record.name}",
        source="learned",
        kind=kind,
        request_id=request_id,
        thread_id=thread_id,
        task_id=task_id,
        chars=body_chars,
        body_chars=body_chars,
        task_key=task_id,
        content_sha256=record.sha256 if content_sha256 is None else content_sha256,
        incarnation_id=str(maint["incarnation_id"]) if incarnation_id is None else incarnation_id,
        activation_id=str(maint["activation_id"]) if activation_id is None else activation_id,
        strike_epoch=int(maint.get("strike_epoch") or 0),
    )


def _seed_prior_adoptions(store, record, *, count: int = 2) -> None:
    maint = dict(record.maintenance)
    for idx in range(1, count + 1):
        observe_learning(
            store,
            record.name,
            sha256=record.sha256,
            task_id=f"seed-adopt-task-{idx}",
            outcome="adopted",
            source=LearningObservationSource(
                authority="model",
                run_id=f"seed-adopt-run-{idx}",
                receipt_id=f"seed-adopt:{idx}",
                incarnation_id=str(maint["incarnation_id"]),
                activation_id=str(maint["activation_id"]),
            ),
        )


def _make_summarizer(store, repo, tmp_path: Path, backend: _FakeBackend, name: str) -> SkillLearningService:
    settings = SkillLearningSettings(min_tool_rounds=3, max_skills=50)
    runtime = SkillLearningRuntime(
        owner_id="local:main",
        deployment_root=tmp_path / "dep",
        backend=backend,
        snapshot_provider=lambda: SimpleNamespace(
            entries=(
                SimpleNamespace(
                    name=name,
                    source="owner",
                    description="r2 test skill",
                    path=store.skill_path(name),
                    stable_id=f"owner:{name}",
                ),
            )
        ),
        guard_config=None,
        usage_runtime_provider=lambda: repo,
    )
    return SkillLearningService(store, settings, runtime)


def _assert_rejected_channel_a(res) -> None:
    assert res.ok is False
    assert res.error_code == "SKILL_FEEDBACK_UNAVAILABLE"
    assert res.effect_outcome == "not_started"
    sf = res.result_envelope["skill_feedback"]
    assert sf["side_effect_status"] == "not_started"
    assert sf["code"] == "PRECONDITION_NOT_MET"


def test_case1_channel_a_rejects_adopted_without_body_delivery_across_three_tasks(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-a-adopted-only")
    _refresh_snapshot(agent)

    claim = None
    for idx in range(1, 4):
        claim = _activate_main_turn(
            agent,
            tmp_path,
            run_id=f"run-c1-{idx}",
            task_id=f"task-c1-{idx}",
            request_id=f"req-c1-{idx}",
            prev_claim=claim,
        )
        adopted = SkillFeedbackTool(agent).execute(
            {"action": "adopted", "skill_id": f"owner:{rec.name}"}
        )
        assert adopted.ok is True

        rejected = SkillFeedbackTool(agent).execute(
            {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
        )
        _assert_rejected_channel_a(rejected)

    assert _skill_hurt_count(store, rec) == 0
    assert store.skill_path(rec.name).exists()
    reg = store.load_registry()
    assert rec.name not in reg.blocked_names
    latest = reg.skills.get(rec.name)
    assert latest is not None
    assert latest.maintenance.get("state") != "retired"


def test_case2_channel_b_rejects_unattributed_or_missing_receipt_even_with_adopted(
    tmp_path: Path,
) -> None:
    _, repo, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-b-unattributed")
    maint = dict(rec.maintenance)
    _seed_prior_adoptions(store, rec, count=2)

    backend = _FakeBackend(
        _ineffective_output(rec.name),
        _ineffective_output(rec.name),
        _ineffective_output(rec.name),
    )
    summarizer = _make_summarizer(store, repo, tmp_path, backend, rec.name)

    # First two finalize requests omit owner:skill from used_skill_ids while holding adopted rows;
    # third includes owner:skill in used_skill_ids AND has an adopted row, but lacks a body receipt.
    cases = [
        ("task-b-1", ()),
        ("task-b-2", (f"capability:{rec.name}", f"builtin:{rec.name}")),
        ("task-b-3", (f"owner:{rec.name}",)),
    ]
    results = []
    for idx, (task_id, used_ids) in enumerate(cases, start=1):
        observe_learning(
            store,
            rec.name,
            sha256=rec.sha256,
            task_id=task_id,
            outcome="adopted",
            source=LearningObservationSource(
                authority="model",
                run_id=f"run-b-{idx}",
                receipt_id=f"adopt-b:{task_id}",
                incarnation_id=str(maint["incarnation_id"]),
                activation_id=str(maint["activation_id"]),
            ),
        )
        assert (
            summarizer.enqueue_from_finalize(
                _finalize_ctx(
                    f"run-b-{idx}",
                    task_id=task_id,
                    root_task_id=task_id,
                    used_skill_ids=used_ids,
                )
            )
            == "queued"
        )
        results.append(summarizer.run_pending())

    assert len(results) == 3
    for res in results:
        assert res.status == "rejected"
        assert res.code == CODE_INEFFECTIVE_UNATTRIBUTED

    assert _skill_hurt_count(store, rec) == 0
    assert store.skill_path(rec.name).exists()
    reg = store.load_registry()
    assert rec.name not in reg.blocked_names
    latest = reg.skills.get(rec.name)
    assert latest is not None
    assert latest.maintenance.get("state") != "retired"


def test_case3_channel_a_accepts_current_and_same_thread_delivery_rejects_other_or_noise(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec_current = _publish_skill(store, name="lesson-cur-get")
    rec_prev_get = _publish_skill(store, name="lesson-prev-get")
    rec_host_inj = _publish_skill(store, name="lesson-host-inj")
    rec_noise = _publish_skill(store, name="lesson-other-and-noise")
    _refresh_snapshot(agent)

    # Task 1 on main thread: manual_get for both rec_current and rec_prev_get.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c3-1",
        task_id="task-c3-1",
        request_id="req-c3-1",
    )
    thread = _main_thread(agent)
    get_cur = SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_current.name}"})
    assert get_cur.ok is True
    get_prev = SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_prev_get.name}"})
    assert get_prev.ok is True

    # Current-task delivery accepted by Channel A in Task 1.
    res_cur = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_current.name}"}
    )
    assert res_cur.ok is True
    assert res_cur.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec_current) == 1

    # Task 1 also receives genuine same-thread host_injected delivery for rec_host_inj.
    _record_host_delivery(
        agent,
        rec_host_inj,
        thread_id=thread.thread_id,
        task_id="task-c3-1",
        request_id="req-c3-1",
        event="host_injected",
        body_chars=240,
    )

    # Seed noise & foreign-thread deliveries for rec_noise (none valid for main thread).
    other_thread = agent.conversation_store.threads.get_or_create(
        {
            "channel": "local",
            "channel_conversation_id": "other-thread-c3",
            "owner_id": str(agent.home_paths.owner_id),
        }
    )
    assert other_thread.thread_id != thread.thread_id
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=other_thread.thread_id,
        task_id="task-other-thread",
        request_id="req-other-thread",
        event="manual_get",
        body_chars=240,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id="",
        task_id="task-empty-thread",
        request_id="req-empty-thread",
        event="manual_get",
        body_chars=240,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=thread.thread_id,
        task_id="task-c3-zero",
        request_id="req-c3-zero",
        event="manual_get",
        body_chars=0,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=thread.thread_id,
        task_id="task-c3-pkg",
        request_id="req-c3-pkg",
        event="host_injected",
        kind="capability_package",
        body_chars=240,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=thread.thread_id,
        task_id="task-c3-search",
        request_id="req-c3-search",
        event="manual_search",
        body_chars=240,
    )

    # Task 2 on SAME main thread: no new get in Task 2.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c3-2",
        task_id="task-c3-2",
        request_id="req-c3-2",
        prev_claim=claim,
    )
    res_prev = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_prev_get.name}"}
    )
    assert res_prev.ok is True
    assert res_prev.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec_prev_get) == 1

    res_host = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_host_inj.name}"}
    )
    assert res_host.ok is True
    assert res_host.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec_host_inj) == 1

    res_noise = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_noise.name}"}
    )
    _assert_rejected_channel_a(res_noise)
    assert _skill_hurt_count(store, rec_noise) == 0


def test_case4_same_thread_dedups_within_complaint_task_and_counts_distinct_tasks(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-c4-complaints")
    _refresh_snapshot(agent)

    # Delivery occurs once in Task 1 on main thread.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-deliver",
        task_id="task-c4-deliver",
        request_id="req-c4-deliver",
    )
    assert SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec.name}"}).ok is True

    # Complaint Task 2: calling twice must deduplicate to 1 strike.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k1a",
        task_id="task-c4-k1",
        request_id="req-c4-k1a",
        prev_claim=claim,
    )
    first = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert first.ok is True
    assert first.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec) == 1

    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k1b",
        task_id="task-c4-k1",
        request_id="req-c4-k1b",
        prev_claim=claim,
    )
    dup = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert dup.ok is True
    assert dup.result_envelope["skill_feedback"]["code"] == "DEDUPLICATED"
    assert _skill_hurt_count(store, rec) == 1

    # Distinct complaint Tasks 3 and 4 in the same thread count separately (2 then 3 -> retired).
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k2",
        task_id="task-c4-k2",
        request_id="req-c4-k2",
        prev_claim=claim,
    )
    second = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert second.ok is True
    assert second.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec) == 2

    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k3",
        task_id="task-c4-k3",
        request_id="req-c4-k3",
        prev_claim=claim,
    )
    third = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert third.ok is True
    assert third.result_envelope["skill_feedback"]["code"] == "RETIRED"


def test_case5_stale_frozen_or_mismatched_activation_binding_cannot_transfer(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec_v1 = _publish_skill(store, name="lesson-c5-ver")
    _refresh_snapshot(agent)

    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c5-1",
        task_id="task-c5-1",
        request_id="req-c5-1",
    )
    assert SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_v1.name}"}).ok is True
    stale_snapshot = agent._current_skill_snapshot

    # Publish v2 (new sha256 & new activation_id on same skill_name).
    rec_v2 = _publish_skill(store, name="lesson-c5-ver", update_from=rec_v1)

    # 1) Stale snapshot from v1 cannot submit feedback once v2 is live.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c5-2",
        task_id="task-c5-2",
        request_id="req-c5-2",
        prev_claim=claim,
    )
    agent._current_skill_snapshot = stale_snapshot
    stale_res = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    assert stale_res.ok is False
    assert stale_res.error_code == "SKILL_LEARNING_REVISION_CONFLICT"

    # 2) Frozen entry lacking verified binding or pointing outside owner store is rejected.
    _refresh_snapshot(agent)
    frozen_snap = agent._current_skill_snapshot
    orig_entry = frozen_snap.resolve(f"owner:{rec_v2.name}")
    assert orig_entry is not None
    frozen_copy_path = tmp_path / "frozen" / rec_v2.name / "SKILL.md"
    frozen_copy_path.parent.mkdir(parents=True, exist_ok=True)
    frozen_copy_path.write_text(BODY, encoding="utf-8")
    broken_entry = replace(
        orig_entry,
        frozen_path=str(frozen_copy_path),
        asset_fields={
            k: v
            for k, v in dict(orig_entry.asset_fields).items()
            if k != "_learning_usage_binding"
        },
    )
    agent._current_skill_snapshot = replace(
        frozen_snap,
        entries=tuple(
            broken_entry if e.stable_id == orig_entry.stable_id else e
            for e in frozen_snap.entries
        ),
    )
    frozen_res = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    assert frozen_res.ok is False
    assert frozen_res.error_code == "SKILL_LEARNING_REVISION_CONFLICT"

    # 3) Live v2 snapshot with ONLY v1's same-thread delivery (different activation/sha) is rejected.
    _refresh_snapshot(agent)
    no_v2_delivery = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    _assert_rejected_channel_a(no_v2_delivery)
    assert _skill_hurt_count(store, rec_v2) == 0


def test_case6_three_channels_dedup_three_tasks_rollback_and_channel_c_adopted_intact(
    tmp_path: Path,
) -> None:
    agent, repo, store = _make_real_agent(tmp_path)
    agent.runtime_db = repo
    name = "lesson-c6-tri"
    rec_v1 = _publish_skill(store, name=name)
    for idx in (1, 2, 3):
        _record_delivery(store, rec_v1, task_id=f"est-{idx}")
        observe_learning(
            store,
            name,
            sha256=rec_v1.sha256,
            task_id=f"est-{idx}",
            outcome="effective",
            source=LearningObservationSource(authority="host", receipt_id=f"est:{idx}"),
        )
    rec_v2 = _publish_skill(store, name=name, update_from=store.load_registry().skills[name])
    _refresh_snapshot(agent)

    # Task 1: real delivery + Channel A + Channel B + Channel C -> deduplicate to 1 strike.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c6-1",
        task_id="task-c6-1",
        request_id="req-c6-1",
    )
    assert SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_v2.name}"}).ok is True
    assert has_body_delivery_receipt(
        store._runtime_repository(),
        incarnation_id=str(rec_v2.maintenance["incarnation_id"]),
        activation_id=str(rec_v2.maintenance["activation_id"]),
        content_sha256=rec_v2.sha256,
        task_id="task-c6-1",
        request_id="req-c6-1",
    )

    ch_a = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    assert ch_a.ok is True
    assert ch_a.result_envelope["skill_feedback"]["code"] == "OK"

    summarizer = _make_summarizer(store, repo, tmp_path, _FakeBackend(_ineffective_output(name)), name)
    assert (
        summarizer.enqueue_from_finalize(
            _finalize_ctx(
                "run-c6-1-bg",
                task_id="task-c6-1",
                root_task_id="task-c6-1",
                used_skill_ids=(f"owner:{name}",),
            )
        )
        == "queued"
    )
    ch_b = summarizer.run_pending()
    assert ch_b.status == "ineffective"
    assert ch_b.code == CODE_INEFFECTIVE_RECORDED

    observe_pack_verification(
        agent,
        agent._current_run_params,
        PackVerificationResult(
            verifier_id="pytest",
            package_id="pkg-c6-1",
            status="failed",
            valid=False,
        ),
        "pk-c6-1",
    )
    assert _skill_hurt_count(store, rec_v2) == 1

    # Tasks 2 and 3 (distinct tasks with Channel A complaints) trigger rollback to v1!
    for idx in (2, 3):
        claim = _activate_main_turn(
            agent,
            tmp_path,
            run_id=f"run-c6-{idx}",
            task_id=f"task-c6-{idx}",
            request_id=f"req-c6-{idx}",
            prev_claim=claim,
        )
        res = SkillFeedbackTool(agent).execute(
            {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
        )
        assert res.ok is True
        expected_code = "OK" if idx == 2 else "ROLLED_BACK"
        assert res.result_envelope["skill_feedback"]["code"] == expected_code

    restored = store.load_registry().skills.get(name)
    assert restored is not None
    assert restored.sha256 == rec_v1.sha256

    # Channel C retains adopted-only rule even without a body delivery receipt!
    rec_c_only = _publish_skill(store, name="lesson-c6-channel-c-adopted")
    _refresh_snapshot(agent)
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="task-c6-chc",
        task_id="task-c6-chc",
        request_id="req-c6-chc",
        prev_claim=claim,
    )
    adopt_only = SkillFeedbackTool(agent).execute(
        {"action": "adopted", "skill_id": f"owner:{rec_c_only.name}"}
    )
    assert adopt_only.ok is True
    assert not has_body_delivery_receipt(
        store._runtime_repository(),
        incarnation_id=str(rec_c_only.maintenance["incarnation_id"]),
        activation_id=str(rec_c_only.maintenance["activation_id"]),
        content_sha256=rec_c_only.sha256,
        task_id="task-c6-chc",
        request_id="req-c6-chc",
    )
    observe_pack_verification(
        agent,
        agent._current_run_params,
        PackVerificationResult(
            verifier_id="pytest",
            package_id="pkg-c6-chc",
            status="failed",
            valid=False,
        ),
        "pk-c6-chc",
    )
    assert _skill_hurt_count(store, rec_c_only) == 1


def test_case7_subagent_and_restricted_context_remain_denied(tmp_path: Path) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-c7-subagent")
    _refresh_snapshot(agent)
    _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c7-main",
        task_id="task-c7-main",
        request_id="req-c7-main",
    )

    tool = SkillFeedbackTool(agent)
    params = {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    val_err = tool.validate_invocation(
        params,
        SimpleNamespace(context_scope="task_local", child=True, subagent_run_id="sub-c7-1"),
    )
    assert val_err is not None
    assert val_err.error_code == "TOOL_PERMISSION_DENIED"
    assert val_err.effect_outcome == "not_started"
    assert val_err.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"
    assert val_err.result_envelope["skill_feedback"]["code"] == "CHILD_CONTEXT_FORBIDDEN"

    agent._current_run_params = replace(
        agent._current_run_params,
        context_scope="task_local",
        task_attributes={
            **dict(agent._current_run_params.task_attributes or {}),
            "agent_run_id": "sub-c7-1",
        },
    )
    exec_res = tool.execute(params)
    assert exec_res.ok is False
    assert exec_res.error_code == "TOOL_PERMISSION_DENIED"
    assert exec_res.effect_outcome == "not_started"
    assert exec_res.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"
    assert exec_res.result_envelope["skill_feedback"]["code"] == "CHILD_CONTEXT_FORBIDDEN"


# =============================================================================
# Source: agent_py_agent/tests/test_sm_r2_lifecycle_rollback_legacy.py
# =============================================================================

BASE_BODY = """---
name: sample
description: Deterministic workflow skill
---

# Steps

1. Inspect the explicit structural inputs.
2. Produce deterministic outputs cleanly.
"""

COOL_SECONDS = 604_800
ARCHIVE_SECONDS = 1_296_000
EVICT_SECONDS = 1_296_000
MAX_SKILLS = 50
BATCH_ENTRIES = 128


def _store_with_runtime(root: Path) -> tuple[object, RuntimeRepository, SkillLearningStore]:
    home = ensure_my_agent_home(root / "home")
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    return home, repo, store


def _make_owner_home(root: Path):
    base = home_paths(root / "data")
    owner = ensure_owner_home(base.data_dir, OwnerIdentity.local_main())
    hp = home_paths_with_owner(base, owner)
    (Path(hp.owner_home_dir) / "skills").mkdir(parents=True, exist_ok=True)
    Path(hp.shared_skills_dir).mkdir(parents=True, exist_ok=True)
    Path(hp.shared_builtin_dir).mkdir(parents=True, exist_ok=True)
    repo = RuntimeRepository(runtime_db_path(hp.owner_home_dir))
    store = SkillLearningStore.for_home(hp, usage_runtime_provider=lambda: repo)
    return hp, repo, store


def _decision(name: str, *, suffix: str = "v1") -> SkillLearningDecision:
    return SkillLearningDecision(
        decision="create",
        reason="deterministic reusable workflow",
        name=name,
        description=f"Reusable helper for {name}",
        body=f"{BASE_BODY}\n\n# skill:{name}:{suffix}\n",
        when_to_use=f"when handling {name}",
        tags=(name, "workflow"),
    )


def _publish(store: SkillLearningStore, name: str, *, suffix: str = "v1", update_from=None):
    dec = _decision(name, suffix=suffix)
    expected_sha256 = ""
    expected_revision = None
    if update_from is not None:
        dec = replace(dec, decision="update", update_target=name)
        expected_sha256 = update_from.sha256
        expected_revision = update_from.revision
    return publish_learned_skill(
        store,
        PublishRequest(dec, expected_revision=expected_revision, expected_sha256=expected_sha256),
    )


def _agent_for(home, repo: RuntimeRepository, run_id: str, *, task_id: str = "", request_id: str = ""):
    params = SimpleNamespace(
        run_id=run_id,
        task_id=task_id,
        request_id=request_id,
        attempt_id="attempt-1",
        task_attributes={},
    )
    return SimpleNamespace(
        config=SimpleNamespace(enable_self_learning=True),
        home_paths=home,
        runtime_db=repo,
        _current_run_params=params,
    )


def _deliver_and_adopt(
    store: SkillLearningStore,
    rec,
    *,
    task_id: str,
    run_id: str,
    event: str = "manual_get",
    epoch: int | None = None,
) -> None:
    inc = str(rec.maintenance.get("incarnation_id") or "")
    act = str(rec.maintenance.get("activation_id") or "")
    strike_epoch = int(rec.maintenance.get("strike_epoch", 0) or 0) if epoch is None else epoch
    record_skill_usage_event(
        store,
        event=event,
        skill_id=f"owner:{rec.name}",
        source="learned",
        kind="skill",
        task_id=task_id,
        chars=240,
        body_chars=240,
        content_sha256=rec.sha256,
        incarnation_id=inc,
        activation_id=act,
        strike_epoch=strike_epoch,
    )
    observe_learning(
        store,
        rec.name,
        sha256=rec.sha256,
        task_id=task_id,
        outcome="adopted",
        source=LearningObservationSource(
            authority="model",
            run_id=run_id,
            incarnation_id=inc,
            activation_id=act,
            strike_epoch=strike_epoch,
        ),
    )


def _patch_maint(store: SkillLearningStore, name: str, *, remove_keys: tuple[str, ...] = (), **updates: object) -> None:
    with store.state_lock():
        reg = store.load_registry()
        rec = reg.skills[name]
        maint = dict(rec.maintenance)
        for key in remove_keys:
            maint.pop(key, None)
        maint.update(updates)
        store.save_registry(replace(reg, skills={**reg.skills, name: replace(rec, maintenance=maint)}))


def _maintain_once(store: SkillLearningStore, *, mode: str = "autonomous", now: float | None = None):
    return maintain_learning(
        store,
        mode=mode,
        cool_seconds=COOL_SECONDS,
        archive_seconds=ARCHIVE_SECONDS,
        evict_seconds=EVICT_SECONDS,
        max_skills=MAX_SKILLS,
        protection_complete=True,
        now=time.time() + 10.0 if now is None else now,
        batch_count=BATCH_ENTRIES,
        batch_ms=5000,
    )


def _converge(store: SkillLearningStore, *, now: float, max_passes: int = 25) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for _ in range(max_passes):
        report = _maintain_once(store, mode="autonomous", now=now)
        reports.append(report)
        if not report["backlog"]:
            return reports
    raise AssertionError(f"maintenance did not converge within {max_passes} passes")


# ---------------------------------------------------------------------------
# P1-02: 回滚与崩溃恢复必须在清空 observations 前保留 _historical_adoptions
# ---------------------------------------------------------------------------


def test_p1_02_manual_revert_preserves_historical_adoptions_and_avoids_b_false_strike(tmp_path: Path) -> None:
    home, repo, store = _store_with_runtime(tmp_path / "manual")
    _publish(store, "skill-a", suffix="v1")
    a_v1 = store.load_registry().skills["skill-a"]
    _publish(store, "skill-a", suffix="v2", update_from=a_v1)
    _publish(store, "skill-b", suffix="v1")

    a_v2 = store.load_registry().skills["skill-a"]
    b_v1 = store.load_registry().skills["skill-b"]

    for idx in range(1, 4):
        _deliver_and_adopt(store, a_v2, task_id=f"task-ab-{idx}", run_id=f"run-ab-{idx}")
        _deliver_and_adopt(store, b_v1, task_id=f"task-ab-{idx}", run_id=f"run-ab-{idx}")

    revert_learned_skill(store, "skill-a")
    reg_after = store.load_registry()
    a_reverted = reg_after.skills["skill-a"]

    assert a_reverted.version == 1
    assert a_reverted.sha256 == a_v1.sha256
    assert a_reverted.maintenance["observations"] == []
    assert a_reverted.maintenance["strike_epoch"] == 0
    assert a_reverted.maintenance["independent_uses_coverage"] == "exact"
    hist = a_reverted.maintenance["historical_adoptions"]
    assert {(item["run_id"], item["sha256"]) for item in hist} == {
        (f"run-ab-{idx}", a_v2.sha256) for idx in range(1, 4)
    }

    # 无目标确定性 pack failure 落在曾同时采纳 {A-v2, B} 的 run 上时，不能误击打 B。
    agent_ab = _agent_for(home, repo, "run-ab-1", task_id="task-ab-1")
    observe_pack_verification(agent_ab, agent_ab._current_run_params, SimpleNamespace(status="failed", valid=False), "p1")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 0

    # 纯 B 运行上的无目标确定性 pack failure 必须精确击打 B 1 次。
    _deliver_and_adopt(store, b_v1, task_id="task-b-only", run_id="run-b-only")
    agent_b = _agent_for(home, repo, "run-b-only", task_id="task-b-only")
    observe_pack_verification(agent_b, agent_b._current_run_params, SimpleNamespace(status="failed", valid=False), "p2")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 1


def test_p1_02_recover_lifecycle_rollback_preserves_historical_adoptions_and_avoids_b_false_strike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, store = _store_with_runtime(tmp_path / "recover")
    _publish(store, "skill-a", suffix="v1")
    a_v1 = store.load_registry().skills["skill-a"]
    _publish(store, "skill-a", suffix="v2", update_from=a_v1)
    _publish(store, "skill-b", suffix="v1")

    a_v2 = store.load_registry().skills["skill-a"]
    b_v1 = store.load_registry().skills["skill-b"]

    for idx in range(1, 4):
        _deliver_and_adopt(store, a_v2, task_id=f"task-rec-{idx}", run_id=f"run-rec-{idx}")
        _deliver_and_adopt(store, b_v1, task_id=f"task-rec-{idx}", run_id=f"run-rec-{idx}")

    real_fsync = store_module.os.fsync
    live_path = store.skill_path("skill-a", record=a_v2)

    def _fail_post_replace_sync(fd: int) -> None:
        real_fsync(fd)
        if live_path.exists() and skill_content_sha256(live_path.read_text(encoding="utf-8")) == a_v1.sha256:
            raise OSError("simulated I/O error during parent sync")

    monkeypatch.setattr(store_module.os, "fsync", _fail_post_replace_sync)
    with pytest.raises(OSError, match="simulated I/O error"):
        revert_learned_skill(store, "skill-a")

    pending = store.load_registry().pending_operation
    assert pending is not None and pending["kind"] == "rollback"

    monkeypatch.setattr(store_module.os, "fsync", real_fsync)
    recovery_report = store.recover_lifecycle()
    assert recovery_report["status"] == "recovered"
    recovered = store.load_registry()
    assert recovered.pending_operation is None
    a_recovered = recovered.skills["skill-a"]
    assert a_recovered.version == 1
    assert a_recovered.sha256 == a_v1.sha256
    assert a_recovered.maintenance["observations"] == []
    assert a_recovered.maintenance["strike_epoch"] == 0
    assert a_recovered.maintenance["independent_uses_coverage"] == "exact"
    assert {(item["run_id"], item["sha256"]) for item in a_recovered.maintenance["historical_adoptions"]} == {
        (f"run-rec-{idx}", a_v2.sha256) for idx in range(1, 4)
    }

    agent_ab = _agent_for(home, repo, "run-rec-2", task_id="task-rec-2")
    observe_pack_verification(agent_ab, agent_ab._current_run_params, SimpleNamespace(status="failed", valid=False), "p1")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 0

    _deliver_and_adopt(store, b_v1, task_id="task-b-only-rec", run_id="run-b-only-rec")
    agent_b = _agent_for(home, repo, "run-b-only-rec", task_id="task-b-only-rec")
    observe_pack_verification(agent_b, agent_b._current_run_params, SimpleNamespace(status="failed", valid=False), "p2")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 1


def test_p1_02_proven_stable_rollback_on_third_strike_preserves_historical_adoptions(tmp_path: Path) -> None:
    home, repo, store = _store_with_runtime(tmp_path / "stable_rb")
    _publish(store, "skill-c", suffix="v1")
    c_v1 = store.load_registry().skills["skill-c"]
    for idx in range(1, 4):
        _deliver_and_adopt(store, c_v1, task_id=f"c-v1-{idx}", run_id=f"rc-v1-{idx}")
    _maintain_once(store)
    c_v1_est = store.load_registry().skills["skill-c"]
    assert c_v1_est.maintenance["quality"] == "established"

    _publish(store, "skill-c", suffix="v2", update_from=c_v1_est)
    c_v2 = store.load_registry().skills["skill-c"]
    _deliver_and_adopt(store, c_v2, task_id="c-v2-1", run_id="rc-v2-1")
    _deliver_and_adopt(store, c_v2, task_id="c-v2-2", run_id="rc-v2-2")

    for strike in range(1, 4):
        neg_task = f"c-v2-neg-{strike}"
        _deliver_and_adopt(store, c_v2, task_id=neg_task, run_id=f"rc-v2-neg-{strike}", epoch=strike - 1)
        observe_learning(
            store,
            "skill-c",
            sha256=c_v2.sha256,
            task_id=neg_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"rcpt-neg-{strike}",
                incarnation_id=str(c_v2.maintenance["incarnation_id"]),
                activation_id=str(c_v2.maintenance["activation_id"]),
                strike_epoch=strike - 1,
            ),
        )

    c_restored = store.load_registry().skills["skill-c"]
    assert c_restored.version == 1
    assert c_restored.maintenance["quality"] == "established"
    assert c_restored.maintenance["strike_epoch"] == 0
    assert c_restored.maintenance["observations"] == []
    assert {
        (item["run_id"], item["sha256"]) for item in c_restored.maintenance["historical_adoptions"]
    } >= {("rc-v1-1", c_v1.sha256), ("rc-v2-1", c_v2.sha256), ("rc-v2-2", c_v2.sha256)}


def test_p1_02_p1_04_proven_stable_revert_and_recover_preserve_adoptions_and_reset_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖 revert_learned_skill 与 _recover_rollback_locked 的 proven-stable 分支。"""
    home, _repo, store = _store_with_runtime(tmp_path / "stable_revert")
    del home
    for tag in ("direct", "crash"):
        name = f"skill-{tag}"
        _publish(store, name, suffix="v1")
        v1 = store.load_registry().skills[name]
        for idx in range(1, 4):
            _deliver_and_adopt(store, v1, task_id=f"{tag}-v1-{idx}", run_id=f"r-{tag}-v1-{idx}")
        _maintain_once(store)
        v1_est = store.load_registry().skills[name]
        assert v1_est.maintenance["quality"] == "established"

        _publish(store, name, suffix="v2", update_from=v1_est)
        v2 = store.load_registry().skills[name]
        _deliver_and_adopt(store, v2, task_id=f"{tag}-v2-1", run_id=f"r-{tag}-v2-1")
        # 先在 v2 上制造 1 次打击让 v2.strike_epoch 推进到 1，再执行回滚。
        observe_learning(
            store,
            name,
            sha256=v2.sha256,
            task_id=f"{tag}-v2-1",
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"rcpt-{tag}-1",
                incarnation_id=str(v2.maintenance["incarnation_id"]),
                activation_id=str(v2.maintenance["activation_id"]),
                strike_epoch=0,
            ),
        )
        assert store.load_registry().skills[name].maintenance["strike_epoch"] == 1

        if tag == "direct":
            revert_learned_skill(store, name)
        else:
            real_fsync = store_module.os.fsync
            live_path = store.skill_path(name, record=v2)

            def _fail_sync(fd: int, *, _real=real_fsync, _path=live_path, _sha=v1.sha256) -> None:
                _real(fd)
                if _path.exists() and skill_content_sha256(_path.read_text(encoding="utf-8")) == _sha:
                    raise OSError("simulated I/O error during stable revert sync")

            monkeypatch.setattr(store_module.os, "fsync", _fail_sync)
            with pytest.raises(OSError, match="simulated I/O error"):
                revert_learned_skill(store, name)
            monkeypatch.setattr(store_module.os, "fsync", real_fsync)
            assert store.recover_lifecycle()["status"] == "recovered"

        restored = store.load_registry().skills[name]
        assert restored.version == 1
        assert restored.maintenance["quality"] == "established"
        assert restored.maintenance["quality_basis"] == "rollback_established"
        assert restored.maintenance["strike_epoch"] == 0
        assert restored.maintenance["observations"] == []
        assert (f"r-{tag}-v2-1", v2.sha256) in {
            (item["run_id"], item["sha256"]) for item in restored.maintenance["historical_adoptions"]
        }


# ---------------------------------------------------------------------------
# P1-03: 遗留 learned 记录在写授权 ownership 维护内补齐绑定，读路径/observe 不造 ID
# ---------------------------------------------------------------------------


def test_p1_03_legacy_record_binding_only_in_write_maintenance_and_end_to_end_lifecycle(tmp_path: Path) -> None:
    hp, repo, store = _make_owner_home(tmp_path)
    _publish(store, "legacy-unused", suffix="v1")
    _publish(store, "legacy-used", suffix="v1")

    now = time.time() + 30.0
    hist_sample = [{"authority": "model", "outcome": "adopted", "run_id": "old-run", "sha256": "a" * 64}]
    obs_sample = [{"authority": "model", "outcome": "adopted", "run_id": "cur-run", "sha256": "b" * 64}]
    _patch_maint(
        store,
        "legacy-unused",
        remove_keys=("incarnation_id", "activation_id", "independent_uses", "uses_since_hurt"),
        ownership="machine",
        state="trial",
        quality="trial",
        baseline_at=now - 500.0,
        independent_uses_coverage="unknown",
    )
    _patch_maint(
        store,
        "legacy-used",
        incarnation_id="",
        activation_id="   ",
        ownership="machine",
        state="active",
        quality="trial",
        baseline_at=now - 1000.0,
        ever_used=True,
        last_used_at=now - 100.0,
        pinned=True,
        observations=obs_sample,
        historical_adoptions=hist_sample,
        independent_uses_coverage="unknown",
    )

    # 只读快照、usage binding 提取与 observe 模式严禁造 ID 或改写登记。
    svc = skill_service.SkillsService(
        home_paths=hp,
        workspace_root=tmp_path / "ws",
        policy_provider=lambda: resolve_effective_owner_policy(hp),
        learning_recommended_limit_provider=lambda: 50,
    )
    snap = freeze_learned_snapshot(svc.snapshot_for(), hp)
    dummy_agent = SimpleNamespace(home_paths=hp, runtime_db=repo)
    for skill_name in ("legacy-unused", "legacy-used"):
        entry = next(item for item in snap.entries if item.name == skill_name)
        assert "_learning_usage_binding" not in entry.asset_fields
        assert "incarnation_id" not in learning_usage_binding(dummy_agent, entry)

    observe_report = _maintain_once(store, mode="observe", now=now)
    assert observe_report["changed"] == []
    reg_after_observe = store.load_registry()
    assert not str(reg_after_observe.skills["legacy-unused"].maintenance.get("incarnation_id") or "").strip()
    assert not str(reg_after_observe.skills["legacy-used"].maintenance.get("activation_id") or "").strip()

    # 写授权自主维护在 ownership 阶段补齐缺失 ID 并保留既有历史边界。
    write_report = _maintain_once(store, mode="autonomous", now=now)
    assert {"legacy-unused", "legacy-used"} <= set(write_report["changed"])

    reg_bound = store.load_registry()
    unused_rec = reg_bound.skills["legacy-unused"]
    used_rec = reg_bound.skills["legacy-used"]

    unused_inc = unused_rec.maintenance["incarnation_id"]
    unused_act = unused_rec.maintenance["activation_id"]
    used_inc = used_rec.maintenance["incarnation_id"]
    used_act = used_rec.maintenance["activation_id"]
    assert len(unused_inc) == 32 and len(unused_act) == 32
    assert len(used_inc) == 32 and len(used_act) == 32
    assert unused_rec.maintenance["independent_uses_coverage"] == "exact"
    assert used_rec.maintenance["independent_uses_coverage"] == "lower_bound"
    assert used_rec.maintenance["ever_used"] is True
    assert used_rec.maintenance["last_used_at"] == now - 100.0
    assert used_rec.maintenance["pinned"] is True
    assert used_rec.maintenance["observations"] == obs_sample
    assert used_rec.maintenance["historical_adoptions"] == hist_sample
    assert used_rec.maintenance["state"] == "active"
    assert used_rec.maintenance["quality"] == "trial"
    assert used_rec.maintenance["baseline_at"] == now - 1000.0
    assert unused_rec.maintenance["independent_uses"] == 0
    assert unused_rec.maintenance["uses_since_hurt"] == 0
    assert unused_rec.maintenance["hurt_count"] == 0
    assert unused_rec.maintenance["strike_epoch"] == 0

    # 再次维护必须保持 ID 稳定不变。
    _maintain_once(store, mode="autonomous", now=now + 5.0)
    reg_stable = store.load_registry()
    assert reg_stable.skills["legacy-unused"].maintenance["incarnation_id"] == unused_inc
    assert reg_stable.skills["legacy-unused"].maintenance["activation_id"] == unused_act
    assert reg_stable.skills["legacy-used"].maintenance["incarnation_id"] == used_inc
    assert reg_stable.skills["legacy-used"].maintenance["activation_id"] == used_act

    # 绑定后 3 次全新独立正文投递建立质量，随后第 3 次宿主负向反馈按合同退场封禁。
    for idx in range(1, 4):
        record_skill_usage_event(
            store,
            event="manual_get",
            skill_id="owner:legacy-unused",
            source="learned",
            kind="skill",
            task_id=f"leg-use-{idx}",
            chars=240,
            body_chars=240,
            content_sha256=unused_rec.sha256,
            incarnation_id=unused_inc,
            activation_id=unused_act,
            strike_epoch=0,
        )
    _maintain_once(store, mode="autonomous", now=now + 10.0)
    established_leg = store.load_registry().skills["legacy-unused"]
    assert established_leg.maintenance["quality"] == "established"
    assert established_leg.maintenance["state"] == "active"

    for strike in range(1, 4):
        neg_task = f"leg-neg-{strike}"
        record_skill_usage_event(
            store,
            event="manual_get",
            skill_id="owner:legacy-unused",
            source="learned",
            kind="skill",
            task_id=neg_task,
            chars=240,
            body_chars=240,
            content_sha256=unused_rec.sha256,
            incarnation_id=unused_inc,
            activation_id=unused_act,
            strike_epoch=strike - 1,
        )
        observe_learning(
            store,
            "legacy-unused",
            sha256=unused_rec.sha256,
            task_id=neg_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"leg-rcpt-{strike}",
                incarnation_id=unused_inc,
                activation_id=unused_act,
                strike_epoch=strike - 1,
            ),
        )
    final_reg = store.load_registry()
    assert "legacy-unused" not in final_reg.skills
    assert "legacy-unused" in final_reg.blocked_names


@pytest.mark.timeout(180)
def test_p1_03_275_legacy_skills_convergence_binds_survivors_and_keeps_known_used(tmp_path: Path) -> None:
    _home, _repo, store = _store_with_runtime(tmp_path / "conv275")
    names = [f"leg-{index:03d}" for index in range(275)]
    now = time.time() + 60.0

    # 先创建 275 个未绑定遗留技能，其中前 22 条带已知历史使用标记。
    for idx, name in enumerate(names):
        _publish(store, name)
        extra_updates: dict[str, object] = {
            "ownership": "machine",
            "baseline_at": now - (275 - idx) * 10.0,
            "ever_used": idx < 22,
            "independent_uses_coverage": "unknown",
        }
        if idx < 22:
            extra_updates["last_used_at"] = now - 3600.0
        _patch_maint(
            store,
            name,
            remove_keys=("incarnation_id", "activation_id", "independent_uses", "uses_since_hurt", "last_used_at"),
            **extra_updates,
        )

    _converge(store, now=now, max_passes=25)
    reg = store.load_registry()
    live = {
        name
        for name, rec in reg.skills.items()
        if rec.layout != "owner"
        and rec.maintenance.get("state") != "archived"
        and rec.maintenance.get("ownership") not in {"owner_layout", "user_modified"}
    }
    known_used = set(names[:22])
    assert len(live) == MAX_SKILLS
    assert known_used <= live

    # 所有经所有权核验为 machine 托管的存活与归档记录均须完成稳定 ID 绑定。
    machine_records = [
        rec
        for rec in reg.skills.values()
        if rec.layout != "owner" and rec.maintenance.get("ownership") == "machine"
    ]
    assert len(machine_records) == 275
    for rec in machine_records:
        assert str(rec.maintenance.get("incarnation_id") or "").strip()
        assert str(rec.maintenance.get("activation_id") or "").strip()

    first_ids = {rec.name: (rec.maintenance["incarnation_id"], rec.maintenance["activation_id"]) for rec in machine_records}
    _converge(store, now=now + 1.0, max_passes=25)
    reg2 = store.load_registry()
    for name, pair in first_ids.items():
        rec2 = reg2.skills[name]
        assert (rec2.maintenance["incarnation_id"], rec2.maintenance["activation_id"]) == pair


# ---------------------------------------------------------------------------
# P1-04: 回滚重置 strike_epoch=0 及回执 epoch 严格相等
# ---------------------------------------------------------------------------


def test_p1_04_rollback_resets_strike_epoch_and_enforces_exact_epoch_equality(tmp_path: Path) -> None:
    _home, repo, store = _store_with_runtime(tmp_path / "epoch_rb")
    _publish(store, "epoch-skill", suffix="v1")
    v1 = store.load_registry().skills["epoch-skill"]
    for idx in range(1, 4):
        _deliver_and_adopt(store, v1, task_id=f"e-v1-{idx}", run_id=f"re-v1-{idx}")
    _maintain_once(store)
    v1_est = store.load_registry().skills["epoch-skill"]
    assert v1_est.maintenance["quality"] == "established"

    _publish(store, "epoch-skill", suffix="v2", update_from=v1_est)
    v2 = store.load_registry().skills["epoch-skill"]
    for strike in range(1, 4):
        neg_task = f"e-v2-neg-{strike}"
        _deliver_and_adopt(store, v2, task_id=neg_task, run_id=f"re-v2-neg-{strike}", epoch=strike - 1)
        observe_learning(
            store,
            "epoch-skill",
            sha256=v2.sha256,
            task_id=neg_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"e-v2-rcpt-{strike}",
                incarnation_id=str(v2.maintenance["incarnation_id"]),
                activation_id=str(v2.maintenance["activation_id"]),
                strike_epoch=strike - 1,
            ),
        )

    restored = store.load_registry().skills["epoch-skill"]
    assert restored.version == 1
    assert restored.maintenance["quality"] == "established"
    assert restored.maintenance["hurt_count"] == 0
    assert restored.maintenance["uses_since_hurt"] == 0
    assert restored.maintenance["strike_epoch"] == 0

    # 恢复后的 v1 受 1 次负反馈：hurt_count=1, strike_epoch 0 -> 1, quality 降为 trial。
    _deliver_and_adopt(store, restored, task_id="e-restored-neg-1", run_id="re-restored-neg-1", epoch=0)
    observe_learning(
        store,
        "epoch-skill",
        sha256=restored.sha256,
        task_id="e-restored-neg-1",
        outcome="ineffective",
        source=LearningObservationSource(
            authority="host",
            receipt_id="e-restored-rcpt-1",
            incarnation_id=str(restored.maintenance["incarnation_id"]),
            activation_id=str(restored.maintenance["activation_id"]),
            strike_epoch=0,
        ),
    )
    after_s1 = store.load_registry().skills["epoch-skill"]
    assert after_s1.maintenance["hurt_count"] == 1
    assert after_s1.maintenance["strike_epoch"] == 1
    assert after_s1.maintenance["quality"] == "trial"

    # 4 次携带过期/错位 epoch=2 的延迟投递均不得计入 uses_since_hurt。
    for idx in range(1, 5):
        _deliver_and_adopt(store, after_s1, task_id=f"stale-e2-{idx}", run_id=f"r-stale-{idx}", epoch=2)
    _maintain_once(store)
    after_stale = store.load_registry().skills["epoch-skill"]
    assert after_stale.maintenance["uses_since_hurt"] == 0
    assert after_stale.maintenance["quality"] == "trial"
    agg = read_usage_aggregate(
        repo,
        incarnation_id=str(after_stale.maintenance["incarnation_id"]),
        activation_id=str(after_stale.maintenance["activation_id"]),
        content_sha256=after_stale.sha256,
    )
    assert agg is not None and agg["uses_since_hurt"] == 0

    # 当前 epoch=1 严格遵循 3 + hurt_count = 4 阶梯升级。
    for idx in range(1, 4):
        _deliver_and_adopt(store, after_stale, task_id=f"cur-e1-{idx}", run_id=f"r-e1-{idx}", epoch=1)
    _maintain_once(store)
    mid_e1 = store.load_registry().skills["epoch-skill"]
    assert mid_e1.maintenance["uses_since_hurt"] == 3
    assert mid_e1.maintenance["quality"] == "trial"

    _deliver_and_adopt(store, mid_e1, task_id="cur-e1-4", run_id="r-e1-4", epoch=1)
    _maintain_once(store)
    re_est = store.load_registry().skills["epoch-skill"]
    assert re_est.maintenance["uses_since_hurt"] == 4
    assert re_est.maintenance["quality"] == "established"

    # 第 2 次负反馈后 strike_epoch 1 -> 2，需 3 + 2 = 5 次当前 epoch=2 使用方能恢复 established。
    _deliver_and_adopt(store, re_est, task_id="e-restored-neg-2", run_id="re-restored-neg-2", epoch=1)
    observe_learning(
        store,
        "epoch-skill",
        sha256=re_est.sha256,
        task_id="e-restored-neg-2",
        outcome="ineffective",
        source=LearningObservationSource(
            authority="host",
            receipt_id="e-restored-rcpt-2",
            incarnation_id=str(re_est.maintenance["incarnation_id"]),
            activation_id=str(re_est.maintenance["activation_id"]),
            strike_epoch=1,
        ),
    )
    after_s2 = store.load_registry().skills["epoch-skill"]
    assert after_s2.maintenance["hurt_count"] == 2
    assert after_s2.maintenance["strike_epoch"] == 2
    assert after_s2.maintenance["quality"] == "trial"

    for idx in range(1, 5):
        _deliver_and_adopt(store, after_s2, task_id=f"cur-e2-{idx}", run_id=f"r-e2-{idx}", epoch=2)
    _maintain_once(store)
    assert store.load_registry().skills["epoch-skill"].maintenance["uses_since_hurt"] == 4
    assert store.load_registry().skills["epoch-skill"].maintenance["quality"] == "trial"

    _deliver_and_adopt(store, after_s2, task_id="cur-e2-5", run_id="r-e2-5", epoch=2)
    _maintain_once(store)
    assert store.load_registry().skills["epoch-skill"].maintenance["uses_since_hurt"] == 5
    assert store.load_registry().skills["epoch-skill"].maintenance["quality"] == "established"


def test_p1_04_duplicate_same_task_receipt_same_vs_next_and_future_epoch(tmp_path: Path) -> None:
    _home, repo, store = _store_with_runtime(tmp_path / "dup_epoch")
    _publish(store, "dup-skill", suffix="v1")
    rec = store.load_registry().skills["dup-skill"]
    inc = str(rec.maintenance["incarnation_id"])
    act = str(rec.maintenance["activation_id"])

    # 1) epoch=0 首次计数
    first = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 0,
            "at": 1000.0,
        },
        runtime=repo,
    )
    assert first["status"] == "counted"
    assert first["uses_since_hurt_incremented"] is True
    assert first["aggregate"]["uses_since_hurt"] == 1

    # 2) 同 epoch=0 同任务重复回执不增 uses_since_hurt 与 independent_uses
    dup_same = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 0,
            "at": 1001.0,
        },
        runtime=repo,
    )
    assert dup_same["status"] == "duplicate_refreshed"
    assert dup_same["independent_uses_incremented"] is False
    assert dup_same["uses_since_hurt_incremented"] is False
    assert dup_same["aggregate"]["uses_since_hurt"] == 1
    assert dup_same["aggregate"]["independent_uses"] == 1

    # 3) 负反馈推动 strike_epoch 0 -> 1，uses_since_hurt 清零
    neg = record_exact_negative(
        store,
        incarnation_id=inc,
        activation_id=act,
        content_sha256=rec.sha256,
        task_id="neg-1",
        receipt_id="rcpt-1",
        at=1002.0,
        runtime=repo,
    )
    assert neg["strike_epoch"] == 1

    # 4) 同任务携带错位未来 epoch=2 严禁记入新轮 uses_since_hurt
    dup_future = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 2,
            "at": 1003.0,
        },
        runtime=repo,
    )
    assert dup_future["uses_since_hurt_incremented"] is False
    assert dup_future["aggregate"]["uses_since_hurt"] == 0

    # 5) 同任务在精确匹配当前 epoch=1 时允许在新周期记分 1 次，再次重复不再增加
    dup_next = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 1,
            "at": 1004.0,
        },
        runtime=repo,
    )
    assert dup_next["status"] == "counted_new_epoch"
    assert dup_next["independent_uses_incremented"] is False
    assert dup_next["uses_since_hurt_incremented"] is True
    assert dup_next["aggregate"]["uses_since_hurt"] == 1

    dup_next_again = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 1,
            "at": 1005.0,
        },
        runtime=repo,
    )
    assert dup_next_again["uses_since_hurt_incremented"] is False
    assert dup_next_again["aggregate"]["uses_since_hurt"] == 1


# =============================================================================
# Source: agent_py_agent/tests/test_sm_r2_scope_projection_paging.py
# =============================================================================

def _seed_migrated_records(
    tmp_path: Path,
    count: int,
    *,
    scope_by_index,
    content_fn=None,
    expires_by_index=None,
) -> tuple[JsonlMemory, list[MemoryRecord]]:
    memory = JsonlMemory(tmp_path / "memory" / "long_term.jsonl")
    records: list[MemoryRecord] = []
    for index in range(count):
        scope_type, scope_key = scope_by_index(index)
        content = content_fn(index) if content_fn is not None else f"f{index}"
        expires_at = expires_by_index(index) if expires_by_index is not None else 0.0
        records.append(
            MemoryRecord(
                role="user",
                entry_id=f"mem-{index:04d}",
                kind="fact",
                content=content,
                created_at=float(index + 1),
                updated_at=float(index + 1),
                last_accessed_at=float(index + 1),
                expires_at=expires_at,
                attributes={
                    "origin": "user_explicit",
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                },
            )
        )
    memory.path.write_text("".join(row.to_json() + "\n" for row in records), encoding="utf-8")
    memory.migrate_tiers()
    return memory, records


def test_scope_pairs_1001_records_two_pages_degrades_then_completes_and_warm_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _scope_for(index: int) -> tuple[str, str]:
        if index < 999:
            return ("company", "company:acme") if index % 2 else ("project", "project:seg0")
        if index == 999:
            return ("project", "project:seg0_end")
        return ("project", "project:seg1_tail")

    memory, _records = _seed_migrated_records(tmp_path, 1001, scope_by_index=_scope_for)
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None
    assert [seg["count"] for seg in manifest["segments"]] == [1000, 1]

    scan_logs: list[tuple[object, dict[str, object]]] = []
    orig_scan = indexing_module.bounded_authority_records

    def spy_scan(mem, *, max_records, max_bytes, continuation=None):
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        scan_logs.append((continuation, cov))
        return rows, cov

    monkeypatch.setattr(indexing_module, "bounded_authority_records", spy_scan)

    # 第 1 次调用：达到单页预算即中断并降级，绝不对外发布残缺 project 范围。
    degraded_scope = runtime_long_term_scope(memory, task_id="base_task")
    assert ("project", "project:base_task") in degraded_scope.keys
    assert ("project", "project:seg0") not in degraded_scope.keys
    assert ("project", "project:seg1_tail") not in degraded_scope.keys
    assert len(scan_logs) == 1
    assert scan_logs[0][0] is None
    assert scan_logs[0][1]["partial"] is True
    assert scan_logs[0][1]["scanned_records"] <= _AUTHORITY_SCAN_MAX_RECORDS
    assert scan_logs[0][1]["continuation"]["cursor"] == [1, 0]
    assert isinstance(memory._scope_pairs_cache, OrderedDict)
    assert len(memory._scope_pairs_cache["project"]) == 4

    # 在新实例上直接验证第 1 次抛错语义，第 2 次续扫完成全集返回。
    memory2 = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory2.scope_pairs("project")
    completed_pairs = memory2.scope_pairs("project")
    expected_pairs = (
        ("project", "project:seg0"),
        ("project", "project:seg0_end"),
        ("project", "project:seg1_tail"),
    )
    assert completed_pairs == expected_pairs
    assert scan_logs[-1][0] == {"operation_id": manifest["operation_id"], "cursor": [1, 0]}
    assert scan_logs[-1][1]["partial"] is False

    # 第 3 次调用：命中热缓存，0 次权威扫描、0 次分片 stat、禁止 deepcopy/all()。
    scans_before_warm = len(scan_logs)
    stat_calls: list[str] = []
    orig_stat = Path.stat

    def spy_stat(self, *args, **kwargs):
        stat_calls.append(str(self))
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(
        jsonl_module,
        "deepcopy",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm hit")),
    )
    monkeypatch.setattr(
        copy,
        "deepcopy",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm hit")),
    )
    monkeypatch.setattr(
        JsonlMemory,
        "all",
        lambda _self: (_ for _ in ()).throw(AssertionError("all() forbidden on warm hit")),
    )

    warm_pairs = memory2.scope_pairs("project")
    assert warm_pairs is completed_pairs
    assert len(scan_logs) == scans_before_warm
    assert not any("long_term.jsonl.generations" in path for path in stat_calls)


def test_scope_pairs_3001_records_four_segments_converges_and_preserves_scope_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _scope_for(index: int) -> tuple[str, str]:
        if index == 0:
            return ("project", "project:seg0")
        if index == 1500:
            return ("project", "project:seg1")
        if index == 2500:
            return ("project", "project:seg2")
        if index == 3000:
            return ("project", "project:seg3_tail")
        if index % 5 == 0:
            return ("global", "global")
        return ("company", "company:other")

    memory, records = _seed_migrated_records(tmp_path, 3001, scope_by_index=_scope_for)
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None
    assert [seg["count"] for seg in manifest["segments"]] == [1000, 1000, 1000, 1]

    from agent_py_agent.agent.common import nofollow_fs

    page_stats: list[tuple[object, dict[str, object], list[tuple[str, ...]]]] = []
    orig_scan = indexing_module.bounded_authority_records
    orig_read = nofollow_fs.read_bytes_beneath
    current_reads: list[tuple[str, ...]] = []

    def spy_read(root, parts, *args, **kwargs):
        current_reads.append(tuple(parts))
        return orig_read(root, parts, *args, **kwargs)

    def spy_scan(mem, *, max_records, max_bytes, continuation=None):
        current_reads.clear()
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        page_stats.append((continuation, cov, list(current_reads)))
        return rows, cov

    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", spy_read)
    monkeypatch.setattr(indexing_module, "bounded_authority_records", spy_scan)

    for expected_cursor in ([1, 0], [2, 0], [3, 0]):
        with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
            memory.scope_pairs("project")
        assert page_stats[-1][1]["partial"] is True
        assert page_stats[-1][1]["continuation"]["cursor"] == expected_cursor

    pairs = memory.scope_pairs("project")
    assert pairs == (
        ("project", "project:seg0"),
        ("project", "project:seg1"),
        ("project", "project:seg2"),
        ("project", "project:seg3_tail"),
    )
    assert len(page_stats) == 4
    assert page_stats[-1][1]["partial"] is False
    seg_names = [Path(seg["path"]).name for seg in manifest["segments"]]
    for page_idx, (_cont, cov, reads) in enumerate(page_stats):
        assert cov["examined_records"] <= _AUTHORITY_SCAN_MAX_RECORDS
        assert cov["scanned_records"] <= _AUTHORITY_SCAN_MAX_RECORDS
        assert cov["scanned_bytes"] <= _AUTHORITY_SCAN_MAX_BYTES
        read_leafs = {parts[-1] for parts in reads if parts}
        for prior_name in seg_names[:page_idx]:
            assert prior_name not in read_leafs
        assert seg_names[page_idx] in read_leafs

    # 普通对话恢复全部跨分片项目召回，同时严守其它 scope 边界与直接按 ID 读。
    ordinary_scope = runtime_long_term_scope(memory)
    assert long_term_record_matches_scope(records[0], ordinary_scope)
    assert long_term_record_matches_scope(records[3000], ordinary_scope)
    assert long_term_record_matches_scope(records[5], ordinary_scope)
    assert not long_term_record_matches_scope(records[1], ordinary_scope)

    explicit_scope = MemoryRecallScope.from_runtime(task_id="only_here")
    assert explicit_scope.allows("project", "project:only_here")
    assert not explicit_scope.allows("project", "project:seg3_tail")

    frozen_ctx = BusinessExecutionContext(
        "owner", "space-A", "biz-alpha", "task-A", 1, None, (), (), "business-context.v1"
    )
    frozen_scope = runtime_long_term_scope(memory, task_id="task-A", business_context=frozen_ctx)
    assert frozen_scope.allows("project", "project:biz-alpha")
    assert frozen_scope.allows("project", "project:task-A")
    assert frozen_scope.allows("global", "global")
    assert frozen_scope.allows("personal", "personal")
    assert not frozen_scope.allows("project", "project:seg0")
    assert not frozen_scope.allows("project", "project:seg3_tail")

    while memory.rebuild_read_index(batch_size=1000)["status"] != "complete":
        pass
    loaded_tail = memory.read_ids([records[3000].entry_id])
    assert [row.entry_id for row in loaded_tail] == [records[3000].entry_id]


def test_mid_segment_offset_resumption_and_byte_limit_never_move_cursor_backwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 使用 40 字符内容触发 active/cold 分层，产生非 1000 整除的分片边界从而覆盖分片内偏移续扫。
    memory, _records = _seed_migrated_records(
        tmp_path,
        2501,
        scope_by_index=lambda i: ("project", f"project:seg_{i // 700}"),
        content_fn=lambda i: f"payload-{i:04d}-" + ("x" * 28),
    )
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None
    assert len(manifest["segments"]) >= 3

    cursors: list[list[int] | None] = []
    orig_scan = indexing_module.bounded_authority_records

    def spy_scan(mem, *, max_records, max_bytes, continuation=None):
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        cont = cov.get("continuation")
        cursors.append(cont["cursor"] if cont else None)
        return rows, cov

    monkeypatch.setattr(indexing_module, "bounded_authority_records", spy_scan)

    for _ in range(2):
        with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
            memory.scope_pairs("project")
    assert cursors[0] is not None and cursors[0][1] > 0
    assert cursors[1] is not None and tuple(cursors[1]) > tuple(cursors[0])

    pairs = memory.scope_pairs("project")
    assert pairs == tuple(("project", f"project:seg_{idx}") for idx in range(4))
    assert cursors[-1] is None

    # 字节上限触发在到达 resume_off 之前时，cursor 不得倒退到 [seg, 0]。
    seg_path = memory.path.parent / manifest["segments"][0]["path"]
    seg_bytes = seg_path.stat().st_size
    rows, cov = bounded_authority_records(
        memory,
        max_records=100,
        max_bytes=seg_bytes // 2,
        continuation={"operation_id": manifest["operation_id"], "cursor": [0, 5]},
    )
    assert rows == []
    assert cov["partial"] is True
    assert cov["continuation"]["cursor"] == [0, 5]


def test_scope_pairs_invalidates_partial_progress_on_delete_generation_change_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = 1_750_000_000.0
    fake_clock = [t0]
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])

    def _scope_for(index: int) -> tuple[str, str]:
        if index == 0:
            return ("project", "project:doomed")
        if index == 1:
            return ("project", "project:ephemeral")
        if index == 1002:
            return ("project", "project:survivor")
        return ("project", "project:steady")

    def _expiry_for(index: int) -> float:
        return t0 + 50.0 if index == 1 else 0.0

    memory, records = _seed_migrated_records(
        tmp_path,
        1003,
        scope_by_index=_scope_for,
        expires_by_index=_expiry_for,
    )

    # 1. 页间 canonical 删除使旧部分进度失效并从新代起点重扫。
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory.scope_pairs("project")
    assert "project" in memory._scope_pairs_cache

    memory.remove(records[0].entry_id)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory.scope_pairs("project")
    pairs_after_delete = memory.scope_pairs("project")
    assert ("project", "project:doomed") not in pairs_after_delete
    assert pairs_after_delete == (
        ("project", "project:ephemeral"),
        ("project", "project:steady"),
        ("project", "project:survivor"),
    )

    # 2. 页间时钟跨过最近 expires_at 时，旧部分进度立即失效，不再带出过期项目。
    sub_dir = tmp_path / "expiry_case"
    sub_records: list[MemoryRecord] = []
    # active: 500 条非过期 (~12KB) + 1 条临期 (t0+50)；cold: 1001 条非过期 (~25KB)，确保首页命中临期记录后仍跨页。
    for idx in range(1502):
        if idx < 500:
            s_key, exp, ts = "project:steady", 0.0, 10_000.0 + idx
        elif idx == 500:
            s_key, exp, ts = "project:ephemeral", t0 + 50.0, 20_000.0
        else:
            s_key, exp, ts = ("project:survivor" if idx == 1501 else "project:steady"), 0.0, float(idx)
        sub_records.append(
            MemoryRecord(
                role="user",
                entry_id=f"exp-{idx:04d}",
                kind="fact",
                content=f"exp-{idx:04d}-" + ("v" * 16),
                created_at=ts,
                updated_at=ts,
                last_accessed_at=ts,
                expires_at=exp,
                attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": s_key},
            )
        )
    memory2 = JsonlMemory(sub_dir / "memory" / "long_term.jsonl")
    memory2.path.write_text("".join(row.to_json() + "\n" for row in sub_records), encoding="utf-8")
    memory2.migrate_tiers()

    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory2.scope_pairs("project")
    cached_partial = memory2._scope_pairs_cache["project"]
    assert len(cached_partial) == 4
    assert cached_partial[1] == t0 + 50.0
    assert ("project", "project:ephemeral") in cached_partial[2]

    fake_clock[0] = t0 + 51.0
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory2.scope_pairs("project")
    assert ("project", "project:ephemeral") not in memory2._scope_pairs_cache["project"][2]
    pairs_after_expiry = memory2.scope_pairs("project")
    assert ("project", "project:ephemeral") not in pairs_after_expiry
    assert pairs_after_expiry == (
        ("project", "project:steady"),
        ("project", "project:survivor"),
    )

    # 3. 页间新增写入（换代）同样使旧部分进度失效，不会遗漏新增 scope。
    memory3 = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory3.scope_pairs("project")
    memory3.add(
        "user",
        "h",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:added_midway"},
    )
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory3.scope_pairs("project")
    pairs_after_add = memory3.scope_pairs("project")
    assert ("project", "project:added_midway") in pairs_after_add
    assert ("project", "project:survivor") in pairs_after_add

    # 4. 续扫末页完成瞬间若发生并发修订变更，不得混用两代结果，必须清缓存并抛不可用。
    memory4 = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory4.scope_pairs("project")
    assert len(memory4._scope_pairs_cache["project"]) == 4

    writer = JsonlMemory(memory.path)
    orig_scan = indexing_module.bounded_authority_records
    bumped = False

    def interleave_on_resume(mem, *, max_records, max_bytes, continuation=None):
        nonlocal bumped
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        if continuation is not None and not bumped:
            bumped = True
            writer.add(
                "user",
                "w",
                kind="fact",
                attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:concurrent"},
            )
        return rows, cov

    monkeypatch.setattr(indexing_module, "bounded_authority_records", interleave_on_resume)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory4.scope_pairs("project")
    assert bumped is True
    assert "project" not in memory4._scope_pairs_cache


def test_bounded_authority_records_backward_compatible_and_stale_continuation(tmp_path: Path) -> None:
    memory, records = _seed_migrated_records(
        tmp_path,
        6,
        scope_by_index=lambda i: ("project", f"project:p{i}"),
    )
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None

    # 1. 不传 continuation（旧调用方兼容）
    direct_rows, direct_cov = bounded_authority_records(memory, max_records=4, max_bytes=1024 * 1024)
    assert [row.entry_id for row in direct_rows] == [row.entry_id for row in records[:4]]
    assert direct_cov["partial"] is True
    assert direct_cov["continuation"] == {"operation_id": manifest["operation_id"], "cursor": [0, 4]}

    # 2. 传入已失效或格式非法的 continuation，均安全退回从 [0, 0] 扫描
    for bad_cont in (
        {"operation_id": "stale-op-id", "cursor": [0, 4]},
        {"operation_id": "", "cursor": [0, 2]},
        {"operation_id": manifest["operation_id"], "cursor": [-1, 0]},
        {"operation_id": manifest["operation_id"], "cursor": [True, 0]},
        {"operation_id": manifest["operation_id"], "cursor": [0]},
        "bad-continuation",
    ):
        rows, cov = bounded_authority_records(
            memory,
            max_records=4,
            max_bytes=1024 * 1024,
            continuation=bad_cont,
        )
        assert [row.entry_id for row in rows] == [row.entry_id for row in records[:4]]
        assert cov["continuation"] == {"operation_id": manifest["operation_id"], "cursor": [0, 4]}


# =============================================================================
# Source: agent_py_agent/tests/test_sm_rework_carrier_source.py
# =============================================================================

def _observe(candidates: CandidateService, *, trigger: str = "user_dialogue", label: str = "first"):
    return candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=(
                "# Procedure\n\n## Steps\n"
                "1. Read the source file.\n2. Validate the output before publication.\n" + label
            ),
            subject_key="source-check-" + label,
            scope=MemoryScope("project", "project:test", applies_when="Review the completed procedure"),
            origin="subagent_lesson",
            source_task_ids=("task-real",),
            source_run_ids=("run-real",),
            evidence_refs=({"kind": "tool_run", "ref_id": "run-real"},),
            learning_source=subagent_projection(
                trigger_kind=trigger, run_ids=("run-real",), task_ids=("task-real",)
            ),
        )
    )


@pytest.fixture
def carrier(tmp_path: Path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    store = SkillLearningStore.for_home(home)
    backend = _FakeBackend(_output())
    runtime = SkillLearningRuntime(
        backend=backend,
        snapshot_provider=lambda: SimpleNamespace(entries=()),
        owner_id="local:main",
        deployment_root=tmp_path / "deployment",
    )
    service = SkillLearningService(store, SkillLearningSettings(autonomous_verified=True), runtime)
    return SimpleNamespace(candidates=candidates, store=store, backend=backend, runtime=runtime, service=service)


def test_mixed_intake_refuses_scheduled_source_and_publishes_dialogue(carrier) -> None:
    scheduled = _observe(carrier.candidates, trigger="schedule", label="scheduled")
    dialogue = _observe(carrier.candidates, label="dialogue")

    result = carrier.service.enqueue_lesson_candidates([scheduled, dialogue])

    assert [(row["candidate_id"], row["code"]) for row in result["refused"]] == [
        (scheduled.candidate_id, "SKILL_CARRIER_INELIGIBLE")
    ]
    assert [row["candidate_id"] for row in result["queued"]] == [dialogue.candidate_id]
    assert not result["deferred"]
    assert len(carrier.store.pending_requests()) == 1
    assert carrier.service.run_pending().status == "published"
    assert len(carrier.backend.prompts) == 1
    assert len(carrier.store.load_registry().skills) == 1
    assert not carrier.store.pending_requests()


def test_intake_revalidates_canonical_candidate_instead_of_old_snapshot(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert carrier.candidates.redact_content(content_hashes={candidate.content_hash}) == 1

    result = carrier.service.enqueue_lesson_candidates([candidate])

    assert result["refused"][0]["code"] == "SKILL_CARRIER_INELIGIBLE"
    assert not result["queued"] and not result["deferred"]
    assert not carrier.store.pending_requests() and not carrier.backend.prompts


@pytest.mark.parametrize("legacy_id", [False, True], ids=["candidate-id", "legacy-id"])
def test_redacted_source_is_rejected_before_model_call(carrier, legacy_id: bool) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    if legacy_id:
        path = carrier.store.pending_requests()[0]
        request = carrier.store.read_request(path)
        request["source_candidate_id"] = request.pop("candidate_id")
        carrier.store.rewrite_request(path, request)
    assert carrier.candidates.redact_content(content_hashes={candidate.content_hash}) == 1

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


def test_redacted_during_verifier_is_rejected_at_commit(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    verified = []

    def verify_and_redact(publish, digest):
        proof = default_machine_promotion_verifier(publish, digest)
        verified.append(proof)
        assert carrier.candidates.redact_content(content_hashes={candidate.content_hash}) == 1
        return proof

    carrier.service.runtime = replace(carrier.runtime, promotion_verifier=verify_and_redact)
    result = carrier.service.run_pending()

    assert len(verified) == 1
    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert len(carrier.backend.prompts) == 1
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills
    assert not list(carrier.store.learned_root.glob("*/SKILL.md"))


def test_deleted_canonical_store_is_rejected_before_model_call(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    shutil.rmtree(carrier.candidates.path.parent)

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_PROPOSAL_SOURCE_MISSING")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


@pytest.mark.parametrize(
    "failure",
    [OSError("simulated I/O error"), CandidateStoreCorruptError("simulated source read failure")],
    ids=["io-error", "corrupt-store"],
)
def test_unreadable_source_defers_intake_and_preserves_request_until_recovery(carrier, failure) -> None:
    candidate = _observe(carrier.candidates)
    with patch.object(CandidateService, "get", side_effect=failure):
        intake = carrier.service.enqueue_lesson_candidates([candidate])
    assert intake["deferred"][0]["code"] == "SKILL_PROPOSAL_SOURCE_UNREADABLE"
    assert not intake["queued"] and not intake["refused"]
    assert not carrier.store.pending_requests()

    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    path = carrier.store.pending_requests()[0]
    before = path.read_bytes()
    with patch.object(CandidateService, "get", side_effect=failure):
        result = carrier.service.run_pending()

    assert (result.status, result.code) == ("failed", "SKILL_PROPOSAL_SOURCE_UNREADABLE")
    assert carrier.store.pending_requests() == [path]
    assert path.read_bytes() == before
    assert not carrier.backend.prompts and not carrier.store.load_registry().skills
    recovered = carrier.service.run_pending()
    assert recovered.status == "published"
    assert len(carrier.backend.prompts) == 1
    assert not carrier.store.pending_requests()


def test_candidate_content_hash_is_rechecked_before_model_call(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    path = carrier.store.pending_requests()[0]
    request = carrier.store.read_request(path)
    request["candidate_content_hash"] = "0" * 64
    carrier.store.rewrite_request(path, request)

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


def test_promotion_request_preserves_procedure_through_real_consumer(tmp_path: Path) -> None:
    owner, promotion, _conv, _thread, candidates, _lt, lessons, hot = _setup_owner(tmp_path)
    marker = "PROCEDURE_SENTINEL_731942"
    content = (
        "# 发布检查步骤\n## 适用场景\n执行版本发布与静态检查前。\n## 执行步骤\n"
        f"1. 检查 {marker}；\n2. 运行 `ruff check agent_py_agent`；\n3. 提交前确认文件白名单与状态。"
    )
    candidate = _observe_tool_lesson(candidates, content=content, subject_key="deploy.procedure")

    delegated = promotion.promote(candidate.candidate_id, automatic=True)

    assert (delegated.status, delegated.reason_code) == ("withdrawn", "DELEGATED_TO_SKILL_CARRIER")
    store = SkillLearningStore(owner / "data" / "skill_learning", owner / "skills")
    pending = store.pending_requests()
    assert len(pending) == 1
    request = store.read_request(pending[0])
    assert request["schema_version"] == REQUEST_SCHEMA_VERSION
    assert request["candidate_id"] == candidate.candidate_id
    assert request["candidate_content_hash"] == candidate.content_hash
    assert request["request_key"] == request_key(
        json.dumps(["subagent_lesson", candidate.candidate_id, candidate.content_hash], ensure_ascii=False)
    )
    assert request["tool_rounds"] is None
    assert request["tool_calls_total"] == len(candidate.source_tool_refs)
    assert request["tool_trace"] == [{"evidence_summary": "run_command"}]
    assert marker in request["user_prompt"] and marker in request["final_response"]
    assert not {"task", "result", "tool_summary", "source_candidate_id"} & request.keys()

    backend = _FakeBackend(_output())
    learning = SkillLearningService(
        store,
        SkillLearningSettings(autonomous_verified=True),
        SkillLearningRuntime(
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(entries=()),
            owner_id="owner-w8",
            deployment_root=tmp_path / "deployment",
        ),
    )
    result = learning.run_pending()

    assert result.status == "published"
    assert len(backend.prompts) == 1 and marker in backend.prompts[0]
    assert "ruff check agent_py_agent" in backend.prompts[0]
    assert not store.pending_requests() and len(store.load_registry().skills) == 1
    canonical = candidates.get(candidate.candidate_id)
    assert (canonical.status, canonical.review_note) == ("withdrawn", "DELEGATED_TO_SKILL_CARRIER")
    assert lessons.list() == [] and hot.list() == []
    assert not (owner / "data" / "skill_proposals").exists()


@pytest.mark.parametrize("scenario", ["Review the procedure", ""])
def test_shared_builder_is_bounded_and_preserves_recorded_evidence(monkeypatch, scenario: str) -> None:
    from agent_py_agent.agent.capability import skill_learning_request as requests

    monkeypatch.setattr(requests, "utc_now", lambda: "fixed-time")
    candidate = SimpleNamespace(
        candidate_id=" candidate-1 ",
        content_hash="a" * 64,
        scope={"scope_type": "project", "scope_key": "project:test", "applies_when": scenario,
               "password": "fixture-value"},
        source_task_ids=("", "task-first", "task-second"),
        source_run_ids=("", "run-first", "run-second"),
        source_tool_refs=({"tool": "read_file"}, {"operation_id": "op-first"}, {}, "ignored"),
        subject_key=" procedure ",
        content="Procedure details " + "x" * requests.TEXT_LIMIT_CHARS,
        learning_source={"can_learn": True, "password": "fixture-value"},
    )
    with (
        patch.object(Path, "open", side_effect=AssertionError("unexpected file read")),
        patch.object(CandidateService, "get", side_effect=AssertionError("unexpected candidate read")),
    ):
        request = requests.lesson_candidate_request(candidate)

    assert request["candidate_id"] == "candidate-1"
    assert request["request_key"] == request_key(
        json.dumps(["subagent_lesson", "candidate-1", candidate.content_hash], ensure_ascii=False)
    )
    assert (request["run_id"], request["task_id"], request["root_task_id"]) == (
        "run-first", "task-first", "task-first"
    )
    assert request["request_id"] == request["thread_id"] == request["trajectory_digest"] == ""
    assert request["tool_rounds"] is None and request["tool_calls_total"] == 3
    assert request["tool_trace"] == [{"evidence_summary": "read_file, op-first"}]
    assert request["trajectory_fingerprint"] == request["candidate_content_hash"] == candidate.content_hash
    assert request["created_at"] == "fixed-time" and request["attempts"] == 0
    assert request["used_skill_ids"] == [] and request["source"] == "subagent_lesson"
    assert request["schema_version"] == REQUEST_SCHEMA_VERSION
    assert len(request["user_prompt"]) <= requests.TEXT_LIMIT_CHARS
    assert len(request["final_response"]) == requests.TEXT_LIMIT_CHARS
    assert request["final_response"].endswith("…")
    if scenario:
        assert request["user_prompt"] == scenario
    else:
        assert request["user_prompt"].startswith("procedure: Procedure details")
        assert request["user_prompt"].endswith("…")
    assert request["lesson_scope"]["password"] == request["learning_source"]["password"] == "<redacted>"
    assert candidate.scope["password"] == candidate.learning_source["password"] == "fixture-value"
    assert not {"task", "result", "tool_summary", "source_candidate_id"} & request.keys()


def test_shared_builder_defaults_do_not_invent_tool_activity() -> None:
    from agent_py_agent.agent.capability.skill_learning_request import lesson_candidate_request

    request = lesson_candidate_request(SimpleNamespace(candidate_id="candidate-empty"))

    assert request["user_prompt"] == "candidate-empty"
    assert request["final_response"] == request["candidate_content_hash"] == ""
    assert request["tool_rounds"] is None
    assert request["tool_calls_total"] == 0 and request["tool_trace"] == []
    assert request["lesson_scope"] == request["learning_source"] == {}
    assert request["run_id"] == request["task_id"] == request["root_task_id"] == ""


@pytest.mark.parametrize("change", ["excluded", "withdrawn"])
def test_later_exclusion_or_unrelated_withdrawal_is_rejected(carrier, change: str) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    if change == "excluded":
        assert carrier.candidates.mark_promotion_excluded(
            [candidate.candidate_id], reason="source-excluded"
        ) == 1
    else:
        carrier.candidates.transition(
            candidate, "withdrawn", reviewer="test-reviewer", review_note="withdrawn-by-review"
        )

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


# =============================================================================
# Source: agent_py_agent/tests/test_sm_rework_config_scope.py
# =============================================================================

_DECISION_ACTIONS = {
    "decision_read", "decision_patch", "decision_reset",
    "decision_models", "decision_probe",
}
_RESTRICTED_CONTEXTS = (
    "runtime", "legacy", "task_local", "control_plane", "isolated", "agent_run_id", "combined",
)


@pytest.fixture
def local_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    config_dir = home / "config"
    config_dir.mkdir()
    path = config_dir / "desktop.yaml"
    path.write_text(
        "enable_self_learning: true\nself_learning_max_skills: 50\nmemory_lesson_archive_days: 15\n",
        encoding="utf-8",
    )
    config = load_config(path)
    register_live_config(config)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"),
        config=config,
        _current_run_params=SimpleNamespace(
            run_id="main-run", task_id="main-task", context_scope="default", task_attributes={},
        ),
    )
    return agent, path


@contextmanager
def _restricted_context(agent: SimpleNamespace, kind: str) -> Iterator[None]:
    """分别设置各类运行标记并恢复原状态，避免组合标记遮住单独分支。"""
    main_params = agent._current_run_params
    scope = kind if kind in {"task_local", "control_plane", "isolated"} else "default"
    attrs = {"agent_run_id": "child-run"} if kind in {"agent_run_id", "combined"} else {}
    agent._current_run_params = SimpleNamespace(
        run_id="child-run", task_id="child-task",
        context_scope="task_local" if kind == "combined" else scope, task_attributes=attrs,
    )
    previous = None
    try:
        if kind in {"runtime", "combined"}:
            previous = runtime_context.set_current_subagent_context(
                agent, run_id="child-run", attempt_id="child-attempt", task_attributes=attrs,
            )
        elif kind == "legacy":
            agent._current_subagent_run_id = "child-run"
        yield
    finally:
        if previous is not None:
            runtime_context.restore_current_subagent_context(agent, previous)
        if kind == "legacy":
            del agent._current_subagent_run_id
        agent._current_run_params = main_params


def test_main_session_keeps_full_schema_persistence_history_and_live_refresh(local_agent, tmp_path):
    agent, path = local_agent
    tool = module.UserConfigTool(agent)
    full_schema = module.UserConfigTool.model_spec.input_schema
    assert module._is_main_owner(agent) is True
    assert tool.model_spec.input_schema == full_schema
    assert {"set", "reset", "revert"} <= set(full_schema["properties"]["action"]["enum"])

    changes = (
        ("self_learning_max_skills", "51", 51),
        ("enable_self_learning", "false", False),
        ("memory_lesson_archive_days", "30", 30),
    )
    for count, (key, value, expected) in enumerate(changes, start=1):
        execution = execute_canonical_test_call(
            tmp_path / key, tools={"user_config": tool}, tool_name="user_config",
            arguments={"action": "set", "key": key, "value": value},
            allowed_tools=["user_config"], run_id="main-run", call_id="main-" + key,
            trusted_run_context={"run_scope": {"agent_kind": "main", "run_id": "main-run"}},
        )
        assert execution.decision.status == "allow", execution.decision
        assert execution.result.ok, execution.result
        assert execution.result.handler_executed is True
        assert getattr(load_config(path), key) == expected
        history = parameter_history(user_path=path, limit=0)
        assert len(history) == count
        assert {item["key"] for item in history} == {item[0] for item in changes[:count]}
        assert key in refresh_live_switches(agent.config)
        assert getattr(agent.config, key) == expected

    assert ledger_path(path).read_bytes()
    assert module.UserConfigTool(agent).model_spec.input_schema == full_schema


@pytest.mark.parametrize("context_kind", _RESTRICTED_CONTEXTS)
def test_restricted_context_has_decision_only_schema(local_agent, context_kind):
    agent, _path = local_agent
    shared_home = agent.home_paths
    full_schema = module.UserConfigTool.model_spec.input_schema
    with _restricted_context(agent, context_kind):
        assert agent.home_paths is shared_home
        assert module._is_main_owner(agent) is False
        schema = module.UserConfigTool(agent).model_spec.input_schema
        assert set(schema["properties"]["action"]["enum"]) == _DECISION_ACTIONS
        assert schema["required"] == ["action"]
        assert not {"key", "value", "include_constants", "include_full_config"} & schema["properties"].keys()
    assert runtime_context.current_subagent_run_id(agent) == ""
    assert module._is_main_owner(agent) is True
    assert module.UserConfigTool(agent).model_spec.input_schema == full_schema


@pytest.mark.parametrize("context_kind", _RESTRICTED_CONTEXTS)
@pytest.mark.parametrize("entrypoint", ("canonical", "direct"))
def test_restricted_global_changes_leave_config_and_ledger_bytes_unchanged(
    local_agent, tmp_path, context_kind, entrypoint,
):
    agent, path = local_agent
    # 沿用主会话构造的完整声明，确认执行阶段自身复核，而不只依赖声明裁剪。
    tool = module.UserConfigTool(agent)
    initial = tool.execute({"action": "set", "key": "self_learning_max_skills", "value": "51"})
    assert initial.ok, initial.output
    history = parameter_history(user_path=path, limit=0)
    assert len(history) == 1
    before_config = path.read_bytes()
    before_ledger = ledger_path(path).read_bytes()
    operations = (
        {"action": "set", "key": "enable_self_learning", "value": "false"},
        {"action": "reset", "key": "self_learning_max_skills"},
        {"action": "revert", "change_id": str(history[0]["id"])},
    )
    allowed_tools = active_model_subagent_tools(["user_config"])
    assert allowed_tools == ["user_config"]
    with _restricted_context(agent, context_kind):
        for operation in operations:
            if entrypoint == "canonical":
                execution = execute_canonical_test_call(
                    tmp_path / operation["action"], tools={"user_config": tool}, tool_name="user_config",
                    arguments=operation, allowed_tools=allowed_tools, run_id="child-run",
                    attempt_id="child-attempt", call_id="child-" + operation["action"],
                    trusted_run_context={"run_scope": {"agent_kind": "subagent", "run_id": "child-run"}},
                )
                assert execution.decision.status == "allow", execution.decision
                assert execution.result.handler_executed is True
                result = execution.result
            else:
                result = tool.execute(operation)
            assert not result.ok, result
            assert result.error_code == "TOOL_PERMISSION_DENIED"
            assert result.effect_outcome == "not_started"
            assert path.read_bytes() == before_config
            assert ledger_path(path).read_bytes() == before_ledger
            assert parameter_history(user_path=path, limit=0) == history

        refresh_live_switches(agent.config)
        assert agent.config.enable_self_learning is True
        assert agent.config.self_learning_max_skills == 51

    # 同一工具返回主会话后继续可写，证明限制没有残留到后续主会话。
    main_result = execute_canonical_test_call(
        tmp_path / "main", tools={"user_config": tool}, tool_name="user_config",
        arguments={"action": "set", "key": "enable_self_learning", "value": "false"},
        allowed_tools=["user_config"], run_id="main-run", call_id="main-resumed",
        trusted_run_context={"run_scope": {"agent_kind": "main", "run_id": "main-run"}},
    )
    assert main_result.result.ok, main_result.result
    refresh_live_switches(agent.config)
    assert agent.config.enable_self_learning is False
    assert len(parameter_history(user_path=path, limit=0)) == 2


@pytest.mark.parametrize("action", sorted(_DECISION_ACTIONS))
def test_child_keeps_all_decision_routes(local_agent, tmp_path, action):
    agent, path = local_agent
    before_config = path.read_bytes()
    assert not ledger_path(path).exists()
    target = "_decision_model_operation" if action in {"decision_models", "decision_probe"} else "_decision"
    operation = action if target == "_decision_model_operation" else action.removeprefix("decision_")
    params = {"action": action}
    outcome = ToolHandlerOutcome("user_config", True, "decision route")

    with _restricted_context(agent, "combined"):
        tool = module.UserConfigTool(agent)
        # 仅替换分流终点，避免目录与连接操作；构造和工具执行仍走真实入口。
        with patch.object(module, target, return_value=outcome) as route:
            assert tool.execute(params) is outcome
            route.assert_called_once_with(agent, operation, params)
            execution = execute_canonical_test_call(
                tmp_path / action, tools={"user_config": tool}, tool_name="user_config",
                arguments=params, allowed_tools=active_model_subagent_tools(["user_config"]),
                run_id="child-run", attempt_id="child-attempt", call_id=action,
                trusted_run_context={"run_scope": {"agent_kind": "subagent", "run_id": "child-run"}},
            )
            assert execution.decision.status == "allow", execution.decision
            assert execution.result.ok, execution.result
            assert execution.result.handler_executed is True
            assert route.call_count == 2
            route.assert_called_with(agent, operation, params)
    assert path.read_bytes() == before_config
    assert not ledger_path(path).exists()


@pytest.mark.parametrize("agent", (
    None,
    SimpleNamespace(home_paths=SimpleNamespace(owner_provider="local", owner_kind="user", owner_id="alice")),
))
def test_missing_or_non_admin_context_has_no_global_set(agent):
    assert module._is_main_owner(agent) is False
    schema = module.UserConfigTool(agent).model_spec.input_schema
    assert set(schema["properties"]["action"]["enum"]) == _DECISION_ACTIONS
    result = module.UserConfigTool(agent).execute({"action": "set", "key": "enable_self_learning", "value": "false"})
    assert not result.ok
    assert result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.effect_outcome == "not_started"
