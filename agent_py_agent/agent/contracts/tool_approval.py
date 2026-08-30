# LLM: 本模块定义工具审批等待链的结构化请求与决定；自然语言标题/说明只用于展示，授权只认精确 binding 和 decision 枚举。
# 模块用途: 为工具执行器、Gateway 桥和本地 TUI 提供同一份审批身份、选项及批准绑定。

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ToolApprovalDecisionValue = Literal[
    "approved",
    "denied",
    "cancelled",
    "unavailable",
]
_DECISION_VALUES = frozenset(
    {"approved", "approved_session", "denied", "cancelled", "unavailable"}
)


# LLM: ToolApprovalCallIdentity 只暴露生成审批 binding 所需的 canonical ToolCall 字段；合同层不得反向导入 tooling 实现。
# 类用途: 约定任意工具调用对象要具备哪些稳定身份字段，便于审批合同保持跨层复用。
class ToolApprovalCallIdentity(Protocol):
    call_id: str
    tool_name: str
    run_id: str
    operation_id: str
    idempotency_key: str
    args_hash: str
    turn_id: str


# LLM: ToolApprovalRequest 的 binding 是唯一授权身份；options 可扩展但每个 decision 必须来自封闭安全枚举。
# 类用途: 描述一次暂停中的工具调用、用户可见内容和允许选择。
@dataclass(frozen=True)
class ToolApprovalRequest:
    permission_id: str
    request_id: str
    tool_name: str
    round: int
    call_index: int
    title: str
    description: str
    binding: dict[str, str]
    options: tuple[dict[str, str], ...]
    requested_at: float = field(default_factory=time.time)

    # LLM: 校验必须在跨线程/跨进程发布前完成；不得容忍空 binding 或展示选项注入未知 decision。
    # 函数用途: 规范审批请求并拒绝不完整身份或非法选择。
    def __post_init__(self) -> None:
        permission_id = str(self.permission_id or "").strip()
        request_id = str(self.request_id or "").strip()
        tool_name = str(self.tool_name or "").strip()
        binding = {str(key): str(value or "").strip() for key, value in self.binding.items()}
        required = ("tool_name", "run_id", "operation_id", "idempotency_key", "args_hash")
        if not permission_id or not request_id or not tool_name:
            raise ValueError("tool approval requires permission_id/request_id/tool_name")
        if tool_name != binding.get("tool_name") or any(not binding.get(key) for key in required):
            raise ValueError("tool approval binding is incomplete or mismatched")
        options = tuple(_validated_option(item) for item in self.options)
        if not options or not any(item["decision"] == "approved" for item in options):
            raise ValueError("tool approval requires an approved option")
        if not any(item["decision"] in {"denied", "cancelled"} for item in options):
            raise ValueError("tool approval requires a rejection option")
        object.__setattr__(self, "permission_id", permission_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "round", max(0, int(self.round or 0)))
        object.__setattr__(self, "call_index", max(0, int(self.call_index or 0)))
        object.__setattr__(self, "title", str(self.title or "Tool use"))
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "requested_at", max(0.0, float(self.requested_at or 0.0)))

    # LLM: payload 只包含已校验公开字段与精确 binding；调用方不得补入原始完整 arguments 或 secret。
    # 函数用途: 生成 Gateway/TUI 可安全传递的审批请求对象。
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "tool_approval_request.v1",
            "permission_id": self.permission_id,
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "round": self.round,
            "call_index": self.call_index,
            "title": self.title,
            "description": self.description,
            "binding": dict(self.binding),
            "options": [dict(item) for item in self.options],
            "requested_at": self.requested_at,
        }

    # LLM: 批准记录必须复用请求的完整 binding；UI 返回的文案、option label 或 feedback 都没有授权效力。
    # 函数用途: 为同一工具调用生成 ActionPolicy 能验证的一次性批准记录。
    def approved_binding(self, decision: ToolApprovalDecision) -> dict[str, str]:
        if decision.permission_id != self.permission_id or not decision.approved:
            raise ValueError("approval decision does not approve this request")
        return {
            **self.binding,
            "approval_id": self.permission_id,
            "status": "APPROVED",
        }

    # LLM: 拒绝记录保留原 binding 供审计，但运行时重复匹配只使用 tool_name + args_hash；provider 生成的新 operation/call id 不能绕过用户决定。
    # 函数用途: 为同一 run 生成一次拒绝或取消记录，阻止完全相同的调用反复弹出确认框。
    def rejected_binding(self, decision: ToolApprovalDecision) -> dict[str, str]:
        if decision.permission_id != self.permission_id or decision.decision not in {
            "denied",
            "cancelled",
        }:
            raise ValueError("approval decision does not reject this request")
        return {
            **self.binding,
            "approval_id": self.permission_id,
            "decision": decision.decision,
            "status": "CANCELLED" if decision.decision == "cancelled" else "DENIED",
        }

    # LLM: 反序列化必须重新走 dataclass 校验，不能信任文件或 Gateway row 已合法。
    # 函数用途: 从跨进程 payload 恢复审批请求。
    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ToolApprovalRequest:
        raw_binding = value.get("binding")
        raw_options = value.get("options")
        return cls(
            permission_id=str(value.get("permission_id") or ""),
            request_id=str(value.get("request_id") or ""),
            tool_name=str(value.get("tool_name") or ""),
            round=_nonnegative_int(value.get("round")),
            call_index=_nonnegative_int(value.get("call_index")),
            title=str(value.get("title") or "Tool use"),
            description=str(value.get("description") or ""),
            binding=(
                {str(key): str(item or "") for key, item in raw_binding.items()}
                if isinstance(raw_binding, Mapping)
                else {}
            ),
            options=tuple(
                dict(item) for item in raw_options if isinstance(item, Mapping)
            )
            if isinstance(raw_options, (list, tuple))
            else (),
            requested_at=float(value.get("requested_at") or 0.0),
        )


