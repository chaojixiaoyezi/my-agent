# LLM: ORCH-04 原生协作交接板与外部助手安全文件邮箱单元测试套件：
#   覆盖：
#   1. Submission != receipt != acceptance 单向状态机与非法流转硬拒绝；
#   2. 冻结输入版本（frozen_input_version）不匹配严格拒绝；
#   3. 稳定 ID 幂等去重 vs 冲突时 IDEMPOTENCY_CONFLICT 失败关闭；
#   4. 排他写入集（exclusive_write_set）路径重叠与父子目录重合 HANDOFF_WRITESET_OVERLAP 失败关闭；
#   5. 外部助手单写者隔离适配器：原子写入、软链接硬拒绝、超大文件隔离、损坏行跳过与全同步；
#   6. ManageHandoffBoardTool 的 REQUESTER_IDENTITY_UNPROVEN 门禁与 6 种动作分发；
#   7. 日期分桶（YYYY-MM-DD/）归档与有界游标分页。
# 模块用途: ORCH-04 交接板与文件适配器完整功能回归测试。
"""Tests for ORCH-04 native collaboration handoff board and external mailbox adapter."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.collaboration.external_mailbox_adapter import (
    ExternalMailboxAdapter,
    ExternalMailboxSecurityError,
    create_external_mailbox_layout,
)
from agent_py_agent.agent.collaboration.handoff_board import (
    FrozenInputVersionMismatchError,
    HandoffBoardService,
    HandoffError,
    HandoffWriteSetOverlapError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
)
from agent_py_agent.agent.collaboration.models import (
    CollaborationCase,
    EvidencePacket,
    new_case_id,
    new_evidence_id,
)
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ToolInvocationContext


@pytest.fixture
def collab_store(tmp_path: Path) -> CollaborationStore:
    return CollaborationStore(tmp_path / "collaboration")


@pytest.fixture
def handoff_service(collab_store: CollaborationStore, tmp_path: Path) -> HandoffBoardService:
    return HandoffBoardService(
        store=collab_store,
        workspace_root=tmp_path / "workspace",
    )


def test_submission_receipt_acceptance_state_machine(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    case = collab_store.open_case({"thread_id": "t1", "title": "test"})
    case_id = case.case_id

    # 1. 发布交接项 -> published
    item = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-1",
        responsible_actor="actor-worker",
        actor_kind="internal_run",
        frozen_input_version="commit_sha_12345",
        exclusive_write_set=("src/core.py",),
        item_id="item-test-1",
        now=100.0,
    )
    assert item.state == "published"
    assert item.item_id == "item-test-1"

    # 非法流转：未 ack 直接 submit -> 失败关闭
    with pytest.raises(InvalidStateTransitionError):
        handoff_service.submit_handoff(
            case_id=case_id,
            item_id=item.item_id,
            evidence_ids=("ev-1",),
            now=101.0,
        )

    # 2. 传输交付 -> delivered
    delivered = handoff_service.record_delivery(
        case_id=case_id,
        item_id=item.item_id,
        delivery_receipt={"transport": "bus", "delivered_at": 102.0},
        delivery_id="deliv-1",
        now=102.0,
    )
    assert delivered.state == "delivered"
    assert delivered.delivery_receipt.get("transport") == "bus"

    # 非法流转：未 ack 未 submit 直接 decide_acceptance -> 失败关闭
    with pytest.raises(InvalidStateTransitionError):
        handoff_service.decide_acceptance(
            case_id=case_id,
            item_id=item.item_id,
            verdict="accepted",
            reviewer_principal="coord",
            now=103.0,
        )

    # 3. 显式 ACK (匹配版本) -> acked
    acked = handoff_service.record_ack(
        case_id=case_id,
        item_id=item.item_id,
        recipient_ack={"received_status": "ready", "frozen_input_version": "commit_sha_12345"},
        frozen_input_version="commit_sha_12345",
        ack_id="ack-1",
        now=104.0,
    )
    assert acked.state == "acked"
    assert acked.recipient_ack["received_status"] == "ready"

    # 4. 提交交付证据包 -> submitted (绝对禁止自动 accepted)
    ev_packet = EvidencePacket(
        evidence_id=new_evidence_id(),
        case_id=case_id,
        request_id="req-1",
        source_agent_id="actor-worker",
        matched=True,
        summary="Done testing core.py",
    )
    submitted = handoff_service.submit_handoff(
        case_id=case_id,
        item_id=item.item_id,
        evidence_ids=("ev-manual-1",),
        evidence_packets=[ev_packet],
        submission_payload={"tests_passed": 42},
        submission_id="sub-1",
        now=105.0,
    )
    # 铁律：submission != acceptance
    assert submitted.state == "submitted"
    assert len(submitted.evidence_ids) == 2
    assert "ev-manual-1" in submitted.evidence_ids

    # 5. 终审裁决 request changes -> changes_requested
    rejected = handoff_service.decide_acceptance(
        case_id=case_id,
        item_id=item.item_id,
        verdict="changes_requested",
        reviewer_principal="coordinator_pro",
        reason="Missing edge test",
        now=106.0,
        frozen_input_version=submitted.frozen_input_version,
        submission_digest=submitted.metadata["submission_digest"],
    )
    assert rejected.state == "changes_requested"
    assert rejected.acceptance_verdict["verdict"] == "changes_requested"

    # 重新提交补丁
    resubmitted = handoff_service.submit_handoff(
        case_id=case_id,
        item_id=item.item_id,
        evidence_ids=("ev-manual-1", "ev-manual-2"),
        submission_id="sub-2",
        now=107.0,
    )
    assert resubmitted.state == "submitted"

    # 6. 终审裁决 accept -> accepted
    accepted = handoff_service.decide_acceptance(
        case_id=case_id,
        item_id=item.item_id,
        verdict="accepted",
        reviewer_principal="coordinator_pro",
        reason="Looks great",
        now=108.0,
        frozen_input_version=resubmitted.frozen_input_version,
        submission_digest=resubmitted.metadata["submission_digest"],
    )
    assert accepted.state == "accepted"
    assert accepted.acceptance_verdict["verdict"] == "accepted"


def test_frozen_input_version_mismatch_rejected(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    item = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-1",
        responsible_actor="actor-1",
        frozen_input_version="v2.0.0",
        item_id="item-ver-test",
    )

    # 尝试用错误的 input version ACK -> 必须抛出 FrozenInputVersionMismatchError
    with pytest.raises(FrozenInputVersionMismatchError) as exc_info:
        handoff_service.record_ack(
            case_id=case_id,
            item_id=item.item_id,
            recipient_ack={"note": "ACKing old version"},
            frozen_input_version="v1.9.9",
        )
    assert "FROZEN_INPUT_VERSION_MISMATCH" in str(exc_info.value)
    assert exc_info.value.error_code == "FROZEN_INPUT_VERSION_MISMATCH"

    # 用正确版本 ACK -> 成功
    acked = handoff_service.record_ack(
        case_id=case_id,
        item_id=item.item_id,
        recipient_ack={"note": "ACKing exact version"},
        frozen_input_version="v2.0.0",
    )
    assert acked.state == "acked"


def test_idempotent_deduplication_vs_conflict(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    # 1. 首次发布
    item1 = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-1",
        responsible_actor="actor-1",
        frozen_input_version="sha-1",
        exclusive_write_set=("pkg/a.py",),
        item_id="item-idem-1",
        idempotency_key="key-idem-1",
    )

    # 2. 相同参数重复发布 -> 幂等返回已有对象
    item2 = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-1",
        responsible_actor="actor-1",
        frozen_input_version="sha-1",
        exclusive_write_set=("pkg/a.py",),
        item_id="item-idem-1",
        idempotency_key="key-idem-1",
    )
    assert item1.item_id == item2.item_id
    assert item1.state == item2.state

    # 3. 相同 item_id / key 但载荷冲突 -> IDEMPOTENCY_CONFLICT
    with pytest.raises(IdempotencyConflictError) as exc_info:
        handoff_service.publish_item(
            case_id=case_id,
            request_id="req-1",
            responsible_actor="actor-1",
            frozen_input_version="sha-DIFFERENT",
            exclusive_write_set=("pkg/a.py",),
            item_id="item-idem-1",
            idempotency_key="key-idem-1",
        )
    assert "IDEMPOTENCY_CONFLICT" in str(exc_info.value)
    assert exc_info.value.error_code == "IDEMPOTENCY_CONFLICT"


def test_exclusive_writeset_overlap_fails_closed(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    # 激活第一个项目：独占写入 "src/module_a.py"
    item1 = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-1",
        responsible_actor="worker-1",
        frozen_input_version="sha-1",
        exclusive_write_set=("src/module_a.py",),
        item_id="item-wset-1",
    )
    assert item1.state == "published"

    # 第二个项目：相同文件 "src/module_a.py" -> 冲突
    with pytest.raises(HandoffWriteSetOverlapError) as exc1:
        handoff_service.publish_item(
            case_id=case_id,
            request_id="req-2",
            responsible_actor="worker-2",
            frozen_input_version="sha-1",
            exclusive_write_set=("src/module_a.py",),
            item_id="item-wset-2",
        )
    assert "HANDOFF_WRITESET_OVERLAP" in str(exc1.value)

    # 第三个项目：父目录覆盖 "src" -> 冲突
    with pytest.raises(HandoffWriteSetOverlapError) as exc2:
        handoff_service.publish_item(
            case_id=case_id,
            request_id="req-3",
            responsible_actor="worker-3",
            frozen_input_version="sha-1",
            exclusive_write_set=("src",),
            item_id="item-wset-3",
        )
    assert "HANDOFF_WRITESET_OVERLAP" in str(exc2.value)

    # 第四个项目：无交集文件 "tests/test_b.py" -> 成功发布
    item4 = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-4",
        responsible_actor="worker-4",
        frozen_input_version="sha-1",
        exclusive_write_set=("tests/test_b.py",),
        item_id="item-wset-4",
    )
    assert item4.state == "published"

    # 关闭 item1 后，重叠路径应解除封锁
    handoff_service.close_item(case_id=case_id, item_id=item1.item_id, reason="finished")
    item5 = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-5",
        responsible_actor="worker-5",
        frozen_input_version="sha-1",
        exclusive_write_set=("src/module_a.py",),
        item_id="item-wset-5",
    )
    assert item5.state == "published"


def test_external_mailbox_adapter_security_and_sync(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    coord_root = tmp_path / "coord_mailboxes"
    coord_root.mkdir(parents=True, exist_ok=True)
    actor_id = "claude-pro-external"
    layout = create_external_mailbox_layout(coord_root, actor_id)

    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root], max_file_bytes=262144)

    # 1. 协调器原子单写者写入
    packet = {"action": "ping", "timestamp": 123456}
    adapter.write_coordinator_message(layout, packet)
    assert layout.coordinator_outbox_path.exists()
    with open(layout.coordinator_outbox_path, encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == packet

    # 2. 软链接安全拒绝测试
    fake_target = tmp_path / "secret.txt"
    fake_target.write_text("classified", encoding="utf-8")
    symlink_inbox = layout.actor_inbox_path
    if symlink_inbox.exists():
        symlink_inbox.unlink()
    os.symlink(str(fake_target), str(symlink_inbox))

    _msgs, quarantined = adapter.read_actor_messages(layout)
    assert len(quarantined) >= 1
    assert "SYMLINK_FORBIDDEN" in str(quarantined[0].get("error_code"))
    symlink_inbox.unlink()

    # 3. 超大文件 (> 256 KiB) 隔离测试
    large_inbox = layout.actor_inbox_path
    large_inbox.write_text("x" * (262144 + 100), encoding="utf-8")
    _msgs, quarantined = adapter.read_actor_messages(layout)
    assert len(quarantined) >= 1
    assert "EXCEEDS_MAX_BYTES" in quarantined[0]["reason"]
    # 原文件已被移动至 .quarantine
    assert not large_inbox.exists()

    # 4. 损坏行跳过测试（不抛死循环）
    corrupt_inbox = layout.actor_inbox_path
    corrupt_inbox.write_text(
        '{"action": "valid_1"}\n{invalid_json}\n"just_a_string"\n{"action": "valid_2"}\n',
        encoding="utf-8",
    )
    valid_msgs, quarantined = adapter.read_actor_messages(layout)
    assert len(valid_msgs) == 2
    assert valid_msgs[0]["action"] == "valid_1"
    assert valid_msgs[1]["action"] == "valid_2"
    assert len(quarantined) == 2  # 一个 json 错误，一个非 dict 错误
    corrupt_inbox.unlink()

    # 5. 全流程双向同步测试
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id
    item = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-ext-1",
        responsible_actor=actor_id,
        actor_kind="external_assistant",
        frozen_input_version="v3.0.0",
        item_id="item-ext-sync-1",
    )
    assert item.state == "published"

    # 第一次同步：发布项推送到 coordinator_outbox，item 变为 delivered
    sync_report1 = adapter.sync_external_mailbox(handoff_service, layout, now=200.0)
    assert item.item_id in sync_report1["delivered_item_ids"]
    reloaded_item = collab_store.load_handoff_item(case_id, item.item_id)
    assert reloaded_item is not None
    assert reloaded_item.state == "delivered"

    # 助手写入 ACK 和 提交到 actor_inbox
    ack_msg = {
        "action": "ack",
        "case_id": case_id,
        "item_id": item.item_id,
        "frozen_input_version": "v3.0.0",
        "recipient_ack": {"status": "accepted_work"},
    }
    submit_msg = {
        "action": "submit",
        "case_id": case_id,
        "item_id": item.item_id,
        "evidence_ids": ["ev-ext-1"],
        "payload": {"result": "ok"},
    }
    with open(layout.actor_inbox_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(ack_msg) + "\n")
        f.write(json.dumps(submit_msg) + "\n")

    # 第二次同步：消费 inbox 消息，推进状态至 submitted
    sync_report2 = adapter.sync_external_mailbox(handoff_service, layout, now=205.0)
    assert item.item_id in sync_report2["acked_item_ids"]
    assert item.item_id in sync_report2["submitted_item_ids"]

    final_item = collab_store.load_handoff_item(case_id, item.item_id)
    assert final_item is not None
    assert final_item.state == "submitted"
    assert "ev-ext-1" in final_item.evidence_ids


def test_manage_handoff_board_tool_scoped_gate_and_actions(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    agent = SimpleNamespace(
        collaboration_store=collab_store,
        workspace_root=tmp_path / "workspace",
        _current_run_params=None,
    )
    tool = ManageHandoffBoardTool(agent=agent, service=handoff_service)

    # 1. 未证明身份请求 -> REQUESTER_IDENTITY_UNPROVEN 失败关闭
    unproven_ctx = ToolInvocationContext(runtime_snapshot=None)
    unproven_outcome = tool.execute_scoped({"action": "list_items"}, unproven_ctx)
    assert not unproven_outcome.ok
    assert unproven_outcome.error_code == "TOOL_PERMISSION_DENIED"
    assert unproven_outcome.effect_outcome == "not_started"
    parsed_unproven = json.loads(unproven_outcome.output)
    assert parsed_unproven["reason"] == "REQUESTER_IDENTITY_UNPROVEN"

    # 证明身份后放行
    agent._current_run_params = SimpleNamespace(run_id="worker-tool")
    proven_ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    # 2. 动作：publish_item
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id
    pub_res = tool.execute_scoped({
        "action": "publish_item",
        "case_id": case_id,
        "request_id": "req-tool-1",
        "responsible_actor": "worker-tool",
        "frozen_input_version": "v4.0.0",
        "exclusive_write_set": ["tools/core.py"],
        "item_id": "item-tool-1",
    }, proven_ctx)
    assert pub_res.ok
    assert pub_res.result_envelope["item"]["state"] == "published"

    # 收件人本人确认和提交，协调者不代签。
    agent._current_run_params = SimpleNamespace(run_id="worker-tool")
    # 3. 动作：record_ack
    ack_res = tool.execute_scoped({
        "action": "record_ack",
        "case_id": case_id,
        "item_id": "item-tool-1",
        "frozen_input_version": "v4.0.0",
        "recipient_ack": {"worker": "ready"},
    }, proven_ctx)
    assert ack_res.ok
    assert ack_res.result_envelope["item"]["state"] == "acked"

    # 4. 动作：submit_handoff
    sub_res = tool.execute_scoped({
        "action": "submit_handoff",
        "case_id": case_id,
        "item_id": "item-tool-1",
        "evidence_ids": ["ev-tool-100"],
        "frozen_input_version": "v4.0.0",
    }, proven_ctx)
    assert sub_res.ok
    assert sub_res.result_envelope["item"]["state"] == "submitted"

    agent._current_run_params = SimpleNamespace(run_id="run-proven-001")
    # 5. 动作：decide_acceptance
    dec_res = tool.execute_scoped({
        "action": "decide_acceptance",
        "case_id": case_id,
        "item_id": "item-tool-1",
        "verdict": "accepted",
        "reason": "Passed all gates",
        "frozen_input_version": "v4.0.0",
        "submission_digest": sub_res.result_envelope["item"]["metadata"]["submission_digest"],
    }, proven_ctx)
    assert dec_res.ok
    assert dec_res.result_envelope["item"]["state"] == "accepted"

    # 6. 动作：list_items
    list_res = tool.execute_scoped({
        "action": "list_items",
        "case_id": case_id,
    }, proven_ctx)
    assert list_res.ok
    assert len(list_res.result_envelope["items"]) >= 1

    # 7. 动作：sync_external_mailbox
    coord_root = (tmp_path / "workspace") / "tool_coord"
    sync_res = tool.execute_scoped({
        "action": "sync_external_mailbox",
        "root_dir": str(coord_root),
        "actor_id": "worker-ext",
    }, proven_ctx)
    assert sync_res.ok


def test_date_bucketed_archiving_and_cursor_pagination(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    # 创建 5 个已接受或关闭的项目
    for i in range(5):
        item = handoff_service.publish_item(
            case_id=case_id,
            request_id=f"req-{i}",
            responsible_actor="worker",
            frozen_input_version="v1",
            item_id=f"item-page-{i}",
            allow_serialized_overlap=True,
            now=1000.0 + i,
        )
        handoff_service.close_item(case_id=case_id, item_id=item.item_id, now=1000.0 + i)

    # 游标分页测试：limit=2
    page1 = handoff_service.list_items(case_id=case_id, limit=2)
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    page2 = handoff_service.list_items(case_id=case_id, limit=2, cursor=page1["next_cursor"])
    assert len(page2["items"]) == 2
    assert page2["next_cursor"] is not None

    page3 = handoff_service.list_items(case_id=case_id, limit=2, cursor=page2["next_cursor"])
    assert len(page3["items"]) == 1
    assert page3["next_cursor"] is None

    # 日期分桶归档测试
    archived_ids = handoff_service.archive_closed_items(case_id=case_id, now=2000.0)
    assert len(archived_ids) == 5

    # 验证目录结构存在 YYYY-MM-DD
    assert handoff_service.archive_dir.exists()
    date_dirs = list(handoff_service.archive_dir.glob("*"))
    assert len(date_dirs) >= 1
    # 目录名符合 YYYY-MM-DD
    bucket_name = date_dirs[0].name
    assert len(bucket_name.split("-")) == 3


def test_tool_error_handling_and_input_validation(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    agent = SimpleNamespace(
        collaboration_store=collab_store,
        workspace_root=tmp_path / "workspace",
        _current_run_params=SimpleNamespace(run_id="run-test-999"),
    )
    tool = ManageHandoffBoardTool(agent=agent, service=handoff_service)
    ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    # 1. 缺少 action
    res_no_action = tool.execute_scoped({}, ctx)
    assert not res_no_action.ok
    assert res_no_action.error_code == "TOOL_INVALID_ARGUMENTS"

    # 2. 未知 action
    res_bad_action = tool.execute_scoped({"action": "fly_to_moon"}, ctx)
    assert not res_bad_action.ok
    assert res_bad_action.error_code == "TOOL_INVALID_ARGUMENTS"

    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    # 3. publish_item 缺少必要参数
    res_missing_param = tool.execute_scoped({"action": "publish_item", "case_id": case_id}, ctx)
    assert not res_missing_param.ok
    assert res_missing_param.error_code == "TOOL_INVALID_ARGUMENTS"

    # 状态机替身用各自已证明的main-self身份接收；未知别名拒绝由独立身份测试覆盖。
    # 4. publish_item 发生写入集重叠
    agent._current_run_params = SimpleNamespace(run_id="act1")
    tool.execute_scoped({
        "action": "publish_item",
        "case_id": case_id,
        "request_id": "r1",
        "responsible_actor": "act1",
        "frozen_input_version": "v1",
        "exclusive_write_set": ["dir/foo.py"],
        "item_id": "item-overlap-1",
    }, ctx)

    agent._current_run_params = SimpleNamespace(run_id="act2")
    res_overlap = tool.execute_scoped({
        "action": "publish_item",
        "case_id": case_id,
        "request_id": "r2",
        "responsible_actor": "act2",
        "frozen_input_version": "v1",
        "exclusive_write_set": ["dir/foo.py"],
        "item_id": "item-overlap-2",
    }, ctx)
    assert not res_overlap.ok
    assert res_overlap.reported_error_code == "HANDOFF_WRITESET_OVERLAP"
    assert res_overlap.effect_outcome == "not_started"

    agent._current_run_params = SimpleNamespace(run_id="act1")
    # 5. record_ack 版本不匹配
    res_ack_mismatch = tool.execute_scoped({
        "action": "record_ack",
        "case_id": case_id,
        "item_id": "item-overlap-1",
        "frozen_input_version": "v2_WRONG",
    }, ctx)
    assert not res_ack_mismatch.ok
    assert res_ack_mismatch.reported_error_code == "FROZEN_INPUT_VERSION_MISMATCH"

    # 6. 未 ACK 直接 submit -> INVALID_STATE_TRANSITION
    res_early_sub = tool.execute_scoped({
        "action": "submit_handoff",
        "case_id": case_id,
        "item_id": "item-overlap-1",
        "evidence_ids": ["ev-1"],
        "frozen_input_version": "v1",
    }, ctx)
    assert not res_early_sub.ok
    assert res_early_sub.reported_error_code == "INVALID_STATE_TRANSITION"


def test_external_mailbox_path_traversal_forbidden(tmp_path: Path) -> None:
    coord_root = tmp_path / "coord_jail"
    coord_root.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside_jail"
    outside_dir.mkdir(parents=True, exist_ok=True)

    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root])
    outside_file = outside_dir / "actor_inbox.jsonl"
    outside_file.write_text('{"action": "ping"}\n', encoding="utf-8")

    layout = create_external_mailbox_layout(coord_root, "actor-jailbreak")
    # 强制将 actor_inbox_path 指向 jail 外部
    bad_layout = create_external_mailbox_layout(coord_root, "actor-jailbreak")
    object.__setattr__(bad_layout, "actor_inbox_path", outside_file)

    # 读取应当被安全拦截
    msgs, quarantined = adapter.read_actor_messages(bad_layout)
    assert len(msgs) == 0
    assert len(quarantined) == 1
    assert "PATH_TRAVERSAL_FORBIDDEN" in str(quarantined[0].get("error_code"))


def test_ack_and_submit_idempotency_conflicts(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    item = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-1",
        responsible_actor="actor-1",
        frozen_input_version="v1.0",
        item_id="item-multi-idem",
    )

    # 1. record_delivery 冲突
    delivered1 = handoff_service.record_delivery(
        case_id=case_id,
        item_id=item.item_id,
        delivery_receipt={"bus": "1"},
        delivery_id="d-1",
    )
    assert delivered1.state == "delivered"

    # 重复同一 receipt 幂等返回
    delivered2 = handoff_service.record_delivery(
        case_id=case_id,
        item_id=item.item_id,
        delivery_receipt={"bus": "1"},
        delivery_id="d-1",
    )
    assert delivered2.state == "delivered"

    # 冲突 receipt
    with pytest.raises(IdempotencyConflictError):
        handoff_service.record_delivery(
            case_id=case_id,
            item_id=item.item_id,
            delivery_receipt={"bus": "DIFFERENT"},
            delivery_id="d-1",
        )

    # 2. record_ack 冲突
    acked1 = handoff_service.record_ack(
        case_id=case_id,
        item_id=item.item_id,
        recipient_ack={"status": "ok"},
        frozen_input_version="v1.0",
        ack_id="ack-1",
    )
    assert acked1.state == "acked"

    # 重复同一 ack 幂等返回
    acked2 = handoff_service.record_ack(
        case_id=case_id,
        item_id=item.item_id,
        recipient_ack={"status": "ok"},
        frozen_input_version="v1.0",
        ack_id="ack-1",
    )
    assert acked2.state == "acked"

    # 冲突 ack
    with pytest.raises(IdempotencyConflictError):
        handoff_service.record_ack(
            case_id=case_id,
            item_id=item.item_id,
            recipient_ack={"status": "DIFFERENT_CONFLICT"},
            frozen_input_version="v1.0",
            ack_id="ack-1",
        )


def test_scenario_a_mailbox_path_traversal_and_confinement(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    """(a) actor_id="../escaped" and outside-workspace root_dir rejected with zero directories created outside authorized roots."""
    authorized_root = tmp_path / "workspace"
    authorized_root.mkdir(parents=True, exist_ok=True)
    outside_root = tmp_path / "outside_workspace"

    # 1. create_external_mailbox_layout rejects traversal actor_id
    with pytest.raises(ExternalMailboxSecurityError) as exc_info:
        create_external_mailbox_layout(authorized_root, "../escaped")
    assert exc_info.value.error_code == "PATH_TRAVERSAL_FORBIDDEN"
    assert not (tmp_path / "escaped").exists()

    with pytest.raises(ExternalMailboxSecurityError) as exc_info_slash:
        create_external_mailbox_layout(authorized_root, "actor/traversal")
    assert exc_info_slash.value.error_code == "PATH_TRAVERSAL_FORBIDDEN"
    assert not (authorized_root / "actor").exists()

    # 2. Tool rejects outside-workspace root_dir
    agent = SimpleNamespace(
        collaboration_store=collab_store,
        workspace_root=authorized_root,
        _current_run_params=SimpleNamespace(run_id="run-test"),
    )
    tool = ManageHandoffBoardTool(agent=agent, service=handoff_service)
    ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    res = tool.execute_scoped({
        "action": "sync_external_mailbox",
        "root_dir": str(outside_root),
        "actor_id": "legit-actor",
    }, ctx)
    assert not res.ok
    assert res.reported_error_code == "PATH_TRAVERSAL_FORBIDDEN"
    assert res.result_envelope["error_code"] == "PATH_TRAVERSAL_FORBIDDEN"
    assert res.effect_outcome == "not_started"
    assert not outside_root.exists()


def test_scenario_b_mailbox_actor_mismatch_rejected(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    """(b) actor-A inbox attempting to ack/submit actor-B's item rejected with ACTOR_MISMATCH."""
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    # Item belongs to actor-B
    handoff_service.publish_item(
        case_id=case_id,
        request_id="req-b",
        responsible_actor="actor-B",
        frozen_input_version="v1.0",
        item_id="item-b-target",
    )

    coord_root = tmp_path / "workspace" / "mailboxes"
    coord_root.mkdir(parents=True, exist_ok=True)
    layout_a = create_external_mailbox_layout(coord_root, "actor-A")
    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root])

    # actor-A tries to ack and submit actor-B's item
    inbox_records = [
        {"action": "ack", "case_id": case_id, "item_id": "item-b-target", "frozen_input_version": "v1.0"},
        {"action": "submit", "case_id": case_id, "item_id": "item-b-target", "evidence_ids": ["ev-spoofed"]},
    ]
    with open(layout_a.actor_inbox_path, "w", encoding="utf-8") as f:
        for r in inbox_records:
            f.write(json.dumps(r) + "\n")

    report = adapter.sync_external_mailbox(handoff_service, layout_a)
    assert len(report["errors"]) == 2
    for err in report["errors"]:
        assert err["error_code"] == "ACTOR_MISMATCH"
        assert "actor-B" in err["error"]
        assert "actor-A" in err["error"]

    # Verify state not mutated
    fresh_item = collab_store.load_handoff_item(case_id, "item-b-target")
    assert fresh_item is not None
    assert fresh_item.state == "published"
    assert fresh_item.recipient_ack is None


