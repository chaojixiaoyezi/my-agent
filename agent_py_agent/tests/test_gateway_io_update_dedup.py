"""gateway_parts/io.update_json_file_atomic 内容去重（E3 / mc4-501+502 返工）单元测试。

覆盖：私有文件内容相同跳过（inode/mtime 不变）、内容不同照写、损坏/空文件/非对象根/缺失文件
照写（修复语义）、updater 原地改共享嵌套也照写、force_write 强制写、require_existing 语义不变；
跳过前的安全状态核对（mc4-502）：0644 仍收紧到 0600、符号链接叶子被拒绝（不跟随不改写）、硬链接被断开；
落盘字节比较（mc4-501）：整数/浮点/布尔/负零等类型更正必须写且读回与返回值一致。
"""
from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import io as gateway_io
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic


@pytest.fixture
def write_counter(monkeypatch):
    """替换真实落盘原语为计数器；返回 list，[0] 是调用次数。"""

    calls = [0]
    original = gateway_io.write_private_json_file_atomic_no_newline_unlocked

    def counting(path: Path, payload, *, sort_keys: bool = True) -> None:
        calls[0] += 1
        original(path, payload, sort_keys=sort_keys)

    monkeypatch.setattr(
        gateway_io, "write_private_json_file_atomic_no_newline_unlocked", counting
    )
    return calls


def _write_initial(path: Path, payload, *, private: bool = True) -> None:
    """按落盘原语的序列化格式写初始文件；private=True 时置 0600（模拟已收紧的私有文件）。"""

    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600 if private else 0o644)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_same_content_private_file_skips_write(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"a": 1, "b": {"c": [1, 2]}})
    before = os.lstat(path)
    result = update_json_file_atomic(path, lambda current: {"a": 1, "b": {"c": [1, 2]}})
    assert write_counter[0] == 0
    assert result == {"a": 1, "b": {"c": [1, 2]}}
    after = os.lstat(path)
    assert after.st_ino == before.st_ino  # 私有文件不重写：inode 不变
    assert after.st_mtime_ns == before.st_mtime_ns  # mtime 不变
    assert _read_json(path) == {"a": 1, "b": {"c": [1, 2]}}


def test_same_content_wide_permissions_still_tightened(tmp_path: Path, write_counter):
    # mc4-502：跳过写入不能吞掉"存量宽权限下次写入收紧"的安全副作用。
    path = tmp_path / "state.json"
    _write_initial(path, {"a": 1}, private=False)
    assert _mode(path) == 0o644
    result = update_json_file_atomic(path, lambda current: {"a": 1})
    assert write_counter[0] == 1  # 权限未收紧 → 照写
    assert _mode(path) == 0o600
    assert result == {"a": 1}


def test_same_content_symlink_leaf_is_replaced(tmp_path: Path, write_counter):
    # mc4-502：内容相同也不能借跳写放过符号链接叶子；ec7217e44 起 header 以 nofollow 打开，链接叶子直接拒绝、不跟随也不改写。
    target = tmp_path / "real.json"
    _write_initial(target, {"a": 1})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        update_json_file_atomic(link, lambda current: pytest.fail("symlink updater ran"))
    assert write_counter[0] == 0
    assert link.is_symlink()  # 链接未被跟随或替换
    assert _read_json(target) == {"a": 1}  # 被指向的文件内容不变


def test_same_content_hard_link_is_rewritten(tmp_path: Path, write_counter):
    # mc4-502：硬链接（nlink>1）写入会断开；跳过则链接保留——必须照写。
    target = tmp_path / "real.json"
    _write_initial(target, {"a": 1})
    hard = tmp_path / "hard.json"
    os.link(target, hard)
    assert os.lstat(hard).st_nlink == 2
    result = update_json_file_atomic(hard, lambda current: {"a": 1})
    assert write_counter[0] == 1
    assert os.lstat(hard).st_nlink == 1  # 写入断开了硬链接
    assert _read_json(hard) == {"a": 1}
    assert result == {"a": 1}


