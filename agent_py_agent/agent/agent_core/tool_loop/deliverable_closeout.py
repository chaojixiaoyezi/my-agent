"""按宿主声明核验本轮文件，使用原资产采用事实恢复父级落地。"""
from __future__ import annotations

import os
from hashlib import file_digest, sha256
from pathlib import Path

from ...common.cancellation import (
    bind_cancellation_token,
    cancellation_requested,
    current_cancellation_token,
)
from ...common.nofollow_fs import read_bytes_beneath
from ...runtime_context import current_subagent_run_id
from ...subagents.services.output_alignment import anchored_output_refs, staged_output_manifest
from ...tooling.registry_invoke import land_accepted_workspace_file
from ...tooling.registry_workspace import open_workspace_delivery_file, workspace_ref_from_boundary

_CHILD_SCOPES = frozenset({"task_local", "control_plane"})
_LEGACY_CHILD_KEYS = ("declared_output_refs", "output_files", "output_refs")
_STATE_KEY = "deliverable_closeout_repairs"
_REPORT_KEY = "declared_file_delivery"
_ACCEPT_TOOL = "business_assets_versions_accept"
_MAX_DELIVERABLE_REPAIR_COUNT = 2
_MISSING_PREVIEW_COUNT = 5
# 多个权威声明对同一目标给出不同摘要时的标记；它不是十六进制摘要，任何文件都不会与之匹配。
_CONFLICTING_DIGESTS = "conflicting-declared-digests"


def _sources(params: object, agent=None):
    current = getattr(agent, "_current_run_params", None)
    binder = getattr(current, "conversation_task_binding_callback", None)
    load_contract = getattr(binder, "staged_delivery_contract", None)
    host_contract = load_contract() if callable(load_contract) else None
    for source in (host_contract, getattr(params, "delivery_contract", None),
                   getattr(params, "task_attributes", None), getattr(params, "live_archive_state", None)):
        if isinstance(source, dict):
            yield source


# LLM: 相对声明与文件工具共用同一可信基准：冻结 WorkspaceRef 根，旧任务无快照时用宿主写入边界的
#   execution_cwd（effective_registry_cwd 同一语义）；两者都没有时返回 None，由调用方保留为未解析义务。
# 函数用途: 给出解析相对交付声明的可信目录，不授予任何写权限。
def _declared_base(boundary) -> Path | None:
    workspace = workspace_ref_from_boundary(boundary)
    if workspace is not None:
        return workspace.root
    cwd = boundary.get("execution_cwd") if isinstance(boundary, dict) else None
    if isinstance(cwd, str) and cwd.strip() and Path(cwd.strip()).expanduser().is_absolute():
        return Path(cwd.strip()).expanduser()
    return None


# LLM: 无可信基准的相对声明保留为相对 Path（未解析），不按宿主进程 cwd 猜位置，也不静默丢弃。
def _declared_path(raw, base) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw or "://" in raw:
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        if base is None:
            return path
        path = base / path
    return Path(os.path.abspath(path))


# LLM: 摘要只来自声明 required_file_refs 的权威来源；同一目标多个非空摘要不一致时标记冲突，不取先到者。
#   暂存摘要与已采用摘要是独立证据，在落地时另行核对，不参与这里的声明合并。
def _declared_files(params: object, agent=None) -> dict[Path, tuple[bool, str]]:
    base = _declared_base(getattr(params, "write_boundary", None))
    child = getattr(params, "context_scope", "") in _CHILD_SCOPES
    targets: dict[Path, tuple[bool, str]] = {}
    for source in _sources(params, agent):
        for key in ("required_file_refs", *(_LEGACY_CHILD_KEYS if child else ())):
            items = source.get(key)
            for item in items if isinstance(items, (list, tuple)) else ():
                raw = item.get("path") if isinstance(item, dict) else item
                if (path := _declared_path(raw, base)) is None:
                    continue
                # 旧产物字段可能混有逻辑名，无可信基准时沿原规则忽略；只有显式 required_file_refs 保留为未解析义务。
                if key != "required_file_refs" and not path.is_absolute():
                    continue
                if key == "required_file_refs":
                    digest = str((item.get("sha256") if isinstance(item, dict) else "") or "")
                    known = targets.get(path, (True, ""))[1]
                    if digest and known and digest != known:
                        digest = _CONFLICTING_DIGESTS
                    targets[path] = (True, digest or known)
                else:
                    targets.setdefault(path, (False, ""))
    return targets


