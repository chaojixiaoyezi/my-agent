# LLM: 本模块只投影可信 owner 根内已提交事实；coverage 不是授权、终态、配额或 claim 证明。
# 模块用途: 为遥测、审计、调度历史和使用统计提供可删重建的有界 SQLite 读投影。
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import marshal
import math
import os
import secrets
import sqlite3
import stat
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import CancelledError
from contextlib import contextmanager
from pathlib import Path

from ..common.nofollow_fs import (
    NoFollowPathError,
    create_private_file_beneath,
    open_directory_beneath,
    open_readonly_file_beneath,
)

_SCHEMA = "owner_read_index.v1"
_DB_NAME = "owner_read_index.sqlite3"
_DB_PARTS = ("cache", _DB_NAME)
# owner 只读投影数据库允许占用的最大字节数。
_OWNER_INDEX_MAX_DB_BYTES = 128 * 1024 * 1024
# 单次投影源扫描允许读取的最大字节数。
_OWNER_INDEX_MAX_READ_BYTES = 32 * 1024 * 1024
# 单条投影源记录允许解析的最大字节数。
_OWNER_INDEX_MAX_ROW_BYTES = 1024 * 1024
# 单份投影文档允许解析的最大字节数。
_OWNER_INDEX_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
# 一次投影查询结果允许累计的最大字节数。
_OWNER_INDEX_MAX_RESULT_BYTES = 16 * 1024 * 1024
# 投影源文件每次分块读取的最大字节数。
_OWNER_INDEX_CHUNK_BYTES = 64 * 1024
# 投影批量查询一次最多接收的记录身份数量。
_MAX_BATCH_IDS = 4096
# 投影查询单页允许返回的最大记录行数。
_OWNER_INDEX_MAX_PAGE_ROWS = 1000
# 进程内投影源备忘缓存最多保留的条目数。
_MAX_MEMO_ENTRIES = 128
# 进程内投影源备忘缓存允许保留的总字节数。
_MAX_MEMO_BYTES = 32 * 1024 * 1024
# 单次投影推进最多跟踪的源身份数量。
_MAX_TRACKED_SOURCES = 128
_EVENTS = ("manual_search", "manual_get", "host_injected", "adopted")
# SQLite 有符号六十四位整数的最大值，属于类型边界而非可调配额。
_INT64_MAX = 2**63 - 1
# 投影追加见证按此字节数划分前缀摘要锚点。
_ANCHOR_BYTES = 1024
_PROCESS_MEMOS = OrderedDict()
_PROCESS_MEMO_LOCK = threading.RLock()
_PROCESS_ADVANCE_LOCK = threading.RLock()
_PROCESS_APPENDS = OrderedDict()


# LLM: 见证必须由持有领域锁的writer提供写后完整前缀字节并核对ori_spans摘要；前缀被等长改写即拒绝见证。
# 函数用途: 校验写后前缀与已发布检查点摘要一致后，保存进程内与SQLite持久追加见证。
def note_owner_append(home, parts, before, after, *, raw_after=None):
    if before is None or after is None or before[:2] != after[:2] or before[2] >= after[2]:
        return
    key = (str(Path(home).absolute()), tuple(parts))
    verified = False
    try:
        verified = bool(
            OwnerReadIndex(home)._record_append_witness(
                tuple(parts), before, after, raw_after=raw_after,
            )
        )
    except (_Issue, OSError, sqlite3.Error, ValueError, TypeError):
        verified = False
    with _PROCESS_MEMO_LOCK:
        if not verified:
            _PROCESS_APPENDS.pop(key, None)
            return
        chain = _PROCESS_APPENDS.pop(key, ())
        if not chain or chain[-1][1] != before:
            chain = ()
        _PROCESS_APPENDS[key] = (*chain[-127:], (before, after))
        while len(_PROCESS_APPENDS) > _MAX_MEMO_ENTRIES:
            _PROCESS_APPENDS.popitem(last=False)

_SHA256_INIT = (
    0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
    0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
)
_SHA256_K = (
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5, 0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
    0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3, 0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
    0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC, 0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
    0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7, 0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
    0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13, 0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
    0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3, 0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
    0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5, 0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
    0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208, 0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
)


# LLM: 按FIPS 180-4处理64字节分组，供跨进程可续前缀分块摘要计算使用。
# 函数用途: 更新SHA-256的8个32位状态字。
def _sha256_compress(state, block):
    mv = memoryview(block)
    h0, h1, h2, h3, h4, h5, h6, h7 = state
    for offset in range(0, len(mv), 64):
        w = list(struct.unpack(">16I", mv[offset:offset + 64])) + [0] * 48
        for idx in range(16, 64):
            s0 = ((w[idx - 15] >> 7) | (w[idx - 15] << 25)) ^ ((w[idx - 15] >> 18) | (w[idx - 15] << 14)) ^ (w[idx - 15] >> 3)
            s1 = ((w[idx - 2] >> 17) | (w[idx - 2] << 15)) ^ ((w[idx - 2] >> 19) | (w[idx - 2] << 13)) ^ (w[idx - 2] >> 10)
            w[idx] = (w[idx - 16] + s0 + w[idx - 7] + s1) & 0xFFFFFFFF
        a, b, c, d, e, f, g, h = h0, h1, h2, h3, h4, h5, h6, h7
        for idx in range(64):
            s1 = ((e >> 6) | (e << 26)) ^ ((e >> 11) | (e << 21)) ^ ((e >> 25) | (e << 7))
            ch = (e & f) ^ ((~e) & g)
            t1 = (h + s1 + ch + _SHA256_K[idx] + w[idx]) & 0xFFFFFFFF
            s0 = ((a >> 2) | (a << 30)) ^ ((a >> 13) | (a << 19)) ^ ((a >> 22) | (a << 10))
            maj = (a & b) ^ (a & c) ^ (b & c)
            t2 = (s0 + maj) & 0xFFFFFFFF
            h, g, f, e, d, c, b, a = g, f, e, (d + t1) & 0xFFFFFFFF, c, b, a, (t1 + t2) & 0xFFFFFFFF
        h0 = (h0 + a) & 0xFFFFFFFF
        h1 = (h1 + b) & 0xFFFFFFFF
        h2 = (h2 + c) & 0xFFFFFFFF
        h3 = (h3 + d) & 0xFFFFFFFF
        h4 = (h4 + e) & 0xFFFFFFFF
        h5 = (h5 + f) & 0xFFFFFFFF
        h6 = (h6 + g) & 0xFFFFFFFF
        h7 = (h7 + h) & 0xFFFFFFFF
    return [h0, h1, h2, h3, h4, h5, h6, h7]


