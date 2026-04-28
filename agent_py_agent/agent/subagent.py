from __future__ import annotations

"""子代理运行记录和能力协商模块。

当前项目还不会真的启动独立子代理，但这里已经按“未来能运行”的方式打底：
- 子代理有自己的运行记录和父子关系。
- 子代理可以记录允许使用的 skill/tool。
- 子代理遇到能力缺口时，可以生成 capability request。
- 父代理或上级代理可以下发 capability grant。
- 最终无法解决的缺口会变成 capability gap，给自学习系统使用。
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .capabilities import CapabilityRouter, CapabilitySearchHit
from .capability_config import CapabilityConfig


@dataclass
class SubAgentCard:
    """子代理角色卡。

    它描述的是“这个子代理适合干什么，以及默认有哪些边界”。
    后续可以从文件加载，也可以由父代理临时生成。
    """

    name: str
    description: str
    role: str = "general"
    default_model: str = "inherit"
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    can_write: bool = False
    can_spawn_children: bool = False
    can_request_capability: bool = True
    max_depth: int = 0
    result_contract: list[str] = field(default_factory=list)


@dataclass
class CapabilityRequest:
    """子代理向父代理上抛的能力请求。"""

    id: str
    from_run_id: str
    problem: str
    needed_capability: str
    expected_output: str = ""
    tried: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class CapabilityGrant:
    """上级代理下发给子代理的能力授权。"""

    id: str
    request_id: str
    grant_to_run_id: str
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    capability_cards: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    constraints: dict[str, str] = field(default_factory=dict)
    expires_after_task: bool = True
    created_at: float = 0.0


@dataclass
class CapabilityGap:
    """最终没找到 skill/tool 时留下的能力缺口。"""

    id: str
    run_id: str
    missing_capability: str
    source_task: str
    why_failed: str
    attempted_skills: list[str] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    needed_outputs: list[str] = field(default_factory=list)
    suggested_skill: str = ""
    suggested_tool: str = ""
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class VerificationEvidence:
    """子代理交付物的验收证据。"""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True
    created_at: float = 0.0


@dataclass
class WorkOrderValidation:
    """工单目录校验结果。"""

    run_id: str
    ok: bool
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TakeoverRecord:
    """父代理或上级代理接管子代理任务的记录。"""

    id: str
    run_id: str
    take_over_by: str
    reason: str
    locked_files: list[str] = field(default_factory=list)
    previous_owner: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeCheck:
    """通道健康检查中的单项结果。"""

    name: str
    ok: bool
    summary: str
    severity: str = "P1"
    evidence_path: str = ""
    error: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeResult:
    """单个子代理运行的通道健康检查结果。"""

    run_id: str
    channel_status: str
    checks: list[ChannelProbeCheck]
    task_dir: str = ""
    goal: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeReport:
    """批量通道健康检查报告。"""

    generated_at: float
    summary: dict[str, int]
    results: list[ChannelProbeResult]


@dataclass
class SubAgentBoardItem:
    """子代理看板里的一行机器事实。"""

    id: str
    root_id: str
    parent_id: str
    depth: int
    status: str
    verification_status: str
    channel_status: str
    owner: str
    supervisor: str
    final_owner: str
    goal: str
    updated_at: float
    heartbeat_at: float
    evidence_count: int
    open_request_count: int
    open_gap_count: int
    child_count: int
    takeover_by: str
    locked_file_count: int
    risk_flags: list[str]
    task_dir: str
    output_json: str


@dataclass
class SubAgentBoard:
    """子代理看板，兼顾机器读取和人类扫视。"""

    generated_at: float
    summary: dict[str, int]
    hot_list: list[SubAgentBoardItem]
    recent: list[SubAgentBoardItem]
    items: list[SubAgentBoardItem]


@dataclass
class DueCheckIssue:
    """父代理巡检发现的一条待处理问题。"""

    run_id: str
    severity: str
    kind: str
    message: str
    suggested_action: str
    status: str = ""
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    goal: str = ""
    task_dir: str = ""
    risk_flags: list[str] = field(default_factory=list)
    evidence_count: int = 0
    open_request_count: int = 0
    open_gap_count: int = 0
    age_seconds: float = 0.0
    stale_seconds: float = 0.0
    created_at: float = 0.0


@dataclass
class DueCheckReport:
    """父代理 due-check 报告。

    这份报告是后续自动接管、重派、缩小目标和能力路由的机器输入。
    """

    generated_at: float
    summary: dict[str, int]
    issues: list[DueCheckIssue]


@dataclass
class ActionPlanItem:
    """由 due-check 转出来的一条 dry-run 动作。"""

    id: str
    run_id: str
    severity: str
    priority: int
    action: str
    reason: str
    source_issue_kinds: list[str]
    suggested_commands: list[str] = field(default_factory=list)
    would_change_status_to: str = ""
    requires_confirmation: bool = True
    dry_run: bool = True
    owner: str = ""
    final_owner: str = ""
    task_dir: str = ""
    created_at: float = 0.0


@dataclass
class ActionPlanReport:
    """父代理动作计划报告。

    当前只用于 dry-run，不直接修改任何子代理运行状态。
    """

    generated_at: float
    summary: dict[str, int]
    actions: list[ActionPlanItem]


@dataclass
class ActionApplyRecord:
    """一次 action apply 的审计记录。"""

    id: str
    action_id: str
    run_id: str
    action: str
    dry_run: bool
    applied: bool
    ok: bool
    message: str
    before_status: str = ""
    after_status: str = ""
    before_channel_status: str = ""
    after_channel_status: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class ActionApplyReport:
    """action apply 报告。

    dry-run 时只说明会做什么；apply 时才会真的修改子代理运行记录。
    """

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ActionApplyRecord]


@dataclass
class CapabilityRouteRecord:
    """一次 capability request 路由记录。"""

    id: str
    run_id: str
    request_id: str
    status: str
    dry_run: bool
    query: str
    candidate_count: int
    granted_skills: list[str] = field(default_factory=list)
    granted_tools: list[str] = field(default_factory=list)
    selected_cards: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    grant_id: str = ""
    gap_id: str = ""
    message: str = ""
    created_at: float = 0.0


@dataclass
class CapabilityRouteReport:
    """能力请求路由报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[CapabilityRouteRecord]


@dataclass
class SubAgentExecutionContext:
    """下发给子代理执行器的瘦身上下文。

    它不是全局 skill/tool 清单，而是父代理确认过的最小授权包。
    未来真正启动子代理时，runner 应优先读取这份上下文。
    """

    run_id: str
    generated_at: float
    goal: str
    thought: str
    plan: list[str]
    agent_name: str = "general"
    role: str = "general"
    status: str = "PLANNING"
    verification_status: str = "UNVERIFIED"
    channel_status: str = "UNKNOWN"
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    parent_id: str = ""
    root_id: str = ""
    depth: int = 0
    task_dir: str = ""
    execution_context_file: str = ""
    execution_context_json: str = ""
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    granted_cards: list[dict[str, str]] = field(default_factory=list)
    grants: list[dict[str, object]] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    write_boundary: dict[str, object] = field(default_factory=dict)
    pending_requests: list[dict[str, object]] = field(default_factory=list)
    open_gaps: list[dict[str, object]] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)


@dataclass
class SubAgentRunnerResult:
    """一次子代理 runner 入口调用的结果。"""

    run_id: str
    dry_run: bool
    ok: bool
    status: str
    verification_status: str
    message: str
    backend: str = ""
    tool_rounds: int = 0
    execution_context_json: str = ""
    execution_context_file: str = ""
    prompt_file: str = ""
    response_file: str = ""
    result_file: str = ""
    result_json: str = ""
    output_json: str = ""
    created_at: float = 0.0


@dataclass
class SubAgentTask:
    """子代理运行记录。

    名字继续叫 `SubAgentTask` 是为了兼容现有代码和测试；
    实际上它已经是一个轻量 SubAgentRun。
    """

    id: str
    goal: str
    thought: str
    plan: list[str]
    agent_name: str = "general"
    role: str = "general"
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    parent_id: str = ""
    root_id: str = ""
    depth: int = 0
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    used_skills: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    capability_requests: list[CapabilityRequest] = field(default_factory=list)
    capability_grants: list[CapabilityGrant] = field(default_factory=list)
    capability_gaps: list[CapabilityGap] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    evidence: list[VerificationEvidence] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)
    status: str = "PLANNING"
    verification_status: str = "UNVERIFIED"
    failure_type: str = ""
    result: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    heartbeat_at: float = 0.0
    ended_at: float = 0.0
    task_dir: str = ""
    data_dir: str = ""
    output_dir: str = ""
    tests_dir: str = ""
    reports_dir: str = ""
    logs_dir: str = ""
    scratch_dir: str = ""
    status_file: str = ""
    work_log_file: str = ""
    acceptance_file: str = ""
    debrief_file: str = ""
    output_json: str = ""
    dependencies_json: str = ""
    takeover_file: str = ""
    execution_context_file: str = ""
    execution_context_json: str = ""
    runner_result_file: str = ""
    runner_result_json: str = ""
    runner_prompt_file: str = ""
    runner_response_file: str = ""
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_write_roots: list[str] = field(default_factory=list)
    takeover_by: str = ""
    takeover_reason: str = ""
    locked_files: list[str] = field(default_factory=list)
    takeover_records: list[TakeoverRecord] = field(default_factory=list)
    channel_status: str = "UNKNOWN"
    last_probe_at: float = 0.0
    channel_checks: list[ChannelProbeCheck] = field(default_factory=list)
    channel_probe_file: str = ""


