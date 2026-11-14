"""NW37-R3 回归测试：V13 提交后补偿保护（skills/canary）与空间归档完整扫描门（U-05/U-06/U-07）。"""
from __future__ import annotations

import errno
import http.client
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_store as v13_store_mod
from agent_py_agent.agent.business_spaces import (
    v13_w9_canary_markers as markers_mod,
)
from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.directory_lock import locked_private_directory
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api as business_api_mod
from agent_py_agent.agent.gateway_parts import business_host_ports as host_ports
from agent_py_agent.agent.gateway_parts.business_upload_registry import (
    lookup_business_upload,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_quota import (
    owner_quota_enforcer_from_policy,
)
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

USER_ID = "business-alice"
REALISTIC_DISK_MB = 100


def _skill_markdown(name: str, summary: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {summary}\n---\n\n"
        f"# {name}\n\n{summary}\n"
    )


def _set_owner_quota_mb(agent: SimpleAgent, disk_mb: int) -> int:
    """将测试 owner 的 quota.json 调整为指定真实生产配额（如 100 MB）。"""
    home_root = agent.home_paths.root
    owner_home = agent.home_paths.owner_home_dir
    quota_parts = (owner_home / "quota.json").relative_to(home_root).parts
    raw = nofollow_fs.read_bytes_beneath(
        home_root, quota_parts, max_bytes=16384, require_dir_fd=True
    )
    assert raw is not None
    quota_obj = json.loads(raw)
    quota_obj["max_disk_mb"] = disk_mb
    encoded = json.dumps(quota_obj, sort_keys=True).encode("utf-8")
    nofollow_fs.write_bytes_atomic_beneath(home_root, quota_parts, encoded)
    enforcer = owner_quota_enforcer_from_policy(owner_home)
    assert enforcer.max_bytes == disk_mb * 1024 * 1024
    return enforcer.max_bytes


@pytest.fixture
def r3_native(tmp_path, monkeypatch):
    """启动真实 GatewayHTTPServer 与隔离 owner home，支持直测 /client/business。"""
    root = tmp_path / "native-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
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
    host_ports.wire_business_host_ports(agent)
    _set_owner_quota_mb(agent, REALISTIC_DISK_MB)
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
    server: GatewayHTTPServer,
    action: str,
    params: dict,
    *,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> tuple[int, dict]:
    """经真实 HTTP POST /client/business 路由调用业务动作并返回状态码与标准信封。"""
    request_body: dict = {
        "action": action,
        "request_id": "r3-" + uuid4().hex,
        "params": params,
    }
    if idempotency_key is not None:
        request_body["idempotency_key"] = idempotency_key
    if expected_revision is not None:
        request_body["expected_revision"] = expected_revision
    conn = http.client.HTTPConnection(
        "127.0.0.1", server.server.server_address[1], timeout=30
    )
    headers = {
        "Content-Type": "application/json",
        "Connection": "close",
        "X-Gateway-Token": server._local_client_credential,
        "X-User-Id": USER_ID,
        "X-Channel": "feishu",
    }
    try:
        conn.request("POST", "/client/business", json.dumps(request_body), headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def _create_space(server: GatewayHTTPServer, name: str) -> tuple[str, int]:
    status, body = _http_business(
        server,
        "spaces.create",
        {"name": name},
        idempotency_key="r3-space-" + uuid4().hex,
    )
    assert status == 200, body
    data = body["data"]
    return data["space_id"], data["revision"]


def _arm_materialization_error(
    monkeypatch,
    target_filename: str,
    *,
    mode: str = "post_journal",
    fail_journal_read: bool = False,
) -> dict:
    """按写入阶段确定性触发一次 OSError(EIO)，模拟物化失败或日志前失败。"""
    orig_write = v13_store_mod.write_bytes_atomic_beneath
    orig_read = v13_store_mod.read_bytes_beneath
    state = {
        "armed": True,
        "journal_written": False,
        "pre_journal_failed": False,
        "materialize_failed": False,
        "read_failed": False,
    }

    def wrapped_write(root, parts, raw, *args, **kwargs):
        leaf = parts[-1] if parts else ""
        if state["armed"] and leaf == ".transaction.json":
            parsed = json.loads(raw) if raw else {}
            if parsed:
                if mode == "pre_journal":
                    state["armed"] = False
                    state["pre_journal_failed"] = True
                    raise OSError(errno.EIO, "simulated pre-journal write error")
                state["journal_written"] = True
        elif (
            state["armed"]
            and mode == "post_journal"
            and state["journal_written"]
            and leaf == target_filename
        ):
            state["armed"] = False
            state["materialize_failed"] = True
            raise OSError(errno.EIO, "simulated post-journal materialize error")
        return orig_write(root, parts, raw, *args, **kwargs)

    def wrapped_read(root, parts, *args, **kwargs):
        leaf = parts[-1] if parts else ""
        if (
            fail_journal_read
            and state["materialize_failed"]
            and not state["read_failed"]
            and leaf in {".transaction.json", target_filename}
        ):
            state["read_failed"] = True
            raise OSError(errno.EIO, "simulated durable receipt read error")
        return orig_read(root, parts, *args, **kwargs)

    monkeypatch.setattr(v13_store_mod, "write_bytes_atomic_beneath", wrapped_write)
    if fail_journal_read:
        monkeypatch.setattr(v13_store_mod, "read_bytes_beneath", wrapped_read)
    return state


def test_skills_lifecycle_post_journal_materialization_error_preserves_asset_and_replays(
    r3_native, monkeypatch
):
    """U-07：skills.create/update/retire/activate 在日志落盘后物化失败时不得回滚原生资产，重放须幂等返回 200。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir
    skill_id = "r3-eio-skill"
    skill_dir = owner_home / "skills" / skill_id

    # 1. skills.create：先持久化 .transaction.json 回执，再在 skill_operations.json 物化时报错
    create_key = "r3-skill-create-" + uuid4().hex
    v1_body = _skill_markdown(skill_id, "version one workflow")
    state = _arm_materialization_error(monkeypatch, "skill_operations.json", mode="post_journal")
    status, err_body = _http_business(
        server,
        "skills.create",
        {"skill_id": skill_id, "body_markdown": v1_body},
        idempotency_key=create_key,
    )
    assert state["journal_written"] and state["materialize_failed"]
    assert status == 503, err_body
    assert err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    assert err_body["effect_outcome"] == "unknown"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == v1_body

    # 解除错误后同键重放，必须直接返回成功且保留原技能
    status, created = _http_business(
        server,
        "skills.create",
        {"skill_id": skill_id, "body_markdown": v1_body},
        idempotency_key=create_key,
    )
    assert status == 200, created
    assert created["state"] == "completed"
    v1_ref = created["data"]["skill"]["version_ref"]

    # 2. skills.update：日志落盘后物化报错，更新后的 v2 正文必须保留而非回退到 v1
    update_key = "r3-skill-update-" + uuid4().hex
    v2_body = _skill_markdown(skill_id, "version two workflow")
    _arm_materialization_error(monkeypatch, "skill_operations.json", mode="post_journal")
    status, err_body = _http_business(
        server,
        "skills.update",
        {
            "skill_id": skill_id,
            "body_markdown": v2_body,
            "expected_version_ref": v1_ref,
        },
        idempotency_key=update_key,
    )
    assert status == 503, err_body
    assert err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == v2_body

    status, updated = _http_business(
        server,
        "skills.update",
        {
            "skill_id": skill_id,
            "body_markdown": v2_body,
            "expected_version_ref": v1_ref,
        },
        idempotency_key=update_key,
    )
    assert status == 200, updated
    assert updated["data"]["skill"]["version_ref"] != v1_ref
    status, fetched = _http_business(server, "skills.get", {"skill_id": skill_id})
    assert status == 200 and fetched["data"]["body_markdown"] == v2_body

    # 3. skills.retire：日志落盘后物化报错，enabled=False 必须保留
    retire_key = "r3-skill-retire-" + uuid4().hex
    _arm_materialization_error(monkeypatch, "skill_operations.json", mode="post_journal")
    status, err_body = _http_business(
        server, "skills.retire", {"skill_id": skill_id}, idempotency_key=retire_key
    )
    assert status == 503 and err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    meta = json.loads((skill_dir / "skill_meta.json").read_text(encoding="utf-8"))
    assert meta["enabled"] is False

    status, retired = _http_business(
        server, "skills.retire", {"skill_id": skill_id}, idempotency_key=retire_key
    )
    assert status == 200 and retired["data"]["skill"]["enabled"] is False

    # 4. skills.activate：日志落盘后物化报错，enabled=True 必须保留
    activate_key = "r3-skill-activate-" + uuid4().hex
    _arm_materialization_error(monkeypatch, "skill_operations.json", mode="post_journal")
    status, err_body = _http_business(
        server, "skills.activate", {"skill_id": skill_id}, idempotency_key=activate_key
    )
    assert status == 503 and err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    meta = json.loads((skill_dir / "skill_meta.json").read_text(encoding="utf-8"))
    assert meta["enabled"] is True

    status, activated = _http_business(
        server, "skills.activate", {"skill_id": skill_id}, idempotency_key=activate_key
    )
    assert status == 200 and activated["data"]["skill"]["enabled"] is True
    status, fetched = _http_business(server, "skills.get", {"skill_id": skill_id})
    assert status == 200 and fetched["data"]["skill"]["enabled"] is True


def test_skills_create_pre_journal_and_unreadable_receipt_compensation(
    r3_native, monkeypatch
):
    """U-07：日志前失败必须干净回滚原生技能；回执读取失败（未知状态）时必须保留原生技能。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir

    # 1. 日志前失败（.transaction.json 未写成功）：应执行 rollback 删除原生技能目录
    pre_skill = "r3-prejournal-skill"
    with monkeypatch.context() as mp:
        pre_state = _arm_materialization_error(
            mp, "skill_operations.json", mode="pre_journal"
        )
        status, err_body = _http_business(
            server,
            "skills.create",
            {
                "skill_id": pre_skill,
                "body_markdown": _skill_markdown(pre_skill, "pre-journal cleanup check"),
            },
            idempotency_key="r3-pre-" + uuid4().hex,
        )
        assert pre_state["pre_journal_failed"] is True, (pre_state, err_body.get("error"))
    assert status == 503 and err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    assert not (owner_home / "skills" / pre_skill).exists()
    status, get_missing = _http_business(server, "skills.get", {"skill_id": pre_skill})
    assert status == 404, get_missing

    # 2. 日志落盘后物化失败，且紧接着核查耐久回执时读盘失败：不能确认未提交，须保留原生技能
    unreadable_skill = "r3-unreadable-skill"
    unreadable_key = "r3-unreadable-" + uuid4().hex
    unreadable_md = _skill_markdown(unreadable_skill, "preserve on unknown state")
    with monkeypatch.context() as mp:
        state = _arm_materialization_error(
            mp,
            "skill_operations.json",
            mode="post_journal",
            fail_journal_read=True,
        )
        status, err_body = _http_business(
            server,
            "skills.create",
            {"skill_id": unreadable_skill, "body_markdown": unreadable_md},
            idempotency_key=unreadable_key,
        )
        assert state["journal_written"] and state["materialize_failed"], (state, err_body.get("error"))
        assert state["read_failed"] is True, (state, err_body.get("error"))
    assert status == 503 and err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    assert (owner_home / "skills" / unreadable_skill / "SKILL.md").read_text(
        encoding="utf-8"
    ) == unreadable_md

    # 磁盘恢复可读后重放同一幂等键，直接收敛返回 200
    status, replayed = _http_business(
        server,
        "skills.create",
        {"skill_id": unreadable_skill, "body_markdown": unreadable_md},
        idempotency_key=unreadable_key,
    )
    assert status == 200 and replayed["data"]["skill"]["skill_id"] == unreadable_skill


def test_canary_marker_document_post_journal_error_and_conflict_guard(
    r3_native, monkeypatch
):
    """U-07：honeytokens.create(document) 日志后物化失败不删除已上传附件，重放正常完成且同键冲突保护已登记附件。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir
    space_id, _ = _create_space(server, "金丝雀文档空间")

    # 1. 日志落盘后 canary_markers.json 物化报错：上传文档必须保留，重放返回 200 与同一 token_id/artifact_id
    doc_key = "r3-canary-post-" + uuid4().hex
    with monkeypatch.context() as mp:
        state = _arm_materialization_error(
            mp, "canary_markers.json", mode="post_journal"
        )
        status, err_body = _http_business(
            server,
            "honeytokens.create",
            {"space_id": space_id, "kind": "document", "label": "doc-post-journal"},
            idempotency_key=doc_key,
        )
        assert state["journal_written"] and state["materialize_failed"]
    assert status == 503, err_body
    assert err_body["error"]["code"] == "OUTCOME_UNKNOWN"

    uploads_before_replay = _http_business(
        server, "artifacts.list", {"filter": {"space_id": space_id}}
    )
    assert uploads_before_replay[0] == 200, uploads_before_replay[1]
    staged_items = uploads_before_replay[1]["data"]["items"]
    assert len(staged_items) == 1
    art_id = staged_items[0]["artifact_id"]
    assert lookup_business_upload(owner_home, art_id).record is not None

    status, replayed = _http_business(
        server,
        "honeytokens.create",
        {"space_id": space_id, "kind": "document", "label": "doc-post-journal"},
        idempotency_key=doc_key,
    )
    assert status == 200, replayed
    assert replayed["state"] == "completed"
    marker = replayed["data"]["marker"]
    assert marker["artifact"]["artifact_id"] == art_id
    assert marker["state"] == "active"
    assert lookup_business_upload(owner_home, art_id).record is not None

    # 2. IDEMPOTENCY_CONFLICT 分支核查：经模块内调用点捕获真实 BusinessCall，确认保护已登记附件并允许清理无关孤儿
    captured_calls = []
    orig_precheck = markers_mod._precheck_w9_receipt

    def capture_precheck(call, collection):
        captured_calls.append(call)
        return orig_precheck(call, collection)

    monkeypatch.setattr(markers_mod, "_precheck_w9_receipt", capture_precheck)
    status_replay, _ = _http_business(
        server,
        "honeytokens.create",
        {"space_id": space_id, "kind": "document", "label": "doc-post-journal"},
        idempotency_key=doc_key,
    )
    assert status_replay == 200 and captured_calls
    conflict_call = replace(
        captured_calls[0],
        params={**captured_calls[0].params, "label": "different-race-label"},
    )
    assert markers_mod._can_compensate_marker_upload(conflict_call, art_id) is False
    assert (
        markers_mod._can_compensate_marker_upload(conflict_call, "art-unrecorded-orphan")
        is True
    )


def test_canary_marker_document_pre_journal_error_rolls_back(r3_native, monkeypatch):
    """U-07：honeytokens.create(document) 日志写入前失败时，_rollback_document 必须清理暂存附件，不留孤儿字节。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir
    space_id, _ = _create_space(server, "金丝雀日志前失败空间")

    rolled_back_ids = []
    orig_rollback_doc = markers_mod._rollback_document

    def tracked_rollback(call, artifact):
        rolled_back_ids.append(artifact.get("artifact_id"))
        return orig_rollback_doc(call, artifact)

    with monkeypatch.context() as mp:
        mp.setattr(markers_mod, "_rollback_document", tracked_rollback)
        pre_state = _arm_materialization_error(
            mp, "canary_markers.json", mode="pre_journal"
        )
        status, err_body = _http_business(
            server,
            "honeytokens.create",
            {"space_id": space_id, "kind": "document", "label": "doc-pre-journal"},
            idempotency_key="r3-canary-pre-" + uuid4().hex,
        )
        assert pre_state["pre_journal_failed"] is True, (
            pre_state,
            err_body.get("error"),
        )
    assert status == 503 and err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    assert len(rolled_back_ids) == 1
    assert lookup_business_upload(owner_home, rolled_back_ids[0]).record is None
    status_after_pre, after_pre_list = _http_business(
        server, "artifacts.list", {"filter": {"space_id": space_id}}
    )
    assert status_after_pre == 200
    assert after_pre_list["data"]["items"] == []


def test_canary_marker_document_unreadable_receipt_preserves_upload(
    r3_native, monkeypatch
):
    """U-07：honeytokens.create(document) 日志后物化失败且回执读取报错时，不得误删附件，恢复可读后重放成功。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir
    space_id, _ = _create_space(server, "金丝雀回执未知态空间")

    unreadable_key = "r3-canary-unreadable-" + uuid4().hex
    with monkeypatch.context() as mp:
        state = _arm_materialization_error(
            mp,
            "canary_markers.json",
            mode="post_journal",
            fail_journal_read=True,
        )
        status, err_body = _http_business(
            server,
            "honeytokens.create",
            {"space_id": space_id, "kind": "document", "label": "doc-unreadable"},
            idempotency_key=unreadable_key,
        )
        assert state["journal_written"] and state["materialize_failed"], (
            state,
            err_body.get("error"),
        )
        assert state["read_failed"] is True, (state, err_body.get("error"))
    assert status == 503 and err_body["error"]["code"] == "OUTCOME_UNKNOWN"
    status_mid, mid_list = _http_business(
        server, "artifacts.list", {"filter": {"space_id": space_id}}
    )
    assert status_mid == 200 and len(mid_list["data"]["items"]) == 1
    art_id = mid_list["data"]["items"][0]["artifact_id"]
    status_ok, replayed_ok = _http_business(
        server,
        "honeytokens.create",
        {"space_id": space_id, "kind": "document", "label": "doc-unreadable"},
        idempotency_key=unreadable_key,
    )
    assert status_ok == 200
    assert replayed_ok["data"]["marker"]["artifact"]["artifact_id"] == art_id
    assert lookup_business_upload(owner_home, art_id).record is not None


def test_spaces_archive_session_tasks_coverage_and_load_errors(r3_native):
    """U-05：坏会话任务文件或非完整 list_page 分页不得静默放行 spaces.archive，修复后应正常归档。"""
    server, agent = r3_native
    space_id, revision = _create_space(server, "会话任务完整性验证空间")

    # 1. 真实 SessionTaskStore 目录写入损坏 JSON 文件 bad.json -> 阻塞归档（HOST_NOT_READY）
    task_dir = agent.conversation_store.session_tasks._dir()
    task_dir.mkdir(parents=True, exist_ok=True)
    bad_file = task_dir / "bad.json"
    bad_file.write_text("{corrupt json", encoding="utf-8")

    with pytest.raises(BusinessError) as exc_info:
        host_ports._list_all_session_tasks(agent.conversation_store.session_tasks)
    assert exc_info.value.code == "HOST_NOT_READY"
    assert exc_info.value.details["reason"] == "SESSION_TASK_SCAN_INCOMPLETE"

    status, archive_blocked = _http_business(
        server,
        "spaces.archive",
        {"space_id": space_id},
        idempotency_key="r3-archive-bad-" + uuid4().hex,
        expected_revision=revision,
    )
    assert status == 503, archive_blocked
    assert archive_blocked["error"]["code"] == "HOST_NOT_READY"

    # 清除损坏文件后再次归档，应正常成功
    bad_file.unlink()
    status, archived = _http_business(
        server,
        "spaces.archive",
        {"space_id": space_id},
        idempotency_key="r3-archive-ok-" + uuid4().hex,
        expected_revision=revision,
    )
    assert status == 200, archived
    assert archived["data"]["space"]["state"] == "archived"

    # 2. list_page 测试替身与实例覆写：coverage="partial"/load_errors 或 "expired" 必须 fail-closed
    partial_double = SimpleNamespace(
        list_page=lambda **_kw: {
            "items": [],
            "next_cursor": None,
            "coverage": "partial",
            "load_errors": ["bad.json"],
        }
    )
    with pytest.raises(BusinessError) as exc_partial:
        host_ports._list_all_session_tasks(partial_double)
    assert exc_partial.value.code == "HOST_NOT_READY"

    expired_double = SimpleNamespace(
        list_page=lambda **_kw: {
            "items": [],
            "next_cursor": None,
            "coverage": "expired",
            "load_errors": [],
        }
    )
    with pytest.raises(BusinessError) as exc_expired:
        host_ports._list_all_session_tasks(expired_double)
    assert exc_expired.value.code == "HOST_NOT_READY"

    real_store = agent.conversation_store.session_tasks
    try:
        real_store.list_page = lambda **_kw: {
            "items": [],
            "next_cursor": None,
            "coverage": "partial",
            "load_errors": ["bad.json"],
        }
        with pytest.raises(BusinessError) as exc_override:
            host_ports._list_all_session_tasks(real_store)
        assert exc_override.value.code == "HOST_NOT_READY"

        real_store.list_page = lambda **_kw: {
            "items": [],
            "next_cursor": None,
            "coverage": "complete",
            "load_errors": [],
        }
        assert host_ports._list_all_session_tasks(real_store) == []
    finally:
        real_store.__dict__.pop("list_page", None)


def test_spaces_archive_skipped_workflow_run_allowed_while_active_blocked(r3_native):
    """U-06：终态 skipped 工作流运行不得阻塞 spaces.archive；running/paused 仍阻塞；SQL 参数化严格跟随 settled_states。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir
    run_store = WorkflowRunStore(owner_home)

    skipped_space_id, skipped_rev = _create_space(server, "跳过运行可归档空间")
    running_space_id, running_rev = _create_space(server, "活跃运行阻塞归档空间")
    paused_space_id, paused_rev = _create_space(server, "暂停运行阻塞归档空间")

    base_ts = 1700000000.0
    run_store.create_run(
        WorkflowRunRecord(
            workflow_run_id="wfrun_skipped_01",
            workflow_id="wf_concurrency",
            owner_id="local/main",
            trigger_kind="manual",
            status="skipped",
            started_at=base_ts,
            ended_at=base_ts + 0.5,
            duration_ms=500.0,
            business_context={"space_id": skipped_space_id},
        )
    )
    run_store.create_run(
        WorkflowRunRecord(
            workflow_run_id="wfrun_running_01",
            workflow_id="wf_active",
            owner_id="local/main",
            trigger_kind="manual",
            status="running",
            started_at=base_ts,
            ended_at=0.0,
            duration_ms=0.0,
            business_context={"space_id": running_space_id},
        )
    )
    run_store.create_run(
        WorkflowRunRecord(
            workflow_run_id="wfrun_paused_01",
            workflow_id="wf_paused",
            owner_id="local/main",
            trigger_kind="manual",
            status="paused",
            started_at=base_ts,
            ended_at=0.0,
            duration_ms=0.0,
            business_context={"space_id": paused_space_id},
        )
    )

    # 1. skipped 不应再阻止空间归档
    status, skipped_archive = _http_business(
        server,
        "spaces.archive",
        {"space_id": skipped_space_id},
        idempotency_key="r3-archive-skip-" + uuid4().hex,
        expected_revision=skipped_rev,
    )
    assert status == 200, skipped_archive
    assert skipped_archive["data"]["blockers"] == []
    assert skipped_archive["data"]["space"]["state"] == "archived"

    # 2. running 与 paused 仍必须阻塞归档，并保留 active 状态
    status, running_archive = _http_business(
        server,
        "spaces.archive",
        {"space_id": running_space_id},
        idempotency_key="r3-archive-run-" + uuid4().hex,
        expected_revision=running_rev,
    )
    assert status == 200, running_archive
    assert running_archive["data"]["space"]["state"] == "active"
    assert running_archive["data"]["blockers"] == [
        {
            "ref": "wfrun_running_01",
            "kind": "workflow_run",
            "reason_code": "ACTIVE_WORKFLOW_RUN",
        }
    ]

    status, paused_archive = _http_business(
        server,
        "spaces.archive",
        {"space_id": paused_space_id},
        idempotency_key="r3-archive-pause-" + uuid4().hex,
        expected_revision=paused_rev,
    )
    assert status == 200, paused_archive
    assert paused_archive["data"]["space"]["state"] == "active"
    assert paused_archive["data"]["blockers"] == [
        {
            "ref": "wfrun_paused_01",
            "kind": "workflow_run",
            "reason_code": "ACTIVE_WORKFLOW_RUN",
        }
    ]

    # 3. 直接核查 _list_unsettled_workflow_runs 参数化 SQL 对自定义 settled_states 的过滤
    default_settled = {
        "succeeded",
        "completed",
        "done",
        "failed",
        "cancelled",
        "aborted",
        "retired",
        "settled",
        "skipped",
    }
    unsettled_default = {
        run_id
        for run_id, _ in host_ports._list_unsettled_workflow_runs(
            owner_home, default_settled
        )
    }
    assert unsettled_default == {"wfrun_running_01", "wfrun_paused_01"}

    unsettled_custom = {
        run_id
        for run_id, _ in host_ports._list_unsettled_workflow_runs(
            owner_home, {"running"}
        )
    }
    assert unsettled_custom == {"wfrun_skipped_01", "wfrun_paused_01"}


def _count_open_fds() -> int:
    for fd_dir in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
    return -1


def _run_blocked_skill_race(
    server: GatewayHTTPServer,
    monkeypatch,
    action_a: str,
    params_a: dict,
    key_a: str,
    action_b: str,
    params_b: dict,
    key_b: str,
    *,
    expect_b_blocked: bool,
) -> tuple[int, tuple[int, dict], tuple[int, dict]]:
    """把请求 A 停在 skill_operations 回执提交前并触发单次 EIO，同时并发发起请求 B。"""
    entered = threading.Event()
    released = threading.Event()
    original = v13_store_mod.V13Store.transaction
    errors = 0

    def transaction(self, collection, mutate, **kwargs):
        nonlocal errors
        if collection == "skill_operations" and kwargs.get("idempotency_key") == key_a:
            entered.set()
            if not released.wait(10):
                raise AssertionError("release signal missing")
            errors += 1
            raise OSError(errno.EIO, "simulated receipt publication error")
        return original(self, collection, mutate, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(v13_store_mod.V13Store, "transaction", transaction)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                _http_business, server, action_a, params_a, idempotency_key=key_a
            )
            b_result = None
            try:
                assert entered.wait(10)
                second = pool.submit(
                    _http_business, server, action_b, params_b, idempotency_key=key_b
                )
                if expect_b_blocked:
                    try:
                        second.result(timeout=1.5)
                    except TimeoutError:
                        pass
                    else:
                        raise AssertionError(
                            "concurrent same-skill request completed before operation guard released"
                        )
                else:
                    b_result = second.result(timeout=10)
                    assert b_result[0] == 200, b_result
            finally:
                released.set()
            a_result = first.result(timeout=10)
            if b_result is None:
                b_result = second.result(timeout=10)
    return errors, a_result, b_result


def test_completed_skill_request_survives_other_request_compensation(
    r3_native, monkeypatch
):
    """P1-04：同技能并发写入时，请求 A 日志前 EIO 补偿不得删除请求 B 已提交并确认的技能。"""
    server, agent = r3_native
    key_a, key_b = "audit-write-a-000001", "audit-write-b-000001"
    params = {
        "skill_id": "shared-skill",
        "body_markdown": _skill_markdown("shared-skill", "Shared operation"),
    }

    errors, (a_status, a_body), (b_status, _b_body) = _run_blocked_skill_race(
        server,
        monkeypatch,
        "skills.create",
        params,
        key_a,
        "skills.create",
        params,
        key_b,
        expect_b_blocked=True,
    )
    path = agent.home_paths.owner_home_dir / "skills" / "shared-skill" / "SKILL.md"
    assert errors == 1
    assert a_status == 503 and a_body["error"]["code"] == "OUTCOME_UNKNOWN"
    assert b_status == 200
    assert path.read_text(encoding="utf-8") == params["body_markdown"]

    replay_status, _replay_body = _http_business(
        server, "skills.create", params, idempotency_key=key_b
    )
    assert replay_status == 200
    assert path.read_text(encoding="utf-8") == params["body_markdown"]


def test_skill_toggle_and_cross_operation_concurrency_and_target_isolation(
    r3_native, monkeypatch
):
    """P1-04：启停补偿、跨动作（update/retire/activate）同技能串行及异技能不阻塞隔离验证。"""
    server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir

    # 1. 创建目标技能后，验证并发 skills.retire 的失败补偿不会把已提交的 enabled=False 翻回 True
    skill_id = "toggle-race-skill"
    v1_body = _skill_markdown(skill_id, "version one")
    create_status, created = _http_business(
        server,
        "skills.create",
        {"skill_id": skill_id, "body_markdown": v1_body},
        idempotency_key="r3-toggle-init-" + uuid4().hex,
    )
    assert create_status == 200, created
    v1_ref = created["data"]["skill"]["version_ref"]

    errors, (a_retire, _), (b_retire, b_retire_body) = _run_blocked_skill_race(
        server,
        monkeypatch,
        "skills.retire",
        {"skill_id": skill_id},
        "r3-retire-a-000001",
        "skills.retire",
        {"skill_id": skill_id},
        "r3-retire-b-000001",
        expect_b_blocked=True,
    )
    assert errors == 1 and a_retire == 503 and b_retire == 200
    assert b_retire_body["data"]["skill"]["enabled"] is False
    meta = json.loads((owner_home / "skills" / skill_id / "skill_meta.json").read_text(encoding="utf-8"))
    assert meta["enabled"] is False
    replay_retire, replay_retire_body = _http_business(
        server, "skills.retire", {"skill_id": skill_id}, idempotency_key="r3-retire-b-000001"
    )
    assert replay_retire == 200 and replay_retire_body["data"]["skill"]["enabled"] is False

    # 2. 并发 skills.activate：A 失败回滚后，B 仍成功启用并保留 enabled=True
    errors, (a_act, _), (b_act, b_act_body) = _run_blocked_skill_race(
        server,
        monkeypatch,
        "skills.activate",
        {"skill_id": skill_id},
        "r3-activate-a-000001",
        "skills.activate",
        {"skill_id": skill_id},
        "r3-activate-b-000001",
        expect_b_blocked=True,
    )
    assert errors == 1 and a_act == 503 and b_act == 200
    assert b_act_body["data"]["skill"]["enabled"] is True
    meta = json.loads((owner_home / "skills" / skill_id / "skill_meta.json").read_text(encoding="utf-8"))
    assert meta["enabled"] is True

    # 3. 并发 skills.update：A（expected_version_ref=v1）在提交前失败回滚 v1，B 同用 v1 成功提交 v2_b
    v2_a = _skill_markdown(skill_id, "version two a")
    v2_b = _skill_markdown(skill_id, "version two b")
    errors, (a_upd, _), (b_upd, b_upd_body) = _run_blocked_skill_race(
        server,
        monkeypatch,
        "skills.update",
        {"skill_id": skill_id, "body_markdown": v2_a, "expected_version_ref": v1_ref},
        "r3-update-a-000001",
        "skills.update",
        {"skill_id": skill_id, "body_markdown": v2_b, "expected_version_ref": v1_ref},
        "r3-update-b-000001",
        expect_b_blocked=True,
    )
    assert errors == 1 and a_upd == 503 and b_upd == 200
    assert b_upd_body["data"]["skill"]["description"] == "version two b"
    assert (owner_home / "skills" / skill_id / "SKILL.md").read_text(encoding="utf-8") == v2_b

    # 4. 不同技能 ID 使用各自独立操作锁：alpha 阻塞在提交前时，beta 须立即完成
    beta_body = _skill_markdown("isolated-beta", "Beta independent skill")
    errors, (alpha_status, _), (beta_status, _beta_body) = _run_blocked_skill_race(
        server,
        monkeypatch,
        "skills.create",
        {
            "skill_id": "isolated-alpha",
            "body_markdown": _skill_markdown("isolated-alpha", "Alpha blocked"),
        },
        "r3-alpha-a-000001",
        "skills.create",
        {"skill_id": "isolated-beta", "body_markdown": beta_body},
        "r3-beta-b-000001",
        expect_b_blocked=False,
    )
    assert errors == 1 and alpha_status == 503 and beta_status == 200
    assert not (owner_home / "skills" / "isolated-alpha").exists()
    assert (owner_home / "skills" / "isolated-beta" / "SKILL.md").read_text(encoding="utf-8") == beta_body
    locks_dir = owner_home / "data" / "business_v13"
    assert (locks_dir / ".skill-operation-isolated-alpha.lock").is_file()
    assert (locks_dir / ".skill-operation-isolated-beta.lock").is_file()


def test_skill_operation_guard_releases_lock_and_zero_fd_delta_on_all_exits(
    r3_native, monkeypatch
):
    """P1-04：成功与异常（含 pre_journal EIO、重放冲突与非法参数）退出均须释放操作锁且 fd_delta == 0。"""
    _server, agent = r3_native
    owner_home = agent.home_paths.owner_home_dir
    context, services = business_api_mod.local_business_context(agent)
    skill_id = "r3-fd-guard-skill"
    v1_body = _skill_markdown(skill_id, "fd check v1")
    v2_body = _skill_markdown(skill_id, "fd check v2")

    def _invoke(action: str, params: dict, key: str) -> tuple[int, dict]:
        return business_api_mod.execute_business(
            {
                "action": action,
                "request_id": "fd-" + uuid4().hex,
                "params": params,
                "idempotency_key": key,
            },
            context,
            services,
        )

    def _assert_guard_unlocked(target_skill: str) -> None:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        lock_file = owner_home / "data" / "business_v13" / f".skill-operation-{target_skill}.lock"
        if fcntl is not None and lock_file.exists():
            fd = os.open(lock_file, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        with locked_private_directory(
            owner_home,
            relative_parts=("data", "business_v13"),
            lock_name=f".skill-operation-{target_skill}.lock",
        ):
            pass

    # 预热一次性懒导入与底层初始化，确保基线稳定
    warm_status, _ = _invoke(
        "skills.create",
        {"skill_id": "r3-fd-warmup", "body_markdown": _skill_markdown("r3-fd-warmup", "warm")},
        "r3-fd-warm-" + uuid4().hex,
    )
    assert warm_status == 200

    fd_before = _count_open_fds()
    try:
        # 1. pre_journal 失败退出 -> 503，锁释放且不留文件
        with monkeypatch.context() as mp:
            _arm_materialization_error(mp, "skill_operations.json", mode="pre_journal")
            st_err, _ = _invoke(
                "skills.create",
                {"skill_id": skill_id, "body_markdown": v1_body},
                "r3-fd-create-err-" + uuid4().hex,
            )
            assert st_err == 503
        _assert_guard_unlocked(skill_id)
        assert not (owner_home / "skills" / skill_id).exists()

        # 2. create / replay / IDEMPOTENCY_CONFLICT / update / retire(err+ok) / activate(err+ok)
        create_key = "r3-fd-create-ok-" + uuid4().hex
        st_create, created = _invoke(
            "skills.create", {"skill_id": skill_id, "body_markdown": v1_body}, create_key
        )
        assert st_create == 200
        v1_ref = created["data"]["skill"]["version_ref"]
        _assert_guard_unlocked(skill_id)

        st_replay, _ = _invoke(
            "skills.create", {"skill_id": skill_id, "body_markdown": v1_body}, create_key
        )
        assert st_replay == 200
        st_conflict, conflict = _invoke(
            "skills.create", {"skill_id": skill_id, "body_markdown": v2_body}, create_key
        )
        assert st_conflict == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        _assert_guard_unlocked(skill_id)

        st_update, _ = _invoke(
            "skills.update",
            {"skill_id": skill_id, "body_markdown": v2_body, "expected_version_ref": v1_ref},
            "r3-fd-update-" + uuid4().hex,
        )
        assert st_update == 200
        _assert_guard_unlocked(skill_id)

        with monkeypatch.context() as mp:
            _arm_materialization_error(mp, "skill_operations.json", mode="pre_journal")
            st_retire_err, _ = _invoke(
                "skills.retire", {"skill_id": skill_id}, "r3-fd-retire-err-" + uuid4().hex
            )
            assert st_retire_err == 503
        _assert_guard_unlocked(skill_id)

        st_retire_ok, retired = _invoke(
            "skills.retire", {"skill_id": skill_id}, "r3-fd-retire-ok-" + uuid4().hex
        )
        assert st_retire_ok == 200 and retired["data"]["skill"]["enabled"] is False
        _assert_guard_unlocked(skill_id)

        with monkeypatch.context() as mp:
            _arm_materialization_error(mp, "skill_operations.json", mode="pre_journal")
            st_act_err, _ = _invoke(
                "skills.activate", {"skill_id": skill_id}, "r3-fd-act-err-" + uuid4().hex
            )
            assert st_act_err == 503
        _assert_guard_unlocked(skill_id)

        st_act_ok, activated = _invoke(
            "skills.activate", {"skill_id": skill_id}, "r3-fd-act-ok-" + uuid4().hex
        )
        assert st_act_ok == 200 and activated["data"]["skill"]["enabled"] is True
        _assert_guard_unlocked(skill_id)

        # 3. 缺省/非法 skill_id 必须在建锁前拒绝
        st_bad, bad = _invoke(
            "skills.activate", {"skill_id": "../bad"}, "r3-fd-bad-" + uuid4().hex
        )
        assert st_bad == 400 and bad["error"]["code"] == "INVALID_SCHEMA"
    finally:
        fd_after = _count_open_fds()
        if fd_before >= 0 and fd_after >= 0:
            assert fd_after - fd_before == 0
