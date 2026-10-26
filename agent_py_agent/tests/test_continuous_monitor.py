from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.ingestion import continuous_monitor
from agent_py_agent.agent.ingestion import harvester as hv
from agent_py_agent.agent.ingestion import watch_state as ws
from agent_py_agent.agent.ingestion.continuous_monitor import (
    ContinuousProofPolicy,
    discover_owner_homes,
    evaluate_continuous_proof,
    recover_active_audit_harvesters,
)


def _watch(index: int, *, error: str = "") -> dict:
    return {
        "watch_id": f"w{index}",
        "source": {
            "scheme": "https" if index < 2 else "file",
            "origin_hash": f"origin-{index}",
        },
        "source_mode": "cursor" if index < 2 else "file",
        "envelope_keys": [f"schema_{index}"],
        "audit_guarantee": True,
        "totals": {"pulls": 10},
        "last_error_code": error,
        "last_source_at": 1000,
    }


def test_proof_requires_real_elapsed_time_not_accelerated_cycle_count() -> None:
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1060, "watches": [_watch(1), _watch(2), _watch(3)]},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=3600, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is False
    assert report["reason"] == "duration_too_short"


def test_proof_requires_continuous_healthy_heterogeneous_guarantee_sources() -> None:
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1060, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1120, "watches": [_watch(1), _watch(2), _watch(3)]},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=60),
    )
    assert report == {
        "proven": True,
        "reason": "ok",
        "continuous_seconds": 120,
        "healthy_guarantee_sources": 3,
        "heterogeneous_signatures": 3,
    }


def test_proof_rejects_unhealthy_source() -> None:
    watches = [_watch(1), _watch(2), _watch(3, error="NETWORK")]
    watches[2]["last_source_at"] = 0
    report = evaluate_continuous_proof(
        [{"observed_at": 0, "watches": watches}, {"observed_at": 120, "watches": watches}],
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is False
    assert report["reason"] == "insufficient_healthy_guarantee_sources"


def test_proof_tolerates_transient_error_while_source_fact_is_fresh() -> None:
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1060, "watches": [_watch(1), _watch(2, error="NETWORK"), _watch(3)]},
        {"observed_at": 1120, "watches": [_watch(1), _watch(2), _watch(3)]},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=60),
    )
    assert report["proven"] is True


def test_proof_resets_duration_after_intermediate_source_staleness() -> None:
    stale = [_watch(1), _watch(2), _watch(3)]
    stale[2]["last_source_at"] = 1000
    recovered_1 = [_watch(1), _watch(2), _watch(3)]
    recovered_2 = [_watch(1), _watch(2), _watch(3)]
    for row in recovered_1:
        row["last_source_at"] = 1180
    for row in recovered_2:
        row["last_source_at"] = 1240
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1120, "watches": stale},
        {"observed_at": 1180, "watches": recovered_1},
        {"observed_at": 1240, "watches": recovered_2},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(
            minimum_seconds=180,
            maximum_sample_gap_seconds=120,
            maximum_source_staleness_seconds=60,
        ),
    )
    assert report["proven"] is False
    assert report["continuous_seconds"] == 60
    assert report["reason"] == "duration_too_short"


