
from __future__ import annotations

"""通用状态文件 schema 版本化 + 迁移框架。

解决的问题(调研 V1-V4):状态文件(各类 metrics/baseline/checkpoint 等)
改格式后,旧数据要么读不了、要么"缺字段=默认值"和"旧版本无此字段"分不清。

做法:给落盘 dict 戳上 `_schema_version`,读取时按注册的迁移链把旧版本数据逐级升级到
当前版本。无版本戳的老数据视为版本 0,从 0 开始迁移。纯字典操作、无 IO,任何状态文件可复用。
"""

from typing import Any, Callable

_SCHEMA_KEY = "_schema_version"

# 一条迁移:接收某版本的 payload,返回升一级后的 payload。key 是"从哪个版本升"。
Migration = Callable[[dict[str, Any]], dict[str, Any]]


def stamp(payload: dict[str, Any], version: int) -> dict[str, Any]:
    """给 payload 戳上 schema 版本(原地改 + 返回,便于链式)。"""
    payload[_SCHEMA_KEY] = int(version)
    return payload


def read_version(payload: dict[str, Any]) -> int:
    """读 payload 的 schema 版本;无戳的老数据视为 0(最古老、未版本化)。"""
    try:
        return max(0, int(payload.get(_SCHEMA_KEY, 0)))
    except (TypeError, ValueError):
        return 0


def needs_migration(payload: dict[str, Any], current_version: int) -> bool:
    """payload 是否比当前版本旧、需要迁移。"""
    return read_version(payload) < current_version


def migrate(
    payload: dict[str, Any], *, current_version: int, migrations: dict[int, Migration] | None = None
) -> dict[str, Any]:
    """把 payload 从它自带的版本逐级迁移到 current_version,沿途戳新版本号。

    migrations: {from_version: fn},fn 把 v 版数据升到 v+1 版。缺某级迁移函数则该级只升版本号
    (适用于"只加可选字段、旧数据靠默认值兜底"的无损升级)。返回迁移后的 payload(戳上 current_version)。
    """
    migrations = migrations or {}
    v = read_version(payload)
    while v < current_version:
        fn = migrations.get(v)
        if fn is not None:
            payload = fn(payload)
        v += 1
    return stamp(payload, current_version)


__all__ = ["stamp", "read_version", "needs_migration", "migrate", "Migration"]
