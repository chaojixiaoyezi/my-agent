"""唯一安装表的激活合同；临时文件与并发进程仅作开发验证，不运行插件或真实模型。"""

import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation, plugin_catalog_digest
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.user_space.owner_quota import owner_quota_enforcer_from_policy
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_plugin_configuration import configure_request, installed
from agent_py_agent.tests.test_plugin_install_store import _request


# LLM: 这里只验证状态合同，用合成解释器摘要，不声称环境或 MCP 已启动；所有写入在临时 owner 中。
# 函数用途: 创建停用安装及宿主原启用计划，供 CAS 与旧代测试共用。
def activation_fixture(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path / "home"))
    entry = store.install(_request(tmp_path)).installation
    plan = PluginEnvironmentPlan("enable-a", entry.manifest.plugin_id, entry.package_sha256, entry.revision,
                                 entry.settings_revision, "a" * 64)
    return store, entry, PluginActivationRequest(plan.operation_id, entry.revision, PluginActivation(plan, "preparing"))


# LLM: 目标目录摘要是合同夹具，不是服务握手证明；产品调用方仍须检查完整候选目录。
# 函数用途: 从已持久准备构造同代发布请求。
def publication(prepared):
    entry = prepared.installation
    return PluginActivationRequest(entry.activation.plan.operation_id, entry.revision,
                                   replace(entry.activation, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest)))


# LLM: 撤销只产生权威关闭记录，不能将测试中的该返回当作 OS 清理成功。
# 函数用途: 为当前固定代构造独立管理撤销请求。
def revocation(entry, operation="disable-a"):
    return PluginActivationRequest(operation, entry.revision, replace(entry.activation, phase="revoked"))


def test_prepare_publish_revoke_share_one_authority_and_old_snapshot_stays_revoked(tmp_path):
    store, original, request = activation_fixture(tmp_path)
    prepared = store.change_activation(request)
    entry = prepared.installation
    assert entry.revision == 2 and not entry.enabled
    assert store.require_activation(entry.manifest.plugin_id, entry.activation_id, phases=frozenset({"preparing"})) == entry
    with pytest.raises(PluginInstallationError, match="不可用"):
        store.require_activation(entry.manifest.plugin_id, entry.activation_id)
    published = store.change_activation(publication(prepared)).installation
    assert published.enabled and published.revision == 3
    assert published.activation_id == entry.activation_id
    assert store.require_activation(published.manifest.plugin_id, published.activation_id) == published
    stopped = store.change_activation(revocation(published)).installation
    assert not stopped.enabled and stopped.revision == 4 and stopped.activation.phase == "revoked"
    assert stopped.activation.plan == entry.activation.plan
    assert store.package_bytes(stopped) == store.package_bytes(original)
    assert published.enabled  # 冻结快照没有被原地修改，执行必须重新核对权威。
    for phases in (frozenset({"active"}), frozenset({"preparing"}), frozenset({"preparing", "active"})):
        with pytest.raises(PluginInstallationError, match="不可用"):
            store.require_activation(published.manifest.plugin_id, published.activation_id, phases=phases)
    payload = json.loads((store.root / "installations.json").read_text())
    assert payload["schema_version"] == "plugin_installations.v3"
    assert "enabled" not in payload["installations"][0] and "activation_id" not in payload["installations"][0]
    assert str(tmp_path) not in json.dumps(payload)


