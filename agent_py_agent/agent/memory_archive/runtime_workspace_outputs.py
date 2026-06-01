# LLM: Runtime workspace outputs preserve output/work roots as soft resume hints.
# 模块用途: 从交付合同或 Current Task Workspace 注入块提取目标输出路径，供 runtime_fact/compact 续接使用。

from __future__ import annotations

from typing import Any


# LLM: runtime_desired_outputs prefers explicit delivery contracts, then falls back to task output_dir.
# 函数用途: 返回可审计的目标产物提示；没有合同但有任务工作区时，把 output/ 作为软目标目录。
def runtime_desired_outputs(
    contract: dict[str, Any] | None,
    runtime_injections: tuple[str, ...] | list[str] | str = (),
) -> list[dict[str, str]]:
    outputs = _contract_outputs(contract)
    return outputs or _workspace_outputs(runtime_injections)


# LLM: run_intent_has_desired_outputs checks the compact-friendly run_intent payload shape.
# 函数用途: 判断 run_intent 是否已经有 desired_outputs，避免重复覆盖显式合同。
def run_intent_has_desired_outputs(intent: dict[str, Any]) -> bool:
    desired = intent.get("desired_outputs") if isinstance(intent, dict) else None
    if not isinstance(desired, dict):
        return False
    items = desired.get("items")
    return isinstance(items, list | tuple) and any(str(item or "").strip() for item in items)


# LLM: _contract_outputs extracts explicit artifact paths without guessing formats.
# 函数用途: 从 delivery contract artifacts 中提取目标路径、类型和 artifact id。
def _contract_outputs(contract: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(contract, dict):
        return []
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list | tuple):
        return []
    outputs: list[dict[str, str]] = []
    for artifact in artifacts:
        if isinstance(artifact, dict) and (item := _artifact_output(artifact)):
            outputs.append(item)
    return outputs


# LLM: _artifact_output accepts common open-world output path field names.
# 函数用途: 从单个 artifact 对象里取 path/preferred_path/target_path/output_path。
def _artifact_output(artifact: dict[str, Any]) -> dict[str, str]:
    path = str(
        artifact.get("path")
        or artifact.get("preferred_path")
        or artifact.get("target_path")
        or artifact.get("output_path")
        or ""
    ).strip()
    artifact_id = str(artifact.get("artifact_id") or artifact.get("id") or path).strip()
    kind = str(artifact.get("kind") or artifact.get("type") or "").strip()
    return {"artifact_id": artifact_id, "kind": kind, "target_path": path} if path or artifact_id or kind else {}


# LLM: _workspace_outputs turns task output_dir into a soft desired output root.
# 函数用途: 没有显式合同但有任务工作区时，返回 output/ 目录提示。
def _workspace_outputs(runtime_injections: tuple[str, ...] | list[str] | str) -> list[dict[str, str]]:
    output_dir = _workspace_output_dir(runtime_injections)
    return [{"artifact_id": "task_output_dir", "kind": "directory", "target_path": output_dir}] if output_dir else []


# LLM: _workspace_output_dir parses only the controlled Current Task Workspace block.
# 函数用途: 从运行时注入块中读取 output_dir 行，不解析用户自由文本。
def _workspace_output_dir(runtime_injections: tuple[str, ...] | list[str] | str) -> str:
    lines = [
        line.strip()
        for text in _runtime_injection_texts(runtime_injections)
        if "# Current Task Workspace" in text
        for line in text.splitlines()
    ]
    return next((line.split(":", 1)[1].strip() for line in lines if line.startswith("- output_dir:")), "")


# LLM: _runtime_injection_texts tolerates old callers that pass one string.
# 函数用途: 将 runtime_injections 统一成字符串列表供路径提示解析。
def _runtime_injection_texts(value: tuple[str, ...] | list[str] | str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return []


__all__ = ["run_intent_has_desired_outputs", "runtime_desired_outputs"]
