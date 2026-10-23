# LLM: 资产写入固定于宿主给定Owner根；全局兜底只读，显式管理员工厂才把全局根设为写根，不注册或执行工具。
# 模块用途: 共用nofollow、分层读取、严格资产ID、技能逻辑引用及提交后读回错误合同。
from __future__ import annotations

import json
import math
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from ..common.json_io import _flock_descriptor, _funlock_descriptor, locked_json_path
from ..common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)

# 单个资产文件允许读取的最大字节数上限。
MAX_ASSET_BYTES = 16 * 1024 * 1024
TRANSACTION_FILE = "asset-transaction.json"


# LLM: ID永远是单个路径段，禁止绝对路径、分隔符及点目录；不从metadata取得路径。
# 函数用途: 校验资产ID或入口文件名，返回原字符串。
def safe_asset_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("资产ID必须是1至128位安全标识符")
    return value


# LLM: Unicode只用于逻辑名称，绝不代替safe_asset_id作为磁盘路径校验；不strip或清洗身份。
# 函数用途: 校验中文等原生Skill名称，拒绝分隔符、点穿越和控制字符。
def _skill_reference_part(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\w[\w.-]{0,127}", value) or ".." in value:
        raise ValueError("技能名称必须是1至128位安全逻辑标识符")
    return value


# LLM: 技能来源是逻辑命名域，名称可为Unicode；脚本/工作流目录仍只认严格safe_asset_id。
# 函数用途: 校验裸技能名和准确来源引用，保留原始身份。
def valid_skill_reference(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("技能引用必须是字符串")
    parts = value.split(":")
    if len(parts) == 1:
        return _skill_reference_part(value)
    if len(parts) == 2 and parts[0] in {"owner", "workspace", "shared", "builtin", "learned"}:
        _skill_reference_part(parts[1])
    elif len(parts) == 3 and parts[0] == "plugin":
        _skill_reference_part(parts[1])
        _skill_reference_part(parts[2])
    else:
        raise ValueError("技能引用来源或形状无效")
    return value


# LLM: 仅为已发现的名称派生安全资产键，不作为外部逻辑引用校验或磁盘路径授权。
# 函数用途: 为Unicode等名称生成与技能目录投影一致的确定性安全ID。
def skill_asset_id(name):
    try:
        return safe_asset_id(name)
    except ValueError:
        if not isinstance(name, str) or not name:
            raise ValueError('Skill名称或ID必须是非空字符串') from None
        slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_.-')[:72] or 'name'
        return f'skill_{slug}_{sha256(name.encode("utf-8")).hexdigest()[:16]}'


# LLM: 先校验来源引用，再为AssetRefs派生安全键；步骤仍保留精确逻辑引用，此键不用于猜物理路径。
# 函数用途: 为工作流依赖摘要取得严格ASCII的技能资产键。
def canonical_skill_name(value: str) -> str:
    return skill_asset_id(valid_skill_reference(value).rsplit(":", 1)[-1])


# LLM: 只表示_write正常返回后的读回失败，不能包装写入自身异常。
# 类用途: 区分已提交但无法确认读回与提交前失败。
class AssetPostCommitReadbackError(RuntimeError):
    pass


# LLM: 超时与运行耗时不是授权；拒绝bool、非有限数和零/负超时。
# 函数用途: 校验脚本和工作流的正超时。
def positive_timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("超时必须是正有限数")
    return value


# LLM: 根只来自可信构造参数；自动发现只认owners祖先，extra按顺序去重，不resolve链接、不创建候选目录。
# 函数用途: 推导默认全局根及额外只读根，排除本Owner自身。
def _global_roots(owner_home, extra_roots):
    if isinstance(extra_roots, (str, bytes, os.PathLike)):
        raise ValueError('extra_roots必须是根目录序列')
    default = next((parent.parent for parent in owner_home.parents if parent.name == 'owners'), None)
    roots = (default, *extra_roots) if default is not None else tuple(extra_roots)
    return tuple(dict.fromkeys(path for root in roots
                               if (path := Path(os.path.abspath(root))) != owner_home))


# LLM: 根由宿主构造传入，payload不能重定位；锁跨Store实例，提交后读回入口仅在_write成功后使用。
# 类用途: 复用元数据和侧文件的nofollow读取、原子替换、异常回滚与受控删除。
class _AssetStore:
    # LLM: 宿主固定Owner写根，is_admin不重定位普通操作；全局候选只供读取，不从metadata或cwd取得权限。
    # 函数用途: 准备Owner根并登记额外只读来源。
    def __init__(self, owner_home, collection, filename, *, extra_roots=(), is_admin=False):
        self.owner_home = Path(owner_home).absolute()
        self._global_roots = _global_roots(self.owner_home, extra_roots)
        self._read_only, self.is_admin = False, is_admin is True
        self.owner_home.mkdir(parents=True, exist_ok=True)
        os.close(open_directory_beneath(self.owner_home, ()))
        self.collection, self.filename = collection, filename

    # LLM: is_admin是可信宿主核实后传入的身份事实，不接受1等真值；拒绝发生在任何目录创建前，不从资产正文授权。
    # 函数用途: 显式为管理员构造全局写存储，不再向其它全局根兜底。
    @classmethod
    def for_global_admin(cls, global_root, *, is_admin=True):
        if is_admin is not True:
            raise PermissionError('仅管理员可修改全局脚本或工作流')
        store = cls(global_root, is_admin=True)
        store._global_roots = ()
        return store

    # LLM: 不调用构造器，避免mkdir或权限收紧；内部视图复用同一领域解码，禁止再递归兜底或写入。
    # 函数用途: 为一个明确全局根建立无IO的只读存储视图。
    def _global_view(self, root):
        view = object.__new__(type(self))
        view.owner_home, view.collection, view.filename = root, self.collection, self.filename
        view._read_only, view.is_admin, view._global_roots = True, False, ()
        return view

    # LLM: 只读打开同一OS锁，正常或异常退出都核身份；锁变优先报重试、未变保留原读取错误，不创建或chmod。
    # 函数用途: 协调全局只读快照，所有出口均核对锁身份并回收锁和FD。
    @contextmanager
    def _readonly_locked(self):
        try:
            descriptor = open_readonly_file_beneath(self.owner_home, (self.collection, '.assets.lock'))
        except FileNotFoundError:
            try:
                yield
            finally:
                self._check_readonly_lock(None)
            return
        try:
            _flock_descriptor(descriptor)
            try:
                self._check_readonly_lock(descriptor)
                yield
            finally:
                try:
                    self._check_readonly_lock(descriptor)
                finally:
                    _funlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    # LLM: 对照持有FD和nofollow重开的同名锁；无锁读取期间出生新锁、旧锁消失或换inode均失败关闭，不修改任何叶。
    # 函数用途: 防止全局锁等待或读取期间的身份变化击穿一致性。
    def _check_readonly_lock(self, descriptor):
        try:
            current = open_readonly_file_beneath(self.owner_home, (self.collection, '.assets.lock'))
        except FileNotFoundError:
            if descriptor is not None:
                raise BlockingIOError('全局资产锁已消失，请重试') from None
            return
        try:
            if descriptor is None:
                raise BlockingIOError('全局资产锁已出现，请重试')
            expected, observed = os.fstat(descriptor), os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (observed.st_dev, observed.st_ino):
                raise BlockingIOError('全局资产锁已变化，请重试')
        finally:
            os.close(current)

    # LLM: Owner沿原nofollow创建与双层锁；内部全局视图只打开已有锁，不能创建或收紧共享文件。
    # 函数用途: 为领域存储提供与实际来源匹配的读写临界区。
    @contextmanager
    def _locked(self):
        if self._read_only:
            with self._readonly_locked():
                yield
            return
        os.close(open_directory_beneath(self.owner_home, (self.collection,), create=True))
        with locked_json_path(self.owner_home / self.collection / ".assets"):
            yield

    # LLM: 只在Owner确认缺失并释放其锁后调用；顺序查根且首命中返回，坏记录明确失败，不使用真假值判缺失。
    # 函数用途: 读取只读全局资产并标记真实来源。
    def _global_get(self, asset_id):
        for root in self._global_roots:
            view = self._global_view(root)
            with view._locked():
                record = view._get(asset_id)
                if record is not None:
                    return view._mark_global(record)
        return None

    # LLM: 普通列表复用领域读取；脚本子类覆写为仅元数据，不读取代码或执行资产。
    # 函数用途: 取得一条列表投影，默认适用于工作流定义。
    def _list_entry(self, asset_id):
        return self._get(asset_id)

    # LLM: 不重试或补写，保留读取故障cause；中断及BaseException继续传播。
    # 函数用途: 为已提交资产提供类型明确的读回失败。
    def _readback_after_commit(self, asset_id):
        try:
            record = self._get(asset_id)
            if record is None:
                raise FileNotFoundError("已提交资产读回缺失")
            return record
        except InterruptedError:
            raise
        except Exception as exc:
            raise AssetPostCommitReadbackError(f"资产{asset_id}提交后读回失败") from exc

    # LLM: 排除已由高优先级根认领的ID后才读取，避免低优先级坏记录击穿Owner遮蔽。
    # 函数用途: 锁内按ID收集当前根的列表条目。
    def _local_records(self, excluded=()):
        return {asset_id: record for asset_id in self._ids() if asset_id not in excluded
                and (record := self._list_entry(asset_id)) is not None}

    # LLM: 调用方已释放Owner锁，各全局根逐个取锁，不形成跨根锁环；结果仅为投影，不持久化作用域标记。
    # 函数用途: 合并Owner和全局列表，同ID保留第一份并按ID稳定排序。
    def _merge_globals(self, records):
        for root in self._global_roots:
            view = self._global_view(root)
            with view._locked():
                records.update({asset_id: view._mark_global(record)
                                for asset_id, record in view._local_records(records).items()})
        return [records[asset_id] for asset_id in sorted(records)]

    # LLM: 严格nofollow读取包含单链接普通文件检查，缺失返回None；损坏不吞掉。
    # 函数用途: 安全读取资产目录中的有界文本，不创建目录。
    def _text(self, asset_id, filename):
        parts = (self.collection, safe_asset_id(asset_id), safe_asset_id(filename))
        limit = MAX_ASSET_BYTES * 16 if filename == TRANSACTION_FILE else MAX_ASSET_BYTES
        value = read_bytes_beneath(self.owner_home, parts, max_bytes=limit)
        return value.decode("utf-8") if value is not None else None

    # LLM: Owner原锁下先恢复；只读全局不能恢复写，遇未提交事务明确失败交管理员处理；ID匹配和严格JSON保持。
    # 函数用途: 按来源安全读取权威元数据，不将全局读取变成隐式修复。
    def _read(self, asset_id):
        if self._read_only:
            if self._text(asset_id, TRANSACTION_FILE) is not None:
                raise ValueError('全局资产事务未提交，请管理员恢复')
        else:
            self._recover(asset_id)
        text = self._text(asset_id, self.filename)
        if text is None:
            return None
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("id") != asset_id:
            raise ValueError("资产元数据ID与目录不匹配")
        return payload

    # LLM: 全局只读视图禁止所有叶写入；写根仍复用原nofollow临时文件+fsync+rename，None仅删管理叶。
    # 函数用途: 在已持锁的可写根保存或清除侧文件。
    def _put(self, asset_id, filename, content):
        if self._read_only:
            raise PermissionError('全局资产视图只读')
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

    # LLM: 全局只读视图不能删除空目录；可写根先验每一普通单链接叶，链接或子目录拒绝，不递归跟随。
    # 函数用途: 在可写根受控删除单个资产目录，缺失返回False。
    def _delete(self, asset_id):
        if self._read_only:
            raise PermissionError('全局资产视图只读')
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


# LLM: Store在原集合锁内调用；省略时间使用UTC，失败继承旧错误而非失败默认清空，错误沿统一脱敏口；不执行资产。
# 函数用途: 从旧摘要计算本次计数、最近耗时和安全错误说明。
def next_run_summary(summary, status, duration_ms, run_at=None, last_error=""):
    from .descriptor import RuntimeSummary, clean_json

    if not isinstance(status, str) or not status.strip():
        raise ValueError("运行状态不能为空")
    if run_at is None:
        run_at = datetime.now(timezone.utc).isoformat()
    if status in {"failed", "error", "timeout", "timed_out"}:
        failures = summary.consecutive_failures + 1
        resolved_error = clean_json(str(last_error or summary.last_error or ""))
    else:
        failures = 0
        resolved_error = clean_json(str(last_error or ""))
    return RuntimeSummary(run_at, status, duration_ms, summary.run_count + 1, failures, resolved_error)
