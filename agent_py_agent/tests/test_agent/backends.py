"""LLM: Stub backends used by subagent runner and dispatch tests; each returns a
hard-coded ModelResponse to exercise a specific code path (structured output,
acceptance, write-boundary enforcement, flaky retry, structured-output repair,
parent-planner dispatch).

给人看的解释：
测试专用后端类，模拟不同场景下模型返回的内容，让测试不依赖真实 LLM。
"""

from agent_py_agent.agent.backend import BaseBackend, ModelResponse


class StructuredSubagentBackend(BaseBackend):
    """测试用后端：直接返回 runner 结构化结果。"""

    name = "structured_subagent_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        return ModelResponse(
            text=(
                "我读取了当前上下文，但缺少 HTTP 检查能力。\n"
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "BLOCKED",\n'
                '  "summary": "已完成代码阅读，但无法发起 HTTP 检查。",\n'
                '  "used_tools": ["read_file", "write_file"],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "已确认需要接口健康检查", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [\n'
                '    {"problem": "需要请求接口确认状态码", "needed_capability": "http_request", "expected_output": "接口状态码", "tried": ["read_file"], "evidence": ["代码阅读不足以确认线上状态"], "constraints": {"method": "GET"}}\n'
                "  ],\n"
                '  "artifacts": [\n'
                '    {"path": "reports/api_notes.md", "kind": "report", "summary": "接口检查前置阅读记录"}\n'
                "  ],\n"
                '  "tests": [\n'
                '    {"name": "static-read", "command": "read_file api.py", "ok": true, "summary": "静态阅读完成"}\n'
                "  ],\n"
                '  "patches": [\n'
                '    {"path": "agent_py_agent/agent/core.py", "status": "planned", "summary": "需要授权后再接 HTTP 检查"}\n'
                "  ],\n"
                '  "lessons": ["缺少线上检查工具时，不要把静态阅读当成接口可用证据"],\n'
                '  "next_actions": ["route_capability_request", "rerun_subagent_after_grant"],\n'
                '  "blocked_reason": "当前上下文没有授权 HTTP 请求工具",\n'
                '  "failure_type": "capability_request"\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


class AcceptedSubagentBackend(BaseBackend):
    """测试用后端：返回可直接验收的结构化结果。"""

    name = "accepted_subagent_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "调度器 runner 已完成。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "调度器结构化执行证据", "ok": true}\n'
                "  ],\n"
                '  "evidence_packets": [\n'
                '    {"id": "evpkt-dispatch-runner", "claim": "调度器 runner 已完成", "checked_scope": "dispatch runner", "evidence_refs": ["runner_result.json"], "artifact_refs": ["output.json"], "confidence": 0.9}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "dispatch-smoke", "command": "", "ok": true, "summary": "通过"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


# LLM: CapabilityThenAcceptedBackend simulates a worker that needs one parent grant before finishing.
# 类用途: 第一轮返回 controlled_exec 能力申请，第二轮看到父级 grant 后返回可验收结果，用于测试 dispatch 内部闭环。
class CapabilityThenAcceptedBackend(BaseBackend):
    """测试用后端：先申请 controlled_exec，授权后完成。"""

    name = "capability_then_accepted_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text=_controlled_exec_request_result(), backend=self.name)
        assert "controlled_exec_grants" in prompt
        return ModelResponse(text=_controlled_exec_done_result(), backend=self.name)