def test_proof_resets_duration_after_sample_gap_and_can_recover() -> None:
    watches = [_watch(1), _watch(2), _watch(3)]
    snapshots = [
        {"observed_at": 1000, "watches": watches},
        {"observed_at": 1060, "watches": watches},
        {"observed_at": 1300, "watches": watches},
        {"observed_at": 1360, "watches": watches},
        {"observed_at": 1420, "watches": watches},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is True
    assert report["continuous_seconds"] == 120


def test_proof_rejects_source_that_only_succeeded_in_the_past() -> None:
    watches = [_watch(1), _watch(2), _watch(3)]
    for row in watches:
        row["last_source_at"] = 1000
    report = evaluate_continuous_proof(
        [{"observed_at": 1000, "watches": watches}, {"observed_at": 2000, "watches": watches}],
        ContinuousProofPolicy(
            minimum_seconds=1000,
            maximum_sample_gap_seconds=1000,
            maximum_source_staleness_seconds=300,
        ),
    )
    assert report["proven"] is False
    assert report["reason"] == "insufficient_healthy_guarantee_sources"


def test_owner_discovery_only_walks_canonical_owner_levels(tmp_path) -> None:
    owner = tmp_path / "owners" / "providers" / "local" / "users" / "u1"
    watch_state = owner / "watch_state"
    watch_state.mkdir(parents=True)
    (watch_state / "ws-1234567890.json").write_text("{}", encoding="utf-8")
    decoy = owner / "tasks" / "deep" / "watch_state"
    decoy.mkdir(parents=True)
    (decoy / "ws-decoy.json").write_text("{}", encoding="utf-8")
    assert discover_owner_homes(tmp_path) == [owner]


def test_restart_recovery_only_reacquires_active_named_audit_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
    rows = [
        {"watch_id": "legacy", "audit_guarantee": False, "closed": False},
        {"watch_id": "inactive", "audit_guarantee": True, "closed": False},
        {"watch_id": "unavailable", "audit_guarantee": True, "closed": False},
        {"watch_id": "active", "audit_guarantee": True, "closed": False},
        {"watch_id": "closed", "audit_guarantee": True, "closed": True},
    ]
    states = {
        watch_id: SimpleNamespace(
            watch_id=watch_id,
            audit_root_task_id=f"task-{watch_id}",
            lock=threading.RLock(),
        )
        for watch_id in ("inactive", "unavailable", "active")
    }
    ensured: list[str] = []
    monkeypatch.setattr(continuous_monitor, "list_states", lambda _home: rows)
    monkeypatch.setattr(
        continuous_monitor.watch_state,
        "registry",
        SimpleNamespace(get_or_load=lambda _home, watch_id: states.get(watch_id)),
    )
    monkeypatch.setattr(continuous_monitor, "refresh_scalars_from_disk", lambda _state: None)
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.source_worker.audit_parent_reconcile_state",
        lambda _agent, task_id: {
            "task-inactive": (False, "inactive"),
            "task-unavailable": (False, "unavailable"),
            "task-active": (True, "active"),
        }[task_id],
    )
    monkeypatch.setattr(
        continuous_monitor,
        "ensure_harvester",
        lambda state, _fetch, **_kwargs: ensured.append(state.watch_id)
        or {"mode": "local"},
    )

    assert recover_active_audit_harvesters(agent) == 1
    assert ensured == ["active"]


def test_restart_recovery_without_owner_scope_fails_closed() -> None:
    assert recover_active_audit_harvesters(SimpleNamespace()) == 0


# 函数用途: 在 owner 下落一条仍在运行的命名 Audit 数据源状态，换上干净的 registry，并让父任务检查视为活跃。
def _active_audit_watch(owner_home: Path, monkeypatch) -> str:
    state = ws.new_state(owner_home, "http://src.example/pull", {})
    state.audit_guarantee = True
    state.audit_root_task_id = "task-active"
    ws.persist_state(state)
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.source_worker.audit_parent_reconcile_state",
        lambda _agent, _task_id: (True, "active"),
    )
    return state.watch_id


