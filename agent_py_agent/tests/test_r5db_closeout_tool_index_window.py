# LLM: r5 D-3 回归：回合收尾登记工具产物引用时不再整读 owner 全部历史索引；只从回合起点标记读主索引，
#   未在本回合写过的 run/task 索引按窗口起点 mtime 跳过；inode 换新、标记未对齐、旧包无标记都退回整读；
#   Compact 续接片重写同名快照（含跨零点）时继承首片窗口，旧快照不可读则整读，另一 run 的旧快照不继承。
# 模块用途: 以逐行解码计数钉住收尾读取量与回合内新增行成正比，并钉住各退化分支仍能找全引用。

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent_py_agent.agent.common import tool_index_stream
from agent_py_agent.agent.common.tool_output_paths import tool_output_root
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

SCOPE = {"request_id": "req-now", "run_id": "run-now", "task_id": "task-now"}
HISTORY_ROWS = 400


def _externalize(root: Path, scope: dict[str, str], call_id: str) -> None:
    externalize_tool_output_record(ExternalizeToolOutputRequest(
        root=root, tool="read_file", call_id=call_id, output="large output\n" + "x" * 2_000,
        ok=True, min_chars=1, **scope,
    ))


def _history(root: Path, rows: int = HISTORY_ROWS) -> Path:
    _externalize(root, {"request_id": "req-old", "run_id": "run-old", "task_id": "task-old"}, "old-1")
    index = tool_output_root(root) / "index.jsonl"
    line = index.read_bytes()
    index.write_bytes(line * rows)
    return index


def _mark(index: Path, *, size_delta: int = 0) -> dict[str, list[int]]:
    info = index.stat()
    return {str(index.resolve()): [info.st_dev, info.st_ino, info.st_size + size_delta]}


def _bundle(tmp_path: Path, root: Path, marks: dict | None) -> Path:
    bundle = build_main_context_bundle(MainContextBundleRequest(
        root=root, home_paths=ensure_my_agent_home(tmp_path / "home"), user_prompt="继续", save=True, **SCOPE,
    ))
    path = Path(bundle.json_path)
    if marks is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["tool_index_marks"] = marks
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _old_mtime(path: Path, bundle: Path) -> None:
    stamp = bundle.stat().st_mtime - 3600
    os.utime(path, (stamp, stamp))


@pytest.fixture
def decoded(monkeypatch):
    calls = {"lines": 0}
    real = tool_index_stream._decode_record_bytes

    def counting(raw):
        calls["lines"] += 1
        return real(raw)

    monkeypatch.setattr(tool_index_stream, "_decode_record_bytes", counting)
    return calls


def _update(root: Path, bundle: Path) -> dict:
    return update_main_context_bundle_artifacts(
        MainContextBundleArtifactUpdateRequest(context_bundle_path=str(bundle), workspace_root=root, **SCOPE))


def _refs(bundle: Path) -> list[str]:
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    return [item["call_id"] for item in payload.get("artifact_refs", {}).get("items", [])]


# 函数用途: 主索引有大量历史行时，收尾只解码回合起点之后追加的行，仍登记本回合引用。
def test_closeout_reads_only_rows_after_turn_start(tmp_path: Path, decoded) -> None:
    root = tmp_path / "workspace"
    index = _history(root)
    bundle = _bundle(tmp_path, root, _mark(index))
    time.sleep(0.01)
    _externalize(root, SCOPE, "now-1")
    assert _update(root, bundle)["artifact_count"] == 1
    assert _refs(bundle) == ["now-1"]
    assert decoded["lines"] == 1


# 函数用途: 本回合未写过的旧 run 索引不读；本回合新建的 run 索引照读，其中的匹配行仍被登记。
def test_untouched_run_index_skipped_new_run_index_read(tmp_path: Path, decoded) -> None:
    root = tmp_path / "workspace"
    index = _history(root, rows=1)
    old_run = root / "runs" / "2026-11-01" / "old" / "work"
    old_index = _history(old_run)
    bundle = _bundle(tmp_path, root, _mark(index))
    _old_mtime(old_index, bundle)
    time.sleep(0.01)
    _externalize(root / "runs" / "2026-11-08" / "new" / "work", SCOPE, "child-1")
    _externalize(root, SCOPE, "now-1")
    assert _update(root, bundle)["artifact_count"] == 2
    assert sorted(_refs(bundle)) == ["child-1", "now-1"]
    assert decoded["lines"] == 2


