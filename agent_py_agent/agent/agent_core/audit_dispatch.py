"""特权工具动作审计接线(审计 #13):shell/写删/外发 URL 等特权动作落审计,审计接口不再零调用。

原状:AuditLogger 写齐了脱敏白名单/UUID/双层锁,但生产执行路径零调用——特权工具动作全程无审计。
本模块在工具分发 seam(execute_traced_tool_call)对特权工具落一条审计:谁(agent owner)对什么
(target)做了什么(工具)、成败。actor 取 config owner 身份;接 gateway 端"哪个用户触发"是更细的
请求级身份穿透(seam 处够不到),留专项。target_id 记命令/路径/URL——属审计价值(脱敏白名单刻意
不脱它们,见审计 #13 bfdb19e4),超长截断。

铁律:审计失败只吞不冒泡,绝不影响工具执行本身(热路径稳定性第一)。
"""

from __future__ import annotations

import threading

# 特权工具 → 取作审计 target 的候选参数键(按序取第一个存在的)
_PRIVILEGED_TARGETS = {
    "run_command": ("command",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "apply_patch": ("path", "patch"),
    "web_fetch": ("url", "urls"),
}

_LOGGERS: dict[str, object] = {}
_LOCK = threading.Lock()


def audit_privileged_tool_call(agent: object, payload: dict, result: object) -> None:
    """特权工具调用后落一条审计。非特权工具 / 缺 config / 异常一律静默跳过,绝不影响工具执行。"""
    try:
        tool = str(getattr(result, "tool", "") or payload.get("tool") or "")
        config = getattr(agent, "config", None)
        if tool not in _PRIVILEGED_TARGETS or config is None:
            return
        _write_tool_audit(config, tool, payload, result)
    except Exception:
        pass  # 审计绝不影响工具执行(审计 #13)


def _write_tool_audit(config: object, tool: str, payload: dict, result: object) -> None:
    logger = _logger_for(config)
    if logger is None:
        return
    from ..audit.records import AuditStatus, LogParams

    ok = bool(getattr(result, "ok", False))
    from ..tooling.registry_envelopes import tool_input_facts

    params = payload.get("params")
    safe_input = params if isinstance(params, dict) else {
        key: value for key, value in payload.items() if key not in {"tool", "kind"}
    }
    details: dict[str, object] = {
        "ok": ok,
        "input_facts": tool_input_facts(safe_input),
    }
    if not ok:
        details.update(
            {
                "error_code": str(getattr(result, "error_code", "") or "UNKNOWN_ERROR"),
                "reported_error_code": str(
                    getattr(result, "reported_error_code", "") or "UNKNOWN_ERROR"
                ),
            }
        )
    logger.log(LogParams(
        action="TOOL_EXECUTE",
        user_id=str(getattr(config, "my_agent_owner_id", "") or "main"),
        channel=str(getattr(config, "my_agent_owner_kind", "") or "main"),
        target_type=tool,
        target_id=_tool_target(tool, payload),
        status=AuditStatus.SUCCESS if ok else AuditStatus.ERROR,
        details=details,
    ))


def _tool_target(tool: str, payload: dict) -> str:
    params = payload.get("params")
    source = params if isinstance(params, dict) else payload
    for key in _PRIVILEGED_TARGETS.get(tool, ()):
        value = source.get(key)
        if value:
            return str(value)[:200]
    return ""


def _logger_for(config: object) -> object | None:
    key = str(getattr(config, "audit_log_path", "") or "")
    with _LOCK:
        logger = _LOGGERS.get(key)
        if logger is None:
            from ..audit.logger import AuditLogger

            logger = AuditLogger(config)
            _LOGGERS[key] = logger
        return logger


def reset_for_test() -> None:
    """测试钩子:清掉缓存的 AuditLogger(换 tmp 审计路径时用)。"""
    with _LOCK:
        _LOGGERS.clear()