# LLM: 仅保存8个uint32状态字、已处理字节数与不足64字节尾块，支持大span在冷进程小预算下持久续算。
# 类用途: 提供与hashlib.sha256逐位一致且可安全JSON序列化的增量摘要器。
class _ResumableSha256:
    def __init__(self, state=None, count=0, tail=b""):
        self._state = list(state) if state is not None else list(_SHA256_INIT)
        self._count = int(count)
        self._tail = bytes(tail)

    def copy(self):
        return _ResumableSha256(self._state, self._count, self._tail)

    def update(self, data):
        buf = self._tail + bytes(data)
        self._count += len(data)
        cut = (len(buf) // 64) * 64
        if cut:
            self._state = _sha256_compress(self._state, buf[:cut])
        self._tail = buf[cut:]

    def hexdigest(self):
        pad = self._tail + b"\x80"
        pad += b"\x00" * ((56 - len(pad)) % 64) + struct.pack(">Q", self._count * 8)
        return "".join(f"{word:08x}" for word in _sha256_compress(list(self._state), pad))

    def to_state(self):
        return [list(self._state), self._count, self._tail.hex()]

    @classmethod
    def from_state(cls, raw, expected_count):
        if (not isinstance(raw, list) or len(raw) != 3
                or not isinstance(raw[0], list) or len(raw[0]) != 8
                or any(type(w) is not int or not 0 <= w <= 0xFFFFFFFF for w in raw[0])
                or type(raw[1]) is not int or raw[1] != expected_count
                or not isinstance(raw[2], str) or len(raw[2]) >= 128):
            raise ValueError("invalid resumable sha256 state")
        tail = bytes.fromhex(raw[2])
        if len(tail) != expected_count % 64:
            raise ValueError("invalid resumable sha256 tail length")
        return cls(raw[0], raw[1], tail)

_FIELDS = {
    "telemetry": {
        "call_id": "TEXT", "tool_name": "TEXT", "owner_id": "TEXT",
        "task_id": "TEXT", "run_id": "TEXT", "parent_run_id": "TEXT",
        "root_run_id": "TEXT", "workflow_run_id": "TEXT", "step_id": "TEXT",
        "status": "TEXT", "error_code": "TEXT", "artifact_ref": "TEXT",
        "started_at": "REAL", "ended_at": "REAL", "duration_ms": "REAL",
        "output_bytes": "INTEGER",
    },
    "audit": {
        "entry_id": "TEXT", "timestamp": "REAL", "action": "TEXT",
        "user_id": "TEXT", "channel": "TEXT", "target_type": "TEXT",
        "target_id": "TEXT", "status": "TEXT", "ip_address": "TEXT",
        "user_agent": "TEXT",
    },
    "scheduler": {
        "run_id": "TEXT", "owner_id": "TEXT", "task_id": "TEXT",
        "status": "TEXT", "created_at": "TEXT", "ended_at": "TEXT",
    },
    "usage": {
        "record_type": "TEXT", "skill_id": "TEXT", "event": "TEXT",
        "kind": "TEXT", "source": "TEXT", "date": "TEXT", "at": "TEXT",
        "sequence": "INTEGER", "chars": "INTEGER",
    },
}
_TABLES = {domain: "ori_" + domain for domain in _FIELDS}
_ID_FIELDS = {
    "telemetry": "call_id", "audit": "entry_id",
    "scheduler": "run_id", "usage": "skill_id",
}
_TIME_FIELDS = {
    "telemetry": "started_at", "audit": "timestamp",
    "scheduler": "ended_at", "usage": "at",
}
_JSON_ROOTS = {
    "telemetry": {"schema", "args_summary", "metadata"},
    "audit": {"details", "metadata"},
    "scheduler": {"schema", "metadata", "event", "result"},
    "usage": {
        "totals", "by_skill", "daily_history", "last_sequence", "metadata",
    },
}


# LLM: 失败类别使用固定原因码，不回显原始记录、数据库 SQL 或任意异常文本。
# 类用途: 携带投影不可用或预算未完成状态。
class _Issue(Exception):
    # LLM: partial 只能表示明确的未完成证明，不能作为成功空值。
    # 函数用途: 保存错误原因与 coverage 状态。
    def __init__(self, reason, status="unavailable"):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# LLM: 所有事实正文读取都经过本预算；无可信追加见证时复核全部已投影分块。
# 类用途: 约束一次推进或查询的实际读取量、解码数和墙钟时间。
class _Budget:
    # LLM: 上限是模块维护边界，调用方只能收紧，不能无限放大。
    # 函数用途: 初始化一次操作的资源预算。
    def __init__(self, max_rows=25000, max_bytes=_OWNER_INDEX_MAX_READ_BYTES, max_ms=1500):
        for value in (max_rows, max_bytes, max_ms):
            if type(value) is not int or value <= 0:
                raise ValueError("budgets must be positive integers")
        self.max_rows = min(max_rows, 25000)
        self.remaining = min(max_bytes, _OWNER_INDEX_MAX_READ_BYTES)
        self.deadline = time.monotonic() + min(max_ms, 5000) / 1000
        self.decoded = 0
        self.read_bytes = 0

    # LLM: 已读缓冲可继续解析，但不能越过解码条数或时间预算。
    # 函数用途: 判断是否还能处理下一条记录。
    def active(self):
        return (
            self.decoded < self.max_rows
            and time.monotonic() < self.deadline
        )

    # LLM: pread 不共享文件游标，短读后仍须由来源稳定性检查裁决。
    # 函数用途: 从真实字节位置读取受预算约束的一块正文。
    def read(self, descriptor, offset, maximum):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            return b""
        size = min(maximum, self.remaining, _OWNER_INDEX_CHUNK_BYTES)
        if size <= 0:
            return b""
        data = os.pread(descriptor, size, offset)
        if not data:
            raise _Issue("source_short_read")
        self.remaining -= len(data)
        self.read_bytes += len(data)
        return data


# LLM: JSON 编码固定且禁止非有限数，用于检查点、签名及独立返回正文。
# 函数用途: 生成规范紧凑 JSON。
def _json(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )


# LLM: 五元 stamp 同时包含身份、长度及两种高精度时间。
# 函数用途: 提取来源文件的稳定性观察值。
def _stamp(info):
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


# LLM: 不规范化穿越、不接受路径字符串冒充路径组件集合。
# 函数用途: 校验只能在可信 home 下使用的相对路径段。
def _parts(value):
    if not isinstance(value, (tuple, list)) or not value:
        raise ValueError("relative_parts must be a nonempty tuple or list")
    result = tuple(value)
    for part in result:
        if (
            not isinstance(part, str) or not part or part in {".", ".."}
            or "/" in part or "\\" in part or "\0" in part
            or Path(part).is_absolute()
        ):
            raise ValueError("invalid relative path component")
    return result


# LLM: bool 不属于领域数值，NaN、Infinity 和负计数不能进入 typed 列。
# 函数用途: 校验有限非负数。
def _number(value):
    if (
        type(value) not in (int, float)
        or (type(value) is float and not math.isfinite(value)) or value < 0
    ):
        raise _Issue("invalid_schema")
    if type(value) is int and value > _INT64_MAX:
        raise _Issue("integer_out_of_range")
    return value


# LLM: JSON 的 NaN/Infinity 扩展不是可接受事实格式。
# 函数用途: 拒绝非标准 JSON 常量。
def _reject_constant(_value):
    raise ValueError("nonfinite JSON constant")


# LLM: 只允许本模块固定 SQL；即使缓存 schema 异常也不得借 ATTACH 或函数访问外部文件。
# 函数用途: 限制 SQLite 连接的外部能力。
def _authorize(action, arg1, arg2, _database, _trigger):
    denied = {
        sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE,
    }
    if action in denied:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        name = str(arg2 or arg1 or "").lower()
        if name in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


# LLM: 只从已观察异常类型和 SQLite 错误码分类，不猜测权限或隔离原因。
# 函数用途: 将底层错误映射为固定 coverage 原因。
def _failure(error):
    if isinstance(error, _Issue):
        return error.status, error.reason
    if isinstance(error, NoFollowPathError):
        return "unavailable", "unsafe_path"
    if isinstance(error, PermissionError):
        return "unavailable", "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "unavailable", "source_or_cache_missing"
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return "unavailable", "sqlite_busy"
        if code == sqlite3.SQLITE_INTERRUPT:
            return "partial", "sqlite_budget"
        return "unavailable", "sqlite_error"
    if isinstance(error, OSError):
        return "unavailable", "io_error"
    return "unavailable", "invalid_input"

# LLM: 旧list或int接口不能表达未知，异常保存独立coverage而非把故障变零。
# 类用途: 表示尚未取得完整来源覆盖证明。
class OwnerReadUnavailable(ValueError):
    # LLM: 只暴露固定原因，不把来源正文和底层异常文本带入日志。
    # 函数用途: 携带可供调用方展示的结构化未知状态。
    def __init__(self, coverage):
        self.coverage = copy.deepcopy(coverage)
        super().__init__('owner read incomplete: ' + str(coverage.get('reason', 'unknown')))


# LLM: partial或unavailable不能转换成旧接口的成功空值。
# 函数用途: 检查完整性后原样返回查询报告。
def require_complete(result):
    coverage = result.get('coverage') or {'status': 'unavailable', 'reason': 'missing_coverage'}
    if coverage.get('status') != 'complete':
        raise OwnerReadUnavailable(coverage)
    return result


# LLM: 构造只绑定宿主提供的 home；不从来源目录猜 owner，不连接数据库或创建目录。
# 类用途: 管理一个可信 owner 根下的分域派生读投影。
class OwnerReadIndex:
    # LLM: 实例状态只保存有界证明和未解码片段，不保存 lifetime ID 集合。
    # 函数用途: 纯绑定 owner 根、可选身份及实例级同步状态。
    def __init__(self, owner_home, *, owner_id=""):
        if not isinstance(owner_id, str):
            raise ValueError("owner_id must be a string")
        self._home = Path(os.path.abspath(os.path.expanduser(os.fspath(owner_home))))
        self._owner_id = owner_id
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._memo = OrderedDict()
        self._memo_bytes = 0
        self._seen = set()
        self._seen_saturated = False
        self._revoked = {}

    # LLM: source ID 绑定 home 内相对来源、领域和 owner 过滤配置。
    # 函数用途: 生成数据库内稳定的来源键。
    def _source_id(self, domain, parts):
        value = _json([domain, list(parts), self._owner_id]).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    # LLM: validator 版本变化应重建；普通函数的闭包计数器不属于格式版本。
    # 函数用途: 为校验器生成可持久化的格式标识。
    def _validator_tag(self, validator):
        if validator is None:
            return "builtin-v1"
        if not callable(validator):
            raise ValueError("validator must be callable")
        explicit = getattr(validator, "owner_read_index_schema", None)
        if isinstance(explicit, str) and explicit:
            return "explicit:" + explicit
        code = getattr(validator, "__code__", None)
        if code is not None:
            identity = marshal.dumps(code)
        else:
            cls = type(validator)
            identity = (cls.__module__ + ":" + cls.__qualname__).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    # LLM: 共享证明取出时锁内复制hasher，释放锁后才读取正文和推进，避免并发修改同一对象。
    # 函数用途: 获取一个有界内存证明或片段。
    def _memo_get(self, key):
        if key[0] == 'proof':
            shared = (str(self._home), self._owner_id, key)
            with _PROCESS_MEMO_LOCK:
                item = _PROCESS_MEMOS.get(shared)
                if item is not None:
                    _PROCESS_MEMOS.move_to_end(shared)
                    return (*item[:-1], item[-1].copy() if item[-1] is not None else None) if item is not True else True
            return None
        with self._lock:
            item = self._memo.get(key)
            if item is not None:
                self._memo.move_to_end(key)
                return item[0]
        return None

    # LLM: 证明发布时锁内复制hasher；其余大正文仅交换不可变引用并在锁外释放淘汰项。
    # 函数用途: 有界保存片段和可续前缀证明。
    def _memo_put(self, key, value, cost=0):
        if key[0] == 'proof':
            shared = (str(self._home), self._owner_id, key)
            with _PROCESS_MEMO_LOCK:
                _PROCESS_MEMOS[shared] = ((*value[:-1], value[-1].copy() if value[-1] is not None else None)
                                           if value is not True else True)
                _PROCESS_MEMOS.move_to_end(shared)
                while len(_PROCESS_MEMOS) > _MAX_MEMO_ENTRIES:
                    _PROCESS_MEMOS.popitem(last=False)
            return
        discarded = []
        with self._lock:
            old = self._memo.pop(key, None)
            if old is not None:
                self._memo_bytes -= old[1]
                discarded.append(old)
            if cost <= _MAX_MEMO_BYTES:
                self._memo[key] = (value, cost)
                self._memo_bytes += cost
            while (
                len(self._memo) > _MAX_MEMO_ENTRIES
                or self._memo_bytes > _MAX_MEMO_BYTES
            ):
                _, item = self._memo.popitem(last=False)
                self._memo_bytes -= item[1]
                discarded.append(item)
        discarded.clear()

    # LLM: 来源元数据有硬界且不淘汰已见事实；每源只需撤销当前检查点，旧代由DB代次拒绝。
    # 函数用途: 跟踪有限来源及其最新撤销代次，耗尽时显式partial而不遗忘。
    def _mark(self, sid, generation=None):
        with self._lock:
            if sid not in self._seen and len(self._seen) >= _MAX_TRACKED_SOURCES:
                self._seen_saturated = True
                raise _Issue('source_tracking_budget', 'partial')
            self._seen.add(sid)
            if generation:
                self._revoked[sid] = generation

    # LLM: 耗尽后不再为未知来源作首次不存在判断，防止漏记的存在被后来删除掩盖。
    # 函数用途: 查询保守的来源存在标记或当前撤销代次。
    def _marked(self, sid, generation=None):
        with self._lock:
            return (
                (sid in self._seen or self._seen_saturated) if generation is None
                else self._revoked.get(sid) == generation
            )

    # LLM: view 由本实例签名，调用方不能改 through 或把 partial 改成 complete。
    # 函数用途: 签署可独立传递的 coverage 字典。
    def _sign(self, value):
        result = dict(value)
        result.pop("_signature", None)
        result["_signature"] = hmac.new(
            self._key, _json(result).encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        return result

    # LLM: through 是已投影的真实字节边界；完整性只属于本次捕获范围。
    # 函数用途: 创建固定上界的查询 view。
    def _view(self, domain, parts, sid, checkpoint, status, reason, source_size=0):
        checkpoint = checkpoint or {}
        return self._sign({
            "view_version": 1,
            "domain": domain,
            "owner_id": self._owner_id,
            "source_parts": list(parts),
            "source_id": sid,
            "generation": checkpoint.get("generation", ""),
            "through": checkpoint.get("through", 0),
            "source_size": source_size,
            "status": status,
            "captured_status": status,
            "reason": reason,
        })

    # LLM: 临时复核不足不能篡改原始捕获范围或完整性证据。
    # 函数用途: 返回一次查询的 coverage 状态。
    def _coverage(self, view, status=None, reason=None):
        value = dict(view)
        if status is not None:
            value["status"] = status
        if reason is not None:
            value["reason"] = reason
        return self._sign(value)

    # LLM: 先验签再使用路径、领域和 SQL 参数，不接受跨实例或篡改 view。
    # 函数用途: 验证一个查询 view。
    def _validate_view(self, view):
        if not isinstance(view, dict):
            raise _Issue("invalid_view")
        value = dict(view)
        signature = value.pop("_signature", None)
        expected = hmac.new(
            self._key, _json(value).encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise _Issue("invalid_view")
        domain = value.get("domain")
        if domain not in _TABLES or value.get("view_version") != 1:
            raise _Issue("invalid_view")
        parts = _parts(value.get("source_parts"))
        if (
            value.get("owner_id") != self._owner_id
            or value.get("source_id") != self._source_id(domain, parts)
            or type(value.get("through")) is not int or value["through"] < 0
        ):
            raise _Issue("invalid_view")
        return dict(view), parts

    # LLM: 各 prefix 均复用 strict no-follow 原语，不把一次 resolve 当安全打开。
    # 函数用途: 捕获 home 到指定父目录的身份及目录变更时间。
    def _directories(self, parts):
        result = []
        for length in range(len(parts) + 1):
            descriptor = open_directory_beneath(
                self._home, parts[:length], require_dir_fd=True,
            )
            try:
                info = os.fstat(descriptor)
                result.append((info.st_dev, info.st_ino, info.st_ctime_ns))
            finally:
                os.close(descriptor)
        return tuple(result)

    # LLM: 初始 FD 与返回前路径均须仍为同一单链接普通文件。
    # 函数用途: 打开一个只读来源并捕获本次来源稳定性边界。
    def _open_source(self, parts):
        parents = self._directories(parts[:-1])
        descriptor = open_readonly_file_beneath(self._home, parts)
        try:
            current = _stamp(os.fstat(descriptor))
            self._check_source(descriptor, parts, current, parents)
            return descriptor, current, parents
        except BaseException:
            os.close(descriptor)
            raise

    # LLM: 打开旧 FD 不证明真实来源仍在；所有成功出口都重核规范路径和父链。
    # 函数用途: 验证一次读取前后来源身份、内容元数据和附着状态未改变。
    def _check_source(self, descriptor, parts, expected, parents):
        held = os.fstat(descriptor)
        if (
            not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
            or _stamp(held) != expected
        ):
            raise _Issue("source_changed")
        current_parents = self._directories(parts[:-1])
        if tuple(x[:2] for x in current_parents) != tuple(x[:2] for x in parents):
            raise _Issue("source_parent_changed")
        check = open_readonly_file_beneath(self._home, parts)
        try:
            if _stamp(os.fstat(check)) != expected:
                raise _Issue("source_changed")
        finally:
            os.close(check)

    # LLM: 缓存必须私有且属于当前宿主用户；不自动 chmod 他人或既有文件。
    # 函数用途: 验证缓存目录或文件的权限和链接属性。
    def _private(self, info, *, directory=False):
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (
            not correct_type or stat.S_IMODE(info.st_mode) & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or (not directory and info.st_nlink != 1)
        ):
            raise _Issue("unsafe_cache_permissions")

    # LLM: MEMORY journal 不需要磁盘侧文件；任何既有侧文件都不能被静默跟随或清理。
    # 函数用途: 拒绝数据库 journal、WAL 和 SHM 侧文件。
    def _reject_sidefiles(self):
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                descriptor = open_readonly_file_beneath(
                    self._home, ("cache", _DB_NAME + suffix),
                )
            except FileNotFoundError:
                continue
            else:
                os.close(descriptor)
                raise _Issue("sqlite_sidefile_present")

    # LLM: 主文件 FD 跨 connect 存活，目录和叶身份在 SQL 前再次核验。
    # 函数用途: 检查 SQLite 仍使用本次绑定的私有缓存位置。
    def _check_database(self, descriptor, cache_fd, parents, expected, *, opening=False):
        self._private(os.fstat(cache_fd), directory=True)
        held = os.fstat(descriptor)
        self._private(held)
        if (held.st_dev, held.st_ino) != expected[:2]:
            raise _Issue("cache_changed")
        if held.st_size > _OWNER_INDEX_MAX_DB_BYTES:
            raise _Issue("cache_size_limit")
        current_parents = self._directories(("cache",))
        if current_parents != parents:
            raise _Issue("cache_parent_changed")
        check = open_readonly_file_beneath(self._home, _DB_PARTS)
        try:
            actual = os.fstat(check)
            self._private(actual)
            if (actual.st_dev, actual.st_ino) != expected[:2]:
                raise _Issue("cache_changed")
            if opening and (
                _stamp(actual) != expected or _stamp(held) != expected
            ):
                raise _Issue("cache_changed_during_connect")
        finally:
            os.close(check)
        self._reject_sidefiles()

    # LLM: schema 只包含固定四域 typed 表及来源检查点，不承载领域写事实。
    # 函数用途: 在同一事务中初始化派生数据库。
    def _initialize(self, connection, binding):
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE ori_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE ori_sources (
                    sid TEXT PRIMARY KEY, domain TEXT NOT NULL,
                    parts TEXT NOT NULL, validation TEXT NOT NULL,
                    generation TEXT NOT NULL, revision INTEGER NOT NULL,
                    stamp TEXT NOT NULL, through INTEGER NOT NULL,
                    sealed INTEGER NOT NULL, complete INTEGER NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE ori_spans (
                    sid TEXT NOT NULL, generation TEXT NOT NULL,
                    start INTEGER NOT NULL, stop INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (sid, generation, start)
                )"""
            )
            for domain, fields in _FIELDS.items():
                table = _TABLES[domain]
                columns = ", ".join(name + " " + kind for name, kind in fields.items())
                connection.execute(
                    f"""CREATE TABLE {table} (
                        sid TEXT NOT NULL, generation TEXT NOT NULL,
                        pos INTEGER NOT NULL, stop INTEGER NOT NULL,
                        payload TEXT NOT NULL, {columns},
                        PRIMARY KEY (sid, generation, pos, stop)
                    )"""
                )
                identity = _ID_FIELDS[domain]
                connection.execute(
                    f"CREATE INDEX {table}_identity ON {table}"
                    f"(sid, generation, {identity}, pos, stop)"
                )
                time_field = _TIME_FIELDS[domain]
                connection.execute(
                    f"CREATE INDEX {table}_time ON {table}"
                    f"(sid, generation, {time_field}, pos, stop)"
                )
            connection.execute(
                "CREATE INDEX ori_telemetry_duration ON ori_telemetry"
                "(sid, generation, duration_ms, pos, stop)"
            )
            connection.executemany(
                "INSERT INTO ori_meta(key,value) VALUES (?,?)",
                [("schema", _SCHEMA), ("binding", binding)],
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    # LLM: timeout 为零；无 ATTACH、无全库 VACUUM、无磁盘临时表或磁盘 journal。
    # 函数用途: 提供始终关闭的、带身份检查和执行预算的短 SQLite 连接。
    @contextmanager
    def _database(self, *, create=False, write=False, deadline=None):
        cache_fd = descriptor = -1
        connection = None
        try:
            cache_fd = open_directory_beneath(
                self._home, ("cache",), create=create, require_dir_fd=True,
            )
            self._private(os.fstat(cache_fd), directory=True)
            try:
                descriptor = open_readonly_file_beneath(self._home, _DB_PARTS)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    seed = create_private_file_beneath(self._home, _DB_PARTS)
                except FileExistsError:
                    pass
                else:
                    os.close(seed)
                descriptor = open_readonly_file_beneath(self._home, _DB_PARTS)
            held = os.fstat(descriptor)
            self._private(held)
            if held.st_size > _OWNER_INDEX_MAX_DB_BYTES:
                raise _Issue("cache_size_limit")
            self._reject_sidefiles()
            expected = _stamp(held)
            parents = self._directories(("cache",))
            binding = _json({
                "home": str(self._home), "root": list(parents[0][:2]),
            })
            if held.st_size:
                header = os.pread(descriptor, 20, 0)
                if (
                    len(header) != 20
                    or header[:16] != b"SQLite format 3\0"
                    or header[18:20] != b"\x01\x01"
                ):
                    raise _Issue("unsupported_or_corrupt_cache")
            address = (self._home / "cache" / _DB_NAME).as_uri()
            address += "?mode=" + ("rw" if write else "ro")
            connection = sqlite3.connect(
                address, uri=True, timeout=0.0, isolation_level=None,
            )
            self._check_database(
                descriptor, cache_fd, parents, expected, opening=True,
            )
            connection.row_factory = sqlite3.Row
            connection.set_authorizer(_authorize)
            connection.enable_load_extension(False)
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA mmap_size=0")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("PRAGMA foreign_keys=ON")
            deadline = deadline if deadline is not None else time.monotonic() + 1.5
            steps = 0

            # LLM: 执行中断是明确 partial，不被包装成查询成功零行。
            # 函数用途: 限制数据库虚拟机工作量及墙钟时间。
            def progress():
                nonlocal steps
                steps += 1000
                return int(steps > 2_000_000 or time.monotonic() >= deadline)

            connection.set_progress_handler(progress, 1000)
            if hasattr(connection, "setlimit"):
                connection.setlimit(
                    sqlite3.SQLITE_LIMIT_LENGTH, _OWNER_INDEX_MAX_DOCUMENT_BYTES + 65536,
                )
                connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            if write:
                mode = connection.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
                if str(mode).lower() != "memory":
                    raise _Issue("unsupported_journal_mode")
                connection.execute("PRAGMA synchronous=FULL")
            else:
                connection.execute("PRAGMA query_only=ON")
            if held.st_size == 0:
                if not write:
                    raise _Issue("empty_cache")
                self._initialize(connection, binding)
            else:
                metadata = dict(
                    connection.execute("SELECT key,value FROM ori_meta WHERE key IN ('schema','binding')").fetchall()
                )
                if metadata != {"schema": _SCHEMA, "binding": binding}:
                    raise _Issue("cache_binding_mismatch")
            if write:
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute(
                    "PRAGMA max_page_count=" + str(_OWNER_INDEX_MAX_DB_BYTES // page_size)
                )
            self._check_database(descriptor, cache_fd, parents, expected)
            yield connection
            self._check_database(descriptor, cache_fd, parents, expected)
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                try:
                    if descriptor >= 0:
                        os.close(descriptor)
                finally:
                    if cache_fd >= 0:
                        os.close(cache_fd)

    # LLM: 检查点损坏不能被解释成冷空库，且 through 必须落在原真实来源边界内。
    # 函数用途: 读取并验证一个来源的已提交检查点。
    def _checkpoint(self, connection, sid, domain, parts):
        row = connection.execute(
            "SELECT * FROM ori_sources WHERE sid=?", (sid,),
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            source_stamp = tuple(json.loads(value["stamp"]))
            valid = (
                value["domain"] == domain and value["parts"] == _json(list(parts))
                and len(source_stamp) == 5
                and all(type(x) is int for x in source_stamp)
                and isinstance(value["generation"], str) and bool(value["generation"])
                and isinstance(value["validation"], str)
                and type(value["revision"]) is int and value["revision"] > 0
                and type(value["through"]) is int
                and type(value["sealed"]) is int
                and 0 <= value["sealed"] <= value["through"] <= source_stamp[2]
                and value["complete"] in (0, 1)
            )
        except (TypeError, ValueError, KeyError):
            valid = False
        if not valid:
            raise _Issue("corrupt_checkpoint")
        value["stamp"] = source_stamp
        return value

    # LLM: 首次无来源不能因探测缓存而创建目录、种子或数据库。
    # 函数用途: 只读探测一个已存在的检查点。
    def _existing_checkpoint(self, sid, domain, parts, deadline):
        try:
            with self._database(deadline=deadline) as connection:
                return self._checkpoint(connection, sid, domain, parts)
        except FileNotFoundError:
            return None

    # LLM: 不为writer创建缓存；必须核对写后前缀字节与检查点全部ori_spans及链式前缀摘要一致才发布见证。
    # 函数用途: 持久保存经前缀摘要核实的追加见证，拦截before/after窗口内的等长前缀改写。
    def _record_append_witness(self, parts, before, after, *, raw_after=None):
        if not isinstance(raw_after, (bytes, bytearray)) or len(raw_after) != after[2] or before[2] > len(raw_after):
            return False
        sid = self._source_id('telemetry', parts)
        with self._database(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            checkpoint = self._checkpoint(connection, sid, 'telemetry', parts)
            if checkpoint is None or checkpoint['through'] > before[2]:
                connection.rollback()
                return False
            base = [checkpoint['generation'], checkpoint['through'], list(checkpoint['stamp'])]
            value = connection.execute('SELECT value FROM ori_meta WHERE key=?', ('append:' + sid,)).fetchone()
            previous = json.loads(value[0]) if value is not None else None
            chained = (
                isinstance(previous, list)
                and len(previous) == 5
                and isinstance(previous[4], str)
                and len(previous[4]) == 64
                and all(ch in '0123456789abcdef' for ch in previous[4])
                and previous[:3] == base
                and previous[3] == list(before)
            )
            if checkpoint['stamp'] != before and not chained:
                connection.execute('DELETE FROM ori_meta WHERE key=?', ('append:' + sid,))
                connection.commit()
                return False
            spans = connection.execute(
                'SELECT start,stop,digest FROM ori_spans WHERE sid=? AND generation=? ORDER BY start',
                (sid, checkpoint['generation']),
            ).fetchall()
            pos = 0
            for span in spans:
                start, stop, digest = span['start'], span['stop'], span['digest']
                if start != pos or stop > checkpoint['through']:
                    connection.execute('DELETE FROM ori_meta WHERE key=?', ('append:' + sid,))
                    connection.commit()
                    return False
                if not hmac.compare_digest(hashlib.sha256(raw_after[start:stop]).hexdigest(), digest):
                    connection.execute('DELETE FROM ori_meta WHERE key=?', ('append:' + sid,))
                    connection.commit()
                    return False
                pos = stop
            if pos != checkpoint['through']:
                connection.execute('DELETE FROM ori_meta WHERE key=?', ('append:' + sid,))
                connection.commit()
                return False
            if chained:
                before_digest = hashlib.sha256(raw_after[:before[2]]).hexdigest()
                if not hmac.compare_digest(before_digest, previous[4]):
                    connection.execute('DELETE FROM ori_meta WHERE key=?', ('append:' + sid,))
                    connection.commit()
                    return False
            after_digest = hashlib.sha256(raw_after).hexdigest()
            connection.execute(
                'INSERT INTO ori_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('append:' + sid, _json([*base, list(after), after_digest])),
            )
            connection.commit()
            return True

    # LLM: 只有经写后ori_spans前缀摘要核实的5字段追加见证可免重复读盘；旧4字段见证一律降级走分块重验。
    # 函数用途: 判断检查点到当前来源是否存在已验证前缀完整性的可信追加见证。
    def _witnessed_append(self, connection, checkpoint, observed):
        key = (str(self._home), tuple(json.loads(checkpoint['parts'])))
        with _PROCESS_MEMO_LOCK:
            chain = _PROCESS_APPENDS.get(key, ())
            stamp = checkpoint['stamp']
            for before, after in chain:
                if before == stamp:
                    stamp = after
                    if stamp == observed:
                        return True
        saved = connection.execute('SELECT value FROM ori_meta WHERE key=?', ('append:' + checkpoint['sid'],)).fetchone()
        if saved is not None:
            try:
                proof = json.loads(saved[0])
            except (ValueError, TypeError):
                raise _Issue('corrupt_append_witness') from None
            return (
                isinstance(proof, list)
                and len(proof) == 5
                and isinstance(proof[4], str)
                and len(proof[4]) == 64
                and all(ch in '0123456789abcdef' for ch in proof[4])
                and proof[:4] == [checkpoint['generation'], checkpoint['through'], list(checkpoint['stamp']), list(observed)]
            )
        return False

    # LLM: 持久续点绑定当前generation、through和来源stamp；支持保存span内增量摘要状态供冷进程小预算续跑。
    # 函数用途: 保存当前来源stamp对应的有限分块证明水位与可选块内摘要状态。
    def _save_proof(self, connection, checkpoint, observed, position, mid_span=None):
        if connection.execute('PRAGMA query_only').fetchone()[0]:
            return
        payload = [checkpoint['generation'], checkpoint['through'], list(observed), position]
        if mid_span is not None:
            payload.append(mid_span)
        value = _json(payload)
        connection.execute(
            'INSERT INTO ori_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('proof:' + checkpoint['sid'], value),
        )

    # LLM: 每一未见证的已发布字节均参与摘要复核，见证本身必须在写后核实前缀ori_spans摘要；共享hasher锁内复制。
    # 函数用途: 有界、可跨实例且可跨冷进程地核验检查点的完整已验证前缀。
    def _prove_prefix(self, connection, descriptor, checkpoint, observed, budget):
        sid = checkpoint["sid"]
        generation = checkpoint["generation"]
        target = checkpoint["through"]
        key = (
            "proof", sid, generation, checkpoint['validation'],
            observed, target,
        )
        if self._witnessed_append(connection, checkpoint, observed):
            return True
        cached = self._memo_get(key)
        if cached is True:
            return True
        if cached is None:
            position, span_start, span_stop, digest, hasher = 0, 0, 0, "", None
            saved = connection.execute('SELECT value FROM ori_meta WHERE key=?', ('proof:' + sid,)).fetchone()
            if saved is not None:
                try:
                    proof = json.loads(saved[0])
                    if (isinstance(proof, list) and len(proof) in (4, 5)
                            and proof[:3] == [generation, target, list(observed)]
                            and type(proof[3]) is int and 0 <= proof[3] <= target):
                        position = proof[3]
                        if len(proof) == 5 and proof[4] is not None:
                            mid = proof[4]
                            if (not isinstance(mid, list) or len(mid) != 4
                                    or type(mid[0]) is not int or type(mid[1]) is not int
                                    or not 0 <= mid[0] < position < mid[1] <= target
                                    or not isinstance(mid[2], str) or len(mid[2]) != 64):
                                raise ValueError("invalid mid-span proof")
                            span_row = connection.execute(
                                """SELECT start,stop,digest FROM ori_spans
                                   WHERE sid=? AND generation=? AND start=?""",
                                (sid, generation, mid[0]),
                            ).fetchone()
                            if (span_row is None or span_row["stop"] != mid[1]
                                    or span_row["digest"] != mid[2]):
                                raise ValueError("mismatched mid-span proof")
                            span_start, span_stop, digest = mid[0], mid[1], mid[2]
                            hasher = _ResumableSha256.from_state(mid[3], position - span_start)
                except (ValueError, TypeError, IndexError):
                    raise _Issue('corrupt_prefix_proof') from None
        else:
            position, span_start, span_stop, digest, old_hasher = cached
            hasher = old_hasher.copy() if old_hasher is not None else None
        while position < target:
            if budget.remaining <= 0 or time.monotonic() >= budget.deadline:
                state = (position, span_start, span_stop, digest, hasher)
                self._memo_put(key, state)
                mid_span = ([span_start, span_stop, digest, hasher.to_state()]
                            if hasher is not None else None)
                self._save_proof(connection, checkpoint, observed, position, mid_span)
                return None
            if hasher is None:
                span = connection.execute(
                    """SELECT start,stop,digest FROM ori_spans
                       WHERE sid=? AND generation=? AND start=?""",
                    (sid, generation, position),
                ).fetchone()
                if span is None or not position < span["stop"] <= target:
                    raise _Issue("corrupt_prefix_spans")
                span_start, span_stop, digest = (
                    span["start"], span["stop"], span["digest"],
                )
                hasher = _ResumableSha256()
            data = budget.read(descriptor, position, span_stop - position)
            if not data:
                state = (position, span_start, span_stop, digest, hasher)
                self._memo_put(key, state)
                mid_span = ([span_start, span_stop, digest, hasher.to_state()]
                            if hasher is not None else None)
                self._save_proof(connection, checkpoint, observed, position, mid_span)
                return None
            hasher.update(data)
            position += len(data)
            if position == span_stop:
                if not hmac.compare_digest(hasher.hexdigest(), digest):
                    return False
                hasher = None
        self._save_proof(connection, checkpoint, observed, position)
        self._memo_put(key, True)
        return True

    # LLM: summary 只作为规范 JSON 文档投影，不从裁剪明细重造 lifetime。
    # 函数用途: 检查使用汇总中的累计计数和原水位。
    def _validate_summary(self, row):
        if any(
            not isinstance(row.get(key), dict)
            for key in ("totals", "by_skill", "daily_history")
        ):
            raise _Issue("invalid_schema")
        total = 0
        for event in _EVENTS:
            value = row["totals"].get(event, 0)
            if type(value) is not int or value < 0:
                raise _Issue("invalid_schema")
            total += value
        sequence = row.get("last_sequence", total)
        if type(sequence) is not int or sequence != total:
            raise _Issue("invalid_schema")
        for collection in ("by_skill", "daily_history"):
            for key, counts in row[collection].items():
                if not isinstance(key, str) or not isinstance(counts, dict):
                    raise _Issue("invalid_schema")
                for event in _EVENTS:
                    value = counts.get(event, 0)
                    if type(value) is not int or value < 0:
                        raise _Issue("invalid_schema")
        return sequence

    # LLM: typed 列只来自通过领域基本结构门的原记录，不补造正文或业务事实。
    # 函数用途: 校验四域基本结构并提取允许查询的类型化字段。
    def _typed(self, domain, row, document):
        if not isinstance(row, dict):
            raise _Issue("invalid_schema")
        values = {
            name: row.get(name, "" if kind == "TEXT" else 0)
            for name, kind in _FIELDS[domain].items()
        }
        if domain == "telemetry":
            if row.get("schema") != "tool_call_telemetry.v1":
                raise _Issue("invalid_schema")
            for key in ("call_id", "tool_name"):
                if not isinstance(row.get(key), str) or not row[key]:
                    raise _Issue("invalid_schema")
            for key in ("started_at", "ended_at", "duration_ms"):
                if key not in row:
                    raise _Issue("invalid_schema")
                values[key] = _number(row[key])
            values["output_bytes"] = _number(row.get("output_bytes", 0))
            if (
                type(values["output_bytes"]) is not int
                or values["ended_at"] < values["started_at"]
                or not isinstance(row.get('status', 'ok'), str)
                or row.get("status", "ok") not in {"ok", "error", "denied", "timeout"}
                or not isinstance(row.get("args_summary", {}), dict)
            ):
                raise _Issue("invalid_schema")
            values["status"] = row.get("status", "ok")
        elif domain == "audit":
            required = {
                "entry_id", "timestamp", "action", "user_id", "channel",
                "target_type", "target_id", "status",
            }
            optional = {"details", "ip_address", "user_agent"}
            if (
                not required.issubset(row) or set(row) - required - optional
                or not isinstance(row.get("details", {}), dict)
                or not isinstance(row["entry_id"], str) or not row["entry_id"]
            ):
                raise _Issue("invalid_schema")
            values["timestamp"] = _number(row["timestamp"])
        elif domain == "scheduler":
            # LLM: 去重 membership 保留原始 dict run_id 口径；严格公开历史校验由 validator 注入。
            # 局部用途: 提取原始调度历史键而不接管 owner 事务或终态合法性。
            run_id = row.get("run_id") or ""
            if not isinstance(run_id, (str, int, float, bool)):
                raise _Issue("invalid_schema")
            values["run_id"] = str(run_id)
            for key in ("created_at", "ended_at"):
                raw = row.get(key)
                if raw is None:
                    values[key] = ""
                elif isinstance(raw, str):
                    values[key] = raw
                elif type(raw) in (int, float) and math.isfinite(raw):
                    values[key] = str(raw)
                else:
                    raise _Issue("invalid_schema")
            for key in ("owner_id", "task_id", "status"):
                if row.get(key) is None:
                    values[key] = ""
            if "schema" in row and not isinstance(row["schema"], str):
                raise _Issue("invalid_schema")
        elif document:
            values = {
                name: "" if kind == "TEXT" else 0
                for name, kind in _FIELDS[domain].items()
            }
            values["record_type"] = "summary"
            values["sequence"] = self._validate_summary(row)
        else:
            if (
                not isinstance(row.get('event'), str)
                or row.get("event") not in _EVENTS
                or not isinstance(row.get("skill_id"), str) or not row["skill_id"]
                or not isinstance(row.get('kind', 'skill'), str)
                or row.get("kind", "skill") not in {"skill", "capability_package"}
            ):
                raise _Issue("invalid_schema")
            values["record_type"] = "event"
            values["kind"] = row.get("kind", "skill")
            for key in ("at", "date", "source"):
                if not isinstance(row.get(key), str):
                    raise _Issue("invalid_schema")
            for key in ("chars", "sequence"):
                value = row.get(key, 0)
                if type(value) is not int or value < 0:
                    raise _Issue("invalid_schema")
                values[key] = value
        for key, kind in _FIELDS[domain].items():
            if kind == "TEXT" and not isinstance(values[key], str):
                raise _Issue("invalid_schema")
            if kind == 'INTEGER':
                if type(values[key]) is not int:
                    raise _Issue('invalid_schema')
                if not -2**63 <= values[key] <= _INT64_MAX:
                    raise _Issue('integer_out_of_range')
        if domain in {"telemetry", "scheduler"}:
            owner = values["owner_id"]
            if self._owner_id and owner not in ("", self._owner_id):
                raise _Issue("owner_mismatch")
        return tuple(values[name] for name in _FIELDS[domain])

    # LLM: 每次真实 JSON 解码成功后恰调用一次 validator；取消异常不能变成假空。
    # 函数用途: 解码一条新记录并生成独立的缓存正文及 typed 值。
    def _decode(self, raw, domain, document, validator, budget):
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _Issue("invalid_utf8") from None
        try:
            row = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise _Issue("invalid_json") from None
        budget.decoded += 1
        if validator is not None:
            try:
                accepted = validator(copy.deepcopy(row))
            except CancelledError:
                raise
            except Exception:
                raise _Issue("validator_rejected") from None
            if accepted is not None and accepted is not True:
                raise _Issue("validator_rejected")
        typed = self._typed(domain, row, document)
        try:
            payload = _json(row)
            size = len(payload.encode("utf-8"))
        except (ValueError, TypeError, RecursionError):
            raise _Issue("invalid_schema") from None
        if size > (_OWNER_INDEX_MAX_DOCUMENT_BYTES if document else _OWNER_INDEX_MAX_ROW_BYTES):
            raise _Issue("record_size_limit")
        return payload, typed

    # LLM: 无 LF 末行可见但保留其起点；追加后重读该行，并保留旧 through 可见版本。
    # 函数用途: 按真实 byte offset 解析有限新记录，返回可提交前缀及未解码片段。
    def _parse(self, descriptor, checkpoint, observed, document, validator, budget):
        domain = checkpoint["domain"]
        start = 0 if document else checkpoint["sealed"]
        if not document and checkpoint["sealed"] == checkpoint["through"]:
            start = checkpoint["through"]
        key = (
            "fragment", checkpoint["sid"], checkpoint["generation"],
            checkpoint["revision"], observed, start,
        )
        cached = self._memo_get(key)
        buffer = bytearray(cached if isinstance(cached, bytes) else b"")
        target = observed[2]
        maximum = _OWNER_INDEX_MAX_DOCUMENT_BYTES if document else _OWNER_INDEX_MAX_ROW_BYTES
        if document and target > maximum:
            raise _Issue("document_size_limit")
        if start + len(buffer) > target:
            raise _Issue("corrupt_fragment")
        cursor = start
        through = checkpoint["through"]
        sealed = checkpoint["sealed"]
        records = []
        committed = bytearray()
        error = None
        while budget.active():
            if document:
                if len(buffer) < target:
                    data = budget.read(descriptor, len(buffer), target - len(buffer))
                    if not data:
                        break
                    buffer.extend(data)
                    continue
                try:
                    payload, typed = self._decode(
                        bytes(buffer), domain, True, validator, budget,
                    )
                except _Issue as failure:
                    error = failure.reason
                    break
                records.append((0, target, payload, typed))
                committed.extend(buffer)
                buffer.clear()
                through = sealed = target
                break
            newline = buffer.find(b"\n")
            at_target = cursor + len(buffer) == target
            if newline < 0 and not at_target:
                if len(buffer) > maximum:
                    error = "record_size_limit"
                    break
                data = budget.read(
                    descriptor, cursor + len(buffer),
                    min(target - cursor - len(buffer), maximum + 1 - len(buffer)),
                )
                if not data:
                    break
                buffer.extend(data)
                continue
            if newline < 0:
                if not buffer:
                    break
                length = len(buffer)
                has_lf = False
            else:
                length = newline + 1
                has_lf = True
            if length > maximum:
                error = "record_size_limit"
                break
            raw = bytes(buffer[:length])
            try:
                payload, typed = self._decode(
                    raw, domain, False, validator, budget,
                )
            except _Issue as failure:
                error = failure.reason
                break
            stop = cursor + length
            records.append((cursor, stop, payload, typed))
            committed.extend(raw)
            del buffer[:length]
            cursor = stop
            through = stop
            if has_lf:
                sealed = stop
            if not has_lf:
                break
        skip = max(0, checkpoint["through"] - start)
        delta = bytes(committed[skip:])
        if len(delta) != through - checkpoint["through"]:
            raise _Issue("invalid_parse_boundary")
        return {
            "records": records,
            "delta": delta,
            "through": through,
            "sealed": sealed,
            "fragment": bytes(buffer),
            "error": error,
        }

    # LLM: 哈希和大正文准备均在 SQL 事务及内存锁外完成。
    # 函数用途: 为新发布字节生成连续、不重叠的完整摘要区间。
    def _new_spans(self, checkpoint, delta):
        result = []
        start = checkpoint["through"]
        for offset in range(0, len(delta), _ANCHOR_BYTES):
            data = delta[offset:offset + _ANCHOR_BYTES]
            stop = start + len(data)
            result.append((
                checkpoint["sid"], checkpoint["generation"],
                start, stop, hashlib.sha256(data).hexdigest(),
            ))
            start = stop
        return result

    # LLM: 来源检查点、字节摘要和正文在同一 SQLite 事务发布，旧 flight 必须通过 revision/CAS。
    # 函数用途: 原子提交一个有限批次，不修改任何规范领域来源。
    def _publish(self, connection, old, checkpoint, parsed, observed, validation):
        spans = self._new_spans(checkpoint, parsed["delta"])
        table = _TABLES[checkpoint["domain"]]
        fields = list(_FIELDS[checkpoint["domain"]])
        columns = ["sid", "generation", "pos", "stop", "payload", *fields]
        placeholders = ",".join("?" for _ in columns)
        rows = [
            (
                checkpoint["sid"], checkpoint["generation"], pos, stop,
                payload, *typed,
            )
            for pos, stop, payload, typed in parsed["records"]
        ]
        complete = not parsed["error"] and parsed["through"] == observed[2]
        updated = {
            **checkpoint,
            "revision": 1 if old is None else old["revision"] + 1,
            "stamp": observed,
            "validation": validation,
            "through": parsed["through"],
            "sealed": parsed["sealed"],
            "complete": int(complete),
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT generation,revision FROM ori_sources WHERE sid=?",
                (checkpoint["sid"],),
            ).fetchone()
            if old is None:
                matches = current is None
            else:
                matches = (
                    current is not None
                    and current["generation"] == old["generation"]
                    and current["revision"] == old["revision"]
                )
            if not matches:
                raise _Issue("publication_conflict", "partial")
            if old is not None and old["generation"] != checkpoint["generation"]:
                connection.execute(
                    f"DELETE FROM {table} WHERE sid=?", (checkpoint["sid"],),
                )
                connection.execute(
                    "DELETE FROM ori_spans WHERE sid=?", (checkpoint["sid"],),
                )
            if rows:
                connection.executemany(
                    f"INSERT INTO {table} ({','.join(columns)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
            if spans:
                connection.executemany(
                    "INSERT INTO ori_spans(sid,generation,start,stop,digest)"
                    " VALUES (?,?,?,?,?)", spans,
                )
            connection.execute(
                """INSERT INTO ori_sources
                   (sid,domain,parts,validation,generation,revision,stamp,
                    through,sealed,complete)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sid) DO UPDATE SET
                     domain=excluded.domain, parts=excluded.parts,
                     validation=excluded.validation,
                     generation=excluded.generation, revision=excluded.revision,
                     stamp=excluded.stamp, through=excluded.through,
                     sealed=excluded.sealed, complete=excluded.complete""",
                (
                    updated["sid"], updated["domain"], updated["parts"],
                    validation, updated["generation"], updated["revision"],
                    _json(list(observed)), updated["through"],
                    updated["sealed"], updated["complete"],
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        next_start = 0 if checkpoint["document"] else updated["sealed"]
        if not checkpoint["document"] and updated["sealed"] == updated["through"]:
            next_start = updated["through"]
        if parsed["fragment"]:
            fragment_key = (
                "fragment", updated["sid"], updated["generation"],
                updated["revision"], observed, next_start,
            )
            self._memo_put(
                fragment_key, parsed["fragment"], len(parsed["fragment"]),
            )
        return updated

    # LLM: 新代只重建派生表，不接管事实写、due 认领、配额或领域 CAS。
    # 函数用途: 构造一个从零推进的来源代次。
    def _fresh(self, sid, domain, parts, document, validation):
        return {
            "sid": sid,
            "domain": domain,
            "parts": _json(list(parts)),
            "validation": validation,
            "generation": secrets.token_hex(16),
            "revision": 0,
            "stamp": (0, 0, 0, 0, 0),
            "through": 0,
            "sealed": 0,
            "complete": 0,
            "document": document,
        }

    # LLM: 首次缺源是无创建的有效空；已存在后删除、坏源、busy 和预算不足都有明确 coverage。
    # 函数用途: 有界推进一个固定领域和相对来源的读投影。
    def advance(
        self, domain, relative_parts, *, validator=None,
        max_rows=25000, max_bytes=33554432, max_ms=1500,
    ):
        with _PROCESS_ADVANCE_LOCK:
            return self._advance(domain, relative_parts, validator=validator,
                                 max_rows=max_rows, max_bytes=max_bytes, max_ms=max_ms)

    # LLM: 进程内写推进由可重入锁串行；进程间仍以SQLite事务和revision比较发布。
    # 函数用途: 执行锁内一次真实来源解析及检查点发布。
    def _advance(
        self, domain, relative_parts, *, validator=None,
        max_rows=25000, max_bytes=33554432, max_ms=1500,
    ):
        budget = _Budget(max_rows, max_bytes, max_ms)
        if domain not in _TABLES:
            raise ValueError("unsupported domain")
        try:
            parts = _parts(relative_parts)
            validation = self._validator_tag(validator)
        except ValueError:
            return self._view(
                domain, (), "", None, "unavailable", "invalid_source",
            )
        sid = self._source_id(domain, parts)
        checkpoint = None
        descriptor = -1
        observed = (0, 0, 0, 0, 0)
        try:
            try:
                descriptor, observed, parents = self._open_source(parts)
            except FileNotFoundError:
                previous = self._existing_checkpoint(
                    sid, domain, parts, budget.deadline,
                )
                if previous is not None or self._marked(sid):
                    return self._view(
                        domain, parts, sid, previous,
                        "unavailable", "source_deleted",
                    )
                return self._view(
                    domain, parts, sid, None, "complete", "source_absent",
                )
            self._mark(sid)
            document = domain == "usage" and parts[-1].lower().endswith(".json")
            with self._database(
                create=True, write=True, deadline=budget.deadline,
            ) as connection:
                checkpoint = self._checkpoint(connection, sid, domain, parts)
                old = checkpoint
                rebuild = checkpoint is None
                if checkpoint is not None:
                    checkpoint = {**checkpoint, "document": document}
                    old_stamp = checkpoint["stamp"]
                    rebuild = (
                        checkpoint["validation"] != validation
                        or self._marked(sid, checkpoint["generation"])
                    )
                    if not rebuild and observed != old_stamp:
                        rebuild = (
                            document
                            or observed[:2] != old_stamp[:2]
                            or observed[2] <= old_stamp[2]
                        )
                        if not rebuild:
                            proved = self._prove_prefix(
                                connection, descriptor, checkpoint,
                                observed, budget,
                            )
                            self._check_source(
                                descriptor, parts, observed, parents,
                            )
                            if proved is None:
                                return self._view(
                                    domain, parts, sid, checkpoint,
                                    "partial", "prefix_verification_budget",
                                    observed[2],
                                )
                            rebuild = not proved
                    if rebuild:
                        self._mark(sid, checkpoint["generation"])
                if rebuild:
                    checkpoint = self._fresh(
                        sid, domain, parts, document, validation,
                    )
                elif checkpoint["complete"] and checkpoint["stamp"] == observed:
                    self._check_source(descriptor, parts, observed, parents)
                    return self._view(
                        domain, parts, sid, checkpoint,
                        "complete", "unchanged", observed[2],
                    )
                parsed = self._parse(
                    descriptor, checkpoint, observed, document, validator, budget,
                )
                self._check_source(descriptor, parts, observed, parents)
                checkpoint = self._publish(
                    connection, old, checkpoint, parsed, observed, validation,
                )
                self._check_source(descriptor, parts, observed, parents)
                if checkpoint["complete"]:
                    status, reason = "complete", "covered"
                elif parsed["error"]:
                    # 已观察坏源时整次新查询不可用；已提交前缀只用于修复后续推进，不当作有效空或当前全貌。
                    status = "unavailable"
                    reason = parsed["error"]
                else:
                    status, reason = "partial", "advance_budget"
                return self._view(
                    domain, parts, sid, checkpoint, status, reason, observed[2],
                )
        except (_Issue, OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as error:
            status, reason = _failure(error)
            return self._view(
                domain, parts, sid, checkpoint, status, reason, observed[2],
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    # LLM: 固定through与检查点、count、page、聚合共享一个读事务；源变化仍须前后复核。
    # 函数用途: 为查询建立来源fence与一致SQLite读快照。
    @contextmanager
    def _reading(self, original):
        view, parts = self._validate_view(original)
        if view["status"] == "unavailable":
            raise _Issue(view["reason"])
        if view["captured_status"] not in {"complete", "partial"}:
            raise _Issue("invalid_view")
        sid = view["source_id"]
        descriptor = -1
        try:
            try:
                descriptor, observed, parents = self._open_source(parts)
            except FileNotFoundError:
                if not view["generation"] and not self._marked(sid):
                    yield view, None, None
                    return
                raise _Issue("source_deleted") from None
            if not view["generation"]:
                raise _Issue("source_appeared")
            self._mark(sid)
            budget = _Budget()
            with self._database(deadline=budget.deadline) as connection:
                connection.execute('BEGIN')
                checkpoint = self._checkpoint(
                    connection, sid, view["domain"], parts,
                )
                if (
                    checkpoint is None
                    or checkpoint["generation"] != view["generation"]
                    or checkpoint["through"] < view["through"]
                    or self._marked(sid, view["generation"])
                ):
                    raise _Issue("stale_generation")
                old_stamp = checkpoint["stamp"]
                if observed != old_stamp:
                    if (
                        observed[:2] != old_stamp[:2]
                        or observed[2] <= old_stamp[2]
                    ):
                        self._mark(sid, checkpoint["generation"])
                        raise _Issue("stale_generation")
                    proved = self._prove_prefix(
                        connection, descriptor, checkpoint, observed, budget,
                    )
                    self._check_source(descriptor, parts, observed, parents)
                    if proved is None:
                        raise _Issue("prefix_verification_budget", "partial")
                    if not proved:
                        self._mark(sid, checkpoint["generation"])
                        raise _Issue("stale_generation")
                yield view, connection, checkpoint
                self._check_source(descriptor, parts, observed, parents)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    # LLM: 无 LF 尾行追加后可产生同一物理行的新版本，旧 through 仍选择当时版本。
    # 函数用途: 生成固定来源代次和字节边界的可见行条件。
    def _visible(self, view):
        table = _TABLES[view["domain"]]
        sql = (
            "r.sid=? AND r.generation=? AND r.stop<=?"
            f" AND NOT EXISTS (SELECT 1 FROM {table} v"
            " WHERE v.sid=r.sid AND v.generation=r.generation"
            " AND v.pos=r.pos AND v.stop>r.stop AND v.stop<=?)"
        )
        args = [
            view["source_id"], view["generation"],
            view["through"], view["through"],
        ]
        return table, sql, args

    # LLM: 字段及运算符由内部白名单决定；JSON 路径和值只通过绑定参数进入 SQL。
    # 函数用途: 编译等值、时间范围、最小耗时及多字段 JSON 过滤。
    def _filters(self, domain, filters):
        if filters is None:
            return "", []
        if not isinstance(filters, dict):
            raise ValueError("filters must be a dictionary")
        clauses, args = [], []
        for key, expected in filters.items():
            if key in ("since", "until", "min_duration_ms"):
                if expected is None:
                    continue
                if key == "min_duration_ms":
                    if domain != "telemetry":
                        raise ValueError("duration filter requires telemetry")
                    field, operator = "duration_ms", ">="
                    value = _number(expected)
                else:
                    field = _TIME_FIELDS[domain]
                    operator = ">=" if key == "since" else "<="
                    if domain in {"telemetry", "audit"}:
                        value = _number(expected)
                    elif isinstance(expected, str):
                        value = expected
                    else:
                        raise ValueError("expected string timestamp")
                clauses.append(f"r.{field}{operator}?")
                args.append(value)
                continue
            if isinstance(key, str) and key in _FIELDS[domain]:
                if expected is None:
                    clauses.append(f"r.{key} IS NULL")
                elif type(expected) in (str, int, float, bool):
                    if type(expected) is float and not math.isfinite(expected):
                        raise ValueError("nonfinite filter")
                    clauses.append(f"r.{key}=?")
                    args.append(expected)
                else:
                    raise ValueError("typed equality requires a scalar")
                continue
            if isinstance(key, tuple):
                segments = key
            elif isinstance(key, str):
                segments = tuple(key.split("."))
            else:
                raise ValueError("unknown filter")
            if (
                not segments or len(segments) > 8
                or segments[0] not in _JSON_ROOTS[domain]
                or any(
                    not isinstance(part, str) or not part or len(part) > 512
                    or "\0" in part for part in segments
                )
            ):
                raise ValueError("unknown filter")
            path = "$" + "".join(
                "." + json.dumps(part, ensure_ascii=False) for part in segments
            )
            if expected is None:
                clauses.append("json_type(r.payload,?)='null'")
                args.append(path)
            elif isinstance(expected, (dict, list)):
                clauses.append("json_extract(r.payload,?)=json(?)")
                args.extend((path, _json(expected)))
            elif type(expected) in (str, int, float, bool):
                if type(expected) is float and not math.isfinite(expected):
                    raise ValueError("nonfinite filter")
                clauses.append("json_extract(r.payload,?)=?")
                args.extend((path, expected))
            else:
                raise ValueError("unsupported JSON equality")
        return (
            "".join(" AND " + clause for clause in clauses),
            args,
        )

    # LLM: 遥测按物理顺序；审计时间相等时始终保留原物理顺序。
    # 函数用途: 选择固定领域的稳定排序。
    def _order(self, domain, descending):
        direction = "DESC" if descending else "ASC"
        if domain == "audit":
            return f"r.timestamp {direction},r.pos ASC"
        if domain == "scheduler":
            return (
                "COALESCE(NULLIF(r.ended_at,''),r.created_at) "
                f"{direction},r.run_id {direction},r.pos {direction}"
            )
        return f"r.pos {direction}"

    # LLM: 每页正文重新解码成独立对象，调用方修改不会污染下一次查询。
    # 函数用途: 从游标读取一个受输出字节上限保护的结果页。
    def _payloads(self, cursor):
        rows = []
        size = 0
        for record in cursor:
            payload = record["payload"]
            if not isinstance(payload, str):
                raise _Issue("corrupt_cached_payload")
            size += len(payload.encode("utf-8"))
            if size > _OWNER_INDEX_MAX_RESULT_BYTES:
                raise _Issue("result_byte_budget", "partial")
            try:
                row = json.loads(payload, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise _Issue("corrupt_cached_payload") from None
            if not isinstance(row, dict):
                raise _Issue("corrupt_cached_payload")
            rows.append(row)
        return rows

    # LLM: 排序键只来自固定SQL白名单；审计同时间始终按物理位置升序，不丢掉并列行。
    # 函数用途: 给分页生成真实排序表达式与各键方向。
    def _seek_keys(self, domain, descending):
        if domain == 'audit':
            return [('r.timestamp', descending), ('r.pos', False)]
        if domain == 'scheduler':
            return [("COALESCE(NULLIF(r.ended_at,''),r.created_at)", descending),
                    ('r.run_id', descending), ('r.pos', descending)]
        return [('r.pos', descending)]

    # LLM: 游标绑定固定view、owner、来源、代次、水位、规范化过滤SQL及排序，而非调用对象地址。
    # 函数用途: 构造不可跨范围复用的分页绑定摘要。
    def _cursor_binding(self, view, extra, args, descending):
        material = [str(self._home), self._owner_id, view['domain'], view['source_id'],
                    view['generation'], view['through'], view['captured_status'], extra, args, descending]
        return hashlib.sha256(_json(material).encode('utf-8')).hexdigest()

    # LLM: 验签后才可使用键；有效签名也必须符合本次字段类型、过滤及排序绑定。
    # 函数用途: 校验seek游标并生成不依赖OFFSET的词典序范围谓词。
    def _seek_clause(self, cursor, binding, keys):
        if cursor is None:
            return '', []
        if not isinstance(cursor, dict):
            raise _Issue('invalid_cursor')
        try:
            value = dict(cursor)
            signature = value.pop('_signature', None)
            expected = self._sign(value)['_signature']
            values = value.get('keys')
            valid = (set(value) == {'cursor_version', 'binding', 'keys'}
                     and value['cursor_version'] == 1 and value['binding'] == binding
                     and isinstance(signature, str) and hmac.compare_digest(signature, expected)
                     and isinstance(values, list) and len(values) == len(keys)
                     and all(type(v) in (str, int, float) for v in values)
                     and all(not isinstance(v, float) or math.isfinite(v) for v in values)
                     and all(not isinstance(v, int) or -2**63 <= v <= _INT64_MAX for v in values))
        except (ValueError, TypeError, OverflowError):
            valid = False
        if not valid:
            raise _Issue('invalid_cursor')
        directions = [descending for _expression, descending in keys]
        if len(set(directions)) == 1:
            expressions = ','.join(expression for expression, _descending in keys)
            placeholders = ','.join('?' for _key in keys)
            operator = '<' if directions[0] else '>'
            return f' AND ({expressions}){operator}({placeholders})', values
        # 审计时间倒序、同时间位置正序：首个边界约束让时间索引可执行范围seek。
        return ' AND r.timestamp<=? AND (r.timestamp<? OR r.pos>?)', [values[0], *values]

    # LLM: 参数错误不触碰事实或隐藏为零；总数按固定view缓存，seek和旧offset分别执行。
    # 函数用途: 查询一个固定 view 的过滤分页。
    def query(
        self, view, *, filters=None, limit=100, offset=0, descending=False,
        after_cursor=None,
    ):
        if (
            type(limit) is not int or not 0 <= limit <= _OWNER_INDEX_MAX_PAGE_ROWS
            or type(offset) is not int or not 0 <= offset <= 10_000_000
            or type(descending) is not bool
        ):
            raise ValueError("invalid pagination")
        if after_cursor is not None and offset:
            raise ValueError('after_cursor cannot be combined with offset')
        checked = None
        try:
            checked, _ = self._validate_view(view)
            extra, filter_args = self._filters(checked["domain"], filters)
            keys = self._seek_keys(checked['domain'], descending)
            binding = self._cursor_binding(checked, extra, filter_args, descending)
            seek, seek_args = self._seek_clause(after_cursor, binding, keys)
            with self._reading(checked) as (current, connection, _checkpoint):
                if connection is None:
                    return {
                        "rows": [], "total_count": 0, 'next_cursor': None,
                        "coverage": self._coverage(current, "complete", "source_absent"),
                    }
                table, condition, args = self._visible(current)
                condition += extra
                args.extend(filter_args)
                total = None
                if current["captured_status"] == "complete":
                    count_key = ('count', binding)
                    total = self._memo_get(count_key)
                    if total is None:
                        total = connection.execute(
                            f"SELECT COUNT(*) FROM {table} r WHERE {condition}", args,
                        ).fetchone()[0]
                        self._memo_put(count_key, total)
                selected = ','.join(f'{expression} AS _key{n}' for n, (expression, _direction) in enumerate(keys))
                cursor = connection.execute(
                    f"SELECT r.payload,{selected} FROM {table} r WHERE {condition}{seek}"
                    " ORDER BY " + self._order(current["domain"], descending)
                    + (' LIMIT ? OFFSET ?' if offset else ' LIMIT ?'),
                    [*args, *seek_args, limit + 1, *([offset] if offset else [])],
                )
                records, result_size = [], 0
                for record in cursor:
                    result_size += len(record['payload'].encode('utf-8'))
                    if result_size > _OWNER_INDEX_MAX_RESULT_BYTES:
                        raise _Issue('result_byte_budget', 'partial')
                    records.append(record)
                rows = self._payloads(records[:limit])
                next_cursor = None
                if limit and len(records) > limit:
                    next_cursor = self._sign({'cursor_version': 1, 'binding': binding,
                        'keys': [records[limit - 1][f'_key{n}'] for n in range(len(keys))]})
                coverage = self._coverage(
                    current, current["captured_status"],
                    "covered" if total is not None else current["reason"],
                )
                result = {
                    "rows": rows, "total_count": total, "coverage": coverage, 'next_cursor': next_cursor,
                }
            return result
        except (_Issue, OSError, sqlite3.Error, OverflowError, TypeError) as error:
            status, reason = _failure(error)
            if checked is None:
                coverage = self._view(
                    "", (), "", None, status, reason,
                )
            else:
                coverage = self._coverage(checked, status, reason)
            return {"rows": [], "total_count": None, "coverage": coverage, 'next_cursor': None}

    # LLM: 输入只保留本批 ID；超过上限显式拒绝，不构造或复制历史全集。
    # 函数用途: 规范化有界批量查找参数。
    def _ids(self, ids):
        if isinstance(ids, (str, bytes)):
            raise ValueError("ids must be an iterable of strings")
        result = []
        seen = set()
        for value in ids:
            if not isinstance(value, str):
                raise ValueError("each id must be a string")
            if len(value) > _OWNER_INDEX_MAX_ROW_BYTES:
                raise ValueError("id too large")
            if value not in seen:
                seen.add(value)
                result.append(value)
            if len(result) > _MAX_BATCH_IDS:
                raise ValueError("too many ids")
        return result

    # LLM: 失败的空集合不代表“不存在”，只有 complete coverage 才能用于负判断。
    # 函数用途: 生成批量 API 的一致失败信封。
    def _batch_failure(self, checked, error, *, lookup=False):
        status, reason = _failure(error)
        if checked is None:
            coverage = self._view("", (), "", None, status, reason)
        else:
            coverage = self._coverage(checked, status, reason)
        if lookup:
            return {"rows": {}, "coverage": coverage}
        return {"ids": frozenset(), "coverage": coverage}

    # LLM: SQL 的 IN 只接收本批 ID；scheduler 不扫描再复制 lifetime Python set。
    # 函数用途: 返回固定 view 中命中的本批身份及覆盖状态。
    def membership(self, view, ids):
        requested = self._ids(ids)
        checked = None
        try:
            checked, _ = self._validate_view(view)
            found = set()
            with self._reading(checked) as (current, connection, _checkpoint):
                if connection is not None:
                    table, condition, args = self._visible(current)
                    identity = _ID_FIELDS[current["domain"]]
                    for start in range(0, len(requested), 200):
                        batch = requested[start:start + 200]
                        placeholders = ",".join("?" for _ in batch)
                        cursor = connection.execute(
                            f"SELECT DISTINCT r.{identity} AS identity"
                            f" FROM {table} r WHERE {condition}"
                            f" AND r.{identity} IN ({placeholders})",
                            [*args, *batch],
                        )
                        found.update(row["identity"] for row in cursor)
                result = {
                    "ids": frozenset(found),
                    "coverage": self._coverage(
                        current, current["captured_status"],
                        "covered" if current["captured_status"] == "complete"
                        else current["reason"],
                    ),
                }
            return result
        except (_Issue, OSError, sqlite3.Error) as error:
            return self._batch_failure(checked, error)

    # LLM: 只查请求身份，重复 call_id 按固定 through 内最后物理记录胜出，历史 query 不去重。
    # 函数用途: 批量读取遥测或其它领域的 last-wins 身份明细。
    def lookup_many(self, view, ids):
        requested = self._ids(ids)
        checked = None
        try:
            checked, _ = self._validate_view(view)
            found = {}
            size = 0
            with self._reading(checked) as (current, connection, _checkpoint):
                if connection is not None:
                    table, condition, args = self._visible(current)
                    identity = _ID_FIELDS[current["domain"]]
                    for start in range(0, len(requested), 200):
                        batch = requested[start:start + 200]
                        placeholders = ",".join("?" for _ in batch)
                        cursor = connection.execute(
                            f"SELECT r.{identity} AS identity,r.payload"
                            f" FROM {table} r WHERE {condition}"
                            f" AND r.{identity} IN ({placeholders})"
                            f" AND r.pos=(SELECT MAX(n.pos) FROM {table} n"
                            " WHERE n.sid=r.sid AND n.generation=r.generation"
                            f" AND n.{identity}=r.{identity} AND n.stop<=?)",
                            [*args, *batch, current["through"]],
                        )
                        for record in cursor:
                            payload = record["payload"]
                            if not isinstance(payload, str):
                                raise _Issue("corrupt_cached_payload")
                            size += len(payload.encode("utf-8"))
                            if size > _OWNER_INDEX_MAX_RESULT_BYTES:
                                raise _Issue("result_byte_budget", "partial")
                            try:
                                row = json.loads(
                                    payload, parse_constant=_reject_constant,
                                )
                            except (ValueError, RecursionError):
                                raise _Issue("corrupt_cached_payload") from None
                            if not isinstance(row, dict):
                                raise _Issue("corrupt_cached_payload")
                            found[record["identity"]] = row
                result = {
                    "rows": found,
                    "coverage": self._coverage(
                        current, current["captured_status"],
                        "covered" if current["captured_status"] == "complete"
                        else current["reason"],
                    ),
                }
            return result
        except (_Issue, OSError, sqlite3.Error) as error:
            return self._batch_failure(checked, error, lookup=True)

    # LLM: 精确 nearest-rank 使用 SQLite 排序样本，不以日桶或近似算法冒充 p50/p95。
    # 函数用途: 在完整固定 view 和白名单过滤下计算精确分位。
    def quantiles(
        self, view, *, field="duration_ms",
        probabilities=(0.5, 0.95), filters=None,
    ):
        probabilities = tuple(probabilities)
        if (
            len(probabilities) > 32
            or any(
                type(value) not in (int, float)
                or not math.isfinite(value) or not 0 <= value <= 1
                for value in probabilities
            )
        ):
            raise ValueError("invalid probabilities")
        checked = None
        try:
            checked, _ = self._validate_view(view)
            allowed = {
                "telemetry": {"duration_ms", "started_at", "ended_at", "output_bytes"},
                "audit": {"timestamp"},
                "scheduler": set(),
                "usage": {"sequence", "chars"},
            }
            if field not in allowed[checked["domain"]]:
                raise ValueError("unknown numeric field")
            extra, filter_args = self._filters(checked["domain"], filters)
            with self._reading(checked) as (current, connection, _checkpoint):
                if current["captured_status"] != "complete":
                    raise _Issue("incomplete_capture", "partial")
                if connection is None:
                    result = {
                        "values": {str(value): None for value in probabilities},
                        "total_count": 0,
                        "coverage": self._coverage(current, "complete", "source_absent"),
                    }
                else:
                    table, condition, args = self._visible(current)
                    condition += extra + f" AND r.{field} IS NOT NULL"
                    args.extend(filter_args)
                    total = connection.execute(
                        f"SELECT COUNT(*) FROM {table} r WHERE {condition}",
                        args,
                    ).fetchone()[0]
                    values = {}
                    for probability in probabilities:
                        if total == 0:
                            values[str(probability)] = None
                            continue
                        rank = max(1, math.ceil(total * probability))
                        value = connection.execute(
                            f"SELECT r.{field} FROM {table} r"
                            f" WHERE {condition}"
                            f" ORDER BY r.{field} ASC,r.pos ASC LIMIT 1 OFFSET ?",
                            [*args, rank - 1],
                        ).fetchone()
                        if value is None:
                            raise _Issue("corrupt_quantile_projection")
                        values[str(probability)] = value[0]
                    result = {
                        "values": values,
                        "total_count": total,
                        "coverage": self._coverage(current, "complete", "covered"),
                    }
            return result
        except (_Issue, OSError, sqlite3.Error) as error:
            status, reason = _failure(error)
            coverage = (
                self._coverage(checked, status, reason)
                if checked is not None
                else self._view("", (), "", None, status, reason)
            )
            return {"values": {}, "total_count": None, "coverage": coverage}


    # LLM: 聚合失败不带业务零计数，保留真实coverage类别。
    # 函数用途: 生成聚合错误信封。
    def _rollup_failure(self, checked, error):
        status, reason = _failure(error)
        coverage = (self._coverage(checked, status, reason) if checked is not None
                    else self._view('', (), '', None, status, reason))
        return {'value': None, 'coverage': coverage}

    # LLM: lifetime样本在SQLite中排序，整数秩为ceil(n/2)与ceil(95*n/100)，不做近似。
    # 函数用途: 计算固定完整遥测视图的工具统计和精确分位。
    def telemetry_rollup(self, view, *, filters=None):
        checked = None
        try:
            checked, _ = self._validate_view(view)
            if checked['domain'] != 'telemetry':
                raise ValueError('telemetry view required')
            extra, filter_args = self._filters('telemetry', filters)
            with self._reading(checked) as (current, connection, _checkpoint):
                if current['captured_status'] != 'complete':
                    raise _Issue(current['reason'], 'partial')
                value = {'schema': 'tool_telemetry_stats.v1', 'total_calls': 0, 'tools': {}}
                if connection is not None:
                    _table, condition, args = self._visible(current)
                    condition += extra
                    args.extend(filter_args)
                    cursor = connection.execute(
                        f"""WITH samples AS (
                            SELECT r.tool_name,r.duration_ms,r.status,
                                   r.error_code,r.started_at,r.pos
                            FROM ori_telemetry r WHERE {condition}
                        ), ranked AS (
                            SELECT *,ROW_NUMBER() OVER (
                                PARTITION BY tool_name ORDER BY duration_ms,pos
                            ) AS rn,COUNT(*) OVER (PARTITION BY tool_name) AS n
                            FROM samples
                        ), stats AS (
                            SELECT tool_name,COUNT(*) AS calls,
                                SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS succeeded,
                                AVG(duration_ms) AS avg_duration_ms,
                                MAX(CASE WHEN rn=(n+1)/2 THEN duration_ms END) AS p50_duration_ms,
                                MAX(CASE WHEN rn=(95*n+99)/100 THEN duration_ms END) AS p95_duration_ms,
                                MAX(duration_ms) AS max_duration_ms,MAX(started_at) AS last_called_at
                            FROM ranked GROUP BY tool_name
                        ), error_counts AS (
                            SELECT tool_name,error_code,COUNT(*) AS amount FROM samples
                            WHERE error_code<>'' GROUP BY tool_name,error_code
                        ), error_maps AS (
                            SELECT tool_name,json_group_object(error_code,amount) AS codes
                            FROM error_counts GROUP BY tool_name
                        )
                        SELECT stats.*,COALESCE(error_maps.codes,'{{}}') AS codes
                        FROM stats LEFT JOIN error_maps USING(tool_name)
                        ORDER BY stats.tool_name""", args,
                    )
                    size = 0
                    for record in cursor:
                        item = dict(record)
                        name = item.pop('tool_name')
                        item['error_codes'] = json.loads(item.pop('codes'))
                        item['failed'] = item['calls'] - item['succeeded']
                        item['success_rate'] = item['succeeded'] / item['calls']
                        size += len(_json([name, item]).encode('utf-8'))
                        if size > _OWNER_INDEX_MAX_RESULT_BYTES:
                            raise _Issue('result_byte_budget', 'partial')
                        value['tools'][name] = item
                        value['total_calls'] += item['calls']
                result = {'value': value, 'coverage': self._coverage(
                    current, 'complete', 'source_absent' if connection is None else 'covered',
                )}
            return result
        except (_Issue, OSError, sqlite3.Error, ValueError, TypeError) as error:
            return self._rollup_failure(checked, error)


    # LLM: 审计聚合只接受固定SQL与绑定参数，不开放路径、ATTACH或外部SQL。
    # 函数用途: 计算固定view的审计摘要或有限最近用户列表。
    def audit_rollup(self, view, *, user_id=None, recent_limit=None):
        if recent_limit is not None and (type(recent_limit) is not int or recent_limit < 0):
            raise ValueError('invalid recent-user limit')
        checked = None
        try:
            checked, _ = self._validate_view(view)
            if checked['domain'] != 'audit':
                raise ValueError('audit view required')
            extra, filter_args = self._filters('audit', {'user_id': user_id} if user_id else None)
            with self._reading(checked) as (current, connection, _checkpoint):
                if current['captured_status'] != 'complete':
                    raise _Issue(current['reason'], 'partial')
                value = [] if recent_limit is not None else {
                    'user_id': user_id, 'total_actions': 0, 'by_action': {},
                    'by_status': {}, 'last_action_time': 0,
                }
                if connection is not None:
                    _table, condition, args = self._visible(current)
                    condition += extra
                    args.extend(filter_args)
                    if recent_limit is not None:
                        cursor = connection.execute(
                            f'''SELECT r.user_id,MAX(r.timestamp) AS last_action_time
                            FROM ori_audit r WHERE {condition} GROUP BY r.user_id
                            ORDER BY last_action_time DESC,MIN(r.pos) ASC LIMIT ?''',
                            [*args, recent_limit],
                        )
                    else:
                        cursor = connection.execute(
                            f'''SELECT 'action' AS bucket,r.action AS name,
                            COUNT(*) AS amount,MAX(r.timestamp) AS last_time
                            FROM ori_audit r WHERE {condition} GROUP BY r.action
                            UNION ALL SELECT 'status',r.status,COUNT(*),MAX(r.timestamp)
                            FROM ori_audit r WHERE {condition} GROUP BY r.status''', [*args, *args],
                        )
                    size = 0
                    for record in cursor:
                        row = dict(record)
                        size += len(_json(row).encode('utf-8'))
                        if size > _OWNER_INDEX_MAX_RESULT_BYTES:
                            raise _Issue('result_byte_budget', 'partial')
                        if recent_limit is not None:
                            value.append(row)
                        else:
                            value['by_' + row['bucket']][row['name']] = row['amount']
                            if row['bucket'] == 'action':
                                value['total_actions'] += row['amount']
                            value['last_action_time'] = max(value['last_action_time'], row['last_time'] or 0)
                result = {'value': value, 'coverage': self._coverage(
                    current, 'complete', 'source_absent' if connection is None else 'covered',
                )}
            return result
        except (_Issue, OSError, sqlite3.Error, ValueError, TypeError) as error:
            return self._rollup_failure(checked, error)


__all__ = ['OwnerReadIndex', 'OwnerReadUnavailable', 'require_complete']
