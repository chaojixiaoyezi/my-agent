# LLM: remember 工具——把【值得长期复用】的信息写进 owner 长期记忆(agent.memory),未来会话自动召回。
#   对标 长期助手 tools/memory_tool.py:
#   ① 用户明确提供或本轮成功工具证实的事实可即时写入；模型自己推测的内容只能进入 owner
#      ops.jsonl 候选账本，不能直接污染可召回长期记忆。
#   ② 写 owner memory(跨会话持久),不写 task-scoped;琐碎/一次性细节别记成噪音(会稀释召回);
#   ③ 写入前 scan_memory_content 注入扫描兜底。改动时同步 tests/test_memory_tool.py。
# 模块用途: 用结构化来源和证据边界写长期记忆；不从自然语言猜来源。
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    current_conversation_task_attributes,
)
from ..memory_store.jsonl import MemorySubjectConflict
from ..memory_store.operations import (
    normalized_memory_content,
    record_memory_candidate,
)
from ..memory_store.security import scan_memory_content
from ..tooling.models import BaseTool, ToolAvailability, ToolExecutionResult, ToolSpec
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: schema 是模型可见的来源/证据合同；变更时同步 execute 校验和 native/text 工具测试。
# 函数用途: 定义 remember 的说明、参数结构和安全使用边界。
def build_remember_spec() -> ToolSpec:
    return ToolSpec(
        name="remember",
        category="capability",
        effect="mutating",
        idempotency_scope="operation",
        description=(
            "把【需要时才想起的具体事实/事件/任务知识】记进长期记忆,未来会话按相关性召回。"
            "例:'下周三交报告'、'项目叫 moneywise'、'某接口的坑'、下次同类任务能复用的做法。"
            "同一个工具支持 list/replace/remove/batch；修改和删除必须使用 list 返回的稳定 entry_id，"
            "不能靠相似文本猜测。batch 整批校验，任一项失败时全部不写。"
            "**注意:用户的长期人设/画像/称呼/性格/沟通偏好(如'以后叫我小王''你说话活泼点')不要用这个——"
            "那些用 update_persona 写进人格文件(每轮注入),写进 memory 不会每轮生效。**"
        ),
        use_cases=[
            "得知一个未来同类任务能直接复用的做法、或踩过的坑,主动沉淀",
            "用户提到具体的事实/日程/项目名等'需要时才想起'的信息",
            "用户明确要求'记住X'且 X 是事实/事件(不是称呼/性格/偏好那类人设)",
        ],
        avoid_when=[
            "用户长期人设/画像/称呼/性格/沟通偏好 → 用 update_persona 写人格文件(不是这个)",
            "一次性任务的临时细节(写进任务产物/工作区,不进长期记忆)",
            "不确定是否长期有用的琐碎信息——别记成噪音(会稀释召回)",
            "凭证/口令/密钥等敏感信息(本就不该长期留存)",
        ],
        keywords=[
            "记住",
            "remember",
            "记一下",
            "记下",
            "别忘了",
            "事实",
            "日程",
            "项目名",
            "踩坑",
            "复用做法",
        ],
        parameters={
            "action": "add(默认)/list/replace/remove/batch。",
            "entry_id": "replace/remove 必填；来自 list，不接受近似文本。",
            "content": "add/replace 必填。要长期记住的一句话，具体、自包含。",
            "kind": "结构化类型：fact/event/project/lesson/note。不要从正文关键词硬判。",
            "tags": "可选。标签列表(如 ['preference','format']),便于未来检索。",
            "expected_version": "可选。list 返回的版本；用于并发修改冲突检测。",
            "expires_at": "可选。Unix 时间戳；到期后不再召回。",
            "origin": (
                "add/replace 必填。user_explicit=用户明确提供；tool_verified=来自本轮成功工具结果；"
                "model_inferred=模型推测，仅进入候选账本，不直接进入长期记忆。"
            ),
            "evidence_refs": "tool_verified 必填；只能引用本轮成功工具记录返回的结构化 ref。",
            "subject_key": "可选的稳定主题键；同主题出现不同内容时必须 list 后 replace，不能自动覆盖。",
            "operations": "batch 必填。add/replace/remove 操作数组，整批原子提交。",
        },
        parameter_schema={
            "action": {"type": "string", "enum": ["add", "list", "replace", "remove", "batch"]},
            "entry_id": {"type": "string"},
            "content": {"type": "string"},
            "kind": {"type": "string", "enum": ["fact", "event", "project", "lesson", "note"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "expected_version": {"type": "integer", "minimum": 1},
            "expires_at": {"type": "number", "minimum": 0},
            "origin": {
                "type": "string",
                "enum": ["user_explicit", "tool_verified", "model_inferred"],
            },
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "subject_key": {"type": "string"},
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "entry_id": {"type": "string"},
                        "content": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["fact", "event", "project", "lesson", "note"],
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "expected_version": {"type": "integer", "minimum": 1},
                        "expires_at": {"type": "number", "minimum": 0},
                        "origin": {
                            "type": "string",
                            "enum": ["user_explicit", "tool_verified", "model_inferred"],
                        },
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "subject_key": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        },
        required_parameters=[],
        examples=[
            '{"tool":"remember","content":"moneywise 项目使用 UTC 保存时间","kind":"project",'
            '"tags":["moneywise","time"],"origin":"user_explicit","subject_key":"project:moneywise:timezone"}',
            '{"tool":"remember","action":"list"}',
        ],
    )


class RememberTool(BaseTool):
    # 类用途: 把"按用户指令写长期记忆"暴露成模型工具,落到 owner memory(跨会话持久)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_remember_spec()

    def availability(self) -> ToolAvailability:
        attributes = current_conversation_task_attributes(self.agent)
        if attributes.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True:
            return ToolAvailability.unavailable(
                "named Audit preparation is task-scoped and cannot mutate owner memory"
            )
        return ToolAvailability.ready()

    # LLM: 模型推测只写候选；active mutation 必须经统一 schema、来源、证据和 JsonlMemory 权威链。
    # 函数用途: 执行长期记忆的列出、候选、新增、修改、删除或原子批处理。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        action = str(params.get("action") or "add").strip().lower()
        if action not in {"add", "list", "replace", "remove", "batch"}:
            return _memory_error(
                "action 必须是 add/list/replace/remove/batch", "TOOL_INVALID_ARGUMENTS"
            )
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "add"):
            return _memory_error("长期记忆不可用", "TOOL_UNAVAILABLE")
        if action == "list":
            return _memory_list_result(memory)
        operations = _memory_operations(params, action, self.agent)
        validation = _validate_memory_operations(operations, self.agent)
        if validation is not None:
            return validation
        inferred = next(
            (
                operation
                for operation in operations
                if str(operation.get("origin") or "") == "model_inferred"
            ),
            None,
        )
        if inferred is not None:
            return _candidate_memory_result(memory, inferred)
        return _active_memory_result(memory, operations, action)


# LLM: 候选路径只能写 ops.jsonl 并明确 active_memory_changed=false，不得回落到 memory.add。
# 函数用途: 保存模型推测候选并返回不会被召回的结果。
def _candidate_memory_result(
    memory: object,
    operation: dict[str, object],
) -> ToolExecutionResult:
    ops_path = getattr(memory, "ops_path", None)
    if ops_path is None:
        return _memory_error(
            "当前记忆候选账本不可用，拒绝把模型推测直接写入长期记忆。",
            "MEMORY_CANDIDATE_STORE_UNAVAILABLE",
        )
    candidate = record_memory_candidate(
        ops_path,
        role=operation.get("role"),
        kind=operation.get("kind"),
        content=operation.get("content"),
        tags=_normalize_tags(operation.get("tags")),
        source=operation.get("source"),
        subject_key=operation.get("subject_key"),
        evidence_refs=_normalize_refs(operation.get("evidence_refs")),
    )
    return ToolExecutionResult(
        "remember",
        True,
        json.dumps(
            {
                "ok": True,
                "action": "candidate",
                "candidate_id": candidate["candidate_id"],
                "active_memory_changed": False,
                "hint": "这是模型推测，已进入候选账本；未写入可召回的长期记忆。",
            },
            ensure_ascii=False,
        ),
    )


# LLM: active mutation 的所有后端异常必须映射稳定错误码；不能逃逸成未分类异常或自动重试副作用。
# 函数用途: 提交已经通过来源/证据校验的长期记忆操作，并生成统一结果。
def _active_memory_result(
    memory: object,
    operations: list[dict[str, object]],
    action: str,
) -> ToolExecutionResult:
    try:
        if hasattr(memory, "apply_batch"):
            committed = memory.apply_batch(operations)
        elif action == "add":
            operation = operations[0]
            committed = [
                memory.add(
                    "user",
                    str(operation["content"]),
                    kind=str(operation.get("kind") or "fact"),
                    tags=_normalize_tags(operation.get("tags")),
                )
            ]
        else:
            return _memory_error("当前记忆后端不支持修改操作", "TOOL_UNAVAILABLE")
    except MemorySubjectConflict as exc:
        return _memory_error(
            str(exc),
            "MEMORY_SUBJECT_CONFLICT",
            hint=f"先 action=list，再 replace entry_id={exc.entry_id}；不要新增第二条冲突事实。",
        )
    except KeyError as exc:
        return _memory_error(
            f"entry_id 不存在或已被修改: {exc.args[0] if exc.args else ''}",
            "MEMORY_ENTRY_NOT_FOUND",
            hint="先 action=list 取得当前 entry_id 和 version。",
        )
    except OwnerQuotaExceeded as exc:
        return _memory_error(
            str(exc),
            "OWNER_DISK_QUOTA_EXCEEDED",
            hint="清理当前 owner 文件或联系管理员调整配额。",
        )
    except OwnerQuotaUnavailable:
        return _memory_error(
            "owner 配额策略当前不可用",
            "OWNER_QUOTA_UNAVAILABLE",
            hint="配额策略恢复前拒绝写入。",
        )
    except RuntimeError as exc:
        return _memory_error(str(exc), "MEMORY_VERSION_CONFLICT", hint="重新 list 后再提交修改。")
    except ValueError as exc:
        return _memory_error(str(exc), "TOOL_INVALID_ARGUMENTS")
    except Exception as exc:  # noqa: BLE001
        return _memory_error(
            f"写入失败: {exc}",
            "TOOL_EXECUTION_FAILED",
            hint="长期记忆写入异常，可原样重试一次。",
        )
    deduplicated = not committed and action == "add"
    if deduplicated:
        committed = _matching_active_records(memory, operations[0])
    payload = {
        "ok": True,
        "action": action,
        "entries": [_memory_record_payload(record) for record in committed],
        "deduplicated": deduplicated,
        "hint": (
            "完全相同的活跃记忆已存在；未重复写入。"
            if deduplicated
            else "变更已写入 owner 长期记忆；无需重复调用。"
        ),
    }
    return ToolExecutionResult("remember", True, json.dumps(payload, ensure_ascii=False))


_MEMORY_KINDS = frozenset({"fact", "event", "project", "lesson", "note"})
_MEMORY_ORIGINS = frozenset({"user_explicit", "tool_verified", "model_inferred"})
_EVIDENCE_REF_KEYS = frozenset(
    {
        "call_id",
        "scoped_call_id",
        "artifact_ref",
        "artifact_id",
        "output_path",
        "effect_source_ref",
        "operation_id",
        "source_ref",
        "path",
        "ref",
        "uri",
        "url",
    }
)
_EVIDENCE_CONTAINERS = frozenset(
    {
        "tool_result_refs",
        "artifact_registry_refs",
        "tool_result_envelope",
        "runtime_gate",
        "refs",
        "artifacts",
        "result_refs",
    }
)


# LLM: 参数只从 ToolSpec 字段投影；origin/evidence/subject 进入 attributes，不能从 content 猜。
# 函数用途: 把单操作或 batch 参数统一成底层记忆操作数组。
def _memory_operations(
    params: dict[str, object],
    action: str,
    agent: object,
) -> list[dict[str, object]]:
    source = _memory_source(agent)
    if action == "batch":
        raw = params.get("operations")
        if not isinstance(raw, list):
            return []
        operations = [dict(item) for item in raw if isinstance(item, dict)]
    else:
        operations = [
            {
                key: value
                for key, value in params.items()
                if key
                in {
                    "entry_id",
                    "content",
                    "kind",
                    "tags",
                    "expected_version",
                    "expires_at",
                    "origin",
                    "evidence_refs",
                    "subject_key",
                }
            }
        ]
        operations[0]["action"] = action
    for operation in operations:
        operation["source"] = source
        operation["role"] = "user"
        origin = str(operation.get("origin") or "").strip()
        evidence_refs = _normalize_refs(operation.get("evidence_refs"))
        subject_key = str(operation.get("subject_key") or "").strip()
        operation["origin"] = origin
        operation["evidence_refs"] = evidence_refs
        operation["subject_key"] = subject_key
        operation["attributes"] = {
            key: value
            for key, value in {
                "origin": origin,
                "evidence_refs": evidence_refs,
                "subject_key": subject_key,
            }.items()
            if value not in ("", [])
        }
        if str(operation.get("action") or "").strip().lower() == "add":
            operation.setdefault("kind", "fact")
    return operations


def _memory_source(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    request_id = str(getattr(current, "request_id", "") or "").strip()
    if request_id:
        return f"agent_tool:{request_id}"
    return "agent_tool"


# LLM: 校验只认结构化 origin 和成功归档 refs；失败工具或正文提到的 ref 都不能获得证据权威。
# 函数用途: 在任何长期写入前检查动作、来源、证据、内容安全和留存规则。
def _validate_memory_operations(
    operations: list[dict[str, object]],
    agent: object,
) -> ToolExecutionResult | None:
    if not operations:
        return _memory_error("operations 必须是非空数组", "TOOL_INVALID_ARGUMENTS")
    for index, operation in enumerate(operations, start=1):
        action = str(operation.get("action") or "").strip().lower()
        if action not in {"add", "replace", "remove"}:
            return _memory_error(
                f"第 {index} 个操作的 action 必须是 add/replace/remove",
                "TOOL_INVALID_ARGUMENTS",
            )
        if action in {"replace", "remove"} and not str(operation.get("entry_id") or "").strip():
            return _memory_error(
                f"第 {index} 个操作缺少 entry_id",
                "TOOL_INVALID_ARGUMENTS",
                hint="先 action=list 取得稳定 entry_id。",
            )
        if action == "remove":
            continue
        origin = str(operation.get("origin") or "").strip()
        if origin not in _MEMORY_ORIGINS:
            return _memory_error(
                f"第 {index} 个操作缺少有效 origin",
                "TOOL_INVALID_ARGUMENTS",
                hint="使用 user_explicit/tool_verified/model_inferred；不能让系统猜来源。",
            )
        if origin == "model_inferred":
            if action != "add" or len(operations) != 1:
                return _memory_error(
                    "model_inferred 只能单独 add 为候选，不能和活跃记忆修改混在 batch 中。",
                    "TOOL_INVALID_ARGUMENTS",
                )
        if origin == "tool_verified":
            evidence_refs = _normalize_refs(operation.get("evidence_refs"))
            if not evidence_refs:
                return _memory_error(
                    f"第 {index} 个 tool_verified 操作缺少 evidence_refs",
                    "TOOL_INVALID_ARGUMENTS",
                )
            trusted_refs = _successful_tool_evidence_refs(agent)
            unknown = [ref for ref in evidence_refs if ref not in trusted_refs]
            if unknown:
                return _memory_error(
                    f"第 {index} 个操作引用了未验证的工具证据: {unknown}",
                    "MEMORY_EVIDENCE_NOT_VERIFIED",
                    hint="只能使用本轮已成功工具记录中的结构化 ref。",
                )
        content = str(operation.get("content") or "").strip()
        if not content:
            return _memory_error(f"第 {index} 个操作缺少 content", "TOOL_INVALID_ARGUMENTS")
        kind = str(operation.get("kind") or "fact").strip()
        if kind not in _MEMORY_KINDS:
            return _memory_error(
                f"第 {index} 个操作的 kind 必须是 fact/event/project/lesson/note",
                "TOOL_INVALID_ARGUMENTS",
            )
        operation["kind"] = kind
        operation["tags"] = _normalize_tags(operation.get("tags"))
        scan = scan_memory_content(content)
        if not scan.safe:
            return _memory_error(
                scan.reason(),
                "MEMORY_INJECTION_BLOCKED",
                hint="改写成不含可执行指令或凭证语义的纯描述；外部内容不要原样落库。",
            )
        retention = classify_memory_retention(content, operation["tags"])
        if not retention.durable:
            return ToolExecutionResult(
                "remember",
                False,
                json.dumps(
                    {
                        "error": "这条内容含临时验证码、解锁码或一次性凭据，不能写进长期记忆。",
                        "operation_index": index,
                        "retention": retention.to_dict(),
                        "hint": "只在当前请求中使用；需要留痕时必须脱敏。",
                    },
                    ensure_ascii=False,
                ),
                error_code="MEMORY_TRANSIENT_DATA_BLOCKED",
            )
        expires_at = operation.get("expires_at")
        if expires_at not in (None, ""):
            try:
                operation["expires_at"] = float(expires_at)
            except (TypeError, ValueError):
                return _memory_error(
                    f"第 {index} 个操作的 expires_at 必须是 Unix 时间戳",
                    "TOOL_INVALID_ARGUMENTS",
                )
    return None


# LLM: 证据集合只来自当前 run 的 ok=true archive 白名单字段，禁止遍历 parameters/output 正文。
# 函数用途: 收集本轮成功工具真正产生的可引用编号和产物引用。
def _successful_tool_evidence_refs(agent: object) -> set[str]:
    # ToolLoopExecuteParams 是运行中工具事实的唯一权威；RunParams 不承载
    # archive_tool_calls，读取它会让所有真实工具证据都被误判为不存在。
    current = getattr(agent, "_current_tool_loop_params", None)
    archive = getattr(current, "archive_tool_calls", None)
    if not isinstance(archive, list):
        return set()
    refs: set[str] = set()
    for record in archive:
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        _collect_evidence_refs(record, refs, include_containers=True)
    return refs


# LLM: 递归仅进入显式结构化容器，不能把任意 tool envelope 字符串当 ref。
# 函数用途: 从一条成功工具归档的白名单字段中提取引用。
def _collect_evidence_refs(
    payload: dict[str, object],
    refs: set[str],
    *,
    include_containers: bool,
) -> None:
    for key in _EVIDENCE_REF_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                refs.add(text)
    if not include_containers:
        return
    for key in _EVIDENCE_CONTAINERS:
        value = payload.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                _collect_evidence_refs(item, refs, include_containers=True)


# LLM: 此查询只用于幂等 add 返回已有稳定 ID，不改变权威数据或扩大召回。
# 函数用途: 完全重复写入时找到已经存在的那条活跃记忆供模型确认。
def _matching_active_records(memory: object, operation: dict[str, object]) -> list[object]:
    if not hasattr(memory, "all"):
        return []
    target = (
        str(operation.get("role") or "user").strip().casefold(),
        str(operation.get("kind") or "fact").strip().casefold(),
        normalized_memory_content(operation.get("content")),
    )
    try:
        records = list(memory.all())
    except Exception:
        return []
    return [
        record
        for record in records
        if (
            str(getattr(record, "role", "") or "").strip().casefold(),
            str(getattr(record, "kind", "") or "").strip().casefold(),
            normalized_memory_content(getattr(record, "content", "")),
        )
        == target
    ][:1]


def _memory_list_result(memory: object) -> ToolExecutionResult:
    if not hasattr(memory, "all"):
        return _memory_error("当前记忆后端不支持列出条目", "TOOL_UNAVAILABLE")
    try:
        entries = [_memory_record_payload(record) for record in memory.all()]
    except Exception as exc:  # noqa: BLE001
        return _memory_error(f"读取失败: {exc}", "TOOL_EXECUTION_FAILED")
    return ToolExecutionResult(
        "remember",
        True,
        json.dumps({"ok": True, "action": "list", "entries": entries}, ensure_ascii=False),
    )


def _memory_record_payload(record: object) -> dict[str, object]:
    return {
        "entry_id": str(getattr(record, "entry_id", "") or ""),
        "version": int(getattr(record, "version", 1) or 1),
        "content": str(getattr(record, "content", "") or ""),
        "kind": str(getattr(record, "kind", "fact") or "fact"),
        "tags": list(getattr(record, "tags", None) or []),
        "created_at": float(getattr(record, "created_at", 0.0) or 0.0),
        "updated_at": float(getattr(record, "updated_at", 0.0) or 0.0),
        "expires_at": float(getattr(record, "expires_at", 0.0) or 0.0),
    }


def _memory_error(message: str, code: str, *, hint: str = "") -> ToolExecutionResult:
    payload = {"error": message}
    if hint:
        payload["hint"] = hint
    return ToolExecutionResult(
        "remember",
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code=code,
    )


def _normalize_tags(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


# LLM: refs 只做类型、空白和顺序去重，不解析路径或自然语言含义。
# 函数用途: 规范化模型提交的结构化证据引用数组。
def _normalize_refs(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


_TRANSIENT_CODE_PATTERNS = (
    re.compile(
        r"(?:验证码|校验码|动态码|一次性(?:密码|口令|代码)|临时(?:密码|口令|码)|"
        r"解锁码|卡片密码|登录密码|访问口令|OTP|one[- ]time (?:password|code)|unlock code)"
        r"\s*(?:是|为|[:：=])?\s*[A-Za-z0-9][A-Za-z0-9._-]{3,63}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:otp|pin|passcode|verification[_ -]?code)\s*[:=]\s*[A-Za-z0-9._-]{4,64}\b",
        re.IGNORECASE,
    ),
)
_TRANSIENT_TAGS = frozenset(
    {"otp", "password", "passcode", "verification-code", "temporary-code", "unlock-code", "secret"}
)


@dataclass(frozen=True)
class MemoryRetentionDecision:
    """长期留存裁决；只用结构化 code 执行，reason 仅供诊断。"""

    durable: bool
    code: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"durable": self.durable, "code": self.code, "reason": self.reason}


def classify_memory_retention(
    content: str, tags: list[str] | None = None
) -> MemoryRetentionDecision:
    """拒绝一次性凭据进入 owner 长期记忆，避免临时聊天数据污染未来会话。"""
    normalized_tags = {str(item).strip().lower() for item in (tags or []) if str(item).strip()}
    if normalized_tags & _TRANSIENT_TAGS:
        return MemoryRetentionDecision(False, "transient_credential_tag", "标签表明内容是临时凭据")
    if any(pattern.search(str(content or "")) for pattern in _TRANSIENT_CODE_PATTERNS):
        return MemoryRetentionDecision(
            False, "transient_credential_pattern", "内容包含临时凭据标签和值"
        )
    return MemoryRetentionDecision(True, "durable_candidate", "未命中临时凭据规则")


__all__ = [
    "MemoryRetentionDecision",
    "RememberTool",
    "build_remember_spec",
    "classify_memory_retention",
]
