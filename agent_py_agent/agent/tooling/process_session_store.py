# LLM: 原 Store 是 v2/v3/v4 session 唯一权威，锁内先恢复 redo；任务/激活分别冻结，共享和显式保留记录只在原结果落账后消费。
#   便宜检查仅复用已可信读取的目录内容，发布与停止仍在原事务重读，不缓存通知或执行结论。
# 模块用途: 为 launcher、host 和 Gateway 串行管理预留与停止意图，保留原目录、CAS 和 v1 显式句柄权限。
from __future__ import annotations

import hashlib
import math
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.cache_freshness import cache_entry_trustworthy
from ..common.directory_lock import locked_private_directory
from ..common.json_io import locked_json_path, write_json_file_atomic_unlocked
from ..common.nofollow_fs import read_bytes_beneath, unlink_file_beneath
from ..runtime_errors import runtime_error_report
from .process_cleanup_evidence import confirm_cleanup_reference, validate_cleanup_reference
from .process_scope import ProcessActivationScope, ProcessExecutionScope
from .process_session_commit import (
    ProcessSessionCommitPendingError,
    ProcessSessionCommitReceipt,
    commit_process_records,
    read_process_record,
    recover_process_commit,
)
from .process_session_records import (
    LEGACY_PROCESS_SESSION_SCHEMA,
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
    merge_process_record,
    validate_process_record,
    validate_session_id,
)


# LLM: 一期候选③（background_tick_cheap_checks）的进程记录目录投影缓存: 缓存"读到的记录/错误 +
#   目录身份 + 最近一次全量读取时刻",不含任何"无事"结论——调用方每次重新决定要不要通知。
#   命中只凭目录身份 (dev, ino, mtime_ns): 记录的新建/改写/删除全走 root 内 rename/unlink,
#   目录 mtime 必变;锁文件稳态 flock 不改 mtime。2 秒保护窗口 + 60 秒全量兜底由共用设施保证。
#   条目上限按 owner 数 LRU: 测试机 owner 池上限 600,但热点轮转窗口远小于池上限,单条缓存
#   只有几条记录;被逐出的 owner 下次全读,只变慢不出错。调用方拿到的永远是 deepcopy。
# 类用途: 保存一代目录中的记录与错误；命中仅提供内容，调用方仍实时决定完成通知。
@dataclass(frozen=True)
class _ProcessRecordsProjection:
    stat_key: tuple[int, int, int]
    records: tuple[dict[str, object], ...]
    errors: tuple[dict[str, object], ...]
    verified_at: float


# 进程记录投影缓存最多缓存 64 个 owner 快照（按 owner 数 LRU）；被逐出的 owner 下次全读，只变慢不出错。
_PROCESS_RECORDS_PROJECTION_MAX_ENTRIES_COUNT = 64
# 便宜检查下,距最近一次全量读取(事务 + glob + 逐文件读)超过这个秒数就无条件重读一次:
# 防时间戳精度与极端竞态。命中复用不会刷新这个时间。
_PROCESS_RECORDS_PROJECTION_FULL_READ_INTERVAL_SECONDS = 60.0
_PROCESS_RECORDS_PROJECTION_CACHE: OrderedDict[str, _ProcessRecordsProjection] = OrderedDict()
_PROCESS_RECORDS_PROJECTION_LOCK = threading.Lock()


# LLM: 仅真实目录可命中优化；链接或类型异常交回原Store事务，原锚点正规化和允许/拒绝语义不变。
# 函数用途: 取进程记录目录的身份指纹,供便宜检查判定缓存是否仍绑定当前目录状态。
def _process_dir_stat_key(root: Path) -> tuple[int, int, int] | None:
    try:
        status = root.lstat()
    except OSError:
        return None
    return (status.st_dev, status.st_ino, status.st_mtime_ns) if stat.S_ISDIR(status.st_mode) else None