def test_each_exact_commit_replays_without_rewrite_or_switching_stage(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    prepared = store.change_activation(request)
    stages = [(request, prepared), (publication(prepared), None)]
    path = store.root / "installations.json"
    for current, result in stages:
        result = result or store.change_activation(current)
        before = path.read_bytes(), path.stat().st_mtime_ns
        assert store.change_activation(current).outcome == "replayed"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    with pytest.raises(PluginInstallationError):
        store.change_activation(request)
    current = revocation(store.snapshot()[0])
    stopped = store.change_activation(current)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert store.change_activation(current).receipt == stopped.receipt
    other = revocation(stopped.installation, "disable-b")
    assert store.change_activation(other).outcome == "unchanged"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("phase", ["preparing", "active", "revoked"])
def test_configuration_and_new_generation_wait_for_confirmed_cleanup(tmp_path, phase):
    store, _, request = activation_fixture(tmp_path)
    result = store.change_activation(request)
    if phase == "active":
        result = store.change_activation(publication(result))
    elif phase == "revoked":
        result = store.change_activation(revocation(result.installation))
    entry = result.installation
    before = (store.root / "installations.json").read_bytes()
    changed = replace(request.activation.plan, operation_id="enable-b", installation_revision=entry.revision)
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(PluginActivationRequest("enable-b", entry.revision, PluginActivation(changed, "preparing")))
    assert error.value.reason == "activation_unsettled"
    from agent_py_agent.agent.plugin_configuration import PluginConfigureRequest
    with pytest.raises(PluginInstallationError) as error:
        store.configure(PluginConfigureRequest(entry.manifest.plugin_id, entry.package_sha256, "configure-b", entry.revision, "{}"))
    assert error.value.reason == "activation_unsettled"
    assert (store.root / "installations.json").read_bytes() == before


def test_required_settings_must_exist_before_reserving_activation(tmp_path):
    store, entry, _ = installed(tmp_path)
    plan = PluginEnvironmentPlan("enable-a", entry.manifest.plugin_id, entry.package_sha256, entry.revision, 0, "a" * 64)
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(PluginActivationRequest("enable-a", entry.revision, PluginActivation(plan, "preparing")))
    assert error.value.reason == "invalid_settings"
    assert store.snapshot() == (entry,)
    entry = store.configure(configure_request(entry)).installation
    plan = replace(plan, installation_revision=entry.revision, settings_revision=entry.settings_revision)
    prepared = store.change_activation(PluginActivationRequest("enable-a", entry.revision, PluginActivation(plan, "preparing")))
    assert prepared.installation.settings_json == entry.settings_json
    assert prepared.installation.settings_revision == entry.settings_revision


@pytest.mark.parametrize("change,reason", [
    ({"plugin_id": "missing"}, "plugin_missing"),
    ({"package_sha256": "b" * 64}, "activation_binding_conflict"),
    ({"settings_revision": 1}, "activation_binding_conflict"),
    ({"installation_revision": 2}, "activation_binding_conflict"),
])
def test_prepare_rejects_changed_frozen_binding(tmp_path, change, reason):
    store, entry, request = activation_fixture(tmp_path)
    target = replace(request.activation, plan=replace(request.activation.plan, **change))
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(replace(request, activation=target))
    assert error.value.reason == reason
    assert store.snapshot() == (entry,)


def test_publish_needs_preparation_original_plan_and_complete_catalog_digest(tmp_path):
    store, entry, request = activation_fixture(tmp_path)
    target = replace(request.activation, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
    with pytest.raises(PluginInstallationError):
        store.change_activation(replace(request, activation=target))
    prepared = store.change_activation(request)
    original = publication(prepared)
    for change in ({"catalog_sha256": "b" * 64}, {"plan": replace(target.plan, interpreter_fingerprint="c" * 64)}):
        with pytest.raises(PluginInstallationError):
            store.change_activation(replace(original, activation=replace(original.activation, **change)))
    assert store.snapshot() == (prepared.installation,)


def test_revoke_before_publication_prevents_old_preparation_from_returning(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    prepared = store.change_activation(request)
    publish = publication(prepared)
    stopped = store.change_activation(revocation(prepared.installation))
    with pytest.raises(PluginInstallationError):
        store.change_activation(publish)
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(replace(publish, expected_revision=stopped.installation.revision))
    assert error.value.reason == "activation_revoked"
    assert store.snapshot() == (stopped.installation,)


def test_publish_and_revoke_compete_on_same_revision_without_lost_update(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    prepared = store.change_activation(request)
    barrier = Barrier(2)

    def submit(change):
        barrier.wait(timeout=5)
        try:
            return store.change_activation(change).outcome
        except PluginInstallationError as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (publication(prepared), revocation(prepared.installation))))
    assert results.count("revision_conflict") == 1
    current = store.snapshot()[0]
    assert current.revision == 3
    assert current.activation.phase in {"active", "revoked"}
    assert (current.activation.phase == "active") == ("activate" in results)


def test_revoke_does_not_wait_for_owner_quota_or_require_free_capacity(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    entry = store.change_activation(request).installation
    owner = resolve_owner_home(tmp_path / "home")
    quota = owner_quota_enforcer_from_policy(owner.home_dir, quota_path=owner.quota_json)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with quota.admission():
            result = pool.submit(store.change_activation, revocation(entry)).result(timeout=2)
    assert result.installation.activation.phase == "revoked"
    owner.quota_json.write_text('{"max_disk_mb":1}')
    (owner.home_dir / "occupied.bin").write_bytes(b"x" * (1024 * 1024))
    assert store.change_activation(revocation(result.installation, "disable-b")).outcome == "unchanged"


@pytest.mark.parametrize("phase", ["preparing", "active", "revoked"])
@pytest.mark.parametrize("fault,expected", [("before", "not_committed"), ("after", "committed"), ("unreadable", "unknown")])
def test_activation_commit_errors_preserve_original_truth(tmp_path, monkeypatch, phase, fault, expected):
    from agent_py_agent.agent import plugin_install_store
    store, _, request = activation_fixture(tmp_path)
    if phase != "preparing":
        prepared = store.change_activation(request)
        request = publication(prepared) if phase == "active" else revocation(prepared.installation)
    write = plugin_install_store.write_text_atomic_beneath
    before = store.snapshot()

    def failing(root, parts, text):
        if fault == "after":
            write(root, parts, text)
        elif fault == "unreadable":
            (store.root / "installations.json").write_text("{")
        raise OSError("synthetic write acknowledgement failure")

    monkeypatch.setattr(plugin_install_store, "write_text_atomic_beneath", failing)
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(request)
    assert error.value.commit_state == expected
    assert error.value.receipt.action == request.action
    if fault == "after":
        assert store.change_activation(request).outcome == "replayed"
    elif fault == "before":
        assert store.snapshot() == before


@pytest.mark.parametrize("previous", [None, {"from_schema": "plugin_installations.v1", "source_sha256": "a" * 64}])
def test_v2_config_migration_is_explicit_and_keeps_prior_source(tmp_path, previous):
    store, entry, _ = installed(tmp_path)
    entry = store.configure(configure_request(entry)).installation
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    payload.update(schema_version="plugin_installations.v2", migration=previous)
    for row in payload["installations"]:
        del row["activation"], row["last_commit"]["activation_sha256"]
        row.update(enabled=False, activation_id="")
    path.write_text(json.dumps(payload))
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert store.snapshot() == (entry,)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    plan = PluginEnvironmentPlan("enable-a", entry.manifest.plugin_id, entry.package_sha256,
                                 entry.revision, entry.settings_revision, "a" * 64)
    result = store.change_activation(PluginActivationRequest("enable-a", entry.revision, PluginActivation(plan, "preparing")))
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == "plugin_installations.v3"
    assert saved["migration"] == {"from_schema": "plugin_installations.v2",
                                  "source_sha256": hashlib.sha256(before[0]).hexdigest(), "previous": previous}
    assert result.installation.settings_json == entry.settings_json
    assert store.snapshot() == (result.installation,)


@pytest.mark.parametrize("change", [
    lambda row: row.pop("activation"),
    lambda row: row["activation"].update(phase="disabled"),
    lambda row: row["activation"].update(phase="active"),
    lambda row: row["activation"].update(extra=True),
    lambda row: row["activation"]["plan"].update(settings_revision=1),
    lambda row: row["activation"]["plan"].update(operation_id="changed"),
    lambda row: row["last_commit"].update(activation_sha256="b" * 64),
    lambda row: row.update(enabled=True),
])
def test_damaged_v3_activation_never_falls_back_or_rewrites(tmp_path, change):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(request)
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    change(payload["installations"][0])
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(PluginInstallationError):
        store.snapshot()
    assert path.read_bytes() == before


@pytest.mark.parametrize("phase", ["preparing", "active"])
def test_readback_rejects_receipt_from_a_different_enable_operation(tmp_path, phase):
    store, _, request = activation_fixture(tmp_path)
    prepared = store.change_activation(request)
    if phase == "active":
        store.change_activation(publication(prepared))
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    payload["installations"][0]["last_commit"]["operation_id"] = "another-enable"
    path.write_text(json.dumps(payload))
    with pytest.raises(PluginInstallationError):
        store.snapshot()


def test_independent_processes_cannot_reserve_two_generations(tmp_path):
    store, _, _ = activation_fixture(tmp_path)
    script = """
import sys,time
from pathlib import Path
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
root,op=Path(sys.argv[1]),sys.argv[2]
store=PluginInstallStore(resolve_owner_home(root/'home'))
entry=store.snapshot()[0]
plan=PluginEnvironmentPlan(op,entry.manifest.plugin_id,entry.package_sha256,entry.revision,0,'a'*64)
(root/op).touch()
deadline=time.monotonic()+5
while not (root/'start').exists():
    if time.monotonic()>deadline: raise TimeoutError('test barrier')
    time.sleep(.01)
try: print(store.change_activation(PluginActivationRequest(op,entry.revision,PluginActivation(plan,'preparing'))).outcome)
except PluginInstallationError as exc: print(exc.reason)
"""
    jobs = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("enable-a", "enable-b")]
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / name).exists() for name in ("enable-a", "enable-b")):
            assert time.monotonic() < deadline
            time.sleep(.01)
        (tmp_path / "start").touch()
        results = [job.communicate(timeout=10) for job in jobs]
        assert all(job.returncode == 0 for job in jobs), results
        assert sorted(output.strip() for output, _ in results) == ["prepare", "revision_conflict"]
    finally:
        for job in jobs:
            if job.poll() is None:
                job.kill()
            job.wait(timeout=5)
    assert store.snapshot()[0].revision == 2


def test_missing_activation_query_stays_cold(tmp_path):
    owner = resolve_owner_home(tmp_path / "home")
    with pytest.raises(PluginInstallationError):
        PluginInstallStore(owner).require_activation("missing", "a" * 64)
    assert not owner.home_dir.exists()
