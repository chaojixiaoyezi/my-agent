"""workspace 根解析取证探针（2026-08-15 双席 seq2072 要求，只读不改行为）。

3×3 真机：收口时 delivery_verify 事件报「workspace 根缺失」但 contract 非空
（有 contract_hash）——矛盾点。本探针复现 current_run_task_workspace_root
的五个候选解析全过程，逐候选 try/except 返回结构化值，供 gate 入口日志
定位丢失边界。生产路径不调用本模块（仅 gate 取证日志用）。
"""

from __future__ import annotations


def workspace_root_candidates_probe(agent: object, params: object) -> list[str]:
    """逐个候选解析（与 current_run_task_workspace_root 同源，每项独立容错）。"""
    try:
        from ..run_task_workspace_writer import (
            _internal_agent_run_workspace,
            _subagent_run_workspace,
            _workspace_root_from_mapping,
        )
    except Exception:  # noqa: BLE001 探针失败不阻断
        return ["probe_import_failed"]
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None
    results: list[str] = []
    for label, value in (
        ("subagent", _safe(_subagent_run_workspace, agent, params)),
        ("internal", _safe(_internal_agent_run_workspace, params, attrs)),
        (
            "contract.task_workspace",
            _safe(_workspace_root_from_mapping, contract, "task_workspace"),
        ),
        (
            "attrs.run_workspace",
            _safe(_workspace_root_from_mapping, attrs, "run_workspace"),
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
        results.append(f"{label}={value!r}")
    return results


def _safe(func, *args, **kwargs):  # noqa: ANN001 探针容错
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 探针失败记录类型
        return f"<exc:{type(exc).__name__}>"
