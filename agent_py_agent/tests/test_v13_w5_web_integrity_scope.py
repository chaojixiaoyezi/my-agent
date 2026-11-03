"""W5：写普通脚本不能把整个 owner home 当作网站。"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents.static_site import validator
from agent_py_agent.agent.tooling import web_project_integrity as web


@pytest.mark.parametrize("marker", ["runtime.db", "sessions", "workspace/runtime"])
def test_host_root_never_becomes_site_root(tmp_path, marker):
    marker_path = tmp_path / marker
    if marker == "runtime.db":
        marker_path.write_text("")
    else:
        marker_path.mkdir(parents=True)
    (tmp_path / "poster.html").write_text("<html>old poster</html>")
    script = tmp_path / "tasks/x/tmp/test.js"
    script.parent.mkdir(parents=True)
    script.write_text("console.log(1)")
    assert web._nearest_site_root(script, tmp_path) is None


def test_cjs_without_index_does_not_scan(tmp_path, monkeypatch):
    (tmp_path / "poster.html").write_text("<html>old poster</html>")
    script = tmp_path / "tasks/x/tmp/test.cjs"
    script.parent.mkdir(parents=True)
    script.write_text("console.log(1)")
    calls = []
    import agent_py_agent.agent.subagents.static_site as static_site
    monkeypatch.setattr(static_site, "run_static_site_check", lambda *args: calls.append(args))
    result = web.check_web_project_post_write(script, tmp_path)
    assert result.ok is True
    assert calls == []


def test_cjs_next_to_or_below_index_is_checked(tmp_path, monkeypatch):
    site = tmp_path / "site"
    script = site / "scripts/test.cjs"
    script.parent.mkdir(parents=True)
    script.write_text("console.log(1)")
    (site / "index.html").write_text("<html>website</html>")
    calls = []
    import agent_py_agent.agent.subagents.static_site as static_site

    def check(*args):
        calls.append(args)
        return SimpleNamespace(validation_result={})

    monkeypatch.setattr(static_site, "run_static_site_check", check)
    result = web.check_web_project_post_write(script, tmp_path)
    assert result.kind == "web_project"
    assert calls[0][0]["site_root"] == "site"


def test_workspace_boundary_cannot_be_crossed_for_index(tmp_path):
    (tmp_path / "index.html").write_text("<html>outside</html>")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "test.cjs"
    script.write_text("")
    assert web._nearest_site_root(script, workspace) is None


def test_static_site_prunes_runtime_before_scandir(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>")
    for name in ("local_store", "compact", "runs", "sessions", ".git", "__pycache__", "node_modules", "workspace/runtime"):
        root = tmp_path / name
        root.mkdir(parents=True)
        (root / "hidden.html").write_text("<a href='#'>ignored</a>")
    noise = tmp_path / "local_store"
    for number in range(10000):
        (noise / f"{number}.html").write_text("")
    original = os.scandir
    visited = []

    def observe(path):
        visited.append(Path(path).relative_to(tmp_path).as_posix())
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    record = validator.run_static_site_check({"site_root": "."}, tmp_path)
    assert record.passed is True
    assert record.validation_result["checked_files"] == ["index.html"]
    assert set(visited) <= {".", "workspace"}


def test_static_site_excludes_runtime_even_in_explicit_scope(tmp_path):
    hidden = tmp_path / "sessions/hidden.html"
    hidden.parent.mkdir()
    hidden.write_text("<a href='#'>not part of site</a>")
    (tmp_path / "index.html").write_text("<html>site</html>")
    record = validator.run_static_site_check({"site_root": ".", "html_files": ["sessions/hidden.html"]}, tmp_path)
    assert "sessions/hidden.html" not in record.validation_result["checked_files"]


def test_static_site_reports_truncation_and_never_passes_partial_scan(tmp_path):
    for number in range(3):
        (tmp_path / f"{number}.html").write_text("<html><body>ok</body></html>")
    record = validator.run_static_site_check({"site_root": ".", "max_files": 1}, tmp_path)
    assert record.validation_result["scan_incomplete"] is True
    assert record.passed is False
    assert len(record.validation_result["checked_files"]) == 1


def test_canonical_workspace_alias_still_locates_site(tmp_path):
    root = tmp_path / "real"
    site = root / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html>ok</html>")
    script = site / "test.cjs"
    script.write_text("")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert web._nearest_site_root(alias / "site/test.cjs", alias) == site


@pytest.mark.parametrize("owner_kind", ["users", "groups"])
def test_canonical_provider_owner_without_markers_is_not_site(tmp_path, owner_kind):
    owner = tmp_path / "owners/providers/demo" / owner_kind / "one"
    script = owner / "tasks/x/tmp/test.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    (owner / "poster.html").write_text("<html>poster</html>")
    assert web._nearest_site_root(script, owner) is None


@pytest.mark.parametrize("root", ["sessions", "runs/nested", "workspace/runtime", "workspace/runtime/site"])
@pytest.mark.parametrize("explicit", [False, True])
def test_runtime_directory_cannot_become_site_root(tmp_path, monkeypatch, root, explicit):
    site = tmp_path / root
    site.mkdir(parents=True)
    (site / "hidden.html").write_text("<html><body>hidden</body></html>")
    visited = []
    original = os.scandir

    def observe(path):
        visited.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    request = {"site_root": root}
    if explicit:
        request["html_files"] = ["hidden.html"]
    record = validator.run_static_site_check(request, tmp_path)
    assert record.passed is False
    assert record.validation_result["checked_files"] == []
    assert not visited


@pytest.mark.parametrize("scope", ["missing.html", "sessions/hidden.html", "../outside.html", "wrong.txt"])
def test_invalid_explicit_html_never_passes_as_empty_scope(tmp_path, scope):
    hidden = tmp_path / "sessions/hidden.html"
    hidden.parent.mkdir()
    hidden.write_text("<html>not site</html>")
    (tmp_path / "wrong.txt").write_text("not html")
    record = validator.run_static_site_check({"site_root": ".", "html_files": [scope]}, tmp_path)
    assert record.validation_result["checked_files"] == []
    assert record.validation_result["scan_incomplete"] is True
    assert record.passed is False
