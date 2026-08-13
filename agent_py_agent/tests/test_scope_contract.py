from __future__ import annotations

"""共享 scope-key canonical/validator 合同：全 scope 正向/负向矩阵。

写入侧（validate_typed_scope_key / for_new_observation）与召回侧
（canonical_scope_key / _append_runtime_identity / _append_declared_scopes）
共用同一把尺：raw 与已 canonical typed key 必须落到同一键；嵌套 typed prefix
（双前缀身份分裂）、空 remainder（company:）与空值一律拒绝；project 单一权威
project:<id>，task:<id> 只作召回兼容别名且写入入口归一（不允许以 task:<id>
新持久化，避免 project/task 双正式身份）。
"""

import pytest

from agent_py_agent.agent.memory_store.candidate_models import MemoryScope, stable_candidate_id
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.memory_store.scope_contract import (
    canonical_scope_key,
    scope_key_aliases,
    validate_typed_scope_key,
)

# ---------------------------------------------------------------- canonical 正向

RAW_TO_CANONICAL = {
    ("company", "acme"): "company:acme",
    ("project", "alpha"): "project:alpha",
    ("task_class", "review"): "task_class:review",
    ("session", "s1"): "session:s1",
    ("temporary", "turn-1"): "temporary:turn-1",
}
TYPED_KEEP = {
    ("company", "company:acme"),
    ("project", "project:alpha"),
    ("task_class", "task_class:review"),
    ("session", "session:s1"),
    ("temporary", "temporary:turn-1"),
}
FIXED_KEEP = {("global", "global"), ("personal", "personal")}
TASK_ALIAS_NORMALIZE = {
    ("project", "task:alpha"): "project:alpha",  # 旧账本别名 → 归一 project:<id>
}


@pytest.mark.parametrize("scope_type,raw,expected", [
    *[(t, raw, canon) for (t, raw), canon in RAW_TO_CANONICAL.items()],
])
def test_canonical_raw_id_becomes_typed(scope_type: str, raw: str, expected: str) -> None:
    """raw host ID → canonical <type>:<id>，与写侧 typed 键一致。"""
    assert canonical_scope_key(scope_type, raw) == expected


@pytest.mark.parametrize("scope_type,key", [
    *TYPED_KEEP,
    *FIXED_KEEP,
])
def test_canonical_typed_key_stays_unchanged(scope_type: str, key: str) -> None:
    """已 canonical typed key 原样使用，不再重复 prefix。"""
    assert canonical_scope_key(scope_type, key) == key


@pytest.mark.parametrize("scope_type,key,expected", [
    *[(t, alias, canon) for (t, alias), canon in TASK_ALIAS_NORMALIZE.items()],
])
def test_canonical_task_alias_normalizes_to_project(scope_type: str, key: str, expected: str) -> None:
    """project 的 task:<id> 旧账本别名归一为 project:<id>（单一权威）。"""
    assert canonical_scope_key(scope_type, key) == expected


def test_canonical_idempotent() -> None:
    """canonical(canonical(x)) == canonical(x)，raw/typed/别名落同一键。"""
    for scope_type, raw, expected in [
        ("company", "acme", "company:acme"),
        ("project", "alpha", "project:alpha"),
        ("project", "task:alpha", "project:alpha"),
    ]:
        once = canonical_scope_key(scope_type, raw)
        assert once == expected
        assert canonical_scope_key(scope_type, once) == once


# ---------------------------------------------------------------- canonical 负向

@pytest.mark.parametrize("scope_type,key", [
    ("company", "company:company:acme"),  # 嵌套同前缀
    ("project", "project:project:alpha"),  # 嵌套同前缀
    ("project", "project:task:alpha"),  # 嵌套跨前缀
    ("project", "task:project:alpha"),  # task 别名内嵌 project 前缀
    ("company", "task:acme"),  # 别的 scope 前缀
    ("session", "company:s1"),  # 别的 scope 前缀
    ("company", ""),  # 空
    ("company", "   "),  # 空白
    ("company", "company:"),  # 空 remainder
    ("project", "project:"),  # 空 remainder
    ("project", "task:"),  # 空 remainder（task 别名）
    ("task_class", "task_class:"),  # 空 remainder
    ("session", "session:"),  # 空 remainder
    ("temporary", "temporary:"),  # 空 remainder
    ("global", "globalx"),  # 固定键错值
    ("personal", "personal:acme"),  # personal 不许 typed
    ("nonsense", "x:y"),  # 未知 scope_type
])
def test_canonical_rejects_nested_empty_or_alien(scope_type: str, key: str) -> None:
    """空值、空 remainder、嵌套 typed prefix、异类型前缀、固定键错值一律拒绝。"""
    with pytest.raises(ValueError):
        canonical_scope_key(scope_type, key)