def declared_deliverables(params: object) -> list[str]:
    return list(map(str, _declared_files(params)))


def _file_matches(path: Path, *, regular: bool, digest: str, workspace, boundary) -> bool:
    return _file_state(path, regular=regular, digest=digest, workspace=workspace, boundary=boundary) == "match"


# LLM: 只读核验：unresolved（无可信基准的相对声明）、digest_conflict（权威摘要不一致）、missing、
#   mismatch（存在但摘要不符）、unverified（读不了或不在受理根下）与 match 互相区分。
# 函数用途: 返回一个声明交付文件的核验状态。
def _file_state(path: Path, *, regular: bool, digest: str, workspace, boundary) -> str:
    if not path.is_absolute():
        return "unresolved"
    if digest == _CONFLICTING_DIGESTS:
        return "digest_conflict"
    try:
        if not regular:
            return "match" if path.exists() else "missing"
        # 只读已受理根下的显式声明，历史无快照的任务沿用绝对路径。
        roots = (workspace.root, *workspace.roots) if workspace is not None else (path.parent,)
        root = next((entry for entry in roots if path != entry and path.is_relative_to(entry)), None)
        if workspace is not None:
            if not workspace.is_current():
                return "unverified"
            if root is not None and workspace.identity_for(root) is None:
                return "unverified"
            if root is None and isinstance(boundary, dict):
                output = boundary.get("task_output_dir")
                if isinstance(output, str):
                    output_root = Path(output)
                    if output_root.is_absolute() and path != output_root and path.is_relative_to(output_root):
                        root = output_root
        if root is None:
            return "unverified"
        descriptor = open_workspace_delivery_file(root, path.relative_to(root).parts)
        with os.fdopen(descriptor, "rb") as stream:
            return "match" if not digest or file_digest(stream, "sha256").hexdigest() == digest else "mismatch"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unverified"


def missing_deliverables(params: object) -> tuple[list[str], list[str]]:
    targets = _declared_files(params)
    boundary = getattr(params, "write_boundary", None)
    workspace = workspace_ref_from_boundary(boundary)
    return list(map(str, targets)), [
        str(path) for path, (regular, digest) in targets.items()
        if not _file_matches(path, regular=regular, digest=digest, workspace=workspace, boundary=boundary)
    ]


def _read_run(agent, run_id):
    manager = getattr(agent, "subagents", None)
    if manager is None or not isinstance(run_id, str) or not run_id:
        return None
    try:
        return manager.load(run_id)
    except (FileNotFoundError, ValueError, KeyError):
        return None