def test_restart_recovery_hands_the_refreshed_registry_object_to_the_harvester(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    watch_id = _active_audit_watch(owner_home, monkeypatch)
    # registry 里缓存着一份游标落后的旧对象，盘上游标已前进到 100。
    cached = ws.registry.get_or_load(owner_home, watch_id)
    newer = ws.load_state(owner_home, watch_id)
    newer.cursor = 100
    ws.persist_state(newer)
    captured = []
    monkeypatch.setattr(
        continuous_monitor,
        "ensure_harvester",
        lambda state, _fetch, **_kwargs: captured.append(state) or {"mode": "local"},
    )
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
    assert recover_active_audit_harvesters(agent) == 1
    assert len(captured) == 1 and captured[0] is cached and ws.registry.get(watch_id) is cached
    assert cached.cursor == 100


def test_pull_after_restart_recovery_reuses_the_harvester_holding_the_registry_object(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    watch_id = _active_audit_watch(owner_home, monkeypatch)
    held = []

    # 只把收割循环换成记录状态对象、等停止信号的假循环；登记、租约和复用都走真实 ensure_harvester。
    def loop(state, _fetch, stop_event, *rest):
        held.append(state)
        stop_event.wait(10)
        hv._clear_lease(state, rest[0])  # rest[0] 是 lease_id，其后是两个回调

    monkeypatch.setattr(hv, "_harvest_loop", loop)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
    assert recover_active_audit_harvesters(agent) == 1
    started = hv.harvesters.get_live(watch_id)
    assert started is not None
    try:
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            time.sleep(0.01)
        # 与 pull 入口相同：先经 registry 取对象，再 ensure_harvester（watch_tool 的 pull 与 _pull_from_spool）。
        pulled = ws.registry.get_or_load(owner_home, watch_id)
        assert hv.ensure_harvester(pulled, lambda *_args, **_kwargs: None) == {"mode": "local"}
        assert hv.harvesters.get_live(watch_id) is started
        assert len(held) == 1 and held[0] is pulled
    finally:
        hv.stop_harvester(watch_id)
        started.thread.join(timeout=10)


# LLM: 只构造本地健康度证据，不启动真实来源；时间及陈旧度由用例显式控制。
# 函数用途: 生成可用于全量与增量对照的快照。
def _proof_sample(at, *, count=3, last_source_at=None, error="", heterogeneous=True):
    watches = [_watch(index + 1 if heterogeneous else 1, error=error) for index in range(count)]
    for watch in watches:
        watch["last_source_at"] = at if last_source_at is None else last_source_at
    return {"observed_at": at, "watches": watches}


# LLM: 参考路径仍经过原公开read_snapshots和全量排序，不能由增量评估器生成期望值。
# 函数用途: 核对本轮增量结果与公开全量接口逐字段一致。
def _assert_file_proof(evaluator, policy=None):
    expected = evaluate_continuous_proof(continuous_monitor.read_snapshots(evaluator.path), policy)
    actual = evaluator.evaluate(policy)
    assert actual == expected
    return actual


# LLM: 明确列出重置、恢复、相等时间和负间隙的期望，不让两条共用算法路径互相掩盖错误。
# 函数用途: 验证折叠器保持最后连续区间与最新源计数。
def test_proof_accumulator_resets_and_recovers_without_history():
    accumulator = continuous_monitor._ProofAccumulator()
    policy = ContinuousProofPolicy(maximum_sample_gap_seconds=120)
    assert not accumulator.has_rows and accumulator.metrics() == continuous_monitor.ProofMetrics()
    samples = [
        (1000, 3, 3, 0, 0), (1060, 3, 3, 60, 60), (1120, 2, 2, 0, 0),
        (1180, 3, 3, 0, 0), (1240, 3, 3, 60, 60), (1420, 3, 3, 0, 0),
        (1480, 3, 1, 0, 0), (1540, 3, 3, 0, 0), (1540, 3, 3, 0, 0),
        (1500, 3, 3, 0, 0), (1560, 3, 3, 60, 60),
    ]
    for at, healthy, signatures, duration, gap in samples:
        accumulator.push(at, healthy, signatures, policy)
        assert accumulator.has_rows and accumulator.previous_at == at
        assert accumulator.metrics() == continuous_monitor.ProofMetrics(duration, healthy, signatures, gap)


# LLM: 用真实全量调用捕获push，要求全量与文件路径只有一个折叠算法权威。
# 函数用途: 防止全量评估保留另一份区间计算实现。
def test_full_proof_metrics_uses_shared_accumulator(monkeypatch):
    pushed = []
    original = continuous_monitor._ProofAccumulator.push

    # LLM: 只记录投影参数，仍执行原方法，不伪造证明结果。
    # 函数用途: 观测全量路径实际使用折叠器。
    def tracked(self, observed_at, healthy, signatures, policy):
        pushed.append((observed_at, healthy, signatures))
        return original(self, observed_at, healthy, signatures, policy)

    monkeypatch.setattr(continuous_monitor._ProofAccumulator, "push", tracked)
    evaluate_continuous_proof([_proof_sample(1060), _proof_sample(1000)])
    assert pushed == [(1000.0, 3, 3), (1060.0, 3, 3)]


# LLM: 真实追加后逐轮与全量比较，覆盖瞬态错误、陈旧、异构不足、恢复和采样间隙。
# 函数用途: 验证单调追加中的报告不因增量化改变。
def test_file_evaluator_matches_full_proof_after_each_append(tmp_path):
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(tmp_path / "evidence.ndjson")
    policy = ContinuousProofPolicy(minimum_seconds=60, maximum_source_staleness_seconds=90)
    for row in [
        _proof_sample(1000), _proof_sample(1060, error="NETWORK"),
        _proof_sample(1120, last_source_at=1000), _proof_sample(1180, count=0),
        _proof_sample(1240), _proof_sample(1300, heterogeneous=False),
        _proof_sample(1360), _proof_sample(1420), _proof_sample(1720), _proof_sample(1780),
    ]:
        continuous_monitor.append_snapshot(evaluator.path, row)
        _assert_file_proof(evaluator, policy)
        assert evaluator._committed_offset == evaluator.path.stat().st_size
        assert not evaluator._non_monotonic


# LLM: 仅包装真实文件readline，记录实际读取位置与字节；不会把全文件预装进内存。
# 类用途: 证明评估器只读取提交游标后的新增内容。
class _EvidenceReadSpy:
    # LLM: 只持有本轮真实文件句柄和测试记录表。
    # 函数用途: 创建字节读取观察器。
    def __init__(self, handle, reads):
        self.handle, self.reads = handle, reads

    # LLM: 流式评估必须显式从提交游标seek，交给真实句柄执行。
    # 函数用途: 转发文件定位。
    def seek(self, offset):
        return self.handle.seek(offset)

    # LLM: 每条记录保存读取前位置和真实返回字节数，包含EOF读。
    # 函数用途: 观测增量读取范围。
    def readline(self):
        position = self.handle.tell()
        raw = self.handle.readline()
        self.reads.append((position, len(raw)))
        return raw


# LLM: 拦截真实open而非伪造内容；常驻状态只允许路径、固定stat/policy及七标量折叠器。
# 函数用途: 证明追加和无变化评估不重读历史、不保留历史点列表。
def test_file_evaluator_reads_new_bytes_and_keeps_constant_state(tmp_path, monkeypatch):
    path = tmp_path / "evidence.ndjson"
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    _assert_file_proof(evaluator)
    committed = path.stat().st_size
    original_open, reads = Path.open, []

    # LLM: 只包装目标证据的二进制读取，写入与其他文件均保持原实现。
    # 函数用途: 安装不改变文件语义的读取探针。
    @contextmanager
    def traced_open(target, mode="r", *args, **kwargs):
        with original_open(target, mode, *args, **kwargs) as handle:
            yield _EvidenceReadSpy(handle, reads) if target == path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", traced_open)
    continuous_monitor.append_snapshot(path, _proof_sample(1060))
    expected = evaluate_continuous_proof(continuous_monitor.read_snapshots(path))
    assert evaluator.evaluate() == expected
    assert reads[0][0] == committed
    assert sum(size for _, size in reads) == path.stat().st_size - committed
    reads.clear()
    assert evaluator.evaluate() == expected
    assert reads == [(path.stat().st_size, 0)]
    for at in range(1120, 61120, 60):
        continuous_monitor.append_snapshot(path, _proof_sample(at))
    _assert_file_proof(evaluator)
    assert set(vars(evaluator)) == {
        "path", "_file_identity", "_committed_offset", "_last_stat_stamp",
        "_policy", "_accumulator", "_non_monotonic", "_head_digest",
    }
    assert {field.name for field in fields(evaluator._accumulator)} == {
        "has_rows", "previous_at", "latest_at", "segment_start",
        "segment_maximum_gap", "latest_healthy", "latest_signatures",
    }
    assert all(isinstance(value, (bool, float, int)) for value in vars(evaluator._accumulator).values())
    assert not any(isinstance(value, (list, dict, set, bytes)) for value in vars(evaluator).values())


# LLM: 合法尾行只在临时副本上参与证明，直到LF到达才提交一次。
# 函数用途: 验证未换行尾行的重复观察及后续换行追加不漏不重。
def test_file_evaluator_uses_unterminated_valid_tail_without_committing(tmp_path):
    path = tmp_path / "evidence.ndjson"
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    committed = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(json.dumps(_proof_sample(1060)).encode())
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    policy = ContinuousProofPolicy(minimum_seconds=60)
    for _ in range(2):
        assert _assert_file_proof(evaluator, policy)["continuous_seconds"] == 60
        assert evaluator._committed_offset == committed
        assert evaluator._accumulator.previous_at == 1000
    with path.open("ab") as handle:
        handle.write(b"\n")
    continuous_monitor.append_snapshot(path, _proof_sample(1120))
    assert _assert_file_proof(evaluator, policy)["continuous_seconds"] == 120
    assert evaluator._committed_offset == path.stat().st_size
    assert evaluator._accumulator.previous_at == 1120


# LLM: UTF8多字节字符和JSON均可能跨追加边界；半行失败不得提前消耗字节。
# 函数用途: 验证半截尾行补全后恢复及Unicode正文不被误切。
def test_file_evaluator_retries_partial_utf8_and_json_tail(tmp_path):
    path = tmp_path / "evidence.ndjson"
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    initial = _assert_file_proof(evaluator)
    offset = path.stat().st_size
    raw = json.dumps({**_proof_sample(1060), "label": "中🌊\u0085\u2028\u2029"}, ensure_ascii=False).encode()
    split = raw.index("🌊".encode()) + 2
    with path.open("ab") as handle:
        handle.write(raw[:split])
    assert evaluator.evaluate() == initial
    assert evaluator._committed_offset == offset
    with path.open("ab") as handle:
        handle.write(raw[split:])
    assert _assert_file_proof(evaluator)["continuous_seconds"] == 60
    assert evaluator._committed_offset == offset
    with path.open("ab") as handle:
        handle.write(b"\r\n")
    _assert_file_proof(evaluator)
    assert evaluator._committed_offset == path.stat().st_size


# LLM: JSONL只认物理LF，坏行和非对象跳过，但合法空对象不是零条证据。
# 函数用途: 验证坏记录、CRLF、Unicode正文及无有效证据的等价性。
@pytest.mark.parametrize("raw", [
    b"", b"\n\r\n", b"bad\n[]\ntrue\nnull\n12\n", b'{"observed_at":', b"{}\r\n",
    b"bad\r\n[]\n" + json.dumps({**_proof_sample(1000), "label": "\u0085\u2028\u2029"}, ensure_ascii=False).encode() + b"\r\n",
], ids=["empty", "blank", "invalid-and-nonobjects", "partial-json", "empty-object", "unicode-crlf"])
def test_file_evaluator_preserves_jsonl_record_boundaries(tmp_path, raw):
    path = tmp_path / "evidence.ndjson"
    path.write_bytes(raw)
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    _assert_file_proof(evaluator)
    assert evaluator._committed_offset == (raw.rfind(b"\n") + 1)


# LLM: 文件评估器按任务要求逐行跳过坏UTF8，不改变原read_snapshots的公开异常行为。
# 函数用途: 验证坏UTF8已提交行不会吞掉后续合法记录。
def test_file_evaluator_skips_invalid_utf8_line_without_changing_public_reader(tmp_path):
    path = tmp_path / "evidence.ndjson"
    path.write_bytes(b"\xff\n[]\n")
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    assert evaluator.evaluate() == evaluate_continuous_proof([_proof_sample(1000)])
    assert evaluator._committed_offset == path.stat().st_size
    with pytest.raises(UnicodeDecodeError):
        continuous_monitor.read_snapshots(path)


# LLM: 同尺寸重写检测同时覆盖有未提交尾行的情况；截断不能只比较提交游标。
# 函数用途: 验证轮转、完整/带尾行覆写及截断重写均丢弃旧折叠状态。
@pytest.mark.parametrize("mutation", ["truncate", "overwrite", "rotate", "tail_overwrite", "tail_truncate"])
def test_file_evaluator_rebuilds_after_file_replacement(tmp_path, mutation):
    path = tmp_path / "evidence.ndjson"
    for at in (1000, 1060):
        continuous_monitor.append_snapshot(path, _proof_sample(at))
    if mutation.startswith("tail_"):
        with path.open("ab") as handle:
            handle.write(json.dumps(_proof_sample(1120)).encode() if mutation == "tail_overwrite" else b"incomplete" * 1000)
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    policy = ContinuousProofPolicy(minimum_seconds=60)
    assert _assert_file_proof(evaluator, policy)["proven"]
    original = path.read_bytes()
    identity = evaluator._file_identity
    rewritten = original.replace(b'"audit_guarantee": true', b'"audit_guarantee": false')
    if mutation in {"truncate", "tail_truncate"}:
        rewritten = b"\n".join(rewritten.split(b"\n")[:1 if mutation == "truncate" else 3]) + b"\n"
        if mutation == "tail_truncate":
            # 保留大小仍超过旧提交游标的重写，钉住未提交尾行掩盖截断的情况。
            rewritten = rewritten[:rewritten.find(b"incomplete")] + json.dumps(_proof_sample(1120, count=0)).encode() + b"\n"
            assert len(rewritten) > evaluator._committed_offset
    if mutation == "rotate":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(rewritten)
        replacement.replace(path)
    else:
        path.write_bytes(rewritten)
    assert not _assert_file_proof(evaluator, policy)["proven"]
    assert (evaluator._file_identity != identity) is (mutation == "rotate")


@pytest.mark.parametrize("unterminated", [False, True], ids=["overwrite", "tail_overwrite"])
@pytest.mark.parametrize("digest_failures", [(), (0,), (0, 1), (1,)], ids=["available", "initial-empty", "both-empty", "comparison-empty"])
def test_file_evaluator_rebuilds_after_larger_in_place_rewrite(tmp_path, monkeypatch, unterminated, digest_failures):
    path = tmp_path / "evidence.ndjson"
    original_digest, calls = continuous_monitor._file_head_digest, []

    def transient_digest(target, limit):
        index = len(calls)
        calls.append(limit)
        return "" if index in digest_failures else original_digest(target, limit)

    monkeypatch.setattr(continuous_monitor, "_file_head_digest", transient_digest)
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    with path.open("ab") as handle:
        handle.write(json.dumps(_proof_sample(1060)).encode() + (b"" if unterminated else b"\n"))
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    policy = ContinuousProofPolicy(minimum_seconds=90)
    assert _assert_file_proof(evaluator, policy)["continuous_seconds"] == 60
    before = path.stat()
    # 只改第一条已提交快照：后续健康记录不能掩盖旧区间起点被错误复用。
    rewritten = path.read_bytes().replace(b'"audit_guarantee": true', b'"audit_guarantee": false', 3)
    path.write_bytes(rewritten.rstrip(b"\n") + b"\n" + json.dumps(_proof_sample(1120)).encode() + b"\n")
    after = path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_size > before.st_size >= evaluator._committed_offset
    expected = evaluate_continuous_proof(continuous_monitor.read_snapshots(path), policy)
    assert expected["continuous_seconds"] == 60 and not expected["proven"]
    assert evaluator.evaluate(policy) == expected
    assert evaluator.evaluate(policy) == expected
    assert evaluator._head_digest == hashlib.sha256(path.read_bytes()[:4096]).hexdigest()


@pytest.mark.parametrize("limit", [-1, 0, 1, 4095, 4096, 4097, 9000])
def test_file_head_digest_is_bounded_sha256_text(tmp_path, monkeypatch, limit):
    path = tmp_path / "evidence.ndjson"
    raw = bytes(range(256)) * 40
    path.write_bytes(raw)
    original_read, reads = os.read, []

    def tracked_read(descriptor, size):
        reads.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", tracked_read)
    count = max(0, min(limit, 4096))
    expected = hashlib.sha256(raw[:count]).hexdigest() if count else ""
    actual = continuous_monitor._file_head_digest(path, limit)
    assert isinstance(actual, str) and actual == expected
    assert reads == ([count] if count else [])


@pytest.mark.parametrize("kind", ["missing", "empty", "symlink", "read_error"])
def test_file_head_digest_returns_empty_on_unreadable_or_empty_file(tmp_path, monkeypatch, kind):
    path = tmp_path / "evidence.ndjson"
    if kind == "empty":
        path.touch()
    elif kind == "symlink":
        target = tmp_path / "target.ndjson"
        target.write_bytes(b"not permitted through a link")
        path.symlink_to(target)
    elif kind == "read_error":
        path.write_bytes(b"head")
        original_read, descriptors = os.read, []

        def broken_read(descriptor, size):
            descriptors.append(descriptor)
            raise OSError("test digest read failure")

        monkeypatch.setattr(os, "read", broken_read)
    assert continuous_monitor._file_head_digest(path, 4096) == ""
    if kind == "read_error":
        assert len(descriptors) == 1
        monkeypatch.setattr(os, "read", original_read)
        with pytest.raises(OSError):
            os.fstat(descriptors[0])


def test_file_evaluator_refreshes_digest_across_head_cap_without_replaying(tmp_path):
    path = tmp_path / "evidence.ndjson"
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    _assert_file_proof(evaluator)
    accumulator = evaluator._accumulator
    assert 0 < evaluator._committed_offset < 4096
    for at in range(1060, 1661, 60):
        continuous_monitor.append_snapshot(path, _proof_sample(at))
        _assert_file_proof(evaluator)
        assert evaluator._accumulator is accumulator
        expected = hashlib.sha256(path.read_bytes()[:min(evaluator._committed_offset, 4096)]).hexdigest()
        assert vars(evaluator).get("_head_digest") == expected
    assert evaluator._committed_offset > 4096


def test_file_evaluator_retries_empty_digest_then_skips_unchanged_head_reads(tmp_path, monkeypatch):
    path = tmp_path / "evidence.ndjson"
    for at in range(1000, 1661, 60):
        continuous_monitor.append_snapshot(path, _proof_sample(at))
    assert path.stat().st_size > 4096
    original_digest, calls = continuous_monitor._file_head_digest, []

    def transient_empty(target, limit):
        calls.append(limit)
        return "" if len(calls) == 1 else original_digest(target, limit)

    monkeypatch.setattr(continuous_monitor, "_file_head_digest", transient_empty)
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    expected = _assert_file_proof(evaluator)
    assert vars(evaluator).get("_head_digest") == ""
    accumulator = evaluator._accumulator
    assert evaluator.evaluate() == expected
    assert evaluator._head_digest == hashlib.sha256(path.read_bytes()[:4096]).hexdigest()
    assert evaluator.evaluate() == expected
    assert evaluator._accumulator is accumulator
    assert calls == [evaluator._committed_offset] * 2


# LLM: policy不同会改变每条历史健康投影；语义相等的新policy实例不应破坏增量状态。
# 函数用途: 验证策略切换时重算历史并保持公开报告语义。
def test_file_evaluator_rebuilds_health_after_policy_change(tmp_path):
    path = tmp_path / "evidence.ndjson"
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    for at in (1000, 1060, 1120):
        continuous_monitor.append_snapshot(path, _proof_sample(at, last_source_at=1000))
    policy = ContinuousProofPolicy(minimum_seconds=120)
    assert _assert_file_proof(evaluator, policy)["proven"]
    strict = replace(policy, maximum_source_staleness_seconds=30)
    assert _assert_file_proof(evaluator, strict)["reason"] == "insufficient_healthy_guarantee_sources"
    assert _assert_file_proof(evaluator, policy)["proven"]
    state = evaluator._accumulator
    _assert_file_proof(evaluator, replace(policy))
    assert evaluator._accumulator is state


# LLM: 相等时间保持原文件顺序；乱序时按时间和文件索引稳定排序，后续轮次不得复用不安全前缀。
# 函数用途: 验证乱序回退、重复时间的最后状态和重写后的单调恢复。
@pytest.mark.parametrize("unterminated", [False, True])
def test_file_evaluator_sorts_non_monotonic_projection_stably(tmp_path, unterminated):
    path = tmp_path / "evidence.ndjson"
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    rows = [_proof_sample(1120), _proof_sample(1000), _proof_sample(1180), _proof_sample(1180, count=0), _proof_sample(1060)]
    for index, row in enumerate(rows):
        if unterminated and index == len(rows) - 1:
            with path.open("ab") as handle:
                handle.write(json.dumps(row).encode())
        else:
            continuous_monitor.append_snapshot(path, row)
        _assert_file_proof(evaluator)
        if index:
            assert evaluator._non_monotonic
    assert _assert_file_proof(evaluator)["healthy_guarantee_sources"] == 0
    path.write_text(json.dumps(_proof_sample(1240)) + "\n", encoding="utf-8")
    _assert_file_proof(evaluator)
    assert not evaluator._non_monotonic


# LLM: stat/open/read的OSError都必须清空缓存，不允许沿用旧成功证明。
# 函数用途: 验证删除与读取故障恢复后不会返回陈旧成功。
@pytest.mark.parametrize("failure", ["delete", "stat", "open", "read"])
def test_file_evaluator_discards_old_proof_on_oserror(tmp_path, monkeypatch, failure):
    path = tmp_path / "evidence.ndjson"
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    policy = ContinuousProofPolicy(minimum_seconds=60)
    for at in (1000, 1060):
        continuous_monitor.append_snapshot(path, _proof_sample(at))
    assert _assert_file_proof(evaluator, policy)["proven"]
    with monkeypatch.context() as patch:
        if failure == "delete":
            path.unlink()
        elif failure == "stat":
            original = Path.stat

            # LLM: 仅目标文件注入权限类OSError，其他stat不受影响。
            # 函数用途: 模拟证据文件stat失败。
            def broken_stat(target, *args, **kwargs):
                if target == path:
                    raise PermissionError("test stat failure")
                return original(target, *args, **kwargs)

            patch.setattr(Path, "stat", broken_stat)
        else:
            original = Path.open

            # LLM: 在原二进制读取边界注入故障，不伪造正常行或评估结果。
            # 函数用途: 模拟open或readline失败。
            @contextmanager
            def broken_open(target, mode="r", *args, **kwargs):
                if target == path and mode == "rb":
                    if failure == "open":
                        raise PermissionError("test open failure")
                    yield SimpleNamespace(seek=lambda _offset: None, readline=lambda: (_ for _ in ()).throw(OSError("test read failure")))
                else:
                    with original(target, mode, *args, **kwargs) as handle:
                        yield handle

            patch.setattr(Path, "open", broken_open)
        assert evaluator.evaluate(policy) == evaluate_continuous_proof([], policy)
        assert evaluator._committed_offset == 0 and evaluator._file_identity is None
        assert not evaluator._accumulator.has_rows
    path.write_text(json.dumps(_proof_sample(2000)) + "\n", encoding="utf-8")
    assert _assert_file_proof(evaluator, policy)["continuous_seconds"] == 0


# LLM: 新增类需要公开导出，三个既有函数仍按原公开入口调用。
# 函数用途: 防止文件评估器未接入模块导出。
def test_file_evaluator_is_exported():
    assert "ContinuousProofFileEvaluator" in continuous_monitor.__all__


# LLM: 使用真实证据追加与真实summary写入，替换外部初始化并仅跑两轮后停止，不启动生产长跑服务。
# 函数用途: 验证serve循环外只创建一次评估器且每轮实际调用增量入口。
def test_monitor_serve_reuses_one_file_evaluator(tmp_path, monkeypatch):
    from agent_py_agent.agent import continuous_monitor_entry as entry

    path = tmp_path / "evidence.ndjson"
    constructed, evaluated, sleeps = [], [], []
    rows = iter([_proof_sample(1000), _proof_sample(1060)])

    # LLM: 仍执行真实文件评估，只记录服务创建和评估调用，不能伪造summary。
    # 类用途: 观察serve持有同一评估器实例。
    class TrackedEvaluator(continuous_monitor.ContinuousProofFileEvaluator):
        # LLM: 每次创建记录路径后调用原构造器。
        # 函数用途: 统计评估器实例数。
        def __init__(self, evidence):
            constructed.append(evidence)
            super().__init__(evidence)

        # LLM: 每次评估保留真实返回值及原策略。
        # 函数用途: 统计循环对增量入口的调用。
        def evaluate(self, policy=None):
            evaluated.append(policy)
            return super().evaluate(policy)

    # LLM: 两轮真实本地循环后抛出可识别哨兵，避免常驻或等待真实时间。
    # 函数用途: 有界停止serve测试。
    def stop_after_two(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:
            raise StopIteration("test stop")

    monkeypatch.setattr(entry, "ContinuousProofFileEvaluator", TrackedEvaluator, raising=False)
    monkeypatch.setattr(entry, "ScaleRuntimeConfig", SimpleNamespace(from_env=lambda *_args: SimpleNamespace(is_scale=False, agent_home=str(tmp_path))))
    monkeypatch.setattr(entry, "configure_otel_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(entry, "discover_owner_homes", lambda _home: [])
    monkeypatch.setattr(entry, "build_snapshot", lambda _owners: next(rows))
    monkeypatch.setattr(entry.time, "sleep", stop_after_two)
    for name, value in {
        "EVIDENCE": str(path), "SAMPLE_SECONDS": "30", "PROOF_SECONDS": "3600",
        "MIN_SOURCES": "3", "MIN_SIGNATURES": "2", "MAX_GAP_SECONDS": "120", "MAX_SOURCE_STALE_SECONDS": "900",
    }.items():
        monkeypatch.setenv("CONTINUOUS_MONITOR_" + name, value)
    with pytest.raises(StopIteration, match="test stop"):
        entry.serve()
    assert constructed == [path] and len(evaluated) == 2
    assert json.loads(path.with_suffix(".summary.json").read_text()) == evaluate_continuous_proof(
        continuous_monitor.read_snapshots(path), ContinuousProofPolicy(minimum_seconds=3600),
    )


# LLM: 已读出无LF尾行后若文件又增长，不能把后到字节当独立完整行而提前推进游标。
# 函数用途: 验证评估期间补全尾行并追加新记录后，下一轮仍从原LF边界正确恢复。
@pytest.mark.parametrize("partial", [False, True])
def test_file_evaluator_stops_at_tail_when_writer_appends_during_read(tmp_path, monkeypatch, partial):
    path = tmp_path / "evidence.ndjson"
    continuous_monitor.append_snapshot(path, _proof_sample(1000))
    offset = path.stat().st_size
    raw = json.dumps(_proof_sample(1060)).encode()
    split = len(raw) // 2 if partial else len(raw)
    with path.open("ab") as handle:
        handle.write(raw[:split])
    original_open, appended = Path.open, []

    # LLM: 先真实读到尾行，再通过原文件句柄补齐并追加，明确控制竞争观察点。
    # 类用途: 在一次尾行读取后推进真实写入方。
    class AppendingReader(_EvidenceReadSpy):
        # LLM: 只触发一次追加，不修改返回给评估器的已读字节。
        # 函数用途: 模拟尾行读完与下次readline之间的新写入。
        def readline(self):
            line = self.handle.readline()
            if line and not line.endswith(b"\n") and not appended:
                appended.append(True)
                with original_open(path, "ab") as writer:
                    writer.write(raw[split:] + b"\n" + json.dumps(_proof_sample(1120)).encode() + b"\n")
            return line

    # LLM: 只替换目标文件的二进制读取，保留真实seek及追加操作。
    # 函数用途: 安装受控追加时序探针。
    @contextmanager
    def concurrent_open(target, mode="r", *args, **kwargs):
        with original_open(target, mode, *args, **kwargs) as handle:
            yield AppendingReader(handle, []) if target == path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", concurrent_open)
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    policy = ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=60)
    expected_rows = [_proof_sample(1000)] + ([] if partial else [_proof_sample(1060)])
    assert evaluator.evaluate(policy) == evaluate_continuous_proof(expected_rows, policy)
    assert appended and evaluator._committed_offset == offset
    assert _assert_file_proof(evaluator, policy)["continuous_seconds"] == 120


# LLM: 合法JSON中的坏时间/健康字段原本会抛错；重试时不得跳过已推进游标的坏行伪造成功。
# 函数用途: 验证评估异常后清缓存并按原公开接口继续传播数据错误。
@pytest.mark.parametrize("bad_fields,error", [({"observed_at": "invalid-time"}, ValueError), ({"watches": None}, TypeError)])
def test_file_evaluator_retries_semantically_invalid_rows(tmp_path, bad_fields, error):
    path = tmp_path / "evidence.ndjson"
    evaluator = continuous_monitor.ContinuousProofFileEvaluator(path)
    for at in (1000, 1060):
        continuous_monitor.append_snapshot(path, _proof_sample(at))
    _assert_file_proof(evaluator)
    continuous_monitor.append_snapshot(path, {**_proof_sample(1120), **bad_fields})
    continuous_monitor.append_snapshot(path, _proof_sample(1180))
    for _ in range(2):
        with pytest.raises(error):
            evaluate_continuous_proof(continuous_monitor.read_snapshots(path))
        with pytest.raises(error):
            evaluator.evaluate()
