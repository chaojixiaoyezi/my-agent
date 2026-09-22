# LLM: 宿主请求只绑定原 RuntimeDB 身份；这里不做授权、不调用模型/工具，也不持有独立状态或租约。
# 模块用途: 原子登记显式命令的真实运行，并严格读取同一请求的原始绑定。

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass

from ..common.id_generator import new_id
from ..common.strict_json import load_strict_json
from .operations import RuntimeConflictError
from .run_creation import RunCreation, create_run_chain

_SCHEMA = "host_command_request.v1"
_EVENT = "host_command.registered"


# LLM: 身份只接受宿主鉴权后的字段；查询不需要来源文件或命令正文，也不授予原运行新的执行权。
# 类用途: 绑定同一操作者与会话中的请求，供提交去重与只读状态查询共用。
@dataclass(frozen=True)
class HostCommandIdentity:
    owner_id: str
    actor_id: str
    channel: str
    thread_id: str
    request_id: str

    # LLM: 标识只做传输校验，不规范化身份；非法 UTF-8、空值及超长输入不能进入持久请求账。
    # 函数用途: 检查宿主提供的身份与摘要，失败不写数据库。
    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
                raise ValueError(f"宿主命令字段无效: {name}")
            if len(value.encode("utf-8")) > 1024:
                raise ValueError(f"宿主命令字段过长: {name}")

    # LLM: 不含命令与参数，保证同一消息改变输入仍命中原绑定并拒绝，而不是生成新操作。
    # 函数用途: 生成已鉴权请求范围的稳定摘要，供原事件唯一索引使用。
    @property
    def operation_id(self) -> str:
        scope = [_SCHEMA, self.owner_id, self.actor_id, self.channel, self.thread_id, self.request_id]
        digest = hashlib.sha256(json.dumps(scope, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return f"host-command:{digest}"


# LLM: 提交在可信范围上附加命令和完整规范输入摘要；与纯查询共用身份，不改变原 v1 持久字段或 operation ID。
# 类用途: 固定一次显式管理命令的输入，重送改变参数时拒绝。
@dataclass(frozen=True)
class HostCommandRequest(HostCommandIdentity):
    command_name: str
    input_digest: str

    # LLM: 基类检查全部字符串字段；摘要必须规范，不能用展示正文代替参数事实。
    # 函数用途: 在登记前拒绝不完整或格式错误的输入摘要。
    def __post_init__(self) -> None:
        super().__post_init__()
        if re.fullmatch(r"[0-9a-f]{64}", self.input_digest) is None:
            raise ValueError("宿主命令输入摘要无效")

    # LLM: 仅保存结构化身份和输入摘要，不复制完整命令正文、来源文件或凭据。
    # 函数用途: 生成 TaskRun 中唯一的冻结请求记录。
    def to_payload(self) -> dict:
        return {"schema_version": _SCHEMA, **asdict(self)}


# LLM: 绑定不是执行许可；后续只能精确激活原 pending，或只读原 operation，不能从 current 补新身份。
# 类用途: 返回原任务和首次尝试的身份，标明是否为本次首次登记。
@dataclass(frozen=True)
class HostCommandBinding:
    request: HostCommandRequest
    task_id: str
    task_run_id: str
    agent_run_id: str
    run_id: str
    attempt_id: str
    created: bool = False


# LLM: 调用方已持有 BEGIN IMMEDIATE；链与唯一索引必须同事务，失败不留半棵树或可重跑占位。
# 函数用途: 为首次管理请求登记独立 pending 运行，不创建聊天投影、唤醒或后台进程。
def insert_host_command(
    conn: sqlite3.Connection, request: HostCommandRequest, *, now: float,
) -> HostCommandBinding:
    run_id = new_id("run_id")
    chain = create_run_chain(
        conn,
        RunCreation(
            owner_id=request.owner_id, thread_id=request.thread_id, run_id=run_id,
            role="host_command", attempt_status="pending", attempt_metadata={"lifecycle": "pending"},
            task_run_metadata={"host_command": request.to_payload()},
        ),
        now=now,
    )
    conn.execute(
        "INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id, "
        "task_run_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, '{}', ?)",
        (request.operation_id, _EVENT, chain["attempt_id"], chain["agent_run_id"], chain["task_run_id"], now),
    )
    return HostCommandBinding(
        request=request, run_id=run_id, created=True,
        **{key: value for key, value in chain.items() if key != "delegation_id"},
    )


# LLM: 须核对原链和冻结请求；现存索引的断链不能视为首次请求。去重依赖原事件表只追加、不删除。
# 函数用途: 只读获取同一宿主请求的原始运行，输入变化或缺失事实时拒绝。
def read_host_command(
    conn: sqlite3.Connection, request: HostCommandIdentity,
) -> HostCommandBinding | None:
    row = conn.execute(
        "SELECT e.event_type, e.attempt_id, e.agent_run_id, e.task_run_id, "
        "tr.task_id, tr.metadata_json, t.owner_id, t.thread_id, ar.run_id, ar.role, "
        "ar.parent_agent_run_id, ar.delegation_id, at.attempt_generation "
        "FROM runtime_events e "
        "LEFT JOIN task_runs tr ON tr.task_run_id = e.task_run_id "
        "LEFT JOIN tasks t ON t.task_id = tr.task_id "
        "LEFT JOIN agent_runs ar ON ar.agent_run_id = e.agent_run_id AND ar.task_run_id = tr.task_run_id "
        "LEFT JOIN agent_attempts at ON at.attempt_id = e.attempt_id AND at.agent_run_id = ar.agent_run_id "
        "WHERE e.event_id = ?",
        (request.operation_id,),
    ).fetchone()
    if row is None:
        return None
    expected = {
        "event_type": _EVENT, "owner_id": request.owner_id, "thread_id": request.thread_id,
        "role": "host_command", "parent_agent_run_id": "", "delegation_id": "", "attempt_generation": 1,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise RuntimeConflictError("宿主命令的原始运行链不完整或身份不符")
    try:
        metadata = load_strict_json(row["metadata_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeConflictError("宿主命令的冻结请求记录无效") from exc
    frozen = _read_frozen_request(metadata, request)
    keys = ("task_id", "task_run_id", "agent_run_id", "run_id", "attempt_id")
    if any(not isinstance(row[key], str) or not row[key] for key in keys):
        raise RuntimeConflictError("宿主命令缺少原始执行身份")
    return HostCommandBinding(request=frozen, **{key: row[key] for key in keys})


# LLM: 状态查询只省略命令参数，不省略完整可信身份；提交仍核对全部冻结输入，不从客户端补丢失记录。
# 函数用途: 解码原请求并检查当前读取者范围，返回唯一持久请求。
def _read_frozen_request(metadata: object, requested: HostCommandIdentity) -> HostCommandRequest:
    try:
        raw = metadata["host_command"]
        if raw["schema_version"] != _SCHEMA:
            raise ValueError("未知请求版本")
        frozen = HostCommandRequest(**{key: value for key, value in raw.items() if key != "schema_version"})
        if any(getattr(frozen, key) != value for key, value in asdict(requested).items()):
            raise ValueError("输入或身份冲突")
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        raise RuntimeConflictError("宿主命令标识已绑定不同输入或请求记录损坏") from exc
    return frozen


# LLM: operation_id 只接受宿主已保存引用；反查仍核对可信 owner、原请求摘要及完整首次运行链，不跟随 current attempt。
# 函数用途: 让已授权管理操作找回原资源创建者，缺失和坏记录均不能被猜成另一个运行。
def read_host_command_by_operation(
    conn: sqlite3.Connection, *, owner_id: str, operation_id: str,
) -> HostCommandBinding | None:
    row = conn.execute(
        "SELECT tr.metadata_json FROM runtime_events e "
        "LEFT JOIN task_runs tr ON tr.task_run_id = e.task_run_id WHERE e.event_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        metadata = load_strict_json(row["metadata_json"])
        raw = metadata["host_command"]
        if raw["schema_version"] != _SCHEMA:
            raise ValueError("未知请求版本")
        frozen = HostCommandRequest(**{key: value for key, value in raw.items() if key != "schema_version"})
        if frozen.owner_id != owner_id or frozen.operation_id != operation_id:
            raise ValueError("宿主命令归属不符")
    except (TypeError, KeyError, ValueError, RecursionError) as exc:
        raise RuntimeConflictError("宿主命令的原操作引用不可读") from exc
    return read_host_command(conn, frozen)