def _apply_staged_version(agent, params, phase, target, declared_digest) -> dict[str, object]:
    receipt = {"version_ref": phase.get("version_ref", ""), "source_digest": "",
               "destination": str(target), "landed_digest": "", "state": "manifest_mismatch"}
    child = _read_run(agent, phase.get("child_run_id"))
    owner_id = getattr(getattr(agent, "home_paths", None), "owner_id", None)
    if (child is None or not owner_id or child.owner != owner_id
            or child.parent_id not in (params.run_id, getattr(params, "request_id", None))):
        return receipt
    staging = Path(child.output_dir)
    staged_ref = phase.get("staging_ref")
    if not isinstance(staged_ref, str):
        return receipt
    source = Path(staged_ref)
    if not source.is_relative_to(staging):
        return receipt
    relative = source.relative_to(staging).parts
    # 有正式子任务清单时必须与原写入位置绑定；旧任务仍必须位于其规范 output。
    staging_locations = [row for row in anchored_output_refs(child).warnings if "staging_ref" in row]
    if staging_locations and not any(row["target_path"] == str(target) and row["staging_ref"] == staged_ref
                                     for row in staging_locations):
        return receipt
    read_accepted = getattr(agent.tools.tools.get(_ACCEPT_TOOL), "accepted_workspace_version", None)
    if not callable(read_accepted) or any(not isinstance(phase.get(key), str) or not phase[key]
                                         for key in ("space_id", "asset_id", "version_ref")):
        return receipt
    try:
        accepted = read_accepted(space_id=phase["space_id"], asset_id=phase["asset_id"], version_ref=phase["version_ref"])
    except PermissionError as error:
        return {**receipt, "state": "adoption_unverified", "error_code": str(error)}
    if accepted is None:
        return {**receipt, "state": "not_accepted"}
    digest, budget = accepted
    # 父任务声明摘要、已采用版本摘要、子任务暂存摘要三者独立；任一不一致都不写入、不算交付。
    if declared_digest == _CONFLICTING_DIGESTS:
        return {**receipt, "state": "digest_conflict", "accepted_digest": digest}
    if phase.get("sha256") != digest or (declared_digest and declared_digest != digest):
        return {**receipt, "state": "digest_mismatch", "accepted_digest": digest,
                **({"declared_digest": declared_digest} if declared_digest else {})}
    try:
        payload = read_bytes_beneath(staging, relative, max_bytes=budget)
    except (OSError, ValueError) as error:
        return {**receipt, "state": "source_unverified", "error_code": type(error).__name__}
    if payload is not None:
        receipt["source_digest"] = sha256(payload).hexdigest()
        if receipt["source_digest"] != digest:
            return {**receipt, "state": "digest_mismatch", "accepted_digest": digest}
    from ..tool_runtime_ledger import write_boundary_with_runtime_ledger

    boundary = write_boundary_with_runtime_ledger(agent, params)
    with bind_cancellation_token(getattr(params, "cancellation_token", None) or current_cancellation_token()):
        if cancellation_requested():
            return {**receipt, "state": "cancelled"}
        # 暂存已被清理时 payload=None：目标已持有采用摘要则只读确认 already_landed，缺失才需要暂存字节。
        state, landed, code = land_accepted_workspace_file(agent.tools, boundary, target, payload, max_bytes=budget,
                                                           accepted_digest=digest)
    receipt.update(state=state, landed_digest=landed)
    if code:
        receipt["error_code"] = code
    return receipt


def _apply_receipts(params, agent, targets, adoption=None):
    seen: set[Path] = set()
    for source in _sources(params, agent):
        phases = source.get("staged_deliverables")
        for phase in phases if isinstance(phases, (list, tuple)) else ():
            if not isinstance(phase, dict):
                continue
            if adoption is not None and any(phase.get(key) != adoption.get(key)
                                            for key in ("space_id", "asset_id", "version_ref")):
                continue
            target = _declared_path(phase.get("destination"), None)
            if target in targets and target.is_absolute() and targets[target][0] and target not in seen:
                seen.add(target)
                yield _apply_staged_version(agent, params, phase, target, targets[target][1])


def _attach_apply_receipts(params, receipts):
    pending = {row["version_ref"]: [] for row in receipts}
    for row in receipts:
        pending[row["version_ref"]].append(row)
    for record in reversed(getattr(params, "archive_tool_calls", ()) or ()):
        if not isinstance(record, dict) or record.get("tool") != _ACCEPT_TOOL:
            continue
        arguments = record.get("parameters")
        arguments = arguments.get("params") if isinstance(arguments, dict) else None
        if isinstance(arguments, dict) and (rows := pending.pop(arguments.get("version_ref"), None)):
            envelope = record.setdefault("tool_result_envelope", {})
            envelope.setdefault("landing", rows)
        if not pending:
            break


def deliverable_report(params: object) -> dict[str, object] | None:
    state = getattr(params, "live_archive_state", None)
    return state.get(_REPORT_KEY) if isinstance(state, dict) else None


