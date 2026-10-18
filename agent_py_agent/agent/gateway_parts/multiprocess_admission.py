# LLM: B1b P3 的 C 侧持久准入：单一临界区里先把预留原子写+fsync 持久化，再发布可认领的盖章路由；
#   占用只认还没释放的预留，释放只凭规范终态（线程返回、目录移动、responses 投影都不算）。
#   mc3-p3-align：会话键统一走 mc1 冻结投影（MC4-1502），预留结构写 v2、v1 旧记录照常可读（读旧写新）；
#   跨代转换只走共同授权门（接手证明 + 屏障结果 + 持锁 C 上下文），自动路径跨代一律拒绝。
#   mc2 review26：released 缓存核五元身份/ctime/racy；每 gate 首写整链 fsync；v1 回退仅在迁移适配层。
#   默认单进程（count=0）不经过本模块：工厂函数立即返回 None，不读状态、不建目录、零 I/O。
#   管理员版本只消费首次prepared内的冻结值，不独立读绑定文件，不在route发表时改写完整metadata或指纹。
#   已有输入回执时先核原prepared归属和完整摘要，再写预留；旧无回执路径不伪造新身份凭据。
# LLM: cold C的真实ControlPlane仅准元数据，原prepared身份核实后拒C route，不建P3或claim；实际Agent gate/Count0与原终态、配额、同Root短门不变。
#   ordinary新建/同代补齐只在末发表消费原retain绑定的Root门，与stop线性化；共用恢复/终态链不改。
# 模块用途: 提供原准入预留、盖章、规范终态释放和认领校验；新责任遵守原Root关闭门，控制面不充当业务Agent。
from __future__ import annotations