# LLM: 便宜命中(一期候选③): 只做一次 stat。条件与设计逐字对应:
#   ① 目录身份与缓存一致;② mtime 已离开 2 秒粗粒度保护窗口(cache_freshness 唯一实现);
#   ③ 距最近一次全量读取不足 60 秒。命中不刷新 verified_at;未命中返回 None(调用方走原路径)。
# 函数用途: 判断缓存记录投影能否仅凭目录身份复用;命中时原子提级 LRU。
def _lookup_process_records_projection(root: Path) -> _ProcessRecordsProjection | None:
    stat_key = _process_dir_stat_key(root)
    if stat_key is None:
        return None
    key = str(root)
    now = time.time()
    with _PROCESS_RECORDS_PROJECTION_LOCK:
        entry = _PROCESS_RECORDS_PROJECTION_CACHE.get(key)
        if entry is None or entry.stat_key != stat_key:
            return None
        if not cache_entry_trustworthy(stat_key[2]):
            return None
        if now - entry.verified_at >= _PROCESS_RECORDS_PROJECTION_FULL_READ_INTERVAL_SECONDS:
            return None
        _PROCESS_RECORDS_PROJECTION_CACHE.move_to_end(key)
        return entry


# LLM: 全量读取完成后写缓存: 只有"读取前"与"读取后"目录身份一致(读期间没有写者/恢复落盘)
#   才绑定缓存——否则这份记录可能已经过期,宁可不缓存(下次全读,只慢不错)。
#   MC4-902/1201 返工: "读取时是否可信"由调用方在读取前冻结(read_trusted),本函数只在发布时
#   复核目录身份没变——不再用发布时的目录年龄代替读取时的判断(跨窗口延迟发布时旧结果不得
#   变成可信缓存)。
#   MC4-903 返工: 入缓存前 deepcopy,缓存与调用方拿到的返回值不共享可变对象(防调用方原地修改
#   污染缓存)。
#   私有loader可显式移交刚从事务新读出的对象；所有公开返回仍逐条深拷贝，不能把过滤子集交给缓存。
# 函数用途: 把一次完整目录读取的结果绑定到目录身份写入有界缓存(信任判断由调用方读取前冻结)。
def _store_process_records_projection(
    root: Path,
    before: tuple[int, int, int] | None,
    records: list[dict[str, object]],
    errors: list[dict[str, object]],
    *, take_ownership: bool = False,
) -> None:
    after = _process_dir_stat_key(root)
    if before is None or after is None or after != before:
        return
    entry = _ProcessRecordsProjection(
        stat_key=after,
        records=tuple(records) if take_ownership else tuple(deepcopy(record) for record in records),
        errors=tuple(errors) if take_ownership else tuple(deepcopy(error) for error in errors),
        verified_at=time.time(),
    )
    key = str(root)
    with _PROCESS_RECORDS_PROJECTION_LOCK:
        _PROCESS_RECORDS_PROJECTION_CACHE[key] = entry
        _PROCESS_RECORDS_PROJECTION_CACHE.move_to_end(key)
        while len(_PROCESS_RECORDS_PROJECTION_CACHE) > _PROCESS_RECORDS_PROJECTION_MAX_ENTRIES_COUNT:
            _PROCESS_RECORDS_PROJECTION_CACHE.popitem(last=False)


# LLM: 读取损坏与记录不存在必须区分；异常不能暴露原始命令、环境或记录内容。
# 类用途: 返回一条可信记录或结构化读取错误，供查询层判断能否继续使用该句柄。
@dataclass(frozen=True)
class ProcessSessionLoadReport:
    record: dict[str, object]
    load_error: dict[str, object] | None = None


# LLM: records与load_errors均为调用者独占副本，不能泄漏投影缓存可变对象或把读失败解释成无候选。
# 类用途: 承载候选完成记录及完整读取错误。
@dataclass(frozen=True)
class ProcessSessionListReport:
    records: list[dict[str, object]]
    load_errors: list[dict[str, object]]


