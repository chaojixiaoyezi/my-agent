# LLM: 只用 pytest 临时 owner、真实收割管线与真实 pull/submit_verdicts；每个场景只出现一次普通模拟 I/O 错误，
#   不改游标、欠账、令牌或租约算法。
# 模块用途: 回归 P1-R9-14——保证档重投递读失败保留不确定：不按缺口签收、在途身份不动、交原公开可重试结果。
from __future__ import annotations

import errno
import json
from pathlib import Path

from agent.common.audit_activation import audit_source_worker_key
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion.watch_state import persist_state

from agent_py_agent.tests.test_watch_audit_guarantee import (
    _ack_ids,
    _audit_state,
    _FakeSource,
    _verdict,
)
from agent_py_agent.tests.test_watch_audit_guarantee import owner_home as _owner_home

owner_home = _owner_home


# LLM: 包住原区间读者，第一次调用抛普通 EIO，之后原样委托；故障点就是重投递读取的真实入口。
# 函数用途: 让在途区间重读失败一次并返回计数。
def _fail_range_read_once(monkeypatch) -> dict[str, int]:
    original = hv._scan_spool_range
    calls = {"failed": 0}

    def flaky(state, inflight):
        if not calls["failed"]:
            calls["failed"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original(state, inflight)

    monkeypatch.setattr(hv, "_scan_spool_range", flaky)
    return calls


# 函数用途: 读取盘上读游标 sidecar 原始字节，用于逐字节比较“未写账”。
def _cursor_bytes(state) -> bytes:
    return hv._read_sidecar_path(state).read_bytes()


# LLM: 一次读失败返回原公开可重试形状且游标逐字节不变；无故障重试拿到同一批同一令牌，部分签收后只重投剩余，
#   全程没有缺口计数。
# 函数用途: 验证读失败不变成空签收批，并覆盖无故障重试与部分签收。
def test_redelivery_read_error_is_retryable_and_keeps_inflight_identity(owner_home, monkeypatch):
    state = _audit_state(owner_home, _FakeSource(), 4, chunk=4)
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")
    ids = _ack_ids(first)
    assert len(ids) == 4
    before = _cursor_bytes(state)
    calls = _fail_range_read_once(monkeypatch)

    failed, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-b")

    assert calls["failed"] == 1 and failed == []
    assert backlog["authorization_error"]["error_code"] == "AUDIT_SPOOL_READ_UNAVAILABLE"
    assert _cursor_bytes(state) == before
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["pending_acks"] == ids and "redelivery_gap_candidates" not in cursor
    assert hv.acked_candidates(cursor) == 0

    retried, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-b")
    assert _ack_ids(retried) == ids and "authorization_error" not in backlog
    assert backlog["pending_verdicts"] == 4

    partial = hv.submit_verdicts(state, consumer="judge-b", verdicts=[_verdict(ack_id) for ack_id in ids[:2]])
    assert partial["ok"] and partial["acked_now"] == 2 and partial["pending_remaining"] == 2
    rest, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-b")
    assert _ack_ids(rest) == ids[2:] and backlog["pending_verdicts_total"] == 2
    done = hv.submit_verdicts(state, consumer="judge-b", verdicts=[_verdict(ack_id) for ack_id in ids[2:]])
    assert done["ok"] and done["pending_remaining"] == 0
    cursor = hv.read_spool_cursor(state)
    assert hv.acked_candidates(cursor) == 4 and "redelivery_gap_candidates" not in cursor


# LLM: 部分签收后读游标 sidecar 一次读失败：不能当全新游标从头重领、清零签收计数，只交可重试结果；
#   之后无故障拉取只重投未签收的两条。
# 函数用途: 验证游标读失败保持在途欠账与签收计数。
def test_cursor_read_error_does_not_reclaim_from_start(owner_home, monkeypatch):
    state = _audit_state(owner_home, _FakeSource(), 4, chunk=4)
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")
    ids = _ack_ids(first)
    hv.submit_verdicts(state, consumer="judge-a", verdicts=[_verdict(ack_id) for ack_id in ids[:2]])
    sidecar = hv._read_sidecar_path(state)
    before = sidecar.read_bytes()
    original = Path.read_text
    calls = {"failed": 0}

    def flaky_read_text(self, *args, **kwargs):
        if self == sidecar and not calls["failed"]:
            calls["failed"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    failed, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")

    assert calls["failed"] == 1 and failed == []
    assert backlog["authorization_error"]["error_code"] == "AUDIT_SPOOL_CURSOR_UNAVAILABLE"
    assert sidecar.read_bytes() == before
    retried, _ = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")
    assert _ack_ids(retried) == ids[2:]
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 2


# LLM: 读失败期间与之后，过期来源尝试既不能领取也不能签收；在途令牌和欠账只属于当前租约，当前租约重试拿同一批。
# 函数用途: 验证过期租约拒绝与读失败后的身份保持。
def test_stale_lease_refused_while_current_lease_retries_same_ids(owner_home, monkeypatch):
    state = _audit_state(owner_home, _FakeSource(), 2, chunk=2)
    state.audit_root_task_id = "audit-r9-io"
    state.source_id = "source-r9-io"
    persist_state(state)
    lease = {
        "schema_version": "audit-source-worker-lease.v1",
        "audit_id": state.audit_root_task_id,
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "worker_key": audit_source_worker_key(state.audit_root_task_id, state.watch_id),
        "run_id": "run-current",
        "attempt_id": "attempt-current",
        "epoch": 2,
        "expires_at": 9_999_999_999.0,
    }
    lease_path = ws.state_dir(owner_home) / f"{state.watch_id}.worker-lease.json"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    current = {**lease, "lease_epoch": 2}
    stale = {**current, "run_id": "run-stale", "attempt_id": "attempt-stale", "lease_epoch": 1}
    first, _ = hv.read_spool_records(state, max_candidates=2, consumer="run-current", source_authority=current)
    ids = _ack_ids(first)
    before = _cursor_bytes(state)
    _fail_range_read_once(monkeypatch)

    failed, backlog = hv.read_spool_records(state, max_candidates=2, consumer="run-current",
                                            source_authority=current)
    assert failed == [] and backlog["authorization_error"]["error_code"] == "AUDIT_SPOOL_READ_UNAVAILABLE"
    refused, backlog = hv.read_spool_records(state, max_candidates=2, consumer="run-stale", source_authority=stale)
    assert refused == [] and backlog["authorization_error"]["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    assert _cursor_bytes(state) == before

    retried, backlog = hv.read_spool_records(state, max_candidates=2, consumer="run-current",
                                             source_authority=current)
    assert _ack_ids(retried) == ids and "authorization_error" not in backlog
    stale_settle = hv.submit_verdicts(state, consumer="run-stale", verdicts=[_verdict(ack_id) for ack_id in ids],
                                      source_authority=stale)
    assert stale_settle["ok"] is False and stale_settle["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    settled = hv.submit_verdicts(state, consumer="run-current", verdicts=[_verdict(ack_id) for ack_id in ids],
                                 source_authority=current)
    assert settled["ok"] and settled["acked_now"] == 2
    assert "redelivery_gap_candidates" not in hv.read_spool_cursor(state)
