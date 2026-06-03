from __future__ import annotations

from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import RuntimeIdentity, SecuritySignal


def test_subagent_save_preserves_security_signal_reserve_fields(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(
        goal="检查可疑安全提示",
        thought="当前只记录安全信号，不做自动拦截。",
        plan=["记录信号", "交给后续安全模块判断"],
    )
    task.security_review_required = True
    task.security_signals = [
        SecuritySignal(
            signal_type="prompt_injection_suspected",
            severity="medium",
            summary="工具输出要求忽略上级指令，疑似安全欺骗。",
            evidence_refs=["reports/status_report.json"],
        )
    ]

    manager.save(task)

    loaded = manager.load(task.id)
    projected = store.get_agent_run(task.id)

    assert loaded.security_review_required is True
    assert len(loaded.security_signals) == 1
    assert loaded.security_signals[0].signal_type == "prompt_injection_suspected"
    assert loaded.security_signals[0].severity == "medium"
    assert loaded.security_signals[0].evidence_refs == ["reports/status_report.json"]
    assert projected is not None
    assert projected.metadata["security_review_required"] is True
    assert projected.metadata["security_signal_count"] == 1
    assert projected.metadata["security_signal_types"] == ["prompt_injection_suspected"]


def test_subagent_save_preserves_runtime_identity_memory_and_config_scope_reserve_fields(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(
        goal="处理员工会话里的大输出恢复",
        thought="只记录隔离边界，不启用员工长期记忆或全局配置写入。",
        plan=["保存 scope", "投影到控制面"],
    )
    task.runtime_identity = RuntimeIdentity(
        service_owner_id="owner-admin",
        requester_id="employee-1",
        effective_principal_id="employee-1",
        conversation_id="feishu-dm-1",
        root_run_id="root-chat-1",
        memory_namespace="conversation:feishu-dm-1",
        conversation_memory_policy="not_enabled",
        promotion_policy="explicit_review",
        config_scope="conversation_overlay",
        config_overlay_ref="overlays/feishu-dm-1.json",
        config_promotion_policy="admin_approval_required",
    )

    manager.save(task)

    loaded = manager.load(task.id)
    projected = store.get_agent_run(task.id)

    assert loaded.runtime_identity.requester_id == "employee-1"
    assert loaded.runtime_identity.memory_namespace == "conversation:feishu-dm-1"
    assert projected is not None
    assert projected.metadata["runtime_identity"]["service_owner_id"] == "owner-admin"
    assert projected.metadata["runtime_identity"]["effective_principal_id"] == "employee-1"
    assert projected.metadata["memory_scope"]["namespace"] == "conversation:feishu-dm-1"
    assert projected.metadata["memory_scope"]["conversation_memory_policy"] == "not_enabled"
    assert projected.metadata["memory_scope"]["promotion_policy"] == "explicit_review"
    assert projected.metadata["config_scope"]["scope"] == "conversation_overlay"
    assert projected.metadata["config_scope"]["overlay_ref"] == "overlays/feishu-dm-1.json"
    assert projected.metadata["config_scope"]["promotion_policy"] == "admin_approval_required"
    assert projected.metadata["config_scope"]["writes_global_config"] is False


def test_create_run_inherits_parent_config_overlay_ref(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    parent = manager.create_run(
        goal="父任务",
        thought="配置覆盖从父层传给子层。",
        plan=["创建父任务"],
        attributes={"config_overlay_ref": "overlays/parent.yaml", "config_scope": "task"},
    )

    child = manager.create_run(
        goal="子任务",
        thought="继承父任务运行配置覆盖。",
        plan=["创建子任务"],
        parent_id=parent.id,
    )

    assert child.runtime_identity.config_overlay_ref == "overlays/parent.yaml"
    assert child.runtime_identity.config_scope == "run"
    assert child.attributes["runtime_config_scope"]["overlay_ref"] == "overlays/parent.yaml"
    assert child.attributes["runtime_config_scope"]["loaded_as"] == "run_layer"
