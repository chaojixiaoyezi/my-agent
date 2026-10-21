# LLM: 资产包只提供Owner固定根下的值契约与存储，不注册工具或执行资产；共用nofollow及原JSON锁。
# 模块用途: 提供两类Store的私有IO、安全ID和可恢复跨文件事务；读者先恢复未提交旧版，再观察一致状态。
from __future__ import annotations

import json
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)

MAX_ASSET_BYTES = 16 * 1024 * 1024
TRANSACTION_FILE = "asset-transaction.json"


# LLM: ID永远是单个路径段，禁止绝对路径、分隔符及点目录；不从metadata取得路径。
# 函数用途: 校验资产ID或入口文件名，返回原字符串。
def safe_asset_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("资产ID必须是1至128位安全标识符")
    return value


# LLM: 超时与运行耗时不是授权；拒绝bool、非有限数和零/负超时。
# 函数用途: 校验脚本和工作流的正超时。
def positive_timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("超时必须是正有限数")
    return value


# LLM: 根由宿主构造传入，payload不能重定位；锁跨Store实例，读改写都在同一集合锁下。
# 类用途: 复用元数据和侧文件的nofollow读取、原子替换、异常回滚与受控删除。
class _AssetStore:
    # LLM: 构造只准备显式Owner根；不从当前cwd、资产owner_id或模型字段推断Owner。
    # 函数用途: 固定本实例的Owner和权威集合路径。
    def __init__(self, owner_home, collection, filename):
        self.owner_home = Path(owner_home).absolute()
        self.owner_home.mkdir(parents=True, exist_ok=True)
        os.close(open_directory_beneath(self.owner_home, ()))
        self.collection, self.filename = collection, filename

    # LLM: 集合先用nofollow打开/创建；原锁提供线程及进程互斥，不在资产内留下锁文件。
    # 函数用途: 让同Owner同集合的保存、更新、读取与删除串行提交。
    @contextmanager
    def _locked(self):
        os.close(open_directory_beneath(self.owner_home, (self.collection,), create=True))
        with locked_json_path(self.owner_home / self.collection / ".assets"):
            yield

    # LLM: 严格nofollow读取包含单链接普通文件检查，缺失返回None；损坏不吞掉。
    # 函数用途: 安全读取资产目录中的有界文本，不创建目录。
    def _text(self, asset_id, filename):
        parts = (self.collection, safe_asset_id(asset_id), safe_asset_id(filename))
        limit = MAX_ASSET_BYTES * 16 if filename == TRANSACTION_FILE else MAX_ASSET_BYTES
        value = read_bytes_beneath(self.owner_home, parts, max_bytes=limit)
        return value.decode("utf-8") if value is not None else None

    # LLM: JSON只从唯一权威叶读取；先在原集合锁下恢复未提交事务，ID必须匹配目录，损坏不当缺失。
    # 函数用途: 读取平铺元数据供领域Store进一步做严格类型校验。
    def _read(self, asset_id):
        self._recover(asset_id)
        text = self._text(asset_id, self.filename)
        if text is None:
            return None
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("id") != asset_id:
            raise ValueError("资产元数据ID与目录不匹配")
        return payload

    # LLM: 单文件提交复用原nofollow临时文件+fsync+rename；None只删除显式管理叶。
    # 函数用途: 保存或清除侧文件，调用方持集合锁。
    def _put(self, asset_id, filename, content):
        parts = (self.collection, safe_asset_id(asset_id), safe_asset_id(filename))
        if content is None:
            unlink_file_beneath(self.owner_home, parts)
        else:
            write_text_atomic_beneath(self.owner_home, parts, content)

    # LLM: undo先原子持久化，元数据最后替换，删除undo才提交；异常或重启读取都恢复一致旧版。
    # 函数用途: 在集合锁下发布元数据及侧文件，拒绝不可读的大内容，保留失败恢复材料。
    def _write(self, payload, side_files=None):
        asset_id = safe_asset_id(payload["id"])
        updates = dict(side_files or {})
        updates[self.filename] = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        for content in updates.values():
            if content is not None and len(content.encode("utf-8")) > MAX_ASSET_BYTES:
                raise ValueError("资产文件超过安全读取字节预算")
        previous = {name: self._text(asset_id, name) for name in updates}
        journal = json.dumps({"schema_version": "asset_transaction.v1", "previous": previous}, ensure_ascii=False)
        if len(journal.encode("utf-8")) > MAX_ASSET_BYTES * 16:
            raise ValueError("资产事务恢复记录过大")
        self._put(asset_id, TRANSACTION_FILE, journal)
        try:
            for name, content in updates.items():
                self._put(asset_id, name, content)
            self._put(asset_id, TRANSACTION_FILE, None)
        except BaseException:
            self._recover(asset_id)
            raise

    # LLM: 只读固定undo叶，先校验全部恢复名和值再写；恢复再失败保留undo，下次读取仍拒绝混合版本。
    # 函数用途: 锁内恢复进程中断的保存，全部旧叶恢复后才删除事务记录。
    def _recover(self, asset_id):
        text = self._text(asset_id, TRANSACTION_FILE)
        if text is None:
            return
        journal = json.loads(text)
        if not isinstance(journal, dict) or set(journal) != {"schema_version", "previous"} or journal["schema_version"] != "asset_transaction.v1":
            raise ValueError("损坏的资产事务记录")
        previous = journal["previous"]
        if not isinstance(previous, dict) or self.filename not in previous or len({str(key).casefold() for key in previous}) != len(previous):
            raise ValueError("无效资产恢复目标")
        for name, content in previous.items():
            safe_asset_id(name)
            if name.casefold() == TRANSACTION_FILE or content is not None and (not isinstance(content, str) or len(content.encode("utf-8")) > MAX_ASSET_BYTES):
                raise ValueError("无效资产恢复文件")
        for name, content in reversed(tuple(previous.items())):
            self._put(asset_id, name, content)
        self._put(asset_id, TRANSACTION_FILE, None)

    # LLM: 不扫Owner外路径，不静默接受损坏记录；点开头只属于集合内部锁。
    # 函数用途: 列出集合中可能存在的安全资产目录ID。
    def _ids(self):
        try:
            names = list_names_beneath(self.owner_home, (self.collection,))
        except FileNotFoundError:
            return []
        return [safe_asset_id(name) for name in sorted(names) if not name.startswith(".")]

    # LLM: 先验证目录内每一普通单链接叶，再逐叶安全删除；链接或子目录一律拒绝，不递归跟随。
    # 函数用途: 删除本资产的管理目录，缺失返回False；调用方先完成领域记录校验并持锁。
    def _delete(self, asset_id):
        safe_asset_id(asset_id)
        parts = (self.collection, asset_id)
        try:
            names = list_names_beneath(self.owner_home, parts)
        except FileNotFoundError:
            return False
        for name in names:
            self._text(asset_id, name)
        for name in names:
            self._put(asset_id, name, None)
        parent = open_directory_beneath(self.owner_home, (self.collection,))
        try:
            os.rmdir(asset_id, dir_fd=parent)
        finally:
            os.close(parent)
        return True


# LLM: 只接受结构化运行观察；计数在原集合锁内更新，不产生运行、网络请求或新账本。
# 函数用途: 从旧摘要计算本次次数、最近耗时与连续失败数。
def next_run_summary(summary, status, duration_ms, run_at):
    from .descriptor import RuntimeSummary

    if not isinstance(status, str) or not status.strip():
        raise ValueError("运行状态不能为空")
    failures = summary.consecutive_failures + 1 if status in {"failed", "error", "timeout", "timed_out"} else 0
    return RuntimeSummary(run_at, status, duration_ms, summary.run_count + 1, failures)
