# LLM: 原索引副作用保持；native诊断共用有限schema，追加原资源缺失枚举但不回显路径/正文，不作权限/重试/终态事实。
# 模块用途: 镜像Gateway记录并输出安全失败诊断，诊断失败绝不覆盖原执行异常。

from __future__ import annotations

"""mirrors gateway lifecycle and request payloads into LocalStore with non-fatal errors.

文件队列是 gateway 的事实源，LocalStore 是方便搜索和排查的索引。
这个文件负责把请求、响应、生命周期事件写进索引；索引失败会报告，但不会弄坏主请求。
side-effect 错误统一输出 runtime_error_report JSON；native诊断另用固定安全字段，不展开异常正文。
"""

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..local_storage.records import DERIVED_RECORD_TTL_SECONDS, derived_terminal_expired
from ..runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class _GatewayPayloadRenderContext:

    payload: dict
    request_id: str
    kind: str
    status: str
    request_path: Path | None
    response_path: Path | None


@dataclass(frozen=True)
class GatewayIndexPayloadOptions:
    request_path: Path | None = None
    response_path: Path | None = None
    event_type: str = "gateway_request_rebuilt"


@dataclass(frozen=True)
class GatewayPayloadLogParams:
    event_type: str
    request_path: Path | None = None
    response_path: Path | None = None