# ---------------------------------------------------------- 写入侧 typed 校验

@pytest.mark.parametrize("scope_type,key", [
    *TYPED_KEEP,
    *FIXED_KEEP,
])
def test_validate_typed_key_accepts_canonical(scope_type: str, key: str) -> None:
    """写入侧放行已 canonical typed key（含固定键），返回原样键。"""
    assert validate_typed_scope_key(scope_type, key) == key


@pytest.mark.parametrize("scope_type,key,expected", [
    *[(t, alias, canon) for (t, alias), canon in TASK_ALIAS_NORMALIZE.items()],
])
def test_validate_typed_key_normalizes_task_alias(scope_type: str, key: str, expected: str) -> None:
    """写入侧把 project 的 task:<id> 旧账本兼容输入归一为 project:<id> 持久化。"""
    assert validate_typed_scope_key(scope_type, key) == expected


@pytest.mark.parametrize("scope_type,key", [
    *[(t, raw) for (t, raw), _canon in RAW_TO_CANONICAL.items()],
    ("company", "company:company:acme"),
    ("project", "project:task:alpha"),
    ("company", ""),
    ("company", "company:"),
    ("project", "task:"),
    ("global", "globalx"),
    ("personal", "local"),
])
def test_validate_typed_key_rejects_raw_nested_empty(scope_type: str, key: str) -> None:
    """写入侧拒绝 raw（身份分裂）、嵌套双前缀、空值、空 remainder 与固定键错值。"""
    with pytest.raises(ValueError):
        validate_typed_scope_key(scope_type, key)


def test_for_new_observation_normalizes_task_alias_to_project() -> None:
    """task:alpha 与 project:alpha 写入归一为同一键（单一权威持久化）。"""
    s1 = MemoryScope.for_new_observation({"scope_type": "project", "scope_key": "task:alpha"})
    s2 = MemoryScope.for_new_observation({"scope_type": "project", "scope_key": "project:alpha"})
    assert s1.scope_key == s2.scope_key == "project:alpha"


def test_stable_candidate_id_same_after_normalization() -> None:
    """旧 task:alpha 与 新 project:alpha 写入归一后候选身份一致（幂等/冲突同键）。"""
    scope_task = MemoryScope.for_new_observation({"scope_type": "project", "scope_key": "task:alpha"})
    scope_project = MemoryScope.for_new_observation({"scope_type": "project", "scope_key": "project:alpha"})
    kwargs = {
        "candidate_type": "long_term_fact",
        "content": "事实 X",
        "subject_key": "sub",
        "proposed_action": "add",
        "target_entry_id": "",
    }
    assert stable_candidate_id(scope=scope_task, **kwargs) == stable_candidate_id(
        scope=scope_project, **kwargs
    )


# ---------------------------------------------------------------- 召回别名

def test_scope_key_aliases_project_task_compat() -> None:
    """project 召回补出 task:<id> 老账本别名，其余 scope 单键。"""
    assert scope_key_aliases("project", "project:alpha") == (
        "project:alpha",
        "task:alpha",
    )
    assert scope_key_aliases("company", "company:acme") == ("company:acme",)
    assert scope_key_aliases("session", "session:s1") == ("session:s1",)


# ---------------------------------------------------------------- 集成：召回侧

def test_from_runtime_raw_and_typed_company_same_keys() -> None:
    """company_id raw 与 typed 生成同一组键，无双前缀债务。"""
    raw = MemoryRecallScope.from_runtime(task_attributes={"company_id": "acme"})
    typed = MemoryRecallScope.from_runtime(task_attributes={"company_id": "company:acme"})
    assert ("company", "company:acme") in raw.keys
    assert ("company", "company:acme") in typed.keys
    assert ("company", "company:company:acme") not in raw.keys
    assert ("company", "company:company:acme") not in typed.keys
    assert set(raw.keys) == set(typed.keys)


