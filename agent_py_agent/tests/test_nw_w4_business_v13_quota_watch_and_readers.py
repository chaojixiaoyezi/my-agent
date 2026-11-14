"""Next-Wave W4 (#12, #13, #16, #17) 运行时回归测试。"""
from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.artifacts.registry import REGISTRY_RELATIVE_PATH
from agent_py_agent.agent.business_spaces import (
    gap158_w7_fs_watch,
    v13_w9_canary_markers,
    v13_w11_packages,
    v13_w11_skills_learning,
)
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_observability import (
    ObservationSourceError,
    ObservationSourcePage,
)
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.conversation.store_threads import ThreadStore
from agent_py_agent.agent.gateway_parts import business_host_ports
from agent_py_agent.agent.gateway_parts.business_api import BusinessServices, bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.gateway_parts.business_api_v13_readers import default_observation_readers
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import owner_quota
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from scripts.build_capability_package import build_capability_package


@pytest.fixture
def w4_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent

    root = tmp_path / "native-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            enable_plugins=True,
            business_spaces_enabled=True,
            gateway_per_user_owner_scoping=False,
            memory_path="memory.jsonl",
            gateway_http_socket_timeout_seconds=30.0,
            gateway_http_queue_timeout_seconds=30.0,
            gateway_http_birth_gate_timeout_seconds=10.0,
        ),
        tmp_path / "workspace",
    )
    server = GatewayHTTPServer(
        0,
        gateway_paths_from_root(agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


def _http_business(
    server,
    action: str,
    params: dict,
    *,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    user: str | None = "business-alice",
    channel: str = "feishu",
):
    request_body: dict = {"action": action, "request_id": "w4-" + uuid4().hex, "params": params}
    if idempotency_key is not None:
        request_body["idempotency_key"] = idempotency_key
    if expected_revision is not None:
        request_body["expected_revision"] = expected_revision
    headers = {
        "Content-Type": "application/json",
        "Connection": "close",
        "X-Gateway-Token": server._local_client_credential,
        "X-Channel": channel,
    }
    if user is not None:
        headers["X-User-Id"] = user
    conn = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=30)
    try:
        conn.request("POST", "/client/business", json.dumps(request_body), headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def _ok_http(server, action: str, params: dict, **kwargs):
    status, body = _http_business(server, action, params, **kwargs)
    assert status == 200, body
    assert body["state"] == "completed" and body["error"] is None, body
    return body["data"]


def _owner_home_for(agent, user: str) -> Path:
    return resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user("feishu", user)).home_dir


def _install_quota_reentry_spy(monkeypatch):
    from agent_py_agent.agent import plugin_install_store as plugin_install_store_mod
    from agent_py_agent.agent.business_spaces import v13_store as v13_store_mod
    from agent_py_agent.agent.gateway_parts import business_artifacts_http as artifacts_http_mod

    original_factory = owner_quota.owner_quota_enforcer_from_policy
    thread_state = threading.local()
    observed = {"entries": 0, "checks": []}

    class _NonReentrantEnforcer:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @contextmanager
        def admission(self):
            depth = getattr(thread_state, "depth", 0)
            assert depth == 0, f"nested owner_quota.admission re-entry detected (depth={depth})"
            thread_state.depth = depth + 1
            observed["entries"] += 1
            try:
                with self._inner.admission() as real_admission:
                    orig_check = real_admission.check

                    def wrapped_check(changes, **kwargs):
                        observed["checks"].extend(
                            (str(c.path), c.final_size) for c in changes
                        )
                        return orig_check(changes, **kwargs)

                    real_admission.check = wrapped_check
                    yield real_admission
            finally:
                thread_state.depth = depth

    def spy_factory(*args, **kwargs):
        return _NonReentrantEnforcer(original_factory(*args, **kwargs))

    monkeypatch.setattr(owner_quota, "owner_quota_enforcer_from_policy", spy_factory)
    monkeypatch.setattr(v13_store_mod, "owner_quota_enforcer_from_policy", spy_factory)
    monkeypatch.setattr(artifacts_http_mod, "owner_quota_enforcer_from_policy", spy_factory)
    monkeypatch.setattr(plugin_install_store_mod, "owner_quota_enforcer_from_policy", spy_factory)
    return observed


def test_w11_skills_with_quota_no_nested_admission_and_rollback(w4_http, monkeypatch):
    server, agent = w4_http
    owner_home = resolve_owner_home(agent.home_paths.root).home_dir
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "quota.json").write_text(json.dumps({"max_disk_mb": 10}), encoding="utf-8")

    spy = _install_quota_reentry_spy(monkeypatch)
    markdown = "---\nname: quota-skill\ndescription: 隔离演示技能\n---\n\n# Steps\n\n1. Check\n"
    create_key = "skill-create-" + uuid4().hex
    created = _ok_http(
        server,
        "skills.create",
        {"skill_id": "quota-skill", "body_markdown": markdown},
        idempotency_key=create_key,
    )
    assert created["skill"]["name"] == "quota-skill"
    assert spy["entries"] >= 2

    entries_before_replay = spy["entries"]
    replay = _ok_http(
        server,
        "skills.create",
        {"skill_id": "quota-skill", "body_markdown": markdown},
        idempotency_key=create_key,
    )
    assert replay == created
    # 回放不重跑领域磁盘写及二次配额准入。
    assert spy["entries"] == entries_before_replay

    status_conflict, conflict = _http_business(
        server,
        "skills.create",
        {"skill_id": "quota-skill", "body_markdown": markdown + "\nExtra\n"},
        idempotency_key=create_key,
    )
    assert status_conflict == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    updated_md = markdown + "\n2. Verify\n"
    updated = _ok_http(
        server,
        "skills.update",
        {
            "skill_id": "quota-skill",
            "body_markdown": updated_md,
            "expected_version_ref": created["skill"]["version_ref"],
        },
        idempotency_key="skill-update-" + uuid4().hex,
    )
    assert updated["skill"]["version_ref"] != created["skill"]["version_ref"]

    retire_key = "skill-retire-" + uuid4().hex
    retired = _ok_http(server, "skills.retire", {"skill_id": "quota-skill"}, idempotency_key=retire_key)
    assert retired["skill"]["enabled"] is False
    activated = _ok_http(
        server,
        "skills.activate",
        {"skill_id": "quota-skill"},
        idempotency_key="skill-activate-" + uuid4().hex,
    )
    assert activated["skill"]["enabled"] is True
    assert _ok_http(server, "skills.retire", {"skill_id": "quota-skill"}, idempotency_key=retire_key) == retired

    # 提交阶段模拟配额超限时，已写入磁盘的技能与空目录须干净回滚并归零账本预留。
    orig_tx = V13Store.transaction

    def failing_commit(self, collection, mutate, **kwargs):
        if collection == "skill_operations":
            raise owner_quota.OwnerQuotaExceeded("quota_exceeded", projection=None)
        return orig_tx(self, collection, mutate, **kwargs)

    monkeypatch.setattr(V13Store, "transaction", failing_commit)
    status_fail, body_fail = _http_business(
        server,
        "skills.create",
        {
            "skill_id": "rollback-skill",
            "body_markdown": "---\nname: rollback-skill\ndescription: 回滚技能\n---\n\nbody\n",
        },
        idempotency_key="skill-fail-" + uuid4().hex,
    )
    assert status_fail != 200 and body_fail["state"] == "failed"
    assert not (owner_home / "skills" / "rollback-skill" / "SKILL.md").exists()
    assert not (owner_home / "skills" / "rollback-skill").exists()
    assert any(Path(p) == owner_home / "skills" / "rollback-skill" / "SKILL.md" and s == 0 for p, s in spy["checks"])

    # retire 提交失败时须回滚 SkillAdapter 启用态，保持原先 enabled=True。
    status_retire_fail, _ = _http_business(
        server,
        "skills.retire",
        {"skill_id": "quota-skill"},
        idempotency_key="skill-retire-fail-" + uuid4().hex,
    )
    assert status_retire_fail != 200
    listed = _ok_http(server, "skills.list", {})
    by_name = {row["name"]: row for row in listed["items"]}
    assert by_name["quota-skill"]["enabled"] is True


