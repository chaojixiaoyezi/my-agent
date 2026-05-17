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
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


# LLM: This fixture mirrors a compactable saved run while keeping this focused test file self-contained.
# 函数用途: 写入 raw/hook/token 三类最小归档事实，让 compact apply/resume 可以走真实路径。
def _write_compact_fixture(root: Path) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-compact-1",
            session_id="session-compact",
            request_id="request-compact",
            run_id="run-compact",
            task_id="run-compact",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="需要自动 compact dry-run 计划",
            source="run",
            archive_level=2,
            created_at="2026-05-06T08:00:00+00:00",
        ),
    )
    snapshot = CompressionSnapshot(
        snapshot_id="snapshot-compact-1",
        session_id="session-compact",
        compression_id="compression-compact",
        turn_range={"start": 1, "end": 1, "request_id": "request-compact", "run_id": "run-compact"},
        user_intents=["需要自动 compact dry-run 计划"],
        assistant_actions=["准备扫描归档、snapshot 和 token ledger。"],
        task_refs=["run-compact"],
        next_actions=["先看 dry-run，再决定是否启用 apply。"],
        archive_level=2,
        created_at="2026-05-06T08:01:00+00:00",
    )
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    append_session_token_usage(
        root,
        usage=TurnTokenUsage(
            session_id="session-compact",
            turn_id="turn-1",
            input_tokens=100,
            output_tokens=20,
            tool_tokens=5,
            created_at="2026-05-06T08:02:00+00:00",
        ),
    )


# LLM: _write_home_config gives CLI compact tests an isolated my-agent home for context bundles.
# 函数用途: 写入带绝对 my_agent_home 的临时配置，避免测试读写用户真实家目录。
def _write_home_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    home = tmp_path / "home"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        f'my_agent_home: "{home}"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


# LLM: compact apply/resume should carry the root run context card instead of making resume guess scope.
# 函数用途: 验证手动 compact apply 登记主代理 context bundle，resume 时优先推荐读取并写进继续包。
def test_memory_compact_apply_and_resume_use_main_context_bundle(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    home_paths = ensure_my_agent_home(tmp_path / "home")
    bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=home_paths,
            user_prompt="继续主代理家具网站任务",
            request_id="request-compact",
            run_id="run-compact",
            task_id="家具网站",
            save=True,
        )
    )

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
            main_context_bundle_ref=bundle.json_path,
        ),
    )
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

    assert apply_result["refs"]["main_context_bundle"] == bundle.json_path
    assert apply_result["main_context_bundle"]["loaded"] is True
    assert apply_result["main_context_bundle"]["scope"]["request_id"] == "request-compact"
    assert apply_result["restore_refs"]["source_refs"]["context_bundles"][0]["path"] == bundle.json_path
    assert resume["main_context_bundle"]["ref"] == bundle.json_path
    assert resume["main_context_bundle"]["scope"]["task_id"] == "家具网站"
    assert bundle.json_path in resume["recommended_read_paths"]
    assert "Main Context Bundle" in resume["context_block"]
    assert resume["continue_packet"]["main_context_bundle"]["ref"] == bundle.json_path


# LLM: CLI compact apply should find the latest saved root context bundle without a user-supplied path.
# 函数用途: 验证真实 run --save 后执行 memory-compact --apply 会自动携带最新主代理 context bundle。
def test_memory_compact_cli_apply_uses_latest_main_context_bundle(tmp_path: Path, capsys) -> None:
    config_path = _write_home_config(tmp_path)
    run_args = build_parser().parse_args([
        "--config",
        str(config_path),
        "run",
        "主代理 compact context bundle CLI 测试。\n验收条件:\n- context bundle 自动进入 compact apply",
        "--save",
    ])
    assert run_args.func(run_args) == 0
    capsys.readouterr()
    compact_args = build_parser().parse_args([
        "--config",
        str(config_path),
        "memory-compact",
        "--apply",
        "--json",
    ])

    code = compact_args.func(compact_args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["main_context_bundle"]["loaded"] is True
    assert payload["main_context_bundle"]["scope"]["context_scope"] == "default"
    assert payload["refs"]["main_context_bundle"].startswith(str(tmp_path / "home"))
