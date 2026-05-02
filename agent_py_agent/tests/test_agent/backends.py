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
