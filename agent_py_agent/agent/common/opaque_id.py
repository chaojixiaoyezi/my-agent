from __future__ import annotations

"""规范 opaque ID 类型（R0 #90，3.txt B 节 1-2 条）。

给人看的解释：
run_id、task_id、task_run_id、agent_run_id、attempt_id、session_id、
delegation_id 都是框架生成的 opaque identifier：有长度上限、只含安全字符、
拒绝绝对路径/../单点/双点段/斜杠/反斜杠/控制字符。任何 ID 在拼成文件路径
之前必须经过 validate_opaque_id 拒绝式校验——校验不过直接抛错，绝不静默
改写（净化器会掩盖攻击意图，拒绝式校验把坏输入挡在门外）。
"""

import re
from typing import Any

OPAQUE_ID_MAX_LENGTH = 128

# 只允许字母/数字/连字符/下划线打头与组成：一个正则同时排除斜杠、反斜杠、
# 点段（. / ..）、控制字符、空白与所有路径分隔特征。
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# 7 种规范 ID 类型。task_run_id / agent_run_id / delegation_id 由 R1 权威
# 实体使用；run_id/task_id/attempt_id/session_id 已接线。
ID_KINDS = (
    "run_id",
    "task_id",
    "task_run_id",
    "agent_run_id",
    "attempt_id",
    "session_id",
    "delegation_id",
)

_FRAMEWORK_PREFIXES = ("subagent", "attempt", "session", "thread", "dispatch", "task", "run", "agent")

# 框架生成器产出形如 `subagent-1780127110-469bfd0e`；模型侧可能传 `{run_id}`
# 占位符或从提示词抄来的自由文本。校验器只认框架形态，避免把任意用户输入
# 当 ID 拼进路径。
_KNOWN_SHAPES = re.compile(
    r"^(?:subagent|attempt|session|thread|dispatch|task|run|agent|session)-"
    r"[0-9]{9,11}-[0-9a-f]{8}$"
)


class OpaqueIdError(ValueError):
    """ID 不合法：超长、含路径特征或非法字符。拒绝式校验的统一错误。"""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"invalid {kind}: {detail}")
        self.kind = kind
        self.detail = detail


def validate_opaque_id(value: Any, *, kind: str) -> str:
    """拒绝式校验一个框架 ID；合法返回原值，非法抛 OpaqueIdError。

    校验规则（3.txt B.2）：
    - 必须是非空字符串；
    - 长度 ≤ OPAQUE_ID_MAX_LENGTH；
    - 只含 [A-Za-z0-9_-]（隐含排除 / \\ . .. 空格与控制字符）；
    - 不是绝对路径形态。
    不校验"是否框架生成"——存量数据与测试夹具里存在历史格式，只需保证
    它不可能逃逸出目录边界。
    """
    if not isinstance(value, str) or not value:
        raise OpaqueIdError(kind, "must be a non-empty string")
    if len(value) > OPAQUE_ID_MAX_LENGTH:
        raise OpaqueIdError(kind, f"exceeds {OPAQUE_ID_MAX_LENGTH} chars")
    if not _OPAQUE_ID_RE.fullmatch(value):
        raise OpaqueIdError(
            kind,
            "must be an opaque identifier (letters/digits/underscore/hyphen only; "
            "no slashes, backslashes, dot segments, whitespace or control characters)",
        )
    if _looks_like_absolute_path(value):
        raise OpaqueIdError(kind, "must not look like an absolute path")
    return value


def is_opaque_id(value: Any, *, kind: str) -> bool:
    try:
        validate_opaque_id(value, kind=kind)
        return True
    except OpaqueIdError:
        return False


def validate_path_segment(value: Any, *, kind: str) -> str:
    """校验一个将作为路径段使用的 ID（与 validate_opaque_id 同规则，语义别名）。

    调用方约定：任何"ID 直接拼进文件路径"的位置都必须先过这里，让代码阅读
    者一眼看到这是受控路径段而不是任意字符串。
    """
    return validate_opaque_id(value, kind=kind)


def _looks_like_absolute_path(value: str) -> bool:
    return value.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", value))


__all__ = [
    "ID_KINDS",
    "OPAQUE_ID_MAX_LENGTH",
    "OpaqueIdError",
    "is_opaque_id",
    "validate_opaque_id",
    "validate_path_segment",
]
