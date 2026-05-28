# LLM: Dispatch payload helpers keep orchestration tools thin and refs-only.
# 模块用途: 构建 dispatch_subagents 工具返回给 runner 的轻量记录，不读取正文或执行动作。

from __future__ import annotations

from pathlib import Path


# LLM: dispatch_record_payload exposes compact dispatch facts to coordinator runners.
# 函数用途: 给模型工具返回每条调度记录的核心字段和 runner 结果摘要；不再注入额外验收阶段。
def dispatch_record_payload(item) -> dict[str, object]:
    payload = {
        "step": item.step,
        "action": item.action,
        "run_id": item.run_id,
        "ok": item.ok,
        "record_dry_run": item.dry_run,
        "record_applied": item.applied,
        "message": item.message,
        "before_status": item.before_status,
        "after_status": item.after_status,
    }
    payload.update(_dispatch_record_runner_payload(item))
    return payload


# LLM: dispatch_recovery_payload keeps blocking runner-selection fixes visible before bulky records.
# 函数用途: 当模型传错 run_id 时，把可执行恢复指令放到 dispatch_subagents 顶层返回。
def dispatch_recovery_payload(records: list[object]) -> dict[str, object]:
    for item in records:
        if item.step == "runner_selection" and item.action == "invalid_run_ids":
            return {
                "action": "retry_dispatch_with_valid_run_id",
                "message": item.message,
                "valid_run_ids": _valid_run_ids_from_refs(item.evidence_paths or []),
                "valid_task_refs": list(item.evidence_paths or [])[:20],
            }
    return {}


# LLM: _dispatch_record_runner_payload surfaces nested runner child creation as facts, not prose guesses.
# 函数用途: 顶层主代理读取 dispatch_subagents 结果时，直接看到 runner 内部创建的 child ids/roles。
def _dispatch_record_runner_payload(item) -> dict[str, object]:
    keys = {
        "runner_summary": "runner_summary",
        "runner_created_child_count": "runner_created_child_count",
        "runner_created_child_ids": "runner_created_child_ids",
        "runner_created_roles": "runner_created_roles",
        "runner_child_status_counts": "runner_child_status_counts",
        "runner_unfinished_child_ids": "runner_unfinished_child_ids",
        "runner_partial_success": "runner_partial_success",
    }
    return {
        name: value
        for name, attr in keys.items()
        if (value := getattr(item, attr, "")) not in ("", 0, [], None)
    }


# LLM: _valid_run_ids_from_refs converts task-dir refs into machine-readable retry ids.
# 函数用途: 从 dispatch 阻断证据路径中提取 run id，放到顶层 recovery payload，避免模型从自然语言里猜 id。
def _valid_run_ids_from_refs(paths: list[str]) -> list[str]:
    ids: list[str] = []
    for path in paths[:20]:
        run_id = Path(str(path)).name.strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


# LLM: _dispatch_record_acceptance_payload keeps result check/follow-up refs compact and optional.
# 函数用途: 只在字段存在时附加测试数量、失败数、follow-up 动作和引用，避免普通记录输出噪声。
# LLM: _unique_text mirrors the local refs de-duplication used by repair payload helpers.
# 函数用途: 过滤空字符串并保持 artifact refs 的首次出现顺序。
def _unique_text(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in unique:
            unique.append(text)
    return unique
