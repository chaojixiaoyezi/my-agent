"""E1a：主上下文快照每回合只写一次（逐字节一致 / 写入次数 / 不读回 / artifacts 原子写）。

测试方法：
- 逐字节一致：按旧实现的渲染/定稿调用顺序在内存里重放（不落盘），得到旧实现的最终
  prompt_section、bundle 与四个快照文件的期望字节；再跑新实现逐字节比对（≥3 种输入）。
- 写入次数：用 sys.addaudithook 捕获 os.replace，断言一个回合里每个快照文件恰好替换 1 次。
- 不读回：监视 Path.read_text，断言新实现不再把刚写的 json/md 读回来刷 latest。
- artifacts：更新走私有原子写；replace 失败时原文件不受损；无匹配产物时不更新（语义不变）。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import date
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.user_space import context_bundle as context_bundle_module
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from agent_py_agent.agent.user_space.context_bundle_contracts import (
    finalize_context_bundle_contracts,
)
from agent_py_agent.agent.user_space.context_bundle_rendering import (
    render_markdown_bundle,
    render_prompt_section,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    runtime_snapshot_for_model_specs,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


# LLM: sys.addaudithook 安装后不能卸载，用模块级开关控制窗口；os.replace 与 os.rename 共用
#   "os.rename" 审计事件，只记录窗口内的替换目标。
class _RenameTrace:
    """记录窗口内 os.rename/os.replace 事件的（源、目标）对。"""

    def __init__(self) -> None:
        self.active = False
        self.events: list[tuple[str, str]] = []


_TRACE = _RenameTrace()


def _rename_trace_hook(event: str, args: tuple) -> None:
    if event != "os.rename" or not _TRACE.active:
        return
    _TRACE.events.append((str(args[0]), str(args[1])))


sys.addaudithook(_rename_trace_hook)


def _request_basic(tmp_path: Path) -> MainContextBundleRequest:
    root = tmp_path / "workspace-basic"
    root.mkdir()
    return MainContextBundleRequest(
        root=root,
        home_paths=ensure_my_agent_home(tmp_path / "home-basic"),
        user_prompt="基础请求：生成一个清单",
        request_id="bundle-basic",
        run_id="run-basic",
        task_id="task-basic",
        created_at="2026-10-07T10:00:00+00:00",
    )


def _rich_snapshot():
    specs = (
        make_test_model_spec(
            "read_file",
            category="filesystem",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
    )
    return runtime_snapshot_for_model_specs(
        specs, run_id="run-rich", allowed_tools=["read_file"]
    )


def _request_rich(tmp_path: Path) -> MainContextBundleRequest:
    root = tmp_path / "workspace-rich"
    root.mkdir()
    return MainContextBundleRequest(
        root=root,
        home_paths=ensure_my_agent_home(tmp_path / "home-rich"),
        user_prompt="丰富字段请求",
        request_id="bundle-rich",
        run_id="run-rich",
        task_id="task-rich",
        source="gateway",
        context_scope="rich",
        memory_count=3,
        runtime_injection_count=2,
        routed_required_read_paths=("docs/plan.md",),
        routed_candidate_paths=("docs/notes.md", "docs/log.txt"),
        recalled_refs=(
            {"entry_id": "memory-1", "kind": "fact", "origin": "user_explicit"},
            {"entry_id": "memory-2", "kind": "event", "origin": "tool_verified"},
        ),
        recall_findings=("hit_primary",),
        resume_context_injected=True,
        task_attributes={
            "acceptance": ["有登录"],
            "constraints": ["单文件"],
            "latest_tests": ["点击不报错"],
        },
        workspace_roots=(str(root), str(tmp_path / "shared-rich")),
        write_boundary={
            "allowed_write_roots": [str(root / "outputs")],
            "forbidden_write_roots": [str(root / "system")],
            "locked_files": [str(root / "README.md")],
        },
        tool_runtime_snapshot=_rich_snapshot(),
        tool_runtime_errors=({"context": "probe", "category": "data_parse"},),
        artifact_refs=(str(root / "outputs" / "index.html"),),
        owner_type="main_agent",
        owner_id="root",
        root_run_id="run-rich",
        parent_run_id="run-parent",
        created_at="2026-10-07T11:00:00+00:00",
    )


def _request_special_chars(tmp_path: Path) -> MainContextBundleRequest:
    root = tmp_path / "workspace-special"
    root.mkdir()
    return MainContextBundleRequest(
        root=root,
        home_paths=ensure_my_agent_home(tmp_path / "home-special"),
        user_prompt="多行请求：\n第一行 \"引号\" \nemoji 🚀 和制表符\t结束",
        request_id="bundle-special-字符-🚀",
        run_id="run-special",
        task_id="task-special",
        created_at="2026-10-07T12:00:00+00:00",
    )


def _request_save_false(tmp_path: Path) -> MainContextBundleRequest:
    root = tmp_path / "workspace-unsaved"
    root.mkdir()
    return MainContextBundleRequest(
        root=root,
        home_paths=ensure_my_agent_home(tmp_path / "home-unsaved"),
        user_prompt="只构造不落盘",
        request_id="bundle-unsaved",
        run_id="run-unsaved",
        task_id="task-unsaved",
        save=False,
        created_at="2026-10-07T13:00:00+00:00",
    )


def _legacy_expected_outputs(request: MainContextBundleRequest) -> dict[str, object]:
    """按旧实现的调用顺序在内存里重放渲染/定稿（不落盘），得到旧实现的最终输出。

    旧顺序：render("") + finalize 两轮 → （写）→ render(带路径) + finalize → （重写）→
    render(带路径)。写盘不改变内存值，所以最终落盘字节 = 第二次带路径 finalize 后的
    bundle 序列化；最终 prompt_section = 最后一次 render 的结果。
    """
    bundle = context_bundle_module._bundle_payload(request)
    prompt_section = render_prompt_section(bundle, json_path="")
    bundle = finalize_context_bundle_contracts(bundle, prompt_section)
    prompt_section = render_prompt_section(bundle, json_path="")
    bundle = finalize_context_bundle_contracts(bundle, prompt_section)
    expected: dict[str, object] = {
        "prompt_section": prompt_section,
        "bundle": bundle,
        "json_path": None,
        "markdown_path": None,
        "json_text": None,
        "markdown_text": None,
    }
    if not request.save or request.home_paths is None:
        return expected
    archive = context_bundle_module._memory_archive_root(request.home_paths)
    base = Path(
        os.path.abspath(archive / "snapshots" / "context_bundles" / date.today().isoformat())
    )
    stem = context_bundle_module._bundle_stem(request)
    json_path = base / f"{stem}.json"
    markdown_path = base / f"{stem}.md"
    prompt_section = render_prompt_section(bundle, json_path=str(json_path))
    bundle = finalize_context_bundle_contracts(bundle, prompt_section)
    prompt_section = render_prompt_section(bundle, json_path=str(json_path))
    expected.update(
        {
            "prompt_section": prompt_section,
            "bundle": bundle,
            "json_path": json_path,
            "markdown_path": markdown_path,
            "json_text": json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
            "markdown_text": render_markdown_bundle(bundle, json_path=str(json_path)),
        }
    )
    return expected


@pytest.mark.parametrize(
    "factory",
    [_request_basic, _request_rich, _request_special_chars, _request_save_false],
    ids=["basic", "rich", "special_chars", "save_false"],
)
def test_snapshot_outputs_match_legacy_order_byte_for_byte(tmp_path: Path, factory) -> None:
    request = factory(tmp_path)
    expected = _legacy_expected_outputs(request)
    result = build_main_context_bundle(request)

    assert result.prompt_section == expected["prompt_section"]
    assert result.bundle == expected["bundle"]
    if expected["json_path"] is None:
        assert result.json_path == ""
        assert result.markdown_path == ""
        return
    json_path = Path(result.json_path)
    markdown_path = Path(result.markdown_path)
    expected_json = str(expected["json_text"]).encode("utf-8")
    expected_markdown = str(expected["markdown_text"]).encode("utf-8")
    assert json_path == expected["json_path"]
    assert markdown_path == expected["markdown_path"]
    assert json_path.read_bytes() == expected_json
    assert markdown_path.read_bytes() == expected_markdown
    assert (json_path.parent / "latest_context_bundle.json").read_bytes() == expected_json
    assert (json_path.parent / "latest_context_bundle.md").read_bytes() == expected_markdown


def test_each_snapshot_file_replaced_once_per_turn(tmp_path: Path) -> None:
    request = _request_basic(tmp_path)
    _TRACE.events.clear()
    _TRACE.active = True
    try:
        result = build_main_context_bundle(request)
    finally:
        _TRACE.active = False

    base = Path(result.json_path).parent
    targets = {
        str(Path(result.json_path)): 0,
        str(Path(result.markdown_path)): 0,
        str(base / "latest_context_bundle.json"): 0,
        str(base / "latest_context_bundle.md"): 0,
    }
    for _src, dst in _TRACE.events:
        if dst in targets:
            targets[dst] += 1
    assert targets == dict.fromkeys(targets, 1)
    snapshot_dir = f"{base}{os.sep}"
    dir_events = [pair for pair in _TRACE.events if pair[1].startswith(snapshot_dir)]
    assert len(dir_events) == 4


def test_latest_copies_do_not_read_back_written_files(tmp_path: Path, monkeypatch) -> None:
    request = _request_basic(tmp_path)
    reads: list[str] = []
    real_read_text = Path.read_text

    def spy(self, *args, **kwargs):
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    result = build_main_context_bundle(request)
    monkeypatch.undo()

    assert str(Path(result.json_path)) not in reads
    assert str(Path(result.markdown_path)) not in reads


def _artifact_ready_bundle(tmp_path: Path):
    """构造一份已落盘快照 + 一条匹配的工具产物，供 artifacts 更新测试使用。"""
    root = tmp_path / "workspace-artifact"
    root.mkdir()
    home_paths = ensure_my_agent_home(tmp_path / "home-artifact")
    scope = {"request_id": "request-artifact", "run_id": "run-artifact", "task_id": "task-artifact"}
    bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=home_paths,
            user_prompt="读取大日志后继续分析",
            save=True,
            created_at="2026-10-07T14:00:00+00:00",
            **scope,
        )
    )
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="read_file",
            call_id="1-1",
            output="large output\n" + ("x" * 25_000),
            ok=True,
            # 默认 externalize 阈值 200K，25K 输出不会落 artifact——显式 min_chars=1 强制走恢复产物路径。
            min_chars=1,
            **scope,
        )
    )
    return root, bundle, scope


@_POSIX_ONLY
def test_artifact_update_writes_private_files_and_refreshes_latest(tmp_path: Path) -> None:
    root, bundle, scope = _artifact_ready_bundle(tmp_path)
    json_path = Path(bundle.json_path)
    latest = json_path.parent / "latest_context_bundle.json"
    os.chmod(json_path, 0o644)
    os.chmod(latest, 0o644)

    update = update_main_context_bundle_artifacts(
        MainContextBundleArtifactUpdateRequest(
            context_bundle_path=bundle.json_path,
            workspace_root=root,
            **scope,
        )
    )

    assert update["status"] == "updated"
    assert stat.S_IMODE(json_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    assert json_path.read_bytes() == latest.read_bytes()


@_POSIX_ONLY
def test_artifact_update_failure_keeps_original_files(tmp_path: Path, monkeypatch) -> None:
    root, bundle, scope = _artifact_ready_bundle(tmp_path)
    json_path = Path(bundle.json_path)
    latest = json_path.parent / "latest_context_bundle.json"
    before_json = json_path.read_bytes()
    before_latest = latest.read_bytes()

    def boom(src, dst, *args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    try:
        with pytest.raises(OSError):
            update_main_context_bundle_artifacts(
                MainContextBundleArtifactUpdateRequest(
                    context_bundle_path=bundle.json_path,
                    workspace_root=root,
                    **scope,
                )
            )
    finally:
        monkeypatch.undo()

    assert json_path.read_bytes() == before_json
    assert latest.read_bytes() == before_latest


def test_artifact_update_skips_without_matching_outputs(tmp_path: Path) -> None:
    root = tmp_path / "workspace-artifact-empty"
    root.mkdir()
    home_paths = ensure_my_agent_home(tmp_path / "home-artifact-empty")
    bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=home_paths,
            user_prompt="没有工具产物",
            request_id="request-artifact-empty",
            run_id="run-artifact-empty",
            task_id="task-artifact-empty",
            save=True,
            created_at="2026-10-07T15:00:00+00:00",
        )
    )
    json_path = Path(bundle.json_path)
    before = json_path.read_bytes()

    update = update_main_context_bundle_artifacts(
        MainContextBundleArtifactUpdateRequest(
            context_bundle_path=bundle.json_path,
            workspace_root=root,
            request_id="request-artifact-empty",
            run_id="run-artifact-empty",
            task_id="task-artifact-empty",
        )
    )

    assert update["status"] == "no_matching_tool_output_artifacts"
    assert json_path.read_bytes() == before


def test_artifact_update_reads_bundle_once(tmp_path: Path, monkeypatch) -> None:
    root, bundle, scope = _artifact_ready_bundle(tmp_path)
    json_path = Path(bundle.json_path)
    reads: list[str] = []
    real_read_text = Path.read_text

    def spy(self, *args, **kwargs):
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    update = update_main_context_bundle_artifacts(
        MainContextBundleArtifactUpdateRequest(
            context_bundle_path=bundle.json_path,
            workspace_root=root,
            **scope,
        )
    )
    monkeypatch.undo()

    assert update["status"] == "updated"
    # 更新时只读一次源 JSON（读取校验）；latest 不再把刚写的文件读回来。
    assert reads.count(str(json_path)) == 1