# LLM: ToolApprovalDecision 的枚举是授权控制量；feedback 只是后续模型上下文，不参与 approve/deny 判断。
# 类用途: 表示用户、控制面或取消令牌对一条审批请求的结构化答复。
@dataclass(frozen=True)
class ToolApprovalDecision:
    permission_id: str
    decision: ToolApprovalDecisionValue
    feedback: str = ""
    decided_at: float = field(default_factory=time.time)

    # LLM: 决定必须精确绑定 permission_id 并落在有限安全枚举；未知字符串 fail-closed。
    # 函数用途: 规范一条审批答复。
    def __post_init__(self) -> None:
        permission_id = str(self.permission_id or "").strip()
        decision = str(self.decision or "").strip().lower()
        if not permission_id:
            raise ValueError("tool approval decision requires permission_id")
        if decision not in _DECISION_VALUES:
            raise ValueError(f"invalid tool approval decision: {decision}")
        object.__setattr__(self, "permission_id", permission_id)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "feedback", str(self.feedback or "").strip())
        object.__setattr__(self, "decided_at", max(0.0, float(self.decided_at or 0.0)))

    # LLM: approved 只认显式 approved 枚举；label、option id 和反馈文本不得产生授权。
    # approved_session 是会话级批准（#5 引入），同样放行——否则选"会话级批准"会走
    # 拒绝分支并抛 ValueError 导致整个请求失败（R2-7 实测）。
    # 函数用途: 判断该决定是否允许当前调用继续执行。
    @property
    def approved(self) -> bool:
        return self.decision in {"approved", "approved_session"}

    # LLM: 输出包含精确 binding id，但不复制工具参数或凭据。
    # 函数用途: 生成跨进程审批答复 payload。
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "tool_approval_decision.v1",
            "permission_id": self.permission_id,
            "decision": self.decision,
            "feedback": self.feedback,
            "decided_at": self.decided_at,
        }

    # LLM: 外部 payload 必须重新验证枚举和 id；缺失值不得默认批准。
    # 函数用途: 从文件、Gateway 或 UI intent 恢复审批答复。
    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ToolApprovalDecision:
        return cls(
            permission_id=str(value.get("permission_id") or ""),
            decision=str(value.get("decision") or "unavailable"),
            feedback=str(value.get("feedback") or ""),
            decided_at=float(value.get("decided_at") or 0.0),
        )


# LLM: permission_id 只从 canonical ToolCall 身份哈希产生；description 是已脱敏展示摘要，不参与 id 或授权绑定。
# 函数用途: 为工具轮中的一条待审批调用生成通用 Yes/No 请求。
def build_tool_approval_request(
    call: ToolApprovalCallIdentity,
    *,
    request_id: str,
    round_number: int,
    call_index: int,
    description: str,
) -> ToolApprovalRequest:
    binding = {
        "tool_name": call.tool_name,
        "run_id": call.run_id,
        "operation_id": call.operation_id,
        "idempotency_key": call.idempotency_key,
        "args_hash": call.args_hash,
    }
    identity = json.dumps(
        {"call_id": call.call_id, **binding},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    permission_id = f"approval:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
    return ToolApprovalRequest(
        permission_id=permission_id,
        request_id=str(request_id or call.turn_id),
        tool_name=call.tool_name,
        round=round_number,
        call_index=call_index,
        title="工具授权",
        description=str(description or call.tool_name),
        binding=binding,
        options=(
            {
                "id": "allow_once",
                "label": "允许一次",
                "decision": "approved",
                "feedback_type": "accept",
                "feedback_placeholder": "可选：告诉 my-agent 接下来怎么做",
            },
            {
                "id": "allow_session",
                "label": "本会话允许同一操作",
                "decision": "approved_session",
                "feedback_type": "accept",
                "feedback_placeholder": "可选：告诉 my-agent 接下来怎么做",
            },
            {
                "id": "deny",
                "label": "拒绝",
                "decision": "denied",
                "feedback_type": "reject",
                "feedback_placeholder": "可选：告诉 my-agent 改用什么方式",
            },
        ),
    )


# LLM: option 校验保持 id/label 开放而 decision 封闭，便于未来增加持久规则文案而不扩大授权枚举。
# 函数用途: 规范一个审批选择项。
def _validated_option(value: Mapping[str, object]) -> dict[str, str]:
    option_id = str(value.get("id") or "").strip()
    label = str(value.get("label") or "").strip()
    decision = str(value.get("decision") or "").strip().lower()
    if not option_id or not label or decision not in _DECISION_VALUES:
        raise ValueError("invalid tool approval option")
    option = {"id": option_id, "label": label, "decision": decision}
    feedback_type = str(value.get("feedback_type") or "").strip().lower()
    if feedback_type:
        if feedback_type not in {"accept", "reject"}:
            raise ValueError("invalid tool approval feedback type")
        option["feedback_type"] = feedback_type
        option["feedback_placeholder"] = str(
            value.get("feedback_placeholder") or ""
        ).strip()
    return option


# LLM: 数字转换只影响 UI round/index，不参与 binding 或工具执行顺序。
# 函数用途: 安全读取非负整数。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ToolApprovalCallIdentity",
    "ToolApprovalDecision",
    "ToolApprovalDecisionValue",
    "ToolApprovalRequest",
    "build_tool_approval_request",
]
