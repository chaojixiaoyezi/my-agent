# LLM: 仅消费正面退休证明；原发布、消费、guidance迁移和锁回收器均不修改，所有变更通过请求写门。
# 模块用途: 有界清理到期handled回执、移动归档和已清空回收站线程的guidance侧记录。
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from ..common.json_io import read_json_object_report
from ..common.nofollow_fs import open_readonly_file_beneath
from ..gateway_parts.io import cleanup_orphan_lock_files, locked_file_transition
from ..memory_store.retired_thread_journal import iter_retired_threads, retired_thread_state
from ..runtime_errors import DataCorruptionError
from ..user_space.storage_lanes import (
    STORAGE_LANE_CURSOR_MAX_BYTES,
    STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT,
    STORAGE_LANE_SLICE_BYTES,
    STORAGE_LANE_SLICE_ENTRIES,
    STORAGE_LANE_SLICE_MS,
    StorageLaneRequest,
    lane_cursor_window,
    owner_conversation_roots,
)
from .models import WakeSignal
from .store_guidance_records import GuidanceOnceReceipt
from .store_layout import ConversationStorage
from .store_wake_publication import (
    _frozen_pair,
    _stable_signal,
    session_task_identity,
    signal_responsibility_guard,
)

# 未被永久幂等记录保护的已处理唤醒回执保留天数。
HANDLED_WAKE_RETENTION_DAYS = 14
# 仅移动归档在满足安全条件后的保留天数。
LEDGER_ARCHIVE_RETENTION_DAYS = 30
# 每批退休线程台账最多读取的物理行数。
LEDGER_GUIDANCE_JOURNAL_BATCH_ROWS = 64
# 主记录按根逐阶段扫描，不能扫pending、attempts或replayed。
_PHASES = ("handled", "ledger_policies", "ledger_claims", "quarantine_archive", "quarantine_unreadable")


# LLM: 保护原因是局部控制流，不授予删除权或启动任何恢复写入。
# 类用途: 将一个候选明确保留并附上原因。
class _Keep(Exception):
    pass


# LLM: 字节或墙钟不足时保留当前续点，不吞掉可能已提交的删除计量。
# 类用途: 结束本片，下一片从同一候选重试。
class _SliceEnd(Exception):
    pass


# LLM: 只给冻结回收器提供本片已枚举的真实Path，不模拟文件或替换锁协议；其余目录候选留给续片。
# 类用途: 将原整目录回收器的枚举范围收窄到有界只读视窗。
class _LockWindow:
    # LLM: 视窗只持当前目录的一条原Path，构造不读写文件。
    # 函数用途: 保存目录与本次已验证候选。
    def __init__(self, directory, path):
        self.directory = directory
        self.path = path

    # LLM: 保持原回收器的目录存在检查，不把缺失或非目录伪装成可回收目录。
    # 函数用途: 返回原目录是否仍存在。
    def is_dir(self):
        return self.directory.is_dir()

    # LLM: 原函数唯一枚举模式冻结为*.lock；若契约变动则明确拒绝，不能意外扩到整个目录。
    # 函数用途: 将原回收器的候选限制在当前真实锁文件。
    def glob(self, pattern):
        if pattern != "*.lock":
            raise ValueError("invalid_ledger_lock_window")
        return (self.path,)


# LLM: 回执只记录本片实际检查和unlink结果；游标由调用者推进，不写维护状态文件。
# 函数用途: 创建一个空的通道计量结果。
def _receipt(cursor: dict) -> dict:
    return {"cursor": cursor, "complete": False, "items": 0, "bytes": 0,
            "deleted_items": 0, "freed_bytes": 0, "protected": Counter(), "skipped": Counter(),
            "errors": [], "backlog": 0}


# LLM: 不能把布尔、NaN或无限大当处理完成时间；无合法时间才回退到双文件时间的较新值。
# 函数用途: 返回正的有限时间，非法值返回零。
def _timestamp(value) -> float:
    return float(value) if type(value) in (int, float) and 0 < value < math.inf else 0.0


