# LLM: 仅生成本任务tmp下的合成owner并清理，计量原collector与冻结旧读取器；不读真实home或调用模型。
# 模块用途: 可复跑E11c约800线程/1GB和100审计分片/120MB的CPU、分配峰值与扫描量对照。
from __future__ import annotations

import argparse
import gc
import json
import shutil
import tempfile
import time
import tracemalloc
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.memory_store import curator_inputs
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig, MemoryCuratorState
from agent_py_agent.tests.support.curator_scan_cases import encode_row, make_messages, observe_reads
from agent_py_agent.tests.support.curator_scan_legacy import (
    LegacyMessageReader,
    legacy_collect_audit,
)


# LLM: 所有身份/路径/游标仅属于新建合成根；不用模型、状态提交器或真实runtime。
# 类用途: 保存一次性能试验的真实Store、原游标和生成元数据。
@dataclass
class BenchWorld:
    store: object
    audit: Path
    state: MemoryCuratorState
    metadata: dict


# LLM: 复用原MessageLogEntry编码，模板只用于同格式合成；最大约77MB模板生成不计入读取批次。
# 函数用途: 生成某档合成消息文件的原JSONL字节。
def _message_blob(sample, count: int) -> bytes:
    return b"".join(encode_row(replace(sample, message_id=f"msg-{i:05}", created_at=float(i + 2)).to_dict()) for i in range(count))


# LLM: 原Store先建一个样本，其余线程用原to_dict结构写入合成目录；没有生产权限或历史迁移。
# 函数用途: 生成800个线程，含一大文件、32个较大文件及其余小文件。
def _build_messages(root: Path):
    store, sample_tid, rows = make_messages(root, count=1, content_chars=5000)
    sample_thread = store.threads.load(sample_tid)
    sample = replace(rows[0], thread_id="thread-template")
    templates = {count: _message_blob(sample, count) for count in (15_300, 2500, 180)}
    cursors = {}
    for index in range(800):
        count = 15_300 if index == 0 else (2500 if index < 33 else 180)
        tid = sample_tid if index == 0 else f"thread-{index:04}"
        thread = replace(sample_thread, thread_id=tid, updated_at=float(index + 3))
        store.storage.thread_path(tid).write_text(json.dumps(thread.to_dict()), encoding="utf-8")
        store.storage.message_path(tid).write_bytes(templates[count].replace(b"thread-template", tid.encode()))
        cursors[tid] = f"msg-{count - 1:05}"
    return store, cursors


# LLM: 审计日分片均为合成工具事件，原字段/UTF-8/LF格式，与真实工具执行账无关。
# 函数用途: 写100份约1.2MB的合成审计历史。
def _build_audit(root: Path) -> str:
    root.mkdir()
    template = b"".join(encode_row({"event_id": f"event-TPL-{i:04}", "event_type": "tool_call", "status": "ok",
                                    "tool_name": "read_file", "created_at": 1.0, "preview": "x" * 1060}) for i in range(1000))
    for day in range(100):
        (root / f"slice-{day:03}.jsonl").write_bytes(template.replace(b"TPL", f"{day:03}".encode()))
    return "event-099-0999"


# LLM: 文件大小来自实际stat，数字不手填或估算；state只在试验进程内构造、不落盘。
# 函数用途: 建立可由原collector真实读取的合成规模与游标。
def _build_world(root: Path) -> BenchWorld:
    store, cursors = _build_messages(root)
    audit = root / "audit"
    cursor = _build_audit(audit)
    sizes = [path.stat().st_size for path in store.storage.messages_dir.glob("*.jsonl")]
    audit_sizes = [path.stat().st_size for path in audit.glob("*.jsonl")]
    metadata = {"threads": len(sizes), "message_bytes": sum(sizes), "max_message_bytes": max(sizes),
                "audit_files": len(audit_sizes), "audit_bytes": sum(audit_sizes)}
    state = MemoryCuratorState(per_thread_cursors=cursors, last_processed_audit_event_id=cursor)
    return BenchWorld(store, audit, state, metadata)


