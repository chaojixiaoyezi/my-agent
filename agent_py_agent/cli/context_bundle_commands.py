# LLM: Context bundle CLI exposes refs-only observability for latest root run cards.
# 模块用途: 提供 `context-bundle latest` 只读命令，让用户和前端查看最新任务卡、scope 和自检状态。

from __future__ import annotations

import json
from pathlib import Path

from ..agent.user_space.context_bundle import latest_main_context_bundle_path
from .common import make_agent


# LLM: cmd_context_bundle is the top-level CLI dispatcher for context bundle diagnostics.
# 函数用途: 根据子命令输出 context bundle 观测信息；只读文件，不触发模型或 compact。
def cmd_context_bundle(args) -> int:
    action = str(getattr(args, "context_bundle_action", "") or "latest")
    if action == "latest":
        return _cmd_context_bundle_latest(args)
    print(f"unsupported context-bundle action: {action}")
    return 2


# LLM: _cmd_context_bundle_latest renders the latest saved main context bundle status.
# 函数用途: 读取 latest_context_bundle.json 的轻量摘要，展示 path、scope、self_check 和关键合同字段。
def _cmd_context_bundle_latest(args) -> int:
    agent = make_agent(args)
    path = latest_main_context_bundle_path(getattr(agent, "home_paths", None))
    payload = _latest_payload(path)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["ok"] else 2
    _print_latest(payload)
    return 0 if payload["ok"] else 2


# LLM: _latest_payload is intentionally small; full JSON remains at the reported path.
# 函数用途: 从最新 context bundle 提取观测字段，避免 CLI 把完整大 JSON 直接打印到人类输出。
def _latest_payload(path: str) -> dict:
    if not path:
        return {"ok": False, "status": "missing_context_bundle", "path": "", "scope": {}, "self_check": {}}
    data = _read_json(Path(path))
    if not data:
        return {"ok": False, "status": "invalid_context_bundle", "path": path, "scope": {}, "self_check": {}}
    return {
        "ok": True,
        "status": "ok",
        "path": path,
        "schema": str(data.get("schema") or ""),
        "version": int(data.get("version", 0) or 0),
        "scope": dict(data.get("scope", {}) if isinstance(data.get("scope"), dict) else {}),
        "run_scope": dict(data.get("run_scope", {}) if isinstance(data.get("run_scope"), dict) else {}),
        "tool_manifest": _tool_manifest_summary(data),
        "acceptance_contract": dict(
            data.get("acceptance_contract", {}) if isinstance(data.get("acceptance_contract"), dict) else {}
        ),
        "self_check": dict(data.get("self_check", {}) if isinstance(data.get("self_check"), dict) else {}),
        "prompt_budget": dict(data.get("prompt_budget", {}) if isinstance(data.get("prompt_budget"), dict) else {}),
    }


# LLM: _tool_manifest_summary reports counts and names without dumping full tool specs.
# 函数用途: 输出工具清单摘要，方便用户判断恢复后模型能看到/执行哪些工具。
def _tool_manifest_summary(data: dict) -> dict:
    manifest = data.get("tool_manifest", {}) if isinstance(data.get("tool_manifest"), dict) else {}
    return {
        "visible_tools": list(manifest.get("visible_tools") or []),
        "executable_tools": list(manifest.get("executable_tools") or []),
        "permission_mode": str(manifest.get("permission_mode") or ""),
        "failure_taxonomy": list(manifest.get("failure_taxonomy") or []),
    }


# LLM: _read_json keeps bad files diagnostic-only.
# 函数用途: 读取 JSON 对象；缺失、坏 JSON 或非对象时返回空 dict。
def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _print_latest gives humans the same key fields as the JSON payload.
# 函数用途: 输出简短 context bundle 观测报告，完整细节仍通过 path 查看。
def _print_latest(payload: dict) -> None:
    print("MY-AGENT CONTEXT BUNDLE LATEST")
    print(f"status={payload['status']}")
    print(f"path={payload['path']}")
    print("scope=" + json.dumps(payload.get("scope", {}), ensure_ascii=False, sort_keys=True))
    print("self_check=" + json.dumps(payload.get("self_check", {}), ensure_ascii=False, sort_keys=True))


__all__ = ["cmd_context_bundle"]