# LLM: 比较读取前后完整文件指纹，文件被替换或改写时不得用旧内容授权unlink。
# 函数用途: 提取单链接普通记录的稳定指纹。
def _fingerprint(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 路径始终锚在同一owner内；每个删除都再次lstat，父级链接、硬链接和身份漂移均保留。
# 类用途: 共用安全读取、写门和物理释放计量，不引入持久状态。
class _LedgerPass:
    # LLM: 构造不创建目录或修账；独立helper使用自己的计量，lane共享本片计量和预算。
    # 函数用途: 绑定当前会话根及已有写门。
    def __init__(self, storage, now, run_write, *, owner=None, receipt=None, budget=None):
        self.storage = storage
        self.root = Path(storage.root)
        self.owner = self.root if owner is None else Path(owner)
        self.now = now
        self.run_write = run_write
        self.result = _receipt({}) if receipt is None else receipt
        self.budget = budget
        self.anchor = os.lstat(self.root)

    # LLM: 逐级lstat而不是resolve；目录或叶不是预期类型时抛保护，不跟随另一owner的路径。
    # 函数用途: 返回owner内叶信息，明确不存在才返回None。
    def info(self, path, *, directory=False):
        relative = Path(path).relative_to(self.owner)
        if ".." in relative.parts:
            raise _Keep("unsafe_path")
        current = self.owner
        if not stat.S_ISDIR(os.lstat(current).st_mode):
            raise _Keep("unsafe_path")
        for component in relative.parts[:-1]:
            current /= component
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(info.st_mode):
                raise _Keep("unsafe_path")
        if (self.anchor.st_dev, self.anchor.st_ino) != (os.lstat(self.root).st_dev, os.lstat(self.root).st_ino):
            raise _Keep("root_changed")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise _Keep("unsafe_path")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _Keep("unsafe_path")
        return info

    # LLM: 单个JSON读取也服从字节/时间封顶；解析器不迁移；读取期间换inode或内容则保护。
    # 函数用途: 读一个安全普通JSON文件并计量，坏内容与缺失分开返回。
    def read(self, path, context):
        info = self.info(path)
        if info is None:
            return None, None
        if info.st_size > STORAGE_LANE_SLICE_BYTES:
            raise _Keep("oversize")
        if self.budget is not None:
            if self.budget.remaining_ms() <= 0 or self.result["bytes"] + info.st_size > self.budget.max_bytes:
                raise _SliceEnd
        report = read_json_object_report(path, context=context)
        self.result["bytes"] += info.st_size
        latest = self.info(path)
        if latest is None or _fingerprint(info) != _fingerprint(latest):
            raise _Keep("changed")
        return (None if report.load_error is not None else report.payload), info

    # LLM: 只在成功unlink后计物理释放；ENOENT是已消失而不是本片删除，其他错误原样传播。
    # 函数用途: 再核身份后删除单个普通文件。
    def unlink(self, path, expected):
        latest = self.info(path)
        if latest is None:
            return
        if _fingerprint(latest) != _fingerprint(expected):
            raise _Keep("changed")
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return
        self.result["deleted_items"] += 1
        self.result["freed_bytes"] += latest.st_size

    # LLM: 状态异常只保护当前候选；所有错误路径owner相对且有界，不能伪装成已删除。
    # 函数用途: 收集保护、锁忙和I/O结果。
    def attempt(self, operation, candidate):
        try:
            status = operation(candidate)
        except BlockingIOError:
            status = "busy"
        except _Keep as error:
            status = "kept_" + str(error)
        if status == "busy":
            self.result["skipped"]["lock_busy"] += 1
        elif status == "not_due":
            self.result["skipped"]["not_due"] += 1
        elif status.startswith("kept_"):
            self.result["protected"][status[5:]] += 1
        return status

    # LLM: 坏handled不会因时间够长而删除；满30天只额外报错，保留原始恢复证据。
    # 函数用途: 添加一个owner相对的有界错误。
    def error(self, code, path):
        if len(self.result["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
            self.result["errors"].append({"code": code, "path": str(Path(path).relative_to(self.owner))})

    # LLM: 只按原顶层WakeSignal解析，不用嵌套signal替代权威终态；坏handled_at按合同回退文件年龄。
    # 函数用途: 读取并核对一个handled信号和它的到期条件。
    def handled_record(self, path):
        raw, info = self.read(path, "conversation.ledger_retention.handled")
        if info is None:
            raise _Keep("missing")
        if raw is None:
            if max(info.st_mtime, info.st_ctime) <= self.now - LEDGER_ARCHIVE_RETENTION_DAYS * 86400:
                self.error("handled_wake_unreadable", path)
            raise _Keep("unreadable")
        try:
            signal = WakeSignal.from_dict({**raw, "handled_at": _timestamp(raw.get("handled_at"))})
        except (ValueError, TypeError, KeyError, OverflowError):
            raise _Keep("identity") from None
        if signal.wake_signal_id != path.stem or signal.status != "handled" or not signal.thread_id:
            raise _Keep("identity")
        handled_at = _timestamp(raw.get("handled_at")) or max(info.st_mtime, info.st_ctime)
        return signal, info, handled_at <= self.now - HANDLED_WAKE_RETENTION_DAYS * 86400

    # LLM: 旧格式同代/坏账/永久保护均不能删；不同代只释放旧handled，原v2纯校验器不执行迁移。
    # 函数用途: 返回可成对删除的dedupe及其指纹，否则只允许删旧handled。
    def dedupe_candidate(self, signal):
        if not signal.dedupe_key:
            return None, None
        path = self.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
        raw, info = self.read(path, "conversation.ledger_retention.dedupe")
        if info is None:
            return None, None
        if raw is None:
            raise _Keep("dedupe_unreadable")
        schema = raw.get("schema_version")
        identity = raw.get("wake_signal_id")
        if schema == "wake_dedupe.v1":
            if identity == signal.wake_signal_id:
                raise _Keep("legacy_dedupe")
            if isinstance(identity, str) and identity and raw.get("thread_id") == signal.thread_id and raw.get("dedupe_key") == signal.dedupe_key:
                return None, None
            raise _Keep("dedupe_identity")
        if schema != "wake_dedupe.v2":
            raise _Keep("dedupe_schema")
        try:
            _, frozen = _frozen_pair(raw, signal.thread_id, signal.dedupe_key)
        except (DataCorruptionError, ValueError, TypeError, KeyError):
            raise _Keep("dedupe_identity") from None
        if frozen.wake_signal_id != signal.wake_signal_id:
            return None, None
        if _stable_signal(frozen) != _stable_signal(signal):
            raise _Keep("dedupe_identity")
        if raw["retain_handled"]:
            raise _Keep("retain_handled")
        return path, info

    # LLM: 预读排除无变更候选，写门内按原guard→dedupe锁顺序复读；dedupe必须先于handled删除。
    # 函数用途: 退休一个到期的handled回执，不触碰队列和定位投影。
    def handled(self, name):
        path = self.storage.wake_handled_dir / name
        signal, _, due = self.handled_record(path)
        if not due:
            return "not_due"
        self.dedupe_candidate(signal)
        if session_task_identity(signal) is not None and self.info(self.storage.wake_task_index_path) is None:
            raise _Keep("task_index_unknown")

        # LLM: 每次只持一个信号的领域锁；重读身份和TTL，不因预读合格而信任旧文件。
        # 函数用途: 在写门和原发布锁内执行一次幂等退休。
        def operation():
            with signal_responsibility_guard(self.storage, signal, blocking=False), ExitStack() as locks:
                if signal.dedupe_key:
                    dedupe_path = self.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
                    self.info(dedupe_path)
                    locks.enter_context(locked_file_transition(dedupe_path, blocking=False))
                current, info, due = self.handled_record(path)
                if current.to_dict() != signal.to_dict():
                    raise _Keep("changed")
                if not due:
                    return "not_due"
                dedupe_path, dedupe_info = self.dedupe_candidate(current)
                if dedupe_path is not None:
                    self.unlink(dedupe_path, dedupe_info)
                self.unlink(path, info)
                return "retired_pair" if dedupe_path is not None else "retired"

        return self.run_write(operation)

    # LLM: 归档只按两个文件时间的较新值到期，硬链接和目录不产生删除权。
    # 函数用途: 检查一个移动归档是否已经满30天。
    def archived_info(self, path):
        info = self.info(path)
        if info is None:
            raise _Keep("missing")
        stamp = max(info.st_mtime, info.st_ctime)
        if not math.isfinite(stamp):
            raise _Keep("archive_time")
        return info, stamp <= self.now - LEDGER_ARCHIVE_RETENTION_DAYS * 86400

    # LLM: 预检排除新归档，写门内重核年龄和inode；ENOENT只视为已消失。
    # 函数用途: 清理一个policies/claims或unreadable移动归档。
    def archive(self, path):
        _, due = self.archived_info(path)
        if not due:
            return "not_due"

        # LLM: 无其他领域读者的归档仍须在写门内按当前文件重新确认TTL。
        # 函数用途: 原地删除到期普通归档，不改任何内容。
        def operation():
            info, due = self.archived_info(path)
            if not due:
                return "not_due"
            self.unlink(path, info)
            return "retired"

        return self.run_write(operation)

    # LLM: 即便dedupe内容损坏也保护结案档；只有明确没有dedupe叶时才能删。
    # 函数用途: 核对归档顶层信号及仍存在的发布权威。
    def quarantine_record(self, path):
        raw, info = self.read(path, "conversation.ledger_retention.quarantine")
        if raw is None:
            raise _Keep("quarantine_unreadable")
        try:
            signal = WakeSignal.from_dict(raw)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise _Keep("quarantine_identity") from None
        if signal.wake_signal_id != path.stem or signal.status != "failed_permanently" or not signal.thread_id:
            raise _Keep("quarantine_identity")
        if signal.dedupe_key:
            try:
                candidate = self.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
                if self.info(candidate) is not None:
                    raise _Keep("quarantine_dedupe")
            except (OSError, ValueError, _Keep):
                raise _Keep("quarantine_dedupe") from None
        return signal, info

    # LLM: 原quarantine锁覆盖复读和主档/ledger删除；不列replayed，也不回放任何信号。
    # 函数用途: 删除没有dedupe引用的到期结案归档及其同名坏账。
    def quarantine(self, name):
        path = self.storage.wake_quarantine_archive_dir / name
        _, due = self.archived_info(path)
        if not due:
            return "not_due"
        signal, _ = self.quarantine_record(path)
        lock_path = self.storage.wake_quarantine_path(signal.wake_signal_id)
        self.info(lock_path)

        # LLM: 主档先删、同名ledger后删；任一未知身份保留，不向pending或重放次数写入。
        # 函数用途: 在既有quarantine转换锁内清除一份到期归档。
        def operation():
            with locked_file_transition(lock_path, blocking=False):
                _, due = self.archived_info(path)
                if not due:
                    return "not_due"
                current, info = self.quarantine_record(path)
                if current.to_dict() != signal.to_dict():
                    raise _Keep("changed")
                ledger_path = path.parent / "ledger" / name
                ledger_info = self.info(ledger_path)
                self.unlink(path, info)
                if ledger_info is not None:
                    self.unlink(ledger_path, ledger_info)
                return "retired"

        return self.run_write(operation)

    # LLM: 纯解析回执，绝不调用会回写迁移的GuidanceLedger.read_receipt；非线程、未结算都不删。
    # 函数用途: 读取并核对本批purged线程的已结算guidance。
    def guidance_record(self, path, thread_ids):
        raw, info = self.read(path, "conversation.ledger_retention.guidance")
        if raw is None:
            raise _Keep("guidance_unreadable")
        try:
            receipt = GuidanceOnceReceipt.from_dict(raw)
        except (DataCorruptionError, ValueError, TypeError, KeyError, OverflowError):
            raise _Keep("guidance_unreadable") from None
        if not receipt.dedupe_key or self.storage.guidance_dedupe_path(receipt.dedupe_key) != path:
            raise _Keep("guidance_identity")
        if receipt.entry.target_type != "thread" or receipt.entry.target_id not in thread_ids:
            raise _Keep("guidance_unrelated")
        if receipt.status not in {"consumed", "rejected"}:
            raise _Keep("guidance_unsettled")
        return receipt, info

    # LLM: turn索引必须完全相等；input索引必须同key；坏或未知格式索引均保留并阻止删回执。
    # 函数用途: 收集本回执可删除的精确引用，先读完再执行第一处unlink。
    def guidance_references(self, receipt):
        metadata = receipt.entry.metadata
        turn = str(metadata.get("expected_turn_id") or "").strip()
        input_id = str(metadata.get("gateway_input_request_id") or "").strip()
        references = []
        if turn:
            path = self.storage.guidance_turn_index_path(turn, receipt.dedupe_key)
            raw, info = self.read(path, "conversation.ledger_retention.guidance_turn")
            expected = {"schema_version": "conversation_guidance_turn_ref.v1", "expected_turn_id": turn, "dedupe_key": receipt.dedupe_key}
            if info is not None:
                if raw != expected:
                    raise _Keep("guidance_index_unknown")
                references.append((path, info))
        if input_id:
            path = self.storage.guidance_input_index_path(input_id)
            raw, info = self.read(path, "conversation.ledger_retention.guidance_input")
            if info is not None:
                if raw is None or raw.get("schema_version") != "conversation_guidance_input_ref.v1":
                    raise _Keep("guidance_index_unknown")
                if raw.get("dedupe_key") == receipt.dedupe_key:
                    references.append((path, info))
        return turn, references

    # LLM: journal批必须已有purged证明；写门内仍核同一回执，turn锁先于receipt锁，引用先于权威回执。
    # 函数用途: 退休一个已清空回收站线程的已结算guidance侧记录。
    def guidance(self, name, thread_ids, verify):
        path = self.storage.guidance_dedupe_dir / name
        receipt, _ = self.guidance_record(path, thread_ids)
        if not verify(receipt):
            raise _Keep("journal_changed")
        turn, _ = self.guidance_references(receipt)
        if turn:
            digest = hashlib.sha256(turn.encode("utf-8")).hexdigest()
            for directory in (self.storage.guidance_submission_batches_dir, self.storage.guidance_ack_batches_dir):
                if self.info(directory / digest, directory=True) is not None:
                    self.result["skipped"]["guidance_batches"] += 1

        # LLM: 原精确回合锁保护同回合提交/确认；每次仅持一个receipt锁，读完所有索引后再逐个删。
        # 函数用途: 复读并按引用到权威的顺序执行guidance退休。
        def operation():
            with ExitStack() as locks:
                if turn:
                    turn_lock = self.storage.guidance_turn_index_dir / ("." + hashlib.sha256(turn.encode("utf-8")).hexdigest() + ".transition")
                    self.info(turn_lock)
                    locks.enter_context(locked_file_transition(turn_lock, blocking=False))
                receipt_lock = path.with_name("." + path.name + ".transition")
                self.info(receipt_lock)
                locks.enter_context(locked_file_transition(receipt_lock, blocking=False))
                current, info = self.guidance_record(path, thread_ids)
                if current.to_dict() != receipt.to_dict():
                    raise _Keep("changed")
                _, references = self.guidance_references(current)
                if not verify(current):
                    raise _Keep("journal_changed")
                for reference, reference_info in references:
                    self.unlink(reference, reference_info)
                self.unlink(path, info)
                if turn:
                    directory = self.storage.guidance_turn_index_path(turn, receipt.dedupe_key).parent
                    if self.info(directory, directory=True) is not None:
                        try:
                            directory.rmdir()
                        except OSError:
                            # best-effort: 原JSON锁伴生或同回合其他引用仍在时，保留目录供原清扫器处理。
                            pass
                return "retired"

        return self.run_write(operation)

    # LLM: JSON目录全扫后单独续扫原伴生锁；真正unlink仍由冻结try-flock/inode复核器执行，单片不收集整目录。
    # 函数用途: 有界回收一把移动归档留下的无数据老锁。
    def archive_lock(self, path):
        info = self.info(path)
        if info is None:
            raise _Keep("missing")
        if self.info(path.with_suffix("")) is not None:
            raise _Keep("lock_with_data")
        if info.st_mtime > self.now - 86400:
            return "not_due"

        # LLM: 原清扫器仍自行试锁并复核inode、年龄和data缺失；视窗只限制枚举，不替它决定删除。
        # 函数用途: 在一个写门中复用原单候选孤儿锁回收协议。
        def operation():
            latest = self.info(path)
            if latest is None:
                return "kept_missing"
            if _fingerprint(latest) != _fingerprint(info):
                raise _Keep("changed")
            count = cleanup_orphan_lock_files(_LockWindow(path.parent, path), now=self.now, data_sidecars_only=True)
            if count:
                self.result["deleted_items"] += count
                self.result["freed_bytes"] += latest.st_size
                return "retired"
            return "busy"

        return self.run_write(operation)


# LLM: 独立helper保留冻结字符串结果；lane与helper共用同一个实际实现，不复制删除决策。
# 函数用途: 退休一份到期handled回执。
def _retire_handled(storage, name, *, now, run_write: Callable) -> str:
    scope = _LedgerPass(storage, now, run_write)
    return scope.attempt(scope.handled, name)


# LLM: 只接受约定的两种移动归档目录，不用任意kind拼出删除路径。
# 函数用途: 清理一个无读者的到期策略或claim归档。
def _purge_ledger_archive(root, kind, name, *, now, run_write):
    if kind not in {"policies", "claims"}:
        raise ValueError("invalid_ledger_archive_kind")
    scope = _LedgerPass(ConversationStorage(root, initialize=False), now, run_write)
    return scope.attempt(scope.archive, Path(root) / ".ledger_archive" / kind / name)


# LLM: 保持原quarantine转换锁及dedupe保护，未授权归档只读保留。
# 函数用途: 清理一份到期且没有发布回执引用的quarantine归档。
def _purge_quarantine_archive(storage, name, *, now, run_write):
    scope = _LedgerPass(storage, now, run_write)
    return scope.attempt(scope.quarantine, name)


# LLM: 目录逐级no-follow复核，窗口只保留有限名称；不递归、不建立第二份索引。
# 函数用途: 读取续点之后的一片JSON名称。
def _window(scope, directory, after, limit, *, suffix=".json"):
    if scope.info(directory, directory=True) is None:
        return []
    with os.scandir(directory) as entries:
        return lane_cursor_window({"after": after}, (entry.name for entry in entries if entry.name.endswith(suffix)), limit)


# LLM: 原页API不附行offset且不限字节；先用no-follow FD限长探一行，再以原API按单行纯解析，批最多64行。
#   两次真实读取都计bytes，每行检查同一deadline；屏障、半行或超大行均不推进其水位，不做事后计量截断。
# 函数用途: 在原始字节与时间预算内构造一批正面退休证明。
def _journal_batch(home, cursor, result, limit, budget):
    offset = cursor["journal_offset"]
    path = Path(home.owner_data_dir) / "retention" / "retired_threads.jsonl"
    parts = path.relative_to(home.owner_home_dir).parts
    batch, proofs, examined, complete, deferred = {}, {}, 0, False, ""
    try:
        descriptor = open_readonly_file_beneath(home.owner_home_dir, parts)
    except FileNotFoundError:
        return batch, offset, True, examined, proofs, deferred
    with os.fdopen(descriptor, "rb") as handle:
        handle.seek(offset)
        for _ in range(limit):
            available = budget.max_bytes - result["bytes"]
            if budget.remaining_ms() <= 0 or available < 2:
                deferred = "budget"
                break
            before = os.fstat(handle.fileno())
            raw = handle.readline(available // 2 + 1)
            result["bytes"] += len(raw)
            if not raw:
                complete = True
                break
            if len(raw) > available // 2:
                if available == budget.max_bytes:
                    result["protected"]["journal_oversize"] += 1
                    complete = True
                else:
                    deferred = "budget"
                break
            if not raw.endswith(b"\n"):
                result["protected"]["journal_partial"] += 1
                complete = True
                break
            if budget.remaining_ms() <= 0:
                deferred = "budget"
                break
            if _fingerprint(before) != _fingerprint(os.lstat(path)):
                result["protected"]["journal_changed"] += 1
                complete = True
                break
            page = iter_retired_threads(home, offset=offset, max_rows=1)
            result["bytes"] += max(0, page.next_offset - offset)
            examined += 1
            if page.next_offset - offset != len(raw):
                result["protected"]["journal_changed"] += 1
                complete = True
                break
            if not page.rows:
                offset = page.next_offset
                continue
            if budget.remaining_ms() <= 0:
                deferred = "budget"
                break
            row = page.rows[0]
            state = retired_thread_state(home, row)
            if budget.remaining_ms() <= 0:
                deferred = "budget"
                break
            if state in {"waiting", "unknown"}:
                result["protected"]["journal_" + state] += 1
                complete = True
                break
            if state == "purged":
                root = row["conversation_root"]
                candidate = {key: list(ids) for key, ids in batch.items()}
                candidate.setdefault(root, [])
                if row["thread_id"] not in candidate[root]:
                    candidate[root].append(row["thread_id"])
                if len(json.dumps(candidate).encode("utf-8")) > STORAGE_LANE_CURSOR_MAX_BYTES // 2:
                    result["protected"]["journal_cursor_window"] += 1
                    complete = not batch
                    break
                batch = candidate
                proofs[(root, row["thread_id"])] = row
            offset = page.next_offset
        else:
            complete = offset >= os.fstat(handle.fileno()).st_size
    return batch, offset, complete, examined, proofs, deferred


# LLM: 游标不是删除权；恢复的batch必须重读原journal并重新判定purged，不能只信持久化线程ID。
# 函数用途: 加载或复核本批正面退休证明。
def _prepare_batch(request, cursor, result, budget):
    active = bool(cursor.get("batch"))
    limit = LEDGER_GUIDANCE_JOURNAL_BATCH_ROWS if active else min(LEDGER_GUIDANCE_JOURNAL_BATCH_ROWS, budget.max_items - result["items"])
    batch, next_offset, complete, examined, proofs, deferred = _journal_batch(request.home, cursor, result, limit, budget)
    if active:
        original = cursor["batch"]
        batch = {root: [thread for thread in ids if thread in original.get(root, ())]
                 for root, ids in batch.items() if any(thread in original.get(root, ()) for thread in ids)}
        if batch != original:
            cursor.update(root=0, after="")
        next_offset = min(cursor["journal_next"], next_offset)
        complete = cursor["journal_complete"] if next_offset == cursor["journal_next"] else complete
    else:
        result["items"] += examined
    cursor.update(batch=batch, journal_next=next_offset, journal_complete=complete, journal_deferred=deferred)
    return proofs


# LLM: 外部游标仅提供续点，绝不能注入路径或删除证明；未知阶段从头扫，journal水位只认非负整数。
# 函数用途: 创建当前片的有界游标副本。
def _cursor(request):
    raw = request.cursor
    offset = raw.get("journal_offset", 0)
    offset = offset if type(offset) is int and 0 <= offset < (1 << 63) else 0
    try:
        valid_size = len(json.dumps(raw, allow_nan=False).encode("utf-8")) <= STORAGE_LANE_CURSOR_MAX_BYTES
    except (TypeError, ValueError, OverflowError, RecursionError):
        valid_size = False
    after = raw.get("after", "")
    if not valid_size or not isinstance(after, str) or len(after.encode("utf-8", errors="replace")) > 255:
        raw = {"journal_offset": offset}
    phase = raw.get("phase", "handled")
    phase = phase if phase in (*_PHASES, "guidance") else "handled"
    root = raw.get("root", 0)
    result = {"root": root if type(root) is int and 0 <= root < (1 << 63) else 0, "phase": phase,
              "after": raw.get("after", "") if isinstance(raw.get("after", ""), str) else "",
              "journal_offset": offset, "batch": {}}
    batch = raw.get("batch")
    next_offset = raw.get("journal_next", offset)
    if (phase == "guidance" and isinstance(batch, dict) and batch
            and all(isinstance(key, str) and isinstance(ids, list) and all(isinstance(value, str) for value in ids) for key, ids in batch.items())
            and type(next_offset) is int and offset <= next_offset < (1 << 63)):
        result.update(batch={key: list(ids) for key, ids in batch.items()}, journal_next=next_offset,
                      journal_complete=raw.get("journal_complete") is True,
                      journal_deferred=raw.get("journal_deferred") if raw.get("journal_deferred") in {"budget", "journal_partial"} else "")
    if phase in {"ledger_policies", "ledger_claims"} and raw.get("locks") is True:
        result["locks"] = True
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > STORAGE_LANE_CURSOR_MAX_BYTES:
        return {"root": 0, "phase": "handled", "after": "", "journal_offset": offset, "batch": {}}
    return result


# LLM: 完整一轮后只保留journal确认水位；其他阶段回到起点，下轮才能重试busy或尚未到期记录。
# 函数用途: 标记全根阶段完成但不消费任何未知证明。
def _finish(result):
    result["complete"] = True
    result["cursor"] = {"root": 0, "phase": "handled", "after": "",
                        "journal_offset": result["cursor"]["journal_offset"], "batch": {}}
    return result


# LLM: 单个冻结请求入口；legal_hold零I/O零commit，单片封顶128条/8MiB/25ms，所有变更沿原写门和原锁。
# 函数用途: 逐根逐阶段清理会话侧账，并通过回执续扫退休证明。
def maintain_conversation_ledgers_lane(request: StorageLaneRequest) -> dict:
    if request.legal_hold:
        return {"cursor": dict(request.cursor), "complete": True, "items": 0, "bytes": 0, "deferred_reason": "legal_hold"}
    cursor = _cursor(request)
    result = _receipt(cursor)
    if request.budget.exhausted():
        result["deferred_reason"] = "budget"
        return result
    budget = request.budget.capped(items=STORAGE_LANE_SLICE_ENTRIES, size=STORAGE_LANE_SLICE_BYTES, ms=STORAGE_LANE_SLICE_MS)
    owner = Path(request.home.owner_home_dir)
    roots = owner_conversation_roots(request.home)
    scopes = {}
    proofs = {}

    # LLM: 构造只读布局，同片同根共用计量和预算；未知根路径不能逃出owner。
    # 函数用途: 取得一个当前片会话根的安全操作上下文。
    def scope_for(root):
        if root not in scopes:
            scopes[root] = _LedgerPass(ConversationStorage(root, initialize=False), request.now, request.run_write,
                owner=owner, receipt=result, budget=budget)
            scopes[root].info(root, directory=True)
        return scopes[root]

    while not budget.exhausted(result["items"], result["bytes"]):
        phase = cursor["phase"]
        if phase == "guidance":
            try:
                if not cursor["batch"] or not proofs:
                    proofs = _prepare_batch(request, cursor, result, budget)
                if budget.exhausted(result["items"], result["bytes"]):
                    result["deferred_reason"] = "budget"
                    return result
                if not cursor["batch"]:
                    cursor["journal_offset"] = cursor.pop("journal_next")
                    if cursor.pop("journal_complete"):
                        return _finish(result)
                    deferred = cursor.pop("journal_deferred", "")
                    if deferred:
                        result["deferred_reason"] = deferred
                        return result
                    continue
                batch_roots = sorted(cursor["batch"])
                if cursor["root"] >= len(batch_roots):
                    cursor["journal_offset"] = cursor.pop("journal_next")
                    complete = cursor.pop("journal_complete")
                    deferred = cursor.pop("journal_deferred", "")
                    cursor.update(batch={}, root=0, after="")
                    if complete:
                        return _finish(result)
                    if deferred:
                        result["deferred_reason"] = deferred
                        return result
                    continue
                root_rel = batch_roots[cursor["root"]]
                root = owner / root_rel
                scope = scope_for(root)
                directory = scope.storage.guidance_dedupe_dir
                # LLM: 当前根只能消费当前批的purged线程集合，不以其他根同名线程授权。
                # 函数用途: 处理这一根的一条guidance回执。
                def operation(name):
                    # LLM: 原证明对象和当前trash/thread四态均须仍有效；不以一次批预读代替提交前授权。
                    # 函数用途: 在第一处unlink前再次核验当前线程仍已purged。
                    def verify(receipt):
                        row = proofs.get((root_rel, receipt.entry.target_id))
                        return row is not None and retired_thread_state(request.home, row) == "purged"

                    return scope.guidance(name, cursor["batch"][root_rel], verify)
            except (OSError, ValueError, _Keep) as error:
                result["protected"]["journal_unknown"] += 1
                if isinstance(error, OSError):
                    result["errors"].append({"code": "retired_journal_unreadable", "path": "data/retention/retired_threads.jsonl"})
                return _finish(result)
        else:
            if cursor["root"] >= len(roots):
                cursor.update(root=0, phase="guidance", after="")
                continue
            root = roots[cursor["root"]]
            try:
                scope = scope_for(root)
            except (OSError, ValueError, _Keep):
                result["protected"]["unsafe_root"] += 1
                cursor.update(root=cursor["root"] + 1, phase="handled", after="")
                continue
            directories = {"handled": scope.storage.wake_handled_dir,
                "ledger_policies": root / ".ledger_archive" / "policies",
                "ledger_claims": root / ".ledger_archive" / "claims",
                "quarantine_archive": scope.storage.wake_quarantine_archive_dir,
                "quarantine_unreadable": scope.storage.wake_quarantine_archive_dir / "unreadable"}
            directory = directories[phase]
            operation = (scope.handled if phase == "handled" else scope.quarantine if phase == "quarantine_archive"
                         else lambda name: scope.archive(directory / name))
        locking = phase in {"ledger_policies", "ledger_claims"} and cursor.get("locks") is True
        if locking:
            # LLM: JSON全扫之后按同一有限窗口续扫伴生锁，每把都走冻结回收器原协议。
            # 函数用途: 处理当前归档目录的一把老孤儿锁。
            def operation(name):
                return scope.archive_lock(directory / name)

        limit = min(STORAGE_LANE_SLICE_ENTRIES, budget.max_items - result["items"])
        if limit <= 0:
            break
        try:
            names = _window(scope, directory, cursor["after"], limit, suffix=".json.lock" if locking else ".json")
        except (OSError, ValueError, _Keep):
            result["protected"]["unsafe_directory"] += 1
            if phase == "guidance":
                scope.error("guidance_directory_unknown", directory)
                result["backlog"] += 1
                result["deferred_reason"] = "guidance_retry"
                return result
            names = []
        for name in names:
            if budget.exhausted(result["items"], result["bytes"]):
                result["backlog"] += 1
                return result
            result["items"] += 1
            try:
                status = scope.attempt(operation, name)
                if phase == "guidance" and status in {"busy", "kept_journal_changed"}:
                    result["backlog"] += 1
                    result["deferred_reason"] = "guidance_retry"
                    return result
            except _SliceEnd:
                result["backlog"] += 1
                return result
            except (OSError, ValueError, DataCorruptionError):
                scope.error("ledger_retirement_failed", directory / name)
                if phase == "guidance":
                    result["backlog"] += 1
                    result["deferred_reason"] = "guidance_retry"
                    return result
            cursor["after"] = name
        if len(names) < limit:
            if phase in {"ledger_policies", "ledger_claims"} and not locking:
                cursor.update(locks=True, after="")
                continue
            cursor.pop("locks", None)
            cursor["after"] = ""
            if phase == "guidance":
                cursor["root"] += 1
            else:
                next_phase = _PHASES.index(phase) + 1
                if next_phase == len(_PHASES):
                    cursor.update(root=cursor["root"] + 1, phase="handled")
                else:
                    cursor["phase"] = _PHASES[next_phase]
    result["deferred_reason"] = "budget"
    return result
