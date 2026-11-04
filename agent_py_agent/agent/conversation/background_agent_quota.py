"""Owner后台身份额度；原线程是唯一身份权威，不创建第二份生命周期或执行账。

可丢弃SQLite只保存非retired身份的定位引用。旧名册每次至多回填一页，未追平即unknown/
partial并拒绝新槽；热查询只重读当前活跃集。create/restore共用跨进程Owner门，先登记
引用再提交canonical，进程中断最多保守保留一个引用，下一次锁内重读即可清除未提交项。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager

from ..common.nofollow_fs import ensure_private_dir
from ..gateway_parts.io import locked_file_transition
from ..settings.defaults import default_config_value
from .background_team_lifecycle import background_identity
from .business_access import BusinessError
from .models import ConversationThread
from .store_threads import _read_thread_record_report


# LLM: 不缓存Owner配置；0只表示不限身份数，不绕过任何执行并发门。
# 函数用途: 读取宿主已加载的管理员身份配额并拒绝无效配置。
def _limit(service):
    config = getattr(service, "quota_config", service.host.config)
    value = getattr(config, "max_background_agents", default_config_value("max_background_agents"))
    if type(value) is not int or value < 0:
        raise ValueError("max_background_agents必须是非负整数")
    return value


class BackgroundAgentQuota:
    """定位投影没有执行许可；所有增槽动作必须持guard直到canonical提交结束。"""

    # LLM: service只能来自原后台服务，Owner从可信宿主取得，不接受请求中的owner键。
    # 函数用途: 绑定同一canonical线程库与可重建活跃集。
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.owner_id = str(service.host.home_paths.owner_id)
        self.path = self.store.storage.root / ".background-agent-quota.sqlite3"

    # LLM: 与线程准入锁分离；顺序固定Owner门→身份准入→canonical线程锁，不领执行租约。
    # 函数用途: 跨进程串行同Owner的create和restore整个增槽事务。
    @contextmanager
    def guard(self):
        key = hashlib.sha256(self.owner_id.encode()).hexdigest()
        with locked_file_transition(self.store.storage.root / (".background-agent-quota-" + key)):
            yield

    # LLM: 只保存定位引用，拒绝链接/特殊文件；坏库不截断成零，交由调用方暴露unknown。
    # 函数用途: 在短数据库门内打开私有额度投影。
    @contextmanager
    def _connection(self):
        ensure_private_dir(self.path.parent)
        with locked_file_transition(self.path):
            if not self.path.exists():
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(descriptor)
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("额度投影不是私有普通文件")
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, timeout=5.0)
            try:
                connection.execute("CREATE TABLE IF NOT EXISTS active_refs (owner_id TEXT, thread_id TEXT, PRIMARY KEY(owner_id,thread_id))")
                connection.execute("CREATE TABLE IF NOT EXISTS bootstrap (owner_id TEXT PRIMARY KEY, cursor TEXT, complete INTEGER NOT NULL)")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    # LLM: 旧历史只通过原分页投影逐批回填；页链失效不拼接为完整计数，已发现的引用可幂等保留。
    # 函数用途: 有界发现旧身份，返回本次覆盖事实而不扫描全部历史。
    def _bootstrap(self, connection):
        row = connection.execute("SELECT cursor,complete FROM bootstrap WHERE owner_id=?", (self.owner_id,)).fetchone()
        if row and row[1]:
            return "complete"
        page = self.service._index().page(limit=100, cursor=row[0] if row else None, filter_key=self.owner_id)
        if page["load_errors"]:
            return "unknown"
        for payload in page["items"]:
            thread = ConversationThread.from_dict(payload)
            if thread.owner_id != self.owner_id:
                raise ValueError("OWNER_MISMATCH")
            identity = background_identity(thread)
            if identity is not None and identity.identity_state != "retired":
                connection.execute("INSERT OR IGNORE INTO active_refs VALUES (?,?)", (self.owner_id, thread.thread_id))
        complete = page["coverage"] == "complete" and page["next_cursor"] is None
        cursor = page["next_cursor"] if page["coverage"] == "complete" else None
        connection.execute("INSERT OR REPLACE INTO bootstrap VALUES (?,?,?)", (self.owner_id, cursor, int(complete)))
        return "complete" if complete else "partial"

    # LLM: 只读取活跃定位集并重验canonical；生命周期自动退役不需要第二个状态同步写者。
    # 函数用途: 计算唯一口径的额度快照，不把未知、坏引用或未回填当作0。
    def snapshot(self):
        limit = _limit(self.service)
        used, available_agents, unassigned = 0, 0, 0
        with self._connection() as connection:
            coverage = self._bootstrap(connection)
            rows = connection.execute("SELECT thread_id FROM active_refs WHERE owner_id=?", (self.owner_id,)).fetchall()
            for (identifier,) in rows:
                try:
                    path = self.store.storage.thread_path(identifier)
                    thread, error = _read_thread_record_report(path)
                    if error or (thread is None and path.exists()):
                        raise ValueError("BACKGROUND_IDENTITY_UNREADABLE")
                    if thread is None:
                        # 增槽事务持Owner门；不存在说明预留进程已结束且canonical从未提交。
                        connection.execute("DELETE FROM active_refs WHERE owner_id=? AND thread_id=?", (self.owner_id, identifier))
                        continue
                    identity = background_identity(thread)
                    if thread.owner_id != self.owner_id or identity is None:
                        raise ValueError("BACKGROUND_IDENTITY_MISMATCH")
                    if identity.identity_state == "retired":
                        connection.execute("DELETE FROM active_refs WHERE owner_id=? AND thread_id=?", (self.owner_id, identifier))
                        continue
                    used += 1
                    available_agents += identity.identity_state == "available"
                    unassigned += identity.space_id is None
                except (OSError, ValueError, TypeError):
                    coverage = "unknown"
        known = coverage == "complete"
        return {"limit": limit, "used": used if known else None,
                "available": max(0, limit - used) if known and limit else None,
                "unlimited": limit == 0, "over_limit": used > limit if known and limit else (False if known else None),
                "available_agents": available_agents if known else None, "unassigned": unassigned if known else None,
                "coverage": coverage}

    # LLM: 先核完整canonical用量，再在同Owner门内提交引用；调用方须紧接原线程提交，不能释放门后另写。
    # 函数用途: 原子保留一个新身份槽，额度满时保留精确limit/used细节。
    def reserve(self, identifier):
        try:
            summary = self.snapshot()
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            raise BusinessError("HOST_NOT_READY", details={"coverage": "unknown"}) from error
        if summary["coverage"] != "complete":
            raise BusinessError("HOST_NOT_READY", details=summary)
        if summary["limit"] and summary["used"] >= summary["limit"]:
            raise BusinessError("BACKGROUND_AGENT_LIMIT_REACHED", details=summary)
        self.track(identifier)

    # LLM: 幂等重放只修复定位引用，不再申请新槽；canonical身份与请求指纹仍由原服务校验。
    # 函数用途: 登记已校验或即将同门提交的身份引用。
    def track(self, identifier):
        with self._connection() as connection:
            connection.execute("INSERT OR IGNORE INTO active_refs VALUES (?,?)", (self.owner_id, identifier))


# LLM: 只读业务语义；可推进可丢弃定位索引，不创建线程/任务、不运行模型、不修改生命周期。
# 函数用途: 向route/TUI等可信宿主调用者提供同Owner真实limit/used/available及覆盖事实。
def background_agent_quota_summary(host, *, actor):
    from .background_agents import BackgroundAgentService

    return BackgroundAgentService(host).quota_summary(actor=actor)