# LLM: _controlled_exec_request_result keeps the capability backend class compact.
# 函数用途: 返回一个结构化 BLOCKED 结果，包含 shell/tool/path/output budget 申请字段。
def _controlled_exec_request_result() -> str:
    return (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "BLOCKED",\n'
        '  "summary": "需要父级授权 controlled_exec 后继续。",\n'
        '  "used_tools": ["write_file"],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "note", "summary": "已写 sentinel，等待 shell grant", "ok": true}],\n'
        '  "evidence_packets": [],\n'
        '  "capability_requests": [\n'
        '    {"problem": "需要受控 shell 验证 pwd 和 trash 行为", "needed_capability": "controlled_exec", "capability_type": "shell", "expected_output": "stdout/audit/trash refs", "requested_tools": ["controlled_exec"], "requested_commands": ["pwd", "rm"], "path_scope": ["."], "output_budget": {"stdout_bytes": 2048, "stderr_bytes": 1024}, "risk_level": "low"}\n'
        "  ],\n"
        '  "artifacts": [],\n'
        '  "tests": [],\n'
        '  "patches": [],\n'
        '  "lessons": [],\n'
        '  "next_actions": ["route_capability_request", "rerun_subagent_after_grant"],\n'
        '  "blocked_reason": "缺少 controlled_exec grant",\n'
        '  "failure_type": "capability_request"\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )


# LLM: _controlled_exec_done_result includes the refs required by controlled_exec acceptance.
# 函数用途: 返回授权后完成的结构化结果，明确记录 used_tools 和 stdout/audit/trash refs。
def _controlled_exec_done_result() -> str:
    return (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "controlled_exec 已通过父级 grant 执行。stdout_ref=stdout.log audit_ref=audit.jsonl trash_manifest_ref=trash_manifest.jsonl",\n'
        '  "used_tools": ["controlled_exec"],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "command", "summary": "controlled_exec pwd/rm smoke passed", "ok": true}],\n'
        '  "evidence_packets": [{"id": "evpkt-controlled-exec", "claim": "受控 shell 已完成", "checked_scope": "controlled_exec dispatch", "evidence_refs": ["stdout_ref=stdout.log", "audit_ref=audit.jsonl", "trash_manifest_ref=trash_manifest.jsonl"], "artifact_refs": ["output.json"], "confidence": 0.9}],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [{"name": "controlled-exec-smoke", "ok": true, "summary": "refs present"}],\n'
        '  "patches": [],\n'
        '  "lessons": [],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )


class BoundaryWriteSubagentBackend(BaseBackend):
    """测试用后端：先尝试越界写文件，再根据工具拦截结果收口。"""

    name = "boundary_write_subagent_backend"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            assert "allowed_write_roots" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool": "write_file", "path": "README.md", "content": "bad"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )

        assert "写入被阻止" in prompt
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "越界写入已被工具层阻止。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "工具结果显示 README.md 越界写入被阻止", "ok": true}\n'
                "  ],\n"
                '  "evidence_packets": [\n'
                '    {"id": "evpkt-boundary", "claim": "越界写入已被阻止", "checked_scope": "write_file README.md", "evidence_refs": ["runner_result.json"], "artifact_refs": ["output.json"], "confidence": 0.9}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [],\n'
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


class HierarchicalScheduleSubagentBackend(BaseBackend):
    """测试用后端：主节点 runner 通过层级调度工具创建下一层子节点。"""

    name = "hierarchical_schedule_subagent_backend"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            assert "schedule_child_subagents [orchestration]" in prompt
            return _hierarchical_schedule_tool_call_response(self.name)

        assert "child-catalog" in prompt
        return _hierarchical_schedule_result_response(self.name)


class CoordinatorAnalysisOnlyBackend(BaseBackend):
    """测试用后端：coordinator 只分析下一步派工，但没有真正调用 schedule_child_subagents。"""

    name = "coordinator_analysis_only_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        assert "schedule_child_subagents [orchestration]" in prompt
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "我已经分析完，下一步应该调用 schedule_child_subagents 创建 worker。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [],\n'
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [],\n'
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": ["schedule_child_subagents"],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


# LLM: _hierarchical_schedule_tool_call_response keeps the fake model's first turn short.
# 函数用途: 返回测试模型第一次调用 schedule_child_subagents 的固定响应。
def _hierarchical_schedule_tool_call_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "[TOOL_CALL]\n"
            "{"
            '"tool":"schedule_child_subagents",'
            '"apply":true,'
            '"max_depth":3,'
            '"max_children":4,'
            '"children":[{'
            '"role":"child_coordinator",'
            '"agent_name":"child-catalog",'
            '"goal":"作为主节点的下一层，继续拆分目录和商品列表实现任务",'
            '"allowed_tools":["schedule_child_subagents","dispatch_subagents","subagent_board","read_file","write_file"],'
            '"acceptance_checks":["必须只通过父节点汇报 refs 和状态"]'
            "}]"
            "}\n"
            "[/TOOL_CALL]"
        ),
        backend=backend,
    )


# LLM: _hierarchical_schedule_result_response keeps the fake model's final result reusable.
# 函数用途: 返回测试模型第二次收口的结构化 subagent 结果。
def _hierarchical_schedule_result_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "[SUBAGENT_RESULT]\n"
            "{\n"
            '  "status": "AWAITING_ACCEPTANCE",\n'
            '  "summary": "主节点已创建下一层 child coordinator，等待父级继续调度。",\n'
            '  "used_tools": ["schedule_child_subagents"],\n'
            '  "used_skills": [],\n'
            '  "evidence": [\n'
            '    {"kind": "tool", "summary": "schedule_child_subagents 已返回 child-catalog", "ok": true}\n'
            "  ],\n"
            '  "capability_requests": [],\n'
            '  "artifacts": [],\n'
            '  "tests": [\n'
            '    {"name": "hierarchy-schedule", "command": "", "ok": true, "summary": "下一层已创建"}\n'
            "  ],\n"
            '  "patches": [],\n'
            '  "lessons": [],\n'
            '  "next_actions": ["dispatch_child_coordinator_from_parent"],\n'
            '  "blocked_reason": "",\n'
            '  "failure_type": ""\n'
            "}\n"
            "[/SUBAGENT_RESULT]"
        ),
        backend=backend,
    )


