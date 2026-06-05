from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.context_bundle import build_runtime_main_context_bundle
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
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
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli.parser import build_parser


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
    failure_contracts = {item["code"]: item for item in payload["tool_manifest"]["failure_contracts"]}
    assert failure_contracts["WRITE_FORBIDDEN"]["recommended_action"] == "request_permission"
    assert payload["tool_manifest"]["tool_specs"][0]["visible_in_context"] is True
    assert payload["artifact_refs"]["items"][0]["ref"].endswith("outputs/index.html")
    assert payload["acceptance_contract"]["items"] == ["有登录", "有购买"]
    assert payload["acceptance_contract"]["constraints"] == ["单文件 HTML"]
    assert payload["acceptance_contract"]["latest_tests"] == ["人工检查按钮不失效"]
    assert payload["self_check"]["ok"] is True
    assert payload["prompt_budget"]["prompt_section_chars"] <= payload["prompt_budget"]["max_prompt_section_chars"]
    assert Path(result.json_path).exists()


def test_runtime_context_bundle_surfaces_tool_spec_load_error(tmp_path: Path) -> None:
    class BrokenTools:
        def specs(self, **_kwargs):
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
    assert errors[0]["context"] == "main_context_bundle.tool_specs"
    assert errors[0]["category"] == "data_parse"
    assert "读取失败" in errors[0]["model_message"]


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
        allowed_tools=("read_file", "write_file"),
        granted_capabilities=("filesystem",),
        tool_specs=_contract_tool_specs(),
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


def _contract_tool_specs() -> tuple[dict[str, object], ...]:
    return (
        {"name": "read_file", "category": "filesystem", "parameters": {"path": "文件路径"}},
        {"name": "write_file", "category": "filesystem", "parameters": {"path": "文件路径", "content": "内容"}},
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


def test_compact_apply_explicit_context_bundle_ref_records_mismatch_but_keeps_ref(tmp_path: Path) -> None:
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
    assert result["restore_refs"]["source_refs"]["context_bundles"][0]["path"] == explicit_bundle.json_path


def test_context_bundle_latest_cli_reports_observability_payload(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "agent_config.yaml"
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        f'workspace_root: "{workspace}"\n'
        f'my_agent_home: "{home}"\n'
        'model_backend: "echo"\n'
        'memory_path: "data/memory.jsonl"\n'
        'local_store_path: "data/local_store/local.db"\n'
        'local_store_files_dir: "data/local_store/files"\n'
        'local_store_events_path: "data/local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    run_args = build_parser().parse_args([
        "--config",
        str(config_path),
        "run",
        "context bundle CLI 观测测试",
        "--save",
    ])
    assert run_args.func(run_args) == 0
    capsys.readouterr()

    latest_args = build_parser().parse_args(["--config", str(config_path), "context-bundle", "latest", "--json"])
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


def test_main_context_bundle_artifacts_can_be_updated_from_tool_output_index(tmp_path: Path) -> None:
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
