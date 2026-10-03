from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import build_runtime_main_context_bundle
from agent_py_agent.agent.common.json_io import (
    PrivateDirectoryChainResult,
    ensure_private_directory_chain,
)
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    TurnTokenUsage,
    append_raw_event,
    append_session_token_usage,
    append_snapshot,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    _rewrite_bundle_files,
    _write_bundle_files,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from agent_py_agent.agent.user_space.context_bundle_rendering import render_markdown_bundle
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    runtime_snapshot_for_model_specs,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


@pytest.fixture
def open_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def _private_context_bundle_inputs(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    home_paths = ensure_my_agent_home(tmp_path / "home")
    (Path(home_paths.owner_home_dir) / "memory_archive").mkdir(mode=0o755)
    request = MainContextBundleRequest(
        root=root,
        home_paths=home_paths,
        user_prompt="包含会话上下文的私有快照",
        request_id="bundle-private",
        run_id="run-private",
        task_id="task-private",
        save=False,
        created_at="2026-10-03T12:00:00+00:00",
    )
    result = build_main_context_bundle(request)
    return request, result.bundle or {}, home_paths


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _expected_json_bytes(bundle: dict[str, object]) -> bytes:
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


@_POSIX_ONLY
def test_context_bundle_initial_writer_preserves_exact_bytes_before_rewrite(
    tmp_path: Path, open_umask
) -> None:
    request, bundle, home_paths = _private_context_bundle_inputs(tmp_path)
    archive = Path(home_paths.owner_home_dir) / "memory_archive"
    base = archive / "snapshots" / "context_bundles" / date.today().isoformat()
    directory_result = ensure_private_directory_chain(archive, base)
    json_path, markdown_path = _write_bundle_files(request, bundle, directory_result)
    latest_json = base / "latest_context_bundle.json"
    latest_markdown = base / "latest_context_bundle.md"
    expected_json = _expected_json_bytes(bundle)
    expected_markdown = render_markdown_bundle(bundle, json_path=str(json_path)).encode("utf-8")

    assert json_path.read_bytes() == expected_json
    assert markdown_path.read_bytes() == expected_markdown
    assert latest_json.read_bytes() == expected_json
    assert latest_markdown.read_bytes() == expected_markdown


@_POSIX_ONLY
def test_context_bundle_first_write_is_private_and_byte_stable(tmp_path: Path, open_umask) -> None:
    request, _bundle, home_paths = _private_context_bundle_inputs(tmp_path)
    archive = Path(home_paths.owner_home_dir) / "memory_archive"
    archive_mode = _mode(archive)
    owner_home_mode = _mode(archive.parent)

    result = build_main_context_bundle(replace(request, save=True))
    bundle = result.bundle or {}
    json_path = Path(result.json_path)
    markdown_path = Path(result.markdown_path)
    base = json_path.parent
    snapshots = archive / "snapshots"
    context_bundles = snapshots / "context_bundles"
    latest_json = base / "latest_context_bundle.json"
    latest_markdown = base / "latest_context_bundle.md"
    expected_json = _expected_json_bytes(bundle)
    expected_markdown = render_markdown_bundle(bundle, json_path=str(json_path)).encode("utf-8")

    assert json_path.name == "bundle-private.json"
    assert markdown_path.name == "bundle-private.md"
    assert {_mode(path) for path in (json_path, markdown_path, latest_json, latest_markdown)} == {0o600}
    assert {_mode(path) for path in (snapshots, context_bundles, base)} == {0o700}
    assert _mode(archive) == archive_mode
    assert _mode(archive.parent) == owner_home_mode
    assert json_path.read_bytes() == expected_json
    assert markdown_path.read_bytes() == expected_markdown
    assert latest_json.read_bytes() == expected_json
    assert latest_markdown.read_bytes() == expected_markdown


@_POSIX_ONLY
def test_context_bundle_rewrite_tightens_files_and_preserves_exact_output(tmp_path: Path) -> None:
    request, _bundle, home_paths = _private_context_bundle_inputs(tmp_path)
    archive = Path(home_paths.owner_home_dir) / "memory_archive"
    result = build_main_context_bundle(replace(request, save=True))
    bundle = result.bundle or {}
    json_path = Path(result.json_path)
    markdown_path = Path(result.markdown_path)
    base = json_path.parent
    snapshots = archive / "snapshots"
    context_bundles = snapshots / "context_bundles"
    latest_json = base / "latest_context_bundle.json"
    latest_markdown = base / "latest_context_bundle.md"
    for path in (json_path, markdown_path, latest_json, latest_markdown):
        os.chmod(path, 0o644)

    rewritten = json.loads(json.dumps(bundle, ensure_ascii=False))
    rewritten["tooling"]["runtime_injection_count_before_bundle"] += 1
    directory_result = PrivateDirectoryChainResult(archive, base, True, None)
    _rewrite_bundle_files(json_path, markdown_path, rewritten, directory_result)
    expected_json = _expected_json_bytes(rewritten)
    expected_markdown = render_markdown_bundle(rewritten, json_path=str(json_path)).encode("utf-8")

    assert {_mode(path) for path in (json_path, markdown_path, latest_json, latest_markdown)} == {0o600}
    assert {_mode(path) for path in (snapshots, context_bundles, base)} == {0o700}
    assert json_path.read_bytes() == expected_json
    assert markdown_path.read_bytes() == expected_markdown
    assert latest_json.read_bytes() == expected_json
    assert latest_markdown.read_bytes() == expected_markdown


@_POSIX_ONLY
def test_context_bundle_write_tightens_legacy_latest_copies_and_date_directory(tmp_path: Path) -> None:
    request, _bundle, home_paths = _private_context_bundle_inputs(tmp_path)
    archive = Path(home_paths.owner_home_dir) / "memory_archive"
    snapshots = archive / "snapshots"
    context_bundles = snapshots / "context_bundles"
    base = context_bundles / date.today().isoformat()
    base.mkdir(parents=True)
    os.chmod(snapshots, 0o700)
    os.chmod(context_bundles, 0o700)
    os.chmod(base, 0o755)
    latest_json = base / "latest_context_bundle.json"
    latest_markdown = base / "latest_context_bundle.md"
    for path in (latest_json, latest_markdown):
        path.write_text("stale legacy copy", encoding="utf-8")
        os.chmod(path, 0o644)

    result = build_main_context_bundle(replace(request, save=True))
    json_path = Path(result.json_path)
    markdown_path = Path(result.markdown_path)

    assert json_path.parent == base
    assert markdown_path.parent == base
    assert {_mode(path) for path in (json_path, markdown_path, latest_json, latest_markdown)} == {0o600}
    assert {_mode(path) for path in (snapshots, context_bundles, base)} == {0o700}
    assert latest_json.read_bytes() == json_path.read_bytes()
    assert latest_markdown.read_bytes() == markdown_path.read_bytes()


@_POSIX_ONLY
def test_context_bundle_with_symlinked_archive_root_writes_private_files_and_warns(
    tmp_path: Path, caplog
) -> None:
    request, _bundle, home_paths = _private_context_bundle_inputs(tmp_path)
    archive = Path(home_paths.owner_home_dir) / "memory_archive"
    moved_archive = tmp_path / "moved-archive"
    archive.rename(moved_archive)
    snapshots = moved_archive / "snapshots"
    context_bundles = snapshots / "context_bundles"
    base = context_bundles / date.today().isoformat()
    base.mkdir(parents=True)
    for directory in (moved_archive, snapshots, context_bundles, base):
        os.chmod(directory, 0o755)
    before_modes = {_mode(path) for path in (moved_archive, snapshots, context_bundles, base)}
    os.symlink(moved_archive, archive, target_is_directory=True)

    with caplog.at_level(logging.WARNING):
        result = build_main_context_bundle(replace(request, save=True))

    files = (
        Path(result.json_path),
        Path(result.markdown_path),
        Path(result.json_path).parent / "latest_context_bundle.json",
        Path(result.json_path).parent / "latest_context_bundle.md",
    )
    assert all(path.is_file() for path in files)
    assert {_mode(path) for path in files} == {0o600}
    assert {_mode(path) for path in (moved_archive, snapshots, context_bundles, base)} == before_modes
    assert archive.is_symlink()
    _assert_private_directory_symlink_warning(caplog, ".")


@_POSIX_ONLY
def test_context_bundle_with_symlinked_date_parent_keeps_target_modes_and_warns(
    tmp_path: Path, caplog
) -> None:
    request, _bundle, home_paths = _private_context_bundle_inputs(tmp_path)
    archive = Path(home_paths.owner_home_dir) / "memory_archive"
    snapshots = archive / "snapshots"
    snapshots.mkdir(mode=0o755)
    target = tmp_path / "moved-context-bundles"
    target.mkdir(mode=0o755)
    base = target / date.today().isoformat()
    base.mkdir(mode=0o755)
    os.chmod(archive, 0o755)
    os.chmod(snapshots, 0o755)
    os.chmod(target, 0o755)
    os.chmod(base, 0o755)
    link = snapshots / "context_bundles"
    os.symlink(target, link, target_is_directory=True)

    with caplog.at_level(logging.WARNING):
        result = build_main_context_bundle(replace(request, save=True))

    files = (
        Path(result.json_path),
        Path(result.markdown_path),
        Path(result.json_path).parent / "latest_context_bundle.json",
        Path(result.json_path).parent / "latest_context_bundle.md",
    )
    assert all(path.is_file() for path in files)
    assert {_mode(path) for path in files} == {0o600}
    assert _mode(archive) == 0o755
    assert _mode(snapshots) == 0o700
    assert (_mode(target), _mode(base)) == (0o755, 0o755)
    assert link.is_symlink()
    _assert_private_directory_symlink_warning(caplog, "snapshots/context_bundles")


def _assert_private_directory_symlink_warning(caplog, expected_path: str) -> None:
    records = [
        record
        for record in caplog.records
        if getattr(record, "reason_code", "") == "private_directory_symlink_skipped"
    ]
    assert len(records) == 1
    assert records[0].structured_warning["severity"] == "warning"
    assert records[0].structured_warning["path"] == expected_path


def test_main_context_bundle_contains_contract_surfaces_and_self_check(tmp_path: Path) -> None:
    root, result = _build_contract_bundle_result(tmp_path)

    payload = result.bundle or {}
    assert payload["schema_policy"]["required_fields"] == [
        "identity",
        "scope",
        "run_scope",
        "tool_manifest",
        "acceptance_contract",
        "self_check",
    ]
    assert payload["identity"]["owner_type"] == "main_agent"
    assert payload["identity"]["root_run_id"] == "run-main-contract"
    assert payload["identity"]["parent_run_id"] == ""
    assert payload["run_scope"]["workspace_roots"][0] == str(root.resolve())
    assert payload["run_scope"]["allowed_write_roots"] == [str((root / "outputs").resolve())]
    assert payload["run_scope"]["forbidden_write_roots"] == [str((root / "system").resolve())]
    assert payload["run_scope"]["locked_files"] == [str((root / "README.md").resolve())]
    assert payload["tool_manifest"]["visible_tools"] == ["read_file", "write_file"]
    assert payload["tool_manifest"]["executable_tools"] == ["read_file", "write_file"]
    assert "WRITE_FORBIDDEN" in payload["tool_manifest"]["failure_taxonomy"]
    failure_contracts = {
        item["code"]: item for item in payload["tool_manifest"]["failure_contracts"]
    }
    assert failure_contracts["WRITE_FORBIDDEN"]["recommended_action"] == "request_permission"
    assert payload["tool_manifest"]["tool_runtimes"][0]["visible_in_context"] is True
    assert payload["artifact_refs"]["items"][0]["ref"].endswith("outputs/index.html")
    assert payload["acceptance_contract"]["items"] == ["有登录", "有购买"]
    assert payload["acceptance_contract"]["constraints"] == ["单文件 HTML"]
    assert payload["acceptance_contract"]["latest_tests"] == ["人工检查按钮不失效"]
    assert payload["self_check"]["ok"] is True
    assert (
        payload["prompt_budget"]["prompt_section_chars"]
        <= payload["prompt_budget"]["max_prompt_section_chars"]
    )
    assert "request_id:" not in result.prompt_section
    assert "run_id:" not in result.prompt_section
    assert "task_id:" not in result.prompt_section
    assert "运行标识只供 runtime 内部关联" in result.prompt_section
    assert Path(result.json_path).exists()


def test_main_context_bundle_keeps_conversation_task_id_out_of_model_prompt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    result = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=ensure_my_agent_home(tmp_path / "home"),
            user_prompt="继续原任务",
            request_id="chat-request-not-work-id",
            run_id="chat-run-not-work-id",
            task_id="chat-task-not-work-id",
            task_attributes={"conversation_task_id": "durable-work-task"},
            save=False,
        )
    )

    assert "chat-request-not-work-id" not in result.prompt_section
    assert "chat-run-not-work-id" not in result.prompt_section
    assert "chat-task-not-work-id" not in result.prompt_section
    assert "durable-work-task" not in result.prompt_section
    assert result.bundle["scope"]["request_id"] == "chat-request-not-work-id"
    assert result.bundle["scope"]["task_id"] == "chat-task-not-work-id"
    assert result.bundle["task"]["attributes"]["conversation_task_id"] == "durable-work-task"


