# LLM: 只缓存完整结构声明的摘要，不缓存路径/owner或初始化结论；fork子进程重建计算锁，调用方仍查持久指纹并同事务盖章。
# 模块用途: 给两个独立数据库提供持久结构指纹，旧副本恢复后自动重新迁移。

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from functools import lru_cache

# 独立 metadata 键不占用其它库的 user_version，完整摘要避免截断哈希碰撞。
_SQLITE_SCHEMA_FINGERPRINT_KEY = "sqlite_schema_fingerprint"
# 两套固定声明只占少数项；保留少量版本交替即可，避免动态声明形成无界进程缓存。
_SQLITE_SCHEMA_DIGEST_CACHE_ENTRIES_COUNT = 32
_SQLITE_SCHEMA_DIGEST_LOCK = threading.RLock()


# LLM: after_in_child不能获取父线程遗留锁；只替换本进程锁，继承的不可变声明摘要可保留，不读写任何用户库。
# 函数用途: fork后让子进程能独立计算冷热摘要，避免默认关闭复用时也被继承锁卡住。
def _reset_schema_digest_lock_after_fork() -> None:
    global _SQLITE_SCHEMA_DIGEST_LOCK
    _SQLITE_SCHEMA_DIGEST_LOCK = threading.RLock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_schema_digest_lock_after_fork)


# LLM: 快照包含全部嵌套内容和标量类型；列表/元组本来都序列化为JSON数组，字典则保留原插入顺序。
# 函数用途: 将当前声明变成不可变缓存键，原对象之后改变也不会污染旧摘要。
def _declaration_key(value: object) -> tuple:
    if isinstance(value, dict):
        return dict, tuple((_declaration_key(key), _declaration_key(item)) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return tuple, tuple(_declaration_key(item) for item in value)
    return type(value), value


# LLM: 只从完整快照恢复JSON形状；不是SQL或库状态解析，不持有调用方的可变声明引用。
# 函数用途: 在首次摘要计算时还原声明内容，保持原有序列化结果。
def _declaration_value(key: tuple) -> object:
    kind, value = key
    if kind is dict:
        return {_declaration_value(item): _declaration_value(data) for item, data in value}
    if kind is tuple:
        return [_declaration_value(item) for item in value]
    return value


# LLM: 缓存键是完整声明而非名称或路径；限量LRU只存结构快照与摘要，不保存用户库是否已初始化。
# 函数用途: 对仍驻留的同一声明只序列化和哈希一次，声明变化自动换键。
@lru_cache(maxsize=_SQLITE_SCHEMA_DIGEST_CACHE_ENTRIES_COUNT)
def _cached_fingerprint(key: tuple) -> str:
    text = json.dumps(_declaration_value(key), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# LLM: 锁仅覆盖纯计算缓存，fork后先换成本进程锁；防并发首次计算，不缓存schema_is_current或跳过事务内重查。
# 函数用途: 从完整结构声明取得摘要，改迁移无需手动版本号，也不每次构造重算。
def schema_fingerprint(declarations: object) -> str:
    with _SQLITE_SCHEMA_DIGEST_LOCK:
        return _cached_fingerprint(_declaration_key(declarations))


# LLM: 只查现有 metadata；新库无表视为未初始化，其它查询错误不能伪装为命中。
# 函数用途: 用一次只读查询判断是否可以跳过完整建表迁移。
def schema_is_current(conn: sqlite3.Connection, fingerprint: str) -> bool:
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (_SQLITE_SCHEMA_FINGERPRINT_KEY,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: metadata" not in str(exc):
            raise
        return False
    return row is not None and row[0] == fingerprint


# LLM: 有写库副作用，必须与完整迁移在同一事务；调用方提交失败时不得留下已完成标记。
# 函数用途: 结构初始化成功后持久保存摘要，供后续实例与进程复用。
def stamp_schema(conn: sqlite3.Connection, fingerprint: str) -> None:
    conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (_SQLITE_SCHEMA_FINGERPRINT_KEY, fingerprint))
