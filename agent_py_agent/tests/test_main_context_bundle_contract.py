from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
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


# LLM: Main context bundles must carry contracts, not only loose path summaries.
# 函数用途: 验证主代理上下文包包含运行范围、工具清单、验收合同、自检、预算和 owner 预留字段。
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
    assert failure_contracts["WRITE_FORBIDDEN"]["recommended_action"] == "request_permission_or_choose_allowed_root"
    assert payload["tool_manifest"]["tool_specs"][0]["visible_in_context"] is True
    assert payload["artifact_refs"]["items"][0]["ref"].endswith("outputs/index.html")
    assert payload["acceptance_contract"]["items"] == ["有登录", "有购买"]
    assert payload["acceptance_contract"]["constraints"] == ["单文件 HTML"]
    assert payload["acceptance_contract"]["latest_tests"] == ["人工检查按钮不失效"]
    assert payload["self_check"]["ok"] is True
    assert payload["prompt_budget"]["prompt_section_chars"] <= payload["prompt_budget"]["max_prompt_section_chars"]
    assert Path(result.json_path).exists()


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


# LLM: Compact apply must not bind the latest context bundle if it belongs to another scope.
# 函数用途: 验证自动 latest context bundle 有 scope match，避免老任务 compact 误拿新任务任务卡。
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


# LLM: Explicit bundle refs may override a mismatch while still recording the mismatch.
# 函数用途: 验证用户显式传 context bundle 时保留引用，但 match report 仍提示 scope 不一致。
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


# LLM: The CLI should expose the latest context bundle and its self-check without reading large bodies.
# 函数用途: 验证 `context-bundle latest --json` 能让用户/前端查看最新任务卡、scope 和自检状态。
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


# LLM: Saved context bundles should receive post-tool artifact refs by scope, not by broad scans.
# 函数用途: 验证工具循环后同 request/run/task 的外置工具输出会写回主 context bundle 的 artifact_refs。
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
            output="large output\n" + ("x" * 2000),
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


# LLM: _write_compact_scope gives compact apply a real old task scope.
# 函数用途: 写入 old scope 的 raw/snapshot/token 事实，供 scope mismatch 测试构造 compact plan。
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