class SubAgentManager:
    """负责创建、保存和更新子代理运行记录。"""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cards: dict[str, SubAgentCard] = {}

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
        data["capability_requests"] = [
            CapabilityRequest(**item) for item in data.get("capability_requests", [])
        ]
        data["capability_grants"] = [
            CapabilityGrant(**item) for item in data.get("capability_grants", [])
        ]
        data["capability_gaps"] = [
            CapabilityGap(**item) for item in data.get("capability_gaps", [])
        ]
        data["evidence"] = [VerificationEvidence(**item) for item in data.get("evidence", [])]
        data["takeover_records"] = [
            TakeoverRecord(**item) for item in data.get("takeover_records", [])
        ]
        data["channel_checks"] = [
            ChannelProbeCheck(**item) for item in data.get("channel_checks", [])
        ]
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

    def record_capability_request(
        self,
        run_id: str,
        *,
        problem: str,
        needed_capability: str,
        expected_output: str = "",
        tried: list[str] | None = None,
        evidence: list[str] | None = None,
        constraints: dict[str, str] | None = None,
    ) -> CapabilityRequest:
        """给某个子代理记录一条能力请求。"""

        task = self.load(run_id)
        request = CapabilityRequest(
            id=_new_id("capreq"),
            from_run_id=run_id,
            problem=problem,
            needed_capability=needed_capability,
            expected_output=expected_output,
            tried=tried or [],
            evidence=evidence or [],
            constraints=constraints or {},
            created_at=time.time(),
        )
        task.capability_requests.append(request)
        task.updated_at = time.time()
        self.save(task)
        return request

    def record_capability_grant(
        self,
        run_id: str,
        *,
        request_id: str,
        skills: list[str] | None = None,
        tools: list[str] | None = None,
        capability_cards: list[dict[str, str]] | None = None,
        reason: str = "",
        constraints: dict[str, str] | None = None,
        expires_after_task: bool = True,
    ) -> CapabilityGrant:
        """给某个子代理记录一条能力授权。"""

        task = self.load(run_id)
        grant = CapabilityGrant(
            id=_new_id("capgrant"),
            request_id=request_id,
            grant_to_run_id=run_id,
            skills=skills or [],
            tools=tools or [],
            capability_cards=capability_cards or [],
            reason=reason,
            constraints=constraints or {},
            expires_after_task=expires_after_task,
            created_at=time.time(),
        )
        task.capability_grants.append(grant)
        task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
        task.allowed_tools = _merge_list(task.allowed_tools, grant.tools)
        task.updated_at = time.time()
        self.save(task)
        return grant

    def record_capability_gap(
        self,
        run_id: str,
        *,
        missing_capability: str,
        why_failed: str,
        attempted_skills: list[str] | None = None,
        attempted_tools: list[str] | None = None,
        needed_outputs: list[str] | None = None,
        suggested_skill: str = "",
        suggested_tool: str = "",
    ) -> CapabilityGap:
        """给某个子代理记录一条能力缺口。"""

        task = self.load(run_id)
        gap = CapabilityGap(
            id=_new_id("capgap"),
            run_id=run_id,
            missing_capability=missing_capability,
            source_task=task.goal,
            why_failed=why_failed,
            attempted_skills=attempted_skills or [],
            attempted_tools=attempted_tools or [],
            needed_outputs=needed_outputs or [],
            suggested_skill=suggested_skill,
            suggested_tool=suggested_tool,
            created_at=time.time(),
        )
        task.capability_gaps.append(gap)
        task.updated_at = time.time()
        self.save(task)
        return gap

    def record_evidence(
        self,
        run_id: str,
        *,
        kind: str,
        summary: str,
        command: str = "",
        path: str = "",
        url: str = "",
        ok: bool = True,
    ) -> VerificationEvidence:
        """记录一条验收证据。"""

        task = self.load(run_id)
        evidence = VerificationEvidence(
            kind=kind,
            summary=summary,
            command=command,
            path=path,
            url=url,
            ok=ok,
            created_at=time.time(),
        )
        task.evidence.append(evidence)
        task.verification_status = "VERIFIED" if ok else "FAILED"
        task.updated_at = time.time()
        self.save(task)
        return evidence

    def touch_heartbeat(self, run_id: str) -> None:
        """刷新子代理心跳时间。"""

        task = self.load(run_id)
        task.heartbeat_at = time.time()
        task.updated_at = task.heartbeat_at
        self.save(task)

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ) -> SubAgentTask:
        """更新任务状态。

        `require_evidence=True` 时，没有验收证据不能标记为 DONE。
        这是防 Fake Done 的第一道硬约束。
        """

        task = self.load(run_id)
        normalized = status.upper()
        if require_evidence and normalized == "DONE" and not task.evidence:
            raise ValueError("缺少验收证据，不能标记为 DONE。")
        task.status = normalized
        if result:
            task.result = result
        if failure_type:
            task.failure_type = failure_type
        if normalized in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
            task.ended_at = time.time()
        task.updated_at = time.time()
        self.save(task)
        return task

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

    def validate_work_order(self, run_id: str) -> WorkOrderValidation:
        """检查子代理工单目录是否具备最小可接管结构。"""

        task = self.load(run_id)
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
            task.acceptance_file,
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

    def build_board(self, *, recent_limit: int = 20) -> SubAgentBoard:
        """构建子代理红绿灯看板。"""

        items = [self._to_board_item(task) for task in self.list_runs()]
        summary: dict[str, int] = {"total": len(items)}
        for item in items:
            summary[item.status] = summary.get(item.status, 0) + 1
            summary[item.verification_status] = summary.get(item.verification_status, 0) + 1
            summary[f"channel_{item.channel_status}"] = (
                summary.get(f"channel_{item.channel_status}", 0) + 1
            )
        hot_list = [item for item in items if item.risk_flags]
        hot_list.sort(key=lambda item: (-_risk_weight(item.risk_flags), -(item.updated_at or 0)))
        recent = items[:recent_limit]
        return SubAgentBoard(
            generated_at=time.time(),
            summary=summary,
            hot_list=hot_list,
            recent=recent,
            items=items,
        )

    def write_board(self, *, recent_limit: int = 20) -> SubAgentBoard:
        """写出机器 JSON 和人类 Markdown 看板。"""

        board = self.build_board(recent_limit=recent_limit)
        (self.workspace / "subagent_board.json").write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_BOARD.md").write_text(
            render_board_markdown(board),
            encoding="utf-8",
        )
        return board

    def due_check(self, config: CapabilityConfig | None = None) -> DueCheckReport:
        """巡检所有子代理运行，找出需要父代理介入的事项。

        它只产出判断和建议，不会自动接管或重派。
        这样可以先把“应该处理谁”做准，再把自动动作接上去。
        """

        cfg = config or CapabilityConfig()
        now = time.time()
        heartbeat_timeout = cfg.subagent_heartbeat_timeout
        run_timeout = cfg.subagent_run_timeout
        min_evidence = cfg.subagent_min_evidence_for_done
        issues: list[DueCheckIssue] = []

        for task in self.list_runs():
            open_request_count = sum(
                1 for item in task.capability_requests if item.status == "OPEN"
            )
            open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
            risk_flags = self._risk_flags(
                task,
                open_request_count=open_request_count,
                open_gap_count=open_gap_count,
            )
            age_seconds = max(0.0, now - (task.created_at or now))
            stale_seconds = max(0.0, now - (task.heartbeat_at or task.updated_at or now))

            validation = self.validate_work_order(task.id)
            if not validation.ok:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="missing_work_order_files",
                        message=f"工单目录缺少 {len(validation.missing)} 个关键路径，后续接管和验收不可靠。",
                        suggested_action="repair_work_order",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if task.status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED"}:
                severity = {
                    "FAILED": "P0",
                    "TIMEOUT": "P0",
                    "CHANNEL_ERROR": "P0",
                    "BLOCKED": "P1",
                }[task.status]
                action = {
                    "FAILED": "inspect_failure_and_reassign_or_takeover",
                    "TIMEOUT": "shrink_scope_or_takeover",
                    "CHANNEL_ERROR": "probe_channel_before_reassign",
                    "BLOCKED": "classify_blocker_and_route_capability",
                }[task.status]
                issues.append(
                    _make_due_issue(
                        task,
                        severity=severity,
                        kind=f"status_{task.status.lower()}",
                        message=f"任务状态为 {task.status}，需要父代理确认原因，不能当作完成。",
                        suggested_action=action,
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if task.channel_status == "BROKEN":
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="channel_broken",
                        message="最近一次通道检查为 BROKEN，优先修复 runtime / workdir / JSON 现场。",
                        suggested_action="run_channel_probe_and_fix_runtime",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if task.channel_status == "DEGRADED":
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P1",
                        kind="channel_degraded",
                        message="最近一次通道检查为 DEGRADED，建议先修复弱项再继续派工。",
                        suggested_action="inspect_channel_probe_evidence",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if task.status == "CHANNEL_ERROR" and not task.last_probe_at:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="channel_probe_missing",
                        message="任务状态为 CHANNEL_ERROR，但还没有 probe 证据。",
                        suggested_action="run_channel_probe",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if task.status == "DONE" and min_evidence > 0 and len(task.evidence) < min_evidence:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="fake_done_risk",
                        message=(
                            f"DONE 任务只有 {len(task.evidence)} 条证据，"
                            f"少于配置要求的 {min_evidence} 条。"
                        ),
                        suggested_action="require_evidence_or_reopen",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if task.status == "DONE" and task.verification_status != "VERIFIED":
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P1",
                        kind="unverified_done",
                        message="任务已标记 DONE，但 verification_status 还不是 VERIFIED。",
                        suggested_action="run_acceptance_or_assign_reviewer",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if open_request_count:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P1",
                        kind="open_capability_request",
                        message=f"存在 {open_request_count} 条未处理能力请求。",
                        suggested_action="route_capability_request",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if open_gap_count:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P2",
                        kind="open_capability_gap",
                        message=f"存在 {open_gap_count} 条未关闭能力缺口。",
                        suggested_action="triage_gap_for_learning_or_tooling",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if _is_active(task.status):
                if heartbeat_timeout > 0 and stale_seconds > heartbeat_timeout:
                    severity = "P0" if stale_seconds > heartbeat_timeout * 3 else "P1"
                    issues.append(
                        _make_due_issue(
                            task,
                            severity=severity,
                            kind="heartbeat_stale",
                            message=(
                                f"心跳已停滞 {stale_seconds:.0f}s，"
                                f"超过配置阈值 {heartbeat_timeout}s。"
                            ),
                            suggested_action="check_runtime_or_takeover",
                            risk_flags=risk_flags,
                            open_request_count=open_request_count,
                            open_gap_count=open_gap_count,
                            age_seconds=age_seconds,
                            stale_seconds=stale_seconds,
                        )
                    )
                if run_timeout > 0 and age_seconds > run_timeout:
                    issues.append(
                        _make_due_issue(
                            task,
                            severity="P0",
                            kind="run_timeout",
                            message=(
                                f"任务已运行 {age_seconds:.0f}s，"
                                f"超过配置阈值 {run_timeout}s。"
                            ),
                            suggested_action="shrink_scope_reassign_or_takeover",
                            risk_flags=risk_flags,
                            open_request_count=open_request_count,
                            open_gap_count=open_gap_count,
                            age_seconds=age_seconds,
                            stale_seconds=stale_seconds,
                        )
                    )

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        summary: dict[str, int] = {"total": len(issues)}
        for issue in issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1
            summary[issue.kind] = summary.get(issue.kind, 0) + 1
        return DueCheckReport(generated_at=now, summary=summary, issues=issues)

    def write_due_check(self, config: CapabilityConfig | None = None) -> DueCheckReport:
        """写出 due-check JSON 和 Markdown 报告。"""

        report = self.due_check(config)
        (self.workspace / "subagent_due_check.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_DUE_CHECK.md").write_text(
            render_due_check_markdown(report),
            encoding="utf-8",
        )
        return report

    def plan_actions(self, config: CapabilityConfig | None = None) -> ActionPlanReport:
        """把 due-check 问题转成 dry-run 动作清单。

        这里故意只生成计划，不自动修改任务。
        后续真正接 takeover / reassign / capability routing 时，再按这些 action 增加 apply 层。
        """

        due_report = self.due_check(config)
        merged: dict[tuple[str, str], ActionPlanItem] = {}
        for issue in due_report.issues:
            action, priority, would_change_status_to = _action_for_issue(issue)
            key = (issue.run_id, action)
            if key not in merged:
                merged[key] = ActionPlanItem(
                    id=_new_id("action"),
                    run_id=issue.run_id,
                    severity=issue.severity,
                    priority=priority,
                    action=action,
                    reason=issue.message,
                    source_issue_kinds=[issue.kind],
                    suggested_commands=_commands_for_action(action, issue.run_id),
                    would_change_status_to=would_change_status_to,
                    owner=issue.owner,
                    final_owner=issue.final_owner,
                    task_dir=issue.task_dir,
                    created_at=time.time(),
                )
                continue
            item = merged[key]
            item.source_issue_kinds = _merge_list(item.source_issue_kinds, [issue.kind])
            item.reason = f"{item.reason} / {issue.message}"
            if _severity_weight(issue.severity) > _severity_weight(item.severity):
                item.severity = issue.severity
            item.priority = max(item.priority, priority)

        actions = list(merged.values())
        actions.sort(key=lambda item: (-item.priority, item.run_id, item.action))
        summary: dict[str, int] = {"total": len(actions)}
        for action in actions:
            summary[action.severity] = summary.get(action.severity, 0) + 1
            summary[action.action] = summary.get(action.action, 0) + 1
        return ActionPlanReport(
            generated_at=time.time(),
            summary=summary,
            actions=actions,
        )

    def write_action_plan(self, config: CapabilityConfig | None = None) -> ActionPlanReport:
        """写出 dry-run 动作计划 JSON 和 Markdown。"""

        report = self.plan_actions(config)
        (self.workspace / "subagent_action_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_ACTION_PLAN.md").write_text(
            render_action_plan_markdown(report),
            encoding="utf-8",
        )
        return report

    def apply_actions(
        self,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        action_filter: str = "",
        run_id: str = "",
        take_over_by: str = "",
        locked_files: list[str] | None = None,
        limit: int = 0,
    ) -> ActionApplyReport:
        """执行或 dry-run 执行动作计划。

        默认 `apply=False`，只产出会做什么。
        真正执行时只支持低风险动作，并把所有动作写入审计日志。
        """

        plan = self.plan_actions(config)
        actions = _filter_action_plan_items(
            plan.actions,
            action_filter=action_filter,
            run_id=run_id,
            limit=limit,
        )
        records: list[ActionApplyRecord] = []
        for action in actions:
            record = self._apply_action_item(
                action,
                apply=apply,
                take_over_by=take_over_by,
                locked_files=locked_files or [],
            )
            records.append(record)
            if apply:
                self._append_action_apply_log(record)

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.action] = summary.get(record.action, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["applied" if record.applied else "dry_run"] = summary.get(
                "applied" if record.applied else "dry_run",
                0,
            ) + 1
        return ActionApplyReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_action_apply_report(
        self,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        action_filter: str = "",
        run_id: str = "",
        take_over_by: str = "",
        locked_files: list[str] | None = None,
        limit: int = 0,
    ) -> ActionApplyReport:
        """写出 action apply 报告。"""

        report = self.apply_actions(
            config,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files or [],
            limit=limit,
        )
        (self.workspace / "subagent_action_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_ACTION_APPLY.md").write_text(
            render_action_apply_markdown(report),
            encoding="utf-8",
        )
        return report

    def route_capability_requests(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        """把 OPEN capability request 路由到 skill/tool card。

        默认 dry-run，只展示会下发哪些能力。
        `apply=True` 时才会真正生成 capability grant 或 capability gap。
        """

        cfg = config or CapabilityConfig()
        records: list[CapabilityRouteRecord] = []
        selected_runs = self._select_runs(run_ids)
        for task in selected_runs:
            for request in task.capability_requests:
                if request.status != "OPEN":
                    continue
                query = _capability_request_query(task, request)
                hits = router.search(query, limit=cfg.capability_candidate_limit)
                selected_hits = _select_capability_hits(hits, cfg)
                record = self._route_capability_request(
                    task,
                    request,
                    query=query,
                    hits=hits,
                    selected_hits=selected_hits,
                    apply=apply,
                )
                records.append(record)
                if limit > 0 and len(records) >= limit:
                    break
            if limit > 0 and len(records) >= limit:
                break

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.status] = summary.get(record.status, 0) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
        return CapabilityRouteReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_capability_route_report(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        """写出 capability request 路由报告。"""

        report = self.route_capability_requests(
            router,
            config,
            apply=apply,
            run_ids=run_ids,
            limit=limit,
        )
        (self.workspace / "subagent_capability_route_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_CAPABILITY_ROUTE.md").write_text(
            render_capability_route_markdown(report),
            encoding="utf-8",
        )
        if apply:
            for record in report.records:
                self._append_capability_route_log(record)
        return report

    def build_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        """生成单个子代理执行器可读取的最小上下文。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        granted_skills: list[str] = []
        granted_tools: list[str] = []
        grants: list[dict[str, object]] = []
        for grant in task.capability_grants:
            granted_skills = _merge_list(granted_skills, grant.skills)
            granted_tools = _merge_list(granted_tools, grant.tools)
            grants.append(
                {
                    "id": grant.id,
                    "request_id": grant.request_id,
                    "skills": grant.skills,
                    "tools": grant.tools,
                    "reason": grant.reason,
                    "constraints": grant.constraints,
                    "expires_after_task": grant.expires_after_task,
                    "created_at": grant.created_at,
                }
            )

        allowed_skills = _merge_list(task.allowed_skills, granted_skills)
        allowed_tools = _merge_list(task.allowed_tools, granted_tools)
        return SubAgentExecutionContext(
            run_id=task.id,
            generated_at=time.time(),
            goal=task.goal,
            thought=task.thought,
            plan=task.plan,
            agent_name=task.agent_name,
            role=task.role,
            status=task.status,
            verification_status=task.verification_status,
            channel_status=task.channel_status,
            owner=task.owner,
            supervisor=task.supervisor,
            final_owner=task.final_owner,
            parent_id=task.parent_id,
            root_id=task.root_id,
            depth=task.depth,
            task_dir=task.task_dir,
            execution_context_file=task.execution_context_file,
            execution_context_json=task.execution_context_json,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
            granted_cards=_dedupe_granted_cards(task.capability_grants, max_cards=max_cards),
            grants=grants,
            acceptance_checks=task.acceptance_checks,
            evidence=[asdict(item) for item in task.evidence],
            write_boundary={
                "task_dir": task.task_dir,
                "allowed_write_roots": task.allowed_write_roots,
                "forbidden_write_roots": task.forbidden_write_roots,
                "locked_files": task.locked_files,
                "status_file": task.status_file,
                "work_log_file": task.work_log_file,
                "acceptance_file": task.acceptance_file,
                "debrief_file": task.debrief_file,
                "output_json": task.output_json,
                "dependencies_json": task.dependencies_json,
            },
            pending_requests=[
                asdict(item) for item in task.capability_requests if item.status == "OPEN"
            ],
            open_gaps=[asdict(item) for item in task.capability_gaps if item.status == "OPEN"],
            instructions=_execution_context_instructions(),
        )

    def write_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        """写出子代理执行上下文 JSON 和 Markdown。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        context = self.build_execution_context(run_id, max_cards=max_cards)
        Path(context.execution_context_json).write_text(
            json.dumps(asdict(context), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(context.execution_context_file).write_text(
            render_execution_context_markdown(context),
            encoding="utf-8",
        )
        task = self.load(run_id)
        self._append_task_work_log(
            task,
            f"execution_context: 已生成执行上下文，cards={len(context.granted_cards)}。",
        )
        return context

    def record_runner_result(
        self,
        run_id: str,
        *,
        dry_run: bool,
        ok: bool,
        message: str,
        prompt: str = "",
        response: str = "",
        backend: str = "",
        tool_rounds: int = 0,
        status: str = "",
        verification_status: str = "",
        failure_type: str = "",
    ) -> SubAgentRunnerResult:
        """把 runner 调用结果写回标准工单。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        now = time.time()

        if prompt:
            Path(task.runner_prompt_file).write_text(prompt, encoding="utf-8")
        if response:
            Path(task.runner_response_file).write_text(response, encoding="utf-8")

        if status:
            task.status = status.upper()
        if verification_status:
            task.verification_status = verification_status.upper()
        if failure_type:
            task.failure_type = failure_type
        elif not ok:
            task.failure_type = task.failure_type or "runner_error"
        if response:
            task.result = response
        elif message:
            task.result = message
        if task.status in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
            task.ended_at = now
        task.updated_at = now
        task.heartbeat_at = now

        output_payload = {
            "run_id": task.id,
            "dry_run": dry_run,
            "ok": ok,
            "status": task.status,
            "verification_status": task.verification_status,
            "message": message,
            "backend": backend,
            "tool_rounds": tool_rounds,
            "response": response,
            "artifacts": [],
            "tests": [],
            "acceptance": [],
            "blockers": [] if ok else [message],
            "next_action": "run_acceptance" if ok and not dry_run else "",
            "created_at": now,
        }
        Path(task.output_json).write_text(
            json.dumps(output_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = SubAgentRunnerResult(
            run_id=task.id,
            dry_run=dry_run,
            ok=ok,
            status=task.status,
            verification_status=task.verification_status,
            message=message,
            backend=backend,
            tool_rounds=tool_rounds,
            execution_context_json=task.execution_context_json,
            execution_context_file=task.execution_context_file,
            prompt_file=task.runner_prompt_file if prompt else "",
            response_file=task.runner_response_file if response else "",
            result_file=task.runner_result_file,
            result_json=task.runner_result_json,
            output_json=task.output_json,
            created_at=now,
        )
        Path(task.runner_result_json).write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(task.runner_result_file).write_text(
            render_runner_result_markdown(result),
            encoding="utf-8",
        )
        self.save(task)
        self._append_task_work_log(
            task,
            f"subagent_runner: dry_run={dry_run} ok={ok} status={task.status} message={message}",
        )
        return result

    def probe_channel(self, run_id: str) -> ChannelProbeResult:
        """检查单个子代理运行的通道健康状态。

        这里的“通道”先指最基础的运行现场：
        工单文件、机器 JSON、任务目录写入和 probe 证据落盘。
        后续真正接入执行器时，再把模型 session、ACP adapter 等检查接进来。
        """

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        now = time.time()
        checks: list[ChannelProbeCheck] = []

        validation = self.validate_work_order(run_id)
        if validation.ok:
            checks.append(
                _probe_ok(
                    "work_order_files",
                    "标准工单目录和关键文件完整。",
                    severity="P0",
                    evidence_path=task.task_dir,
                    created_at=now,
                )
            )
        else:
            checks.append(
                _probe_fail(
                    "work_order_files",
                    f"缺少 {len(validation.missing)} 个关键路径。",
                    severity="P0",
                    error="; ".join(validation.missing[:10]),
                    evidence_path=task.task_dir,
                    created_at=now,
                )
            )

        checks.append(
            _probe_json_file("task_json_readable", Path(task.task_dir) / "task.json", "P0", now)
        )
        checks.append(
            _probe_json_file("run_json_readable", Path(task.task_dir) / "run.json", "P1", now)
        )
        checks.append(
            _probe_json_file("output_json_readable", Path(task.output_json), "P1", now)
        )
        checks.append(
            _probe_json_file("dependencies_json_readable", Path(task.dependencies_json), "P1", now)
        )
        checks.append(_probe_writable_dir("scratch_writable", Path(task.scratch_dir), "P0", now))

        status = _channel_status(checks)
        result = ChannelProbeResult(
            run_id=task.id,
            channel_status=status,
            checks=checks,
            task_dir=task.task_dir,
            goal=task.goal,
            created_at=now,
        )
        write_check = self._write_channel_probe_files(task, result)
        result.checks.append(write_check)
        task.channel_checks = result.checks
        task.channel_status = _channel_status(result.checks)
        result.channel_status = task.channel_status
        task.last_probe_at = now
        task.updated_at = now
        if task.channel_status == "BROKEN":
            task.failure_type = "channel"
        self.save(task)
        return result

    def probe_channels(
        self,
        run_ids: list[str] | None = None,
        *,
        limit: int = 0,
    ) -> ChannelProbeReport:
        """批量检查子代理通道健康状态。"""

        selected = run_ids or [task.id for task in self.list_runs()]
        if limit > 0:
            selected = selected[:limit]
        results: list[ChannelProbeResult] = []
        for run_id in selected:
            try:
                results.append(self.probe_channel(run_id))
            except FileNotFoundError:
                continue
        summary: dict[str, int] = {"total": len(results)}
        for result in results:
            summary[result.channel_status] = summary.get(result.channel_status, 0) + 1
            for check in result.checks:
                if not check.ok:
                    summary[check.name] = summary.get(check.name, 0) + 1
        return ChannelProbeReport(
            generated_at=time.time(),
            summary=summary,
            results=results,
        )

    def write_channel_probe_report(
        self,
        run_ids: list[str] | None = None,
        *,
        limit: int = 0,
    ) -> ChannelProbeReport:
        """写出批量通道健康检查报告。"""

        report = self.probe_channels(run_ids, limit=limit)
        (self.workspace / "subagent_channel_probe.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_CHANNEL_PROBE.md").write_text(
            render_channel_probe_markdown(report),
            encoding="utf-8",
        )
        return report

    def _to_board_item(self, task: SubAgentTask) -> SubAgentBoardItem:
        """把运行记录压缩成看板行。"""

        open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
        open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
        flags = self._risk_flags(task, open_request_count=open_request_count, open_gap_count=open_gap_count)
        return SubAgentBoardItem(
            id=task.id,
            root_id=task.root_id,
            parent_id=task.parent_id,
            depth=task.depth,
            status=task.status,
            verification_status=task.verification_status,
            channel_status=task.channel_status,
            owner=task.owner,
            supervisor=task.supervisor,
            final_owner=task.final_owner,
            goal=task.goal,
            updated_at=task.updated_at,
            heartbeat_at=task.heartbeat_at,
            evidence_count=len(task.evidence),
            open_request_count=open_request_count,
            open_gap_count=open_gap_count,
            child_count=len(task.child_ids),
            takeover_by=task.takeover_by,
            locked_file_count=len(task.locked_files),
            risk_flags=flags,
            task_dir=task.task_dir,
            output_json=task.output_json,
        )

    def _risk_flags(
        self,
        task: SubAgentTask,
        *,
        open_request_count: int,
        open_gap_count: int,
    ) -> list[str]:
        """给看板行打风险标记，让 100+ 子代理时异常能浮上来。"""

        flags: list[str] = []
        if task.status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
            flags.append(task.status.lower())
        if task.status == "DONE" and not task.evidence:
            flags.append("done_without_evidence")
        if task.status == "DONE" and task.verification_status != "VERIFIED":
            flags.append("done_without_verification")
        if open_request_count:
            flags.append("open_capability_request")
        if open_gap_count:
            flags.append("open_capability_gap")
        if task.takeover_by:
            flags.append("taken_over")
        if task.channel_status == "BROKEN":
            flags.append("channel_broken")
        if task.channel_status == "DEGRADED":
            flags.append("channel_degraded")
        validation = self.validate_work_order(task.id)
        if not validation.ok:
            flags.append("missing_work_order_files")
        return flags

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
            "acceptance_file": str(task_dir / "ACCEPTANCE.md"),
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

    def _write_channel_probe_files(
        self,
        task: SubAgentTask,
        result: ChannelProbeResult,
    ) -> ChannelProbeCheck:
        """把单个 run 的 probe 证据写入任务目录。"""

        now = time.time()
        try:
            probe_json = Path(task.logs_dir) / "last_channel_probe.json"
            probe_json.parent.mkdir(parents=True, exist_ok=True)
            probe_json.write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            Path(task.channel_probe_file).write_text(
                render_single_channel_probe_markdown(result),
                encoding="utf-8",
            )
            return _probe_ok(
                "probe_evidence_writable",
                "probe 证据已写入任务目录。",
                severity="P1",
                evidence_path=str(probe_json),
                created_at=now,
            )
        except Exception as exc:
            return _probe_fail(
                "probe_evidence_writable",
                "probe 证据无法写入任务目录。",
                severity="P1",
                error=str(exc),
                evidence_path=task.channel_probe_file,
                created_at=now,
            )

    def _apply_action_item(
        self,
        action: ActionPlanItem,
        *,
        apply: bool,
        take_over_by: str,
        locked_files: list[str],
    ) -> ActionApplyRecord:
        """执行单条 action plan item。"""

        now = time.time()
        try:
            task = self.load(action.run_id)
        except FileNotFoundError as exc:
            return ActionApplyRecord(
                id=_new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=not apply,
                applied=False,
                ok=False,
                message=str(exc),
                created_at=now,
            )

        before_status = task.status
        before_channel_status = task.channel_status
        if not apply:
            return ActionApplyRecord(
                id=_new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=True,
                applied=False,
                ok=True,
                message=f"dry-run: would {action.action}",
                before_status=before_status,
                after_status=before_status,
                before_channel_status=before_channel_status,
                after_channel_status=before_channel_status,
                evidence_paths=[task.task_dir],
                created_at=now,
            )

        if action.action in {"probe_or_repair_channel", "inspect_channel_probe"}:
            result = self.probe_channel(action.run_id)
            task = self.load(action.run_id)
            return ActionApplyRecord(
                id=_new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=False,
                applied=True,
                ok=True,
                message=f"已执行 channel probe，结果为 {result.channel_status}。",
                before_status=before_status,
                after_status=task.status,
                before_channel_status=before_channel_status,
                after_channel_status=task.channel_status,
                evidence_paths=[task.channel_probe_file, str(Path(task.logs_dir) / "last_channel_probe.json")],
                created_at=now,
            )

        if action.action == "repair_work_order":
            self.save(task)
            validation = self.validate_work_order(action.run_id)
            task = self.load(action.run_id)
            ok = validation.ok
            message = "已补齐标准工单现场。" if ok else f"工单仍缺少 {len(validation.missing)} 个路径。"
            self._append_task_work_log(task, f"action_apply repair_work_order: {message}")
            return ActionApplyRecord(
                id=_new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=False,
                applied=ok,
                ok=ok,
                message=message,
                before_status=before_status,
                after_status=task.status,
                before_channel_status=before_channel_status,
                after_channel_status=task.channel_status,
                evidence_paths=[task.task_dir, task.work_log_file],
                created_at=now,
            )

        if action.action == "reopen_for_evidence":
            task.status = "BLOCKED"
            task.failure_type = "missing_evidence"
            task.verification_status = "UNVERIFIED"
            task.updated_at = now
            task.result = task.result or "缺少验收证据，等待补充 evidence 后再完成。"
            self.save(task)
            self._append_task_work_log(task, "action_apply reopen_for_evidence: 已重开任务并等待验收证据。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                "已把缺证据的 DONE 任务改为 BLOCKED。",
            )

        if action.action == "run_acceptance":
            task.verification_status = "NEEDS_ACCEPTANCE"
            task.updated_at = now
            self.save(task)
            self._append_task_work_log(task, "action_apply run_acceptance: 已标记为需要验收。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                "已标记为需要验收，未自动执行未知命令。",
            )

        if action.action == "takeover_or_reassign":
            if not take_over_by:
                return ActionApplyRecord(
                    id=_new_id("apply"),
                    action_id=action.id,
                    run_id=action.run_id,
                    action=action.action,
                    dry_run=False,
                    applied=False,
                    ok=False,
                    message="takeover_or_reassign 需要 --take-over-by。",
                    before_status=before_status,
                    after_status=before_status,
                    before_channel_status=before_channel_status,
                    after_channel_status=before_channel_status,
                    evidence_paths=[task.task_dir],
                    created_at=now,
                )
            if task.channel_status != "OK":
                self.probe_channel(action.run_id)
                task = self.load(action.run_id)
            if task.channel_status != "OK":
                return ActionApplyRecord(
                    id=_new_id("apply"),
                    action_id=action.id,
                    run_id=action.run_id,
                    action=action.action,
                    dry_run=False,
                    applied=False,
                    ok=False,
                    message=f"通道状态为 {task.channel_status}，未接管。请先修复通道。",
                    before_status=before_status,
                    after_status=task.status,
                    before_channel_status=before_channel_status,
                    after_channel_status=task.channel_status,
                    evidence_paths=[task.channel_probe_file],
                    created_at=now,
                )
            self.record_takeover(
                action.run_id,
                take_over_by=take_over_by,
                reason=action.reason,
                locked_files=locked_files,
            )
            task = self.load(action.run_id)
            self._append_task_work_log(task, f"action_apply takeover_or_reassign: 已由 {take_over_by} 接管。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                f"已由 {take_over_by} 接管任务。",
                evidence_paths=[task.takeover_file, task.work_log_file],
            )

        if action.action in {
            "route_capability_request",
            "triage_capability_gap",
            "inspect_failure",
            "classify_blocker",
        }:
            task.updated_at = now
            self.save(task)
            self._append_task_work_log(task, f"action_apply {action.action}: 已记录待人工处理，不自动修改能力授权。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                f"已记录 {action.action} 待人工处理。",
            )

        return ActionApplyRecord(
            id=_new_id("apply"),
            action_id=action.id,
            run_id=action.run_id,
            action=action.action,
            dry_run=False,
            applied=False,
            ok=False,
            message=f"暂不支持 apply 动作: {action.action}",
            before_status=before_status,
            after_status=before_status,
            before_channel_status=before_channel_status,
            after_channel_status=before_channel_status,
            evidence_paths=[task.task_dir],
            created_at=now,
        )

    def _record_after_task_action(
        self,
        action: ActionPlanItem,
        task: SubAgentTask,
        before_status: str,
        before_channel_status: str,
        message: str,
        *,
        evidence_paths: list[str] | None = None,
    ) -> ActionApplyRecord:
        """创建修改任务后的 apply 记录。"""

        return ActionApplyRecord(
            id=_new_id("apply"),
            action_id=action.id,
            run_id=action.run_id,
            action=action.action,
            dry_run=False,
            applied=True,
            ok=True,
            message=message,
            before_status=before_status,
            after_status=task.status,
            before_channel_status=before_channel_status,
            after_channel_status=task.channel_status,
            evidence_paths=evidence_paths or [task.work_log_file],
            created_at=time.time(),
        )

    def _append_action_apply_log(self, record: ActionApplyRecord) -> None:
        """写入全局 action apply 审计日志。"""

        jsonl = self.workspace / "subagent_action_apply_log.jsonl"
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        markdown = self.workspace / "ACTION_APPLY_LOG.md"
        if not markdown.exists():
            markdown.write_text("# ACTION APPLY LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} action={record.action} "
                f"applied={record.applied} message={record.message}\n"
            )

    def _append_task_work_log(self, task: SubAgentTask, message: str) -> None:
        """把 apply 过程写入任务自己的 WORK_LOG。"""

        path = Path(task.work_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# WORK_LOG\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    def _select_runs(self, run_ids: list[str] | None) -> list[SubAgentTask]:
        """按 run id 选择运行记录。"""

        if not run_ids:
            return self.list_runs()
        runs: list[SubAgentTask] = []
        for run_id in run_ids:
            try:
                runs.append(self.load(run_id))
            except FileNotFoundError:
                continue
        return runs

    def _route_capability_request(
        self,
        task: SubAgentTask,
        request: CapabilityRequest,
        *,
        query: str,
        hits: list[CapabilitySearchHit],
        selected_hits: list[CapabilitySearchHit],
        apply: bool,
    ) -> CapabilityRouteRecord:
        """路由单条 capability request。"""

        now = time.time()
        selected_cards = [_route_card_payload(hit) for hit in selected_hits]
        granted_skills = [hit.card.name for hit in selected_hits if hit.card.kind == "skill"]
        granted_tools = [hit.card.name for hit in selected_hits if hit.card.kind == "tool"]
        reasons = _merge_list([], [reason for hit in selected_hits for reason in hit.reasons])

        if not selected_hits:
            if not apply:
                return CapabilityRouteRecord(
                    id=_new_id("route"),
                    run_id=task.id,
                    request_id=request.id,
                    status="WOULD_GAP",
                    dry_run=True,
                    query=query,
                    candidate_count=len(hits),
                    message="未找到足够可信的 skill/tool card；apply 时会记录 capability gap。",
                    created_at=now,
                )
            gap = self.record_capability_gap(
                task.id,
                missing_capability=request.needed_capability,
                why_failed="CapabilityRouter 没有找到匹配的 skill/tool card。",
                attempted_tools=request.tried,
                needed_outputs=[request.expected_output] if request.expected_output else [],
            )
            self._mark_capability_request_status(task.id, request.id, "GAP")
            routed_task = self.load(task.id)
            self._append_task_work_log(
                routed_task,
                f"capability_route: request {request.id} 未命中能力卡，已记录 gap {gap.id}。",
            )
            return CapabilityRouteRecord(
                id=_new_id("route"),
                run_id=task.id,
                request_id=request.id,
                status="GAP",
                dry_run=False,
                query=query,
                candidate_count=len(hits),
                gap_id=gap.id,
                message="未找到足够可信的 skill/tool card，已记录 capability gap。",
                created_at=now,
            )

        if not apply:
            return CapabilityRouteRecord(
                id=_new_id("route"),
                run_id=task.id,
                request_id=request.id,
                status="WOULD_GRANT",
                dry_run=True,
                query=query,
                candidate_count=len(hits),
                granted_skills=granted_skills,
                granted_tools=granted_tools,
                selected_cards=selected_cards,
                reasons=reasons,
                message="找到候选能力；apply 时会生成 capability grant。",
                created_at=now,
            )

        grant = self.record_capability_grant(
            task.id,
            request_id=request.id,
            skills=granted_skills,
            tools=granted_tools,
            capability_cards=selected_cards,
            reason=f"CapabilityRouter 命中 {len(selected_hits)} 张能力卡。",
            expires_after_task=True,
        )
        self._mark_capability_request_status(task.id, request.id, "GRANTED")
        routed_task = self.load(task.id)
        self._append_task_work_log(
            routed_task,
            f"capability_route: request {request.id} 已生成 grant {grant.id}，"
            f"skills={','.join(granted_skills) or 'none'} tools={','.join(granted_tools) or 'none'}。",
        )
        return CapabilityRouteRecord(
            id=_new_id("route"),
            run_id=task.id,
            request_id=request.id,
            status="GRANTED",
            dry_run=False,
            query=query,
            candidate_count=len(hits),
            granted_skills=granted_skills,
            granted_tools=granted_tools,
            selected_cards=selected_cards,
            reasons=reasons,
            grant_id=grant.id,
            message="已生成 capability grant。",
            created_at=now,
        )

    def _mark_capability_request_status(self, run_id: str, request_id: str, status: str) -> None:
        """更新 capability request 状态。"""

        task = self.load(run_id)
        for request in task.capability_requests:
            if request.id == request_id:
                request.status = status
        task.updated_at = time.time()
        self.save(task)

    def _append_capability_route_log(self, record: CapabilityRouteRecord) -> None:
        """写入 capability route 审计日志。"""

        jsonl = self.workspace / "subagent_capability_route_log.jsonl"
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        markdown = self.workspace / "CAPABILITY_ROUTE_LOG.md"
        if not markdown.exists():
            markdown.write_text("# CAPABILITY ROUTE LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- [{record.status}] {record.id} run={record.run_id} request={record.request_id} "
                f"skills={','.join(record.granted_skills) or 'none'} "
                f"tools={','.join(record.granted_tools) or 'none'} message={record.message}\n"
            )

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
            Path(task.acceptance_file),
            "# ACCEPTANCE\n\n"
            "## Checks\n"
            + "\n".join(f"- [ ] {item}" for item in task.acceptance_checks or ["未设置"])
            + "\n\n## Evidence\n\n- 暂无\n",
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


def _new_id(prefix: str) -> str:
    """生成短 ID。"""

    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _merge_list(left: list[str], right: list[str]) -> list[str]:
    """保持顺序合并两个字符串列表。"""

    merged = list(left)
    for item in right:
        if item not in merged:
            merged.append(item)
    return merged


def _probe_ok(
    name: str,
    summary: str,
    *,
    severity: str,
    evidence_path: str = "",
    created_at: float,
) -> ChannelProbeCheck:
    """创建成功的 probe check。"""

    return ChannelProbeCheck(
        name=name,
        ok=True,
        summary=summary,
        severity=severity,
        evidence_path=evidence_path,
        created_at=created_at,
    )


def _probe_fail(
    name: str,
    summary: str,
    *,
    severity: str,
    error: str,
    evidence_path: str = "",
    created_at: float,
) -> ChannelProbeCheck:
    """创建失败的 probe check。"""

    return ChannelProbeCheck(
        name=name,
        ok=False,
        summary=summary,
        severity=severity,
        evidence_path=evidence_path,
        error=error,
        created_at=created_at,
    )


def _probe_json_file(
    name: str,
    path: Path,
    severity: str,
    created_at: float,
) -> ChannelProbeCheck:
    """检查机器 JSON 是否存在且可读。"""

    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _probe_fail(
            name,
            "机器 JSON 不可读或格式不正确。",
            severity=severity,
            error=str(exc),
            evidence_path=str(path),
            created_at=created_at,
        )
    return _probe_ok(
        name,
        "机器 JSON 可读。",
        severity=severity,
        evidence_path=str(path),
        created_at=created_at,
    )


def _probe_writable_dir(
    name: str,
    directory: Path,
    severity: str,
    created_at: float,
) -> ChannelProbeCheck:
    """检查目录是否可写，并留下一个轻量证据文件。"""

    probe_file = directory / f"channel_probe_{int(created_at)}.txt"
    try:
        if not directory.exists():
            raise FileNotFoundError(f"目录不存在: {directory}")
        probe_file.write_text(
            f"channel probe ok at {created_at}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return _probe_fail(
            name,
            "目录不可写。",
            severity=severity,
            error=str(exc),
            evidence_path=str(probe_file),
            created_at=created_at,
        )
    return _probe_ok(
        name,
        "目录可写。",
        severity=severity,
        evidence_path=str(probe_file),
        created_at=created_at,
    )


def _channel_status(checks: list[ChannelProbeCheck]) -> str:
    """根据检查项计算通道状态。"""

    if not checks:
        return "UNKNOWN"
    if any(not check.ok and check.severity == "P0" for check in checks):
        return "BROKEN"
    if any(not check.ok for check in checks):
        return "DEGRADED"
    return "OK"


def render_board_markdown(board: SubAgentBoard) -> str:
    """渲染人类可扫视的红绿灯看板。"""

    lines = [
        "# SUBAGENT BOARD",
        "",
        f"- generated_at: {board.generated_at}",
        f"- total: {board.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(board.summary):
        lines.append(f"- {key}: {board.summary[key]}")
    lines.extend(["", "## Hot List", ""])
    if board.hot_list:
        for item in board.hot_list[:50]:
            lines.append(_render_board_line(item))
    else:
        lines.append("- 暂无红灯任务")
    lines.extend(["", "## Recent", ""])
    if board.recent:
        for item in board.recent:
            lines.append(_render_board_line(item))
    else:
        lines.append("- 暂无任务")
    return "\n".join(lines) + "\n"


def render_due_check_markdown(report: DueCheckReport) -> str:
    """渲染父代理 due-check 报告。"""

    lines = [
        "# SUBAGENT DUE CHECK",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total_issues: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Issues", ""])
    if not report.issues:
        lines.append("- 暂无需要介入的问题")
    for issue in report.issues[:100]:
        goal = issue.goal.replace("\n", " ")[:100]
        flags = ",".join(issue.risk_flags) if issue.risk_flags else "ok"
        lines.append(
            f"- [{issue.severity}] `{issue.run_id}` {issue.kind} "
            f"status={issue.status} action={issue.suggested_action} "
            f"flags={flags} :: {goal}"
        )
        lines.append(f"  - {issue.message}")
    return "\n".join(lines) + "\n"


def render_action_plan_markdown(report: ActionPlanReport) -> str:
    """渲染 dry-run 动作计划。"""

    lines = [
        "# SUBAGENT ACTION PLAN",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total_actions: {report.summary.get('total', 0)}",
        "- mode: dry-run",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Actions", ""])
    if not report.actions:
        lines.append("- 暂无建议动作")
    for action in report.actions[:100]:
        kinds = ",".join(action.source_issue_kinds)
        lines.append(
            f"- [{action.severity}] `{action.run_id}` priority={action.priority} "
            f"action={action.action} sources={kinds}"
        )
        lines.append(f"  - reason: {action.reason}")
        if action.would_change_status_to:
            lines.append(f"  - would_change_status_to: {action.would_change_status_to}")
        if action.suggested_commands:
            lines.append("  - suggested_commands:")
            for command in action.suggested_commands[:5]:
                lines.append(f"    - `{command}`")
    return "\n".join(lines) + "\n"


def render_action_apply_markdown(report: ActionApplyReport) -> str:
    """渲染 action apply 报告。"""

    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT ACTION APPLY",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
    if not report.records:
        lines.append("- 暂无动作记录")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` action={record.action} "
            f"applied={record.applied} {record.before_status}->{record.after_status}"
        )
        lines.append(f"  - {record.message}")
        if record.evidence_paths:
            lines.append("  - evidence:")
            for path in record.evidence_paths[:5]:
                lines.append(f"    - `{path}`")
    return "\n".join(lines) + "\n"


def render_capability_route_markdown(report: CapabilityRouteReport) -> str:
    """渲染 capability request 路由报告。"""

    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT CAPABILITY ROUTE",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
    if not report.records:
        lines.append("- 暂无待路由能力请求")
    for record in report.records[:100]:
        cards = ", ".join(
            f"{item.get('kind')}:{item.get('name')}" for item in record.selected_cards
        ) or "none"
        lines.append(
            f"- [{record.status}] `{record.run_id}` request={record.request_id} "
            f"cards={cards}"
        )
        lines.append(f"  - message: {record.message}")
        if record.reasons:
            lines.append(f"  - reasons: {'; '.join(record.reasons[:5])}")
    return "\n".join(lines) + "\n"


def render_execution_context_markdown(context: SubAgentExecutionContext) -> str:
    """渲染给子代理执行器读取的人类版上下文。"""

    lines = [
        "# SUBAGENT EXECUTION CONTEXT",
        "",
        f"- run_id: {context.run_id}",
        f"- generated_at: {context.generated_at}",
        f"- status: {context.status}",
        f"- verification_status: {context.verification_status}",
        f"- channel_status: {context.channel_status}",
        f"- agent: {context.agent_name}",
        f"- role: {context.role}",
        f"- owner: {context.owner or 'none'}",
        f"- supervisor: {context.supervisor or 'none'}",
        f"- final_owner: {context.final_owner or 'none'}",
        f"- parent_id: {context.parent_id or 'none'}",
        f"- root_id: {context.root_id or context.run_id}",
        f"- depth: {context.depth}",
        f"- task_dir: {context.task_dir}",
        "",
        "## Goal",
        "",
        context.goal,
        "",
        "## Thought",
        "",
        context.thought or "未设置",
        "",
        "## Plan",
        "",
    ]
    lines.extend(f"- {item}" for item in context.plan or ["未设置"])
    lines.extend(
        [
            "",
            "## Allowed Capabilities",
            "",
            f"- skills: {', '.join(context.allowed_skills) or 'none'}",
            f"- tools: {', '.join(context.allowed_tools) or 'none'}",
            "",
            "## Granted Cards",
            "",
        ]
    )
    if context.granted_cards:
        for card in context.granted_cards:
            lines.append(
                f"- [{card.get('kind', 'unknown')}] {card.get('name', 'unknown')} "
                f"risk={card.get('risk_level', 'unknown')} source={card.get('source', 'unknown')}"
            )
            if card.get("description"):
                lines.append(f"  - description: {card['description']}")
            if card.get("path"):
                lines.append(f"  - path: {card['path']}")
            if card.get("reasons"):
                lines.append(f"  - reasons: {card['reasons']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Write Boundary", ""])
    allowed_roots = context.write_boundary.get("allowed_write_roots") or []
    forbidden_roots = context.write_boundary.get("forbidden_write_roots") or []
    locked_files = context.write_boundary.get("locked_files") or []
    lines.append(f"- task_dir: {context.write_boundary.get('task_dir') or context.task_dir}")
    lines.append(f"- allowed_write_roots: {', '.join(allowed_roots) if allowed_roots else 'none'}")
    lines.append(
        f"- forbidden_write_roots: {', '.join(forbidden_roots) if forbidden_roots else 'none'}"
    )
    lines.append(f"- locked_files: {', '.join(locked_files) if locked_files else 'none'}")

    lines.extend(["", "## Acceptance Checks", ""])
    lines.extend(f"- [ ] {item}" for item in context.acceptance_checks or ["未设置"])

    lines.extend(["", "## Evidence", ""])
    if context.evidence:
        for item in context.evidence:
            status = "OK" if item.get("ok") else "FAIL"
            lines.append(f"- [{status}] {item.get('kind', 'unknown')}: {item.get('summary', '')}")
            if item.get("command"):
                lines.append(f"  - command: `{item['command']}`")
            if item.get("path"):
                lines.append(f"  - path: {item['path']}")
            if item.get("url"):
                lines.append(f"  - url: {item['url']}")
    else:
        lines.append("- 暂无")

    lines.extend(["", "## Pending Capability Requests", ""])
    if context.pending_requests:
        for item in context.pending_requests:
            lines.append(
                f"- `{item.get('id')}` needed={item.get('needed_capability')} "
                f"status={item.get('status')}: {item.get('problem')}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Open Capability Gaps", ""])
    if context.open_gaps:
        for item in context.open_gaps:
            lines.append(
                f"- `{item.get('id')}` missing={item.get('missing_capability')} "
                f"status={item.get('status')}: {item.get('why_failed')}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Execution Rules", ""])
    lines.extend(f"- {item}" for item in context.instructions)
    return "\n".join(lines) + "\n"


def render_runner_result_markdown(result: SubAgentRunnerResult) -> str:
    """渲染子代理 runner 调用结果。"""

    status = "OK" if result.ok else "FAIL"
    mode = "dry-run" if result.dry_run else "execute"
    lines = [
        "# SUBAGENT RUNNER RESULT",
        "",
        f"- run_id: {result.run_id}",
        f"- created_at: {result.created_at}",
        f"- mode: {mode}",
        f"- status: {result.status}",
        f"- verification_status: {result.verification_status}",
        f"- ok: {status}",
        f"- backend: {result.backend or 'none'}",
        f"- tool_rounds: {result.tool_rounds}",
        "",
        "## Message",
        "",
        result.message or "none",
        "",
        "## Files",
        "",
        f"- execution_context_json: {result.execution_context_json}",
        f"- execution_context_file: {result.execution_context_file}",
        f"- prompt_file: {result.prompt_file or 'none'}",
        f"- response_file: {result.response_file or 'none'}",
        f"- result_json: {result.result_json}",
        f"- output_json: {result.output_json}",
    ]
    return "\n".join(lines) + "\n"


def render_channel_probe_markdown(report: ChannelProbeReport) -> str:
    """渲染批量通道健康检查报告。"""

    lines = [
        "# SUBAGENT CHANNEL PROBE",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Results", ""])
    if not report.results:
        lines.append("- 暂无可检查的子代理记录")
    for result in report.results[:100]:
        failed = [check for check in result.checks if not check.ok]
        goal = result.goal.replace("\n", " ")[:100]
        lines.append(
            f"- `{result.run_id}` channel={result.channel_status} "
            f"failed_checks={len(failed)} :: {goal}"
        )
        for check in failed[:5]:
            lines.append(f"  - [{check.severity}] {check.name}: {check.summary} {check.error}".rstrip())
    return "\n".join(lines) + "\n"


def render_single_channel_probe_markdown(result: ChannelProbeResult) -> str:
    """渲染单个 run 的通道健康检查证据。"""

    lines = [
        "# CHANNEL PROBE",
        "",
        f"- run_id: {result.run_id}",
        f"- channel_status: {result.channel_status}",
        f"- created_at: {result.created_at}",
        f"- task_dir: {result.task_dir}",
        "",
        "## Checks",
        "",
    ]
    for check in result.checks:
        status = "OK" if check.ok else "FAIL"
        lines.append(
            f"- [{status}] {check.name} severity={check.severity} "
            f"evidence={check.evidence_path or 'none'}"
        )
        lines.append(f"  - {check.summary}")
        if check.error:
            lines.append(f"  - error: {check.error}")
    return "\n".join(lines) + "\n"


def _render_board_line(item: SubAgentBoardItem) -> str:
    """渲染看板的一行。"""

    flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
    goal = item.goal.replace("\n", " ")[:100]
    return (
        f"- `{item.id}` status={item.status} verify={item.verification_status} "
        f"channel={item.channel_status} "
        f"depth={item.depth} owner={item.owner or 'none'} final={item.final_owner or 'none'} "
        f"evidence={item.evidence_count} requests={item.open_request_count} "
        f"gaps={item.open_gap_count} flags={flags} :: {goal}"
    )


def filter_board_items(
    items: list[SubAgentBoardItem],
    *,
    status: str = "",
    owner: str = "",
    root_id: str = "",
) -> list[SubAgentBoardItem]:
    """按 CLI 参数过滤看板行。"""

    result = items
    if status:
        normalized = status.upper()
        result = [item for item in result if item.status == normalized]
    if owner:
        result = [
            item
            for item in result
            if item.owner == owner or item.supervisor == owner or item.final_owner == owner
        ]
    if root_id:
        result = [item for item in result if item.root_id == root_id]
    return result


def _risk_weight(flags: list[str]) -> int:
    """让严重风险在 Hot List 里排前面。"""

    weights = {
        "failed": 100,
        "timeout": 95,
        "channel_error": 90,
        "blocked": 80,
        "channel_broken": 75,
        "missing_work_order_files": 70,
        "done_without_evidence": 60,
        "done_without_verification": 55,
        "channel_degraded": 52,
        "open_capability_gap": 50,
        "open_capability_request": 40,
        "taken_over": 30,
    }
    return max((weights.get(item, 1) for item in flags), default=0)


def _make_due_issue(
    task: SubAgentTask,
    *,
    severity: str,
    kind: str,
    message: str,
    suggested_action: str,
    risk_flags: list[str],
    open_request_count: int,
    open_gap_count: int,
    age_seconds: float,
    stale_seconds: float,
) -> DueCheckIssue:
    """统一创建 due-check 问题，避免不同分支字段不一致。"""

    return DueCheckIssue(
        run_id=task.id,
        severity=severity,
        kind=kind,
        message=message,
        suggested_action=suggested_action,
        status=task.status,
        owner=task.owner,
        supervisor=task.supervisor,
        final_owner=task.final_owner,
        goal=task.goal,
        task_dir=task.task_dir,
        risk_flags=risk_flags,
        evidence_count=len(task.evidence),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        age_seconds=age_seconds,
        stale_seconds=stale_seconds,
        created_at=time.time(),
    )


def _issue_weight(issue: DueCheckIssue) -> int:
    """due-check 排序权重。"""

    severity_weight = _severity_weight(issue.severity)
    kind_weight = {
        "missing_work_order_files": 90,
        "fake_done_risk": 85,
        "run_timeout": 80,
        "heartbeat_stale": 70,
        "channel_broken": 68,
        "status_failed": 65,
        "status_timeout": 65,
        "status_channel_error": 60,
        "status_blocked": 50,
        "channel_probe_missing": 48,
        "unverified_done": 45,
        "channel_degraded": 42,
        "open_capability_request": 40,
        "open_capability_gap": 20,
    }.get(issue.kind, 1)
    return severity_weight + kind_weight


def _severity_weight(severity: str) -> int:
    """统一的 P0/P1/P2 权重。"""

    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)


def _action_for_issue(issue: DueCheckIssue) -> tuple[str, int, str]:
    """把 due-check issue 映射为 dry-run 动作。"""

    kind = issue.kind
    if kind in {"channel_broken", "channel_probe_missing", "status_channel_error"}:
        return "probe_or_repair_channel", 980, "CHANNEL_ERROR"
    if kind == "channel_degraded":
        return "inspect_channel_probe", 780, ""
    if kind == "missing_work_order_files":
        return "repair_work_order", 960, "BLOCKED"
    if kind == "fake_done_risk":
        return "reopen_for_evidence", 940, "BLOCKED"
    if kind == "unverified_done":
        return "run_acceptance", 760, ""
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_reassign", 900, "TIMEOUT"
    if kind == "status_failed":
        return "inspect_failure", 860, ""
    if kind == "status_blocked":
        return "classify_blocker", 740, ""
    if kind == "open_capability_request":
        return "route_capability_request", 700, ""
    if kind == "open_capability_gap":
        return "triage_capability_gap", 420, ""
    return issue.suggested_action or "inspect_manually", 100, ""


def _commands_for_action(action: str, run_id: str) -> list[str]:
    """给 dry-run 动作提供下一步可运行命令。"""

    commands = {
        "probe_or_repair_channel": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "inspect_channel_probe": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "repair_work_order": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "reopen_for_evidence": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "run_acceptance": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "takeover_or_reassign": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "inspect_failure": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "classify_blocker": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "route_capability_request": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "triage_capability_gap": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
    }
    return commands.get(action, [f"python3 -m agent_py_agent subagent {run_id}"])


def _filter_action_plan_items(
    actions: list[ActionPlanItem],
    *,
    action_filter: str = "",
    run_id: str = "",
    limit: int = 0,
) -> list[ActionPlanItem]:
    """按 CLI 参数过滤动作计划。"""

    result = actions
    if action_filter:
        result = [item for item in result if item.action == action_filter]
    if run_id:
        result = [item for item in result if item.run_id == run_id]
    if limit > 0:
        result = result[:limit]
    return result


def _capability_request_query(task: SubAgentTask, request: CapabilityRequest) -> str:
    """把子代理能力请求压成检索 query。"""

    parts = [
        task.goal,
        request.needed_capability,
        request.problem,
        request.expected_output,
        " ".join(request.tried),
        " ".join(request.evidence),
        " ".join(f"{key}:{value}" for key, value in request.constraints.items()),
    ]
    return "\n".join(part for part in parts if part)


def _select_capability_hits(
    hits: list[CapabilitySearchHit],
    config: CapabilityConfig,
) -> list[CapabilitySearchHit]:
    """按配置限制挑选要下发的 skill/tool card。"""

    selected: list[CapabilitySearchHit] = []
    skill_count = 0
    tool_count = 0
    for hit in hits:
        if not _capability_hit_is_confident(hit):
            continue
        if hit.card.kind == "skill":
            if config.capability_grant_max_skills and skill_count >= config.capability_grant_max_skills:
                continue
            selected.append(hit)
            skill_count += 1
            continue
        if hit.card.kind == "tool":
            if config.capability_grant_max_tools and tool_count >= config.capability_grant_max_tools:
                continue
            selected.append(hit)
            tool_count += 1
    return selected


def _capability_hit_is_confident(hit: CapabilitySearchHit) -> bool:
    """过滤掉只因泛词弱命中的能力卡。"""

    return hit.score >= 4.0


def _route_card_payload(hit: CapabilitySearchHit) -> dict[str, str]:
    """把能力命中结果压成 grant 里可审计的短卡。"""

    card = hit.card
    return {
        "id": card.id,
        "kind": card.kind,
        "name": card.name,
        "description": card.description[:240],
        "risk_level": card.risk_level,
        "source": card.source,
        "path": card.path,
        "score": f"{hit.score:.2f}",
        "reasons": "；".join(hit.reasons[:4]),
    }


def _dedupe_granted_cards(
    grants: list[CapabilityGrant],
    *,
    max_cards: int = 0,
) -> list[dict[str, str]]:
    """从 capability grants 中提取去重后的短卡。"""

    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for grant in grants:
        for card in grant.capability_cards:
            key = card.get("id") or f"{card.get('kind')}:{card.get('name')}"
            if not key or key in seen:
                continue
            seen.add(key)
            cards.append(
                {str(item_key): str(item_value) for item_key, item_value in card.items()}
            )
            if max_cards > 0 and len(cards) >= max_cards:
                return cards
    return cards


def _execution_context_instructions() -> list[str]:
    """生成子代理执行上下文里的硬规则。"""

    return [
        "只能使用本上下文列出的 allowed_skills、allowed_tools 和 granted_cards。",
        "不要读取或展开全局 skill/tool registry；缺能力时提交 capability_request。",
        "工具失败要记录 tried/evidence，并优先在已授权能力内换 fallback；无可用 fallback 时上抛。",
        "完成前必须写入可验收 evidence，不能只口头声明完成。",
        "写入只允许发生在 allowed_write_roots 内，禁止写 forbidden_write_roots 和 locked_files。",
        "如果通道损坏、工单文件缺失或任务边界不清，先标记 BLOCKED 并等待父代理处理。",
    ]


def _is_active(status: str) -> bool:
    """判断任务是否仍应有心跳和运行时限。"""

    return status.upper() not in {
        "DONE",
        "FAILED",
        "BLOCKED",
        "AWAITING_ACCEPTANCE",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }


def _apply_paths(task: SubAgentTask, paths: dict[str, object]) -> None:
    """把路径字典写回任务对象。"""

    for key, value in paths.items():
        setattr(task, key, value)


def _apply_missing_paths(task: SubAgentTask, paths: dict[str, object]) -> None:
    """只补齐缺失路径，避免覆盖已有工单位置。"""

    for key, value in paths.items():
        if not getattr(task, key, None):
            setattr(task, key, value)


def _write_if_missing(path: Path, content: str) -> None:
    """只在文件不存在时写入，避免覆盖子代理已产出的内容。"""

    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json_if_missing(path: Path, payload: dict[str, object]) -> None:
    """只在 JSON 文件不存在时写入默认结构。"""

    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_forbidden_write_roots() -> list[str]:
    """默认禁止子代理写入的高风险目录。"""

    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".openclaw"),
    ]