def test_w9_canary_document_and_w11_packages_decoupled_and_rollback(w4_http, monkeypatch):
    server, agent = w4_http
    main_owner = resolve_owner_home(agent.home_paths.root)
    alice_home = main_owner.home_dir
    alice_home.mkdir(parents=True, exist_ok=True)

    # 先预装待卸载包，再开启 quota.json 并建立权威账本基线，同时测试金丝雀回滚与包卸载。
    pkg_src = agent.home_paths.root / "pkg-src"
    pkg_src.mkdir(parents=True, exist_ok=True)
    (pkg_src / "CAPABILITY.md").write_text("# Demo Package\n", encoding="utf-8")
    pkg_zip = build_capability_package(
        {
            "plugin_id": "w4-demo-pkg",
            "version": "1.0",
            "summary": "演示包",
            "capability": {"description": "演示包卸载", "keywords": ["demo"], "entry_document": "CAPABILITY.md"},
            "files": [{"path": "CAPABILITY.md"}],
            "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        pkg_src,
        agent.home_paths.root / "w4-demo-pkg.zip",
    )
    snap = inspect_plugin_package(pkg_zip.read_bytes())
    PluginInstallStore(main_owner).install(PluginInstallRequest(snap, "w4-pkg-seed-001", 0))

    (alice_home / "quota.json").write_text(json.dumps({"max_disk_mb": 10}), encoding="utf-8")
    owner_quota.reconcile_owner_quota_slice(alice_home, max_entries=10_000, max_ms=5_000)

    spy = _install_quota_reentry_spy(monkeypatch)
    space = _ok_http(
        server,
        "spaces.create",
        {"name": "canary-space"},
        idempotency_key="space-" + uuid4().hex,
    )["space_id"]

    marker_key = "marker-doc-" + uuid4().hex
    created = _ok_http(
        server,
        "honeytokens.create",
        {"space_id": space, "kind": "document", "label": "doc-canary"},
        idempotency_key=marker_key,
    )
    assert created["secret"].startswith("canary_document_")
    artifact_id = created["marker"]["artifact"]["artifact_id"]
    assert (alice_home / "artifacts" / "business" / f"{artifact_id}.blob").is_file()

    # 重放必须在锁外即命中一次秘密脱敏回执，不再重新上传附件。
    replay = _ok_http(
        server,
        "honeytokens.create",
        {"space_id": space, "kind": "document", "label": "doc-canary"},
        idempotency_key=marker_key,
    )
    assert replay["secret"] == "<redacted>"
    assert replay["marker"] == created["marker"]

    status_conflict, conflict = _http_business(
        server,
        "honeytokens.create",
        {"space_id": space, "kind": "document", "label": "other-label"},
        idempotency_key=marker_key,
    )
    assert status_conflict == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # 文档上传成功但 store 提交抛 OwnerQuotaExceeded 时，必须删除孤立 blob 与 registry 行。
    orig_w9_write = v13_w9_canary_markers.w9_write

    def fail_canary_commit(call, collection, mutate, receipt_view=None):
        if collection == "canary_markers":
            raise owner_quota.OwnerQuotaExceeded("quota_exceeded", projection=None)
        return orig_w9_write(call, collection, mutate, receipt_view=receipt_view)

    monkeypatch.setattr(v13_w9_canary_markers, "w9_write", fail_canary_commit)
    reg_path = alice_home / "artifacts" / "business" / REGISTRY_RELATIVE_PATH
    reg_before = reg_path.read_bytes()
    blobs_before = {p.name for p in (alice_home / "artifacts" / "business").glob("*.blob")}
    status_fail, _ = _http_business(
        server,
        "honeytokens.create",
        {"space_id": space, "kind": "document", "label": "rollback-doc"},
        idempotency_key="marker-fail-" + uuid4().hex,
    )
    assert status_fail != 200
    assert {p.name for p in (alice_home / "artifacts" / "business").glob("*.blob")} == blobs_before
    assert reg_path.read_bytes() == reg_before
    assert any(size == 0 for _, size in spy["checks"])

    # W11 packages.uninstall 在配额开启下同样禁止与 .store.lock/外层配额锁嵌套重入。
    monkeypatch.setattr(v13_w9_canary_markers, "w9_write", orig_w9_write)

    remove_called_under_store_lock = []
    orig_store_tx = V13Store.transaction
    store_lock_depth = threading.local()

    def track_store_tx(self, collection, mutate, **kwargs):
        depth = getattr(store_lock_depth, "depth", 0)
        store_lock_depth.depth = depth + 1
        try:
            return orig_store_tx(self, collection, mutate, **kwargs)
        finally:
            store_lock_depth.depth = depth

    orig_remove = PluginInstallStore.remove

    def track_remove(self, *args, **kwargs):
        remove_called_under_store_lock.append(getattr(store_lock_depth, "depth", 0))
        return orig_remove(self, *args, **kwargs)

    monkeypatch.setattr(V13Store, "transaction", track_store_tx)
    monkeypatch.setattr(PluginInstallStore, "remove", track_remove)

    uninstall_key = "pkg-uninstall-" + uuid4().hex
    uninstalled = _ok_http(
        server,
        "packages.uninstall",
        {"package_id": "w4-demo-pkg"},
        idempotency_key=uninstall_key,
    )
    assert uninstalled["uninstalled"] is True
    assert remove_called_under_store_lock == [0]
    assert _ok_http(
        server,
        "packages.uninstall",
        {"package_id": "w4-demo-pkg"},
        idempotency_key=uninstall_key,
    ) == uninstalled
    assert len(remove_called_under_store_lock) == 1

    status_pkg_conflict, pkg_conflict = _http_business(
        server,
        "packages.uninstall",
        {"package_id": "other-pkg"},
        idempotency_key=uninstall_key,
    )
    assert status_pkg_conflict == 409 and pkg_conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_w9_canary_list_triggers_degrades_on_quota_or_store_unavailable(w4_http, monkeypatch):
    server, agent = w4_http
    main_owner = resolve_owner_home(agent.home_paths.root)
    main_owner.home_dir.mkdir(parents=True, exist_ok=True)

    space = _ok_http(
        server,
        "spaces.create",
        {"name": "degrade-space"},
        idempotency_key="sp-" + uuid4().hex,
    )["space_id"]
    created = _ok_http(
        server,
        "honeytokens.create",
        {"space_id": space, "kind": "api_key", "label": "k"},
        idempotency_key="mk-" + uuid4().hex,
    )
    token_id = created["marker"]["token_id"]
    secret = created["secret"]

    now = datetime.now(timezone.utc).isoformat()
    alert = {
        "alert_id": "alert-quota-1",
        "space_id": space,
        "title": "命中告警",
        "description": secret,
        "severity": "info",
        "status": "new",
        "verdict": None,
        "ai_confidence": None,
        "disposition": None,
        "rule_id": None,
        "source": "ingest",
        "external_ref": None,
        "entities": [],
        "techniques": [],
        "assignee": None,
        "case_id": None,
        "tags": [],
        "evidence_refs": [],
        "acknowledged_at": None,
        "resolved_at": None,
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "owner_id": "local/main",
    }
    V13Store(main_owner.home_dir).transaction(
        "soc_alerts",
        lambda recs: recs.__setitem__("alert-quota-1", alert) or {"ok": True},
        idempotency_key="seed-alert-" + uuid4().hex,
        params_digest="seed",
    )

    # 先将磁盘填充至超出 1 MB 配额并开启 quota.json，使只读触发写回执真实命中 OwnerQuotaExceeded。
    (main_owner.home_dir / "filler.bin").write_bytes(b"z" * (1024 * 1024 + 64))
    main_owner.quota_json.write_text(json.dumps({"max_disk_mb": 1}), encoding="utf-8")

    host = SimpleNamespace(
        home_paths=home_paths_with_owner(agent.home_paths, main_owner),
        config=agent.config,
        business_v13_providers=getattr(agent, "business_v13_providers", {}),
    )
    ctx = bind_business_context(host, authorize=lambda *_: True, subject_id="local/main")
    services = BusinessServices(host)
    call = V13Call(
        "honeytokens.triggers.list",
        {"token_id": token_id, "limit": 20},
        "req-degrade",
        "local/main",
        "local/main",
        ctx,
        services,
        main_owner.home_dir,
        "",
    )
    degraded = v13_w9_canary_markers.list_triggers(call)
    assert degraded["coverage"] == "partial"
    assert degraded["persistence_degraded"] is True
    assert degraded["persistence_warning"] == "OWNER_QUOTA_EXCEEDED"
    assert [row["source_ref"] for row in degraded["items"]] == ["alert-quota-1"]

    def raise_unavailable(*args, **kwargs):
        raise owner_quota.OwnerQuotaUnavailable("quota_unavailable")

    monkeypatch.setattr(v13_w9_canary_markers, "w9_write_many", raise_unavailable)
    degraded_unavail = v13_w9_canary_markers.list_triggers(call)
    assert degraded_unavail["persistence_degraded"] is True
    assert degraded_unavail["persistence_warning"] == "PERSISTENCE_UNAVAILABLE"
    assert [row["source_ref"] for row in degraded_unavail["items"]] == ["alert-quota-1"]

    def raise_store_unavailable(*args, **kwargs):
        raise BusinessError("STORE_UNAVAILABLE")

    monkeypatch.setattr(v13_w9_canary_markers, "w9_write_many", raise_store_unavailable)
    degraded_store = v13_w9_canary_markers.list_triggers(call)
    assert degraded_store["persistence_degraded"] is True
    assert degraded_store["persistence_warning"] == "STORE_UNAVAILABLE"
    assert [row["source_ref"] for row in degraded_store["items"]] == ["alert-quota-1"]


def test_active_references_and_resolver_scan_past_caps_and_fail_closed(tmp_path):
    owner_home = tmp_path / "owner-13"
    owner_home.mkdir(parents=True)
    clean_host = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home), conversation_store=None)
    clean_probe = business_host_ports.build_default_business_active_references(clean_host)
    assert clean_probe("sp-1", None) == []
    assert not (owner_home / "data" / "workflow_run_index.sqlite3").exists()

    # 1. 构造 1 条更早的活跃 WorkflowRun + 60 条更新的已完结 WorkflowRun（超过原 limit=50 截断）。
    rstore = WorkflowRunStore(owner_home)
    rstore.create_run(
        WorkflowRunRecord(
            workflow_run_id="run-old-active",
            workflow_id="wf-1",
            owner_id="local/main",
            trigger_kind="manual",
            status="running",
            started_at=10.0,
            ended_at=0.0,
            duration_ms=0.0,
            business_context={"space_id": "space-nw-13"},
        )
    )
    for idx in range(60):
        rstore.create_run(
            WorkflowRunRecord(
                workflow_run_id=f"run-done-{idx:03d}",
                workflow_id="wf-1",
                owner_id="local/main",
                trigger_kind="manual",
                status="succeeded",
                started_at=100.0 + idx,
                ended_at=101.0 + idx,
                duration_ms=10.0,
                business_context={"space_id": "space-nw-13"},
            )
        )

    # 2. 构造 >110 条线程（活跃后台线程排在 100 名之后）与多页 SessionTask。
    tstore = ThreadStore(ConversationStorage(owner_home))
    active_thread = tstore.create(canonical_user_id="local/main", title="worker-old-agent", owner_id="local/main", owner_home=str(owner_home))
    tstore.update_atomic(
        active_thread.thread_id,
        lambda cur: replace(
            cur,
            metadata={"background_agent": {"space_id": "space-nw-13", "identity_state": "running", "name": "worker-old-agent"}},
        ),
    )
    for idx in range(115):
        retired_thread = tstore.create(canonical_user_id="local/main", title=f"retired-{idx}", owner_id="local/main", owner_home=str(owner_home))
        tstore.update_atomic(
            retired_thread.thread_id,
            lambda cur, n=idx: replace(
                cur,
                metadata={"background_agent": {"space_id": "space-nw-13", "identity_state": "retired", "name": f"retired-{n}"}},
            ),
        )

    class _PagedSessionTasks:
        def list_page(self, *, limit=100, cursor=None):
            if cursor is None:
                items = [
                    SimpleNamespace(task_id=f"t-done-{i}", status="completed", metadata={"space_id": "space-nw-13"})
                    for i in range(100)
                ]
                return {"items": items, "next_cursor": "page-2"}
            assert cursor == "page-2"
            return {
                "items": [SimpleNamespace(task_id="t-active-tail", status="running", metadata={"space_id": "space-nw-13"})],
                "next_cursor": None,
            }

    host = SimpleNamespace(
        home_paths=SimpleNamespace(owner_id="local/main", owner_home_dir=owner_home),
        conversation_store=SimpleNamespace(threads=tstore, session_tasks=_PagedSessionTasks()),
    )
    active = business_host_ports.build_default_business_active_references(host)("space-nw-13", None)
    by_kind = {item["kind"]: item["ref"] for item in active}
    assert by_kind["workflow_run"] == "run-old-active"
    assert by_kind["agent"] == active_thread.thread_id
    assert by_kind["task"] == "t-active-tail"

    resolver = business_host_ports.build_default_business_space_reference_resolver(host)
    resolved = resolver("agent", "worker-old-agent", None)
    assert resolved is not None and resolved["state"] == "available" and resolved["space_id"] == "space-nw-13"

    # 3. 任意数据源返回 partial、游标停滞或读盘错误必须 fail-closed 抛 HOST_NOT_READY。
    bad_tasks_host = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        conversation_store=SimpleNamespace(
            threads=tstore,
            session_tasks=SimpleNamespace(list_page=lambda **_: {"items": [], "partial": True}),
        ),
    )
    with pytest.raises(BusinessError) as exc_partial:
        business_host_ports.build_default_business_active_references(bad_tasks_host)("space-nw-13", None)
    assert exc_partial.value.code == "HOST_NOT_READY"

    stalled_tasks_host = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        conversation_store=SimpleNamespace(
            threads=tstore,
            session_tasks=SimpleNamespace(list_page=lambda **_: {"items": [], "next_cursor": "same-cursor"}),
        ),
    )
    with pytest.raises(BusinessError) as exc_stalled:
        business_host_ports.build_default_business_active_references(stalled_tasks_host)("space-nw-13", None)
    assert exc_stalled.value.code == "HOST_NOT_READY"

    # 损坏 ThreadStore 报告错误时亦须 fail-closed。
    bad_threads_host = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        conversation_store=SimpleNamespace(
            threads=SimpleNamespace(list_report=lambda **_: ([], [{"error": "corrupt"}])),
            session_tasks=_PagedSessionTasks(),
        ),
    )
    with pytest.raises(BusinessError) as exc_thread:
        business_host_ports.build_default_business_active_references(bad_threads_host)("space-nw-13", None)
    assert exc_thread.value.code == "HOST_NOT_READY"