@pytest.mark.parametrize("variant", ["replaced_inode", "unaligned_mark", "legacy_bundle"])
def test_fallbacks_rescan_whole_index(tmp_path: Path, decoded, variant: str) -> None:
    root = tmp_path / "workspace"
    index = _history(root)
    marks = None if variant == "legacy_bundle" else _mark(index, size_delta=-1 if variant == "unaligned_mark" else 0)
    bundle = _bundle(tmp_path, root, marks)
    time.sleep(0.01)
    if variant == "replaced_inode":
        data = index.read_bytes()
        replacement = index.with_suffix('.replacement')
        replacement.write_bytes(data)
        assert replacement.stat().st_ino != index.stat().st_ino
        replacement.replace(index)
    _externalize(root, SCOPE, "now-1")
    assert _update(root, bundle)["artifact_count"] == 1
    assert decoded["lines"] == HISTORY_ROWS + 1


# 函数用途: 真实上下文包构建写入 owner 主索引标记；不传标记时包内容不增键（旧快照逐字节测试不受影响）。
def test_builder_records_owner_index_mark(tmp_path: Path) -> None:
    from agent_py_agent.agent.common.tool_output_paths import tool_output_index_marks

    root = tmp_path / "workspace"
    index = _history(root, rows=3)
    marks = tool_output_index_marks(root)
    assert marks == _mark(index)
    built = build_main_context_bundle(MainContextBundleRequest(
        root=root, home_paths=ensure_my_agent_home(tmp_path / "home"), user_prompt="继续", save=True,
        tool_index_marks=marks, **SCOPE,
    ))
    assert json.loads(Path(built.json_path).read_text(encoding="utf-8"))["tool_index_marks"] == marks
    assert tool_output_index_marks(tmp_path / "empty") == {}


def _build(root: Path, home, scope: dict[str, str] = SCOPE) -> Path:
    from agent_py_agent.agent.common.tool_output_paths import tool_output_index_marks

    return Path(build_main_context_bundle(MainContextBundleRequest(
        root=root, home_paths=home, user_prompt="继续", save=True, tool_index_marks=tool_output_index_marks(root), **scope,
    )).json_path)


# 函数用途: Compact 自动续跑按同一 stem 重写快照（含跨零点落到新日期目录）时继承首片窗口，首片的主索引与 run 索引引用都不丢。
@pytest.mark.parametrize("day", ["same_day", "previous_day"])
def test_continuation_rebuild_keeps_first_slice_window(tmp_path: Path, decoded, day: str) -> None:
    root = tmp_path / "workspace"
    _history(root)
    home = ensure_my_agent_home(tmp_path / "home")
    first = _build(root, home)
    first_marks = json.loads(first.read_text(encoding="utf-8"))["tool_index_marks"]
    time.sleep(0.01)
    _externalize(root, SCOPE, "slice1-main")
    _externalize(root / "runs" / "2026-11-08" / "task" / "work", SCOPE, "slice1-run")
    assert _update(root, first)["artifact_count"] == 2
    if day == "previous_day":
        earlier = first.parent.with_name((date.fromisoformat(first.parent.name) - timedelta(days=1)).isoformat())
        earlier.mkdir()
        first.rename(earlier / first.name)
    time.sleep(0.01)
    second = _build(root, home)
    assert json.loads(second.read_text(encoding="utf-8"))["tool_index_marks"] == first_marks
    time.sleep(0.01)
    _externalize(root, SCOPE, "slice2-main")
    assert _update(root, second)["artifact_count"] == 3
    assert sorted(_refs(second)) == ["slice1-main", "slice1-run", "slice2-main"]
    assert decoded["lines"] == 5


# 函数用途: 同名旧快照不可读时去掉标记退回整读；另一 run 留下的同名旧快照不继承，新窗口照常收窄。
@pytest.mark.parametrize("prior", ["corrupt", "other_run"])
def test_rebuild_prior_fallbacks(tmp_path: Path, decoded, prior: str) -> None:
    root = tmp_path / "workspace"
    _history(root)
    home = ensure_my_agent_home(tmp_path / "home")
    first = _build(root, home, {**SCOPE, "run_id": "run-before"} if prior == "other_run" else SCOPE)
    if prior == "corrupt":
        first.write_text("{", encoding="utf-8")
    time.sleep(0.01)
    second = _build(root, home)
    assert ("tool_index_marks" in json.loads(second.read_text(encoding="utf-8"))) is (prior == "other_run")
    time.sleep(0.01)
    _externalize(root, SCOPE, "now-1")
    assert _update(root, second)["artifact_count"] == 1
    assert decoded["lines"] == (1 if prior == "other_run" else HISTORY_ROWS + 1)