def test_scenario_c_repeated_sync_mailbox_cursor_idempotency(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    """(c) repeated sync_external_mailbox idempotency with zero spurious errors."""
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    handoff_service.publish_item(
        case_id=case_id,
        request_id="req-c",
        responsible_actor="actor-C",
        frozen_input_version="v1.0",
        item_id="item-c-flow",
    )

    coord_root = tmp_path / "workspace" / "mailboxes"
    coord_root.mkdir(parents=True, exist_ok=True)
    layout_c = create_external_mailbox_layout(coord_root, "actor-C")
    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root])

    # 1. First sync with delivery outbox write
    sync1 = adapter.sync_external_mailbox(handoff_service, layout_c)
    assert sync1["errors"] == []
    assert "item-c-flow" in sync1["delivered_item_ids"]

    # 2. actor-C appends ACK to inbox
    with open(layout_c.actor_inbox_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "action": "ack",
            "case_id": case_id,
            "item_id": "item-c-flow",
            "frozen_input_version": "v1.0",
            "ack_id": "ack-c-1",
        }) + "\n")

    sync2 = adapter.sync_external_mailbox(handoff_service, layout_c)
    assert sync2["errors"] == []
    assert "item-c-flow" in sync2["acked_item_ids"]

    # 3. Repeated sync without any new inbox content
    sync3 = adapter.sync_external_mailbox(handoff_service, layout_c)
    assert sync3["errors"] == []
    assert sync3["acked_item_ids"] == []
    assert sync3["submitted_item_ids"] == []

    # 4. actor-C appends submit to inbox
    with open(layout_c.actor_inbox_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "action": "submit",
            "case_id": case_id,
            "item_id": "item-c-flow",
            "evidence_ids": ["ev-c-1"],
            "submission_id": "sub-c-1",
        }) + "\n")

    sync4 = adapter.sync_external_mailbox(handoff_service, layout_c)
    assert sync4["errors"] == []
    assert "item-c-flow" in sync4["submitted_item_ids"]

    # 5. Repeated sync again: zero spurious InvalidStateTransitionError
    sync5 = adapter.sync_external_mailbox(handoff_service, layout_c)
    assert sync5["errors"] == []
    assert sync5["submitted_item_ids"] == []