def test_fs_watch_control_ring_evicts_and_never_bricks(tmp_path, monkeypatch):
    ring_size = 6
    monkeypatch.setattr(gap158_w7_fs_watch, "FS_WATCH_EVENT_RING", ring_size)

    class _EpochRepo:
        def __init__(self):
            self.lock = threading.RLock()
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
            self.conn.commit()

        @contextmanager
        def transaction(self):
            with self.lock, self.conn:
                yield self.conn

    owner_home = tmp_path / "watch-owner"
    watch_root = owner_home / "workspace"
    watch_root.mkdir(parents=True)
    (watch_root / "note.txt").write_text("alpha\n", encoding="utf-8")

    clock_state = {"now": 1000.0}
    epoch_repo = _EpochRepo()
    host = SimpleNamespace(
        home_paths=SimpleNamespace(owner_id="local/watch", owner_home_dir=owner_home, owner_workspace_dir=watch_root),
        config=SimpleNamespace(workspace_root="", business_spaces_enabled=True),
        subagents=SimpleNamespace(runtime_db=epoch_repo),
    )
    context = bind_business_context(host, authorize=lambda *_: True, subject_id="watch-sub")
    services = SimpleNamespace(owner_id="local/watch")

    def call_watch(action, params, key):
        return V13Call(
            f"fs_watch.{action}",
            params,
            "req-watch",
            "local/watch",
            "watch-sub",
            context,
            services,
            owner_home,
            key,
            clock=lambda: clock_state["now"],
        )

    try:
        # 1. 超过 FS_WATCH_EVENT_RING + 25 次连续订阅/重挂/退订循环绝不锁死。
        last_sid = None
        last_key = None
        last_res = None
        for idx in range(ring_size + 25):
            clock_state["now"] += 1.0
            sub_res = gap158_w7_fs_watch.subscribe(call_watch("subscribe", {"root": "."}, f"sub-{idx}"))
            sid = sub_res["subscription_id"]
            gap158_w7_fs_watch.remount(
                call_watch("remount", {"subscription_id": sid}, f"rem-{idx}")
            )
            last_sid = sid
            last_key = f"unsub-{idx}"
            last_res = gap158_w7_fs_watch.unsubscribe(call_watch("unsubscribe", {"subscription_id": sid}, last_key))

        state = gap158_w7_fs_watch._state(call_watch("subscribe", {"root": "."}, "probe"))
        assert len(state.receipts) <= ring_size
        assert last_res == {"removed": True}
        assert gap158_w7_fs_watch.unsubscribe(call_watch("unsubscribe", {"subscription_id": last_sid}, last_key)) == last_res

        # 2. 即使 ring 被搜索填满，remount / search_cancel / unsubscribe 绝不返回 503 OUTCOME_UNKNOWN。
        sub_main = gap158_w7_fs_watch.subscribe(call_watch("subscribe", {"root": "."}, "main-sub"))
        main_sid = sub_main["subscription_id"]
        search_ids = []
        for idx in range(ring_size - 1):
            clock_state["now"] += 1.0
            sres = gap158_w7_fs_watch.search(
                call_watch("search", {"subscription_id": main_sid, "query": f"alpha-{idx}"}, f"srch-{idx}")
            )
            search_ids.append(sres["search_id"])
            job = state.searches.get(sres["search_id"])
            if job is not None:
                job.done.wait(timeout=2.0)

        rem = gap158_w7_fs_watch.remount(
            call_watch("remount", {"subscription_id": main_sid}, "remount-full")
        )
        assert rem["epoch"] > sub_main["epoch"]
        gap158_w7_fs_watch.search_cancel(call_watch("search_cancel", {"search_id": search_ids[-1]}, "cancel-full"))

        # 3. 时钟跨过空闲有效期后，search 亦应淘汰过期回执而非永久卡死。
        clock_state["now"] += gap158_w7_fs_watch.FS_WATCH_IDLE_EXPIRY_SECONDS + 10.0
        sub_fresh = gap158_w7_fs_watch.subscribe(call_watch("subscribe", {"root": "."}, "fresh-sub"))
        fresh_search = gap158_w7_fs_watch.search(
            call_watch("search", {"subscription_id": sub_fresh["subscription_id"], "query": "alpha"}, "fresh-srch")
        )
        assert "search_id" in fresh_search
        gap158_w7_fs_watch.unsubscribe(call_watch("unsubscribe", {"subscription_id": sub_fresh["subscription_id"]}, "unsub-fresh"))
        gap158_w7_fs_watch.unsubscribe(call_watch("unsubscribe", {"subscription_id": main_sid}, "unsub-main-cleanup"))
    finally:
        owner_state = gap158_w7_fs_watch._owner_states.pop(("local/watch", str(owner_home.resolve())), None)
        if owner_state is not None:
            for job in list(owner_state.searches.values()):
                job.cancel.set()
            for job in list(owner_state.searches.values()):
                job.done.wait(timeout=2.0)
        epoch_repo.conn.close()


