"""原C lease/P3/claim与真实Curator事务；仅供应商为阻塞假模型，不称正式native/LLM验收。"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.io import (
    gateway_turn_transition,
    read_json_file,
    update_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    CoordinatorAdmissionScope,
    PersistentAdmission,
    reservation_file,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    enqueue_source,
    freeze_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_errors import RequestExecutionOutcomeUncertainError
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.gateway_parts.request_worker import (
    AdmissionLimits,
    terminalize_unhandled_claimed_gateway_request,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_memory_curator_v2 import _BlockingBackend, _service, _valid_output


# LLM: 只替换学习供应商，原request/锁/模型提交/P5/state路径仍由产品运行；不复用Curator专属prompt断言。
# 类用途: 为原S3网络等待提供可释放的假供应商。
class _BlockingLearningBackend:
    # LLM: output是测试层schema样本，不参与产品权限或状态裁决。
    # 函数用途: 保存本次假供应商的输出和等待事件。
    def __init__(self, output):
        self.output, self.calls = output, 0
        self.started, self.release = threading.Event(), threading.Event()

    # LLM: 真正bounded worker调用后才记started，等待时不代写候选；只返回标准text供应商结果。
    # 函数用途: 返回一份学习生成结果供原解析器处理。
    def generate_structured(self, prompt, *, response_schema):
        assert response_schema["additionalProperties"] is False and "tools" not in response_schema
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return SimpleNamespace(text=self.output)


# LLM: 仅真实runner供应商生成等待用替身，原任务/RunParams/权限/RuntimeDB与结果文件都由产品链推进。
# 类用途: 给独立child请求制造可撤销的物理模型等待。
class _BlockingChildBackend(EchoBackend):
    # LLM: 事件只控制测试I/O，不参与任何生产授权。
    # 函数用途: 准备一份模型等待与调用计数。
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.calls = 0

    # LLM: 只有原模型提交后调用才记实际发送，不代写任何业务结果。
    # 函数用途: 返回普通模型回复，交给原child收口。
    def generate(self, prompt, **kwargs):
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return ModelResponse(text="材料检查已完成。", backend="echo")


# LLM: 原真实临时owner/store/curator/repos；同原ControlPlane唯一Root先retain实际gate，再走P3/冻结claim；调用方须复用此Root而非重绑。
# 函数用途: 为停止和正向提交准备同一条完整的后台策展运输。
@contextmanager
def curator_case(tmp_path, monkeypatch, *, source_kind="curator"):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo", max_tokens=65536,
                                   enable_self_learning=source_kind == "learning"), tmp_path / "workspace")
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic", "channel": "internal", "channel_conversation_id": "curator-background"})
    message = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "我的个人电脑使用 macOS。",
                                    "metadata": {"request_id": "request-1", "task_id": "task-1", "run_id": "run-1"}, "now": 11})
    backend = _BlockingBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    service = _service(agent.home_paths.owner_home_dir, backend, store, owner_id="local/main")
    agent.memory_curator = service
    service.request("admin")
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    source = service.state_store.path
    if source_kind == "learning":
        from agent_py_agent.agent.capability.skill_learning import (
            SkillLearningRuntime,
            SkillLearningService,
            SkillLearningSettings,
        )
        from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
        from agent_py_agent.tests.test_skill_learning import _ctx, _output
        backend = _BlockingLearningBackend(_output())
        learning_store = SkillLearningStore.for_home(agent.home_paths)
        service = SkillLearningService(learning_store, SkillLearningSettings(min_tool_rounds=1),
            SkillLearningRuntime(backend, lambda: agent.skills_service.snapshot_for(force_reload=True)))
        agent.skill_learning = service
        assert service.enqueue_from_finalize(_ctx()) == "queued"
        [source] = learning_store.pending_requests()
    if source_kind == "maintenance":
        policy = json.loads(agent.home_paths.owner_retention_json.read_text())
        policy.update(cache_days=1, maintenance_interval_seconds=100)
        agent.home_paths.owner_retention_json.write_text(json.dumps(policy))
        stale = agent.home_paths.owner_cache_dir / "old.tmp"
        stale.write_bytes(b"old-cache")
        os.utime(stale, (1, 1))
        source = agent.home_paths.owner_data_dir / "maintenance.json"
    if source_kind == "orphan":
        task = agent.subagents.create_run(goal="检查已有材料", thought="原来源合同", plan=["阅读并回复"], allowed_tools=[])
        backend = _BlockingChildBackend()
        agent._subagent_worker_backend_override = backend
        monkeypatch.setattr(EchoBackend, "generate", lambda actual, prompt, **kwargs: backend.generate(prompt, **kwargs))
        source = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    ref = freeze_source(agent.home_paths.root, OwnerIdentity.local_main(), source, source_kind)
    gate = PersistentAdmission(paths, agent, CoordinatorAdmissionScope(2, "curator-C"))
    guard = coordinator_guard(paths.root, gate.count)
    guard.__enter__()
    closed = []
    def close():
        if not closed:
            closed.append(True)
            guard.__exit__(None, None, None)
    try:
        controller = prepare_coordinator_takeover(paths, gate.count, gate.c_run_id)
        gate._runtime_takeover, gate._runtime_takeover_required = controller, True
        from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane

        control = GatewayControlPlane(agent.config, agent.home_paths.root, paths, gate.count, gate.c_run_id, agent_root=agent.root)
        from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import (
            GatewayDrainRuntime,
        )

        runtime = GatewayDrainRuntime(control)
        runtime.retain_admission(gate)
        path, _ = enqueue_source(paths, agent.home_paths.root, ref)
        request = read_json_file(path)
        decision = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(request)), AdmissionLimits(8, 8))
        assert decision["status"] == "admitted", decision
        claim = claim_request(paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
        assert claim is not None
        yield agent, service, backend, gate, claim, close
    finally:
        backend.release.set()
        close()


# LLM: 真实_handle决定捕获/停止资源；假供应商只替换网络等待，不替代state acquire、committer、lease或终态。
# 函数用途: 在真实原C claim作用域运行后台请求。
def run_original(agent, gate, claim):
    with coordinator_execution_scope(gate, claim, agent):
        return _handle_gateway_request(agent, claim.path)


@pytest.mark.parametrize("fault", ["cancel", "generation"])
def test_network_wait_then_revoke_never_enters_late_curator_commit_or_canonical(tmp_path, monkeypatch, fault):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, close):
        p3 = reservation_file(gate.paths, claim.request_id)
        p3_before = p3.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert backend.started.wait(5), "original_provider_must_really_wait"
                state_before = service.state_store.path.read_bytes()
                assert service.state_store.load().active_lease
                if fault == "cancel":
                    with gateway_turn_transition(gate.paths, claim.request_id):
                        update_json_file_atomic(claim.path, lambda row: {**row, "cancel_requested": True, "turn_phase": "closing"})
                else:
                    close()
            finally:
                backend.release.set()
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                future.result(5)
        error = caught.value
        assert error.request_id == claim.request_id and error.execution_attempt_id == claim.execution_attempt_id
        assert error.operation_entered is False and backend.calls == 1
        assert service.state_store.path.read_bytes() == state_before
        assert service.candidate_service.list() == []
        assert service.daily_store.list(day=datetime.now(timezone.utc).date()) == []
        assert service.run_log.list() == []
        terminalize_unhandled_claimed_gateway_request(gate.paths, claim.path, error, agent=agent,
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        assert p3.read_bytes() == p3_before
        assert not (gate.paths.terminal / claim.path.name).exists()
        pending = read_json_file(claim.path)
        assert pending["status"] == "processing" and pending["execution_outcome"]["status"] == "unknown"
        assert pending["execution_outcome"]["operation_entered"] is False
        assert pending["execution_outcome"]["execution_attempt_id"] == claim.execution_attempt_id


def test_original_curator_nested_transactions_commit_without_holding_t_during_model_wait(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, _):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert backend.started.wait(5)
                # 同原T真的可取得，证明模型等待没持T；正常body仍会进入原multi-file事务及state内部调用。
                with gateway_turn_transition(gate.paths, claim.request_id):
                    assert read_json_file(claim.path)["status"] == "processing"
            finally:
                backend.release.set()
            response = future.result(5)
        assert response["ok"] is True and response["background_result"]["status"] == "succeeded"
        assert backend.calls == 1 and len(service.candidate_service.list()) == 1
        assert len(service.run_log.list()) == 1 and not service.state_store.load().active_lease


@pytest.mark.parametrize("fault", ["ordinary", "request", "attempt"])
def test_wrong_uncertain_identity_does_not_block_original_canonical_failure(tmp_path, monkeypatch, fault):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, claim, _):
        identity = read_json_file(claim.path)
        if fault == "ordinary":
            error = RuntimeError("ordinary business failure")
        else:
            identity = {**identity, "id" if fault == "request" else "execution_attempt_id": "another-original-execution"}
            error = RequestExecutionOutcomeUncertainError(identity, "memory_curator_commit")
        terminalize_unhandled_claimed_gateway_request(gate.paths, claim.path, error, agent=agent,
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        terminal = read_json_file(gate.paths.terminal / claim.path.name)
        assert terminal["status"] == "failed" and terminal["terminal_response"]["ok"] is False
        assert "execution_outcome" not in terminal
        gate.release_settled()
        assert read_json_file(reservation_file(gate.paths, claim.request_id))["state"] == "released"
