"""释放/原结果消费的确定性交错；真实 OS 组件仍不代替产品 TUI 验收。"""

import json
import time

import pytest

from agent_py_agent.agent import plugin_install_store
from agent_py_agent.agent.plugin_activation import prepare_release
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.plugin_release import preparation_exit
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    ProcessSessionTransaction,
)
from agent_py_agent.tests.plugin_deactivation_fixtures import activation_component
from agent_py_agent.tests.test_plugin_deactivation import installed_manager


# LLM: 环境仅是临时目录夹具，真实进程由 activation_component 原入口启动；不伪装已安装 Python 环境。
# 函数用途: 创建一个可观察删除时机的旧代目录，并留下树外对照文件。
def environment_fixture(service, entry, tmp_path):
    environment = service.context.owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    environment.mkdir(parents=True)
    (environment / "probe.txt").write_text("old generation")
    outside = tmp_path / "unrelated.txt"
    outside.write_text("keep")
    (environment / "outside-link").symlink_to(outside)
    return environment, outside


def test_cancelled_preparation_handler_must_exit_before_environment_is_deleted(tmp_path):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, running=True, preparation=False) as state:
        environment, outside = environment_fixture(service, state["entry"], tmp_path)
        first = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="first")
        assert first["state"] == "succeeded", first
        assert first["details"]["released"] is False
        assert first["details"]["release_pending"] == "preparation_executor_unconfirmed"
        assert environment.is_dir() and service.installations.snapshot()[0].activation.phase == "revoked"
        state["release"].set()
        deadline = time.monotonic() + 5
        while True:
            try:
                preparation_exit(state["repo"], state["binding"])
                break
            except PluginInstallationError as exc:
                assert exc.reason == "preparation_executor_unconfirmed" and time.monotonic() < deadline
                time.sleep(.01)
        second = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="second")
        assert second["state"] == "succeeded" and second["details"]["released"], second
        assert not environment.exists() and outside.read_text() == "keep"
        assert service.installations.snapshot()[0].activation is None
        assert service.command("/plugins status first", revision="", request_id="query")["details"] == first["details"]


def test_environment_delete_failure_preserves_old_plan_and_fresh_disable_can_finish(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        environment, outside = environment_fixture(service, state["entry"], tmp_path)
        original = plugin_install_store.remove_tree_beneath
        monkeypatch.setattr(plugin_install_store, "remove_tree_beneath",
                            lambda *_: (_ for _ in ()).throw(OSError("fixture filesystem failure")))
        first = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="first")
        assert first["state"] == "outcome_unknown", first
        assert service.installations.snapshot()[0].activation.plan == state["entry"].activation.plan
        assert environment.exists()
        monkeypatch.setattr(plugin_install_store, "remove_tree_beneath", original)
        second = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="second")
        assert second["state"] == "succeeded" and second["details"]["released"], second
        assert not environment.exists() and outside.exists()
        assert service.command("/plugins status first", revision="", request_id="query")["state"] == "outcome_unknown"


def test_successful_result_survives_consumption_failure_and_explicit_resubmit_only_consumes(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        root = state["client"].connection().binding.managed.hosted.store_root
        store = ProcessSessionStore(root)
        original = ProcessSessionTransaction.consume_cleanup
        monkeypatch.setattr(ProcessSessionTransaction, "consume_cleanup",
                            lambda *_: (_ for _ in ()).throw(OSError("fixture unlink failure")))
        revision = service.catalog().revision
        first = service.command("/plugins disable sample-peek", revision=revision, request_id="disable")
        assert first["state"] == "succeeded" and first["cleanup_consumption"]["state"] == "pending", first
        assert len(store.list_records()[0]) == 1
        monkeypatch.setattr(ProcessSessionTransaction, "consume_cleanup", original)
        monkeypatch.setattr(service, "_prepare", lambda *_: pytest.fail("重送不能再执行原 handler"))
        query = service.command("/plugins status disable", revision="", request_id="query")
        assert query["details"] == first["details"] and len(store.list_records()[0]) == 1
        replay = service.command("/plugins disable sample-peek", revision=revision, request_id="disable")
        assert replay["details"] == first["details"] and replay["cleanup_consumption"]["state"] == "consumed"
        assert not store.list_records()[0]


def test_missing_original_executor_evidence_blocks_release_despite_cancelled_state(tmp_path):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        environment, _ = environment_fixture(service, state["entry"], tmp_path)
        with state["repo"].transaction() as conn:
            attempt_id = state["binding"].attempt_id
            metadata = json.loads(state["repo"].get_attempt(attempt_id)["metadata_json"])
            metadata.pop("executor")
            conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(metadata), attempt_id))
        result = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert result["state"] == "outcome_unknown", result
        assert environment.exists() and service.installations.snapshot()[0].activation.phase == "revoked"


