"""用户项目与真实工作区目录的绑定。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..tooling.registry_workspace import WorkspaceRef, freeze_workspace_ref
from .scope_contract import canonical_scope_key


def _path(owner_home: Path) -> Path:
    return owner_home / "memory" / "project_bindings.json"


def _read(path: Path) -> tuple[dict, dict]:
    report = read_json_object_report(path, context="memory.project_bindings")
    roots = report.payload.get("roots", {})
    aliases = report.payload.get("aliases", {})
    if report.load_error or not isinstance(roots, dict) or not isinstance(aliases, dict):
        raise ValueError("PROJECT_BINDINGS_UNREADABLE")
    return roots, aliases


def _generation(binding: dict | None) -> str:
    if binding is None:
        return ""
    if not isinstance(binding, dict):
        raise ValueError("PROJECT_BINDINGS_UNREADABLE")
    generation = binding.get("generation")
    if isinstance(generation, str) and generation:
        return generation
    # Existing registrations predate generations. Their exact row is the initial CAS basis.
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def generation(owner_home: Path, workspace_root: Path) -> str:
    roots, _ = _read(_path(owner_home))
    return _generation(roots.get(str(workspace_root)))


def record(
    owner_home: Path, project_key: str, workspace_root: Path, *, alias: str = "",
    expected_identity: tuple[int, int] | None = None,
    publication: tuple[str, str] | None = None,
) -> str:
    """Publish under the registry lock, optionally against a frozen generation."""
    project = canonical_scope_key("project", project_key)
    path = _path(owner_home)
    with locked_json_path(path):
        workspace = freeze_workspace_ref(workspace_root)
        if workspace is None or (expected_identity is not None and (
            workspace.root != workspace_root or workspace.root_identity != expected_identity
        )):
            return "superseded"
        roots, aliases = _read(path)
        root = str(workspace.root)
        previous = roots.get(root)
        if previous is not None and not isinstance(previous, dict):
            raise ValueError("PROJECT_BINDINGS_UNREADABLE")
        binding = {"project": project, "identity": list(workspace.root_identity)}
        previous_alias = previous.get("alias", "") if previous else ""
        if not isinstance(previous_alias, str):
            raise ValueError("PROJECT_BINDINGS_UNREADABLE")
        if publication is not None:
            basis, operation = publication
            current = _generation(previous)
            if current == operation:
                return "settled"
            if current != basis:
                return "superseded"
        same_binding = previous is not None and all(previous.get(key) == value for key, value in binding.items())
        requested_alias = alias.strip()
        selected_alias = requested_alias or (previous_alias if same_binding else "")
        if selected_alias:
            binding["alias"] = selected_alias
        if (publication is None and same_binding and previous_alias == selected_alias
                and (not requested_alias or aliases.get(selected_alias) == root)):
            return "settled"
        if previous_alias and aliases.get(previous_alias) == root:
            del aliases[previous_alias]
        if selected_alias:
            aliases[selected_alias] = root
        binding["generation"] = publication[1] if publication else uuid4().hex
        roots[root] = binding
        write_json_file_atomic_unlocked(path, {"roots": roots, "aliases": aliases})
        return "settled"


def lookup(owner_home: Path, workspace_root: Path) -> tuple[str, ...]:
    """查最近的注册祖先，目录失效时关闭该绑定。"""
    project, _ = resolve(owner_home, workspace_root)
    return (project,) if project else ()


def resolve(
    owner_home: Path, workspace_root: Path | None, *, aliases: set[str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """从同一原子快照返回目录绑定，或在无目录匹配时返回查询别名。"""
    try:
        roots, alias_index = _read(_path(owner_home))
    except ValueError:
        logging.getLogger(__name__).warning("PROJECT_BINDINGS_UNREADABLE")
        return "", ()
    workspace = freeze_workspace_ref(workspace_root) if roots else None
    if workspace is not None:
        for root in (workspace.root, *workspace.root.parents):
            key = str(root)
            if key in roots:
                ref = workspace if root == workspace.root else freeze_workspace_ref(root)
                if project := _current_project(roots[key], ref):
                    return project, ()
                break
    projects = []
    for alias in sorted(aliases or ()):
        root = alias_index.get(alias)
        binding = roots.get(root) if isinstance(root, str) else None
        if isinstance(binding, dict) and binding.get("alias") == alias:
            ref = freeze_workspace_ref(root)
            if ref is not None and str(ref.root) == root:
                if project := _current_project(binding, ref):
                    projects.append(project)
    return "", tuple(dict.fromkeys(projects))


def _current_project(binding: object, workspace: WorkspaceRef | None) -> str:
    if (workspace is None or not isinstance(binding, dict)
            or binding.get("identity") != list(workspace.root_identity)):
        return ""
    project = binding.get("project")
    if not isinstance(project, str):
        return ""
    try:
        return project if canonical_scope_key("project", project) == project else ""
    except ValueError:
        return ""
