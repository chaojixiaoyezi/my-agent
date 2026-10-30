# LLM: blocking=False沿原try-file-lock，抢不到显式失败；直接JSON替换在原锁内先读当前header拒写高代际，JSON字节/CAS/锁身份不变，不签执行许可。
# 模块用途: 可选deadline只守原IO末步骤，不签权；None默认与所有原锁/序列化保持。
# 模块用途: 供原任务refs与发布使用同一原文件事务，并可拒绝显式等待。
# LLM: Gateway原文件队列读写和只读统计；首次prepare沿原T内单次提交，延迟callback不得越过PID/thread/退出边界，不由计数派生许可或终态。
# 模块用途: 保持请求文件格式、原子写入和轻量目录统计，不执行业务逻辑。
from __future__ import annotations

"""provides deterministic JSON-file queue IO helpers for gateway protocol files.

这个文件只管 gateway 文件队列的基础读写。
比如写请求文件、读响应文件、数队列里有多少 JSON、追加历史流水。
这里不执行业务，只保证文件格式和移动规则稳定。
"""

import errno
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import (
    append_private_jsonl_records,
    check_io_deadline,
    jsonl_lines,
    path_thread_lock,
    serialize_private_json_no_newline,
    write_private_json_file_atomic_no_newline,
    write_private_json_file_atomic_no_newline_unlocked,
    write_private_text_file_atomic,
)
from ..common.json_io import (
    locked_json_path as locked_private_json_path,
)
from ..common.logical_reference_ids import GATEWAY_REQUEST_ID_PREFIX
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_private_lock_beneath_tightened,
    split_existing_anchor,
)
from ..contracts.optimization_contracts import require_schema_generation
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .paths import GatewayPaths

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX import guard.
    msvcrt = None

_GATEWAY_HISTORY_CACHE_LOCK = threading.Lock()
_path_lock = path_thread_lock
# 限制历史文件进程内快照缓存条目上限，防止多工作区长期运行无界增长。
_MAX_GATEWAY_HISTORY_CACHE_ENTRIES = 64
# 网关历史缓存允许缓存的单个文件最大字节数。
_MAX_GATEWAY_HISTORY_CACHE_FILE_BYTES = 4 * 1024 * 1024
# 网关历史文件进程内缓存允许占用的总字节数上限。
_MAX_GATEWAY_HISTORY_CACHE_TOTAL_BYTES = 16 * 1024 * 1024
_GATEWAY_HISTORY_CACHE: dict[
    str,
    tuple[tuple[int, int], tuple[str, ...], dict[str, _GatewayHistoryIndexEntry]],
] = {}
GATEWAY_REQUEST_FINGERPRINT_SCHEMA = "gateway_request_fingerprint.v1"
_GATEWAY_REQUEST_FINGERPRINT_KEYS = (
    "kind",
    "goal",
    "prompt",
    "source",
    "user_id",
    "channel",
    "conversation",
    "metadata",
    "system_task",
    "inject",
    "prompt_files",
    "input_media",
    "save",
    "include_prompt",
    "resume_context",
    "client_capabilities",
)


# LLM: Gateway 历史索引仅为进程内缓存，终态规范归档仍是权威；外部写入以 mtime/size 使缓存失效，修改须联查历史修复测试。
# 类用途: 缓存某个请求 ID 在历史投影中的完整正文、出现次数和冲突情况。
@dataclass(frozen=True)
class _GatewayHistoryIndexEntry:
    payload: dict
    count: int = 1
    conflict: bool = False


# LLM: Gateway JSON 读口用独立字段区分正文、读取错误与暂时锁忙；调用方不得把竞争误判为缺失或损坏。
# 类用途: 返回一次 JSON 读取的正文和错误分类，供状态读取与队列调用方决定是否重试。
@dataclass(frozen=True)
class GatewayJsonReadReport:
    payload: dict
    load_error: dict | None = None
    # LLM: 取锁失败（身份核对重试耗尽、取锁被打断）是暂时竞争，不是数据损坏：单独置位，供调用方
    #   按“可重试的锁失败”处理；不读它、只看 load_error 的调用方行为保持原样。
    lock_busy: bool = False


# LLM: Gateway 指纹包含 owner、会话、提示、任务及不可变媒体引用等执行选项，不含可变租约/状态/投影；以调用方文件名 ID 覆盖正文别名，修改须联查幂等入队。
# 函数用途: 计算 Gateway 请求不可变身份与执行内容的稳定指纹。
def gateway_request_fingerprint(payload: dict, request_id: str) -> str:
    canonical_id = str(request_id or "").strip()
    immutable: dict[str, object] = {"id": canonical_id}
    if "request_id" in payload:
        immutable["request_id"] = canonical_id
    for key in _GATEWAY_REQUEST_FINGERPRINT_KEYS:
        if key in payload:
            immutable[key] = payload.get(key)
    encoded = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: Gateway 已存指纹及 schema 必须匹配规范重算；旧记录缺值可由调用方显式补齐，不允许把冲突记录当旧格式重写。
# 函数用途: 校验已保存的 Gateway 请求指纹，并返回当前正确值。
def validated_gateway_request_fingerprint(payload: dict, request_id: str) -> str:
    expected = gateway_request_fingerprint(payload, request_id)
    stored = str(payload.get("request_fingerprint") or "").strip()
    schema = str(payload.get("request_fingerprint_schema") or "").strip()
    if stored and (
        schema != GATEWAY_REQUEST_FINGERPRINT_SCHEMA
        or stored != expected
    ):
        raise DataCorruptionError(
            "gateway request fingerprint conflicts with immutable request content"
        )
    if schema and schema != GATEWAY_REQUEST_FINGERPRINT_SCHEMA:
        raise DataCorruptionError("gateway request fingerprint schema is invalid")
    return expected


# LLM: 兼容旧名称但不保留无栅栏写者；与原私有原子入口同编码，遇高代际在原文件锁内拒写。
# 函数用途: 将旧直接写名称收敛到同一R1安全写入口。
def write_json_file(path: Path, payload: dict) -> None:
    write_json_file_atomic(path, payload)


