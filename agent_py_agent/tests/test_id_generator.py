"""B.1 统一 ID 生成器测试：七类框架 ID 全部经 new_id(kind) 生成。

覆盖：七类 kind 前缀映射、opaque 形态（可过 validate_opaque_id）、
未知 kind 拒绝、唯一性、既有调用点行为零变化（run_id 仍 subagent- 前缀）。
"""

from __future__ import annotations

import re

import pytest

from agent_py_agent.agent.common.id_generator import (
    ID_KIND_PREFIXES,
    UnknownIdKindError,
    new_id,
)
from agent_py_agent.agent.common.opaque_id import (
    OpaqueIdError,
    validate_opaque_id,
    validate_path_segment,
)

# B.1 七类必须齐全，缺一类就是回归。
B1_KINDS = (
    "run_id",
    "task_id",
    "task_run_id",
    "agent_run_id",
    "attempt_id",
    "session_id",
    "delegation_id",
)

_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@pytest.mark.parametrize("kind", B1_KINDS)
def test_all_seven_b1_kinds_registered(kind):
    assert kind in ID_KIND_PREFIXES


@pytest.mark.parametrize("kind", B1_KINDS)
def test_new_id_prefix_matches_kind(kind):
    value = new_id(kind)
    expected_prefix = ID_KIND_PREFIXES[kind]
    assert value.startswith(f"{expected_prefix}-")


def test_new_id_is_opaque():
    # B.2: 生成物必须能过拒绝式校验（无路径段/无控制字符/长度受限）。
    for kind in B1_KINDS:
        validate_opaque_id(new_id(kind), kind=kind)


def test_new_id_has_no_path_shape():
    for kind in B1_KINDS:
        value = new_id(kind)
        assert "/" not in value
        assert "\\" not in value
        assert value != ".."
        assert ".." not in value


def test_new_id_unique_per_call():
    first = new_id("run_id")
    second = new_id("run_id")
    assert first != second


def test_new_id_unknown_kind_rejected():
    with pytest.raises(UnknownIdKindError, match="未知 ID 种类"):
        new_id("explode")
    with pytest.raises(UnknownIdKindError, match="未知 ID 种类"):
        new_id("")
    with pytest.raises(UnknownIdKindError, match="未知 ID 种类"):
        new_id("subagent")  # 裸前缀不是 kind，防绕过映射


def test_run_id_prefix_unchanged():
    # 存量 task.json 记录全用 subagent- 前缀，改名会造成新旧形态分裂。
    assert ID_KIND_PREFIXES["run_id"] == "subagent"
    assert new_id("run_id").startswith("subagent-")


def test_attempt_id_prefix_unchanged():
    assert ID_KIND_PREFIXES["attempt_id"] == "attempt"
    assert new_id("attempt_id").startswith("attempt-")


@pytest.mark.parametrize("value", ["/tmp/file", r"C:\home", "Z:/home", r"\\host\share", "../task", "thread/other",
                                   "thread\n", "a：b", "-run", "x" * 129])
def test_opaque_path_segments_reject_path_shapes_on_every_platform(value):
    with pytest.raises(OpaqueIdError):
        validate_path_segment(value, kind="thread_id")


def test_opaque_path_segments_keep_valid_historical_and_max_length_ids():
    for value in ("t", "thread-aB_1", "x" * 128):
        assert validate_path_segment(value, kind="thread_id") is value
