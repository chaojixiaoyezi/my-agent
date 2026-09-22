# LLM: 原 Store 是 v2/v3 session 唯一权威，锁内先恢复 redo；任务/激活分别冻结，共享资源退出证据不参与普通裁剪。
# 模块用途: 为 launcher、host 和 Gateway 串行管理预留与停止意图，保留原目录、CAS 和 v1 显式句柄权限。
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.directory_lock import locked_private_directory
from ..common.json_io import locked_json_path, write_json_file_atomic_unlocked
from ..runtime_errors import runtime_error_report
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
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
    merge_process_record,
    validate_process_record,
    validate_session_id,
)


# LLM: 读取损坏与记录不存在必须区分；异常不能暴露原始命令、环境或记录内容。
# 类用途: 返回一条可信记录或结构化读取错误，供查询层判断能否继续使用该句柄。
@dataclass(frozen=True)
class ProcessSessionLoadReport:
    record: dict[str, object]
    load_error: dict[str, object] | None = None


# LLM: 过期 v2/v3 写入不得通过生命周期合并绕过 CAS；调用方应重读原句柄而不是启动新进程。
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

    # LLM: v1 沿原逐记录锁，v2/v3 共用 revision CAS 和 redo；旧版本原样更新，不能迁移或覆盖坏记录。
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

    # LLM: scope 必须是宿主冻结身份；坏记录使整批拒绝，v1/共享激活/其他任务不参加，回执只含固定集合。
    # 函数用途: 在同一锁内选中任务当前的 v2/v3 资源并提交停止意图，不发送进程信号。
    def request_stop(self, scope: ProcessExecutionScope) -> ProcessSessionCommitReceipt:
        records, errors = self.list_records()
        if errors:
            raise ValueError("damaged managed process records prevent complete stop selection")
        selected = [
            record
            for record in records
            if record["schema"] in MANAGED_PROCESS_SESSION_SCHEMAS
            and record.get("activation_scope") is None
            and record["status"] not in PROCESS_TERMINAL_STATUSES
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
        selected = [record for record in records if record["schema"] == PROCESS_SESSION_SCHEMA
                    and record["activation_scope"] == asdict(scope)]
        transitions = [self._prepare_managed({**record, "stop_requested": True}) for record in selected]
        return self._commit(transitions) if transitions else ProcessSessionCommitReceipt("", ())

    # LLM: 只删普通任务已确认且通知消费的终态；共享激活保留退出证据，必须由插件管理完成收口后明确释放。
    # 函数用途: 限制普通后台历史数量，不把已退出插件记录丢失当作清理完成。
    def prune_finished(self, max_finished: int) -> None:
        records, _errors = self.list_records()
        finished = [
            record
            for record in records
            if record["status"] in PROCESS_TERMINAL_STATUSES
            and record.get("activation_scope") is None
            and (not record.get("completion_target") or record.get("completion_notice_id"))
        ]
        finished.sort(
            key=lambda item: float(item.get("finished_at") or item.get("started_at") or 0),
            reverse=True,
        )
        for record in finished[max(0, int(max_finished)) :]:
            path = self.root / f"{record['session_id']}.json"
            with locked_json_path(path):
                current = self.load(record["session_id"])
                if current == record:
                    path.unlink(missing_ok=True)


# LLM: 公共入口共用原目录锁和恢复；v2/v3 原版本更新，v1 不参与按任务控制，共享激活另按完整归属冻结。
# 类用途: 提供跨进程的后台会话存取，以及需要持锁多检查点的启动与停止事务。
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

    # LLM: v2/v3 的提交后异常携带固定回执，调用方不能把异常当成记录没有写入。
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

    # LLM: 只有普通 session 损坏可返回健康子集；事务日志损坏影响整个视图，必须返回空集及错误。
    # 函数用途: 读取同一目录中的后台会话，并保留结构化错误。
    def list_records(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        try:
            if not self.root.exists():
                return [], []
            with self.transaction() as transaction:
                return transaction.list_records()
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return [], [process_session_error_report(exc, "process_session_store.list", path=self.root)]

    # LLM: 与预留和交接共用锁；调用方必须先关闭旧执行轮准入，再冻结资源，不能用本方法替代任务取消。
    # 函数用途: 提交精确任务的停止清单，供控制层在锁外清理这些资源。
    def request_stop(self, scope: ProcessExecutionScope) -> ProcessSessionCommitReceipt:
        if not self.root.exists():
            return ProcessSessionCommitReceipt("", ())
        with self.transaction() as transaction:
            return transaction.request_stop(scope)

    # LLM: 入口只选择已登记资源，不替代安装表撤销；空清单不证明没有尚未登记的启动，必须配合相同准入边界。
    # 函数用途: 冻结一个 owner/插件/代次的共享资源，保持缺失目录查询无副作用。
    def request_stop_activation(self, scope: ProcessActivationScope) -> ProcessSessionCommitReceipt:
        if not isinstance(scope, ProcessActivationScope):
            raise TypeError("managed activation stop requires exact scope")
        if not self.root.exists():
            return ProcessSessionCommitReceipt("", ())
        with self.transaction() as transaction:
            return transaction.request_stop_activation(scope)

    # LLM: 裁剪前必须恢复，错误不能被忽略后继续删除可能属于未完成事务的记录。
    # 函数用途: 删除超出保留数的旧终态 session，启动中和未知资源持续可见。
    def prune_finished(self, max_finished: int) -> None:
        if self.root.exists():
            with self.transaction() as transaction:
                transaction.prune_finished(max_finished)


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
