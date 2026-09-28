# LLM: 测试专用：给每个 pytest 进程一个自己的 PostgreSQL schema，通过连接 URL 的 libpq options 设 search_path。
#   共用本机 postgres 的用例（入站队列等）原来都在 public 里建同一张 ingress_messages 并互相 DROP，12 分片并行时
#   跨分片互删表，出现“未在超时内 drain / claim 拿不到 / 续租失败”的偶发失败。这里只隔离测试，不改产品代码；
#   产品侧的表名、迁移和索引语句都不带 schema 前缀，跟随 search_path 落进本进程 schema。
#   连接失败原样抛异常，调用方按原来的方式 skip；退出时删本进程 schema，并顺手清掉已死进程留下的同前缀 schema。
# 模块用途: 让并行分片里的 PostgreSQL 真测各用各的 schema，互不干扰。
from __future__ import annotations

import atexit
import os
import uuid
from urllib.parse import quote

_BASE_URL = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
_SCHEMA_PREFIX = "pytest_ingress_"
_STATE: dict[str, str] = {}


# LLM: 每个进程只建一次 schema（模块级缓存）；URL 已带 options 时视为操作者自己做了隔离，原样返回。
#   建 schema 用一条短连接，不复用测试自己的 engine；任何异常都向上抛，让调用方沿用“无可用 PostgreSQL → skip”。
# 函数用途: 返回带本进程专属 search_path 的 PostgreSQL 连接 URL，首次调用时创建 schema 并登记退出清理。
def isolated_postgres_url() -> str:
    if "url" in _STATE:
        return _STATE["url"]
    if "options=" in _BASE_URL:
        return _BASE_URL
    from sqlalchemy import create_engine, text

    schema = f"{_SCHEMA_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:8]}"
    engine = create_engine(_BASE_URL)
    try:
        with engine.begin() as conn:
            _drop_dead_process_schemas(conn)
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        engine.dispose()
    separator = "&" if "?" in _BASE_URL else "?"
    _STATE["schema"] = schema
    _STATE["url"] = f"{_BASE_URL}{separator}options={quote(f'-csearch_path={schema}', safe='')}"
    atexit.register(_drop_own_schema)
    return _STATE["url"]


# LLM: 只删本进程建的 schema；测试 engine 可能还没 dispose，用 CASCADE 并吞掉异常（残留只是本机测试库里的空 schema）。
# 函数用途: 进程退出时删掉本进程的测试 schema。
def _drop_own_schema() -> None:
    schema = _STATE.get("schema")
    if not schema:
        return
    try:
        _admin_execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except Exception:  # noqa: BLE001 - 退出清理尽力而为
        return


# LLM: 管理语句用一条短连接自己的 engine，不碰测试 engine 的连接池；异常原样抛给调用方决定。
# 函数用途: 在基础库上执行一条管理语句（建/删 schema）。
def _admin_execute(statement: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(_BASE_URL)
    try:
        with engine.begin() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


# LLM: 名字里的 pid 已不存活才删（被 kill 的分片留下的残留）；pid 仍存活的一律不碰，即使它已不是 pytest 也只是留一个空 schema。
# 函数用途: 顺手清理已死进程留下的测试 schema，避免本机测试库无限堆积。
def _drop_dead_process_schemas(conn) -> None:
    from sqlalchemy import text

    names = conn.execute(
        text("SELECT nspname FROM pg_namespace WHERE nspname LIKE :pattern"), {"pattern": f"{_SCHEMA_PREFIX}%"},
    ).scalars().all()
    for name in names:
        pid_text = str(name)[len(_SCHEMA_PREFIX):].split("_", 1)[0]
        if not pid_text.isdigit() or _pid_alive(int(pid_text)):
            continue
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


# LLM: 信号 0 只探测存在；无权限（EPERM）说明进程存在，按存活处理。
# 函数用途: 判断一个 pid 是否还在。
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
