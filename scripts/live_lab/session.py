
from __future__ import annotations

"""Live Lab session workspace and isolated config writer.

这个模块只负责创建隔离测试目录、fixture 小项目和临时配置，避免 Live Lab 污染开发仓库。
"""

import argparse
import hashlib
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .compact_observations import (
    UsageFileCursor,
    aggregate_usage_records,
    assert_isolated_home_path,
    collect_bg_process_census,
    discover_usage_ledger_files,
    read_usage_delta_since,
    resolve_live_lab_home_context,
    snapshot_compaction_baseline,
    snapshot_usage_cursors,
)


def live_lab_model_request_timeout(args: argparse.Namespace) -> int:
    """Return the per-model-call timeout used by the isolated Live Lab config."""

    return max(30, int(getattr(args, "timeout", 300) or 300))


def live_lab_gateway_wait_timeout(args: argparse.Namespace) -> int:
    """Return the total wait budget for a gateway request.

    A single user request can include several model calls: the root turn,
    tool-result follow-up turns, child runner turns, and a final response. Keep
    this budget separate from the per-call model timeout so the harness does not
    kill valid multi-step work before the model request budget has even elapsed.
    """

    per_call = live_lab_model_request_timeout(args)
    max_cycles = max(1, int(getattr(args, "max_cycles", 1) or 1))
    estimated_model_calls = max(4, max_cycles + 2)
    return per_call * estimated_model_calls + 120


def live_lab_gateway_processing_timeout(args: argparse.Namespace) -> int:
    """Return the stale-processing timeout for the isolated gateway."""

    return max(live_lab_gateway_wait_timeout(args) + 120, 180)


class LabSessionManager:
    """owns workspace setup and config writing.

    测试前先建目录、写 fixture 小项目、写一份临时配置。
    这份配置会把 memory、gateway、subagent、LocalStore 都关进测试目录。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.source_config = Path(args.config).expanduser().resolve()
        self.runs_dir = Path(args.runs_dir).expanduser().resolve()
        stamp = args.run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_root = self.runs_dir / stamp
        self.fixture_root = self.run_root / "fixture_project"
        # 宿主数据根不能嵌在业务工作区内；两个目录同属本轮，仍保持完全隔离。
        self.isolated_home = self.run_root / "home"
        assert_isolated_home_path(self.run_root, label="run_root")
        assert_isolated_home_path(self.isolated_home, label="isolated_home")

        self.prompts_dir = self.run_root / "prompts"
        self.responses_dir = self.run_root / "responses"
        self.config_path = self.run_root / "live_agent_config.yaml"
        self.transcript_path = self.run_root / "TRANSCRIPT.md"
        self.summary_path = self.run_root / "live_lab_summary.json"
        self.stop_file = self.run_root / "STOP"
        self.provenance_path = self.run_root / "run_provenance.json"
        self.created = False

        self.run_token = uuid.uuid4().hex
        self.started_at = time.time()
        self.started_monotonic_ns = time.monotonic_ns()
        self.reused_existing_run_root = self.run_root.exists()
        self.preexisting_artifacts = self._snapshot_preexisting_artifacts()

        self.resolved_home_context: dict[str, Any] = {}
        self.owner_home_dir: Path | None = None
        self.owner_compact_dir: Path | None = None
        self.owner_tool_policy_json: Path | None = None
        self.conversation_workspace: Path | None = None
        self.model_usage_dir: Path | None = None
        self.threads_dir: Path | None = None
        self.process_store_root: Path | None = None
        self.resolved_compact_policy: dict[str, Any] = {}
        self.usage_ledger_path: Path | None = None
        self.turn_usage_records: list[dict[str, Any]] = []
        self._active_usage_cursors: dict[str, UsageFileCursor] = {}
        self.compact_baseline: dict[str, Any] = {}
        self.bg_census_baseline: dict[str, Any] = {}

    def _snapshot_preexisting_artifacts(self) -> dict[str, dict[str, Any]]:
        targets = {
            "summary_json": self.summary_path,
            "compact_stress_source": self.fixture_root / "data" / "long_field_journal.txt",
            "compact_stress_report": self.fixture_root / "lab_outputs" / "compact-stress" / "report.md",
        }
        snapshot: dict[str, dict[str, Any]] = {}
        for name, target in targets.items():
            if target.is_file():
                st = target.stat()
                snapshot[name] = {
                    "exists": True,
                    "path": str(target),
                    "size": int(st.st_size),
                    "mtime_ns": int(st.st_mtime_ns),
                    "sha256": _sha256_file(target) if st.st_size <= 32 * 1024 * 1024 else "",
                }
            else:
                snapshot[name] = {"exists": False, "path": str(target)}
        return snapshot

    def setup(self) -> None:
        """creates the isolated live-lab workspace and config."""
        if not self.source_config.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.source_config}")
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.write_fixture()
        self.write_config()
        self.prepare_runtime_dirs()
        self.write_provenance()
        self.created = True

    @property
    def model_request_timeout(self) -> int:
        """Per-model-call timeout configured for this isolated run."""

        return live_lab_model_request_timeout(self.args)

    @property
    def gateway_wait_timeout(self) -> int:
        """Total wait budget for one gateway ask in this isolated run."""

        return live_lab_gateway_wait_timeout(self.args)

    def write_fixture(self) -> None:
        """writes a tiny project that real agents can safely read and modify."""
        (self.fixture_root / "README.md").write_text(
            "\n".join(
                [
                    "# Live Agent Lab Fixture",
                    "",
                    "这是 my-agent Live Lab 的隔离项目。",
                    "真实模型可以读取这里的文件，也可以把测试报告写到 lab_outputs/。",
                    "",
                    "## 任务素材",
                    "",
                    "- 你需要证明自己读到了 README。",
                    "- 你需要把输出留在 lab_outputs/。",
                    "- 不要读写 fixture_project 之外的文件。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (self.fixture_root / "notes").mkdir(parents=True, exist_ok=True)
        (self.fixture_root / "notes" / "small_task.md").write_text(
            "小任务素材：总结 README 的目标，并说明输出文件路径。\n",
            encoding="utf-8",
        )
        (self.fixture_root / "notes" / "problem_task.md").write_text(
            "问题任务素材：如果证据不足，应明确说缺什么，不要假装完成。\n",
            encoding="utf-8",
        )
        (self.fixture_root / "lab_outputs").mkdir(parents=True, exist_ok=True)

    def write_config(self) -> None:
        """appends isolation overrides while preserving model/API settings."""
        base = self.source_config.read_text(encoding="utf-8")
        fixture = str(self.fixture_root).replace("\\", "/")
        isolated_home = str(self.isolated_home.resolve()).replace("\\", "/")
        backend_override = "" if self.args.real_llm else '\nmodel_backend: "echo"\n'
        request_timeout = live_lab_model_request_timeout(self.args)
        gateway_timeout = live_lab_gateway_wait_timeout(self.args)
        processing_timeout = live_lab_gateway_processing_timeout(self.args)
        overrides = f"""