class FlakyThenAcceptedSubagentBackend(BaseBackend):
    """测试用后端：第一次模型调用失败，第二次返回可验收结果。"""

    name = "flaky_then_accepted_subagent_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary runner backend outage")
        assert "[SUBAGENT_RESULT]" in prompt
        assert "runner_attempts" in prompt
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "重试后 runner 已完成。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "第二次尝试成功生成证据", "ok": true}\n'
                "  ],\n"
                '  "evidence_packets": [\n'
                '    {"id": "evpkt-retry", "claim": "重试后 runner 已完成", "checked_scope": "retry runner", "evidence_refs": ["runner_result.json"], "artifact_refs": ["output.json"], "confidence": 0.9}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "retry-smoke", "command": "", "ok": true, "summary": "重试通过"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


class RepairingSubagentBackend(BaseBackend):
    """测试用后端：第一次漏掉结构化块，修复回合补齐。"""

    name = "repairing_subagent_backend"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            assert "[SUBAGENT_RESULT]" in prompt
            return ModelResponse(text="我已经完成检查，但这次忘记输出机器结果块。", backend=self.name)

        assert "# SubAgent Runner Output Repair" in prompt
        assert "我已经完成检查" in prompt
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "已通过修复回合补齐结构化结果。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "修复回合根据上一轮回复生成可验收证据", "ok": true}\n'
                "  ],\n"
                '  "evidence_packets": [\n'
                '    {"id": "evpkt-repair", "claim": "结构化结果已修复", "checked_scope": "runner repair", "evidence_refs": ["runner_result.json"], "artifact_refs": ["output.json"], "confidence": 0.9}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "repair-format", "command": "", "ok": true, "summary": "结构化格式已恢复"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


class CoordinatorToolLimitBlockedBackend(BaseBackend):
    """测试用后端：coordinator 子层已完成，但收尾继续要工具并在修复回合误报 BLOCKED。"""

    name = "coordinator_tool_limit_blocked_backend"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        if len(self.prompts) <= 2:
            return ModelResponse(
                text=(
                    "直接 child 已经完成并验收，但我还想再查一次 proof.txt。\n"
                    "[TOOL_CALL]\n"
                    '{"tool":"read_file","path":"proof.txt"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )

        assert "# SubAgent Runner Output Repair" in prompt or "已达到最大工具轮数限制" in prompt
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "直接 child 已完成，但工具轮数上限导致无法重复验证。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [],\n'
                '  "capability_requests": [\n'
                '    {"problem": "工具轮数限制导致无法重复读取 proof.txt", "needed_capability": "增加 max_tool_rounds", "expected_output": "确认 proof.txt 内容", "tried": ["read_artifact"], "evidence": [], "constraints": {}}\n'
                "  ],\n"
                '  "artifacts": [],\n'
                '  "tests": [],\n'
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": ["parent_acceptance"],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


class ParentPlannerBackend(BaseBackend):
    """测试用后端：返回父代理 planner 结构化结果。"""

    name = "parent_planner_backend"

    def __init__(self, *, decision: str = "DISPATCH"):
        self.decision = decision
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        assert "[PARENT_PLANNER_RESULT]" in prompt
        return ModelResponse(
            text=(
                "[PARENT_PLANNER_RESULT]\n"
                "{\n"
                f'  "decision": "{self.decision}",\n'
                '  "summary": "父代理 planner 已看到待处理任务。",\n'
                f'  "should_dispatch": {str(self.decision != "HEARTBEAT_OK").lower()},\n'
                '  "runner_instruction": "优先产出可验收证据。",\n'
                '  "suggested_max_runners": 1,\n'
                '  "actions": [\n'
                '    {"action": "execute_runner", "run_id": "", "priority": 1, "reason": "存在 active task"}\n'
                "  ],\n"
                '  "blockers": [],\n'
                '  "risks": [],\n'
                '  "notes": ["planner-test"]\n'
                "}\n"
                "[/PARENT_PLANNER_RESULT]"
            ),
            backend=self.name,
        )
