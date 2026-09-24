from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_runtime import PluginProxyTool
from agent_py_agent.agent.tooling.mcp_client import MCPError
from agent_py_agent.agent.tooling.workspace_write_scope import build_workspace_write_context
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.agent.workspace_write_context import (
    WORKSPACE_WRITE_EXTENSION,
    WorkspaceWriteContext,
)

_NAMES = ("a", "b", "c", "out", "output.json")


def _random_rel(rng: random.Random) -> str:
    parts = [rng.choice(_NAMES) for _ in range(rng.randint(1, 3))]
    if rng.random() < 0.1:
        parts.insert(0, "..")
    return "/".join(parts)


def _random_boundary(rng: random.Random, cwd: Path) -> dict | None:
    if rng.random() < 0.2:
        return None

    def some(k):
        return [(_random_rel(rng) if rng.random() < 0.7 else str(cwd / _random_rel(rng))) for _ in range(k)]

    boundary: dict = {}
    if rng.random() < 0.85:
        boundary["allowed_write_roots"] = some(rng.randint(0, 3))
    if rng.random() < 0.5:
        boundary["forbidden_write_roots"] = some(rng.randint(1, 2))
    if rng.random() < 0.4:
        boundary["locked_files"] = some(rng.randint(1, 2))
    if rng.random() < 0.3:
        boundary["output_json"] = rng.choice(["out/output.json", "a/output.json"])
        boundary["product_write_roots"] = some(rng.randint(1, 2))
    if rng.random() < 0.1:
        boundary["runtime_audit"] = {"run_id": "r"}  # 纯账本字段不构成写入范围
    return boundary


def test_plugin_write_decision_equals_host_decision_within_write_roots(tmp_path):
    rng = random.Random(20260924)
    cwd = (tmp_path / "ws").resolve()
    cwd.mkdir()
    dangerous = [str(cwd / "a" / "b")]
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=dangerous)
    checked = allowed = 0
    for _ in range(2000):
        boundary = _random_boundary(rng, cwd)
        context = build_workspace_write_context(cwd=cwd, write_boundary=boundary, path_policy=policy)
        restored = WorkspaceWriteContext.from_payload(context.to_payload())
        assert restored == context
        for _ in range(4):
            raw = _random_rel(rng) if rng.random() < 0.8 else str(cwd / _random_rel(rng))
            host_ok = validate_write_boundary(
                "write_file", {"path": raw}, workspace_root=cwd, path_access_mode="normal",
                path_dangerous_roots=dangerous, write_boundary=boundary if boundary is not None else {},
            ) == ""
            target = (cwd / raw).resolve(strict=False)
            under = any(target.is_relative_to(root) for root in context.write_roots)
            plugin_ok = context.check(raw).allowed
            assert plugin_ok == (host_ok and under), (boundary, raw, host_ok, under, plugin_ok)
            assert restored.check(raw).allowed == plugin_ok
            checked += 1
            allowed += plugin_ok
    # 样本必须同时覆盖允许与拒绝两类，防止生成器退化
    assert checked == 8000 and 500 < allowed < 7500


def test_without_scope_plugin_is_limited_to_cwd_even_where_host_tool_is_not(tmp_path):
    cwd = (tmp_path / "ws").resolve()
    cwd.mkdir()
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[])
    context = build_workspace_write_context(cwd=cwd, write_boundary=None, path_policy=policy)
    sibling = tmp_path / "other" / "x.txt"
    assert validate_write_boundary("write_file", {"path": str(sibling)}, workspace_root=cwd,
                                   write_boundary={}) == ""
    assert not context.check(str(sibling)).allowed
    assert context.check("new/file.txt").allowed


def test_owner_wall_in_policy_is_enforced(tmp_path):
    cwd = (tmp_path / "owner" / "ws").resolve()
    cwd.mkdir(parents=True)
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[], owner_scope_root=str(tmp_path / "owner"))
    context = build_workspace_write_context(cwd=cwd, write_boundary={"allowed_write_roots": [str(tmp_path)]},
                                            path_policy=policy)
    assert not context.check(str(tmp_path / "elsewhere.txt")).allowed
    assert context.check("ok.txt").allowed


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("write_roots"),
    lambda p: p.update(version="2"),
    lambda p: p.update(write_roots=["relative/path"]),
    lambda p: p.update(extra=1),
])
def test_payload_is_strict(tmp_path, mutate):
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[])
    payload = build_workspace_write_context(cwd=tmp_path, write_boundary=None, path_policy=policy).to_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        WorkspaceWriteContext.from_payload(payload)


def test_anchor_picks_most_specific_root(tmp_path):
    cwd = tmp_path.resolve()
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[])
    context = build_workspace_write_context(cwd=cwd, write_boundary={"allowed_write_roots": [".", "a"]},
                                            path_policy=policy)
    assert context.anchor("a/b/c.txt") == (cwd / "a", ("b", "c.txt"))


# ── 插件代理只在协商且声明写效果时下发写入上下文 ─────────────────────


def _proxy(effect: str, extensions: dict):
    tool = SimpleNamespace(name="save", requested_effect=effect)
    client = SimpleNamespace(installation=SimpleNamespace(manifest=SimpleNamespace(tools=(tool,))))
    proxy = PluginProxyTool.__new__(PluginProxyTool)
    proxy.client, proxy.remote_tool = client, "save"
    proxy.transport = SimpleNamespace(capabilities={"experimental": extensions})
    return proxy


def _context(tmp_path, *, write=True):
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[])
    return SimpleNamespace(
        workspace_read_context=SimpleNamespace(to_payload=lambda: {"read": 1}),
        workspace_write_context=build_workspace_write_context(cwd=tmp_path, write_boundary=None, path_policy=policy)
        if write else None,
    )


def test_write_meta_only_for_negotiated_mutating_tools(tmp_path):
    both = {WORKSPACE_READ_EXTENSION: {"versions": ["1"]}, WORKSPACE_WRITE_EXTENSION: {"versions": ["1"]}}
    meta = _proxy("mutating", both)._request_meta(_context(tmp_path))
    assert set(meta) == {WORKSPACE_READ_EXTENSION, WORKSPACE_WRITE_EXTENSION}
    assert set(_proxy("read_only", both)._request_meta(_context(tmp_path))) == {WORKSPACE_READ_EXTENSION}
    read_only_plugin = {WORKSPACE_READ_EXTENSION: {"versions": ["1"]}}
    assert set(_proxy("mutating", read_only_plugin)._request_meta(_context(tmp_path))) == {WORKSPACE_READ_EXTENSION}
    assert _proxy("mutating", {})._request_meta(_context(tmp_path)) is None


def test_missing_write_context_fails_before_send(tmp_path):
    both = {WORKSPACE_WRITE_EXTENSION: {"versions": ["1"]}}
    with pytest.raises(MCPError) as exc:
        _proxy("dangerous", both)._request_meta(_context(tmp_path, write=False))
    assert exc.value.effect_outcome == "not_started"
