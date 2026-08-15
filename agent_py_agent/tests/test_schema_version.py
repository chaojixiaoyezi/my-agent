"""通用 schema 版本化 + 迁移框架单测。"""

from agent.common.schema_version import migrate, needs_migration, read_version, stamp


def test_stamp_and_read():
    p = stamp({}, 3)
    assert read_version(p) == 3
    assert p["_schema_version"] == 3


def test_unversioned_is_zero():
    assert read_version({}) == 0
    assert read_version({"foo": 1}) == 0  # 无戳的老数据 = 版本 0


def test_corrupt_version_is_zero():
    assert read_version({"_schema_version": "abc"}) == 0
    assert read_version({"_schema_version": -5}) == 0


def test_needs_migration():
    assert needs_migration({}, 2) is True
    assert needs_migration(stamp({}, 2), 2) is False
    assert needs_migration(stamp({}, 3), 2) is False  # 更新的不回迁


def test_migrate_applies_chain():
    """v0→v1 加字段 a;v1→v2 把 a 改名 b。从无戳老数据一路升到 v2。"""
    migrations = {
        0: lambda p: {**p, "a": 1},
        1: lambda p: {k: v for k, v in p.items() if k != "a"} | {"b": p.get("a", 0)},
    }
    out = migrate({}, current_version=2, migrations=migrations)
    assert out["b"] == 1
    assert "a" not in out
    assert read_version(out) == 2


def test_migrate_missing_fn_just_bumps_version():
    """没注册迁移函数的级只升版本号(无损升级,旧数据靠默认值兜底)。"""
    out = migrate({"x": 5}, current_version=3, migrations={})
    assert out["x"] == 5  # 数据不动
    assert read_version(out) == 3


def test_migrate_from_partial_version():
    """已是 v1 的数据只跑 v1→v2,不重跑 v0→v1。"""
    migrations = {
        0: lambda p: {**p, "from_v0": True},
        1: lambda p: {**p, "from_v1": True},
    }
    out = migrate(stamp({}, 1), current_version=2, migrations=migrations)
    assert "from_v0" not in out  # v0 级跳过(数据已是 v1)
    assert out["from_v1"] is True
    assert read_version(out) == 2


def test_migrate_noop_when_current():
    out = migrate(stamp({"x": 1}, 2), current_version=2)
    assert out["x"] == 1
    assert read_version(out) == 2