# LLM: 过期 v2/v3/v4 写入不得通过生命周期合并绕过 CAS；调用方应重读原句柄而不是启动新进程。
# 类用途: 表示调用者拿着旧记录版本更新，磁盘事实没有被本次请求改动。
class ProcessSessionRevisionConflict(ValueError):
    pass


# LLM: Owner-scoped records remain outside the exact owner sandbox; this address must not change with permissions.
# 函数用途: 计算原后台会话权威目录，多用户记录仍放在 owner 沙箱外的同级受保护目录。
def process_session_store_root(workspace_root: str | Path, owner_scope_root: object = "") -> Path:
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    owner_text = str(owner_scope_root or "").strip()
    if not owner_text:
        return workspace / ".background_jobs" / "process_sessions"
    owner = Path(owner_text).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:20]
    return owner.parent / ".my-agent-runtime" / "process_sessions" / digest


# LLM: 此对象只在 Store.transaction 的同一目录锁内有效；不另存任务状态，也不重入公共 Store 方法。
# 类用途: 为需要多次检查与持久检查点的启动交接提供锁内读写，退出上下文后不能再次调用。
class ProcessSessionTransaction:
    # LLM: 构造不取锁，只有持目录锁的 Store 可以提供此对象；根目录已经规范化。
    # 函数用途: 绑定本次临界区的目录和有效期。
    def __init__(self, root: Path) -> None:
        self.root = root
        self.active = True

    # LLM: 离开目录锁后必须失败，防止把一次临界区对象保存在长寿命缓存里继续写。
    # 函数用途: 检查当前事务是否仍处于持锁上下文。
    def _require_active(self) -> None:
        if not self.active:
            raise RuntimeError("managed process transaction is no longer active")

    # LLM: 损坏与缺失不合并；这里抛出读取错误，让更高层决定是否能执行副作用。
    # 函数用途: 读取锁内最新记录，用于启动准入、交接和精确终态更新。
    def load(self, session_id: str) -> dict[str, object] | None:
        self._require_active()
        return read_process_record(self.root, session_id)

    # LLM: 单条损坏保留错误；调用方需要全范围原子操作时必须拒绝有错误的清单。
    # 函数用途: 在一个一致的目录视图内枚举原 session 文件，记录无效项。
    def list_records(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        self._require_active()
        records, errors = [], []
        for path in sorted(self.root.glob("bg-*.json")):
            try:
                record = self.load(path.stem)
                if record is not None:
                    records.append(record)
            except (OSError, TypeError, ValueError) as exc:
                errors.append(process_session_error_report(exc, "process_session_store.validate", path=path))
        return records, errors

    # LLM: v1 沿原逐记录锁，v2/v3/v4 共用 revision CAS 和 redo；旧版本原样更新，不能迁移或覆盖坏记录。
    # 函数用途: 原子保存一条完整记录，返回磁盘接受的有效内容和新版本。
    def write(self, record: dict[str, object]) -> dict[str, object]:
        self._require_active()
        incoming = validate_process_record(record)
        if incoming["schema"] == LEGACY_PROCESS_SESSION_SCHEMA:
            path = self.root / f"{incoming['session_id']}.json"
            with locked_json_path(path):
                existing = self.load(incoming["session_id"])
                effective = merge_process_record(existing, incoming) if existing else incoming
                write_json_file_atomic_unlocked(path, effective)
                path.chmod(0o600)
            return effective
        existing, effective = self._prepare_managed(incoming)
        return self._commit([(existing, effective)]).records[0]

    # LLM: 提交后失败使当前锁内视图失效；调用方必须退出上下文并通过正常恢复入口重新读取。
    # 函数用途: 安装固定批次，阻止调用者捕获待恢复异常后继续读取半份事务。
    def _commit(
        self, transitions: list[tuple[dict[str, object] | None, dict[str, object]]]
    ) -> ProcessSessionCommitReceipt:
        try:
            return commit_process_records(self.root, transitions)
        except ProcessSessionCommitPendingError:
            self.active = False
            raise

    # LLM: 同一句柄版本只能前进一次；命令、实例和任务/激活归属由纯记录合同校验，不允许隐式升 schema。
    # 函数用途: 在磁盘当前版本上准备一次托管记录更新，尚不修改文件。
    def _prepare_managed(
        self, incoming: dict[str, object]
    ) -> tuple[dict[str, object] | None, dict[str, object]]:
        existing = self.load(incoming["session_id"])
        expected = existing.get("revision") if existing else 0
        if incoming["revision"] != expected:
            raise ProcessSessionRevisionConflict("managed process revision conflict")
        effective = merge_process_record(existing, incoming) if existing else dict(incoming)
        return existing, {**effective, "revision": expected + 1}

    # LLM: scope 必须是宿主冻结身份；坏记录拒绝整批，v1/共享激活不参加；终态仅在调用方明确核验全部资源时纳入。
    # 函数用途: 在同一锁内冻结任务资源；管理清理可包含已结束命令的 host，普通任务停止仍只选未终态。
    def request_stop(self, scope: ProcessExecutionScope, *, include_terminal: bool = False) -> ProcessSessionCommitReceipt:
        records, errors = self.list_records()
        if errors:
            raise ValueError("damaged managed process records prevent complete stop selection")
        selected = [
            record
            for record in records
            if record["schema"] in MANAGED_PROCESS_SESSION_SCHEMAS
            and record.get("activation_scope") is None
            and (include_terminal or record["status"] not in PROCESS_TERMINAL_STATUSES)
            and ProcessExecutionScope(**record["execution_scope"]).matches(scope)
        ]
        transitions = [self._prepare_managed({**record, "stop_requested": True}) for record in selected]
        if not transitions:
            return ProcessSessionCommitReceipt("", ())
        return self._commit(transitions)

    # LLM: 调用者先持久撤销且释放安装锁；只按完整激活身份冻结，包括保留的终态证据，不发现其他任务或代次。
    # 函数用途: 在原锁内固定共享插件的全部精确 session，提交停止意图后交给原清理器逐一核对。
    def request_stop_activation(self, scope: ProcessActivationScope) -> ProcessSessionCommitReceipt:
        if not isinstance(scope, ProcessActivationScope):
            raise TypeError("managed activation stop requires exact scope")
        records, errors = self.list_records()
        if errors:
            raise ValueError("damaged managed process records prevent complete stop selection")
        selected = [record for record in records if record["schema"] in MANAGED_PROCESS_SESSION_SCHEMAS
                    and record.get("activation_scope") == asdict(scope)]
        transitions = [self._prepare_managed({**record, "stop_requested": True}) for record in selected]
        return self._commit(transitions) if transitions else ProcessSessionCommitReceipt("", ())

    # LLM: 调用方必须先持久化原操作中的固定引用；原锁先恢复 redo，整批核验后逐条幂等删除，部分失败保留余项供重送。
    # 函数用途: 消费原管理结果已经接管的准确退出证据，不按当前插件或任务重新扫描删除。
    def consume_cleanup(self, references: tuple[dict, ...]) -> None:
        self._require_active()
        selected = []
        seen = set()
        for reference in references:
            validate_cleanup_reference(reference)
            session_id = reference["session_id"]
            if session_id in seen:
                raise ValueError("重复资源清理引用")
            seen.add(session_id)
            record = self.load(session_id)
            if record is not None:
                confirm_cleanup_reference(reference, record)
                selected.append(session_id)
        for session_id in selected:
            unlink_file_beneath(self.root, (f"{session_id}.json",))

    # LLM: 数量裁剪保持原终态集合与排序；TTL只用可信finished_at，pending或未登记通知绝不删除。
    #   调用方需先完成共享资源清理；TTL只回收已终态历史记录，不关闭进程或替代资源释放。
    # 函数用途: 在原目录锁内清理数量超额或安全超龄记录，并返回实际删除的句柄。
    def prune_finished(self, max_finished: int | None = None, *, max_age_seconds: float | None = None,
                       now: float | None = None, is_notice_consumed: Callable[[str], bool] | None = None) -> list[str]:
        records, _errors = self.list_records()
        finished = [
            record
            for record in records
            if record["status"] in PROCESS_TERMINAL_STATUSES
            and not record.get("retain_until_consumed", False)
            and record.get("activation_scope") is None
            and _completion_obligation_finished(record, is_notice_consumed)
        ]
        finished.sort(
            key=lambda item: float(item.get("finished_at") or item.get("started_at") or 0),
            reverse=True,
        )
        selected = {record["session_id"]: record for record in finished[max(0, int(max_finished)) :]} if max_finished is not None else {}
        current_now = time.time() if now is None else float(now)
        for record in records:
            if _record_ttl_expired(record, max_age_seconds, current_now) and _completion_obligation_finished(record, is_notice_consumed):
                selected[record["session_id"]] = record
        return [session_id for session_id, record in selected.items() if _unlink_session_if_unchanged(self.root, record)]


# LLM: 规范空目标{}表示无收件人；有目标必须有回执且显式停止或消费读口证明已不pending，缺读口即保留。
# 函数用途: 判断历史完成通知义务是否已经结清，不缓存或猜测wake状态。
def _completion_obligation_finished(record: dict[str, object], consumed: Callable[[str], bool] | None) -> bool:
    if not record.get("completion_target"):
        return True
    notice = str(record.get("completion_notice_id") or "").strip()
    return bool(notice) and (notice == "explicit_stop" or (consumed is not None and consumed(notice) is True))


# LLM: TTL仅清理exited/killed，不能用started_at兜底；小合成时间、缺失、非有限数均不按年龄删。
# 函数用途: 对可信完成时间执行保守的历史过期判断。
def _record_ttl_expired(record: dict[str, object], max_age_seconds: float | None, now: float) -> bool:
    finished = record.get("finished_at")
    if record.get("status") not in {"exited", "killed"} or type(finished) not in (int, float) or max_age_seconds is None:
        return False
    age = float(max_age_seconds)
    return (math.isfinite(finished) and math.isfinite(age) and math.isfinite(now)
            and float(finished) >= 86400.0 and 0 < age <= now - float(finished))


# LLM: 调用者持目录锁；叶锁内同时比较原记录和两次nofollow字节，版本或字节漂移就不删，侧车只随成功删除清理。
# 函数用途: 删除未并发更新的准确session及其锁文件，不跟随链接、不清理日志或其他资源。
def _unlink_session_if_unchanged(root: Path, record: dict[str, object]) -> bool:
    session_id = validate_session_id(record["session_id"])
    name = f"{session_id}.json"
    with locked_json_path(root / name):
        before = read_bytes_beneath(root, (name,))
        if before is None or read_process_record(root, session_id) != record or read_bytes_beneath(root, (name,)) != before:
            return False
        unlink_file_beneath(root, (name,))
    unlink_file_beneath(root, (name + ".lock",))
    return True


# LLM: 公共入口共用原目录锁和恢复；v2/v3/v4 原版本更新，v1 不参与按任务控制，共享激活另按完整归属冻结。
# 类用途: 提供跨进程的后台会话存取，以及需要持锁多检查点的启动与停止事务。
# LLM: 可选目录投影仅用于初读内容；实际恢复、通知写回、停止与revision CAS仍读取原Store事务。
class ProcessSessionStore:
    # LLM: 构造只规范路径，不创建目录；缺失目录的纯查询保持无副作用。
    # 函数用途: 绑定一个后台会话权威目录。
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)

    # LLM: 文件地址只由已校验句柄生成，不能接受分隔符、相对路径或不匹配正文的别名。
    # 函数用途: 返回 session 的原 JSON 路径。
    def record_path(self, session_id: str) -> Path:
        return self.root / f"{validate_session_id(session_id)}.json"

    # LLM: 原锁及 redo 顺序不变；wait_check 仅影响排队，取得锁后的恢复/提交不能因取消半途丢弃。
    # 函数用途: 获取可选有界等待的原互斥，补齐待恢复提交后开放一次短临界区。
    @contextmanager
    def transaction(self, *, wait_check: Callable[[], None] | None = None) -> Iterator[ProcessSessionTransaction]:
        options = {"wait_check": wait_check} if wait_check is not None else {}
        with locked_private_directory(self.root, lock_name=".process-sessions.lock", **options):
            recover_process_commit(self.root)
            transaction = ProcessSessionTransaction(self.root)
            try:
                yield transaction
            finally:
                transaction.active = False

    # LLM: v2/v3/v4 的提交后异常携带固定回执，调用方不能把异常当成记录没有写入。
    # 函数用途: 在互斥与恢复后保存一条记录，保留既有单条写入 API。
    def write(self, record: dict[str, object]) -> dict[str, object]:
        incoming = validate_process_record(record)
        with self.transaction() as transaction:
            return transaction.write(incoming)

    # LLM: 已存在目录的查询会完成待恢复文件安装；损坏 redo 时拒绝整次查询，不返回部分成功。
    # 函数用途: 读取可信记录，区分缺失、坏记录和已提交待恢复。
    def load(self, session_id: str) -> ProcessSessionLoadReport:
        try:
            self.record_path(session_id)
            if not self.root.exists():
                return ProcessSessionLoadReport({})
            with self.transaction() as transaction:
                return ProcessSessionLoadReport(transaction.load(session_id) or {})
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return ProcessSessionLoadReport(
                {}, process_session_error_report(exc, "process_session_store.load", path=self.root)
            )

    # LLM: 保持原二元组API；公开全量读取始终复制全部记录，私有底座不得缓存过滤后的子集。
    # 函数用途: 返回独占的完整记录与错误，调用者修改不会污染冷热缓存。
    def list_records(
        self, *, cheap_checks: bool = False
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        records, errors = self._load_records_projection_cached(cheap_checks=cheap_checks)
        return [deepcopy(record) for record in records], [deepcopy(error) for error in errors]

    # LLM: 先取完整未过滤快照，再在存储层筛选并只复制命中；空规范目标{}不欠通知，错误不被过滤。
    # 函数用途: 提供完成协调专用查询，避免每tick深拷贝全部历史记录。
    def list_pending_completion_records_report(
        self, *, owner_key: str = "", task_id: str = "", cheap_checks: bool = False
    ) -> ProcessSessionListReport:
        records, errors = self._load_records_projection_cached(cheap_checks=cheap_checks)
        owner, task = str(owner_key or "").strip(), str(task_id or "").strip()
        matched = []
        for record in records:
            target = record.get("completion_target")
            if not isinstance(target, dict) or not target or str(record.get("completion_notice_id") or "").strip():
                continue
            if owner and str(target.get("owner_key") or "").strip() != owner:
                continue
            if task and str(target.get("task_id") or "").strip() != task:
                continue
            matched.append(deepcopy(record))
        return ProcessSessionListReport(matched, [deepcopy(error) for error in errors])

    # LLM: 便捷查询沿同一report实现，不能独立过滤或回写缓存；需读错误的调用方用report入口。
    # 函数用途: 返回已深拷贝的待通知候选列表。
    def list_pending_completion_records(self, *, owner_key: str = "", task_id: str = "", cheap_checks: bool = False) -> list[dict[str, object]]:
        return self.list_pending_completion_records_report(owner_key=owner_key, task_id=task_id, cheap_checks=cheap_checks).records

    # LLM: 私有只读底座永远是全量记录和错误，冷读直接移交新对象；公开API负责复制，False仍强制事务重读。
    #   信任在读前冻结，发布时只核目录身份；不因慢读跨过保护窗口而将旧视图变可信。
    # 函数用途: 复用目录内容投影，不缓存通知结论或暴露可变对象给外部。
    def _load_records_projection_cached(self, *, cheap_checks: bool = False):
        if cheap_checks:
            hit = _lookup_process_records_projection(self.root)
            if hit is not None:
                return hit.records, hit.errors
        before = _process_dir_stat_key(self.root)
        # MC4-1201: 在读取这一刻冻结"本次读取是否可信"——读取前目录 mtime 已离开 2 秒窗口才允许
        #   把结果写进缓存;发布处只认这个事实,不用发布时的目录年龄(跨窗口延迟发布时旧结果不得
        #   变成可信缓存)。
        read_trusted = before is not None and cache_entry_trustworthy(before[2])
        try:
            if not self.root.exists():
                return [], []
            with self.transaction() as transaction:
                records, errors = transaction.list_records()
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return [], [process_session_error_report(exc, "process_session_store.list", path=self.root)]
        if read_trusted:
            _store_process_records_projection(self.root, before, records, errors, take_ownership=True)
        return records, errors

    # LLM: 与预留和交接共用锁；调用方先关闭旧执行轮准入；include_terminal 只扩展同一精确身份的退出证据。
    # 函数用途: 提交任务停止清单，插件准备收口可一并核验终态 host，信号仍在锁外发送。
    def request_stop(self, scope: ProcessExecutionScope, *, include_terminal: bool = False) -> ProcessSessionCommitReceipt:
        if not self.root.exists():
            return ProcessSessionCommitReceipt("", ())
        with self.transaction() as transaction:
            return transaction.request_stop(scope, include_terminal=include_terminal)

    # LLM: 入口只选择已登记资源，不替代安装表撤销；空清单不证明没有尚未登记的启动，必须配合相同准入边界。
    # 函数用途: 冻结一个 owner/插件/代次的共享资源，保持缺失目录查询无副作用。
    def request_stop_activation(self, scope: ProcessActivationScope) -> ProcessSessionCommitReceipt:
        if not isinstance(scope, ProcessActivationScope):
            raise TypeError("managed activation stop requires exact scope")
        if not self.root.exists():
            return ProcessSessionCommitReceipt("", ())
        with self.transaction() as transaction:
            return transaction.request_stop_activation(scope)

    # LLM: 原目录锁先恢复redo；省略数量只做TTL，旧数量调用保持原裁剪，通知未消费无证明时保留。
    # 函数用途: 回收安全的历史终态档案，返回实际删除列表；缺失目录无副作用。
    def prune_finished(self, max_finished: int | None = None, *, max_age_seconds: float | None = 86400.0,
                       now: float | None = None, is_notice_consumed: Callable[[str], bool] | None = None) -> list[str]:
        if self.root.exists():
            with self.transaction() as transaction:
                return transaction.prune_finished(max_finished, max_age_seconds=max_age_seconds, now=now, is_notice_consumed=is_notice_consumed)
        return []


# LLM: 诊断只公开错误分类、路径与已提交身份，不包含记录正文；调用者可以辨别提交后故障。
# 函数用途: 把存储错误转换为统一回执，避免未知安装状态被表现成没有资源。
def process_session_error_report(
    exc: BaseException, context: str, *, path: Path | None = None
) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    if path is not None:
        report["path"] = str(path)
    if isinstance(exc, ProcessSessionCommitPendingError):
        report.update(
            committed=True,
            recovery_required=True,
            transaction_id=exc.receipt.transaction_id,
            session_ids=[record["session_id"] for record in exc.receipt.records],
        )
    return report
