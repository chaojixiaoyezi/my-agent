# LLM: 固定通道数据合同、回执校验及惰性解析；导入不执行领域 I/O，不登记第二份维护状态。
# 模块用途: 为后续存储通道提供共用预算、写门和只读发现接口。
from __future__ import annotations

import heapq
import importlib
import json
import math
import os
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .home_layout import MyAgentHomePaths

T = TypeVar("T")

# 单个存储通道游标序列化后的最大字节数，超限视为非法回执。
STORAGE_LANE_CURSOR_MAX_BYTES = 16384
# 每个存储通道单拍最多检查的候选条数。
STORAGE_LANE_SLICE_ENTRIES = 128
# 每个存储通道单拍最多读取的字节数。
STORAGE_LANE_SLICE_BYTES = 8 * 1024 * 1024
# 每个存储通道单拍墙钟预算毫秒数。
STORAGE_LANE_SLICE_MS = 25
# 单个通道回执最多携带的错误条数。
STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT = 16
# 所有者留存覆盖项允许的最大天数。
OWNER_RETENTION_OVERRIDE_MAX_DAYS = 3650

# 通道名称与领域入口的冻结映射；解析时才导入领域模块。
LANE_BINDINGS: dict[str, str] = {
    "compact_checkpoints": "conversation.compact_reachability:maintain_compact_reachability_lane",
    "conversation_messages": "conversation.transcript_index_gc:maintain_transcript_index_lane",
    "display_archives": "conversation.display_archive_maintenance:maintain_display_archives_lane",
    "conversation_ledgers": "conversation.store_ledger_retention:maintain_conversation_ledgers_lane",
    "process_sessions": "tooling.process_session_retention:maintain_process_sessions_lane",
    "local_events": "local_storage.maintenance:maintain_local_events_lane",
    "subagents": "subagents.services.persistence.locator_hygiene:maintain_subagents_lane",
    "session_task_payloads": "conversation.session_task_retention:maintain_session_task_payloads_lane",
    "tool_outputs": "memory_archive.tool_output_retention:maintain_owner_tool_outputs_lane",
    "asset_auxiliary": "assets.revision_history:maintain_asset_auxiliary_lane",
}
# 压力只调整既有合格候选的通道次序，不缩短保留期。
LANE_EVICTION_ORDER = (
    "display_archives", "conversation_messages", "compact_checkpoints", "local_events",
    "conversation_ledgers", "tool_outputs", "subagents", "asset_auxiliary",
    "session_task_payloads", "process_sessions",
)
# 回执必须提供的计量和续点字段。
_RECEIPT_REQUIRED = ("cursor", "complete", "items", "bytes")
# 可选的非负整数计量字段，不将检查量冒充物理释放量。
_RECEIPT_OPTIONAL_INT = ("deleted_items", "freed_bytes", "backlog")


# LLM: 预算不可变；单调时钟仅描述本片剩余额度，不改变领域策略。
# 类用途: 保存通道的条目、字节与截止时间预算。
@dataclass(frozen=True)
class StorageLaneBudget:
    max_items: int | None
    max_bytes: int | None
    deadline: float | None

    # LLM: 无截止时间返回无穷；到期归零，不产生等待或写入。
    # 函数用途: 返回本片剩余的毫秒数。
    def remaining_ms(self) -> float:
        if self.deadline is None:
            return math.inf
        return max(0.0, (self.deadline - time.monotonic()) * 1000)

    # LLM: 任一累计计量到达上限即耗尽，未设置的上限不参与判定。
    # 函数用途: 判断条目、字节或时间预算是否耗尽。
    def exhausted(self, items: int = 0, size: int = 0) -> bool:
        return (
            (self.max_items is not None and items >= self.max_items)
            or (self.max_bytes is not None and size >= self.max_bytes)
            or self.remaining_ms() <= 0
        )

    # LLM: 按冻结合同取调度预算与领域上限的较小值，返回新对象。
    # 函数用途: 为本次领域调用派生封顶预算。
    def capped(self, *, items: int, size: int, ms: float) -> StorageLaneBudget:
        return StorageLaneBudget(
            min(self.max_items or items, items),
            min(self.max_bytes or size, size),
            min(self.deadline or math.inf, time.monotonic() + ms / 1000),
        )


# LLM: 字段次序固定；调度器提供游标副本，通道不能自行发布维护状态。
# 类用途: 把所有者上下文、预算与协调器写门传给领域通道。
@dataclass(frozen=True)
class StorageLaneRequest:
    kind: str
    home: MyAgentHomePaths
    now: float
    cursor: dict
    budget: StorageLaneBudget
    legal_hold: bool
    coordinator: bool
    pressure: dict | None
    commit: Callable[[Callable[[], T]], T] | None

    # LLM: 仅由调用方有真实变更时调用；协调器写门的返回值和异常原样透传。
    # 函数用途: 在既有写门内执行一个变更批次。
    def run_write(self, operation: Callable[[], T]) -> T:
        return operation() if self.commit is None else self.commit(operation)


# LLM: 严格区分布尔与整数，避免把开关当作释放计数。
# 函数用途: 校验非负整数计量字段。
def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


