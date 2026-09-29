"""插话幂等回执指纹的持久化稳定性与对账收敛（2026-09-29，生产实锤 gwreq-msg-7155fe18…）。

a646a4885 把 _guidance_input_digest 的口径改成"元数据再去掉 expected_turn_id"，却没兼容旧回执：插话
（active_turn_user_input）的元数据一定带 expected_turn_id，之前落盘的幂等回执被重算时全部对不上，
每条都报 DataCorruptionError"input digest mismatch"；对账把它写成 terminal_unknown 后每 15 秒重写一次，
不进任何计数，日志里也看不到。

本文件锁定四件事：
1. 固定样本的 v1/v2 指纹十六进制值钉死（任一口径再被改动就变红）；
2. 用旧口径（冻结在本文件里的 a646a4885 之前算法）写的真实形状回执能被新代码读出，同键重试返回同一条；
   记了版本的回执按版本严格校验；
3. 对账遇到这种旧回执会自然收敛（目标已结束 → 排队），不用手工改文件；
4. 真的坏账（正文被改）保持 terminal_unknown，但状态与错误没变时不重写文件，且计入 summary 并能抛给循环守卫。
全部结构化判定，不看错误文本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import store_guidance_records as records
from agent_py_agent.agent.conversation.models import normalize_guidance_target_type
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    GatewayInputReconcileUnsettledError,
    gateway_input_receipt_path,
    raise_if_input_reconcile_unsettled,
    read_gateway_input_receipt,
    reconcile_gateway_input_receipts,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_steer_delivery_recovery import _background_input

SAMPLE_REQUEST = {
    "target_type": "request",
    "target_id": "gwreq-sample",
    "message": "改用已有资料直接收口",
    "sender": "local-agent",
    "priority": "normal",
    "delivery": "next_turn",
    "metadata": {
        "channel_message_id": "steer-sample",
        "expected_turn_id": "gwreq-sample",
        "gateway_input_request_id": "gwreq-msg-sample",
        "message_id": "steer-sample",
    },
}
# 钉死的指纹：v1 = a646a4885 之前的口径（元数据只去 dedupe_key），v2 = 当前口径（再去 expected_turn_id）。
PINNED_V1 = "c92b8fc98e405898d89879c14500d54c8d4326437910740590dce5beddffe2cb"
PINNED_V2 = "8619b6dc6f715c5db9e4db292a8f39ca8e32ade3f9f5130af9630f088e594c0c"


# LLM: 冻结的 a646a4885 之前算法，故意不调用产品函数：产品的 v1 若被改动，这里算出的值就对不上钉死的常量。
# 函数用途: 按旧口径算一份请求的指纹。
def _frozen_legacy_digest(request: dict) -> str:
    metadata = dict(request.get("metadata") or {})
    metadata.pop("dedupe_key", None)
    canonical = {
        "target_type": normalize_guidance_target_type(request.get("target_type")),
        "target_id": str(request.get("target_id") or "").strip(),
        "message": str(request.get("message") or "").strip(),
        "sender": str(request.get("sender") or "").strip(),
        "priority": str(request.get("priority") or "normal").strip() or "normal",
        "delivery": str(request.get("delivery") or "next_turn").strip() or "next_turn",
        "metadata": metadata,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_digest_versions_are_pinned() -> None:
    assert records.GUIDANCE_INPUT_DIGEST_VERSION == 2
    assert records._guidance_input_digest(SAMPLE_REQUEST, version=1) == PINNED_V1 == _frozen_legacy_digest(SAMPLE_REQUEST)
    assert records._guidance_input_digest(SAMPLE_REQUEST, version=2) == PINNED_V2
    assert records._guidance_input_digest(SAMPLE_REQUEST) == PINNED_V2
    with pytest.raises(ValueError):
        records._guidance_input_digest(SAMPLE_REQUEST, version=3)


# 函数用途: 把一条刚写的回执改写成 a646a4885 之前的落盘形状：旧口径指纹、没有版本字段。
def _downgrade_receipt_to_legacy(store: ConversationStore, dedupe_key: str, request: dict) -> None:
    path = store.storage.guidance_dedupe_path(dedupe_key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("input_digest_version", None)
    payload["input_digest"] = _frozen_legacy_digest(request)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_legacy_receipt_reads_and_replays_the_same_entry(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    key = "thread-1/legacy-steer"
    first = store.guidance.append_once(SAMPLE_REQUEST, dedupe_key=key)
    _downgrade_receipt_to_legacy(store, key, SAMPLE_REQUEST)
    raw = json.loads(store.storage.guidance_dedupe_path(key).read_text(encoding="utf-8"))
    assert raw["input_digest"] == PINNED_V1 and "input_digest_version" not in raw

    receipt = store.guidance.receipt(key)  # 撤修复：DataCorruptionError input digest mismatch
    assert receipt is not None and receipt.entry.guidance_id == first.guidance_id
    assert receipt.input_digest == PINNED_V1 and receipt.input_digest_version == 0
    replay = store.guidance.append_once({**SAMPLE_REQUEST, "now": 99.0}, dedupe_key=key)
    assert replay.guidance_id == first.guidance_id, "同键重试必须回到同一条"

    with pytest.raises(DataCorruptionError):  # 旧口径不等于放水：同键异文仍然报错
        store.guidance.append_once({**SAMPLE_REQUEST, "message": "另一条正文"}, dedupe_key=key)


def test_versionless_receipt_written_by_the_new_rule_is_also_accepted(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    key = "thread-1/versionless-v2"
    first = store.guidance.append_once(SAMPLE_REQUEST, dedupe_key=key)
    path = store.storage.guidance_dedupe_path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["input_digest_version"] == 2 and payload["input_digest"] == PINNED_V2
    payload.pop("input_digest_version")  # a646a4885 之后、本修复之前写的回执：新口径但没有版本
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    receipt = store.guidance.receipt(key)
    assert receipt is not None and receipt.entry.guidance_id == first.guidance_id


def test_versioned_receipts_are_validated_strictly_by_their_version(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    key = "thread-1/strict"
    store.guidance.append_once(SAMPLE_REQUEST, dedupe_key=key)
    path = store.storage.guidance_dedupe_path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["input_digest"] = PINNED_V1  # 声明是 v2 却存了 v1 的值：按版本严格校验必须拒绝
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DataCorruptionError):  # 撤修复（版本 0/2 都两种任一匹配）：这里不会抛
        store.guidance.receipt(key)


# 函数用途: 把生产事故里的状态搬进测试：对账错误已经落盘的 terminal_unknown 回执。
def _mark_terminal_unknown_with_digest_error(paths, request_id: str) -> None:
    receipt = read_gateway_input_receipt(paths, request_id)
    stale = replace(
        receipt,
        state="terminal_unknown",
        reconcile_error={
            "category": "data_corruption",
            "error_type": "DataCorruptionError",
            "message": "conversation guidance receipt input digest mismatch",
            "context": "gateway.input_receipt.reconcile",
            "recoverable": True,
        },
    )
    write_json_file_atomic(gateway_input_receipt_path(paths, request_id), stale.to_dict())


def test_legacy_steer_receipt_converges_after_the_target_ends(tmp_path) -> None:
    agent, paths, request_id = _background_input(tmp_path, "srun_legacy")
    store = agent.conversation_store
    steer_request = {
        "message": "改用已有资料直接收口", "sender": "local-agent", "target_type": "task", "target_id": "srun_legacy",
        "priority": "high", "delivery": "current_request",
        "metadata": {"kind": "active_turn_user_input", "record_in_transcript": True, "expected_turn_id": "srun_legacy",
                     "channel_message_id": "steer-d", "gateway_input_request_id": "gwreq-msg-steer-d"},
    }
    _downgrade_receipt_to_legacy(store, "steer-d", steer_request)
    _mark_terminal_unknown_with_digest_error(paths, request_id)
    store.tasks.update_status({"task_id": "srun_legacy", "status": "completed"})

    summary = reconcile_gateway_input_receipts(paths, agent)  # 撤修复：仍是 terminal_unknown + digest mismatch

    receipt = read_gateway_input_receipt(paths, request_id)
    assert receipt.state == "queued" and (paths.inbox / f"{request_id}.json").is_file(), "旧回执应自然收敛为排队"
    assert receipt.reconcile_error == {}
    assert summary["terminal_unknown_errors"] == 0 and summary["errors"] == 0
    assert store.guidance.receipt("steer-d").status == "rejected"


def test_real_corruption_stays_terminal_unknown_without_rewrites_and_is_counted(tmp_path) -> None:
    agent, paths, request_id = _background_input(tmp_path, "srun_corrupt")
    store = agent.conversation_store
    guidance_path = store.storage.guidance_dedupe_path("steer-d")
    payload = json.loads(guidance_path.read_text(encoding="utf-8"))
    payload["entry"]["message"] = "被改过但保留旧 digest"  # 任何口径都对不上：真坏账
    guidance_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    first = reconcile_gateway_input_receipts(paths, agent)
    receipt_path = gateway_input_receipt_path(paths, request_id)
    receipt = read_gateway_input_receipt(paths, request_id)
    assert receipt.state == "terminal_unknown"
    assert receipt.reconcile_error["error_type"] == "DataCorruptionError"
    assert first["terminal_unknown_errors"] == 1
    assert first["last_terminal_unknown_error"] == {
        "request_id": request_id, "error_type": "DataCorruptionError", "category": "data_corruption",
        "context": "gateway.input_receipt.reconcile",
    }
    before = receipt_path.read_bytes()

    second = reconcile_gateway_input_receipts(paths, agent)

    assert receipt_path.read_bytes() == before, "状态与错误都没变时不能重写回执"  # 撤修复：updated_at 每轮刷新
    assert second["terminal_unknown_errors"] == 1
    with pytest.raises(GatewayInputReconcileUnsettledError) as info:
        raise_if_input_reconcile_unsettled(second)  # 撤修复：什么都不抛，loop_health 看不到
    assert info.value.category == "data_corruption" and info.value.summary["terminal_unknown_errors"] == 1
    assert raise_if_input_reconcile_unsettled({"terminal_unknown_errors": 0, "errors": 0}) is None