# LLM: 队列文件（请求/响应/状态）都是宿主自己的运行数据，一律按私有权限落盘：目录 0700、文件出生 0600、
#   已有宽权限文件下次写入即收紧。读写方（TUI、适配器、派活工具、后台服务）都是同一个系统用户，
#   收紧到 0600 不影响它们；内容格式与原来逐字节一致（indent=2、sort_keys、无尾换行）。
#   C显式False只尝试原common写锁及同一unlocked原语；不能因资源忙跳过锁或假写成功，默认True沿原body。
# 函数用途: 原子替换本人JSON；可选deadline与try锁到同一末writer，原默认行为保留。
# LLM: R1在原private JSON锁内核原header和输入，不能在检查与replace间让迁移写者插入；deadline不授予权限。
# 函数用途: 沿原锁和序列化原子写入，拒绝覆盖当前进程不理解的存储代际。
def write_json_file_atomic(path: Path, payload: dict, *, blocking: bool = True, deadline: float | None = None) -> None:
    check_io_deadline(deadline)
    require_schema_generation(payload)
    with locked_private_json_path(path, blocking=blocking):
        current, _readable, _raw, _version = _read_json_dict_unlocked_report(path)
        require_schema_generation(current)
        check_io_deadline(deadline)
        options = {} if deadline is None else {"deadline": deadline}
        write_private_json_file_atomic_no_newline_unlocked(path, payload, sort_keys=True, **options)


