# LLM: WP-4只读投影共用原nofollow文件入口，不初始化资产Store、进程Registry或唤醒事务。
# 模块用途: 为目录上下文和工作区资产提供冷读回退，并过滤后台进程的线程范围。
from __future__ import annotations

import json
import os
from pathlib import Path

from ..common.nofollow_fs import list_names_beneath, open_readonly_file_beneath, read_bytes_beneath
from .authorization import workspace_directory
from .errors import HostAPIError

_ASSET_KINDS = ("skill", "script", "workflow", "monitor")


# LLM: 缺目录是正常空集合；权限、链接和损坏不是“没有资产”，不可吞掉。
# 函数用途: 在同一可信根下冷列一个相对目录。
def _names(root, parts):
    try:
        return list_names_beneath(root, parts)
    except (FileNotFoundError, NotADirectoryError):
        return []


# LLM: 仅投影有界公共标量，不将代码、凭据、任意refs或文件路径带入资产列表。
# 函数用途: 为扫描到的资产生成带来源层的安全摘要。
def _asset_row(identifier, kind, scope, directory, payload=None):
    payload = payload or {}
    name = payload.get("name") or payload.get("title") or identifier
    summary = payload.get("summary") or payload.get("description") or ""
    return {"id": identifier, "kind": kind, "name": str(name)[:256], "summary": str(summary)[:1024],
            "source_scope": scope, "workspace_root": str(directory) if scope == "workspace" else ""}


# LLM: 技能发现有界且不跟随链接；仅确认SKILL.md存在和可安全读取，不在Host层另建技能解析器。
# 函数用途: 兼容普通和learned嵌套目录，返回技能目录身份。
def _skills(root, parts, scope, directory, depth=0):
    if depth > 8:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    names = _names(root, parts)
    if "SKILL.md" in names:
        read_bytes_beneath(root, (*parts, "SKILL.md"), max_bytes=1024 * 1024)
        return [_asset_row(parts[-1], "skill", scope, directory)]
    rows = []
    for name in sorted(names):
        if not name.startswith("."):
            rows.extend(_skills(root, (*parts, name), scope, directory, depth + 1))
    return rows


# LLM: 目录/文件形状均沿nofollow核验；仅使用原scripts/id/script.json、workflows/id/workflow.json和monitor布局。
# 函数用途: 无锁冷读一层资产元数据，缺失文件不补建。
def _layer(root, prefix, scope, directory):
    rows = _skills(root, (*prefix, "skills"), scope, directory)
    for kind in ("script", "workflow", "monitor"):
        parts = (*prefix, kind + "s")
        if kind == "monitor" and scope == "owner":
            parts = ("data", "monitors")
        for name in sorted(_names(root, parts)):
            if name.startswith("."):
                continue
            leaf = (*parts, name) if name.endswith(".json") else (*parts, name, kind + ".json")
            try:
                raw = read_bytes_beneath(root, leaf, max_bytes=1024 * 1024)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if raw is None:
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            identifier = payload.get("id") or payload.get(kind + "_id") or Path(name).stem
            if not isinstance(identifier, str):
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            rows.append(_asset_row(identifier, kind, scope, directory, payload))
    return rows


# LLM: 优先原Overlay，准确缺模块才冷读回退；只公开元数据，原服务异常不能降级成不一致扫描。
# 函数用途: 返回workspace>owner>builtin资产摘要，不公开代码或授予执行权。
def workspace_asset_rows(context, directory, kind="all"):
    home = context.owner_home.home_dir
    package = Path(__file__).resolve().parents[2]
    try:
        from ..assets.workspace_assets import WorkspaceAssetOverlay
    except ModuleNotFoundError as error:
        if error.name != "agent_py_agent.agent.assets.workspace_assets":
            raise
    else:
        overlay = WorkspaceAssetOverlay(directory, owner_home=home, builtin_roots=(package,))
        return [{**_asset_row(row["id"], row["kind"], row["source_scope"], directory, row),
                 "revision": row.get("revision", 0)} for row in overlay.list_assets(None if kind == "all" else kind)]
    rows = {}
    for root, prefix, scope in ((package, (), "builtin"), (home, (), "owner"), (directory, (".agent",), "workspace")):
        for row in _layer(root, prefix, scope, directory):
            rows[(row["kind"], row["id"])] = row
    return [row for key, row in sorted(rows.items()) if kind == "all" or key[0] == kind]


# LLM: 不读取规则正文、不跟随规则链接；仅安全打开确认存在，FD始终关闭，不创建目录。
# 函数用途: 检查一份AGENTS.md是否可作为本目录规则使用。
def _has_agents(root, parts):
    try:
        descriptor = open_readonly_file_beneath(root, parts)
    except FileNotFoundError:
        return False
    os.close(descriptor)
    return True


# LLM: 原线程范围先于目录读取；返回真实路径、有效owner及各资产数量，不将安装者身份冒充管理员。
# 函数用途: 生成线程或默认家目录的工作区上下文。
def workspace_snapshot(authorized, context, conversation, store, payload):
    thread = (conversation.require_thread(store, payload["thread_id"], authorized, context)
              if payload.get("thread_id") else None)
    from .conversations import thread_workspace
    directory = workspace_directory(context, thread_workspace(thread) if thread else str(context.owner_home.home_dir))
    rows = workspace_asset_rows(context, directory)
    counts = {kind + "s": sum(row["kind"] == kind for row in rows) for kind in _ASSET_KINDS}
    counts["has_agents_md"] = any(_has_agents(root, parts) for root, parts in (
        (context.owner_home.home_dir, ("AGENTS.md",)), (directory, ("AGENTS.md",)), (directory, (".agent", "AGENTS.md"))))
    return {"thread_id": thread.thread_id if thread else None, "cwd": str(directory), "workspace_root": str(directory),
            "runtime_workspace_roots": list(thread.runtime_workspace_roots) if thread else [str(directory)],
            "owner_home": str(context.owner_home.home_dir), "is_admin": context.owner_home.owner_id == "local/main",
            "overlay_counts": counts}


# LLM: 线程和显式task必须都在原范围内；不调用状态修复、通知消费或控制方法，未知归属失败关闭。
# 函数用途: 判断后台会话是否位于当前插件允许的线程及任务范围。
def process_visible(row, authorized, context, conversation, store, services=None):
    tid = row.get("thread_id")
    if not isinstance(tid, str) or not tid:
        return False
    try:
        conversation.require_thread(store, tid, authorized, context)
        return not row.get("task_id") or services is not None and services.allows(authorized, "task", row["task_id"])
    except HostAPIError as error:
        if error.status == 404:
            return False
        raise