def test_scenario_d_self_review_forbidden_and_subagent_reviewer_override(
    tmp_path: Path,
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) SELF_REVIEW_FORBIDDEN on decide_acceptance and subagent reviewer_principal override."""
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    item = handoff_service.publish_item(
        case_id=case_id,
        request_id="req-d",
        responsible_actor="actor-dev",
        frozen_input_version="v1.0",
        item_id="item-d-review",
    )
    handoff_service.record_ack(case_id=case_id, item_id=item.item_id, recipient_ack={"status": "ok"}, frozen_input_version="v1.0")
    handoff_service.submit_handoff(case_id=case_id, item_id=item.item_id, evidence_ids=["ev-d1"])

    # 1. Direct service call rejecting self-review
    with pytest.raises(HandoffError) as exc_info:
        handoff_service.decide_acceptance(
            case_id=case_id,
            item_id=item.item_id,
            verdict="accepted",
            reviewer_principal="actor-dev",
        )
    assert exc_info.value.error_code == "SELF_REVIEW_FORBIDDEN"

    # 2. Tool call in subagent context overrides reviewer_principal
    subagent_run_id = "subagent-run-rev-001"
    agent = SimpleNamespace(
        collaboration_store=collab_store,
        workspace_root=tmp_path / "workspace",
        _current_run_params=SimpleNamespace(run_id=subagent_run_id),
        subagents=SimpleNamespace(load=lambda run_id: SimpleNamespace(
            id=run_id, root_id="review-root", role="worker",
            attributes={"role_template": {"id": "explicit-acceptor", "can_accept": True,
                                           "can_spawn_children": False}})),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool.current_subagent_run_id",
        lambda _agent: subagent_run_id,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool.authoritative_self_principal",
        lambda _agent: (SimpleNamespace(run_id=subagent_run_id), ""),
    )

    tool = ManageHandoffBoardTool(agent=agent, service=handoff_service)
    ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    dec_res = tool.execute_scoped({
        "action": "decide_acceptance",
        "case_id": case_id,
        "item_id": item.item_id,
        "verdict": "accepted",
        "reviewer_principal": "conflicting-reviewer-name",
        "frozen_input_version": "v1.0",
        "submission_digest": collab_store.load_handoff_item(case_id, item.item_id).metadata["submission_digest"],
    }, ctx)
    assert dec_res.ok
    envelope = dec_res.result_envelope
    assert envelope["item"]["acceptance_verdict"]["reviewer_principal"] == subagent_run_id
    assert "scope_warnings" in envelope
    assert len(envelope["scope_warnings"]) == 1
    assert "conflicting-reviewer-name" in envelope["scope_warnings"][0]


def test_scenario_e_concurrent_publish_race_enforces_writeset_overlap(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    """(e) two-thread threading.Barrier concurrent publish_item race enforcing HANDOFF_WRITESET_OVERLAP."""
    case = collab_store.open_case({"thread_id": "t1"})
    case_id = case.case_id

    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = []
    errors: list[Exception] = []

    def worker(worker_id: str, item_id: str) -> None:
        try:
            barrier.wait(timeout=5.0)
            item = handoff_service.publish_item(
                case_id=case_id,
                request_id=f"req-{worker_id}",
                responsible_actor=f"actor-{worker_id}",
                frozen_input_version="v1.0",
                exclusive_write_set=("shared/conflict_file.py",),
                item_id=item_id,
            )
            results.append({"worker_id": worker_id, "item": item})
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("1", "item-race-1"))
    t2 = threading.Thread(target=worker, args=("2", "item-race-2"))

    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], HandoffWriteSetOverlapError)
    assert errors[0].error_code == "HANDOFF_WRITESET_OVERLAP"

    # Exactly 1 item in store
    items = collab_store.case_handoff_items(case_id)
    assert len(items) == 1


def test_scenario_f_archive_closed_items_pruning_and_active_index(
    collab_store: CollaborationStore,
    handoff_service: HandoffBoardService,
) -> None:
    """(f) archive_closed_items pruning closed case JSONL from active list_all_handoff_items(state="published") scan."""
    case_a = collab_store.open_case({"thread_id": "ta"})
    case_b = collab_store.open_case({"thread_id": "tb"})

    # Case A has 1 active item
    item_a = handoff_service.publish_item(
        case_id=case_a.case_id,
        request_id="req-a",
        responsible_actor="actor-A",
        frozen_input_version="v1.0",
        exclusive_write_set=("a.py",),
        item_id="item-a-active",
    )

    # Case B has 1 item that gets closed
    item_b = handoff_service.publish_item(
        case_id=case_b.case_id,
        request_id="req-b",
        responsible_actor="actor-B",
        frozen_input_version="v1.0",
        exclusive_write_set=("b.py",),
        item_id="item-b-closed",
    )
    handoff_service.close_item(case_id=case_b.case_id, item_id=item_b.item_id, reason="done")

    # Before archiving: active index has both cases
    active_idx_path = collab_store.handoff_items_dir / ".active_cases.json"
    assert active_idx_path.exists()
    assert case_a.case_id in collab_store.list_all_handoff_items(state="published")[0].case_id

    # Archive closed items
    archived = handoff_service.archive_closed_items(case_id=case_b.case_id)
    assert "item-b-closed" in archived

    # Case B JSONL should be pruned/unlinked since all its items were closed and archived
    case_b_path = collab_store.handoff_items_dir / f"{case_b.case_id}.jsonl"
    assert not case_b_path.exists()

    # Querying published items consults active index and only returns Case A's item
    published_items = collab_store.list_all_handoff_items(state="published", limit=10)
    assert len(published_items) == 1
    assert published_items[0].item_id == "item-a-active"


def test_scenario_g_unmocked_simple_agent_workspace_resolution(tmp_path: Path) -> None:
    """(g) unmocked SimpleAgent.root workspace directory resolved in ManageHandoffBoardTool."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    home_dir = tmp_path / "home"
    outside_dir = tmp_path / "outside_dir"

    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home_dir),
        execution_mode="local_unmanaged",
    )
    agent = SimpleAgent(config, workspace_dir)
    agent._current_run_params = SimpleNamespace(
        run_id="run-real-agent",
        task_id="task-real-agent",
        request_id="req-real-agent",
    )

    tool = ManageHandoffBoardTool(agent=agent)
    ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    # 1. sync_external_mailbox succeeds for root_dir under agent workspace
    mailbox_dir = workspace_dir / "mailboxes"
    sync_ok = tool.execute_scoped(
        {
            "action": "sync_external_mailbox",
            "root_dir": str(mailbox_dir),
            "actor_id": "worker-real-1",
        },
        ctx,
    )
    assert sync_ok.ok is True, sync_ok.output
    assert (mailbox_dir / "worker-real-1").is_dir()

    # 2. sync_external_mailbox rejects outside_dir fail-closed (PATH_TRAVERSAL_FORBIDDEN, not_started)
    sync_fail = tool.execute_scoped(
        {
            "action": "sync_external_mailbox",
            "root_dir": str(outside_dir),
            "actor_id": "worker-real-1",
        },
        ctx,
    )
    assert sync_fail.ok is False
    assert sync_fail.reported_error_code == "PATH_TRAVERSAL_FORBIDDEN"
    assert sync_fail.result_envelope["error_code"] == "PATH_TRAVERSAL_FORBIDDEN"
    assert sync_fail.effect_outcome == "not_started"
    assert not outside_dir.exists()


