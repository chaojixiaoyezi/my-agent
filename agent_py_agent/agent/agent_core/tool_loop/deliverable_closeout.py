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


def _sources(params: object, agent=None):
    current = getattr(agent, "_current_run_params", None)
    binder = getattr(current, "conversation_task_binding_callback", None)
    load_contract = getattr(binder, "staged_delivery_contract", None)
    host_contract = load_contract() if callable(load_contract) else None
    for source in (host_contract, getattr(params, "delivery_contract", None),
                   getattr(params, "task_attributes", None), getattr(params, "live_archive_state", None)):
        if isinstance(source, dict):
            yield source


def _declared_path(raw, workspace) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw or "://" in raw:
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        if workspace is None:
            return None
        path = workspace.root / path
    return Path(os.path.abspath(path))


def _declared_files(params: object, agent=None) -> dict[Path, tuple[bool, str]]:
    workspace = workspace_ref_from_boundary(getattr(params, "write_boundary", None))
    child = getattr(params, "context_scope", "") in _CHILD_SCOPES
    targets: dict[Path, tuple[bool, str]] = {}
    for source in _sources(params, agent):
        for key in ("required_file_refs", *(_LEGACY_CHILD_KEYS if child else ())):
            items = source.get(key)
            for item in items if isinstance(items, (list, tuple)) else ():
                raw = item.get("path") if isinstance(item, dict) else item
                if (path := _declared_path(raw, workspace)) is None:
                    continue
                if key == "required_file_refs":
                    digest = item.get("sha256") if isinstance(item, dict) else ""
                    targets.setdefault(path, (True, ""))
                    targets[path] = (True, targets[path][1] or str(digest or ""))
                else:
                    targets.setdefault(path, (False, ""))
    return targets


def declared_deliverables(params: object) -> list[str]:
    return list(map(str, _declared_files(params)))


def _file_matches(path: Path, *, regular: bool, digest: str, workspace, boundary) -> bool:
    try:
        if not regular:
            return path.exists()
        # 只读已受理根下的显式声明，历史无快照的任务沿用绝对路径。
        roots = (workspace.root, *workspace.roots) if workspace is not None else (path.parent,)
        root = next((entry for entry in roots if path != entry and path.is_relative_to(entry)), None)
        if workspace is not None:
            if not workspace.is_current():
                return False
            if root is not None and workspace.identity_for(root) is None:
                return False
            if root is None and isinstance(boundary, dict):
                output = boundary.get("task_output_dir")
                if isinstance(output, str):
                    output_root = Path(output)
                    if output_root.is_absolute() and path != output_root and path.is_relative_to(output_root):
                        root = output_root
        if root is None:
            return False
        descriptor = open_workspace_delivery_file(root, path.relative_to(root).parts)
        with os.fdopen(descriptor, "rb") as stream:
            return not digest or file_digest(stream, "sha256").hexdigest() == digest
    except (OSError, ValueError):
        return False


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
    if phase.get("sha256") != digest or (declared_digest and declared_digest != digest):
        return {**receipt, "state": "digest_mismatch", "accepted_digest": digest}
    try:
        payload = read_bytes_beneath(staging, relative, max_bytes=budget)
    except (OSError, ValueError) as error:
        return {**receipt, "state": "source_unverified", "error_code": type(error).__name__}
    if payload is None:
        return {**receipt, "state": "source_missing"}
    receipt["source_digest"] = sha256(payload).hexdigest()
    if receipt["source_digest"] != digest:
        return {**receipt, "state": "digest_mismatch", "accepted_digest": digest}
    from ..tool_runtime_ledger import write_boundary_with_runtime_ledger

    boundary = write_boundary_with_runtime_ledger(agent, params)
    with bind_cancellation_token(getattr(params, "cancellation_token", None) or current_cancellation_token()):
        if cancellation_requested():
            return {**receipt, "state": "cancelled"}
        state, landed, code = land_accepted_workspace_file(agent.tools, boundary, target, payload, max_bytes=budget)
    receipt.update(state=state, landed_digest=landed)
    if code:
        receipt["error_code"] = code
    return receipt


def _apply_receipts(params, agent, targets, workspace, adoption=None):
    seen: set[Path] = set()
    for source in _sources(params, agent):
        phases = source.get("staged_deliverables")
        for phase in phases if isinstance(phases, (list, tuple)) else ():
            if not isinstance(phase, dict):
                continue
            if adoption is not None and any(phase.get(key) != adoption.get(key)
                                            for key in ("space_id", "asset_id", "version_ref")):
                continue
            target = _declared_path(phase.get("destination"), workspace)
            if target in targets and targets[target][0] and target not in seen:
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
    return list(_apply_receipts(params, agent, _declared_files(params, agent), workspace, adoption)) if workspace else []


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
        receipts = list(_apply_receipts(params, agent, targets, workspace))
    receipts_by_target = {row["destination"]: row for row in receipts}
    landed = []
    missing = []
    for path, (regular, digest) in targets.items():
        dest = str(path)
        receipt = receipts_by_target.get(dest)
        if receipt is not None and receipt["state"] not in {"landed", "already_landed"}:
            missing.append(dest)
        elif dest in staged_by_target:
            if not staged_by_target[dest]["files_written"] or (digest and staged_by_target[dest]["sha256"] != digest):
                missing.append(dest)
        elif _file_matches(path, regular=regular, digest=digest or (receipt or {}).get("landed_digest", ""),
                           workspace=workspace, boundary=boundary):
            landed.append(dest)
        else:
            missing.append(dest)
    if isinstance(state, dict):
        state[_REPORT_KEY] = {
            "files_written": bool(landed or any(row["files_written"] for row in staged)
                                  or any(row["source_digest"] for row in receipts)),
            "target_delivered": not missing and not staged,
            "missing": missing, "landed_files": landed, "staged_files": staged, "apply_receipts": receipts,
        }
    if receipts:
        _attach_apply_receipts(params, receipts)
    if not missing or not isinstance(state, dict):
        return None
    attempts = int(state.get(_STATE_KEY) or 0)
    if attempts >= _MAX_DELIVERABLE_REPAIR_COUNT:
        return None
    state[_STATE_KEY] = attempts + 1
    preview = "、".join(missing[:_MISSING_PREVIEW_COUNT])
    suffix = "" if len(missing) <= _MISSING_PREVIEW_COUNT else f" 等 {len(missing)} 个"
    return (
        "[deliverable-closeout-blocked]\n"
        f"宿主声明的交付物有 {len(missing)} 个未通过落盘核验：{preview}{suffix}。\n"
        "请用文件工具写入声明位置并读回；如子任务须暂存，先写入暂存清单的位置。"
        "父任务需采用原资产精确版本，遇到落点冲突须报告现有文件。"
        f"（本次是第 {attempts + 1}/{_MAX_DELIVERABLE_REPAIR_COUNT} 次阻断，超限后按未交付收口。）"
    )


__all__ = ["declared_deliverables", "deliverable_closeout_block", "deliverable_report", "missing_deliverables"]
