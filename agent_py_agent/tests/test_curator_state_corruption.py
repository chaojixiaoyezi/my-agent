from __future__ import annotations

"""第2项返工验收: Curator state 损坏 fail-closed + 原子 quarantine + 人工恢复。

对齐维护记录 + steward seq 1458 五条修复门槛:
  1. 独立错误码 CURATOR_STATE_CORRUPT(不伪装 CURATOR_SCHEMA_INVALID / COMMIT_FAILED)
  2. 损坏后 fail-closed: 不推 cursor、不接管不可信 lease、不消费新批次、不自动从头重跑
  3. 原 state 原子 quarantine: 隔离坏文件原文(取证损坏形态) + 哨兵
     (hash/隔离路径/时间/错误分类); quarantine/写失败可观测、有限次(每 tick 单次)
  4. 只有权威恢复证据才能继续, 否则转人工: 哨兵 recovery="manual",
     恢复 = 从备份/审计账本取回游标 + 删哨兵, 不伪造自愈成功
  5. 验收: 已有 cursor/Candidate/active lease 时损坏、重复 maintenance 不新增事实
     不倒退游标、quarantine 可追溯、失败可读、修复后重启不重复消费、
     Gateway 维护循环连续可用
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_py_agent.agent.backends import ModelResponse  # noqa: E402
from agent_py_agent.agent.conversation.store import ConversationStore  # noqa: E402
from agent_py_agent.agent.memory_store.candidate_models import (  # noqa: E402
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService  # noqa: E402
from agent_py_agent.agent.memory_store.curator import (  # noqa: E402
    MemoryCuratorConfig,
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_models import (  # noqa: E402
    CURATOR_OUTPUT_SCHEMA_VERSION,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog  # noqa: E402
from agent_py_agent.agent.memory_store.curator_state import (  # noqa: E402
    CuratorStateCorruptError,
    CuratorSuccessCommit,
    MemoryCuratorStateStore,
)
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore  # noqa: E402

VALID_EXTRACTION = json.dumps(
    {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [],
        "candidates": [],
        "processed_message_refs": [],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {"per_thread_cursors": [], "last_audit_event_id": None},
    },
    ensure_ascii=False,
)

BROKEN_STATE = "{损坏的JSON"


_IDENTITY_MANIFEST_RE = re.compile(
    r"输入身份清单为 (\{.*?\})。必须逐项复制清单", re.DOTALL
)


class _OkBackend:
    """确定性成功后端: 从 prompt 身份清单回显全量 processed refs(推进游标), 其余为空。

    curator 只按模型声明的 processed 前缀推进游标; 真实模型会逐项复制清单,
    这里等价地回显, 使「恢复后只消费新消息」可被 processed_count 断言证明。
    """

    def __init__(self) -> None:
        self.name = "corrupt-evidence"
        self.calls = 0

    def generate_structured(self, prompt: str, response_schema: dict | None = None) -> ModelResponse:
        self.calls += 1
        match = _IDENTITY_MANIFEST_RE.search(prompt)
        manifest = json.loads(match.group(1)) if match else {}
        message_ids = manifest.get("message_ids") or []
        audit_event_ids = manifest.get("audit_event_ids") or []
        return ModelResponse(
            text=json.dumps(
                {
                    "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
                    "daily_events": [],
                    "candidates": [],
                    "processed_message_refs": [
                        {"message_id": mid} for mid in message_ids
                    ],
                    "processed_audit_refs": [
                        {"event_id": eid} for eid in audit_event_ids
                    ],
                    "unresolved_refs": [],
                    "warnings": [],
                    "next_cursor": {
                        "per_thread_cursors": [],
                        "last_audit_event_id": None,
                    },
                },
                ensure_ascii=False,
            ),
            backend=self.name,
        )


def _append_message(home: Path, thread_id: str, *, seq: int, content: str) -> None:
    store = ConversationStore(home / "conversations")
    store.append_message(
        {
            "thread_id": thread_id,
            "role": "user",
            "content": content,
            "channel": "internal",
            "metadata": {
                "session_id": "session-1",
                "request_id": f"request-{seq}",
                "task_id": "task-1",
                "run_id": "run-1",
            },
            "now": float(1000 + seq),
        }
    )


def _new_home(tmp_path: Path, name: str) -> Path:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _make_service(home: Path, backend: _OkBackend | None = None) -> MemoryCuratorService:
    config = MemoryCuratorConfig(
        interval_seconds=60,
        turn_threshold=1,
        timeout_seconds=2,
        max_retries=0,
    )
    return MemoryCuratorService(
        config=config,
        dependencies=MemoryCuratorDependencies(
            backend=backend or _OkBackend(),
            conversation_store=ConversationStore(home / "conversations"),
            audit_dir=home / "audit",
            state_store=MemoryCuratorStateStore(home / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(home / "memory" / "daily"),
            candidate_service=CandidateService(home / "memory" / "candidates.jsonl"),
            run_log=CuratorRunLog(home / "memory" / "curator" / "runs"),
            identity=MemoryCuratorIdentity(
                provider="corrupt-evidence",
                model="curator-v2",
                owner_id="evidence/local",
            ),
        ),
    )


def _state_path(home: Path) -> Path:
    return home / "memory" / "curator" / "state.json"


def _sentinel_path(home: Path) -> Path:
    return home / "memory" / "curator" / "state.corrupt"


def _quarantine_files(home: Path) -> list[Path]:
    return sorted((home / "memory" / "curator" / "quarantine").glob("state-*.json"))


def _last_run_record(home: Path) -> dict[str, object] | None:
    runs_dir = home / "memory" / "curator" / "runs"
    files = sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
    if not files:
        return None
    rows = [
        json.loads(line)
        for line in files[-1].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return rows[-1] if rows else None


def _run_records(home: Path) -> list[dict[str, object]]:
    runs_dir = home / "memory" / "curator" / "runs"
    files = sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
    rows: list[dict[str, object]] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_with_cursor_candidate_and_lease(home: Path) -> tuple[MemoryCuratorService, dict[str, object]]:
    """构造「已有 cursor + Candidate + active lease」的 state 场景并返回 (service, state)。"""
    thread = ConversationStore(home / "conversations").get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    _append_message(home, thread.thread_id, seq=1, content="我的个人电脑使用 macOS。")
    service = _make_service(home)
    first = service.run(reason="admin", force=True)
    assert first.status == "succeeded", f"前置成功 run 失败: {first}"
    # 已有 Candidate(真实 API 落账, 手写缺字段行会被 CandidateStoreCorruptError 拒读)
    (home / "memory").mkdir(parents=True, exist_ok=True)
    service.candidate_service.observe(
        CandidateObservation(
            candidate_type="user_preference",
            content="用户偏好使用 macOS 工作。",
            subject_key="pref/os",
            scope=MemoryScope(scope_type="personal", scope_key="personal"),
            origin="user_explicit",
            source_message_refs=({"message_id": "msg-1", "thread_id": thread.thread_id},),
        )
    )
    # 已有 active lease(acquire 但不 commit, 模拟运行中损坏)
    service.state_store.acquire(
        reason="interval",
        config_revision=service.config.revision(),
        lease_seconds=300,
    )
    state = json.loads(_state_path(home).read_text(encoding="utf-8"))
    return service, state


# ---------------------------------------------------------------- 门槛1+2+3: 核心 fail-closed

def test_corruption_fail_closed_isolates_original_with_cursor_and_lease(
    tmp_path: Path,
) -> None:
    """已有 cursor/Candidate/active lease 时损坏: 独立错误码 + 不 raise + 隔离取证 + 恢复证据可读。"""
    home = _new_home(tmp_path, "core")
    service, state_before = _state_with_cursor_candidate_and_lease(home)
    lease_id_before = state_before["active_lease"]["lease_id"]
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")

    result = service.run(reason="admin", force=True)  # 不 raise

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_STATE_CORRUPT"  # 门槛1: 独立错误码
    # 门槛2: fail-closed —— 不接管不可信 lease、不消费、不自动重跑
    record = _last_run_record(home)
    assert record is not None and record["failure_code"] == "CURATOR_STATE_CORRUPT"
    assert record["status"] == "failed"
    assert not _state_path(home).exists()  # 隔离后无新 state 被创建(未自动从头)
    # 门槛3: 隔离坏文件原文(取证损坏形态), 字节与损坏时一致
    quarantined = _quarantine_files(home)
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == BROKEN_STATE.encode("utf-8")
    # 哨兵记录损坏证据: hash/时间/错误分类
    marker = json.loads(_sentinel_path(home).read_text(encoding="utf-8"))
    assert marker["original_sha256"] == _sha256(quarantined[0])
    assert marker["error_class"] == "unreadable"
    assert marker["recovery"] == "manual"
    # 恢复证据可读(门槛4): 损坏前的最近一次成功审计保留 cursor_after 投影,
    # 人工恢复可凭审计账本取回游标; 旧 lease 在隔离副本之外不再被任何 run 接管
    succeeded = [r for r in _run_records(home) if r["status"] == "succeeded"]
    assert succeeded and succeeded[-1]["cursor_after"] != {}
    assert state_before["active_lease"]["lease_id"] == lease_id_before  # 前置场景自证


def test_repeated_maintenance_no_new_facts_no_cursor_rollback(tmp_path: Path) -> None:
    """重复 maintenance: 不重复隔离、审计幂等、candidates 不新增、不倒退游标。"""
    home = _new_home(tmp_path, "repeat")
    service, _state = _state_with_cursor_candidate_and_lease(home)
    candidates_before = len(
        [l for l in (home / "memory" / "candidates.jsonl").read_text().splitlines() if l.strip()]
    )
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")

    r1 = service.run(reason="admin", force=True)
    assert r1.failure_code == "CURATOR_STATE_CORRUPT"
    runs_after_first = len(_run_records(home))

    for _ in range(3):  # 重复维护 tick
        again = service.run(reason="admin", force=True)
        assert again.status == "failed"
        assert again.failure_code == "CURATOR_STATE_CORRUPT"

    # 门槛5: 重复 maintenance 不新增事实
    assert len(_quarantine_files(home)) == 1  # 不重复隔离
    assert len(_run_records(home)) == runs_after_first  # 审计幂等(record_id 稳定, 不刷屏)
    candidates_after = len(
        [l for l in (home / "memory" / "candidates.jsonl").read_text().splitlines() if l.strip()]
    )
    assert candidates_after == candidates_before  # 不消费新批次, candidates 不新增
    # 游标不倒退: 损坏后没有任何 run 改写/重建 state(无新 state.json 生成)
    assert not _state_path(home).exists()


# ---------------------------------------------------------------- 门槛3: 可追溯

def test_quarantine_traceable_marker(tmp_path: Path) -> None:
    """quarantine 可追溯: 文件名含时间戳+hash8; 哨兵含 hash/时间/错误分类/人工恢复标记。"""
    home = _new_home(tmp_path, "trace")
    service, _state = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    broken_sha = _sha256(_state_path(home))  # 写坏后的 hash(隔离后原文件已移走, 须 run 前算)

    service.run(reason="admin", force=True)

    quarantined = _quarantine_files(home)
    assert len(quarantined) == 1
    assert broken_sha[:8] in quarantined[0].name  # 文件名含 hash8
    marker = json.loads(_sentinel_path(home).read_text(encoding="utf-8"))
    assert marker["original_sha256"] == broken_sha
    assert marker["quarantine_path"] == f"quarantine/{quarantined[0].name}"
    assert marker["error_class"] == "unreadable"
    assert marker["recovery"] == "manual"  # 门槛4: 明确转人工, 不伪造自愈
    assert marker["quarantined_at"]
    # 隔离副本与哨兵 hash 一致: 隔离后文件字节可验证
    assert _sha256(quarantined[0]) == broken_sha


def test_schema_and_lease_damage_error_classes(tmp_path: Path) -> None:
    """schema 损坏与 lease 损坏各自结构化分类, 且全部 fail-closed。"""
    home = _new_home(tmp_path, "classes")
    service, state = _state_with_cursor_candidate_and_lease(home)

    # schema_invalid: 合法 JSON 但字段形状不对(类型错误;
    # from_dict 对纯未知 key 宽容补默认, 那是既有设计, 不属于本棒损坏分类)
    _state_path(home).write_text(json.dumps({"per_thread_cursors": "oops"}), encoding="utf-8")
    r1 = service.run(reason="admin", force=True)
    assert r1.failure_code == "CURATOR_STATE_CORRUPT"
    assert json.loads(_sentinel_path(home).read_text(encoding="utf-8"))["error_class"] == (
        "schema_invalid"
    )

    # lease 损坏(expires_at 无时区): 独立分类
    home2 = _new_home(tmp_path, "classes-lease")
    service2, _ = _state_with_cursor_candidate_and_lease(home2)
    damaged = json.loads(_state_path(home2).read_text(encoding="utf-8"))
    damaged["active_lease"]["expires_at"] = "2026-08-13T10:00:00"  # 无时区
    _state_path(home2).write_text(json.dumps(damaged), encoding="utf-8")
    r2 = service2.run(reason="admin", force=True)
    assert r2.failure_code == "CURATOR_STATE_CORRUPT"
    assert json.loads(_sentinel_path(home2).read_text(encoding="utf-8"))["error_class"] == (
        "lease_naive_expires_at"
    )
    # 失败可读: 审计含稳定 run_id 与隔离路径 warning
    record = _last_run_record(home2)
    assert record is not None
    assert record["run_id"].startswith("memory-curator-corrupt-")
    assert len(record["run_id"]) == len("memory-curator-corrupt-") + 16
    assert any(str(w).startswith("state_quarantined:") for w in record["warnings"])


# ---------------------------------------------------------------- 门槛2+4: 结构化恢复通道

def test_recover_restores_from_authoritative_audit(tmp_path: Path) -> None:
    """恢复走 recover() 结构化通道: 凭权威审计链重建游标, 恢复审计落账, 只消费新消息。"""
    home = _new_home(tmp_path, "recover")
    service, state = _state_with_cursor_candidate_and_lease(home)
    processed_after_first = int(state.get("processed_count") or 0)
    cursor_before = state["per_thread_cursors"]
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    assert service.run(reason="admin", force=True).failure_code == "CURATOR_STATE_CORRUPT"
    marker = json.loads(_sentinel_path(home).read_text(encoding="utf-8"))
    candidates_before = len(
        [l for l in (home / "memory" / "candidates.jsonl").read_text().splitlines() if l.strip()]
    )

    result = service.recover()

    # 结构化恢复成功 + 证据摘要
    assert result["status"] == "recovered"
    assert result["reason"] == "restored_from_authoritative_audit"
    assert result["audit_recorded"] is True
    assert result["evidence"]["quarantine_path"] == marker["quarantine_path"]
    # 哨兵删除, 隔离副本保留(取证不可变)
    assert not _sentinel_path(home).exists()
    assert len(_quarantine_files(home)) == 1
    # 恢复 state 游标 == 最后成功审计 cursor_after(权威对账, 门槛2)
    succeeded = [r for r in _run_records(home) if r["status"] == "succeeded"]
    last_success = succeeded[-1]
    restored = json.loads(_state_path(home).read_text(encoding="utf-8"))
    assert restored["per_thread_cursors"] == last_success["cursor_after"]["per_thread_cursors"]
    assert restored["last_processed_audit_event_id"] == last_success["cursor_after"][
        "last_audit_event_id"
    ]
    assert restored["active_lease"] == {}  # 损坏前 lease 不可信, 不接管
    # 恢复审计记录: 结构化(状态/阶段/恢复证据), 可审计(门槛2)
    recovery_records = [r for r in _run_records(home) if r["status"] == "recovered_rollback"]
    assert len(recovery_records) == 1
    assert recovery_records[0]["phase"] == "recovery"
    assert recovery_records[0]["recovery"]["kind"] == "manual_from_quarantine"
    assert recovery_records[0]["recovery"]["sentinel_sha256"] == marker["original_sha256"]
    assert recovery_records[0]["recovery"]["restored_from_run_id"] == last_success["run_id"]
    # Candidate/Daily 对账: 恢复动作本身不产生候选/daily
    candidates_after = len(
        [l for l in (home / "memory" / "candidates.jsonl").read_text().splitlines() if l.strip()]
    )
    assert candidates_after == candidates_before

    # 恢复后新消息到达, 重启维护: 只消费新消息(processed_count 只增 1), 不重复处理
    thread = ConversationStore(home / "conversations").get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 2000.0,
        }
    )
    _append_message(home, thread.thread_id, seq=2, content="今天新增了第二台设备。")
    fresh = _make_service(home)
    r = fresh.run(reason="admin", force=True)

    assert r.status == "succeeded", f"恢复后 run 失败: {r}"
    fresh_state = json.loads(_state_path(home).read_text(encoding="utf-8"))
    assert int(fresh_state["processed_count"]) == processed_after_first + 1
    assert fresh_state["per_thread_cursors"] != cursor_before  # 游标从恢复点继续推进


def test_recover_refused_on_copy_mismatch(tmp_path: Path) -> None:
    """隔离副本被篡改(hash 与哨兵不符) → 拒绝执行, 不解除隔离, 不伪造自愈。"""
    home = _new_home(tmp_path, "recover-tamper")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    assert service.run(reason="admin", force=True).failure_code == "CURATOR_STATE_CORRUPT"
    _quarantine_files(home)[0].write_text('{"tampered": true}', encoding="utf-8")

    result = service.recover()

    assert result["status"] == "refused"
    assert result["reason"] == "quarantine_copy_mismatch"
    assert _sentinel_path(home).exists()  # 隔离保持
    assert not _state_path(home).exists()  # 无任何写


def test_recover_refused_without_authoritative_audit(tmp_path: Path) -> None:
    """无成功审计链(v1 迁移/纯手工 state) → 拒绝自动恢复, 明确转人工。"""
    home = _new_home(tmp_path, "recover-noaudit")
    (home / "memory" / "curator").mkdir(parents=True, exist_ok=True)
    _state_path(home).write_text(json.dumps({"per_thread_cursors": {"t1": "msg-9"}}), encoding="utf-8")
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    service = _make_service(home)
    assert service.run(reason="admin", force=True).failure_code == "CURATOR_STATE_CORRUPT"

    result = service.recover()

    assert result["status"] == "refused"
    assert result["reason"] == "no_authoritative_audit"
    assert _sentinel_path(home).exists()


def test_recover_refused_when_cursor_mismatch(tmp_path: Path) -> None:
    """副本可解析但游标与审计链分裂(越界推进痕迹) → 拒绝, 不采用不可信游标。"""
    home = _new_home(tmp_path, "recover-split")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    # 损坏形态: schema_invalid(游标值类型错 → from_dict 失败)且游标与审计链分裂
    _state_path(home).write_text(
        json.dumps({"per_thread_cursors": {"t1": 999}}), encoding="utf-8"
    )
    assert service.run(reason="admin", force=True).failure_code == "CURATOR_STATE_CORRUPT"

    result = service.recover()

    assert result["status"] == "refused"
    assert result["reason"] == "cursor_mismatch_with_audit"
    assert _sentinel_path(home).exists()
    assert not _state_path(home).exists()


def test_recover_idempotent_noop_and_loop_keeps_serving(tmp_path: Path) -> None:
    """恢复后重复 recover → noop; 恢复后 Gateway 维护循环连续可用。"""
    home = _new_home(tmp_path, "recover-idem")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    assert service.run(reason="admin", force=True).failure_code == "CURATOR_STATE_CORRUPT"
    assert service.recover()["status"] == "recovered"

    again = service.recover()

    assert again["status"] == "noop"
    assert again["reason"] == "no_quarantine_marker"
    for _ in range(3):  # 恢复后维护循环连续可用
        result = service.run(reason="admin", force=True)
        assert result.status == "succeeded"


# ---------------------------------------------------------------- 门槛3: 隔离失败退避合同

def test_quarantine_failure_backoff_reuses_audit(tmp_path: Path) -> None:
    """隔离失败退避合同: 冷却窗口内复用上次失败审计(不新增行); 期满重试成功。"""
    home = _new_home(tmp_path, "qfail-backoff")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    (home / "memory" / "curator" / "quarantine").write_text("not a dir", encoding="utf-8")

    r1 = service.run(reason="admin", force=True)
    assert r1.failure_code == "CURATOR_STATE_QUARANTINE_FAILED"
    runs_after_first = len(_run_records(home))
    marker_path = home / "memory" / "curator" / "state.quarantine-backoff"
    assert marker_path.exists()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["error_class"] == "unreadable"
    assert marker["run_id"] == r1.run_id

    # 冷却窗口内: 复用同一条审计(不新增行), run_id 不变, 失败仍可观测
    r2 = service.run(reason="admin", force=True)
    assert r2.failure_code == "CURATOR_STATE_QUARANTINE_FAILED"
    assert r2.run_id == r1.run_id
    assert len(_run_records(home)) == runs_after_first

    # 冷却期满(把 marker 时间改为过去) + 修复占位: 重试隔离 → 成功闭环
    stale = dict(marker)
    stale["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    marker_path.write_text(json.dumps(stale), encoding="utf-8")
    (home / "memory" / "curator" / "quarantine").unlink()
    r3 = service.run(reason="admin", force=True)
    assert r3.failure_code == "CURATOR_STATE_CORRUPT"  # 隔离成功(新审计行)
    assert len(_quarantine_files(home)) == 1
    assert not marker_path.exists()  # 成功隔离清除退避 marker
    assert len(_run_records(home)) == runs_after_first + 1


# ---------------------------------------------------------------- 门槛5: Gateway 连续可用

def test_gateway_maintenance_loop_keeps_serving(tmp_path: Path) -> None:
    """损坏后 Gateway 维护循环连续可用: 多次 run_if_due 全部返回 failed 不抛异常。"""
    home = _new_home(tmp_path, "gateway")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")

    for _ in range(5):  # 模拟维护 tick 循环
        result = service.run_if_due()
        assert result.status == "failed"
        assert result.failure_code == "CURATOR_STATE_CORRUPT"
    assert len(_quarantine_files(home)) == 1  # 全程只隔离一次
    # 审计 = 1 条前置成功 + 1 条损坏(重复 tick 幂等, 不刷屏)
    assert len(_run_records(home)) == 2


# ---------------------------------------------------------------- 门槛3: 隔离写失败可观测

def test_quarantine_failure_is_observable_and_bounded(tmp_path: Path) -> None:
    """quarantine 目录被占位导致隔离失败: 独立错误码 + run_log 落账 + 每 tick 单次尝试。"""
    home = _new_home(tmp_path, "qfail")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    # 占位: quarantine 目标被同名文件占用 -> mkdir/rename 失败
    (home / "memory" / "curator" / "quarantine").write_text("not a dir", encoding="utf-8")

    r1 = service.run(reason="admin", force=True)

    assert r1.status == "failed"
    assert r1.failure_code == "CURATOR_STATE_QUARANTINE_FAILED"  # 可观测: 独立于 corrupt
    record = _last_run_record(home)
    assert record is not None and record["failure_code"] == "CURATOR_STATE_QUARANTINE_FAILED"
    # 坏文件仍在原位(未被误删/未被消费), 修好占位后可隔离
    assert _state_path(home).read_text(encoding="utf-8") == BROKEN_STATE
    (home / "memory" / "curator" / "quarantine").unlink()
    # 退避合同: 冷却窗口内不重试(复用审计), 期满后才再试 → 修好后成功隔离
    marker_path = home / "memory" / "curator" / "state.quarantine-backoff"
    stale = json.loads(marker_path.read_text(encoding="utf-8"))
    stale["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    marker_path.write_text(json.dumps(stale), encoding="utf-8")
    r2 = service.run(reason="admin", force=True)
    assert r2.failure_code == "CURATOR_STATE_CORRUPT"
    assert len(_quarantine_files(home)) == 1  # 修好后成功隔离(补做, 不重复)


# ---------------------------------------------------------------- 门槛2: request 也 fail-closed

def test_request_fail_closed_when_state_damaged(tmp_path: Path) -> None:
    """损坏期间 request(新触发登记)同样 fail-closed: 不写空 state、不隔离(只读路径)。"""
    home = _new_home(tmp_path, "request")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")

    with pytest.raises(CuratorStateCorruptError):
        service.request(reason="session_close")
    # fail-closed: 坏文件原样未动(未被改写/未被隔离), 无哨兵
    assert _state_path(home).read_text(encoding="utf-8") == BROKEN_STATE
    assert not _sentinel_path(home).exists()
    assert not _quarantine_files(home)


# ---------------------------------------------------------------- 门槛1: acquire/commit 也 fail-closed

def test_acquire_and_commit_fail_closed_when_quarantined(tmp_path: Path) -> None:
    """隔离后 acquire/commit_success/commit_failure 全部 fail-closed:
    不接管不可信 lease、不写空 state、不推进 cursor; 隔离副本与哨兵保持不动。"""
    home = _new_home(tmp_path, "acquire-commit")
    service, _ = _state_with_cursor_candidate_and_lease(home)
    _state_path(home).write_text(BROKEN_STATE, encoding="utf-8")
    assert service.run(reason="admin", force=True).failure_code == "CURATOR_STATE_CORRUPT"
    assert _sentinel_path(home).exists()
    isolated = _quarantine_files(home)
    assert len(isolated) == 1

    store = service.state_store
    with pytest.raises(CuratorStateCorruptError):
        store.acquire(reason="admin", config_revision="rev", lease_seconds=120)
    with pytest.raises(CuratorStateCorruptError):
        store.commit_success(
            CuratorSuccessCommit(
                lease_id="memory-curator-lease-x",
                run_id="memory-curator-run-x",
                reason="admin",
                per_thread_cursors={},
                last_processed_audit_event_id="",
                processed_messages=1,
                processed_audit_events=0,
                candidate_count=0,
                daily_event_count=0,
            )
        )
    with pytest.raises(CuratorStateCorruptError):
        store.commit_failure(lease_id="memory-curator-lease-x", failure_code="CURATOR_FAILED")
    with pytest.raises(CuratorStateCorruptError):
        store.load()

    # fail-closed 全程无副作用: 哨兵在、隔离副本不变、无新 state、无新增失败审计
    assert _sentinel_path(home).exists()
    assert _quarantine_files(home) == isolated
    assert not _state_path(home).exists()
    corrupt = [e for e in _run_records(home) if e["failure_code"]]
    assert corrupt and all(e["failure_code"] == "CURATOR_STATE_CORRUPT" for e in corrupt)


# ---------------------------------------------------------------- P0 遗留(seq1625): 纯未知字段 fail-closed

def test_unknown_only_state_fail_closed(tmp_path: Path) -> None:
    """非空但无任何已知字段的 state payload(如 {"not":"a state"}) -> 抛
    ValueError, 不复用默认 state(seq1625 P0 遗留修复)。"""
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorState

    with pytest.raises(ValueError):
        MemoryCuratorState.from_dict({"not": "a state"})


def test_empty_state_first_init_allowed(tmp_path: Path) -> None:
    """空对象 {} = 无 state 首次初始化(允许, 返回默认 state)。"""
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorState

    state = MemoryCuratorState.from_dict({})
    assert state.processed_count == 0  # 默认 state


def test_partial_unknown_keeps_known_fields(tmp_path: Path) -> None:
    """部分未知(含已知字段) -> 按已知子集恢复(兼容旧 state 文件历史字段)。"""
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorState

    state = MemoryCuratorState.from_dict(
        {"processed_count": 42, "future_field": "legacy"}
    )
    assert state.processed_count == 42  # 已知字段恢复
