
from __future__ import annotations

from typing import Any


def runtime_desired_outputs(
    contract: dict[str, Any] | None,
    runtime_injections: tuple[str, ...] | list[str] | str = (),
) -> list[dict[str, str]]:
    outputs = _contract_outputs(contract)
    return outputs or _workspace_outputs(runtime_injections)


def run_intent_has_desired_outputs(intent: dict[str, Any]) -> bool:
    desired = intent.get("desired_outputs") if isinstance(intent, dict) else None
    if not isinstance(desired, dict):
        return False
    items = desired.get("items")
    return isinstance(items, list | tuple) and any(str(item or "").strip() for item in items)


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


def _workspace_outputs(runtime_injections: tuple[str, ...] | list[str] | str) -> list[dict[str, str]]:
    output_dir = _workspace_output_dir(runtime_injections)
    return [{"artifact_id": "task_output_dir", "kind": "directory", "target_path": output_dir}] if output_dir else []


def _workspace_output_dir(runtime_injections: tuple[str, ...] | list[str] | str) -> str:
    lines = [
        line.strip()
        for text in _runtime_injection_texts(runtime_injections)
        if "# Current Task Workspace" in text
        for line in text.splitlines()
    ]
    return next((line.split(":", 1)[1].strip() for line in lines if line.startswith("- output_dir:")), "")


def _runtime_injection_texts(value: tuple[str, ...] | list[str] | str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return []


__all__ = ["run_intent_has_desired_outputs", "runtime_desired_outputs"]