def test_pending_conversation_bundle_exposes_real_home_without_future_cwd_switch(
    tmp_path: Path,
) -> None:
    owner_home = tmp_path / "home" / "owners" / "providers" / "tui" / "users" / "u1"
    owner_home.mkdir(parents=True)
    result = build_main_context_bundle(
        MainContextBundleRequest(
            root=owner_home,
            home_paths=ensure_my_agent_home(tmp_path / "home"),
            user_prompt="在任务目录里创建 bbb",
            request_id="gwreq-pending",
            task_attributes={"conversation_thread_id": "thread-pending"},
            save=False,
        )
    )

    assert str(owner_home.resolve()) in result.prompt_section
    assert str((tmp_path / "home").resolve()) in result.prompt_section
    assert "首个工作工具会固定任务目录" not in result.prompt_section
    assert result.bundle["workspace_refs"]["primary_workspace_root"] == str(
        owner_home.resolve()
    )


def test_runtime_context_bundle_surfaces_tool_spec_load_error(tmp_path: Path) -> None:
    class BrokenTools:
        owner_type = "main_agent"

        def runtime_snapshot(self, **_kwargs):
            raise ValueError("tool registry broken")

    agent = SimpleNamespace(
        root=tmp_path,
        home_paths=None,
        config=SimpleNamespace(auto_save_memory=False),
        workspace_roots=[tmp_path],
        tools=BrokenTools(),
    )
    result = build_runtime_main_context_bundle(
        agent,
        RuntimeContextRequest(
            user_prompt="检查工具清单错误报告",
            inject=None,
            resume_context=None,
            save=False,
        ),
        memories=[],
        runtime_injections=[],
        routed_context=SimpleNamespace(required_read_paths=(), candidate_paths=()),
        resume_context_injected=False,
        task_local=False,
    )

    errors = result.bundle["tool_manifest"]["tool_load_errors"]
    assert errors[0]["context"] == "main_context_bundle.tool_runtime_snapshot"
    assert errors[0]["category"] == "data_parse"
    assert "读取失败" in errors[0]["model_message"]


