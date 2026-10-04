"""global_index 数据文件与目录收私（ds3l 提交2）。

be 2026-10-03 顺带发现 global_index/*.jsonl 的数据文件是 0644、目录 0755。这里把它改成走 common/json_io
私有写（文件 0600、缺失目录按 0700 新建、已有 0644 下次写入即收紧；已存在的目录权限一律不动，
pdp 2026-10-03），内容逐字节不变。

注：be 同批提到的 memory/daily 经复核不成立——daily 分片本来就走私有原子写，其 _ensure_private_dir 已把
daily 目录收紧到 0700（已在干净基线 b6ede99e0 上实测），故本次不改 daily，也就没有对应用例。

umask 固定 0o022 跑，钉“不靠 umask”。全部用 pytest tmp_path，不碰真实 home，不调真实模型。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.user_space.home_indexes import _append_changed_index_record

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


# 函数用途: 固定 umask 0o022（生产常见值），验证私有权限不依赖 umask；用例结束恢复原值。
@pytest.fixture
def default_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# 函数用途: 读一个路径的权限位。
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# 函数用途: 构造一条带稳定 key 的索引记录（key 字段满足 global_index 的 active_tasks 形状）。
def _index_record(task_id: str, title: str) -> dict[str, object]:
    return {"schema_version": "global-task-index.v1", "owner_id": "alice", "task_id": task_id,
            "task_path": f"/tmp/{task_id}", "status": "active", "title": title}


@_POSIX_ONLY
def test_global_index_append_is_private_from_birth(tmp_path, default_umask):
    path = tmp_path / "global_index" / "active_tasks.jsonl"
    assert _append_changed_index_record(path, _index_record("t1", "标题"), key_fields=("owner_id", "task_id"))

    assert (_mode(path), _mode(path.parent)) == (0o600, 0o700), "索引数据文件与其目录都必须私有"


@_POSIX_ONLY
def test_global_index_existing_world_readable_file_is_tightened_without_byte_change(tmp_path, default_umask):
    path = tmp_path / "global_index" / "active_tasks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    first = _index_record("t1", "标题")
    path.write_text(json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)  # 生产旧状态
    os.chmod(path.parent, 0o755)
    before = path.read_bytes()

    assert _append_changed_index_record(path, _index_record("t2", "第二条"), key_fields=("owner_id", "task_id"))

    assert (_mode(path), _mode(path.parent)) == (0o600, 0o755), "文件被收紧；已存在的目录权限不动"
    assert path.read_bytes().startswith(before), "已有内容必须逐字节不变，只在尾部追加"
