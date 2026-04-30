from __future__ import annotations

"""LLM contract: SubAgentBaseMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING

from .models import *
from .reports import *
from .rendering import *
from .runner_rendering import *
from .runner_rendering import _render_runner_item_line
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _severity_weight,
    _runner_next_action,
    _select_capability_hits,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalStore


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_list_value(value: object) -> list[str]:
    return [str(item) for item in _list_value(value) if item not in (None, "")]


def _normalize_quality_contract(value: object) -> QualityContract:
    if isinstance(value, QualityContract):
        return value
    if not isinstance(value, dict):
        return QualityContract()
    payload = {key: value[key] for key in _field_names(QualityContract) if key in value}
    for key in [
        "failure_conditions",
        "forbidden_delivery",
        "must_check",
        "sampling_plan",
        "evidence_required",
        "allowed_degradation",
    ]:
        payload[key] = _string_list_value(payload.get(key))
    payload["cannot_self_accept"] = bool(payload.get("cannot_self_accept", True))
    payload["parent_final_gate"] = bool(payload.get("parent_final_gate", True))
    return QualityContract(**payload)


def _normalize_context_manifest(value: object) -> ContextManifest:
    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    for key in ["task_pack_refs", "required_read_paths", "omitted_context"]:
        payload[key] = _string_list_value(payload.get(key))
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


class SubAgentBaseMixin:
    def __init__(self, workspace: str | Path, local_store: "LocalStore | None" = None):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cards: dict[str, SubAgentCard] = {}
        self.local_store = local_store

    def split(self, goal: str, count: int) -> list[SubAgentTask]:
        """把一个目标拆成若干子任务记录。

        这里暂时还是模板化拆分，不做复杂规划。
        目的不是“真的很聪明地拆”，而是先把整个数据流打通。
        """

        count = max(1, count)
        tasks: list[SubAgentTask] = []
        for i in range(1, count + 1):
            task = self.create_run(
                goal=f"{goal} / 子任务{i}",
                thought="先缩小任务边界，明确输入、输出和验证证据，再执行。",
                plan=["理解目标", "列出交付物", "执行最小验证", "汇报结果和证据"],
            )
            tasks.append(task)
        return tasks

    def register_card(self, card: SubAgentCard) -> None:
        """注册一张子代理角色卡。"""

        self.cards[card.name] = card

    def create_run(
        self,
        *,
        goal: str,
        thought: str,
        plan: list[str],
        agent_name: str = "general",
        role: str = "general",
        parent_id: str = "",
        root_id: str = "",
        depth: int = 0,
        allowed_skills: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        owner: str = "",
        supervisor: str = "",
        final_owner: str = "",
        acceptance_checks: list[str] | None = None,
        quality_contract: QualityContract | dict[str, object] | None = None,
        context_manifest: ContextManifest | dict[str, object] | None = None,
        context_packs: list[dict[str, object]] | dict[str, object] | None = None,
    ) -> SubAgentTask:
        """创建一条子代理运行记录。"""

        now = time.time()
        run_id = _new_id("subagent")
        paths = self._build_work_order_paths(run_id)
        task = SubAgentTask(
            id=run_id,
            goal=goal,
            thought=thought,
            plan=plan,
            agent_name=agent_name,
            role=role,
            owner=owner,
            supervisor=supervisor,
            final_owner=final_owner,
            parent_id=parent_id,
            root_id=root_id or run_id,
            depth=depth,
            allowed_skills=allowed_skills or [],
            allowed_tools=allowed_tools or [],
            acceptance_checks=acceptance_checks or [],
            quality_contract=_normalize_quality_contract(quality_contract),
            context_manifest=_normalize_context_manifest(context_manifest),
            context_packs=_normalize_context_packs(context_packs),
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
            **paths,
        )
        self.save(task)
        if parent_id:
            self.add_child(parent_id, task.id)
        return task

    def load(self, run_id: str) -> SubAgentTask:
        """从磁盘读取一条运行记录。"""

        path = self.workspace / run_id / "task.json"
        if not path.exists():
            raise FileNotFoundError(f"子代理记录不存在: {run_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        data = {key: value for key, value in data.items() if key in _field_names(SubAgentTask)}
        data["capability_requests"] = [
            CapabilityRequest(**item) for item in data.get("capability_requests", []) if isinstance(item, dict)
        ]
        data["capability_grants"] = [
            CapabilityGrant(**item) for item in data.get("capability_grants", []) if isinstance(item, dict)
        ]
        data["capability_gaps"] = [
            CapabilityGap(**item) for item in data.get("capability_gaps", []) if isinstance(item, dict)
        ]
        data["evidence"] = [VerificationEvidence(**item) for item in data.get("evidence", []) if isinstance(item, dict)]
        data["takeover_records"] = [
            TakeoverRecord(**item) for item in data.get("takeover_records", []) if isinstance(item, dict)
        ]
        data["channel_checks"] = [
            ChannelProbeCheck(**item) for item in data.get("channel_checks", []) if isinstance(item, dict)
        ]
        data["quality_contract"] = _normalize_quality_contract(data.get("quality_contract"))
        data["context_manifest"] = _normalize_context_manifest(data.get("context_manifest"))
        data["context_packs"] = _normalize_context_packs(data.get("context_packs"))
        return SubAgentTask(**data)

    def add_child(self, parent_id: str, child_id: str) -> None:
        """把子运行挂到父运行下面。"""

        try:
            parent = self.load(parent_id)
        except FileNotFoundError:
            return
        if child_id not in parent.child_ids:
            parent.child_ids.append(child_id)
            parent.updated_at = time.time()
            self.save(parent)

    def list_runs(self) -> list[SubAgentTask]:
        """扫描当前工作区内所有子代理运行记录。"""

        runs: list[SubAgentTask] = []
        for task_file in sorted(self.workspace.glob("*/task.json")):
            try:
                runs.append(self.load(task_file.parent.name))
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return runs

    def save(self, task: SubAgentTask) -> None:
        """保存子任务记录。

        一份存成 JSON，方便程序继续处理；
        一份存成 Markdown，方便人直接打开看。
        """

        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        task_dir = Path(task.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_work_order_files(task)
        task.updated_at = task.updated_at or time.time()
        (task_dir / "task.json").write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (task_dir / "run.json").write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (task_dir / "thought.md").write_text(
            "# Thought\n\n"
            f"{task.thought}\n\n"
            "## Plan\n"
            + "\n".join(f"- {item}" for item in task.plan)
            + "\n\n"
            "## Capability Boundary\n"
            f"- Agent: {task.agent_name}\n"
            f"- Role: {task.role}\n"
            f"- Owner: {task.owner or 'none'}\n"
            f"- Supervisor: {task.supervisor or 'none'}\n"
            f"- Final owner: {task.final_owner or 'none'}\n"
            f"- Parent: {task.parent_id or 'none'}\n"
            f"- Depth: {task.depth}\n"
            f"- Allowed skills: {', '.join(task.allowed_skills) or 'none'}\n"
            f"- Allowed tools: {', '.join(task.allowed_tools) or 'none'}\n\n"
            "## Write Boundary\n"
            f"- Task dir: {task.task_dir}\n"
            f"- Allowed write roots: {', '.join(task.allowed_write_roots) or 'none'}\n"
            f"- Forbidden write roots: {', '.join(task.forbidden_write_roots) or 'none'}\n\n"
            "## Acceptance Checks\n"
            + "\n".join(f"- {item}" for item in task.acceptance_checks or ["未设置"])
            + "\n\n"
            "## Evidence\n"
            + "\n".join(f"- [{item.kind}] {item.summary}" for item in task.evidence or [])
            + ("\n" if task.evidence else "- 暂无\n"),
            encoding="utf-8",
        )
        self._index_task(task)

    def _build_work_order_paths(self, run_id: str, task_dir: str | Path | None = None) -> dict[str, object]:
        """生成标准工单目录路径。"""

        task_dir = Path(task_dir) if task_dir else self.workspace / run_id
        paths = {
            "task_dir": str(task_dir),
            "data_dir": str(task_dir / "data"),
            "output_dir": str(task_dir / "output"),
            "tests_dir": str(task_dir / "tests"),
            "reports_dir": str(task_dir / "reports"),
            "logs_dir": str(task_dir / "logs"),
            "scratch_dir": str(task_dir / "scratch"),
            "status_file": str(task_dir / "STATUS.md"),
            "work_log_file": str(task_dir / "WORK_LOG.md"),
            "action_receipts_file": str(task_dir / "ACTION_RECEIPTS.md"),
            "acceptance_file": str(task_dir / "ACCEPTANCE.md"),
            "test_checklist_file": str(task_dir / "TEST_CHECKLIST.md"),
            "bugs_file": str(task_dir / "BUGS.md"),
            "skill_usage_file": str(task_dir / "SKILL_USAGE.md"),
            "handoff_file": str(task_dir / "HANDOFF.md"),
            "debrief_file": str(task_dir / "DEBRIEF.md"),
            "output_json": str(task_dir / "output.json"),
            "dependencies_json": str(task_dir / "dependencies.json"),
            "takeover_file": str(task_dir / "TAKEOVER.md"),
            "channel_probe_file": str(task_dir / "CHANNEL_PROBE.md"),
            "execution_context_file": str(task_dir / "EXECUTION_CONTEXT.md"),
            "execution_context_json": str(task_dir / "execution_context.json"),
            "runner_result_file": str(task_dir / "RUNNER_RESULT.md"),
            "runner_result_json": str(task_dir / "reports" / "runner_result.json"),
            "runner_prompt_file": str(task_dir / "logs" / "runner_prompt.md"),
            "runner_response_file": str(task_dir / "logs" / "runner_response.md"),
        }
        paths["allowed_write_roots"] = [str(task_dir)]
        paths["forbidden_write_roots"] = _default_forbidden_write_roots()
        return paths

    def _ensure_work_order_files(self, task: SubAgentTask) -> None:
        """初始化标准工单目录和最小文件。"""

        for directory in [
            task.data_dir,
            task.output_dir,
            task.tests_dir,
            task.reports_dir,
            task.logs_dir,
            task.scratch_dir,
        ]:
            Path(directory).mkdir(parents=True, exist_ok=True)

        _write_if_missing(
            Path(task.status_file),
            "# STATUS\n\n"
            f"- id: {task.id}\n"
            f"- status: {task.status}\n"
            f"- owner: {task.owner or 'none'}\n"
            f"- supervisor: {task.supervisor or 'none'}\n"
            f"- final_owner: {task.final_owner or 'none'}\n"
            f"- updated_at: {task.updated_at or task.created_at}\n",
        )
        _write_if_missing(
            Path(task.work_log_file),
            "# WORK_LOG\n\n"
            f"- {time.strftime('%Y-%m-%d %H:%M:%S')} 创建工单 {task.id}\n",
        )
        _write_if_missing(
            Path(task.action_receipts_file),
            "# ACTION_RECEIPTS\n\n"
            "每轮推进后追加一条 receipt，避免压缩或跨天后丢失现场。\n\n"
            "## Template\n\n"
            "- time: \n"
            "- action: \n"
            "- evidence: \n"
            "- failure_or_fallback: \n"
            "- next: \n",
        )
        _write_if_missing(
            Path(task.acceptance_file),
            "# ACCEPTANCE\n\n"
            "## Checks\n"
            + "\n".join(f"- [ ] {item}" for item in task.acceptance_checks or ["未设置"])
            + "\n\n## Evidence\n\n- 暂无\n",
        )
        _write_if_missing(
            Path(task.test_checklist_file),
            "# TEST_CHECKLIST\n\n"
            "## From Requirement\n\n"
            "- [ ] 原始需求已转成可测试清单\n"
            "- [ ] P0/P1 验收标准已明确\n\n"
            "## Entrypoints\n\n"
            "- [ ] CLI/API/Web/文件入口已实际运行\n"
            "- [ ] 异常路径和边界输入已覆盖\n\n"
            "## Evidence\n\n"
            "- [ ] 测试命令、日志、截图或报告路径已记录\n",
        )
        _write_if_missing(
            Path(task.bugs_file),
            "# BUGS\n\n"
            "## Open P0/P1\n\n- 暂无\n\n"
            "## Non-blocking\n\n- 暂无\n",
        )
        _write_if_missing(
            Path(task.skill_usage_file),
            "# SKILL_USAGE\n\n"
            "记录本任务匹配、读取和实际使用过的 skill / references / 外部知识库。\n\n"
            "## Used\n\n- 暂无\n\n"
            "## Considered But Not Used\n\n- 暂无\n",
        )
        _write_if_missing(
            Path(task.handoff_file),
            "# HANDOFF\n\n"
            "## Current State\n\n- 待填写\n\n"
            "## Done\n\n- 待填写\n\n"
            "## Not Done\n\n- 待填写\n\n"
            "## Next Step\n\n- 待填写\n\n"
            "## Recovery Entry\n\n"
            f"- status: {task.status_file}\n"
            f"- acceptance: {task.acceptance_file}\n"
            f"- tests: {task.test_checklist_file}\n",
        )
        _write_if_missing(
            Path(task.debrief_file),
            "# DEBRIEF\n\n"
            "## 方法\n\n- 待填写\n\n"
            "## 结果\n\n- 待填写\n\n"
            "## 可沉淀经验\n\n- 待填写\n",
        )
        _write_json_if_missing(
            Path(task.output_json),
            {
                "run_id": task.id,
                "status": task.status,
                "artifacts": [],
                "tests": [],
                "acceptance": [],
                "blockers": [],
                "next_action": "",
            },
        )
        _write_json_if_missing(
            Path(task.dependencies_json),
            {
                "run_id": task.id,
                "dependencies": [],
            },
        )

    def _write_takeover_file(self, task: SubAgentTask, record: TakeoverRecord) -> None:
        """写入接管记录文件。"""

        content = (
            "# TAKEOVER\n\n"
            f"- takeover_id: {record.id}\n"
            f"- run_id: {record.run_id}\n"
            f"- take_over_by: {record.take_over_by}\n"
            f"- previous_owner: {record.previous_owner or 'none'}\n"
            f"- reason: {record.reason}\n"
            f"- created_at: {record.created_at}\n\n"
            "## Locked Files\n"
            + "\n".join(f"- {item}" for item in record.locked_files or ["none"])
            + "\n\n"
            "## Rule\n\n"
            "- 接管后，原子代理不得继续写 locked_files 中的文件。\n"
            "- 后续写入必须由 final_owner 或接管者统一收口。\n"
        )
        Path(task.takeover_file).write_text(content, encoding="utf-8")

    def validate_work_order(self, run_id: str) -> WorkOrderValidation:
        """检查子代理工单目录是否具备最小可接管结构。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        required_paths = [
            task.task_dir,
            task.data_dir,
            task.output_dir,
            task.tests_dir,
            task.reports_dir,
            task.logs_dir,
            task.scratch_dir,
            task.status_file,
            task.work_log_file,
            task.action_receipts_file,
            task.acceptance_file,
            task.test_checklist_file,
            task.bugs_file,
            task.skill_usage_file,
            task.handoff_file,
            task.debrief_file,
            task.output_json,
            task.dependencies_json,
        ]
        if task.takeover_by:
            required_paths.append(task.takeover_file)
        missing = [item for item in required_paths if item and not Path(item).exists()]
        warnings: list[str] = []
        if not task.allowed_write_roots:
            warnings.append("未设置 allowed_write_roots")
        if not task.forbidden_write_roots:
            warnings.append("未设置 forbidden_write_roots")
        return WorkOrderValidation(run_id=run_id, ok=not missing, missing=missing, warnings=warnings)

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ) -> TakeoverRecord:
        """记录一次接管，并写入 TAKEOVER.md。

        这一步不真的杀掉子代理进程，但会把所有权和锁文件写成事实。
        后续执行器看到 `TAKEN_OVER` 或 locked files 时，就能避免双写。
        """

        task = self.load(run_id)
        record = TakeoverRecord(
            id=_new_id("takeover"),
            run_id=run_id,
            take_over_by=take_over_by,
            reason=reason,
            locked_files=locked_files or [],
            previous_owner=task.owner,
            created_at=time.time(),
        )
        task.takeover_records.append(record)
        task.takeover_by = take_over_by
        task.takeover_reason = reason
        task.locked_files = _merge_list(task.locked_files, record.locked_files)
        task.final_owner = take_over_by
        task.status = "TAKEN_OVER"
        task.updated_at = time.time()
        self.save(task)
        self._write_takeover_file(task, record)
        return record