def test_runtime_context_bundle_uses_owner_effective_workspace(tmp_path: Path) -> None:
    service_root = tmp_path / "service-cwd"
    owner_workspace = tmp_path / "owners" / "feishu-user"
    service_root.mkdir()
    owner_workspace.mkdir(parents=True)
    agent = SimpleNamespace(
        root=service_root,
        effective_workspace_root=owner_workspace,
        effective_workspace_roots=[owner_workspace],
        workspace_roots=[service_root],
        home_paths=None,
        config=SimpleNamespace(auto_save_memory=False),
        tools=SimpleNamespace(
            owner_type="main_agent",
            runtime_snapshot=lambda **_kwargs: runtime_snapshot_for_model_specs(()),
        ),
    )

    result = build_runtime_main_context_bundle(
        agent,
        RuntimeContextRequest(
            user_prompt="查看我的工作目录",
            inject=None,
            resume_context=None,
            save=False,
        ),
        memories=[],
        runtime_injections=[],
        routed_context=SimpleNamespace(required_read_paths=(), candidate_paths=()),
        resume_context_injected=False,
        task_local=False,
    )

    expected = str(owner_workspace.resolve())
    assert result.bundle["workspace_refs"]["primary_workspace_root"] == expected
    assert result.bundle["run_scope"]["primary_workspace_root"] == expected
    assert result.bundle["run_scope"]["workspace_roots"] == [expected]
    assert str(service_root.resolve()) not in result.prompt_section