# LLM: Gateway 临时文件清理只吞 OSError，不影响替换首因；调用方负责确定该路径确为自身暂存文件。
# 函数用途: 尽力删除临时文件；删除失败不影响已经完成或即将重试的替换。
def _unlink_tmp_file(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


# LLM: Gateway 替换辅助入口仅对 PermissionError 作八次递增短等待；成功会移动暂存文件，耗尽传播原异常，不扩大重试类型。
# 函数用途: 将临时文件替换到目标，遇短暂权限占用时重试，最终失败仍由调用方处理。
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


# LLM: Gateway 简化读口仅投影报告 payload，错误细分由 read_json_file_report 提供；需要区分缺失/锁忙的调用方不可依赖空字典。
# 函数用途: 读取热 JSON 或已封存回执正文，不向调用方返回诊断字段。
def read_json_file(path: Path) -> dict:
    return read_json_file_report(path).payload


# LLM: 可选False只透传原gateway读锁，忙仍保原lock_busy分类；默认等待、JSON校验和错误报告不变，不能把busy当缺失/损坏。
# 函数用途: 原JSON读取仍保报告分类；可选预算在拿锁和读取后核，不把过期当空。
# LLM: 热文件缺失时只查询已提交不可变段，不重建热文件；损坏归档不能洗成不存在。
# 函数用途: 沿同一原JSON读口透明回读封存的请求或答复。
def read_json_file_report(path: Path, *, context: str = "gateway.json.read", blocking: bool = True, deadline: float | None = None) -> GatewayJsonReadReport:
    check_io_deadline(deadline)
    try:
        if not path.exists():
            archived = lookup_gateway_receipt_bytes(path)
            if archived is None:
                return GatewayJsonReadReport({})
            payload = json.loads(archived)
        else:
            with _locked_json_path(path, blocking=blocking):
                check_io_deadline(deadline)
                payload = json.loads(path.read_text(encoding="utf-8"))
    except (BlockingIOError, InterruptedError) as exc:
        # LLM: BlockingIOError/InterruptedError 都是 OSError 子类，必须在下面的 catch-all 之前单独
        #   识别：锁忙（含身份核对重试耗尽）要保持“可重试的锁失败”语义，调用方按锁失败处理，
        #   不能当成数据损坏；load_error 仍带上诊断，其余只认 load_error 的调用方行为不变。
        return GatewayJsonReadReport({}, _gateway_json_load_error(path, exc, context), lock_busy=True)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return GatewayJsonReadReport({}, _gateway_json_load_error(path, exc, context))
    check_io_deadline(deadline)
    if isinstance(payload, dict):
        return GatewayJsonReadReport(payload)
    return GatewayJsonReadReport(
        {},
        _gateway_json_load_error(
            path,
            ValueError(f"gateway JSON root is {type(payload).__name__}, expected object"),
            context,
        ),
    )


# LLM: 只由已知Gateway队列/response布局定位同根段库，不扫描其他owner，也不创建缺失根。
# 函数用途: 对已封存的规范文件路径提供精确字节读口。
def lookup_gateway_receipt_bytes(path: Path) -> bytes | None:
    from .owner_retention import lookup_sealed_receipt
    from .paths import gateway_paths_from_root

    path = Path(path)
    if path.parent.name == 'responses':
        root = path.parent.parent
    elif path.parent.parent.name == 'requests' and path.parent.name in ('terminal', 'done', 'failed', 'completed', 'cancelled') or path.parent.name == 'terminal_complete' and path.parent.parent.name == 'projection_state':
        root = path.parent.parent.parent
    else:
        return None
    return lookup_sealed_receipt(gateway_paths_from_root(root), str(path.relative_to(root)))


# LLM: Gateway 读取错误沿通用运行错误报告附加目标路径，只构造诊断、不写文件；保持 context 与异常分类供上层识别。
# 函数用途: 为 JSON 读取失败生成包含文件位置的结构化错误。
def _gateway_json_load_error(path: Path, exc: BaseException, context: str) -> dict:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


# LLM: Gateway 队列更新以 require_existing 拒绝已移走的记录；会话和协作状态仍可显式创建或更新，不能将两种合同混用。
#   读-改-写整段在同一把锁内完成，落盘走私有原子写（目录 0700、文件 0600、存量宽权限下次写入收紧）。
#   内容去重（E3 返工 / mc4-501）：旧文件存在且能正常解析为对象时，把 updater 结果用与写入原语
#   完全相同的序列化转成文本，与磁盘原文逐字相同才跳过——比较对象是原始文本，不依赖 Python 深度
#   相等（True==1、1.0==1、-0.0==0.0 都会把类型更正误判成"没变"），updater 原地改也不影响比较。
#   跳过还要求磁盘安全状态已与写入后等价（mc4-502）：普通文件、nlink==1、0600、属主为本进程，
#   任一不满足就照常写（存量宽权限文件仍会被收紧）；缺失/损坏/空文件/非对象根照写（保持修复语义）。
#   force_write 供"写动作本身是可观察信号"的调用点显式退回原行为。
# 函数用途: 在 per-path 锁内读改写一个 JSON 文件，落盘走私有原语（0600/0700）；内容与安全状态均未变时跳过写入。
# LLM: 默认仍原阻塞/序列化/锁序；C可显式非阻塞复用原try锁，争用必须报错而不跳过半边提交。
# 函数用途: 在同一原文件事务内可选立即拒绝锁忙，不新增writer或假成功。
# LLM: 可选deadline在读/updater/精确去重后及replace前核；不改原版本验证或CAS，None保持原调用形状。
# 函数用途: 在原JSON锁内更新；短窗过期不启动下一写，已提交事实不回滚。
# LLM: R1在原锁内、updater之前核原header，旧写者遇高代际必须STORE_SCHEMA_NEWER，不允许回写覆盖未来字段。
# 函数用途: 原JSON事务保留原去重与锁序，并在任何业务更新前拒绝不支持的存储代际。
def update_json_file_atomic(
    path: Path,
    updater: Callable[[dict], dict],
    *,
    require_existing: bool = False,
    force_write: bool = False,
    blocking: bool = True,
    deadline: float | None = None,
) -> dict:
    check_io_deadline(deadline)
    with _locked_json_path(path, blocking=blocking):
        check_io_deadline(deadline)
        if require_existing and not path.is_file():
            raise FileNotFoundError(path)
        current, current_readable, raw_text, read_version = _read_json_dict_unlocked_report(path)
        require_schema_generation(current)
        check_io_deadline(deadline)
        updated = _updated_json_dict(updater, current)
        require_schema_generation(updated)
        check_io_deadline(deadline)
        skip_candidate = not force_write and current_readable and _skip_write_is_safe(path, read_version)
        if skip_candidate and _serialized_matches(updated, raw_text):
            check_io_deadline(deadline)
            return updated
        options = {} if deadline is None else {"deadline": deadline}
        check_io_deadline(deadline)
        write_private_json_file_atomic_no_newline_unlocked(path, updated, sort_keys=True, **options)
        return updated


# LLM: 内容去重的最后一步——把 updater 结果用与写入原语完全相同的序列化转成文本，与磁盘原文
#   逐字比较；不能用 Python 深度相等（True==1、1.0==1、-0.0==0.0 会把类型更正误判成"没变"）。
# 函数用途: 判断"若现在写入，落盘字节是否与磁盘现有字节完全相同"。
def _serialized_matches(updated: dict, raw_text: str) -> bool:
    return serialize_private_json_no_newline(updated, sort_keys=True) == raw_text


# LLM: 跳过写入 = 磁盘上不再发生"替换"的副作用，所以必须逐项核对"写入后应有的磁盘状态"现在是否
#   已经成立：替换写入会（a）把叶子换成普通文件（符号链接被替换、不再跟随）、（b）断开硬链接、
#   （c）收紧到 0600、（d）属主变成本进程。全部满足才允许跳过，任一不满足照常写（存量宽权限/被
#   换过的文件仍会被自愈）；lstat 失败按不满足处理（fail-closed）。Windows 没有 os.geteuid，
#   属主项按平台可核对项跳过，其余三项照查。
# 函数用途: 判断磁盘上的 JSON 文件是否已处于"私有原子写之后"的等价状态，允许跳过重写。
def _skip_write_is_safe(path: Path, read_version: os.stat_result | None) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if read_version is None or _json_file_version(info) != _json_file_version(read_version):
        return False
    if not stat.S_ISREG(info.st_mode):  # 符号链接/非常规文件：写入会替换成普通文件
        return False
    if info.st_nlink != 1:  # 硬链接：写入会断开
        return False
    if stat.S_IMODE(info.st_mode) != 0o600:  # 宽权限：写入会收紧
        return False
    euid_getter = getattr(os, "geteuid", None)
    if euid_getter is not None and info.st_uid != euid_getter():  # 属主非本进程：写入会改属主
        return False
    return True


# LLM: 内容去重需要区分"合法旧内容"与"缺失/损坏/非对象"：只有前者能作为跳过写入的依据，
#   后者必须照常写回（修复语义）。按bytes解码保留CRLF，避免通用换行把不同字节误判相同；非法 UTF-8 与旧行为
#   一致地上抛，不在此处吞掉。
#   首先nonblocking/nofollow打开并核实FD普通文件，最多1MiB；超限或特殊文件拒写，不能以缺失降级覆盖未知header。
# 函数用途: 有界读回路径上的 JSON 对象、是否"存在且可正常解析为对象"，以及磁盘原文。
def _read_json_dict_unlocked_report(path: Path) -> tuple[dict, bool, str, os.stat_result | None]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('gateway JSON target is not a regular file')
            limit = 1024 * 1024
            if before.st_size > limit:
                raise ValueError('gateway JSON header exceeds bounded read limit')
            raw = handle.read(limit + 1)
            if len(raw) > limit:
                raise ValueError('gateway JSON header exceeds bounded read limit')
            raw_text = raw.decode('utf-8')
            after = os.fstat(handle.fileno())
            if (_json_file_version(before) != _json_file_version(after)
                    or _json_file_version(after) != _json_file_version(os.lstat(path))):
                raise ValueError('gateway JSON source changed during header read')
        current = json.loads(raw_text)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, False, "", None
    if not isinstance(current, dict):
        return {}, False, raw_text, after
    return current, _json_file_version(before) == _json_file_version(after), raw_text, after


# LLM: 该观测只决定能否省略原写；不授予跨进程事务权，实际写仍由原path锁和原子替换约束。
# 函数用途: 比较读取时与跳写前的同一文件版本，发现外部替换或原位修改就照常写回。
def _json_file_version(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: Gateway updater 在当前字典的浅副本上执行，必须返回 dict；此辅助函数不落盘，但回调自身副作用仍由调用方负责。
# 函数用途: 调用 JSON 更新器并检查返回类型，避免将非对象写成队列状态。
def _updated_json_dict(updater: Callable[[dict], dict], current: dict) -> dict:
    updated = updater(dict(current))
    if not isinstance(updated, dict):
        raise TypeError("update_json_file_atomic updater must return dict")
    return updated


# LLM: 默认仍原阻塞/序列化/锁序；C可显式非阻塞复用原try锁，争用必须报错而不跳过半边提交。
# 函数用途: 在同一原文件事务内可选立即拒绝锁忙，不新增writer或假成功。
@contextmanager
def _locked_json_path(path: Path, *, blocking: bool = True):
    if not blocking:
        with try_locked_file_transition(path) as acquired:
            if not acquired:
                raise BlockingIOError("original_file_transition_busy")
            yield
        return
    handle = path_thread_lock(path)
    with handle.lock:
        with _locked_file_path(path):
            yield


# LLM: 默认仍原阻塞/序列化/锁序；C可显式非阻塞复用原try锁，争用必须报错而不跳过半边提交。
# 函数用途: 在同一原文件事务内可选立即拒绝锁忙，不新增writer或假成功。
@contextmanager
def locked_file_transition(path: Path, *, blocking: bool = True):
    """Serialize a logical transition spanning more than one durable file.

    The JSON helpers make one file atomic.  Conversation steer/stop/complete must
    order a task-link transition and its guidance ledger together, so they share a
    dedicated lock path through this small public boundary.
    """
    with _locked_json_path(path, blocking=blocking):
        yield


# LLM: Gateway 只读状态探测不能排队等待供应商 IO；返回 False 时无修改权，只能保留或重放已有原子回执。
#   common包装强引用覆盖整个等待/释放窗口，OS句柄关闭异常也必须归还线程锁。
# 函数用途: 尝试领取逻辑文件事务锁，锁正被使用时立即返回 False 而不是阻塞。
@contextmanager
def try_locked_file_transition(path: Path):
    handle = path_thread_lock(path)
    if not handle.lock.acquire(blocking=False):
        yield False
        return
    file_handle = None
    os_lock_acquired = False
    try:
        file_handle = _open_lock_handle(path)
        os_lock_acquired = _try_flock_exclusive(file_handle)
        if not os_lock_acquired:
            yield False
            return
        # LLM: 锁文件可能刚被孤儿清理器换掉：身份不符说明拿到的不是当前锁文件的锁，探测不能获得授权
        #   （fail-closed）；按"没拿到"返回，让调用方保留原事实。
        if not _lock_handle_matches_path(file_handle, path.with_name(path.name + ".lock")):
            yield False
            return
        yield True
    finally:
        try:
            _release_transition_file_handle(file_handle, os_lock_acquired)
        finally:
            handle.lock.release()


# LLM: 先释放可能已取得的 OS 排他锁，再确保关闭底层锁文件描述符；任一步异常都不吞掉调用方归还线程锁。
# 函数用途: 安全解锁并关闭非阻塞文件事务探测的 OS 锁文件句柄。
def _release_transition_file_handle(file_handle, os_lock_acquired: bool) -> None:
    if file_handle is None:
        return
    try:
        if os_lock_acquired:
            _flock_unlock(file_handle)
    finally:
        file_handle.close()


# LLM: Gateway 同一请求的入队、执行、终态转换及活动回合入口共用此锁；摘要隔离不可信 ID，修改须联查回合转换调用方。
# 函数用途: 取得并持有某个 Gateway 回合唯一的跨进程状态转换锁。
@contextmanager
def gateway_turn_transition(paths: GatewayPaths, request_id: str):
    turn_id = str(request_id or "").strip()
    if not turn_id:
        raise ValueError("gateway turn transition requires request_id")
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    transition_path = paths.root / "turn_transitions" / f"{digest}.transition"
    with locked_file_transition(transition_path):
        yield


# LLM: Gateway 非阻塞探测必须与阻塞转换共用同一摘要 T 锁路径；取锁失败只能保留或重放事实，不授予状态变更权。
# 函数用途: 非阻塞尝试取得某个 Gateway 回合的跨进程状态转换锁。
@contextmanager
def try_gateway_turn_transition(paths: GatewayPaths, request_id: str):
    turn_id = str(request_id or "").strip()
    if not turn_id:
        raise ValueError("gateway turn transition requires request_id")
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    transition_path = paths.root / "turn_transitions" / f"{digest}.transition"
    with try_locked_file_transition(transition_path) as acquired:
        yield acquired


# LLM: 锁文件可能被孤儿清理器删除或替换（gateway 请求锁 sidecar）：拿到 flock 后核对 fd 与锁路径的
#   (st_dev, st_ino) 一致；不一致说明名字已换或已删——在旧 inode 上持锁不再是互斥保证（阻塞迟到者场景），
#   close 后重开重试。超上限按锁竞争失败（BlockingIOError），与既有线程锁失败语义一致。
_LOCK_IDENTITY_RETRY_COUNT = 5


# LLM: 锁与数据文件同口径私有（0600/0700）；缺失目录经 no-follow 原语按 0700 建、已存在一律不动（lkp/pdp）；保留 blocking=False 非阻塞语义。
# 函数用途: 在 <path>.lock 上取排他锁，包裹调用方的临界区。
@contextmanager
def _locked_file_path(path: Path):
    handle = _acquire_verified_lock(path)
    try:
        yield
    finally:
        _flock_unlock(handle)
        handle.close()


# LLM: 取锁重试循环独立成函数（压平临界区的嵌套）：每轮"打开→阻塞取锁→核对身份"；身份不符说明锁文件
#   被清理器换掉，关闭后重开重试；取锁本身异常时关闭句柄再冒泡，不泄漏 fd；超上限抛 BlockingIOError。
# 函数用途: 反复取锁直到 fd 与锁路径身份一致，返回已验证的锁句柄。
def _acquire_verified_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    for _ in range(_LOCK_IDENTITY_RETRY_COUNT):
        handle = _open_lock_handle(path)
        try:
            _flock_exclusive(handle)
        except BaseException:
            handle.close()
            raise
        if _lock_handle_matches_path(handle, lock_path):
            return handle
        handle.close()
    raise BlockingIOError("锁文件身份反复变化，放弃取锁")


# LLM: 身份核对只比较 (st_dev, st_ino)，不看 mtime/权限；路径缺失或不可 stat 一律视为不匹配（fail-closed）。
# 函数用途: 核对锁句柄与锁路径当前指向同一个 inode。
def _lock_handle_matches_path(handle, lock_path: Path) -> bool:
    try:
        fd_stat = os.fstat(handle.fileno())
        path_stat = os.stat(lock_path)
    except OSError:
        return False
    return (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)


# LLM: 锁文件与数据文件同口径私有（文件 0600、父目录 0700、不跟随符号链接）：原来 lock_path.open("a+")+mkdir
#   会按 umask 落成 0644/0755，同机他用户可打开并 flock 卡住宿主写入（ds3 统一锁写法时漏掉的第四处）。
#   经 open_private_lock_beneath_tightened 拿 fd（已存在宽权限锁打开时收紧到 0600），再包回文本句柄；
#   flock / Windows byte-lock / close 的既有用法不变。
# 函数用途: 打开一个私有锁文件句柄，顺带把已存在的宽权限锁收紧到 0600。
def _open_lock_handle(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    descriptor = _open_private_lock_descriptor(lock_path)
    return os.fdopen(descriptor, "a+", encoding="utf-8")


# LLM: 调用方给的是完整锁路径；这里把绝对路径拆成“已存在的最近祖先 + 其余缺失段”（与其余锁调用点共用
#   nofollow_fs.split_existing_anchor），让缺失目录仍统一经 no-follow 原语按 0700 创建。
# 函数用途: 打开一个私有锁文件描述符。
def _open_private_lock_descriptor(lock_path: Path) -> int:
    lock_path = Path(os.path.abspath(lock_path))
    anchor, missing = split_existing_anchor(lock_path.parent)
    return open_private_lock_beneath_tightened(anchor, (*missing, lock_path.name))


# LLM: Gateway 以 POSIX flock 或 Windows 字节锁实现同一阻塞跨进程边界；无可用原语必须拒绝，不能退为仅线程锁而破坏单次执行合同。
# 函数用途: 跨平台独占当前 lock 文件，直到同路径的另一个进程释放事务。
def _flock_exclusive(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:
        _ensure_windows_lock_byte(handle)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError as exc:
                if not _is_file_lock_contention(exc):
                    raise
                time.sleep(0.05)
    raise RuntimeError("cross-process file locking is unavailable on this platform")


# LLM: Gateway 非阻塞锁探测区分活跃占用与遗留回执；仅明确锁竞争返回 False，其它系统错误传播，修改须联查回执恢复。
# 函数用途: 尝试取得 OS 文件锁并返回是否成功，不等待另一个进程释放。
def _try_flock_exclusive(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        return True
    if msvcrt is not None:
        _ensure_windows_lock_byte(handle)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_file_lock_contention(exc):
                return False
            raise
        return True
    raise RuntimeError("cross-process file locking is unavailable on this platform")


# LLM: Gateway 解锁必须对应取锁平台原语，Windows 固定字节零；调用方在 finally 中执行，不能因业务异常泄漏锁所有权。
# 函数用途: 释放当前 lock 文件的跨进程独占锁。
def _flock_unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


# LLM: Gateway 的 Windows 锁依赖固定字节而非 inode；首次取锁前可写入哨兵字节，不得把锁文件当业务正文，修改须联查 Windows 锁测试。
# 函数用途: 确保 Windows byte-range lock 有固定的第 0 字节可锁。
def _ensure_windows_lock_byte(handle) -> None:
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write("\0")
        handle.flush()
    handle.seek(0)


# LLM: Gateway 仅将明确 errno/winerror 锁竞争码视作占用；坏句柄、磁盘故障及不支持操作不得伪装为 busy。
# 函数用途: 判断一次跨平台文件锁失败是否只是“锁正被其他进程占用”。
def _is_file_lock_contention(exc: OSError) -> bool:
    contention_errnos = {errno.EACCES, errno.EAGAIN}
    deadlock_errno = getattr(errno, "EDEADLK", None)
    if deadlock_errno is not None:
        contention_errnos.add(deadlock_errno)
    if exc.errno in contention_errnos:
        return True
    return getattr(exc, "winerror", None) in {33}


# 孤儿锁 sidecar 的最小年龄：24 小时宽限，避开“文件刚删、别的进程还握着旧句柄读写”的窗口，又让清理每天都能收掉积压；纯维护动作，不加配置开关。
_ORPHAN_LOCK_MIN_AGE_SECONDS = 24 * 3600


# LLM: 请求队列的读写都会给数据文件建 <name>.lock sidecar（_open_lock_handle/_locked_file_path），
#   文件归档/删除后 sidecar 没有任何删除路径，会长期累积（真机 3000+ 空锁文件）。清理只删同时满足三条的：
#   对应数据文件已不存在、mtime 早于宽限（见上）、且能非阻塞拿到 flock（拿不到=有人正持锁）。
#   数据文件还在、宽限内、拿不到锁的一律不动；单文件失败只跳过，绝不把异常抛给维护巡。
# LLM: owner显式过滤逻辑锁并传固定目录FD，扫描/复核/删除不重走可被替换的路径；transition逐锁调用，默认Gateway不变。
# 函数用途: 清扫已登记目录的孤儿伴生锁，可由owner根固定目录句柄后安全回收。
def cleanup_orphan_lock_files(directory: Path, *, now: float | None = None,
                              data_sidecars_only: bool = False, commit_transition=None,
                              directory_fd: int | None = None) -> int:
    if directory_fd is None and not directory.is_dir():
        return 0
    current = time.time() if now is None else now
    try:
        candidates = (sorted(directory.glob("*.lock")) if directory_fd is None else
                      [directory / name for name in sorted(os.listdir(directory_fd)) if name.endswith(".lock")])
    except OSError:
        return 0
    removed = 0
    for lock_path in candidates:
        if data_sidecars_only and not lock_path.with_suffix("").suffix:
            continue
        # 每个候选的原试锁/复核/删除作为单独步骤，授权撤回不能被清扫器吞掉。
        # LLM: 同步transition只执行这一个已枚举锁；绑定路径防延迟闭包取到后续循环项。
        # 函数用途: 沿原试锁与身份核验尝试删除一个孤儿伴生文件。
        def operation(lock_path=lock_path):
            if directory_fd is not None:
                return _cleanup_orphan_lock_at(directory_fd, lock_path.name, current=current)
            return _cleanup_one_orphan_lock(lock_path, current=current)
        removed += bool(operation() if commit_transition is None else commit_transition(operation))
    return removed


# LLM: owner固定目录FD内只开现存单链接普通锁；fdopen接管失败关闭raw FD，try-flock后重核inode/年龄/数据缺失，unlink仍dirfd。
# 函数用途: 安全回收一个owner孤儿伴生锁，祖先路径被换成外部链接也不会改扫外部目录。
def _cleanup_orphan_lock_at(directory_fd: int, name: str, *, current: float) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or current - info.st_mtime < _ORPHAN_LOCK_MIN_AGE_SECONDS
                or _lock_data_exists_at(directory_fd, name)):
            return False
        descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        with handle:
            if not _try_flock_exclusive(handle):
                return False
            opened = os.fstat(handle.fileno())
            latest = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            identity = (info.st_dev, info.st_ino)
            if ((opened.st_dev, opened.st_ino) != identity or (latest.st_dev, latest.st_ino) != identity
                    or opened.st_nlink != 1 or not stat.S_ISREG(latest.st_mode)
                    or current - opened.st_mtime < _ORPHAN_LOCK_MIN_AGE_SECONDS
                    or _lock_data_exists_at(directory_fd, name)):
                return False
            os.unlink(name, dir_fd=directory_fd)
            return True
    except OSError:
        return False


# LLM: 仅在同一固定目录下查伴生数据名，软链接也视为有数据而保留锁，不能跟随到外部路径。
# 函数用途: 判断owner锁对应的数据名是否仍存在。
def _lock_data_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name[:-len(".lock")], dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


# LLM: 三条判据（孤儿、超宽限、非活锁）逐条早返回，单文件失败只跳过；拆出来同时压平整轮清扫的嵌套。
# 函数用途: 判断并尝试删除单个孤儿锁文件，真的删掉返回 True。
def _cleanup_one_orphan_lock(lock_path: Path, *, current: float) -> bool:
    if len(lock_path.name) <= len(".lock"):
        return False
    try:
        modified_at = lock_path.stat().st_mtime
    except OSError:
        return False
    if current - modified_at < _ORPHAN_LOCK_MIN_AGE_SECONDS:
        return False
    if lock_path.with_name(lock_path.name[: -len(".lock")]).exists():
        return False
    try:
        return _remove_orphan_lock_file(lock_path, current=current)
    except Exception:  # noqa: BLE001 单个坏锁文件不能中断整轮清扫
        return False


# LLM: 必须在锁文件本体上试锁：_open_lock_handle 是"数据文件路径"入口，会再加一层 .lock，
#   用它探测会永远拿得到 <name>.lock.lock，活锁保护形同虚设（obsfix34b 实测：真实持有者持锁时照样被删）。
#   拿不到锁说明有活进程正在用这把锁，直接放行不动文件。拿到后还必须核对 fd 与当前 lock_path 的 (st_dev, st_ino) 一致、
#   对应数据文件仍不存在且锁 mtime 仍超宽限，防止双清理者与业务写者交错时在旧 inode 上持锁却按路径误删新 inode。
#   Windows 上打开着的文件删不掉：unlink 失败就放弃本次删除（宁可留孤儿，不回退成先 close 再删）。
# 函数用途: 非阻塞试锁并在持锁状态下复核身份与孤儿条件后删掉孤儿锁文件，真的删掉返回 True。
def _remove_orphan_lock_file(lock_path: Path, *, current: float | None = None) -> bool:
    handle = os.fdopen(_open_private_lock_descriptor(lock_path), "a+", encoding="utf-8")
    try:
        if not _try_flock_exclusive(handle) or not _lock_handle_matches_path(handle, lock_path):
            return False
        if lock_path.with_name(lock_path.name[: -len(".lock")]).exists():
            return False
        if current is not None and current - os.fstat(handle.fileno()).st_mtime < _ORPHAN_LOCK_MIN_AGE_SECONDS:
            return False
        try:
            lock_path.unlink()
        except OSError:
            return False
        return True
    finally:
        handle.close()


# 流式 chunk 文件单次读取上限：整体 f.read() 无上限会 MemoryError，按上限分块读、剩余部分下一拍继续
# （CLI 与 TUI 两个网关客户端共用这一份）。
STREAM_CHUNK_READ_MAX_BYTES = 8 * 1024 * 1024


# LLM: Gateway JSONL 读者可遇写入中的半行；仅 LF 终结的完整行推进字节游标，尾部半个 UTF-8 留待下轮，修改须联查流式读取测试。
# 函数用途: 从字节游标读取一批完整 UTF-8 行，保留尚未写完的最后一行而不误报或丢失。
def read_complete_utf8_rows(
    path: Path,
    offset: int,
    *,
    max_bytes: int,
) -> tuple[tuple[str, ...], int, bool]:
    start = max(0, int(offset or 0))
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if start > size:
            start = 0
        handle.seek(start)
        data = handle.read(max(1, int(max_bytes)))
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return (), start, False
    complete = data[: boundary + 1]
    rows: list[str] = []
    decode_error = False
    for raw in complete.split(b"\n")[:-1]:
        try:
            rows.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            decode_error = True
            rows.append(raw.decode("utf-8", "replace"))
    return tuple(rows), start + len(complete), decode_error


# LLM: Gateway PID 读取仅解析文件整数，缺失或非法返回零；不证明进程存活，也不据此授予停止或回收权。
# 函数用途: 从 PID 文件取得数字，读不到时给状态展示返回零。
def read_pid(path: Path) -> int:

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


# LLM: Gateway 日志展示读口整文件读取并容错解码，OSError 返回空列表；非正行数返回全文，不可误作有界安全正文读口。
# 函数用途: 提取日志最后若干行，供诊断展示，不改变日志文件。
def tail_lines(path: Path, line_count: int) -> list[str]:

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0:
        return lines
    return lines[-line_count:]


# LLM: 前缀与工具层守卫共用 common.logical_reference_ids 的同一常量（禁止另写一份字面量）；
#   守卫按"前缀 + 冒号 + call 编号"形态拒绝把逻辑引用当写路径。
# 函数用途: 生成一次 Gateway 请求号（gwreq-<时间戳>-<随机 hex>）。
def new_gateway_request_id() -> str:

    return f"{GATEWAY_REQUEST_ID_PREFIX}{int(time.time())}-{uuid.uuid4().hex}"


# LLM: Gateway 响应路径由已核验 request_id 与当前 paths 拼接，此处不验证身份、不建目录；调用方必须先守住请求归属和 ID 合同。
# 函数用途: 取得某个请求在 responses 目录中的 JSON 文件路径。
def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:

    return paths.responses / f"{request_id}.json"


# LLM: 这些目录的 JSON 读/写都走 _locked_file_path（<name>.lock sidecar），文件归档/删除后 sidecar 会累积；
#   锁清理巡只扫这一份清单（队列四个目录 + 终态副本 + 响应 + 每个请求都会建锁的 turn_transitions），
#   新增别的锁目录要同步加进来。清理只删孤儿（见 cleanup_orphan_lock_files），活锁与在用文件不动。
# 函数用途: 列出会积累锁 sidecar 的 Gateway 目录，供周期清理。
def gateway_lock_sidecar_dirs(paths: GatewayPaths) -> tuple[Path, ...]:
    return (
        paths.inbox,
        paths.processing,
        paths.done,
        paths.failed,
        paths.terminal,
        paths.responses,
        paths.root / "turn_transitions",
    )


# LLM: 队列计数只投影目录事实，不读取请求或参与准入；scandir 复用目录类型，避免逐文件 stat 抢占状态线程。
# 函数用途: 轻量统计等待和执行数量，历史归档仅在调用方明确需要时纳入。
def gateway_request_counts(paths: GatewayPaths, *, include_archives: bool = True) -> dict[str, int]:

    # LLM: 保持只计 JSON 普通文件的原语义；有界迭代，不把队列正文或所有 Path 常驻内存。
    # 函数用途: 一次目录遍历计数，避免高并发时重复释放和争抢 GIL。
    def count_json(path: Path) -> int:
        try:
            with os.scandir(path) as entries:
                return sum(1 for item in entries if item.name.endswith(".json") and item.is_file())
        except OSError:
            return 0

    counts = {
        "pending": count_json(paths.inbox),
        "processing": count_json(paths.processing),
    }
    if include_archives:
        counts.update(
            {
                "done": count_json(paths.done),
                "failed": count_json(paths.failed),
                "responses": count_json(paths.responses),
            }
        )
    return counts


# LLM: Gateway 队列年龄只以 mtime 提供状态观测，不读正文、不授予调度或恢复权；修改字段须联查 heartbeat/status 消费方。
# 函数用途: 展示等待队列和执行队列中最老文件的年龄。
def gateway_queue_ages(paths: GatewayPaths) -> dict[str, float]:
    """结构化队列年龄观测：最老 pending 等待秒数与最老 processing lease 年龄。

    只用文件 mtime（结构化事实），不读文件内容；供 heartbeat / status 展示，
    不参与任何调度或恢复决策。"""
    now = time.time()

    # LLM: Gateway 年龄观测复用外层同一 now；目录或 stat 失败保守返回零，不将展示值作为租约失效证明。
    # 函数用途: 读取目录中 JSON 的最早修改时间并换算秒数，不写入队列。
    def oldest_age(path: Path) -> float:
        try:
            mtimes = [item.stat().st_mtime for item in path.glob("*.json") if item.is_file()]
        except OSError:
            return 0.0
        return round(now - min(mtimes), 3) if mtimes else 0.0

    return {
        "oldest_pending_age_seconds": oldest_age(paths.inbox),
        "oldest_processing_age_seconds": oldest_age(paths.processing),
    }


# LLM: 请求队列文件也是宿主数据；tmp 与目标都按私有权限落盘（目录 0700、文件出生 0600、
#   已有宽权限文件下次入队即收紧）。读取方（TUI、适配器、派活工具、后台服务）都是同一个系统用户。
# 函数用途: 把一个 Gateway 请求写进 inbox 队列并返回目标路径（仅本人可读写）。
def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:
    request_id = str(payload["id"])
    target = paths.inbox / f"{request_id}.json"
    write_private_json_file_atomic_no_newline(target, payload, sort_keys=True)
    from ..observability.concurrency_metrics import gateway_request_enqueued

    gateway_request_enqueued()  # §6-A 进队计数(与 claimed 对比:排队饿死 vs 认领后卡首轮一眼可分)
    return target


# LLM: 原T锁内允许可信来源最后复核后同步提交；cell只在本PID/thread有效一次，callback存在不授C/P1许可，退出即失效。
#   stable id重放沿原receipt/完整摘要，不复活归档任务；修改时联测输入重放及C后台来源生命周期。
# 函数用途: 按稳定请求ID只入队一次，可由原C生产者在同一输入事务内作最终核权。
def write_gateway_request_once(paths: GatewayPaths, payload: dict, *, commit_transition: Callable | None = None) -> tuple[Path, bool]:
    if commit_transition is not None and not callable(commit_transition):
        raise TypeError("gateway_request_commit_transition_required")
    request_id = str(payload.get("id") or "").strip()
    if not request_id:
        raise ValueError("gateway request id is required")
    expected_digest = _gateway_client_input_digest(payload)
    if not expected_digest:
        raise ValueError("gateway idempotent request requires client_input_digest")
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    from .input_delivery_service import (
        gateway_input_transition,
        load_or_prepare_gateway_input_locked,
        queue_gateway_input_locked,
    )

    window = [False, os.getpid(), threading.get_ident(), False]

    # LLM: 原cell只在本次T内启用；PID/thread不符、重复或退出后先拒绝，实际C/ports来源仍由外层真实原末门复核。
    # 函数用途: 在同一输入事务内提交一次prepare与queue，不允许保存裸写叶在稍后继续落盘。
    def commit():
        if not window[0] or window[1:3] != [os.getpid(), threading.get_ident()] or window[3]:
            raise ValueError("gateway_request_commit_window_required")
        window[3] = True
        receipt, _prepared = load_or_prepare_gateway_input_locked(
            paths,
            request_id=request_id,
            client_input_digest=expected_digest,
            client_message_id=str(metadata.get("message_id") or "").strip(),
            guidance_dedupe_key="",
            prepared_request=payload,
        )
        return queue_gateway_input_locked(paths, receipt)

    with gateway_input_transition(paths, request_id):
        window[0] = True
        try:
            receipt, created = commit() if commit_transition is None else commit_transition(commit)
        finally:
            window[0] = False
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        candidate = folder / f"{request_id}.json"
        if candidate.is_file():
            return candidate, created
    return gateway_response_path(paths, receipt.request_id), created


# LLM: Gateway 客户端输入摘要来自 HTTP/客户端结构化 metadata；缺值保持空，旧随机请求不能借缺值与幂等输入判等。
# 函数用途: 读取一次幂等 Gateway 请求的稳定输入指纹。
def _gateway_client_input_digest(payload: dict) -> str:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("client_input_digest") or "").strip()


# LLM: Gateway 历史兼容写口委托唯一去重实现，可能追加或原子修复磁盘历史；不返回是否写入，修改须联查 once 入口。
# 函数用途: 将请求历史同步到可重建的历史投影，按请求号去重。
def append_gateway_history(paths: GatewayPaths, payload: dict) -> None:
    append_gateway_history_once(paths, payload)


# LLM: Gateway 历史按规范请求号维护可重建投影；全局事务锁与版本缓存保护追加，冲突/重复行以规范正文原子替换，非终态权威。
#   落盘走私有追加/原子重写（0600/0700），存量宽权限历史下次写入即收紧；内容逐字节不变。
# 函数用途: 按请求 ID 最多保留一条准确历史记录，并返回本次是否实际写入或修复。
def append_gateway_history_once(paths: GatewayPaths, payload: dict) -> bool:
    request_id = str(payload.get("id") or payload.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("gateway history projection requires request_id")
    transition = paths.root / "history_transitions" / "history.transition"
    with locked_file_transition(transition):
        lines, index = _gateway_history_snapshot(paths.history)
        existing = index.get(request_id)
        if (
            existing is not None
            and not existing.conflict
            and existing.count == 1
            and existing.payload == payload
        ):
            return False
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if existing is None:
            append_private_jsonl_records(paths.history, [payload], sort_keys=True)
            retained = [*lines, serialized]
        else:
            retained: list[str] = []
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    retained.append(line)
                    continue
                row_id = (
                    str(row.get("id") or row.get("request_id") or "").strip()
                    if isinstance(row, dict)
                    else ""
                )
                if row_id != request_id:
                    retained.append(line)
            retained.append(serialized)
            write_private_text_file_atomic(paths.history, "\n".join(retained) + "\n")
        index[request_id] = _GatewayHistoryIndexEntry(dict(payload))
        _store_gateway_history_cache(paths.history, retained, index)
    return True


# LLM: 原行与索引保留冲突修复合同；单文件及总字节预算只限制内存驻留，不改变返回正文或磁盘权威。
# 函数用途: 读取 Gateway 历史一次并构建按请求 ID 查询的进程内索引。
def _gateway_history_snapshot(
    path: Path,
) -> tuple[list[str], dict[str, _GatewayHistoryIndexEntry]]:
    stamp = _gateway_history_stamp(path)
    key = str(path.resolve())
    with _GATEWAY_HISTORY_CACHE_LOCK:
        cached = _GATEWAY_HISTORY_CACHE.get(key)
    if cached is not None and cached[0] == stamp and stamp[1] <= _MAX_GATEWAY_HISTORY_CACHE_FILE_BYTES:
        return list(cached[1]), dict(cached[2])
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    lines = jsonl_lines(path.read_text(encoding="utf-8")) if path.exists() else []
    index: dict[str, _GatewayHistoryIndexEntry] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        request_id = str(row.get("id") or row.get("request_id") or "").strip()
        if not request_id:
            continue
        existing = index.get(request_id)
        if existing is None:
            index[request_id] = _GatewayHistoryIndexEntry(dict(row))
        else:
            index[request_id] = _GatewayHistoryIndexEntry(
                existing.payload,
                count=existing.count + 1,
                conflict=existing.conflict or existing.payload != row,
            )
    _cache_gateway_history_snapshot(key, stamp, lines, index)
    return lines, index


# LLM: 冷读与写后刷新共用同一锁内预算，超大项移除旧版本；按原插入顺序淘汰，不另存正文副本。
# 函数用途: 仅保留单文件不超4MiB、合计不超16MiB且条数有界的历史快照。
def _cache_gateway_history_snapshot(key: str, stamp: tuple[int, int], lines: list[str], index: dict) -> None:
    with _GATEWAY_HISTORY_CACHE_LOCK:
        if stamp[1] > _MAX_GATEWAY_HISTORY_CACHE_FILE_BYTES:
            _GATEWAY_HISTORY_CACHE.pop(key, None)
            return
        _GATEWAY_HISTORY_CACHE[key] = (stamp, tuple(lines), dict(index))
        total = sum(row[0][1] for row in _GATEWAY_HISTORY_CACHE.values())
        while _GATEWAY_HISTORY_CACHE and (
            len(_GATEWAY_HISTORY_CACHE) > _MAX_GATEWAY_HISTORY_CACHE_ENTRIES
            or total > _MAX_GATEWAY_HISTORY_CACHE_TOTAL_BYTES
        ):
            removed = _GATEWAY_HISTORY_CACHE.pop(next(iter(_GATEWAY_HISTORY_CACHE)))
            total -= removed[0][1]


# LLM: Gateway 历史缓存以 mtime/size 感知其它进程写入，仅作失效提示；不得将缓存戳当跨进程状态或执行授权。
# 函数用途: 返回历史文件当前缓存校验戳，不存在时用零值。
def _gateway_history_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return 0, 0
    return int(stat.st_mtime_ns), int(stat.st_size)


# LLM: 调用方已按原历史事务落盘；读新stat后仍经统一条数/字节准入，超大文件不保留旧快照。
# 函数用途: 在写入历史后同步刷新对应进程内索引缓存。
def _store_gateway_history_cache(
    path: Path,
    lines: list[str],
    index: dict[str, _GatewayHistoryIndexEntry],
) -> None:
    key = str(path.resolve())
    _cache_gateway_history_snapshot(key, _gateway_history_stamp(path), lines, index)
