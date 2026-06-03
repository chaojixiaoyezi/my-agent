
from __future__ import annotations


def _bootstrap_guidance_lines(contract: dict[str, object]) -> list[str]:
    bootstrap = contract.get("bootstrap_contract")
    if not isinstance(bootstrap, dict):
        return []
    targets = _bootstrap_targets(bootstrap.get("materialization_targets"))
    actions = _bootstrap_actions(bootstrap.get("startup_actions"))
    if not targets and not actions:
        return []
    lines = ["开工参考："]
    if targets:
        lines.append("- 可在合适时让下面这些结构化目标中的一个真实出现，避免长期只做目录查看。")
        lines.extend(f"  - {target}" for target in targets[:6])
        lines.append("- 阶段目标可以先写草稿或最小有效骨架，后续再根据证据和验收反馈持续修订。")
    if actions:
        lines.extend(_startup_action_lines(actions))
    lines.append("- 如果暂时不确定具体工具，可以先 list_tools 一次，再按任务进展选择检索、写入或构建工具。")
    return lines


def _bootstrap_targets(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("workspace_relative_path") or "").strip()
        target_type = str(item.get("target_type") or "").strip()
        if relative:
            lines.append(f"{target_type or 'target'}: {relative}")
    return lines


def _bootstrap_actions(items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _startup_action_lines(actions: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for action in sorted(actions, key=lambda item: int(item.get("priority", 0))):
        lines.extend(_startup_action_line_group(action))
    return lines


def _startup_action_line_group(action: dict[str, object]) -> list[str]:
    code = str(action.get("action") or "").strip()
    if code == "materialize_target":
        return ["- 先创建目录并开始写入第一个目标路径，再继续补齐其余内容。"]
    if code == "materialize_checkpoint":
        return _materialize_checkpoint_lines(action)
    if code == "invoke_builder_tool":
        return _builder_startup_lines(action)
    return []


def _materialize_checkpoint_lines(action: dict[str, object]) -> list[str]:
    checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
    if not checkpoint_ref:
        return []
    if action.get("research_first") is True or action.get("requires_auditable_source_evidence") is True:
        fields = ", ".join(str(item) for item in action.get("required_structured_fields", []) if str(item).strip())
        suffix = f"，必须包含 {fields}" if fields else ""
        if action.get("research_first") is True:
            return [
                f"- 先完成来源采集/读取，再写 checkpoint: {checkpoint_ref}",
                f"- 这是来源型 checkpoint，优先用采集/转换工具物化{suffix}。",
            ]
        return [
            f"- 先真实写出 checkpoint: {checkpoint_ref}",
            f"- 这是来源型 checkpoint，优先用采集/转换工具物化{suffix}。",
        ]
    return [
        f"- 先真实写出 checkpoint: {checkpoint_ref}",
        f"- 如果资料还没收全，可以给 {checkpoint_ref} 写阶段草稿，再继续抓取/整理。",
    ]


def _builder_startup_lines(action: dict[str, object]) -> list[str]:
    builder = str(action.get("builder_tool") or "").strip()
    if not builder:
        return []
    source_ref = str(action.get("source_ref") or "").strip()
    output_ref = str(action.get("output_ref") or "").strip()
    if source_ref and output_ref:
        return [f"- 阶段数据就绪后，用通用写入/命令工具生成 {output_ref}；来源参考 {source_ref}。"]
    return ["- 阶段数据就绪后，用通用写入/命令工具生成后续产物。"]


def _builder_source_param(staging: dict[str, object], builder_tool: str) -> str:
    for key in ("source_param", "source_param_name", "input_param"):
        value = str(staging.get(key) or "").strip()
        if value:
            return value
    return "source_ref"
