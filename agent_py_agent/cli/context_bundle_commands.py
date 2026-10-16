# LLM: CLI 只观测原 owner 快照；完整工具信息经统一引用读口解析，不重算权限或回退其它 owner。
# 模块用途: 展示最新上下文包及工具合同诊断，兼容已发布的内联快照。
from __future__ import annotations

import json
from pathlib import Path

from ..agent.common.json_io import read_json_object_report
from ..agent.user_space.context_bundle import latest_main_context_bundle_path
from ..agent.user_space.context_bundle_tool_manifest import (
    ToolManifestLoadError,
    load_bundle_tool_manifest,
)
from .common import make_agent


def cmd_context_bundle(args) -> int:
    action = str(getattr(args, "context_bundle_action", "") or "latest")
    if action == "latest":
        return _cmd_context_bundle_latest(args)
    print(f"unsupported context-bundle action: {action}")
    return 2


def _cmd_context_bundle_latest(args) -> int:
    agent = make_agent(args)
    path = latest_main_context_bundle_path(getattr(agent, "home_paths", None))
    payload = _latest_payload(path)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["ok"] else 2
    _print_latest(payload)
    return 0 if payload["ok"] else 2


# LLM: 实际快照路径必须交给清单读口定位当前归档；损坏快照保留原结构化读取错误。
# 函数用途: 读取最新上下文包并生成 CLI 的诊断投影。
def _latest_payload(path: str) -> dict:
    if not path:
        return {"ok": False, "status": "missing_context_bundle", "path": "", "scope": {}, "self_check": {}}
    report = read_json_object_report(Path(path), context="cli.context_bundle.latest.read")
    data = report.payload
    if not data:
        payload = {"ok": False, "status": "invalid_context_bundle", "path": path, "scope": {}, "self_check": {}}
        if report.load_error:
            payload["load_error"] = report.load_error
        return payload
    return {
        "ok": True,
        "status": "ok",
        "path": path,
        "schema": str(data.get("schema") or ""),
        "version": int(data.get("version", 0) or 0),
        "scope": dict(data.get("scope", {}) if isinstance(data.get("scope"), dict) else {}),
        "run_scope": dict(data.get("run_scope", {}) if isinstance(data.get("run_scope"), dict) else {}),
        "tool_manifest": _tool_manifest_summary(data, path),
        "acceptance_contract": dict(
            data.get("acceptance_contract", {}) if isinstance(data.get("acceptance_contract"), dict) else {}
        ),
        "self_check": dict(data.get("self_check", {}) if isinstance(data.get("self_check"), dict) else {}),
        "prompt_budget": dict(data.get("prompt_budget", {}) if isinstance(data.get("prompt_budget"), dict) else {}),
    }


# LLM: 观测输出保持原有字段形状；failure_taxonomy 在新快照里位于按引用存放的完整清单中，
#   需要按"快照文件自己的路径"解引用读取（见 _manifest_failure_taxonomy），
#   visible/executable/permission_mode 仍在摘要上。
# 函数用途: 给 context-bundle latest 输出生成工具清单观测摘要。
def _tool_manifest_summary(data: dict, snapshot_path: str) -> dict:
    manifest = data.get("tool_manifest", {}) if isinstance(data.get("tool_manifest"), dict) else {}
    return {
        "visible_tools": list(manifest.get("visible_tools") or []),
        "executable_tools": list(manifest.get("executable_tools") or []),
        "permission_mode": str(manifest.get("permission_mode") or ""),
        "failure_taxonomy": _manifest_failure_taxonomy(data, manifest, snapshot_path),
    }


# LLM: 新快照的 failure_taxonomy 在按引用存放的完整清单里，解引用时把快照文件自己的路径传进去
#   （不信任快照里写的绝对路径）；旧快照内联时 load_bundle_tool_manifest 直接返回内联那份。
#   文件缺失或被篡改时退回摘要内联字段（可能为空），CLI 观测不因清单文件问题而失败。
# 函数用途: 取工具失败分类学列表，供 context-bundle latest 观测输出。
def _manifest_failure_taxonomy(data: dict, manifest: dict, snapshot_path: str) -> list:
    try:
        full = load_bundle_tool_manifest(data, snapshot_path=snapshot_path)
    except ToolManifestLoadError:
        full = manifest
    return list(full.get("failure_taxonomy") or [])

def _print_latest(payload: dict) -> None:
    print("MY-AGENT CONTEXT BUNDLE LATEST")
    print(f"status={payload['status']}")
    print(f"path={payload['path']}")
    print("scope=" + json.dumps(payload.get("scope", {}), ensure_ascii=False, sort_keys=True))
    print("self_check=" + json.dumps(payload.get("self_check", {}), ensure_ascii=False, sort_keys=True))


__all__ = ["cmd_context_bundle"]