def test_main_context_bundle_ignores_old_acceptance_attribute_names(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    result = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=ensure_my_agent_home(tmp_path / "home"),
            user_prompt="验收条件仍然只是用户原话，不是 task_attributes 字段。",
            request_id="request-old-attrs",
            run_id="run-old-attrs",
            task_id="task-old-attrs",
            task_attributes={
                "acceptance_criteria": ["旧 acceptance_criteria"],
                "tests": ["旧 tests"],
                "验收条件": ["旧中文验收条件"],
                "约束": ["旧中文约束"],
                "最近测试": ["旧中文最近测试"],
            },
            save=False,
        )
    )

    contract = result.bundle["acceptance_contract"]
    assert contract["source_status"] == "not_recorded"
    assert contract["items"] == []
    assert contract["constraints"] == []
    assert contract["latest_tests"] == []


def _build_contract_bundle_result(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    request = MainContextBundleRequest(
        root=root,
        home_paths=ensure_my_agent_home(tmp_path / "home"),
        user_prompt="做一个高端现代家具网站。\n验收条件:\n- 有登录\n- 有购买",
        request_id="request-main-contract",
        run_id="run-main-contract",
        task_id="task-main-contract",
        workspace_roots=(str(root), str(tmp_path / "shared")),
        write_boundary=_contract_write_boundary(root),
        tool_runtime_snapshot=_contract_tool_runtime_snapshot(),
        artifact_refs=(str(root / "outputs" / "index.html"),),
        task_attributes=_contract_task_attributes(),
        save=True,
    )
    return root, build_main_context_bundle(request)


def _contract_write_boundary(root: Path) -> dict[str, list[str]]:
    return {
        "allowed_write_roots": [str(root / "outputs")],
        "forbidden_write_roots": [str(root / "system")],
        "locked_files": [str(root / "README.md")],
    }


def _contract_tool_runtime_snapshot():
    specs = (
        make_test_model_spec(
            "read_file",
            category="filesystem",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        make_test_model_spec(
            "write_file",
            category="filesystem",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "内容"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
    )
    return runtime_snapshot_for_model_specs(
        specs,
        run_id="run-main-contract",
        allowed_tools=["read_file", "write_file"],
    )


def _contract_task_attributes() -> dict[str, list[str]]:
    return {
        "acceptance": ["有登录", "有购买"],
        "constraints": ["单文件 HTML"],
        "latest_tests": ["人工检查按钮不失效"],
    }


def test_compact_apply_skips_auto_context_bundle_when_scope_mismatches(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    home_paths = ensure_my_agent_home(tmp_path / "home")
    _write_compact_scope(root)
    wrong_bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=home_paths,
            user_prompt="这是另一个更新的任务",
            request_id="request-new",
            run_id="run-new",
            task_id="task-new",
            save=True,
        )
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-old",
                request_id="request-old",
                run_id="run-old",
                task_id="task-old",
            ),
            main_context_bundle_ref=wrong_bundle.json_path,
        ),
    )

    assert result["main_context_bundle_match"]["status"] == "scope_mismatch"
    assert result["main_context_bundle_match"]["candidate_ref"] == wrong_bundle.json_path
    assert "main_context_bundle" not in result["refs"]
    assert result["restore_refs"]["source_refs"]["context_bundles"] == []