# live-agent-lab isolation overrides
workspace_root: "{fixture}"
my_agent_home: "{isolated_home}"
prompt_files:
memory_path: ".my_agent/memory.jsonl"
local_store_path: ".my_agent/local_store/local.db"
local_store_files_dir: ".my_agent/local_store/files"
local_store_events_path: ".my_agent/local_store/events.jsonl"
subagent_workspace: ".my_agent/subagents"
gateway_workspace: ".my_agent/gateway"
adapter_workspace: ".my_agent/adapters/file"
max_subagents: {max(self.args.count, 1)}
gateway_request_timeout: {gateway_timeout}
gateway_processing_timeout_seconds: {processing_timeout}
gateway_request_max_attempts: 2
daemon_apply: false
daemon_execute_runners: false
# live lab keeps runner tool rounds unlimited unless a specific stress case overrides it
max_tool_rounds: 0
request_timeout: {request_timeout}
{backend_override}"""
        self.config_path.write_text(base + overrides, encoding="utf-8")

    def prepare_runtime_dirs(self) -> None:
        """沿正式 CLI 解析结果准备本轮运行文件和空记忆的派生索引。"""
        from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
        from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
        from agent_py_agent.cli.common import make_lightweight_cli_agent_context

        context = make_lightweight_cli_agent_context(argparse.Namespace(config=str(self.config_path)))
        assert_isolated_home_path(context.home_paths.root, label="resolved_home_root")
        ensure_my_agent_home(context.home_paths.root)
        # 项目路径与 owner 固定服务路径的相对基准不同，不能手拼 fixture 子目录。
        event_path = context.runtime_paths["local_store_events_path"]
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.touch(exist_ok=True)
        memory = JsonlMemory(context.runtime_paths["memory_path"])
        memory.migrate_tiers()
        memory.rebuild_read_index()

        self.refresh_resolved_home_context()
        self.compact_baseline = snapshot_compaction_baseline(
            compact_dir=self.owner_compact_dir or (self.isolated_home / "owners" / "local" / "main" / "compact"),
            threads_dir=self.threads_dir,
        )
        self.bg_census_baseline = collect_bg_process_census(self.process_store_root)
        self._active_usage_cursors = snapshot_usage_cursors(self.usage_ledger_files())

    def refresh_resolved_home_context(self) -> dict[str, Any]:
        """Re-read effective isolated paths and compact policy from live_agent_config.yaml."""
        info = resolve_live_lab_home_context(self.config_path)
        self.resolved_home_context = info
        self.owner_home_dir = Path(info["owner_home_dir"])
        self.owner_compact_dir = Path(info["owner_compact_dir"])
        self.owner_tool_policy_json = Path(info["owner_tool_policy_json"])
        self.conversation_workspace = Path(info["conversation_workspace"])
        self.model_usage_dir = Path(info["model_usage_dir"])
        self.threads_dir = Path(info["threads_dir"])
        self.process_store_root = Path(info["process_store_root"])
        self.resolved_compact_policy = dict(info.get("compact_policy") or {})
        return info

    def write_provenance(self) -> Path:
        """Persist explicit run baseline and provenance for D4-REUSE verification."""
        provenance = {
            "schema": "live_lab_run_provenance.v1",
            "run_token": self.run_token,
            "started_at": self.started_at,
            "reused_existing_run_root": self.reused_existing_run_root,
            "run_root": str(self.run_root),
            "isolated_home": str(self.isolated_home),
            "owner_home_dir": str(self.owner_home_dir or ""),
            "owner_compact_dir": str(self.owner_compact_dir or ""),
            "owner_tool_policy_json": str(self.owner_tool_policy_json or ""),
            "config_sha256": _sha256_file(self.config_path) if self.config_path.is_file() else "",
            "source_config_sha256": _sha256_file(self.source_config) if self.source_config.is_file() else "",
            "preexisting_artifacts": self.preexisting_artifacts,
            "compact_baseline": self.compact_baseline,
            "bg_census_baseline": self.bg_census_baseline,
        }
        self.provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.provenance_path

    def usage_ledger_files(self, *, extra_files: Sequence[str | Path] = ()) -> list[Path]:
        """Return deduplicated usage JSONL files belonging strictly to this isolated run."""
        search_roots: list[Path] = [self.run_root, self.isolated_home]
        if self.owner_home_dir is not None:
            search_roots.append(self.owner_home_dir)
        if self.conversation_workspace is not None:
            search_roots.append(self.conversation_workspace)
        files = discover_usage_ledger_files(
            model_usage_dir=self.model_usage_dir,
            llm_usage_path=self.usage_ledger_path,
            search_roots=search_roots,
        )
        seen = {str(item.resolve(strict=False)) for item in files}
        for item in extra_files:
            path = Path(item)
            if path.is_file():
                resolved = str(path.resolve(strict=False))
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(Path(resolved))
        return files

    def begin_turn_usage(
        self,
        turn_label: str = "",
        *,
        extra_files: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        """Snapshot usage JSONL byte/line cursors right before a turn executes."""
        files = self.usage_ledger_files(extra_files=extra_files)
        cursors = snapshot_usage_cursors(files)
        self._active_usage_cursors = dict(cursors)
        return {
            "turn_label": turn_label or f"turn_{len(self.turn_usage_records) + 1}",
            "started_at": time.time(),
            "files": [str(item) for item in files],
            "cursors": {key: value.to_dict() for key, value in cursors.items()},
        }

    def end_turn_usage(
        self,
        turn_token: Mapping[str, Any] | None = None,
        *,
        turn_label: str = "",
        extra_files: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        """Slice ONLY newly completed usage rows since begin_turn_usage and aggregate them."""
        token = dict(turn_token or {})
        label = turn_label or str(token.get("turn_label") or f"turn_{len(self.turn_usage_records) + 1}")
        before_raw = token.get("cursors")
        before_cursors = (
            before_raw
            if isinstance(before_raw, Mapping)
            else self._active_usage_cursors
        )
        files = self.usage_ledger_files(extra_files=extra_files)
        rows, after_cursors, delta_meta = read_usage_delta_since(files, before_cursors)
        self._active_usage_cursors = after_cursors
        summary = aggregate_usage_records(rows)
        record = {
            "turn": label,
            "started_at": token.get("started_at"),
            "finished_at": time.time(),
            "raw_event_count": len(rows),
            **summary,
            "delta_metadata": delta_meta,
        }
        self.turn_usage_records.append(record)
        return record


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
