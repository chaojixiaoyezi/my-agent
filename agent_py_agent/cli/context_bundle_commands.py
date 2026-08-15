
from __future__ import annotations

import json
from pathlib import Path

from ..agent.common.json_io import read_json_object_report
from ..agent.user_space.context_bundle import latest_main_context_bundle_path
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
        "tool_manifest": _tool_manifest_summary(data),
        "acceptance_contract": dict(
            data.get("acceptance_contract", {}) if isinstance(data.get("acceptance_contract"), dict) else {}
        ),
        "self_check": dict(data.get("self_check", {}) if isinstance(data.get("self_check"), dict) else {}),
        "prompt_budget": dict(data.get("prompt_budget", {}) if isinstance(data.get("prompt_budget"), dict) else {}),
    }


def _tool_manifest_summary(data: dict) -> dict:
    manifest = data.get("tool_manifest", {}) if isinstance(data.get("tool_manifest"), dict) else {}
    return {
        "visible_tools": list(manifest.get("visible_tools") or []),
        "executable_tools": list(manifest.get("executable_tools") or []),
        "permission_mode": str(manifest.get("permission_mode") or ""),
        "failure_taxonomy": list(manifest.get("failure_taxonomy") or []),
    }

def _print_latest(payload: dict) -> None:
    print("MY-AGENT CONTEXT BUNDLE LATEST")
    print(f"status={payload['status']}")
    print(f"path={payload['path']}")
    print("scope=" + json.dumps(payload.get("scope", {}), ensure_ascii=False, sort_keys=True))
    print("self_check=" + json.dumps(payload.get("self_check", {}), ensure_ascii=False, sort_keys=True))


__all__ = ["cmd_context_bundle"]