import copy
import errno
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import serialize_private_json_no_newline
from ..common.nofollow_fs import ensure_private_dir
from ..runtime_errors import DataCorruptionError
from ..user_space.owner_resolver import OwnerIdentity
from .io import (
    gateway_turn_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from .multiprocess_admission_fs import (
    confirm_reservation_chain,
    released_cache_is_stable,
    reservation_signature,
)
from .multiprocess_records import (
    MultiprocessRecordError,
    deserialize_record,
    record_digest,
    release_admission,
    serialize_record,
    validate_record,
)
from .multiprocess_route import RouteStamp, freeze_request_route, owner_digest
from .multiprocess_session_key import project_admission_session, validate_session_scope

# LLM: 规范路径冻结为 <gateway_root>/admission/reservations/<request_id>.json；换路径必须同步协议 §17 与测试。
ADMISSION_DIR_NAME = "admission"
RESERVATIONS_DIR_NAME = "reservations"
# LLM: 读不出的预留不是"没有预留"：占位但不允许被覆盖，也不允许算成零（协议 D4′）。
_UNREADABLE = object()
# LLM: 有界对账（mc4-2103）：已确认 released 且文件未变化的条目跳过读盘，用独立哨兵与
#   None（不存在）/_UNREADABLE（读不出）区分，避免调用方把"跳过"误当"没有"。
_SCAN_SKIPPED = object()


# LLM: 路径只由 C 固定的队列根推导，不从请求或配置重选；默认模式不会走到这里。
# 函数用途: 返回准入预留目录。
def reservations_dir(paths) -> Path:
    return paths.root / ADMISSION_DIR_NAME / RESERVATIONS_DIR_NAME


# LLM: 文件名即请求身份（claim、锁、终态归档共用同一 id），不允许请求正文改写目标路径。
# 函数用途: 返回某个请求的预留文件路径。
def reservation_file(paths, request_id: str) -> Path:
    return reservations_dir(paths) / f"{request_id}.json"


# LLM: 【mc3-p3-align】准入会话键投影的唯一来源是 mc1 冻结的 multiprocess_session_key（1502）；
#   本函数只做"从请求结构化字段到投影输入"的适配：三元组复用原顺序键的同一提取
#   （request_conversation_parts，含身份回退与渠道缺省语义），无外部会话时投影为显式 null
#   （不额外串行但仍计 owner 占用），不物化 owner、不从用户身份重推、不猜默认。P3 不再自算摘要。
# 函数用途: 从请求结构化字段提取准入会话投影；输入不是请求对象时返回 None（调用方暂停）。
def session_scope_for_request(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    from .request_worker import request_conversation_parts, request_user_key

    parts = request_conversation_parts(payload)
    if parts:
        return project_admission_session(*parts)
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    channel = str(conversation.get("channel") or "gateway").strip() or "gateway"
    return project_admission_session(request_user_key(payload), channel, "")


# LLM: 请求侧会话范围只认 mc1 冻结投影（multiprocess_session_scope.v1）；v1 明文形状只用于读旧
#   预留，不能作为新准入输入。校验失败一律按"确定不了"暂停，不猜也不清洗。
# 函数用途: 判断会话范围是否为合法可持久化的 mc1 投影。
def _valid_session_scope(scope: object) -> bool:
    try:
        validate_session_scope(scope)
    except MultiprocessRecordError:
        return False
    return True


# LLM: C 的当前代次沿用 B1a 约定（gateway_state.json 的 c_run_id，worker 就绪校验读同一字段）；
#   读不到时返回空串，让准入 fail-closed 暂停，不猜代次也不放行。
# 函数用途: 读取内部多进程 C 的当前运行代次。
def _read_coordinator_run_id(paths) -> str:
    report = read_json_file_report(paths.state, context="gateway.admission.coordinator.read")
    value = report.payload.get("c_run_id") if isinstance(report.payload, dict) else None
    return value.strip() if isinstance(value, str) else ""


# LLM: 预留必须先于路由持久化（协议 D4′ 固定锁序），所以写文件要自己保证 fsync 到磁盘；
#   既有原子写原语不带 fsync，这里只为本模块实现，不改公共写路径。
#   mc4-2102：首次创建目录链时先补目录项耐久（见 _ensure_durable_parent_chain），再写预留。
# 函数用途: 以 0600 临时文件 + fsync + rename + 目录链 fsync 持久化一份预留记录。
def _write_record_durable(path: Path, record: dict) -> None:
    content = serialize_record(record)
    _ensure_durable_parent_chain(path.parent)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_bytes_durable(tmp, content)
        _replace_with_retry(tmp, path)
        _fsync_directory(path.parent)
    finally:
        _unlink_tmp_file(tmp)


# LLM: mc4-2102：ensure_private_dir 只保证"创建成功"，不保证新建目录项在崩溃后仍在；
#   对每个新建段（从最近已存在祖先往下）fsync 其父目录，让整条 admission/reservations 链的
#   目录项落盘。稳态（目录都在）missing 为空，零额外 fsync；符号链接按 nofollow_fs 同一锚点语义。
# 函数用途: 确保目录存在，并把新建段的父目录逐级 fsync（浅→深）。
def _ensure_durable_parent_chain(directory: Path) -> None:
    missing = []
    current = directory
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    ensure_private_dir(directory)
    for created in reversed(missing):
        _fsync_directory(created.parent)


# LLM: 先写满再 fsync 文件本身，崩溃最多留下未 rename 的临时文件，不会出现半个预留。
#   os.write 允许部分写（mc4-1703）：必须按返回长度循环写完，零进度视为失败，不能带着损坏字节继续。
# 函数用途: 把完整字节写入新文件并 fsync。
def _write_bytes_durable(tmp: Path, content: bytes) -> None:
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all_bytes(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# LLM: 循环写在独立函数里，既保住"按返回长度写满"又让嵌套保持浅层（mc4-1703）。
# 函数用途: 反复调用单次写，直到全部字节落盘。
def _write_all_bytes(descriptor: int, content: bytes) -> None:
    written = 0
    total = len(content)
    while written < total:
        written += _write_once(descriptor, content, written)


# LLM: 单次 os.write 允许部分写；零进度是持久化失败而不是"写完了"，必须抛错。
# 函数用途: 写一段并返回实际字节数；零进度抛 OSError。
def _write_once(descriptor: int, content: bytes, offset: int) -> int:
    chunk = os.write(descriptor, content[offset:])
    if not chunk:
        raise OSError(errno.EIO, "short write while persisting admission reservation")
    return chunk


# LLM: 目录项落盘才是 rename 可见性的保证；Windows 没有目录 fsync 这一原语，按平台跳过。
#   fsync 失败（如 EIO）绝不能吞掉（mc4-1703）：耐久性未确认就不算跨过 A1，由调用方保留不确定事实、停止发布。
# 函数用途: fsync 一个目录，让新文件名持久；失败向上抛。
def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# LLM: rename 偶发 PermissionError（同步/杀毒扫描）按既有 json_io 口径短暂重试；失败向上抛。
# 函数用途: 把临时文件替换为目标路径，最多重试 8 次。
def _replace_with_retry(tmp: Path, path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    if last_error is not None:
        raise last_error


# LLM: 删除临时文件是清理动作，失败不影响已完成的替换或调用方的重试语义。
# 函数用途: 尽力删除临时文件。
def _unlink_tmp_file(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


# LLM: 预留读不出的唯一含义是"占位且不可覆盖"；只有不存在才是真的没有预留。
#   mc4-1702：读取必须限定准入预留 schema 和文件身份——其它种类的合法记录（如 C 代次）不能冒充
#   "不占位"，文件名与记录 request_id 不一致的也不能当有效预留。
#   mc3-p3-align：v1（旧明文会话）与 v2（mc1 投影）都照常可读；新写入一律 v2。
# 函数用途: 读取并校验一份准入预留；返回记录、None（不存在）或 _UNREADABLE 哨兵。
def _read_reservation(path: Path) -> dict | None | object:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE
    try:
        record = deserialize_record(content)
    except MultiprocessRecordError:
        return _UNREADABLE
    if record.get("schema_version") not in {
        "multiprocess_admission_reservation.v1",
        "multiprocess_admission_reservation.v2",
    }:
        return _UNREADABLE
    if record.get("request_id") != path.stem:
        return _UNREADABLE
    return record


# LLM: 快照内会话匹配键分版本：v2 投影用摘要键（显式 null 不额外串行，返回 None 不参与计数）；
#   v1 遗留用 (channel, thread_id) 明文键，与请求侧旧键查找保持同形，升级窗口不漏判同会话。
# 函数用途: 返回预留会话范围的计数键；null 投影返回 None（不参与会话计数）。
def _session_key(scope: dict) -> object:
    if scope.get("schema_version") == "multiprocess_session_scope.v1":
        return scope.get("key_sha256") or None
    return (str(scope.get("channel") or ""), str(scope.get("thread_id") or ""))


# LLM: 请求侧会话查找键 = v2 摘要键（若有）+ v1 遗留明文键（若有）的并集；新旧记录跨版本
#   不能互相比较；查当前摘要、v1 明文、旧回退渠道的升级摘要并集，避免迁移后漏判同会话。
# 函数用途: 返回一次请求在占用快照里要检查的会话键元组（null 投影不产生键）。
def _session_lookup_keys(payload: dict, scope: dict) -> tuple:
    keys = []
    key = _session_key(scope)
    if key is not None:
        keys.append(key)
    parts = _legacy_session_parts(payload)
    if parts:
        keys.append((parts[1], parts[2]))
        keys.append(_session_key(project_admission_session(*parts)))
    return tuple(keys)


# LLM: 复用核对分版本（读旧写新）：v2 精确匹配当前投影或同请求的旧渠道升级投影；v1 遗留
#   用旧 channel 回退适配核对 v1，升级的 v2 投影保持该渠道；v2 只接受当前或同一旧渠道投影。
#   不改 mc1 顺序键缺省值；核对不了（无外部会话、字段不同）返回 None，让调用方暂停。
# 函数用途: 返回 None（不匹配）/False（v2 已匹配）/True（v1 匹配且需升级写回）。
def _legacy_scope_matches(payload: dict, record: dict, session_scope: dict):
    if record.get("schema_version") == "multiprocess_admission_reservation.v2":
        parts = _legacy_session_parts(payload)
        scopes = (session_scope, project_admission_session(*parts) if parts else None)
        if record.get("session_scope") in scopes:
            return False
        return None
    parts = _legacy_session_parts(payload)
    scope = record.get("session_scope") or {}
    if not parts or parts[1] != scope.get("channel") or parts[2] != scope.get("thread_id"):
        return None
    return True


# LLM: v1 旧渠道为 conversation.channel → metadata.channel → 空，不把 mc1 的 gateway 缺省盖回旧事实；
#   用户/外部会话边界仍取冻结提取器，只对旧预留查询、核对及升级做适配，不改请求正文或顺序键。
# 函数用途: 提取旧明文会话对应的投影输入；旧渠道或外部会话缺失返回空元组。
def _legacy_session_parts(payload: dict) -> tuple:
    from .request_worker import request_conversation_parts

    parts = request_conversation_parts(payload)
    if not parts:
        return ()
    conversation = payload.get("conversation") or {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    channel = str(conversation.get("channel") or metadata.get("channel") or "").strip()
    return (parts[0], channel, parts[2]) if channel else ()


# LLM: mc4-1706：归属解析只读取顶层 user_id、metadata 与 conversation 三个结构；把它们的稳定投影
#   在锁外快照与锁内正文之间比较，任何实质变化（会话迁移、换人）都暂停，而不是沿用锁外结果。
# 函数用途: 返回归属相关字段的规范投影字符串。
def _ownership_projection(payload: dict) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    return json.dumps(
        {"user_id": payload.get("user_id"), "metadata": metadata, "conversation": conversation},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


# LLM: mc4-1701：代次是重新盖章中唯一允许变化的字段；其余字段（count/owner/worker/stamped_at/binding）
#   必须逐字段一致，比较时先剥掉代次，避免"仅代次不同"被误判为冲突。
# 函数用途: 返回去掉代次字段后的路由投影。
def _route_without_generation(route: dict) -> dict:
    return {key: value for key, value in route.items() if key != "c_run_id"}


# LLM: 分区数/代次来自可信C启动；管理员版本逐请求消费原prepared值，不在此另存热配置快照。
# 类用途: 保存准入分区来源，不能替请求补身份、版本或执行许可。
class CoordinatorAdmissionScope:
    # LLM: 仅保存已冻结启动事实，不读取状态/绑定文件或建立owner资源；实际准入另核原租约。
    # 函数用途: 构造一个准入来源值，不产生持久副作用。
    def __init__(self, count: int, c_run_id: str) -> None:
        self.count = count
        self.c_run_id = c_run_id


# LLM: 占用只认未释放预留；读不出的预留占位（计入 total）但不归因到具体 owner/会话，
#   也不能被覆盖或删除。旧代与隔离分区的预留同样计入，不因代次不同而忽略。
#   正式ordinary末发表还须原Root单次登记，不以closing阻断既有责任收尾；同步关闭门及恢复回归。
# 类用途: C 的持久准入门：按原责任账预留和盖章，新责任与停止共门，规范终态释放和认领校验仍独立。
class PersistentAdmission:
    # LLM: 只初始化进程内观察缓存；不读取或创建目录、不保存另一绑定版本，新实例不能继承另一个gate的耐久确认。
    # 函数用途: 冻结 C 的来源并初始化本实例的扫描和目录链观察。
    def __init__(self, paths, agent, scope: CoordinatorAdmissionScope, observer=None) -> None:
        self.paths = paths
        self.agent = agent
        self.count = int(getattr(scope, "count", 0) or 0)
        self.c_run_id = str(getattr(scope, "c_run_id", "") or "")
        self.observer = observer
        # 仅存已读 released 的五元身份和读前时刻，不跨 gate/目录共享。
        self._released_scan_cache: dict[str, tuple[tuple[int, ...], int]] = {}
        self._durable_chain = None

    # LLM: 代次缺失说明 C 还没有完成当前代次发布（P4 之前），此时必须 fail-closed 暂停准入，
    #   不能拿空代次盖章（worker 校验会拒绝，且旧代预留无法归因）。
    # 函数用途: 判断本门是否已具备盖章所需的 C 代次。
    def ready(self) -> bool:
        return bool(self.c_run_id)

    # LLM: 观察者只用于测试注入屏障；异常必须原样传播（模拟崩溃点），不能被吞掉。
    # 函数用途: 向注入的观察者发送一个结构化事件。
    def observe(self, event: str, payload: dict) -> None:
        if self.observer is not None:
            self.observer(event, dict(payload))

    # LLM: 占用 = 未释放预留数（含读不出的）；不枚举 pending/processing，所以 worker 移动目录
    #   不会造成漏算。released 不再占用。
    # 函数用途: 扫描预留目录并汇总当前占用快照。
    def occupied_snapshot(self) -> dict:
        snapshot = {"total": 0, "per_owner": {}, "per_session": {}, "unreadable": 0}
        directory = reservations_dir(self.paths)
        if not directory.is_dir():
            return snapshot
        for path in sorted(directory.glob("*.json")):
            _count_reservation(self, path, snapshot)
        return snapshot

    # LLM: 固定T→运行时内存→最终生命周期门；等T不占内存门，返回前在同区精确登记自身成功提交。
    #   同一请求重排或
    #   重新盖章复用原预留不重复扣；读不出的预留不能被覆盖；代次缺失或归属不明一律暂停。
    #   mc4-1706：snapshot 是锁外扫描到的请求正文，锁内用同一投影核对归属输入未漂移。
    # 函数用途: 在请求锁内为一次派发完成预留与盖章，返回 admitted/blocked/raced 决策。
    def admit(self, request_path: Path, ownership, limits, snapshot: dict | None = None) -> dict:
        if not self.ready():
            return _blocked("coordinator_generation_unavailable")
        owner, session_scope = ownership if ownership else (None, None)
        if not isinstance(owner, OwnerIdentity):
            return _blocked("owner_scope_unavailable")
        runtime_rejection = _runtime_owner_rejection(self, owner)
        if runtime_rejection:
            return runtime_rejection
        if not _valid_session_scope(session_scope):
            return _blocked("session_scope_unavailable")
        with gateway_turn_transition(self.paths, request_path.stem), _runtime_decision_scope(self):
            rejection = _runtime_owner_rejection(self, owner)
            if rejection:
                return rejection
            decision = self._admit_locked(request_path, ownership, limits, snapshot)
            return _remember_runtime_commit(self, request_path, decision)

    # LLM: 锁内重读请求文件（并发等待信号写者可能已更新字段），再决定复用、新建或暂停；
    #   文件已被移走返回 raced，不写任何预留。mc4-1706：锁内重算会话归属并要求与锁外一致，
    #   不一致或算不出都暂停（session_scope_unavailable / request_scope_drift），不能拿旧结果扣名额。
    #   有原InputReceipt时先核完整输入/typed owner，不从被改队列重新自洽签名；无receipt维持原显式旧合同，不补账。
    # LLM: ControlPlane元数据host不能成为C执行者；在原prepared与归属校验后、任何新P3之前仅作限制性拒绝，原P2隔离原因仍由外层先返回。
    # 函数用途: 请求锁内核实原输入并作准入决策，cold C路由保持pending，不借控制面执行本地业务。
    def _admit_locked(self, request_path: Path, ownership, limits, snapshot) -> dict:
        owner, session_scope = ownership
        request_id = request_path.stem
        report = read_json_file_report(request_path, context="gateway.admission.request.read")
        current = report.payload if report.load_error is None else {}
        if not current:
            return {"status": "raced", "reason": "request_moved"}
        if str(current.get("id") or "") != request_id:
            return _blocked("request_identity_conflict")
        from .input_delivery_service import require_gateway_prepared_identity

        try:
            require_gateway_prepared_identity(self.paths, current)
        except BlockingIOError:
            return _blocked("input_receipt_busy")
        except OSError:
            return _blocked("prepared_input_unavailable")
        except (ValueError, DataCorruptionError):
            return _blocked("prepared_input_invalid")
        locked_scope = session_scope_for_request(current)
        if locked_scope is None:
            return _blocked("session_scope_unavailable")
        if locked_scope != session_scope:
            return _blocked("request_scope_drift")
        if snapshot is not None and _ownership_projection(snapshot) != _ownership_projection(current):
            return _blocked("request_scope_drift")
        from .control_plane import GatewayControlPlane
        from .multiprocess_route import owner_worker

        if type(self.agent) is GatewayControlPlane and owner_worker(owner, self.count) == "C":
            return _blocked("owner_scope_unavailable")
        existing = _read_reservation(reservation_file(self.paths, request_id))
        if existing is _UNREADABLE:
            return _blocked("reservation_unreadable")
        if existing is None:
            return self._create_and_publish(request_path, current, ownership, limits)
        if existing.get("state") == "released":
            return _blocked("reservation_released")
        return self._reuse_and_publish(request_path, current, existing, ownership)

    # LLM: 新建准备不持生命周期门；最终写入共撤权门并重核owner；有路由无预留仍拒绝，不重扣额。
    #   限额通过后
    #   先持久化预留（A1）再发布盖章路由（A2），两次写之间崩溃由下次扫描补齐。
    #   mc4-1702：存在读不出的预留时整体暂停新准入（reservation_unreadable），不能把占位当空位。
    # 函数用途: 新建预留并发布盖章路由。
    def _create_and_publish(self, request_path: Path, current: dict, ownership, limits) -> dict:
        owner, session_scope = ownership
        if current.get("route") is not None:
            return _blocked("reservation_missing")
        snapshot = self.occupied_snapshot()
        if snapshot["unreadable"] > 0:
            return _blocked("reservation_unreadable")
        session_keys = _session_lookup_keys(current, session_scope)
        reason = _limit_rejection(snapshot, owner_digest(owner), session_keys, limits)
        if reason:
            return _blocked(reason)
        stamped = self._stamp_request(current, owner, time.time())
        if stamped is None:
            return _blocked("request_invalid")
        record = _reservation_record(request_path.stem, owner, session_scope, stamped)
        return _commit_created_record(self, request_path, owner, (stamped, record))

    # LLM: ordinary同代复用固定启用原Root关闭末门；漂移拒绝，takeover共用写叶默认不启用，不能拦已有终态收尾。
    # 函数用途: 自动复用当前代预留，不把损坏请求静默重盖成可执行。
    def _reuse_and_publish(self, request_path: Path, current: dict, record: dict, ownership) -> dict:
        if (record.get("route") or {}).get("c_run_id") != self.c_run_id:
            return _blocked("takeover_authority_required")
        published = current.get("route")
        if "route" in current and (not isinstance(published, dict) or published != record.get("route")):
            return {**_blocked("route_generation_conflict"), "error_code": "OWNER_ROUTE_MISMATCH"}
        return self._publish_reuse(request_path, current, record, ownership, ordinary_admission=True)

    # LLM: 核对/准备不持生命周期门；ordinary固定caller额外共Root关闭末门，默认takeover保持原写链，名额/身份/时间不变。
    #   owner/binding/worker/count
    #   必须与冻结值一致（唯一允许变化的是代次 c_run_id）；未发布时按当前代重新盖章后再比较补齐，
    #   已发布时只允许代次差异，其余不同一律暂停（不覆盖别人写下的路由）。
    #   mc3-p3-align：会话核对分版本（v2 精确比较投影；v1 遗留同形核对后升级写回）。本函数只应由
    #   同代补齐或已过共同授权门的显式转换调用（mc4-2101）。
    # 函数用途: 复用已有预留并补齐（或经授权转换）盖章发布。
    def _publish_reuse(self, request_path: Path, current: dict, record: dict, ownership, *, ordinary_admission=False) -> dict:
        owner, session_scope = ownership
        if record.get("owner_sha") != owner_digest(owner):
            return _blocked("reservation_conflict")
        upgrade = _legacy_scope_matches(current, record, session_scope)
        if upgrade is None:
            return _blocked("reservation_conflict")
        old_route = record.get("route") or {}
        if not old_route:
            return _blocked("reservation_conflict")
        published = current.get("route")
        if published is not None and _route_without_generation(published) != _route_without_generation(old_route):
            return _blocked("reservation_conflict")
        new_route = dict(old_route)
        new_route["c_run_id"] = self.c_run_id
        stamped = None
        if published is None:
            stamped = self._restamped_request(current, owner, old_route, new_route)
            if stamped is None:
                return _blocked("reservation_conflict")
        elif new_route != published:
            stamped = copy.deepcopy(current)
            stamped["route"] = new_route
        updated = dict(record)
        updated["route"] = new_route
        if upgrade:
            updated["schema_version"] = "multiprocess_admission_reservation.v2"
            updated["session_scope"] = project_admission_session(*_legacy_session_parts(current))
        if ordinary_admission:
            return _commit_reused_record(self, request_path, owner, (current, stamped, updated), ordinary_admission=True)
        return _commit_reused_record(self, request_path, owner, (current, stamped, updated))

    # LLM: 未发布补齐只投影原prepared输入并逐字段核对，禁止补metadata或重签指纹；除代次外任何漂移
    #   （如绑定版本变化）都说明无法维持冻结事实，返回 None 让调用方暂停。
    # 函数用途: 按当前代次重新盖章并核对除代次外字段一致；不一致返回 None。
    def _restamped_request(self, current: dict, owner, old_route: dict, new_route: dict):
        candidate = self._stamp_request(current, owner, old_route.get("stamped_at"))
        if candidate is None:
            return None
        if _route_without_generation(candidate.get("route") or {}) != _route_without_generation(old_route):
            return None
        candidate["route"] = new_route
        return candidate

    # LLM: 只投影原prepared绑定版本，不查询管理员表或改写metadata/指纹；缺失/类型/内容冲突失败时不写盘、不占名额。
    # 函数用途: 从不可变输入生成当前代次路由，结构异常返回None。
    def _stamp_request(self, current: dict, owner, stamped_at: float):
        try:
            version = current["metadata"]["gateway_admin_binding_version"]
            stamp = RouteStamp(self.count, self.c_run_id, stamped_at, version)
            return freeze_request_route(current, owner, stamp)
        except (KeyError, TypeError, ValueError):
            return None

    # LLM: 发布即把盖章后的完整请求原子写回原队列文件；这是 A2 屏障点，观察者异常照常传播。
    # 函数用途: 把盖章结果写回请求文件并发出 route_stamped 事件。
    def _publish_stamped(self, request_path: Path, stamped: dict) -> None:
        write_json_file_atomic(request_path, stamped)
        self.observe("route_stamped", {"request_id": request_path.stem, "worker": stamped["route"]["worker"]})

    # LLM: 释放的类入口；实现是模块级函数（同一 T 锁与规范终态校验，mc4-1707）。
    # 函数用途: 在请求锁内读取规范终态并释放对应预留；无终态或校验失败一律不释放。
    def release_if_terminal(self, request_id: str) -> dict:
        return release_reservation_if_terminal(self, request_id)

    # LLM: 释放消费者的类入口；实现是模块级函数（派发前对账已有终态的 occupied 预留，mc4-1704）。
    # 函数用途: 扫描预留目录释放已有终态的 occupied 预留，返回本轮释放数。
    def release_settled(self) -> dict:
        return release_settled_reservations(self)

    # LLM: 显式转换统一T→内存；同区精确读回登记自身提交，真正写入共撤权门，不反向等T。
    #   证明结构/stopped 在锁外先做 fail-fast 预检；
    #   权威授权（旧代次、覆盖范围、屏障结果、持锁 C 上下文）在 T 锁内由共同授权门校验，
    #   缺屏障、范围不符或上下文不符一律拒绝，不写任何状态。自动路径不经过这里。
    # 函数用途: 在请求锁内补章，并可在同一原末门内消费恢复元数据；缺授权不写，回调拒绝保已补章占位。
    def restamp_for_takeover(self, request_path: Path, ownership, authority, *, operation=None):
        if not self.ready():
            return _blocked("coordinator_generation_unavailable")
        owner, session_scope = ownership if ownership else (None, None)
        if not isinstance(owner, OwnerIdentity):
            return _blocked("owner_scope_unavailable")
        if not _valid_session_scope(session_scope):
            return _blocked("session_scope_unavailable")
        rejection = _takeover_proof_rejection(_authority_proof(authority))
        if rejection:
            return _blocked(rejection)
        with gateway_turn_transition(self.paths, request_path.stem):
            with _runtime_decision_scope(self):
                decision = (_processing_recovery_decision_locked(self, request_path, ownership, authority)
                            if operation is not None else self._restamp_locked(request_path, ownership, authority))
                if decision.get("request", {}).get("schema_version") != "gateway_terminal_request.v1":
                    decision = _remember_runtime_commit(self, request_path, decision)
                if operation is None or decision.get("status") != "admitted":
                    return decision
                return _consume_processing_recovery(self, owner, authority, decision, operation)

    # LLM: 显式转换的锁内决策：请求身份与锁内会话一致、预留为 occupied，然后由共同授权门
    #   校验旧代次/覆盖范围/屏障结果/持锁 C 上下文；其余一致性与写入复用 _publish_reuse。
    # 函数用途: 请求锁内的显式重新盖章转换。
    def _restamp_locked(self, request_path: Path, ownership, authority) -> dict:
        owner, session_scope = ownership
        request_id = request_path.stem
        report = read_json_file_report(request_path, context="gateway.admission.request.read")
        current = report.payload if report.load_error is None else {}
        if not current:
            return {"status": "raced", "reason": "request_moved"}
        if str(current.get("id") or "") != request_id:
            return _blocked("request_identity_conflict")
        locked_scope = session_scope_for_request(current)
        if locked_scope is None:
            return _blocked("session_scope_unavailable")
        if locked_scope != session_scope:
            return _blocked("request_scope_drift")
        record = _read_reservation(reservation_file(self.paths, request_id))
        if record is None:
            return _blocked("no_reservation")
        if record is _UNREADABLE:
            return _blocked("reservation_unreadable")
        if record.get("state") != "occupied":
            return _blocked("reservation_released")
        rejection = _takeover_authority_rejection(self, record, ownership, authority)
        if rejection:
            return _blocked(rejection)
        return self._publish_reuse(request_path, current, record, ownership)

    # LLM: 认领前校验的类入口；实现是模块级纯函数（worker 侧拒绝门与 C 侧自检同源，mc4-1705）。
    # 函数用途: 校验一份请求是否带着有效预留可被认领。
    @staticmethod
    def verify_claimable(request_payload: dict, reservation) -> dict:
        return verify_claimable_reservation(request_payload, reservation)


# LLM: 仅T锁已取得后进入内存门；缺运行时由原拒绝门处理，纯记录组件不新增I/O或租约。
# 函数用途: 串行准入/显式补章决策，与建立隔离共用内存边界。
@contextmanager
def _runtime_decision_scope(gate):
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        yield
        return
    with controller._mutex:
        yield


# LLM: T/来源核实都已完成；最终同步副作用共撤权门，释放不核owner隔离但必须核原租约/代次。
# 函数用途: 与实际撤权互斥，普通准入另核owner；纯记录合同仍无运行时I/O。
@contextmanager
def _runtime_commit_scope(gate, owner=None):
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        yield _runtime_context_rejection(gate)
        return
    with controller._mutex, controller._lease._lifecycle_gate:
        rejection = _runtime_context_rejection(gate)
        if rejection is None and owner is not None:
            rejection = _runtime_owner_rejection(gate, owner)
        yield rejection


# LLM: 只供ordinary新P3/同代补齐固定末叶，在原T→controller→lease后取唯一Root门；纯记录无controller/nonrequired保持无IO。
# 函数用途: 与stop_producers线性化新责任发表，缺/错实际绑定或closing拒绝；不供terminal/release/ack/takeover/恢复使用。
@contextmanager
def _ordinary_admission_commit_scope(gate):
    controller = getattr(gate, '_runtime_takeover', None)
    if controller is None and not getattr(gate, '_runtime_takeover_required', False):
        yield None
        return
    from .multiprocess_drain_runtime import GatewayDrainRuntime

    binding = getattr(gate, '_ordinary_admission_origin', None)
    if type(binding) is not tuple or len(binding) != 2 or type(binding[0]) is not GatewayDrainRuntime:
        yield _blocked('ordinary_admission_source_unavailable')
        return
    runtime, original = binding
    with runtime._gate:
        runtime.require_original()
        if (type(original) is not tuple or len(original) != 5
                or not any(row is original for row in runtime._admission_origins)
                or original[0] is not gate or original[1] is not gate.paths or original[2] is not gate.agent
                or original[3] != gate.count or original[4] != gate.c_run_id
                or runtime.control.paths is not gate.paths or runtime.controller is not controller
                or runtime.scope.count != gate.count or runtime.scope.c_run_id != gate.c_run_id
                or runtime._lease_origin is not controller._lease):
            yield _blocked('ordinary_admission_source_unavailable')
            return
        if runtime._closing:
            yield _blocked('coordinator_producers_closed')
            return
        yield None


# LLM: 回调必须位于真实T→controller→lease窗口，P3已发表后拒绝保占用，不回滚补章也不声称尚无副作用。
# 函数用途: 同当次签发/P3短门内最后复核来源，消费一次processing元数据动作。
def _consume_processing_recovery(gate, owner, authority, decision, operation):
    with _runtime_commit_scope(gate, owner) as rejection:
        if rejection:
            return rejection
        reason = _runtime_authority_rejection(gate, authority)
        if reason:
            return _blocked(reason)
        return operation(decision, authority)


# LLM: 不从JSON取得C权；即使释放的owner已隔离，也须原lease活动/未收尾且root/count/代次完全一致。
# 函数用途: 核最终写入的当前C上下文，不把隔离混成规范终态不可释放。
def _runtime_context_rejection(gate):
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        return _blocked("coordinator_generation_unavailable") if getattr(gate, "_runtime_takeover_required", False) else None
    if (not controller.active() or controller._recheck_stop.is_set() or gate.paths.root != controller.paths.root
            or gate.count != controller.count or gate.c_run_id != controller.c_run_id):
        return _blocked("coordinator_generation_unavailable")
    return None


# LLM: 调用方仍在T→内存决策区，所有自身准入/补章同样精确读回；来源I/O不占生命周期门。
# 函数用途: 在释放决策区前登记本次成功的原预留，读回漂移保占位并拒绝成功。
def _remember_runtime_commit(gate, path, decision):
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        return decision
    from .multiprocess_takeover_runtime import _remember_restamped

    if not _remember_restamped(controller, path, decision):
        return _blocked("reservation_readback_failed")
    return decision


# LLM: 原quota锁等待/准备在Root外；ordinary新建只在quota已取得后的原末叶持Root门跨A1+A2，与closing线性化，部分写仍不回滚。
# 函数用途: 在当前租约及实际额度内提交新预留和路由，失败保留不确定占位。
def _commit_created_record(gate, path, owner, prepared):
    stamped, record = prepared
    with _runtime_commit_scope(gate, owner) as rejection:
        if rejection:
            return rejection
        from .multiprocess_curator_accounting import (
            CuratorQuotaUnavailable,
            curator_quota_admission,
        )

        try:
            with curator_quota_admission(gate, stamped) as quota_rejection:
                if quota_rejection:
                    return _blocked(quota_rejection)
                return _commit_ordinary_created_record(gate, path, owner, stamped, record)
        except CuratorQuotaUnavailable:
            return _blocked("curator_quota_unavailable")


# LLM: quota准备完成后才取同一Root门，跨A1/A2原写叶；不供takeover/release/ack使用，部分失败仍保责任。
# 函数用途: 将普通新建的关闭末门保持为短作用域，不把quota等待或准备移入Root。
def _commit_ordinary_created_record(gate, path, owner, stamped, record):
    with _ordinary_admission_commit_scope(gate) as closing:
        if closing:
            return closing
        return _persist_created_record(gate, path, owner, stamped, record)


# LLM: 调用方已持原C生命周期及可选原quota锁；原预留→观察→owner复核→盖章顺序不变，不建第二占用账。
# 函数用途: 通过最终准入门后持久写入原P3预留和原请求路由。
def _persist_created_record(gate, path, owner, stamped, record):
    try:
        _write_gate_record_durable(gate, reservation_file(gate.paths, path.stem), record)
    except OSError:
        return _blocked("reservation_write_failed")
    gate.observe("reservation_persisted", {"request_id": path.stem, "reservation_id": record["reservation_id"]})
    rejection = _runtime_owner_rejection(gate, owner)
    if rejection:
        return rejection
    gate._publish_stamped(path, stamped)
    return {"status": "admitted", "request": stamped, "reservation": record}


# LLM: 默认跨代/takeover保持原末写；仅ordinary固定caller额外持Root门跨预留/route，同代名额/身份不变，不持门等T。
# 函数用途: 在当前租约内提交已核的复用/改代；写失败不发布路由。
def _commit_reused_record(gate, path, owner, prepared, *, ordinary_admission=False):
    current, stamped, updated = prepared
    with _runtime_commit_scope(gate, owner) as rejection:
        if rejection:
            return rejection
        if ordinary_admission:
            with _ordinary_admission_commit_scope(gate) as closing:
                if closing:
                    return closing
                return _persist_reused_record(gate, path, owner, current, stamped, updated)
        return _persist_reused_record(gate, path, owner, current, stamped, updated)


# LLM: 只提原共用写叶，默认takeover/恢复仍同参数/写入/owner末核；ordinary额外Root门由固定caller包围，不改旧权限或回执。
# 函数用途: 保原预留写入和可选路由发表次序，失败保occupied，不补终态或回滚。
def _persist_reused_record(gate, path, owner, current, stamped, updated):
    try:
        _write_gate_record_durable(gate, reservation_file(gate.paths, path.stem), updated)
    except OSError:
        return _blocked("reservation_write_failed")
    rejection = _runtime_owner_rejection(gate, owner)
    if rejection:
        return rejection
    if stamped is not None:
        gate._publish_stamped(path, stamped)
    return {"status": "admitted", "request": stamped if stamped is not None else current, "reservation": updated}

# LLM: 仅身份/时间稳定的released可跳读；自身未结摘要强制回读，stat失败/读中漂移仍保守占位。
#   类经本模块级函数读写自己的扫描缓存；扫描选择本身不签发释放或执行许可。
# 函数用途: 扫描用读取；返回 (记录|_SCAN_SKIPPED, 本次是否读盘)，不漏失败收尾。
def _scan_reservation(gate, path: Path):
    cache = gate._released_scan_cache
    try:
        info = path.stat()
    except OSError:
        cache.pop(path.name, None)
        return _read_reservation(path), True
    if _runtime_remembers_reservation(gate, path.stem):
        cache.pop(path.name, None)
    cached = cache.get(path.name)
    if cached is not None and released_cache_is_stable(cached, info):
        return _SCAN_SKIPPED, False
    return _read_scan_reservation(gate, path, info), True


# LLM: 读前/读后身份一致才缓存；漂移意味着本轮 released 内容并非当前文件，按 unreadable 占位。
#   观察时刻在读取之前取得，保守覆盖读期间更新和粗粒度 ctime 碰撞，不把读取耗时当文件变旧。
# 函数用途: 实读并确认扫描文件未漂移，更新本 gate 的 released 缓存。
def _read_scan_reservation(gate, path: Path, info):
    gate._released_scan_cache.pop(path.name, None)
    observed_ns = time.time_ns()
    record = _read_reservation(path)
    if not isinstance(record, dict) or record.get("state") != "released":
        return record
    try:
        after = path.stat()
    except OSError:
        return _UNREADABLE
    if reservation_signature(after) != reservation_signature(info):
        return _UNREADABLE
    gate._released_scan_cache[path.name] = (reservation_signature(after), observed_ns)
    return record


# LLM: 首写前确认完整目录链，全链失败不记确认；原子记录写和目录 fsync 仍沿原公共测试观察点。
# 函数用途: 在本 gate 的固定目录链确认后持久写记录；错误向上抛，不发布路由。
def _write_gate_record_durable(gate, path: Path, record: dict) -> None:
    confirm_reservation_chain(gate, path.parent, _fsync_directory)
    _write_record_durable(path, record)


# LLM: 读不出 = 占位（total+unreadable），不归因；released 跳过；occupied 才参与 owner/会话计数。
#   mc4-2103：走 _scan_reservation 的有界读取；null 投影不计入会话计数（不额外串行）。
# 函数用途: 把一份预留计入快照。
def _count_reservation(gate, path: Path, snapshot: dict) -> None:
    record, _read_performed = _scan_reservation(gate, path)
    if record is _SCAN_SKIPPED or record is None:
        return
    if record is _UNREADABLE:
        snapshot["unreadable"] += 1
        snapshot["total"] += 1
        return
    if record.get("state") != "occupied":
        return
    snapshot["total"] += 1
    owner_sha = str(record.get("owner_sha") or "")
    snapshot["per_owner"][owner_sha] = snapshot["per_owner"].get(owner_sha, 0) + 1
    key = _session_key(record.get("session_scope") or {})
    if key is not None:
        snapshot["per_session"][key] = snapshot["per_session"].get(key, 0) + 1


# LLM: 限额判断只读快照（结构化字段），原因码与既有进程内实现一致，便于上层沿用等待语义。
#   mc3-p3-align：同会话检查按预计算的键集合（v2 摘要 + v1 遗留明文并集），任一命中即忙。
# 函数用途: 按全局/每用户/同会话三层判断本次准入是否超限，返回原因码或空串。
def _limit_rejection(snapshot: dict, owner_sha: str, session_keys: tuple, limits) -> str:
    if snapshot["total"] >= max(1, int(limits.global_inflight)):
        return "global_inflight"
    if snapshot["per_owner"].get(owner_sha, 0) >= max(1, int(limits.user_inflight)):
        return "user_inflight"
    for key in session_keys:
        if snapshot["per_session"].get(key, 0) >= 1:
            return "conversation_busy"
    return ""


# LLM: 预留记录只在 occupied 时保存 route（复用校验依赖 stamped_at 不变）；terminal_fact 留空。
#   mc3-p3-align：新写一律 v2（会话范围为 mc1 冻结投影）；v1 旧记录照常可读、复用核对后升级。
# 函数用途: 构造一份新的准入预留记录（v2）。
def _reservation_record(request_id: str, owner, session_scope: dict, stamped: dict) -> dict:
    return {
        "schema_version": "multiprocess_admission_reservation.v2",
        "reservation_id": f"admission-{uuid.uuid4().hex}",
        "owner_sha": owner_digest(owner),
        "session_scope": dict(session_scope),
        "request_id": request_id,
        "route": stamped["route"],
        "state": "occupied",
        "terminal_fact": None,
    }


# LLM: 统一的结构化暂停结果；调用方按 reason 写等待信号，不解析自然语言。
# 函数用途: 构造 blocked 决策。
def _blocked(reason: str) -> dict:
    return {"status": "blocked", "reason": reason}


# LLM: mc4-1701/2101：接手证明的锁外预检——只认 P0 接手证明（v1/v2）且 status 必须为 stopped；
#   缺失、不合法、种类不符或未停止都不进锁。权威的范围/屏障/上下文校验在锁内共同授权门。
# 函数用途: 预检接手证明结构，返回拒绝原因码或空串。
def _takeover_proof_rejection(proof) -> str:
    try:
        validated = validate_record(proof)
    except MultiprocessRecordError:
        return "takeover_proof_invalid"
    if validated.get("schema_version") not in {"multiprocess_takeover_proof.v1", "multiprocess_takeover_proof.v2"}:
        return "takeover_proof_invalid"
    if validated.get("status") != "stopped":
        return "takeover_proof_not_stopped"
    return ""


# LLM: 产品工厂先核P2b当前同步内存授权及原P1明细，纯记录合同供隔离组件组合；所有跨代转换共门。
#   mc4-2101：跨代接手的共同授权门。绑定持锁 C 的上下文
#   （gate.c_run_id）、旧代次（记录 route 的代次）、进程角色/分区/分区数（证明覆盖范围必须与
#   请求执行者一致）、owner（证明必须覆盖请求 owner），以及 P2 核实过的屏障结果引用；
#   缺屏障、范围不符或上下文不符一律拒绝，绝不静默转换。自动路径不经过本门（跨代直接拒绝）。
# 函数用途: 校验一份接手授权；返回拒绝原因码或空串（放行）。
def _takeover_authority_rejection(gate, record, ownership, authority) -> str:
    owner, _session_scope = ownership
    runtime_rejection = _runtime_authority_rejection(gate, authority)
    if runtime_rejection:
        return runtime_rejection
    proof = _authority_proof(authority)
    rejection = _takeover_proof_rejection(proof)
    if rejection:
        return rejection
    old_route = record.get("route") or {}
    if proof.get("c_run_id") != old_route.get("c_run_id"):
        return "takeover_proof_generation_mismatch"
    scope_rejection = _takeover_scope_rejection(proof, old_route, owner_digest(owner), gate.count)
    if scope_rejection:
        return scope_rejection
    barrier_rejection = _takeover_barrier_rejection(_authority_barrier(authority), old_route.get("c_run_id"))
    if barrier_rejection:
        return barrier_rejection
    if _authority_run(authority) != gate.c_run_id:
        return "takeover_authority_context_mismatch"
    return ""


# LLM: 授权对象可以是结构化 {"proof", "barrier", "c_run_id"}，也可以是过渡期的裸证明
#   （等价于缺屏障、缺上下文，会在门里被拒）；两种形态的归一化只在这里做。
# 函数用途: 从授权对象取接手证明。
def _authority_proof(authority):
    if isinstance(authority, dict) and "proof" in authority:
        return authority.get("proof")
    return authority


# LLM: 只投影已有结构化授权的屏障引用，不从缺字段/裸证明补可信屏障，实际来源由共同门复核。
# 函数用途: 从授权对象取屏障结果引用；裸证明返回None，由原拒绝门处理。
def _authority_barrier(authority):
    if isinstance(authority, dict) and "proof" in authority:
        return authority.get("barrier")
    return None


# LLM: JSON运行号仅供共同门比较，不证明当前C持锁；缺来源不查询新state补代次。
# 函数用途: 从授权对象取C运行号，裸证明返回None，不发生读写。
def _authority_run(authority):
    if isinstance(authority, dict) and "proof" in authority:
        return authority.get("c_run_id")
    return None


# LLM: 纯P3显式构造只提供结构合同；产品工厂必绑P2b，缺当前持锁组合根也失败关闭而非退回裸JSON。
# 函数用途: 在共同授权门内核对真实运行时的当前同步授权。
def _runtime_authority_rejection(gate, authority) -> str:
    if not getattr(gate, "_runtime_takeover_required", False):
        return ""
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        return "takeover_authority_context_mismatch"
    return controller.authority_rejection(gate, authority)


# LLM: 产品工厂的准入也受旧域隔离约束；旧占用仍计数，不能为不同request_id启动同owner新写者。
# 函数用途: 返回真实运行时的owner隔离拒绝；纯记录组件不新增I/O。
def _runtime_owner_rejection(gate, owner):
    if not getattr(gate, "_runtime_takeover_required", False):
        return None
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        return {"status": "blocked", "reason": "owner_partition_quarantined", "error_code": "OWNER_PARTITION_QUARANTINED"}
    return controller.admission_rejection(owner_digest(owner))


# LLM: 覆盖范围必须与请求执行者一致：owner 在证明 owner 集里；进程角色/分区与请求路由的执行者
#   相同（v1 用 worker_index="C" 表达 C 标签，v2 用 role 表达）；分区数与当前 C 一致。角色或
#   分区互相冒充（如用 W 范围处理 C 请求）都拒绝。
# 函数用途: 校验证明覆盖范围；返回拒绝码或空串。
def _takeover_scope_rejection(proof, route, owner_sha, count) -> str:
    scope = proof.get("covered_scope") if isinstance(proof, dict) else None
    if not isinstance(scope, dict):
        return "takeover_scope_mismatch"
    if owner_sha not in (scope.get("owner_shas") or []):
        return "takeover_scope_mismatch"
    if scope.get("worker_count") != count:
        return "takeover_scope_mismatch"
    if not _proof_covers_worker(proof, scope, route.get("worker")):
        return "takeover_scope_mismatch"
    return ""


# LLM: v1/v2保留各自精确角色字段，bool不得充当W编号；这里只核范围形状，不签P2停止或P5执行许可。
# 函数用途: 核对证明覆盖的角色/分区与原请求执行者一致，不产生副作用。
def _proof_covers_worker(proof, scope, worker) -> bool:
    if proof.get("schema_version") == "multiprocess_takeover_proof.v2":
        role = scope.get("role")
        if role == "C":
            return worker == "C"
        return role == "W" and type(worker) is int and scope.get("worker_index") == worker
    if scope.get("worker_index") == "C":
        return worker == "C"
    return type(worker) is int and scope.get("worker_index") == worker


# LLM: 屏障结果最小合同（协议 §17.13；P2 未实现期间一律拒绝）：必须是 P2 为旧代次签发的核实
#   结论，引用执行者覆盖账本、无未结清实例；字段不齐、代次不符或未核实都拒绝。正式 schema 由
#   P2 落地时冻结，这里只按最小合同校验。
# 函数用途: 校验屏障结果引用；返回拒绝码或空串。
def _takeover_barrier_rejection(barrier, generation) -> str:
    if barrier is None:
        return "takeover_barrier_missing"
    fields = ("schema_version", "c_run_id", "stopped", "ledger_ref", "unresolved_instances")
    if not isinstance(barrier, dict) or set(barrier) != set(fields):
        return "takeover_barrier_invalid"
    if barrier.get("schema_version") != "multiprocess_takeover_barrier.v1":
        return "takeover_barrier_invalid"
    if barrier.get("c_run_id") != generation or barrier.get("stopped") is not True:
        return "takeover_barrier_invalid"
    if not (isinstance(barrier.get("ledger_ref"), str) and barrier["ledger_ref"].strip()):
        return "takeover_barrier_invalid"
    if barrier.get("unresolved_instances") != []:
        return "takeover_barrier_invalid"
    return ""


# LLM: 单条终态对账：没有规范终态不动；释放动作的任何异常都按"这条失败"处理，不拖垮整轮。
# 函数用途: 尝试释放一条已有终态的 occupied 预留，返回是否释放。
def _release_settled_if_terminal(gate, request_id: str) -> bool:
    if not (gate.paths.terminal / f"{request_id}.json").is_file():
        return False
    try:
        return bool(release_reservation_if_terminal(gate, request_id).get("released"))
    except Exception:
        return False


# LLM: 产品释放固定T→内存→最终生命周期门；规范终态仍唯一依据，owner隔离不否决终态释放。
#   线程返回、目录移动、responses 投影、心跳消失都不会走到这里。
# 函数用途: 在请求锁内读取规范终态并释放对应预留；无终态或校验失败一律不释放。
def release_reservation_if_terminal(gate, request_id: str) -> dict:
    record_path = reservation_file(gate.paths, request_id)
    with gateway_turn_transition(gate.paths, request_id), _runtime_decision_scope(gate):
        return _release_reservation_locked(gate, record_path, request_id)


# LLM: 重复释放仍先读规范终态经release_admission校验；纯记录幂等不写，当前自身未结摘要另补目录耐久。
#   缺终态/冲突/不可读一律拒绝；写失败不清摘要，但rename已成功时不能声称原文件字节未变。
# 函数用途: T/运行时内存内核规范终态，只有本次租约仍当前才写释放或完成自身收尾。
def _release_reservation_locked(gate, record_path: Path, request_id: str) -> dict:
    record = _read_reservation(record_path)
    if record is None:
        return {"released": False, "reason": "no_reservation"}
    if record is _UNREADABLE:
        return {"released": False, "reason": "reservation_unreadable"}
    terminal = _terminal_payload(gate.paths, request_id)
    if terminal is None:
        return {"released": False, "reason": "terminal_missing"}
    try:
        updated = release_admission(record, terminal)
    except MultiprocessRecordError as exc:
        return {"released": False, "reason": exc.reason}
    if record.get("state") == "released":
        return _finish_runtime_released(gate, record_path, record)
    with _runtime_commit_scope(gate) as rejection:
        if rejection:
            return {"released": False, "reason": rejection["reason"]}
        try:
            _write_gate_record_durable(gate, record_path, updated)
        except OSError:
            return {"released": False, "reason": "release_write_failed"}
        controller = getattr(gate, "_runtime_takeover", None)
        if controller is not None:
            controller._accepted.pop(request_id, None)
        gate.observe("released", {"request_id": request_id})
        return {"released": True}


# LLM: 调用方已持T/内存并核匹配终态；仅释放转换前的精确自身摘要可补耐久，不能把任意released当授权。
# 函数用途: 幂等补上rename成功后失败的目录同步，再清自身摘要；不重写规范文件或重复计释放。
def _finish_runtime_released(gate, record_path: Path, record: dict) -> dict:
    settled = {"released": False, "reason": "already_released"}
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        return settled
    occupied = {**record, "state": "occupied", "terminal_fact": None}
    if controller._accepted.get(record["request_id"]) != record_digest(occupied):
        return settled
    with _runtime_commit_scope(gate) as rejection:
        if rejection:
            return {"released": False, "reason": rejection["reason"]}
        try:
            confirm_reservation_chain(gate, record_path.parent, _fsync_directory)
            _fsync_directory(record_path.parent)
        except OSError:
            return {"released": False, "reason": "release_write_failed"}
        controller._accepted.pop(record["request_id"], None)
    return settled


# LLM: 只短读内存后就退出门，不持内存等T；布尔值仅选择扫描，终态/摘要/租约仍由锁内消费者重核。
# 函数用途: 让自身未结清摘要不被released扫描缓存跳过，不新增持久索引或放行依据。
def _runtime_remembers_reservation(gate, request_id: str) -> bool:
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        return False
    with controller._mutex:
        return request_id in controller._accepted


# LLM: mc4-1704/2103：派发前回收规范终态occupied，也重试当前自身released未结摘要；单条失败不拖整轮。
#   已结清且身份稳定的历史released仍走缓存捷径；未结摘要强制回读后经原终态/租约门，不凭扫描放行。
# 函数用途: 有界扫描释放已有终态名额并幂等收尾，返回本轮新释放数与实际读盘数。
def release_settled_reservations(gate) -> dict:
    directory = reservations_dir(gate.paths)
    if not directory.is_dir():
        return {"released": 0, "scanned": 0}
    released = 0
    scanned = 0
    for path in sorted(directory.glob("*.json")):
        record, read_performed = _scan_reservation(gate, path)
        if read_performed:
            scanned += 1
        if not isinstance(record, dict):
            continue
        if record.get("state") != "occupied" and not _runtime_remembers_reservation(gate, path.stem):
            continue
        released += 1 if _release_settled_if_terminal(gate, path.stem) else 0
    return {"released": released, "scanned": scanned}


# LLM: 终态读取失败、损坏或不存在都按"没有规范终态"处理（fail-closed 不释放）。
# 函数用途: 读取一个请求的规范终态信封。
def _terminal_payload(paths, request_id: str) -> dict | None:
    path = paths.terminal / f"{request_id}.json"
    report = read_json_file_report(path, context="gateway.admission.terminal.read")
    if report.load_error is not None or not report.payload:
        return None
    return report.payload


# LLM: 认领前校验"有路由 + 有有效 occupied 预留且逐字段一致"；这是 worker 侧拒绝门的纯函数版本，
#   P3 在 C 的 durable 派发里先用它做写读回自检。mc4-1705：预留必须整体过 P0 校验器
#   （schema/字段/owner/路由/状态全部合法），不能只查状态位。
# 函数用途: 校验一份请求是否带着有效预留可被认领。
def verify_claimable_reservation(request_payload: dict, reservation) -> dict:
    try:
        validated = validate_record(reservation)
    except MultiprocessRecordError as exc:
        return {"ok": False, "error_code": "ADMISSION_RESERVATION_CONFLICT", "reason": exc.reason}
    if validated.get("schema_version") not in {
        "multiprocess_admission_reservation.v1",
        "multiprocess_admission_reservation.v2",
    }:
        return {"ok": False, "error_code": "ADMISSION_RESERVATION_CONFLICT", "reason": "wrong_record_kind"}
    if validated.get("state") != "occupied":
        return {"ok": False, "error_code": "ADMISSION_RESERVATION_CONFLICT", "reason": "reservation_not_occupied"}
    route = request_payload.get("route") if isinstance(request_payload, dict) else None
    if not isinstance(route, dict):
        return {"ok": False, "error_code": "ADMISSION_RESERVATION_CONFLICT", "reason": "route_missing"}
    if validated.get("request_id") != request_payload.get("id"):
        return {"ok": False, "error_code": "ADMISSION_RESERVATION_CONFLICT", "reason": "request_id"}
    if validated.get("route") != route:
        return {"ok": False, "error_code": "ADMISSION_RESERVATION_CONFLICT", "reason": "route_mismatch"}
    return {"ok": True}


# LLM: Count0立即返回；内部C只从实际lease冻结代次并绑定P2，不读管理员表；无租约时state仅诊断，不能准入/接手。
# 函数用途: 从 Gateway 运行上下文构造持久准入门；默认模式返回 None。
def coordinator_admission_gate(agent, paths, context, observer=None):
    count = int(getattr(context, "coordinator_worker_count", 0) or 0)
    if count <= 0:
        return None
    from .multiprocess_coordinator import current_coordinator
    from .multiprocess_takeover_runtime import bind_admission_takeover

    lease = current_coordinator(paths.root, count)
    scope = CoordinatorAdmissionScope(
        count=count,
        c_run_id=lease.c_run_id if lease and lease.c_run_id else _read_coordinator_run_id(paths),
    )
    gate = PersistentAdmission(paths, agent, scope, observer=observer)
    bind_admission_takeover(gate, lease.controller if lease else None)
    return gate


# LLM: 原T内仅recovery实际issued回调消费，旧P1绑定/完整P2和P3owner/session精确复核；终态只续metadata不改canonical/P3生命周期。
# 函数用途: 为已补章的原processing或终态待投影提供同源短提交，ordinary restamp不使用本分支。
def _processing_recovery_decision_locked(gate, path, ownership, authority):
    from .multiprocess_processing_recovery import processing_authority_rejection
    from .multiprocess_records import _canonical_terminal

    report = read_json_file_report(path, context="gateway.admission.processing_recovery.read")
    current = report.payload
    if report.load_error is not None or current.get("id") != path.stem:
        return _blocked("request_identity_conflict")
    if path.parent not in (gate.paths.processing, gate.paths.terminal):
        return _blocked("processing_recovery_path_conflict")
    record = _read_reservation(reservation_file(gate.paths, path.stem))
    if not isinstance(record, dict) or session_scope_for_request(current) != ownership[1]:
        return _blocked("processing_recovery_reservation_unavailable")
    if record["owner_sha"] != owner_digest(ownership[0]) or record["route"] != current.get("route"):
        return _blocked("processing_recovery_reservation_conflict")
    reason = processing_authority_rejection(gate, record, current, ownership, authority)
    if reason:
        return _blocked(reason)
    if current.get("schema_version") == "gateway_terminal_request.v1":
        try:
            _canonical_terminal(record, current)
            if record["state"] == "released":
                release_admission(record, current)
        except (ValueError, TypeError, KeyError):
            return _blocked("processing_recovery_terminal_conflict")
        return {"status": "admitted", "request": current, "reservation": record}
    if record["state"] != "occupied":
        return _blocked("reservation_released")
    return gate._publish_reuse(path, current, record, ownership)