def test_compact_apply_explicit_context_bundle_ref_records_mismatch_but_keeps_ref(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    home_paths = ensure_my_agent_home(tmp_path / "home")
    _write_compact_scope(root)
    explicit_bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=home_paths,
            user_prompt="用户明确指定的任务卡",
            request_id="request-other",
            run_id="run-other",
            task_id="task-other",
            save=True,
        )
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-old",
                request_id="request-old",
                run_id="run-old",
                task_id="task-old",
            ),
            main_context_bundle_ref=explicit_bundle.json_path,
            main_context_bundle_ref_explicit=True,
        ),
    )

    assert result["main_context_bundle_match"]["status"] == "explicit_scope_mismatch"
    assert result["refs"]["main_context_bundle"] == explicit_bundle.json_path
    assert (
        result["restore_refs"]["source_refs"]["context_bundles"][0]["path"]
        == explicit_bundle.json_path
    )


def test_context_bundle_latest_cli_reports_observability_payload(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "agent_config.yaml"
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        f'workspace_root: "{workspace}"\n'
        f'my_agent_home: "{home}"\n'
        'model_backend: "echo"\n'
        'access_mode: "full-access"\n'
        'memory_path: "data/memory.jsonl"\n'
        'local_store_path: "data/local_store/local.db"\n'
        'local_store_files_dir: "data/local_store/files"\n'
        'local_store_events_path: "data/local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    run_args = build_parser().parse_args(
        [
            "--config",
            str(config_path),
            "run",
            "context bundle CLI 观测测试",
            "--save",
        ]
    )
    assert run_args.func(run_args) == 0
    capsys.readouterr()

    latest_args = build_parser().parse_args(
        ["--config", str(config_path), "context-bundle", "latest", "--json"]
    )
    assert latest_args.func(latest_args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["scope"]["context_scope"] == "default"
    assert payload["self_check"]["ok"] is True
    assert payload["path"].endswith("latest_context_bundle.json")


def test_context_bundle_latest_payload_reports_bad_json(tmp_path: Path) -> None:
    from agent_py_agent.cli.context_bundle_commands import _latest_payload

    bundle_path = tmp_path / "latest_context_bundle.json"
    bundle_path.write_text("{bad json", encoding="utf-8")

    payload = _latest_payload(str(bundle_path))

    assert payload["ok"] is False
    assert payload["status"] == "invalid_context_bundle"
    assert payload["load_error"]["context"] == "cli.context_bundle.latest.read"


def test_main_context_bundle_artifacts_can_be_updated_from_tool_output_index(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    home_paths = ensure_my_agent_home(tmp_path / "home")
    bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=home_paths,
            user_prompt="读取大日志后继续分析",
            request_id="request-artifact",
            run_id="run-artifact",
            task_id="task-artifact",
            save=True,
        )
    )
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="read_file",
            call_id="1-1",
            output="large output\n" + ("x" * 25_000),
            ok=True,
            # 默认 externalize 阈值 200K, 25K 输出不会落 artifact——显式 min_chars=1
            # 强制走恢复产物路径, 验证 bundle 产物更新合同。
            min_chars=1,
            request_id="request-artifact",
            run_id="run-artifact",
            task_id="task-artifact",
        )
    )

    update = update_main_context_bundle_artifacts(
        MainContextBundleArtifactUpdateRequest(
            context_bundle_path=bundle.json_path,
            workspace_root=root,
            request_id="request-artifact",
            run_id="run-artifact",
            task_id="task-artifact",
        )
    )
    payload = json.loads(Path(bundle.json_path).read_text(encoding="utf-8"))

    assert update["status"] == "updated"
    assert payload["artifact_refs"]["collection_phase"] == "post_tool_loop"
    assert payload["artifact_refs"]["items"][0]["ref"] == str(record["artifact_ref"])
    assert payload["artifact_refs"]["items"][0]["scoped_call_id"] == "run-artifact:1-1"


