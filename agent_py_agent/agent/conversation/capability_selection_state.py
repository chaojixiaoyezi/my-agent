# LLM: 本模块只定义原 TaskLink 的一次能力选择值；不持久化、不授权、不读正文、不把状态或 warning 文案反解析成控制。
# 缺键不是 pending；损坏值只能保留原证据并跳过增强。准确包版本仍归 pins/read receipt，修改须联测 TaskStore CAS 与关闭路径字节。
# 模块用途: 校验选择标记、形成有界结果摘要，并让坏标记不阻断普通任务读取。
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from typing import ClassVar

CAPABILITY_SELECTION_KEY = "host_capability_selection.v1"
CAPABILITY_SELECTION_INVALID = "CAPABILITY_SELECTION_MARKER_INVALID"
SELECTION_RESULT_MAX_BYTES = 1024 * 1024
_CLAIM_FIELDS = ("claim_id", "request_id", "run_id", "attempt_id", "candidate_digest", "model_binding_digest")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WARNING_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


# LLM: 字段均来自宿主或已验结果；model_binding_digest只观察准备时实际模型配置，不能把可变profile菜单当实际身份。
# selected_count/digest 只作选择回执，不能替代授权、pin 或入口提交事实。
# 类用途: 表达 pending→claimed→finished 单向状态；构造即验证，不接受大小写别名或未知状态。
@dataclass(frozen=True)
class TaskCapabilitySelection:
    schema: ClassVar[str] = CAPABILITY_SELECTION_KEY
    status: str
    claim_id: str = ""
    request_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    candidate_digest: str = ""
    model_binding_digest: str = ""
    outcome: str = ""
    selected_count: int = 0
    selection_digest: str = ""
    warning_codes: tuple[str, ...] = ()

    # LLM: 校验只接收严格 typed 字段，警告数量和标签长度有界；标记中不存正文或第二份 refs。
    # 函数用途: 拒绝半写状态、损坏身份与不一致的结果，使合法值可以安全进行完整 CAS 比较。
    def __post_init__(self) -> None:
        if self.status not in {"pending", "claimed", "finished"}:
            raise ValueError(CAPABILITY_SELECTION_INVALID)
        for name in _CLAIM_FIELDS:
            value = getattr(self, name)
            if self.status == "pending":
                if value != "":
                    raise ValueError(CAPABILITY_SELECTION_INVALID)
            elif name in {"candidate_digest", "model_binding_digest"}:
                _require_digest(value)
            else:
                _require_identity(value)
        warnings = _warning_codes(self.warning_codes)
        object.__setattr__(self, "warning_codes", warnings)
        if type(self.selected_count) is not int or self.selected_count < 0:
            raise ValueError(CAPABILITY_SELECTION_INVALID)
        if self.status != "finished":
            if self.outcome != "" or self.selected_count or self.selection_digest != "" or warnings:
                raise ValueError(CAPABILITY_SELECTION_INVALID)
        elif self.outcome == "selected":
            if not self.selected_count:
                raise ValueError(CAPABILITY_SELECTION_INVALID)
            _require_digest(self.selection_digest)
        elif self.outcome == "empty":
            if self.selected_count or self.selection_digest != hashlib.sha256(b"[]").hexdigest():
                raise ValueError(CAPABILITY_SELECTION_INVALID)
        elif self.outcome != "failed" or self.selected_count or self.selection_digest != "":
            raise ValueError(CAPABILITY_SELECTION_INVALID)

    # LLM: 仅宿主资格判断后的真正新建任务可使用；此值自身不证明已启用配置或已有授权包。
    # 函数用途: 创建尚未领取的一次选择标记，不产生任何副作用。
    @classmethod
    def pending(cls) -> TaskCapabilitySelection:
        return cls(status="pending")

    # LLM: 序列化只包含固定 schema 和 bounded 标量；调用方必须经 TaskLink.to_dict 写入原任务键。
    # 函数用途: 返回独立 JSON 值，不能通过修改返回容器影响本标记。
    def to_dict(self) -> dict[str, object]:
        return {"schema": self.schema, **asdict(self), "warning_codes": list(self.warning_codes)}

    # LLM: 只解析当前 schema 的精确字段；未知键/状态是损坏，不能隐式迁移成可重试 pending。
    # 函数用途: 从磁盘 JSON 恢复已验证状态，损坏隔离由外层 decode 负责。
    @classmethod
    def from_dict(cls, value: object) -> TaskCapabilitySelection:
        names = {item.name for item in fields(cls)}
        if (not isinstance(value, dict) or value.get("schema") != CAPABILITY_SELECTION_KEY
                or "status" not in value or set(value) - names - {"schema"}):
            raise ValueError(CAPABILITY_SELECTION_INVALID)
        return cls(**{name: value[name] for name in names if name in value})

    # LLM: 完整 claimed 身份保留，引用只用来计算结果摘要；总字节超限明确失败，不按数量或领域截断。
    # 函数用途: 把一次已领取的选择转成结果值，零 I/O，仍须 TaskStore 的执行权及同 claim CAS 才能保存。
    def finished(self, *, outcome: str, selected_refs=(), warning_codes=()) -> TaskCapabilitySelection:
        if self.status != "claimed":
            raise ValueError(CAPABILITY_SELECTION_INVALID)
        count, digest = _selection_summary(selected_refs)
        if outcome == "failed":
            digest = ""
        return replace(self, status="finished", outcome=outcome, selected_count=count,
                       selection_digest=digest, warning_codes=warning_codes)