def test_management_result_persistence_failure_never_consumes_resource_proof(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        store = ProcessSessionStore(state["client"].connection().binding.managed.hosted.store_root)
        original = ManagedOperationStore.finish_tool_operation
        def fail_success(instance, request):
            if request.status == "succeeded":
                raise OSError("fixture result commit failure")
            return original(instance, request)
        monkeypatch.setattr(ManagedOperationStore, "finish_tool_operation", fail_success)
        result = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert result["state"] == "outcome_unknown", result
        assert service.installations.snapshot()[0].activation is None
        records, errors = store.list_records()
        assert not errors and len(records) == 1 and records[0]["termination"]["cleanup"]["confirmed"]
        assert "cleanup_consumption" not in result


def test_corrupt_persisted_success_does_not_authorize_consumption_on_resubmit(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        store = ProcessSessionStore(state["client"].connection().binding.managed.hosted.store_root)
        original = ProcessSessionTransaction.consume_cleanup
        monkeypatch.setattr(ProcessSessionTransaction, "consume_cleanup",
                            lambda *_: (_ for _ in ()).throw(OSError("keep fixture evidence")))
        revision = service.catalog().revision
        first = service.command("/plugins disable sample-peek", revision=revision, request_id="disable")
        assert first["state"] == "succeeded" and len(store.list_records()[0]) == 1
        with state["repo"].transaction() as conn:
            row = conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?",
                               (first["operation_id"],)).fetchone()
            payload = json.loads(row[0])
            payload["result"]["result_envelope"] = "invalid result"
            conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?",
                         (json.dumps(payload), first["operation_id"]))
        monkeypatch.setattr(ProcessSessionTransaction, "consume_cleanup", original)
        monkeypatch.setattr(service, "_prepare", lambda *_: pytest.fail("坏结果不能触发 handler 重跑"))
        replay = service.command("/plugins disable sample-peek", revision=revision, request_id="disable")
        assert replay["state"] == "outcome_unknown", replay
        assert len(store.list_records()[0]) == 1 and "cleanup_consumption" not in replay


def test_release_cas_and_receipt_roundtrip_cannot_clear_a_newer_installation(tmp_path):
    from agent_py_agent.agent.plugin_installation import PluginInstallation
    from agent_py_agent.tests.test_plugin_activation import activation_fixture, revocation

    store, _, request = activation_fixture(tmp_path)
    prepared = store.change_activation(request).installation
    with pytest.raises(PluginInstallationError):
        prepare_release("disable", prepared, store.snapshot())
    stopped = store.change_activation(revocation(prepared, "disable")).installation
    result = prepare_release("disable", stopped, store.snapshot())
    assert result.installation.activation is None
    assert PluginInstallation.from_payload(result.installation.to_payload()) == result.installation
    assert prepare_release("disable", stopped, (result.installation,)).outcome == "replayed"
    with pytest.raises(PluginInstallationError):
        prepare_release("different", stopped, (result.installation,))


@pytest.mark.parametrize("field,value", [("start_time", "NaN"), ("start_time", True),
                                         ("ended_at", True), ("ended_at", "123"), ("started_at", -1)])
def test_invalid_executor_birth_or_exit_time_never_authorizes_deletion(tmp_path, monkeypatch, field, value):
    from agent_py_agent.agent import plugin_release

    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        binding, repo = state["binding"], state["repo"]
        metadata = json.loads(repo.get_attempt(binding.attempt_id)["metadata_json"])
        metadata["executor"][field] = value
        with repo.transaction() as conn:
            conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
                         (json.dumps(metadata), binding.attempt_id))
        monkeypatch.setattr(plugin_release, "executor_exit_reason", lambda *_: pytest.fail("坏身份不能用于 OS 退出判断"))
        with pytest.raises(PluginInstallationError):
            preparation_exit(repo, binding)