def adopted_delivery_receipts(agent, params, adoption: dict) -> list[dict[str, object]]:
    """原审批工具成功后投影落地回执；无回执时由同一收口入口重核。"""
    if getattr(params, "context_scope", "") in _CHILD_SCOPES or current_subagent_run_id(agent):
        return []
    workspace = workspace_ref_from_boundary(getattr(params, "write_boundary", None))
    return list(_apply_receipts(params, agent, _declared_files(params, agent), adoption)) if workspace else []


def deliverable_closeout_block(params: object, *, agent=None) -> str | None:
    targets = _declared_files(params, agent)
    if not targets:
        return None
    child_scope = getattr(params, "context_scope", "") in _CHILD_SCOPES
    state = getattr(params, "live_archive_state", None)
    boundary = getattr(params, "write_boundary", None)
    workspace = workspace_ref_from_boundary(boundary)
    child_run = _read_run(agent, getattr(params, "run_id", "")) if child_scope and agent is not None else None
    staged = staged_output_manifest(child_run) if child_run is not None else []
    staged_by_target = {row["destination"]: row for row in staged}
    receipts = []
    if agent is not None and not child_scope and not current_subagent_run_id(agent) and workspace is not None:
        receipts = list(_apply_receipts(params, agent, targets))
    receipts_by_target = {row["destination"]: row for row in receipts}
    landed = []
    missing = []
    # 每个未交付目标的具体原因（missing/mismatch/unresolved/digest_conflict/unverified 或落地回执状态）。
    outcomes: dict[str, str] = {}
    for path, (regular, digest) in targets.items():
        dest = str(path)
        receipt = receipts_by_target.get(dest)
        if receipt is not None and receipt["state"] not in {"landed", "already_landed"}:
            outcomes[dest] = receipt["state"]
        elif dest in staged_by_target:
            row = staged_by_target[dest]
            if not row["files_written"]:
                outcomes[dest] = "missing"
            elif digest == _CONFLICTING_DIGESTS or (digest and row["sha256"] != digest):
                outcomes[dest] = "digest_conflict" if digest == _CONFLICTING_DIGESTS else "mismatch"
        else:
            verdict = _file_state(path, regular=regular, digest=digest or (receipt or {}).get("landed_digest", ""),
                                  workspace=workspace, boundary=boundary)
            if verdict == "match":
                landed.append(dest)
            else:
                outcomes[dest] = verdict
        if dest in outcomes:
            missing.append(dest)
    if isinstance(state, dict):
        state[_REPORT_KEY] = {
            "files_written": bool(landed or any(row["files_written"] for row in staged)
                                  or any(row["source_digest"] for row in receipts)),
            "target_delivered": not missing and not staged,
            "missing": missing, "landed_files": landed, "staged_files": staged, "apply_receipts": receipts,
            "file_outcomes": outcomes,
        }
    if receipts:
        _attach_apply_receipts(params, receipts)
    if not missing or not isinstance(state, dict):
        return None
    attempts = int(state.get(_STATE_KEY) or 0)
    if attempts >= _MAX_DELIVERABLE_REPAIR_COUNT:
        return None
    state[_STATE_KEY] = attempts + 1
    preview = "、".join(f"{path}（{outcomes[path]}）" for path in missing[:_MISSING_PREVIEW_COUNT])
    suffix = "" if len(missing) <= _MISSING_PREVIEW_COUNT else f" 等 {len(missing)} 个"
    return (
        "[deliverable-closeout-blocked]\n"
        f"宿主声明的交付物有 {len(missing)} 个未通过落盘核验：{preview}{suffix}。\n"
        "请用文件工具写入声明位置并读回；如子任务须暂存，先写入暂存清单的位置。"
        "父任务需采用原资产精确版本，遇到落点冲突须报告现有文件。"
        f"（本次是第 {attempts + 1}/{_MAX_DELIVERABLE_REPAIR_COUNT} 次阻断，超限后按未交付收口。）"
    )


__all__ = ["declared_deliverables", "deliverable_closeout_block", "deliverable_report", "missing_deliverables"]