@pytest.mark.parametrize("project_value", ["alpha", "project:alpha", "task:alpha"])
def test_from_runtime_project_three_inputs_same_keys(project_value: str) -> None:
    """project raw/typed/task 别名三种输入产出完全相同的 keys（输出相等断言）。"""
    scope = MemoryRecallScope.from_runtime(task_attributes={"project_id": project_value})
    project_pairs = {pair for pair in scope.keys if pair[0] == "project"}
    assert project_pairs == {("project", "project:alpha"), ("project", "task:alpha")}


def test_from_runtime_project_task_id_keeps_task_compat() -> None:
    """project 身份生成 project:<id> + task:<id> 两个兼容键，无双前缀。"""
    scope = MemoryRecallScope.from_runtime(task_id="gwreq-123")
    assert ("project", "project:gwreq-123") in scope.keys
    assert ("project", "task:gwreq-123") in scope.keys
    assert ("project", "project:project:gwreq-123") not in scope.keys
    assert ("project", "project:task:gwreq-123") not in scope.keys


def test_from_runtime_bad_values_do_not_expand() -> None:
    """坏值（空/嵌套/空白）不扩大召回范围；task 别名归一为 project 双键。"""
    scope = MemoryRecallScope.from_runtime(
        task_attributes={
            "company_id": "company:company:acme",  # 嵌套双前缀 → 整体拒绝
            "project_id": "task:alpha",  # task 别名 → 归一 project:alpha 双键
            "session_id": "   ",  # 空白 → _append_scope_pair 兜底拒绝
        },
    )
    assert ("company", "company:company:acme") not in scope.keys
    assert ("company", "company:acme") not in scope.keys  # 嵌套被整体拒绝
    assert ("project", "project:alpha") in scope.keys  # 归一后的权威键
    assert ("project", "task:alpha") in scope.keys  # 兼容别名
    assert ("project", "project:project:alpha") not in scope.keys
    assert ("session", "session:   ") not in scope.keys


def test_declared_scopes_use_shared_contract() -> None:
    """显式 memory_scope 声明走共享 canonical 合同：raw 归一、坏值 fail-closed。"""
    scope = MemoryRecallScope.from_runtime(
        task_attributes={
            "memory_scope": [
                {"scope_type": "company", "scope_key": "company:company:acme"},  # 嵌套 → 拒绝
                {"scope_type": "company", "scope_key": "acme"},  # raw → 归一 company:acme
                {"scope_type": "personal", "scope_key": "local"},  # 固定键错值 → 拒绝
                {"scope_type": "personal", "scope_key": "personal"},  # 固定键合法
                {"scope_type": "project", "scope_key": "task:beta"},  # task 别名 → 归一 project 双键
            ],
        },
    )
    assert ("company", "company:company:acme") not in scope.keys
    assert ("company", "company:acme") in scope.keys
    assert ("personal", "local") not in scope.keys
    assert ("personal", "personal") in scope.keys
    assert ("project", "project:beta") in scope.keys
    assert ("project", "task:beta") in scope.keys


# -------------------------------------------------------- 集成：写入侧合同

def test_for_new_observation_nested_company_rejected() -> None:
    """company:company:acme 双前缀在 observe 入口被合同拒绝。"""
    with pytest.raises(ValueError):
        MemoryScope.for_new_observation(
            {"scope_type": "company", "scope_key": "company:company:acme"}
        )


def test_for_new_observation_nested_project_rejected() -> None:
    """project:task:alpha 嵌套跨前缀在 observe 入口被合同拒绝。"""
    with pytest.raises(ValueError):
        MemoryScope.for_new_observation(
            {"scope_type": "project", "scope_key": "project:task:alpha"}
        )


def test_for_new_observation_empty_remainder_rejected() -> None:
    """company: / project: / task: 空 remainder 在 observe 入口被合同拒绝。"""
    for value in (
        {"scope_type": "company", "scope_key": "company:"},
        {"scope_type": "project", "scope_key": "project:"},
        {"scope_type": "project", "scope_key": "task:"},
    ):
        with pytest.raises(ValueError):
            MemoryScope.for_new_observation(value)


def test_for_new_observation_project_task_compat_normalized() -> None:
    """project 的 task:<id> 兼容形式写入时归一为 project:<id> 持久化。"""
    scope = MemoryScope.for_new_observation(
        {"scope_type": "project", "scope_key": "task:alpha"}
    )
    assert scope.scope_key == "project:alpha"
