"""global_index 只追加索引的内部压缩：读取结果逐条不变、只按 LF 切行、峰值内存有界。

背景：四份 jsonl 只追加，生产合计 0.93 GB。压缩只在**索引文件内部**按 key 保留最后一行，
不读任何权威文件。读取方 `home_indexes._latest_unique_refs` 是「先 reversed，再遇到某 key
首次出现就取」，所以只要按原顺序、原字节丢掉"非最后一行"的行，读取结果按构造逐条相同。

本文件把复审的 C1–C3 转成回归，另加随机等价与内存上限两条。
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

from agent_py_agent.agent.io import append_jsonl
from agent_py_agent.agent.user_space.home_index_compact import (
    COMPACT_COOLDOWN_SECONDS,
    COMPACT_MIN_BYTES,
    compact_index_file,
    compact_index_if_due,
    key_fields_for,
    should_compact,
)
from agent_py_agent.agent.user_space.home_indexes import _latest_unique_refs

TASK_KEY = ("owner_id", "task_id")
RUN_KEY = ("owner_id", "run_id")
OWNER_KEY = ("owner_id",)
AGENT_KEY = ("owner_id", "agent_id")


def _task(task: str, status: str = "RUNNING", title: str = "t") -> dict:
    return {"schema_version": "global-task-index.v1", "owner_id": "o", "task_id": task,
            "task_path": f"/x/{task}", "status": status, "title": title,
            "updated_at": "2026-09-28T00:00:00+00:00"}


def _ids(rows: list[dict], field: str) -> list[str]:
    return [row[field] for row in rows]


# ------------------------------------------------------------------ C1：行序不能翻转


def test_c1_reader_order_preserved(tmp_path) -> None:
    """三条互不相同的记录：压缩前后读取顺序与 limit 结果都必须相同（C1）。"""
    path = tmp_path / "active_tasks.jsonl"
    for task in ("A", "B", "C"):
        append_jsonl(path, _task(task), sort_keys=True)

    before = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    before_top1 = _latest_unique_refs(path, key_fields=TASK_KEY, limit=1)
    compact_index_file(path)
    after = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    after_top1 = _latest_unique_refs(path, key_fields=TASK_KEY, limit=1)

    assert _ids(before, "task_id") == ["C", "B", "A"]
    assert _ids(after, "task_id") == _ids(before, "task_id")  # 顺序不变
    assert _ids(after_top1, "task_id") == _ids(before_top1, "task_id") == ["C"]


def test_c1b_order_when_earliest_key_is_not_latest_update(tmp_path) -> None:
    """专门构造「最早出现的 key」≠「最近更新的 key」，把 C1 的盲区钉死。"""
    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("OLD", status="RUNNING"), sort_keys=True)
    append_jsonl(path, _task("NEW", status="RUNNING"), sort_keys=True)
    append_jsonl(path, _task("OLD", status="DONE"), sort_keys=True)  # OLD 最后更新

    before = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert _ids(before, "task_id") == ["OLD", "NEW"]  # OLD 最后出现 → 排最前
    assert before[0]["status"] == "DONE"

    compact_index_file(path)
    after = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert _ids(after, "task_id") == ["OLD", "NEW"]
    assert after[0]["status"] == "DONE"


# ------------------------------------------------------------------ C2：key_fields 不能一刀切


def test_c2_run_key_excludes_task_id(tmp_path) -> None:
    """runs 的读取键是 (owner_id, run_id)：压缩不能把 task_id 也算进 key（C2）。"""
    path = tmp_path / "active_runs.jsonl"
    base = {"schema_version": "global-run-index.v1", "owner_id": "o", "run_id": "r1",
            "run_path": "/x/r1", "updated_at": "2026-09-28T00:00:00+00:00"}
    append_jsonl(path, {**base, "task_id": "", "status": "RUNNING"}, sort_keys=True)
    append_jsonl(path, {**base, "task_id": "T1", "status": "DONE"}, sort_keys=True)

    before = _latest_unique_refs(path, key_fields=RUN_KEY, limit=0)
    compact_index_file(path)
    after = _latest_unique_refs(path, key_fields=RUN_KEY, limit=0)

    assert before[0]["status"] == "DONE"
    assert after[0]["status"] == "DONE"  # 旧状态不复活
    assert len(after) == 1


def test_key_fields_per_file_are_not_uniform() -> None:
    """四份索引各有自己的 key_fields（不能用一个统一 tuple 顶替）。"""
    assert key_fields_for(Path("owners.jsonl")) == OWNER_KEY
    assert key_fields_for(Path("active_tasks.jsonl")) == TASK_KEY
    assert key_fields_for(Path("active_runs.jsonl")) == RUN_KEY
    assert key_fields_for(Path("active_agents.jsonl")) == AGENT_KEY


# ------------------------------------------------------------------ C3：只按 LF 切行


def test_c3_record_with_line_separator_survives(tmp_path) -> None:
    """标题里的 U+2028/U+2029/U+0085 不能被当记录边界，记录不能丢（C3）。"""
    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    for sep in ("\u2028", "\u2029", "\u0085"):
        append_jsonl(path, _task(f"B{ord(sep)}", title=f"第一行{sep}第二行"), sort_keys=True)

    before = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    compact_index_file(path)
    after = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert _ids(after, "task_id") == _ids(before, "task_id")
    assert len(before) == len(after) == 4


# ------------------------------------------------------------------ 随机等价（四类文件）


def test_random_equivalence_across_all_four_indexes(tmp_path) -> None:
    """随机写四类索引，压缩前后 latest_* 结果逐条相同（含 limit=0 与小 limit）。"""
    rng = random.Random(20260929)
    cases = [
        ("owners.jsonl", OWNER_KEY, lambda i: {"schema_version": "global-owner-index.v1", "owner_id": f"o{i % 5}",
                                               "owner_path": f"/h/{i}", "display_name": f"n{i}"}),
        ("active_tasks.jsonl", TASK_KEY, lambda i: _task(f"t{i % 5}", status=rng.choice(["RUNNING", "DONE"]))),
        ("active_runs.jsonl", RUN_KEY, lambda i: {"schema_version": "global-run-index.v1", "owner_id": "o",
                                                  "run_id": f"r{i % 5}", "task_id": f"t{i % 3}",
                                                  "run_path": f"/r/{i}", "status": rng.choice(["RUNNING", "DONE"])}),
        ("active_agents.jsonl", AGENT_KEY, lambda i: {"schema_version": "global-agent-index.v1", "owner_id": "o",
                                                      "agent_id": f"a{i % 5}", "task_id": f"t{i % 3}",
                                                      "run_path": f"/a/{i}", "status": rng.choice(["RUNNING", "DONE"])}),
    ]
    for name, key_fields, make in cases:
        path = tmp_path / name
        for i in range(60):
            append_jsonl(path, make(i), sort_keys=True)
        for limit in (0, 1, 3, 20):
            before = _latest_unique_refs(path, key_fields=key_fields, limit=limit)
            compact_index_file(path)
            after = _latest_unique_refs(path, key_fields=key_fields, limit=limit)
            assert after == before, (name, limit)


def test_random_equivalence_survives_repeated_compaction(tmp_path) -> None:
    """反复压缩 + 追加：等价性保持（幂等，且新追加的行赢得最新）。"""
    path = tmp_path / "active_tasks.jsonl"
    rng = random.Random(7)
    seen_last: dict[str, str] = {}
    for round_no in range(6):
        for _ in range(12):
            task = f"t{rng.randrange(5)}"
            status = rng.choice(["RUNNING", "DONE"])
            append_jsonl(path, _task(task, status=status), sort_keys=True)
            seen_last[task] = status
        compact_index_file(path)
        rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
        assert {r["task_id"]: r["status"] for r in rows} == seen_last, round_no


# ------------------------------------------------------------------ 并发：追加不能丢


def test_append_during_compaction_is_kept(tmp_path, monkeypatch) -> None:
    """前缀压缩与替换之间新追加的行必须原样接上、不能被吞掉。"""
    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    append_jsonl(path, _task("B"), sort_keys=True)

    import agent_py_agent.agent.user_space.home_index_compact as mod
    from agent_py_agent.agent.common import json_io

    real_scan = mod._scan_last_line_per_key
    injected = {"done": False}

    def scan_then_inject(*args, **kwargs):
        # 关键：必须在前缀快照**之后**才注入，否则新行会被算进 prefix_len，
        # _copy_tail 就无事可做——那条断言会假绿（MH4 曾经就是这样存活的）。
        result = real_scan(*args, **kwargs)
        if not injected["done"]:
            injected["done"] = True
            append_jsonl(path, _task("LATE"), sort_keys=True)
        return result

    monkeypatch.setattr(mod, "_scan_last_line_per_key", scan_then_inject)
    compact_index_file(path)

    rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert "LATE" in _ids(rows, "task_id"), "追加的行被吞掉了"
    assert set(_ids(rows, "task_id")) == {"A", "B", "LATE"}


# ------------------------------------------------------------------ 阈值与冷却


def test_crlf_line_is_still_a_valid_record(tmp_path) -> None:
    """CRLF 行尾的记录必须被认出（只按 LF 切，不能把 \\r 留在 JSON 里当解析失败）。"""
    path = tmp_path / "active_tasks.jsonl"
    with path.open("wb") as handle:
        handle.write(json.dumps(_task("A"), sort_keys=True).encode("utf-8") + b"\r\n")
        handle.write(json.dumps(_task("B"), sort_keys=True).encode("utf-8") + b"\r\n")

    result = compact_index_file(path)
    assert result.bad_lines == 0, "CRLF 行被当成坏行了"
    rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert set(_ids(rows, "task_id")) == {"A", "B"}


def test_rebuild_replacing_file_aborts_compaction(tmp_path, monkeypatch) -> None:
    """压缩窗口里 rebuild 整体换掉文件（inode 变）时必须放弃，不能把陈旧前缀写回。"""
    import agent_py_agent.agent.user_space.home_index_compact as mod

    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    append_jsonl(path, _task("B"), sort_keys=True)

    real_scan = mod._scan_last_line_per_key
    swapped = {"done": False}

    def scan_then_swap(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        if not swapped["done"]:
            swapped["done"] = True
            # 模拟 rebuild --apply：换一个新文件（新 inode），内容完全不同。
            replacement = path.with_name("rebuild.tmp")
            replacement.write_bytes(json.dumps(_task("REBUILT"), sort_keys=True).encode() + b"\n")
            replacement.replace(path)
        return result

    monkeypatch.setattr(mod, "_scan_last_line_per_key", scan_then_swap)
    result = compact_index_file(path)

    assert result.compacted is False
    assert result.reason == "identity_changed"
    rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert _ids(rows, "task_id") == ["REBUILT"], "陈旧前缀把 rebuild 的结果盖掉了"


def test_bad_lines_are_counted_and_reported(tmp_path) -> None:
    """坏行必须计数上报，不能静默当成正常丢弃。"""
    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    with path.open("ab") as handle:
        handle.write(b"{ this is not json\n")
        handle.write(b"\n")
    append_jsonl(path, _task("B"), sort_keys=True)

    result = compact_index_file(path)
    assert result.bad_lines == 2, f"坏行计数应为 2（非 JSON + 空行），实际 {result.bad_lines}"
    rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert set(_ids(rows, "task_id")) == {"A", "B"}


def test_non_object_json_line_is_bad_and_does_not_shadow_records(tmp_path) -> None:
    """合法 JSON 但不是对象（数组/标量）的行必须算坏行，且不能变成一个 key 顶掉真记录。"""
    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    with path.open("ab") as handle:
        handle.write(b"[1, 2, 3]\n")
        handle.write(b"42\n")
        handle.write(b'"just a string"\n')
    append_jsonl(path, _task("B"), sort_keys=True)

    result = compact_index_file(path)
    assert result.bad_lines == 3, f"三条非对象行都该算坏行，实际 {result.bad_lines}"
    rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert set(_ids(rows, "task_id")) == {"A", "B"}


def test_missing_key_field_normalizes_to_empty_string(tmp_path) -> None:
    """缺字段的行 key 必须是空串（不是 None 字面量），否则同一实体的行会分成两个 key。"""
    import agent_py_agent.agent.user_space.home_index_compact as mod

    assert mod._record_key(b'{"owner_id": "o"}', TASK_KEY) == ("o", "")
    assert mod._record_key(b'{"task_id": "T"}', TASK_KEY) == ("", "T")


# ------------------------------------------------ 38 复审：锁内再核身份 + 键定义单一来源


def test_rebuild_between_identity_check_and_replace(tmp_path, monkeypatch) -> None:
    """rebuild 换文件落在「锁外核对通过 → 拿到锁」之间时，必须放弃压缩（38 的探针转正）。

    上一版的 `test_rebuild_replacing_file_aborts_compaction` 把换文件注入在**扫描阶段**，
    只覆盖了锁外核对之前的窗口；锁外核对通过之后、拿锁之前那一小段没人管。
    """
    import agent_py_agent.agent.user_space.home_index_compact as mod

    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    append_jsonl(path, _task("A"), sort_keys=True)  # 重复 key，压缩会丢一行
    append_jsonl(path, _task("B"), sort_keys=True)

    real_check = mod._still_same_file
    swapped = {"done": False}

    def check_then_swap(target, stat0, prefix_len):
        ok = real_check(target, stat0, prefix_len)
        # 只在前缀快照之后的**第一次**核对里换文件：那时 tmp 已经写好、锁还没拿。
        if ok and not swapped["done"]:
            swapped["done"] = True
            replacement = path.with_name("rebuild.tmp")
            replacement.write_bytes(json.dumps(_task("REBUILT"), sort_keys=True).encode() + b"\n")
            replacement.replace(path)
        return ok

    monkeypatch.setattr(mod, "_still_same_file", check_then_swap)
    result = compact_index_file(path)

    assert result.compacted is False
    assert result.reason == "identity_changed"
    rows = _latest_unique_refs(path, key_fields=TASK_KEY, limit=0)
    assert _ids(rows, "task_id") == ["REBUILT"], "rebuild 的结果被陈旧前缀盖掉了"


def test_key_fields_come_from_home_indexes_spec() -> None:
    """压缩用的键必须与 home_indexes 的权威注册表**逐一相等**，不能各抄一份。"""
    from agent_py_agent.agent.user_space.home_indexes import (
        INDEX_KEY_FIELDS_BY_FILE,
        key_fields_for_index_file,
    )

    expected = {
        "owners.jsonl": ("owner_id",),
        "active_tasks.jsonl": ("owner_id", "task_id"),
        "active_runs.jsonl": ("owner_id", "run_id"),
        "active_agents.jsonl": ("owner_id", "agent_id"),
    }
    assert expected == INDEX_KEY_FIELDS_BY_FILE
    # key_fields_for 必须真的走注册表，而不是自己那份表。
    for name, fields in expected.items():
        assert key_fields_for(Path(name)) == fields
    assert key_fields_for_index_file("active_tasks.jsonl") == ("owner_id", "task_id")


def test_reader_side_key_fields_equal_registry(tmp_path, monkeypatch) -> None:
    """**读取侧**的键也必须等于注册表（9b 的 MH12 就是从这里漏过去的）。

    压缩的正确性取决于「压缩用的键」和「读取用的键」一致：读取键一旦变宽，
    压缩会把读取侧视为不同的行合并掉，而没有测试会发现。
    这里从真实读取入口反查它们实际传下去的键字段。
    """
    import agent_py_agent.agent.user_space.home_indexes as idx
    from agent_py_agent.agent.user_space.home_indexes import INDEX_KEY_FIELDS_BY_FILE

    class _Home:
        global_index_owners_jsonl = tmp_path / "owners.jsonl"
        global_index_active_tasks_jsonl = tmp_path / "active_tasks.jsonl"
        global_index_active_runs_jsonl = tmp_path / "active_runs.jsonl"
        global_index_active_agents_jsonl = tmp_path / "active_agents.jsonl"

    home = _Home()
    captured: list[tuple[str, tuple[str, ...]]] = []
    real_report = idx._latest_unique_refs_report

    def spy(path, *, key_fields, limit, context):
        captured.append((Path(path).name, tuple(key_fields)))
        return real_report(path, key_fields=key_fields, limit=limit, context=context)

    monkeypatch.setattr(idx, "_latest_unique_refs_report", spy)
    idx.latest_owner_refs_report(home)
    idx.latest_task_refs_report(home)
    idx.latest_run_refs_report(home)
    idx.latest_agent_refs_report(home)

    assert captured, "没有捕获到任何读取调用"
    for name, fields in captured:
        assert fields == INDEX_KEY_FIELDS_BY_FILE[name], f"{name} 的读取键与注册表不一致"

    # dangling 检查用的 _IndexSpec 三处也必须一致。
    specs = idx._dangling_index_specs(home)
    assert specs, "没有取到 dangling spec"
    for spec in specs:
        assert spec.key_fields == INDEX_KEY_FIELDS_BY_FILE[Path(spec.path).name]


def test_consume_reason_is_not_reported_as_failure(tmp_path) -> None:
    """没到期（below_min_bytes 等）不能被算成"压失败"，否则维护状态天天报假失败。"""
    from agent_py_agent.agent.user_space.owner_maintenance import _COMPACT_NOT_ATTEMPTED_REASONS

    assert "below_min_bytes" in _COMPACT_NOT_ATTEMPTED_REASONS
    assert "below_growth_ratio" in _COMPACT_NOT_ATTEMPTED_REASONS
    assert "in_cooldown" in _COMPACT_NOT_ATTEMPTED_REASONS
    assert "missing" in _COMPACT_NOT_ATTEMPTED_REASONS
    # 真正"试了但失败"的原因绝不能进这个集合。
    assert "io_error" not in _COMPACT_NOT_ATTEMPTED_REASONS
    assert "identity_changed" not in _COMPACT_NOT_ATTEMPTED_REASONS


def test_below_threshold_does_not_touch_file(tmp_path) -> None:
    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    before_text = path.read_text(encoding="utf-8")
    before_mtime = path.stat().st_mtime_ns

    assert should_compact(path) == "below_min_bytes"
    result = compact_index_if_due(path)
    assert result.compacted is False
    assert result.reason == "below_min_bytes"
    assert path.read_text(encoding="utf-8") == before_text
    assert path.stat().st_mtime_ns == before_mtime


def test_growth_below_double_does_not_trigger(tmp_path) -> None:
    path = tmp_path / "active_tasks.jsonl"
    path.write_text("x" * (COMPACT_MIN_BYTES + 1), encoding="utf-8")
    # 模拟"上次压缩后大小"已记录
    import agent_py_agent.agent.user_space.home_index_compact as mod

    mod._LAST_COMPACT_BYTES[str(path)] = path.stat().st_size
    mod._LAST_COMPACT_AT[str(path)] = 0.0
    path.write_text("x" * int(COMPACT_MIN_BYTES * 1.5), encoding="utf-8")
    assert should_compact(path, now=COMPACT_COOLDOWN_SECONDS * 10) == "below_growth_ratio"


def test_compaction_records_size_for_next_decision(tmp_path) -> None:
    """压缩必须记下压缩后大小，作为下一次 2 倍门槛的依据。"""
    import agent_py_agent.agent.user_space.home_index_compact as mod

    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    append_jsonl(path, _task("A", status="DONE"), sort_keys=True)
    compact_index_file(path)
    assert mod._LAST_COMPACT_BYTES[str(path)] == path.stat().st_size
    assert mod._LAST_COMPACT_AT[str(path)] > 0


def test_cooldown_blocks_repeat_trigger(tmp_path) -> None:
    import agent_py_agent.agent.user_space.home_index_compact as mod

    path = tmp_path / "active_tasks.jsonl"
    path.write_text("x" * (COMPACT_MIN_BYTES + 1), encoding="utf-8")
    mod._LAST_COMPACT_BYTES[str(path)] = 0
    mod._LAST_COMPACT_AT[str(path)] = 1_000.0
    assert should_compact(path, now=1_000.0 + COMPACT_COOLDOWN_SECONDS - 1) == "in_cooldown"
    assert should_compact(path, now=1_000.0 + COMPACT_COOLDOWN_SECONDS + 1) == "due"


# ------------------------------------------------------------------ sidecar：重启后仍守冷却


def test_compact_state_survives_restart(tmp_path) -> None:
    """压缩状态持久化到 sidecar：清空进程内记录后仍能读到（否则重启后第一次 tick 必压）。"""
    import agent_py_agent.agent.user_space.home_index_compact as mod

    path = tmp_path / "active_tasks.jsonl"
    append_jsonl(path, _task("A"), sort_keys=True)
    append_jsonl(path, _task("A", status="DONE"), sort_keys=True)
    compact_index_file(path)
    recorded = mod._LAST_COMPACT_BYTES[str(path)]
    assert recorded > 0

    # 模拟进程重启：清空内存，再从 sidecar 载入
    mod._LAST_COMPACT_BYTES.clear()
    mod._LAST_COMPACT_AT.clear()
    mod.load_compact_state(path)
    assert mod._LAST_COMPACT_BYTES[str(path)] == recorded
    assert mod._LAST_COMPACT_AT[str(path)] > 0


# ------------------------------------------------------------------ 维护入口：真实链路


def test_maintenance_compacts_oversized_index(tmp_path) -> None:
    """经真实入口 run_owner_retention_if_due 压缩全局索引（不是只测模块函数）。"""
    from agent_py_agent.agent.io import append_jsonl as _append
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path / "home")
    tasks_path = home.global_index_active_tasks_jsonl
    tasks_path.parent.mkdir(parents=True, exist_ok=True)

    # 造一个超过下限、且有大量重复 key 的索引
    filler = "y" * 400
    for i in range(4000):
        _append(tasks_path, _task(f"t{i % 200}", title=filler), sort_keys=True)
    before_size = tasks_path.stat().st_size

    # 把下限临时压低，让这份文件算"超标"
    import agent_py_agent.agent.user_space.home_index_compact as mod

    original_min = mod.COMPACT_MIN_BYTES
    mod.COMPACT_MIN_BYTES = 1
    try:
        policy_path = home.owner_retention_json
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy.update({"maintenance_interval_seconds": 100})
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        marker_before = _latest_unique_refs(tasks_path, key_fields=TASK_KEY, limit=0)
        run_owner_retention_if_due(home, now=200_000)
        marker_after = _latest_unique_refs(tasks_path, key_fields=TASK_KEY, limit=0)
    finally:
        mod.COMPACT_MIN_BYTES = original_min

    assert tasks_path.stat().st_size < before_size, "维护没有压缩这份索引"
    assert marker_after == marker_before, "压缩改变了读取结果"

    state = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert state.get("indexes_compacted"), "维护状态没有记录压缩摘要"


# ------------------------------------------------------------------ 内存上限（子进程实测）


def test_peak_memory_bounded(tmp_path) -> None:
    """约 24 MB 的索引：峰值增量必须远小于文件大小（第一版是 ~6.5 倍）。"""
    path = tmp_path / "active_tasks.jsonl"
    line_tail = "x" * 180
    with path.open("w", encoding="utf-8") as handle:
        for index in range(60_000):
            record = _task(f"t{index % 8_000}", title=line_tail)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    size = path.stat().st_size
    # 本仓的 agent_py_agent 是 namespace package：子进程只要 cwd 不在工作树根，
    # 就会落到别处的 editable 安装（本机是 6 月那份旧源码树），静默测到旧代码。
    # 因此必须显式把工作树根塞进 PYTHONPATH，不能只靠 cwd。
    pkg_root = str(Path(__file__).resolve().parents[2])

    script = textwrap.dedent(
        f"""
        import resource, sys, time
        from pathlib import Path
        from agent_py_agent.agent.user_space.home_index_compact import compact_index_file
        base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result = compact_index_file(Path({str(path)!r}))
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        scale = 1 if sys.platform == 'darwin' else 1024
        print('size_mb=%.1f peak_delta_mb=%.1f after_mb=%.1f' % (
            {size} / 1e6, (peak - base) * scale / 1e6, result.after_bytes / 1e6))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=300, cwd=pkg_root, env=env,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    fields = dict(part.split("=") for part in proc.stdout.split())
    peak_delta = float(fields["peak_delta_mb"])
    size_mb = float(fields["size_mb"])
    assert peak_delta < size_mb, f"峰值 {peak_delta:.1f} MB 超过文件大小 {size_mb:.1f} MB（第一版是 ~6.5 倍）"