@pytest.mark.parametrize("claimed_actor", ["actor-B", None, 42, {"actor_id": "actor-A"}])
@pytest.mark.parametrize("action", ["ack", "submit"])
def test_mailbox_payload_actor_cannot_forge_writer(
    tmp_path: Path, collab_store: CollaborationStore,
    handoff_service: HandoffBoardService, claimed_actor: Any, action: str,
) -> None:
    case = collab_store.open_case({"thread_id": "t1"})
    handoff_service.publish_item(
        case_id=case.case_id, request_id="req-identity", responsible_actor="actor-A",
        frozen_input_version="v1", item_id="identity-item",
    )
    root = tmp_path / "mailbox"
    root.mkdir()
    layout = create_external_mailbox_layout(root, "actor-A")
    adapter = ExternalMailboxAdapter(allowed_roots=[root])
    adapter.sync_external_mailbox(handoff_service, layout)
    if action == "submit":
        handoff_service.record_ack(
            case_id=case.case_id, item_id="identity-item",
            recipient_ack={"actor_id": "actor-A"}, frozen_input_version="v1", ack_id="original-ack",
        )
    before = collab_store.load_handoff_item(case.case_id, "identity-item")
    payload_field = "recipient_ack" if action == "ack" else "payload"
    message = {"action": action, "case_id": case.case_id, "item_id": "identity-item",
               "frozen_input_version": "v1", payload_field: {"actor_id": claimed_actor}}
    layout.actor_inbox_path.write_text(json.dumps(message) + "\n", encoding="utf-8")
    report = adapter.sync_external_mailbox(handoff_service, layout)
    assert report["errors"][0]["error_code"] == "MAILBOX_ACTOR_MISMATCH"
    assert report["errors"][0]["effect_outcome"] == "not_started"
    assert collab_store.load_handoff_item(case.case_id, "identity-item") == before
    message[payload_field] = {}
    with layout.actor_inbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(message) + "\n")
    report = adapter.sync_external_mailbox(handoff_service, layout)
    assert report["errors"] == []
    after = collab_store.load_handoff_item(case.case_id, "identity-item")
    if action == "ack":
        assert after.recipient_ack["actor_id"] == "actor-A"
    else:
        assert after.metadata["submission_payload"]["actor_id"] == "actor-A"