# LLM: 此载体只保留同一个 JSON 键的原坏值，repr 不得泄露内容；没有 pending 权限、无第二份存储或自动修复。
# 类用途: 让任务仍可读取和正常更新，同时无损保留损坏标记供人工诊断。
@dataclass(frozen=True)
class InvalidTaskCapabilitySelection:
    raw: object = field(repr=False)


# LLM: 缺键明确返回无状态；仅当前 marker 的格式错误降为 bounded warning，其它任务字段错误不在此吞掉。
# 函数用途: 解析原任务的可选选择键，返回合法状态或原始坏值，零 I/O。
def decode_capability_selection(payload: dict) -> tuple[TaskCapabilitySelection | None, InvalidTaskCapabilitySelection | None]:
    if CAPABILITY_SELECTION_KEY not in payload:
        return None, None
    raw = payload[CAPABILITY_SELECTION_KEY]
    try:
        return TaskCapabilitySelection.from_dict(raw), None
    except (ValueError, TypeError):
        return None, InvalidTaskCapabilitySelection(deepcopy(raw))


# LLM: 执行身份保持字节精确；不修剪、转字符串或猜别名，限制长度并拒绝控制字符。
# 函数用途: 验证宿主 claim 的非空身份字段。
def _require_identity(value: object) -> None:
    if (not isinstance(value, str) or not value or len(value) > 256 or value.strip() != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(CAPABILITY_SELECTION_INVALID)


# LLM: 摘要必须是小写 SHA256，不把不完整或非字符串值当成等价身份。
# 函数用途: 校验候选、实际模型绑定及结果摘要的准确形状。
def _require_digest(value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(CAPABILITY_SELECTION_INVALID)


# LLM: 警告只保留明确代码，不存异常正文；超过16项或80字符显式拒绝，不静默截断。
# 函数用途: 验证并冻结无正文诊断标签。
def _warning_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError(CAPABILITY_SELECTION_INVALID)
    if any(not isinstance(code, str) or not _WARNING_CODE.fullmatch(code) for code in value):
        raise ValueError(CAPABILITY_SELECTION_INVALID)
    if len(set(value)) != len(value):
        raise ValueError(CAPABILITY_SELECTION_INVALID)
    return tuple(value)


# LLM: 复用原 canonical 包引用校验；本函数不查安装、不授予权限、不写 pins。只按总序列化字节防止失控输入，数量开放。
# 函数用途: 计算选中引用集合的稳定摘要与数量；重复、非包或超预算输入明确报错。
def _selection_summary(value: object) -> tuple[int, str]:
    from ..capability.task_references import normalize_skill_reference

    if not isinstance(value, (list, tuple)):
        raise ValueError(CAPABILITY_SELECTION_INVALID)
    normalized: dict[str, dict[str, str]] = {}
    total_bytes = 2
    for row in value:
        fixed = normalize_skill_reference(row)
        if fixed.get("kind") != "capability_package" or fixed != row or fixed["stable_id"] in normalized:
            raise ValueError(CAPABILITY_SELECTION_INVALID)
        encoded = json.dumps(fixed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        total_bytes += len(encoded) + bool(normalized)
        if total_bytes > SELECTION_RESULT_MAX_BYTES:
            raise ValueError("CAPABILITY_SELECTION_RESULT_TOO_LARGE")
        normalized[fixed["stable_id"]] = fixed
    encoded = json.dumps([normalized[key] for key in sorted(normalized)], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return len(normalized), hashlib.sha256(encoded).hexdigest()
