"""workspace 根解析取证探针（2026-08-15 双席 seq2072/2075/2076 要求，只读不改行为）。

3×3 真机：收口时 delivery_verify 事件报「workspace 根缺失」但 contract 非空
（有 contract_hash）——矛盾点。本探针复现 current_run_task_workspace_root
的五个候选解析全过程，逐候选 try/except 返回结构化值，供 gate 入口诊断
记录定位丢失边界。生产路径不调用本模块（仅 gate 取证日志用）。

字段按双席最小清单：运行身份（模块 __file__/PID/启动时间）、参数边界
（run/attempt/task/source/params 类型/resume_context）、contract 形状
（hash/类型/顶层 keys/task_workspace 的 keys 与 root，绝不写完整 contract
或密钥）、五候选解析链（来源标签+原始值+被选值+异常）。
"""

from __future__ import annotations

import hashlib
import os
import time


def workspace_root_candidates_probe(agent: object, params: object) -> dict[str, object]:
    """逐候选解析（与 current_run_task_workspace_root 同源，每项独立容错）。"""
    try:
        from ..run_task_workspace_writer import (
            _internal_agent_run_workspace,
            _subagent_run_workspace,
            _workspace_root_from_mapping,
        )
    except Exception as exc:  # noqa: BLE001 探针失败不阻断
        return {"probe_error": f"import_failed:{type(exc).__name__}"}
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None

    candidates: list[dict[str, object]] = []
    for label, value in (
        ("subagent", _safe(lambda: _subagent_run_workspace(agent, params))),
        ("internal", _safe(lambda: _internal_agent_run_workspace(params, attrs))),
        (
            "contract.task_workspace",
            _safe(lambda: _workspace_root_from_mapping(contract, "task_workspace")),
        ),
        (
            "attrs.run_workspace",
            _safe(lambda: _workspace_root_from_mapping(attrs, "run_workspace")),
        ),
        (
            "agent._current_run_task_workspace",
            _safe(
                lambda: str(
                    getattr(agent, "_current_run_task_workspace", "") or ""
                ).strip()
            ),
        ),
    ):
        candidates.append({"source": label, "value": value})

    contract_shape: dict[str, object] = {"type": type(contract).__name__}
    if isinstance(contract, dict):
        try:
            raw = __import__("json").dumps(contract, ensure_ascii=False, sort_keys=True)
            contract_shape["hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        except (TypeError, ValueError):
            contract_shape["hash"] = "unserializable"
        contract_shape["top_keys"] = sorted(str(k) for k in contract.keys())[:12]
        tw = contract.get("task_workspace")
        if isinstance(tw, dict):
            contract_shape["task_workspace_keys"] = sorted(str(k) for k in tw.keys())
            contract_shape["task_root"] = str(tw.get("task_root") or "")
        else:
            contract_shape["task_workspace_type"] = type(tw).__name__

    module_path = ""
    try:
        module_path = str(
            __import__(
                "agent_py_agent.agent.agent_core.run_task_workspace_writer",
                fromlist=["x"],
            ).__file__
        )
    except Exception:  # noqa: BLE001
        module_path = "unresolved"
    try:
        module_digest = ""
        with open(module_path, "rb") as fh:
            module_digest = hashlib.sha256(fh.read(65536)).hexdigest()[:12]
    except (OSError, TypeError):
        module_digest = "unreadable"

    return {
        "ts": int(time.time()),
        "pid": os.getpid(),
        "module_file": module_path,
        "module_head_sha": module_digest,
        "run_id": str(getattr(params, "run_id", "") or ""),
        "attempt_id": str(getattr(params, "attempt_id", "") or ""),
        "task_id": str(getattr(params, "task_id", "") or ""),
        "source": str(getattr(params, "source", "") or ""),
        "context_scope": str(getattr(params, "context_scope", "") or ""),
        "params_type": type(params).__name__,
        "resume_context": bool(getattr(params, "resume_context", False)),
        "contract": contract_shape,
        "candidates": candidates,
    }


def _safe(func):  # noqa: ANN001 探针容错(架构守卫: 服务接口禁 *args/**kwargs, 调用方用 lambda 包裹)
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 探针失败记录类型
        return f"<exc:{type(exc).__name__}>"