def test_main_context_bundle_artifacts_report_corrupt_bundle(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    bundle_path = root / "context_bundle.json"
    root.mkdir()
    bundle_path.write_text("{bad-json", encoding="utf-8")

    update = update_main_context_bundle_artifacts(
        MainContextBundleArtifactUpdateRequest(
            context_bundle_path=str(bundle_path),
            workspace_root=root,
            request_id="request-artifact",
            run_id="run-artifact",
            task_id="task-artifact",
        )
    )

    assert update["ok"] is False
    assert update["status"] == "missing_or_invalid_context_bundle"
    assert update["artifact_count"] == 0
    assert update["load_error"]["category"] == "data_parse"
    assert update["load_error"]["context"] == "context_bundle_artifacts.context_bundle"
    assert update["load_error"]["path"] == str(bundle_path)


def _write_compact_scope(root: Path) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-old",
            session_id="session-old",
            request_id="request-old",
            run_id="run-old",
            task_id="task-old",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="老任务需要 compact",
            source="run",
            archive_level=2,
            created_at="2026-05-17T10:00:00+00:00",
        ),
    )
    snapshot = CompressionSnapshot(
        snapshot_id="snapshot-old",
        session_id="session-old",
        compression_id="compression-old",
        turn_range={"start": 1, "end": 1, "request_id": "request-old", "run_id": "run-old"},
        user_intents=["老任务需要 compact"],
        assistant_actions=["准备恢复老任务"],
        task_refs=["task-old"],
        next_actions=["继续老任务"],
        archive_level=2,
        created_at="2026-05-17T10:01:00+00:00",
    )
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    append_session_token_usage(
        root,
        usage=TurnTokenUsage(
            session_id="session-old",
            turn_id="turn-old",
            input_tokens=100,
            output_tokens=50,
            tool_tokens=10,
            created_at="2026-05-17T10:02:00+00:00",
        ),
    )