@pytest.mark.parametrize("mode", ["root", "child", "unbound", "foreign", "conflicting_input"])
def test_open_case_uses_canonical_run_origin_without_guessing(tmp_path: Path, mode: str) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                    enable_subagents=True), tmp_path)
    owner = agent.home_paths.owner_id
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "qa", "channel": "local", "channel_conversation_id": "case-origin",
        "channel_user_id": "qa", "owner_id": owner, "owner_home": str(agent.home_paths.owner_home_dir),
    })
    root_id = "case-origin-root"
    agent.conversation_store.tasks.bind({"thread_id": thread.thread_id, "task_id": root_id,
                                         "goal": "origin", "status": "active"})
    repo = agent.subagents.runtime_db
    task = repo.create_task(owner_id="foreign-owner" if mode == "foreign" else owner,
                            thread_id="" if mode == "unbound" else thread.thread_id,
                            conversation_task_id="" if mode == "unbound" else root_id)
    run = repo.create_task_run(task_id=task["task_id"])
    repo.create_agent_run(task_run_id=run["task_run_id"], run_id=root_id, role="main")
    child = (agent.subagents.create_run(goal="child", thought="", plan=["p"], parent_id=root_id,
                                       root_id=root_id, depth=1) if mode != "foreign" else None)
    agent._current_run_params = SimpleNamespace(run_id=root_id, task_id=root_id, task_attributes={
        "conversation_task_id": root_id, "conversation_thread_id": thread.thread_id})
    if mode == "child":
        agent._current_subagent_run_id = child.id
    tool = ManageHandoffBoardTool(agent=agent)
    service = tool._resolve_service()
    params: dict[str, object] = {"title": "scope-test"}
    if mode == "conflicting_input":
        params["thread_id"] = "another-thread"
    if mode == "foreign":
        with pytest.raises(HandoffError, match="CASE_OWNER_MISMATCH"):
            tool._native_case_context()
        assert service.store.list_cases() == []
        return
    outcome = tool._handle_open_case(service, params)
    if mode == "conflicting_input":
        assert not outcome.ok
        assert outcome.error_code == "CASE_SCOPE_MISMATCH"
        assert service.store.list_cases() == []
        return
    assert outcome.ok, outcome.output
    payload = json.loads(outcome.output)
    assert payload["case"]["thread_id"] == ("" if mode == "unbound" else thread.thread_id)
    assert payload["case"]["task_id"] == ("" if mode == "unbound" else root_id)
    assert payload["scope_resolution"]["case_context"] == ("unbound" if mode == "unbound" else "canonical_task")