def test_observation_readers_stream_media_registry_beyond_2_mib(tmp_path):
    owner_home = tmp_path / "media-owner"
    reg_path = owner_home / "artifacts" / "business" / REGISTRY_RELATIVE_PATH
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    owner_id = "local/media-test"

    # 生成 6,000 行（> 2.5 MiB）业务上传登记，末行并含同一 ID 的最新版本覆盖。
    lines = []
    for idx in range(5999):
        aid = f"artifact-{idx:040x}"
        row_record = {
            "schema_version": "artifact-registry.v1",
            "artifact_id": aid,
            "run_id": "",
            "task_id": "",
            "agent_id": "",
            "kind": "document",
            "mime_type": "text/plain",
            "path": str(owner_home / "artifacts" / "business" / f"{aid}.blob"),
            "sha256": hashlib.sha256(aid.encode()).hexdigest(),
            "size_bytes": 128,
            "status": "ready",
            "source": "business_upload",
            "created_by_tool": "business_upload",
            "updated_at": float(idx),
            "metadata": {
                "owner_id": owner_id,
                "space_id": "sp-media",
                "version_ref": f"ver-{idx:04d}",
                "display_name": f"file-{idx:04d}.txt",
                "padding": "p" * 80,
            },
        }
        lines.append(json.dumps(row_record, separators=(",", ":")))

    target_aid = f"artifact-{5998:040x}"
    superseded = json.loads(lines[-1])
    superseded["metadata"]["display_name"] = "superseded-final.txt"
    superseded["metadata"]["version_ref"] = "ver-superseded"
    lines.append(json.dumps(superseded, separators=(",", ":")))
    reg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert reg_path.stat().st_size > int(2.5 * 1024 * 1024)

    readers = default_observation_readers(owner_home, owner_id, None, None)
    page1 = readers["artifacts"](
        {"limit": 25},
        source_page=ObservationSourcePage(limit=25, position={"phase": "media", "after": ""}),
    )
    assert len(page1.records) == 25 and page1.next_position is not None
    assert page1.coverage == "partial"

    page2 = readers["artifacts"](
        {"limit": 25},
        source_page=ObservationSourcePage(limit=25, position=page1.next_position),
    )
    assert len(page2.records) == 25
    assert page2.records[0].value["artifact_id"] > page1.records[-1].value["artifact_id"]

    # 点查询超过 2.5 MiB 须正常返回末条覆盖后的记录。
    detail = readers["artifacts.get"]({"artifact_id": target_aid})
    assert len(detail.records) == 1
    assert detail.records[0].value["artifact"]["name"] == "superseded-final.txt"
    assert detail.records[0].value["version_ref"] == "ver-superseded"

    # 原文件变化后使用旧 media_revision 游标仍应准确触发 CURSOR_EXPIRED。
    with reg_path.open("a", encoding="utf-8") as fh:
        extra = json.loads(lines[0])
        extra["artifact_id"] = f"artifact-{7000:040x}"
        fh.write(json.dumps(extra, separators=(",", ":")) + "\n")
    with pytest.raises(BusinessError) as exc_cursor:
        readers["artifacts"](
            {"limit": 25},
            source_page=ObservationSourcePage(limit=25, position=page1.next_position),
        )
    assert exc_cursor.value.code == "CURSOR_EXPIRED"

    # 单行超出 1 MiB 上限时须按原预算失败，而不是无界内存读入。
    oversized_path = owner_home / "artifacts" / "business" / REGISTRY_RELATIVE_PATH
    oversized_path.write_text('{"bad":"' + ("x" * (1024 * 1024 + 64)) + '"}\n', encoding="utf-8")
    with pytest.raises(ObservationSourceError) as exc_list_budget:
        readers["artifacts"](
            {"limit": 10},
            source_page=ObservationSourcePage(limit=10, position={"phase": "media", "after": ""}),
        )
    assert exc_list_budget.value.details["reason"] == "ARTIFACT_MEDIA_REGISTRY_READ_BUDGET"
    with pytest.raises(ObservationSourceError) as exc_get_budget:
        readers["artifacts.get"]({"artifact_id": target_aid})
    assert exc_get_budget.value.details["reason"] == "ARTIFACT_REGISTRY_READ_BUDGET"

    # 坏行仍须报不可读错误，不跳过静默成功。
    oversized_path.write_text("{corrupt-jsonl\n", encoding="utf-8")
    with pytest.raises(ObservationSourceError) as exc_bad_line:
        readers["artifacts.get"]({"artifact_id": target_aid})
    assert exc_bad_line.value.details["reason"] == "ARTIFACT_REGISTRY_UNREADABLE"