# LLM: 校验失败统一拒绝，调用方不得发布未经验证的游标；成功只返回浅副本。
# 函数用途: 检查领域回执的字段、类型、游标大小及错误条数。
def validate_lane_receipt(receipt: object) -> dict:
    invalid = "invalid_storage_lane_receipt"
    optional = {*_RECEIPT_OPTIONAL_INT, "protected", "skipped", "errors", "oldest_due_age_seconds", "deferred_reason"}
    if not isinstance(receipt, dict):
        raise ValueError(invalid)
    if not set(_RECEIPT_REQUIRED) <= receipt.keys() or receipt.keys() - set(_RECEIPT_REQUIRED) - optional:
        raise ValueError(invalid)
    if not isinstance(receipt["cursor"], dict) or type(receipt["complete"]) is not bool:
        raise ValueError(invalid)
    try:
        encoded = json.dumps(receipt["cursor"], separators=(",", ":"), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(invalid) from exc
    if len(encoded) > STORAGE_LANE_CURSOR_MAX_BYTES:
        raise ValueError(invalid)
    if any(not _nonnegative_int(receipt[key]) for key in ("items", "bytes", *_RECEIPT_OPTIONAL_INT) if key in receipt):
        raise ValueError(invalid)
    if "protected" in receipt:
        protected = receipt["protected"]
        if not _nonnegative_int(protected) and (
            not isinstance(protected, dict)
            or any(not isinstance(key, str) or not _nonnegative_int(value) for key, value in protected.items())
        ):
            raise ValueError(invalid)
    if "skipped" in receipt:
        skipped = receipt["skipped"]
        if not isinstance(skipped, dict) or any(
            not isinstance(key, str) or not _nonnegative_int(value) for key, value in skipped.items()
        ):
            raise ValueError(invalid)
    if "errors" in receipt:
        errors = receipt["errors"]
        if not isinstance(errors, list) or len(errors) > STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
            raise ValueError(invalid)
        for error in errors:
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ValueError(invalid)
            if "path" in error:
                path = error["path"]
                if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
                    raise ValueError(invalid)
    if "oldest_due_age_seconds" in receipt:
        age = receipt["oldest_due_age_seconds"]
        if type(age) not in (int, float) or not 0 <= age < math.inf:
            raise ValueError(invalid)
    if "deferred_reason" in receipt and not isinstance(receipt["deferred_reason"], str):
        raise ValueError(invalid)
    return dict(receipt)


# LLM: 仅入口或其包父级缺失才回退；领域模块自身依赖失败必须交给调度器记录。
# 函数用途: 惰性解析冻结映射中的领域通道入口。
def resolve_storage_lane(kind: str) -> Callable[[StorageLaneRequest], dict] | None:
    target = LANE_BINDINGS.get(kind)
    if target is None:
        return None
    module, attribute = target.split(":")
    module_name = "agent_py_agent.agent." + module
    try:
        loaded = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == "agent_py_agent" or exc.name.startswith("agent_py_agent.")):
            if module_name == exc.name or module_name.startswith(exc.name + "."):
                return None
        raise
    return getattr(loaded, attribute, None)


# LLM: 逐级 lstat 拒绝链接；仅缺失或非目录按无候选处理，其余读取异常上抛。
# 函数用途: 检查所有者根下的目录链，不创建路径。
def _real_directory(root: Path, parts: tuple[str, ...] = ()) -> bool:
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return False
        current = root
        for part in parts:
            current = current / part
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


# LLM: 只发现当前所有者的规范会话根；每级拒绝链接，不因发现路径而授予删除权。
# 函数用途: 返回直接布局及运行时布局中的真实会话目录。
def owner_conversation_roots(home) -> tuple[Path, ...]:
    owner = Path(home.owner_home_dir)
    workspace = Path(home.owner_workspace_dir)
    roots = []
    if _real_directory(owner, ("conversations", "threads")):
        roots.append(owner / "conversations")
    try:
        workspace_parts = workspace.relative_to(owner).parts
    except ValueError:
        return tuple(roots)
    parts = (*workspace_parts, "runtime", "workspaces")
    if not _real_directory(owner, parts):
        return tuple(roots)
    runtime = owner.joinpath(*parts)
    try:
        with os.scandir(runtime) as entries:
            scopes = sorted(entry.name for entry in entries)
    except (FileNotFoundError, NotADirectoryError):
        return tuple(roots)
    for scope in scopes:
        if _real_directory(owner, (*parts, scope, "conversations")):
            roots.append(runtime / scope / "conversations")
    return tuple(roots)


# LLM: 复用原发现器并惰性导入以避免维护模块循环导入；不新建数据库。
# 函数用途: 返回所有者本地存储候选及原发现错误。
def owner_local_store_databases(home) -> tuple[list[Path], list[dict]]:
    from .owner_maintenance import _local_store_database_paths

    return _local_store_database_paths(Path(home.owner_home_dir), include_event_files=True)


# LLM: 覆盖项只接受约定范围的真正整数，坏文件与非法值均采用原默认值，不回写策略。
# 函数用途: 读取一个可选的留存天数覆盖项。
def owner_retention_override(home, key: str, default: int) -> int:
    from ..common.json_io import read_json_object_report

    report = read_json_object_report(Path(home.owner_retention_json), context="storage_lanes.override")
    value = report.payload.get(key)
    if report.load_error is None and type(value) is int and 1 <= value <= OWNER_RETENTION_OVERRIDE_MAX_DAYS:
        return value
    return default


# LLM: 只对名称流做一遍有界选择，额外内存随 limit 而非目录大小增长。
# 函数用途: 返回续点之后字典序最小的一批名称。
def lane_cursor_window(cursor: dict, names: Iterable[str], limit: int) -> list[str]:
    return heapq.nsmallest(limit, (name for name in names if name > cursor.get("after", "")))