def log_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    *,
    params: GatewayPayloadLogParams | None = None,
    event_type: str,
    request_path: Path | None = None,
    response_path: Path | None = None,
) -> None:
    log_params = params or GatewayPayloadLogParams(event_type, request_path, response_path)
    request_id = str(payload.get("id") or "")
    if not request_id:
        return
    try:
        status = str(payload.get("status") or "queued")
        kind = str(payload.get("kind") or "unknown")
        agent.local_store.log_record(
            source_type="gateway_request",
            source_id=request_id,
            title=f"Gateway {kind} {status} {request_id}",
            content=_gateway_payload_content(
                _GatewayPayloadRenderContext(
                    payload, request_id, kind, status, log_params.request_path, log_params.response_path
                )
            ),
            metadata=_gateway_payload_metadata(
                _GatewayPayloadRenderContext(
                    payload, request_id, kind, status, log_params.request_path, log_params.response_path
                )
            ),
            event_type=log_params.event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_payload", request_id, exc)


def _gateway_payload_content(context: _GatewayPayloadRenderContext) -> str:
    payload = context.payload
    return "\n".join(
        [
            "# Gateway Request",
            f"id: {context.request_id}",
            f"kind: {context.kind}",
            f"status: {context.status}",
            f"ok: {payload.get('ok', '')}",
            f"backend: {payload.get('backend', '')}",
            f"tool_rounds: {payload.get('tool_rounds', '')}",
            f"prompt: {payload.get('prompt', '')}",
            f"response: {payload.get('response', '')}",
            f"error_code: {payload.get('error_code', '')}",
            f"error: {payload.get('error', '')}",
            f"request_file: {context.request_path or payload.get('request_file', '')}",
            f"response_file: {context.response_path or ''}",
            "",
            "## Payload",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )


# LLM: Gateway metadata is a searchable projection, not a second task authority. Preserve only
# the host-authored request/thread/task/path tuple so session_search can point back to canonical files.
# 函数用途: 生成 Gateway 历史索引元数据，并保留后续续作所需的精确任务目录引用。
def _gateway_payload_metadata(context: _GatewayPayloadRenderContext) -> dict:
    payload = context.payload
    metadata = {
        "request_id": context.request_id,
        "kind": context.kind,
        "status": context.status,
        "ok": bool(payload.get("ok", False)),
        "backend": str(payload.get("backend", "")),
        "tool_rounds": int(payload.get("tool_rounds", 0) or 0),
        "error_code": str(payload.get("error_code", "")),
        "created_at": float(payload.get("created_at", 0) or 0),
        "started_at": float(payload.get("started_at", 0) or 0),
        "ended_at": float(payload.get("ended_at", 0) or 0),
        "request_path": str(context.request_path or payload.get("request_file", "")),
        "response_path": str(context.response_path or ""),
    }
    conversation_runtime = _gateway_conversation_runtime(
        payload,
        request_id=context.request_id,
    )
    if conversation_runtime:
        metadata["conversation_runtime"] = conversation_runtime
    return metadata


# LLM: Do not infer task identity from prompt/response text or legacy filenames. The Gateway
# request is the only source allowed to populate this index projection.
# 函数用途: 从 Gateway 请求载荷中筛出可供历史检索返回的结构化会话任务字段。
def _gateway_conversation_runtime(
    payload: dict,
    *,
    request_id: str,
) -> dict[str, str]:
    runtime = payload.get("conversation_runtime")
    if not isinstance(runtime, dict):
        return {}
    result = {
        key: str(runtime.get(key) or "").strip()
        for key in ("thread_id", "task_id", "task_path")
        if str(runtime.get(key) or "").strip()
    }
    if request_id:
        result["request_id"] = request_id
    return result


# LLM: 事件镜像默认关闭，显式agent属性优先于config；关闭时不触Store，payload状态/终态事件映射不变。
# 函数用途: 仅按原开关保存网关事件及TTL时间，不改变独立请求日志入口。
def log_gateway_event(agent: SimpleAgent, event_type: str, payload: dict) -> None:

    try:
        if not getattr(agent, "store_event_mirror_enabled",
                       getattr(getattr(agent, "config", None), "store_event_mirror_enabled", False)):
            return
        created_at = time.time()
        source_id = f"{event_type}:{created_at:.6f}"
        agent.local_store.log_record(
            source_type="gateway_event",
            source_id=source_id,
            title=f"Gateway event {event_type}",
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            metadata={
                "event_type": event_type,
                "status": _gateway_event_status(event_type, payload),
                "pid": int(payload.get("pid", 0) or 0),
                "created_at": created_at,
                "recorded_at": created_at,
            },
            event_type=event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_event", str(payload.get("id", event_type)), exc)


# LLM: 精确事件名映射而非子串匹配，ok或无错误不是终态证明，冲突时保留payload原状态。
# 函数用途: 给已确认的完成/失败事件补齐状态，保留其它事件未知状态。
def _gateway_event_status(event_type, payload):
    known = {"gateway_request_completed": "completed", "gateway_request_failed": "failed",
             "gateway_request_cancelled": "cancelled", "gateway_stopped": "stopped"}
    return str(payload.get("status") or known.get(event_type, ""))


# LLM: 重建只更新派生索引；老于30天的可信终态不回填，不碰canonical归档或正常实时日志。
# 函数用途: 从请求或响应归档补索引，保留原时间窗而非把历史刷成今天。
def _index_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    options: GatewayIndexPayloadOptions | None = None,
) -> bool:

    options = options or GatewayIndexPayloadOptions()
    if _gateway_rebuild_expired(payload, options):
        return False
    request_path = options.request_path
    response_path = options.response_path
    event_type = options.event_type
    request_id = str(payload.get("id") or (request_path.stem if request_path else "")).strip()
    if not request_id:
        return False
    log_gateway_payload(
        agent,
        {
            **payload,
            "id": request_id,
            "kind": str(payload.get("kind") or "ask"),
            "status": str(payload.get("status") or "rebuilt"),
            "ok": bool(payload.get("ok", False)),
        },
        event_type=event_type,
        request_path=request_path,
        response_path=response_path,
    )
    return True


# LLM: 仅重建/归档入口检查TTL；时间未知或状态未结束继续保留，mtime不能覆盖有效载荷时间。
# 函数用途: 阻止30天外的请求全文在重建时重新进入LocalStore。
def _gateway_rebuild_expired(payload, options):
    if options.event_type != "gateway_request_rebuilt" and not (options.request_path or options.response_path):
        return False
    mtimes = []
    for path in (options.request_path, options.response_path):
        if path is None:
            continue
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return derived_terminal_expired(payload, time.time() - DERIVED_RECORD_TTL_SECONDS, max(mtimes, default=None))


def _report_gateway_side_effect_error(operation: str, request_id: str, exc: Exception) -> None:
    try:
        report = runtime_error_report(exc, context=f"gateway.side_effect.{operation}")
        report["operation"] = operation
        report["request_id"] = request_id
        print(
            "[gateway-side-effect-error] "
            + json.dumps(report, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
    except Exception:
        pass


# LLM: 与原reader共用同一已审literal集合；读取原生args描述符，不调用异常str/repr/属性hook，值不参与授权或执行。
# 函数用途: 对exact WorkerCommitRejected筛选固定原因；未知内容一律省略，绝不输出任意args。
def _native_rejection_reason(error):
    from .native_diagnostic_projection import NATIVE_REJECTION_REASONS
    args = BaseException.args.__get__(error)
    return args[0] if type(args) is tuple and len(args) == 1 and type(args[0]) is str and args[0] in NATIVE_REJECTION_REASONS else None


# LLM: 只认准确已审异常类型；原生实例字典和固定集合禁止未知类/子类借error_code或资源路径泄漏正文。
# 函数用途: 生成不含message/args/owner/path的固定字段；资源缺失只接受原owner目录有限枚举，未知字段省略。
def _native_error_fields(error):
    from .multiprocess_bootstrap_resources import WorkerSharedResourceError
    from .multiprocess_kernel_message import KernelMessageError
    from .multiprocess_worker_commit import WorkerCommitRejected

    name = type.__getattribute__(type(error), '__name__')
    fields = {'error_type': name if type(name) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,95}', name) else None,
              'error_code': None, 'reason': None, 'field': None}
    if type(error) is WorkerCommitRejected:
        fields.update(error_code='WORKER_COMMIT_REJECTED', reason=_native_rejection_reason(error))
    elif type(error) is KernelMessageError:
        values = BaseException.__dict__['__dict__'].__get__(error)
        code = values.get('error_code')
        if type(code) is str and code in {'KERNEL_MESSAGE_FORKED', 'KERNEL_MESSAGE_COPIED', 'KERNEL_MESSAGE_IO', 'KERNEL_MESSAGE_MALFORMED', 'KERNEL_MESSAGE_INVALID', 'KERNEL_MESSAGE_TOO_LARGE', 'KERNEL_MESSAGE_CLOSED', 'KERNEL_MESSAGE_TIMEOUT', 'KERNEL_MESSAGE_UNAVAILABLE', 'KERNEL_MESSAGE_UNSUPPORTED_LOCATOR'}:
            fields['error_code'] = code
    elif type(error) is WorkerSharedResourceError:
        fields['error_code'] = 'WORKER_SHARED_RESOURCE_INVALID'
        values = BaseException.__dict__['__dict__'].__get__(error)
        reason, field = values.get('reason'), values.get('field')
        from .native_diagnostic_projection import native_resource_pair_is_public
        if native_resource_pair_is_public(reason, field):
            fields.update(reason=reason, field=field)
    return fields


# LLM: 仅本次已验证定位字符串的SHA/长度，绝不写原ID或从hash恢复执行来源；编码错误由logger边界省整条诊断。
# 函数用途: 把原request/attempt缩成安全的相关性投影，不读取文件或构造权限对象。
def _native_identifier_fingerprint(value):
    if type(value) is not str or not value or len(value) > 512:
        return None
    raw = value.encode('utf-8')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'chars': len(value), 'bytes': len(raw)}


# LLM: 诊断仅固定stderr输出；不调用runtime_error_report/to_payload/异常正文，不写账、不授信；任何诊断失败都不得覆盖原异常。
# 函数用途: 为原native阶段与原Future异常输出有界安全字段，调用方仍bare raise原对象，不加ACK/重试/终态。
def report_native_transport_diagnostic(stage, error, *, op=None, seq=None, request_id=None, execution_attempt_id=None):
    try:
        if stage not in {'original_source', 'receive', 'authenticate', 'decode', 'identity', 'dispatch', 'send', 'worker_future_result'}:
            return
        fields = _native_error_fields(error)
        if stage == 'receive' and fields['error_type'] == 'KernelMessageError' and fields['error_code'] == 'KERNEL_MESSAGE_TIMEOUT':
            from .multiprocess_kernel_message import KernelMessageError

            if type(error) is KernelMessageError:
                return
        linked = stage in {'dispatch', 'send', 'worker_future_result'}
        payload = {'schema_version': 'gateway_native_diagnostic.v1', 'stage': stage,
                   'op': op if linked and type(op) is str and op in {'begin', 'apply', 'finish', 'materialize'} else None,
                   'seq': seq if linked and type(seq) is int and 0 <= seq < 2**63 else None,
                   'request': _native_identifier_fingerprint(request_id) if linked else None,
                   'attempt': _native_identifier_fingerprint(execution_attempt_id) if linked else None,
                   **fields}
        sys.stderr.write('[gateway-native-diagnostic] ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
        sys.stderr.flush()
    except BaseException:
        pass