def test_changed_content_writes(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"a": 1})
    result = update_json_file_atomic(path, lambda current: {**current, "a": 2})
    assert write_counter[0] == 1
    assert result == {"a": 2}
    assert _read_json(path) == {"a": 2}


def test_corrupt_file_is_repaired(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    update_json_file_atomic(path, lambda current: dict(current))
    assert write_counter[0] == 1
    assert _read_json(path) == {}


def test_empty_file_is_repaired(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")
    update_json_file_atomic(path, lambda current: dict(current))
    assert write_counter[0] == 1
    assert _read_json(path) == {}


def test_non_dict_root_is_repaired(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, [1, 2, 3])
    update_json_file_atomic(path, lambda current: dict(current))
    assert write_counter[0] == 1
    assert _read_json(path) == {}


def test_missing_file_writes(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    result = update_json_file_atomic(path, lambda current: {"created": True})
    assert write_counter[0] == 1
    assert result == {"created": True}
    assert _read_json(path) == {"created": True}


def test_in_place_nested_mutation_still_writes(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"nested": {"k": 1}})

    def updater(current):
        current["nested"]["k"] = 2  # 原地改到共享的嵌套对象
        return current

    update_json_file_atomic(path, updater)
    assert write_counter[0] == 1
    assert _read_json(path) == {"nested": {"k": 2}}


def test_force_write_writes_identical_content(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"a": 1})
    result = update_json_file_atomic(path, lambda current: {"a": 1}, force_write=True)
    assert write_counter[0] == 1
    assert result == {"a": 1}


def test_require_existing_missing_file_raises(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    with pytest.raises(FileNotFoundError):
        update_json_file_atomic(path, lambda current: {"a": 1}, require_existing=True)
    assert write_counter[0] == 0


def test_require_existing_present_file_same_content_skips(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"a": 1})
    update_json_file_atomic(path, lambda current: dict(current), require_existing=True)
    assert write_counter[0] == 0


# mc4-501：Python 深度相等会把类型更正误判成"没变"（True==1、1.0==1、-0.0==0.0），
# 落盘字节比较下这些更新必须真的写盘，且读回与返回值一致。


def test_int_to_bool_rewrites_and_round_trips(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"flag": 1})
    result = update_json_file_atomic(path, lambda current: {"flag": True})
    assert write_counter[0] == 1
    assert result["flag"] is True
    assert _read_json(path)["flag"] is True


def test_nested_number_to_bool_rewrites(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"items": [0]})
    result = update_json_file_atomic(path, lambda current: {"items": [False]})
    assert write_counter[0] == 1
    assert result["items"][0] is False
    assert _read_json(path)["items"][0] is False


def test_int_to_float_rewrites(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"n": 1})
    result = update_json_file_atomic(path, lambda current: {"n": 1.0})
    assert write_counter[0] == 1
    assert result["n"] == 1.0 and isinstance(result["n"], float)
    read_back = _read_json(path)["n"]
    assert isinstance(read_back, float) and read_back == 1.0


def test_negative_zero_rewrites(tmp_path: Path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"z": 0.0})
    result = update_json_file_atomic(path, lambda current: {"z": -0.0})
    assert write_counter[0] == 1
    assert math.copysign(1.0, result["z"]) == -1.0
    assert math.copysign(1.0, _read_json(path)["z"]) == -1.0


def test_crlf_bytes_are_not_mistaken_for_canonical_lf(tmp_path, write_counter):
    path = tmp_path / "state.json"
    _write_initial(path, {"a": 1})
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    result = update_json_file_atomic(path, lambda current: current)
    assert result == {"a": 1}
    assert write_counter[0] == 1
    assert b"\r\n" not in path.read_bytes()


def test_replacement_during_update_cannot_masquerade_as_same_file(tmp_path, write_counter):
    path = tmp_path / "state.json"
    other = tmp_path / "other.json"
    _write_initial(path, {"version": 1})
    _write_initial(other, {"version": 2})

    def update(current):
        os.replace(other, path)
        return current

    assert update_json_file_atomic(path, update) == {"version": 1}
    assert write_counter[0] == 1
    assert _read_json(path) == {"version": 1}
