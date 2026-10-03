# LLM: 能力包 v2 块 3 接进工具执行唯一缝隙（agent_core/tool_call_runtime）的两个钩子：
#   - 改工作区的工具（写工具或 ToolRuntimePolicy.mutates_workspace）执行前记基线；
#   - 模型不再调工具准备收尾时跑收尾核验，有错误时给出一次返工提示（closeout_rework_block，由 response_decision 调）；
#   - 写工具成功后，从回执的宿主字段（path/target_path/output_path/artifact_ref/artifact_refs，都是绝对路径）取写出的文件，
#     跑写后核验，把有界摘要列表并进同一个 handler_details 信封的 pack_verification 键（归档白名单和回执渲染都读它）。
#   executor 外的写后/收尾核验显式绑定 params.cancellation_token（本 run），取消只影响核验、不改写工具成败。
#   改动同步 test_pack_verification_service.py 与 tooling/runtime_facts、tool_call_archive_record 的 pack_verification 键。
# 模块用途: 把宿主核验挂到工具执行前后，而不新增任何模型可调用的工具。

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from ..common.cancellation import bind_cancellation_token, current_cancellation_token
from ..plugin_installation import PluginInstallationError
from ..tooling.write_boundary import WRITE_TOOL_NAMES
from .pack_verification_scope import host_verification_enabled
from .pack_verification_service import (
    capture_pack_baseline,
    pack_verification_closeout_block,
    verify_written_files,
)

# 回执里记写出文件的宿主字段（单个绝对路径）。
_PATH_KEYS = ("path", "target_path", "output_path", "artifact_ref")
# 核验过程中可能遇到、只影响核验本身的已知错误；遇到就当作没核验，工具结果不变。
_HOOK_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error, PluginInstallationError)


# LLM: 只在开关打开且这个工具会改工作区时扫一次；工具名在写工具集合里、或运行策略声明 mutates_workspace 才算。
# 函数用途: 在可能改工作区的工具执行前记下本 run 的工作区基线。
def capture_baseline_before_tool(agent: object, params: object, tool_name: str) -> None:
    # 先做便宜的结构判断，只有会改工作区的工具才去读能力配置开关
    if not _mutates_workspace(params, tool_name) or not host_verification_enabled(agent):
        return
    try:
        capture_pack_baseline(agent, params)
    except _HOOK_ERRORS:
        return


# LLM: 写后位于 executor 令牌范围外，必须用本 run 参数重新绑定；未传参数令牌的直接调用保持原上下文，取消摘要不翻转写入事实。
# 函数用途: 写工具成功后可取消地核验交付物，把有界摘要并进原回执。
def attach_post_write_verification(agent: object, params: object, result: object) -> object:
    tool_name = str(getattr(result, "tool_name", "") or "")
    if tool_name not in WRITE_TOOL_NAMES or not getattr(result, "ok", False) or not host_verification_enabled(agent):
        return result
    metadata = dict(getattr(result, "metadata", None) or {})
    details = dict(metadata.get("handler_details") or {})
    try:
        with bind_cancellation_token(getattr(params, "cancellation_token", None) or current_cancellation_token()):
            summaries = verify_written_files(agent, params, written_paths(details))
    except _HOOK_ERRORS:
        return result
    if not summaries:
        return result
    details["pack_verification"] = summaries
    metadata["handler_details"] = details
    return replace(result, metadata=metadata)


# LLM: 收尾不在工具 executor 范围内，须显式绑定当前 run 取消；已取消仍逐目标记账，不给返工提示，开关关保持零 I/O。
# 函数用途: 可取消地跑宿主收尾核验，仅未取消的质量失败返回返工提示。
def closeout_rework_block(agent: object, params: object) -> str:
    if not host_verification_enabled(agent):
        return ""
    try:
        with bind_cancellation_token(getattr(params, "cancellation_token", None) or current_cancellation_token()):
            return pack_verification_closeout_block(agent, params)
    except _HOOK_ERRORS:
        return ""


# LLM: artifact_refs 里标 deleted 的是旧地址墓碑，不算写出的文件；去重并保持回执里的顺序。
# 函数用途: 从写工具回执取出本次写出的绝对路径。
def written_paths(details: dict) -> list[Path]:
    values = [details.get(key) for key in _PATH_KEYS]
    refs = details.get("artifact_refs") if isinstance(details.get("artifact_refs"), list) else []
    values.extend(ref.get("path") for ref in refs if isinstance(ref, dict) and ref.get("status") != "deleted")
    paths: list[Path] = []
    for value in values:
        if isinstance(value, str) and value and Path(value).is_absolute() and Path(value) not in paths:
            paths.append(Path(value))
    return paths


# 函数用途: 判断这个工具会不会改工作区（写工具，或运行策略声明了 mutates_workspace）。
def _mutates_workspace(params: object, tool_name: str) -> bool:
    if tool_name in WRITE_TOOL_NAMES:
        return True
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    runtime = snapshot.runtime(tool_name) if snapshot is not None else None
    return bool(getattr(getattr(runtime, "runtime_policy", None), "mutates_workspace", False))
