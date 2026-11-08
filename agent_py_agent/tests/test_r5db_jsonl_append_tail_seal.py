# LLM: RES D-5 回归：崩溃留下的无 LF 残行不能与下一次锁内 JSONL 追加合成坏行；完整对象补 LF，
#   半行私有留档后截回最后 LF；按文件大小预记偏移的调用方（调度历史、用量账）先封再量。
#   插话队列保留 v14-R1 严格修复合同（残行报错留给运维），通用封口不得抢在其严格读之前改写。
# 模块用途: 钉住通用残尾封口的三种结局、符号链接不跟随、偏移型调用方崩溃后可自愈，及插话队列合同不变。

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.json_io import append_private_jsonl_records_unlocked
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.scheduler.history_index import SchedulerHistoryIndex


def _torn(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.torn-*"))


# 函数用途: 半行残尾被私有留档并截掉，新记录独占一行，旧完整行字节不变。
def test_partial_tail_is_quarantined_before_append(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": ')
    append_private_jsonl_records_unlocked(path, [{"c": 3}])
    assert path.read_bytes() == b'{"a": 1}\n{"c": 3}\n'
    [sidecar] = _torn(path)
    assert sidecar.read_bytes() == b'{"b": '
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


# 函数用途: 残尾本身是完整对象时只补换行，不丢这条已写完的记录。
def test_complete_object_tail_only_gets_newline(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}')
    append_private_jsonl_records_unlocked(path, [{"c": 3}])
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert _torn(path) == []


# 函数用途: 正常、空、缺失文件与符号链接都不被封口改动。
def test_clean_missing_and_symlink_paths_are_untouched(tmp_path: Path) -> None:
    seal = json_io.seal_private_jsonl_tail_unlocked
    clean = tmp_path / "clean.jsonl"
    clean.write_bytes(b'{"a": 1}\n')
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    target = tmp_path / "target.jsonl"
    target.write_bytes(b'{"a": 1}\n{"b": ')
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    assert [seal(clean), seal(empty), seal(tmp_path / "missing.jsonl"), seal(link)] == ["", "", "", ""]
    assert clean.read_bytes() == b'{"a": 1}\n' and empty.read_bytes() == b""
    assert target.read_bytes() == b'{"a": 1}\n{"b": ' and _torn(target) == [] and _torn(link) == []
    append_private_jsonl_records_unlocked(tmp_path / "new.jsonl", [{"a": 1}])
    assert (tmp_path / "new.jsonl").read_bytes() == b'{"a": 1}\n'


def _parse_run(raw, *, terminal=True):
    if not isinstance(raw, dict) or not raw.get("run_id"):
        return None, "SCHEDULER_HISTORY_ROW_INVALID"
    return raw, None


# 函数用途: 调度历史崩溃残尾后再追加，回填完成后不留永久坏行错误且新 run 可查。
def test_scheduler_history_heals_torn_tail(tmp_path: Path) -> None:
    source = tmp_path / "history.jsonl"
    index = SchedulerHistoryIndex(source, {"owner": "o"}, _parse_run)
    index.append([{"run_id": "r1", "job_id": "j", "ended_at": 1.0}])
    with source.open("ab") as handle:
        handle.write(b'{"run_id": "r2", "job_')
    index.append([{"run_id": "r3", "job_id": "j", "ended_at": 3.0}])
    for _ in range(8):
        if index.backfill()["complete"]:
            break
    rows, errors = index.read(job_id="j")
    assert errors == []
    assert [row["run_id"] for row in rows] == ["r3", "r1"]


# 函数用途: 插话队列残尾仍按 v14-R1 合同报修复错误，残行字节原样保留且不产生封口旁车。
def test_guidance_queue_keeps_strict_repair_contract(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.guidance.append_once(
        {"target_type": "request", "target_id": "req", "message": "one", "metadata": {}},
        dedupe_key="t/one",
    )
    queue = store.storage.guidance_path("request", "req")
    with queue.open("ab") as handle:
        handle.write(b'{"guidance_id": "torn')
    before = queue.read_bytes()
    with pytest.raises(DataCorruptionError):
        store.guidance.append_once(
            {"target_type": "request", "target_id": "req", "message": "two", "metadata": {}},
            dedupe_key="t/two",
        )
    assert queue.read_bytes() == before and not _torn(queue)