# LLM: 旧臂只替换冻结旧读取器，collector的投影/预算仍是同一原入口，不模拟统计值。
# 函数用途: 执行一个合成批次并返回实际类型化结果。
def _collect(world: BenchWorld, old: bool):
    config = MemoryCuratorConfig(batch_message_limit=80, max_input_chars=40_000)
    store = SimpleNamespace(threads=world.store.threads, messages=LegacyMessageReader(world.store)) if old else world.store
    reader = legacy_collect_audit if old else curator_inputs._collect_audit
    with patch.object(curator_inputs, "_collect_audit", reader):
        return curator_inputs.collect_curator_inputs(conversation_store=store, audit_dir=world.audit, state=world.state, config=config)


# LLM: 计时采用本进程CPU，峰值为本批新分配；读取字节/打开次数来自真实IO包装，不是估计或mock返回值。
# 函数用途: 为一个原collector批次记录CPU/墙钟/分配峰值及扫描量。
def _measure(world: BenchWorld, old: bool):
    observed = list(world.store.storage.messages_dir.glob("*.jsonl")) + list(world.audit.glob("*.jsonl"))
    monkeypatch = pytest.MonkeyPatch()
    counts = observe_reads(monkeypatch, observed)
    gc.collect()
    tracemalloc.start()
    cpu, wall = time.process_time(), time.perf_counter()
    try:
        batch = _collect(world, old)
        cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        monkeypatch.undo()
    metrics = {"cpu_seconds": cpu, "wall_seconds": wall, "tracemalloc_peak_bytes": peak,
               "retained_new_bytes": retained, "read_bytes": counts.bytes, "opens": len(counts.opens),
               "message_opens": sum(Path(path).parent.name == "messages" for path in counts.opens),
               "messages": len(batch.messages), "audit_events": len(batch.audit_events), "load_errors": len(batch.load_errors)}
    return metrics, batch


# LLM: 只经原append写一个测试消息，其余新增只在合成审计分片；持久cursor没有提交。
# 函数用途: 给热缓存批次制造一个有新消息线程，其余799个保持未变。
def _append_new(world: BenchWorld) -> None:
    tid = "thread-0799"
    world.store.messages.append({"thread_id": tid, "role": "user", "content": "唯一新增合成消息", "now": 10000.0})
    with (world.audit / "slice-099.jsonl").open("ab") as handle:
        handle.write(encode_row({"event_id": "audit-new", "event_type": "tool_call", "preview": "唯一新增合成审计"}))


# LLM: 初始冷扫建立提示，后续原state/输入分别与旧臂逐对象比较；不把热缓存成绩冒称冷启动或生产结果。
# 函数用途: 记录冷/热/混合批次，证明选择等价并量化推进后的稳态。
def _run_world(world: BenchWorld) -> dict:
    result = dict(world.metadata)
    result["new_cold"], cold = _measure(world, False)
    result["new_unchanged"], unchanged = _measure(world, False)
    assert cold == unchanged and not unchanged.messages and not unchanged.audit_events and not unchanged.load_errors
    _append_new(world)
    result["old_mixed"], old_batch = _measure(world, True)
    result["new_mixed"], new_batch = _measure(world, False)
    assert old_batch == new_batch and not new_batch.load_errors
    assert len(new_batch.messages) == len(new_batch.audit_events) == 1
    world.state = replace(world.state, per_thread_cursors={**world.state.per_thread_cursors, "thread-0799": new_batch.messages[-1].message_id},
                          last_processed_audit_event_id=new_batch.audit_events[-1].event_id)
    result["new_advanced"], advanced = _measure(world, False)
    assert not advanced.messages and not advanced.audit_events and not advanced.load_errors
    result["old_vs_new_equal"] = True
    result["cpu_reduction_percent"] = 100 * (1 - result["new_mixed"]["cpu_seconds"] / result["old_mixed"]["cpu_seconds"])
    return result


# LLM: TemporaryDirectory只删除本次新建合成根；输出仅含元数据，空间不足不删除任何既有目录。
# 函数用途: 在当前任务tmp生成、跑对照、清理，并保存真实计量结果。
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("tmp/mc2-e11c/performance.json"))
    output = parser.parse_args().output
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < 2_000_000_000:
        raise SystemExit("合成试验需要至少2GB空闲空间，未删除任何既有文件")
    with tempfile.TemporaryDirectory(prefix="corpus-", dir=output.parent) as temporary:
        corpus = Path(temporary)
        result = _run_world(_build_world(corpus))
    result["corpus_removed"] = not corpus.exists()
    assert result["corpus_removed"]
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
