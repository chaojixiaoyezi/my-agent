# LLM: 此合同只运输原宿主命令与审批，不拥有运行或结果状态；路径始终由本地可信连接和 owner 派生。
# 模块用途: 为同机 Gateway 与 TUI 定义有界消息和命令专属审批地址。

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.opaque_id import validate_opaque_id
from ..user_space.owner_resolver import OwnerIdentity
from .paths import GatewayPaths

COMMAND_STREAM_MEDIA_TYPE = "application/x-ndjson"
COMMAND_STREAM_MAX_BYTES = 4 * 1024 * 1024
_SCHEMA = "host_command_stream.v1"
_KINDS = frozenset({"connected", "heartbeat", "permission_requested", "permission_resolved", "result"})


# LLM: 编号固定原提交；payload 只承载原公开合同，不接收路径或授权替代字段，单帧大小有上限。
# 类用途: 在长命令等待期间传递心跳、审批和最终回执。
@dataclass(frozen=True)
class CommandStreamFrame:
    request_id: str
    kind: str
    payload: dict

    # LLM: 发送前校验同一有限协议，超大结果不能截断后假装完整；已执行结果仍由原查询接口读取。
    # 函数用途: 编码一条完整 JSONL 消息，拒绝无效字段和超出运输限额的正文。
    def encode(self) -> bytes:
        self._validate()
        raw = (json.dumps({"schema": _SCHEMA, **asdict(self)}, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > COMMAND_STREAM_MAX_BYTES:
            raise ValueError("命令消息超过运输上限")
        return raw

    # LLM: 接收只认准确 schema、原编号和字段集；不能从坏帧恢复或猜测成功，正文不成为路径来源。
    # 函数用途: 校验并解码来自当前 HTTP 响应的一行消息。
    @classmethod
    def decode(cls, raw: bytes, *, request_id: str) -> CommandStreamFrame:
        if len(raw) > COMMAND_STREAM_MAX_BYTES or not raw.endswith(b"\n"):
            raise ValueError("命令消息长度或边界无效")
        row = json.loads(raw)
        if not isinstance(row, dict) or set(row) != {"schema", "request_id", "kind", "payload"}:
            raise ValueError("命令消息字段无效")
        if row["schema"] != _SCHEMA or row["request_id"] != request_id:
            raise ValueError("命令消息身份不匹配")
        frame = cls(row["request_id"], row["kind"], row["payload"])
        frame._validate()
        return frame

    # LLM: 校验是纯函数，不规范化无效编号或将未知事件当作业务回执。
    # 函数用途: 检查运输消息的公共字段。
    def _validate(self) -> None:
        validate_opaque_id(self.request_id, kind="host_command_request")
        if not isinstance(self.kind, str) or self.kind not in _KINDS or not isinstance(self.payload, dict):
            raise ValueError("命令消息类型无效")


# LLM: paths 来自原连接，owner 来自认证或本地配置；命令目录隐藏在 processing 下，不冒充普通队列记录。
# 函数用途: 为原文件审批桥提供双方一致的专属父目录，隔离同编号的不同用户命令。
def command_approval_path(paths: GatewayPaths, owner: OwnerIdentity, request_id: str) -> Path:
    validate_opaque_id(request_id, kind="host_command_request")
    owner_hash = hashlib.sha256(json.dumps(asdict(owner), sort_keys=True).encode()).hexdigest()
    return paths.processing / ".commands" / owner_hash / request_id / "approval.chunks.jsonl"


# LLM: 首帧声明认证后实际 owner，只选择可信 Gateway 根内的哈希桶，不提供绝对路径，也不代替执行器授权 binding。
# 函数用途: 读取服务端本次命令的规范用户身份，避免客户端配置与服务端 owner 映射不同而永远等不到审批。
def command_stream_owner(payload: dict) -> OwnerIdentity:
    row = payload.get("owner")
    if set(payload) != {"owner"} or not isinstance(row, dict) or set(row) != {"provider", "owner_kind", "owner_id"}:
        raise ValueError("命令连接身份字段无效")
    if not all(isinstance(value, str) and value for value in row.values()) or row["owner_kind"] not in {"main", "user", "group"}:
        raise ValueError("命令连接身份类型无效")
    return OwnerIdentity(**row)
