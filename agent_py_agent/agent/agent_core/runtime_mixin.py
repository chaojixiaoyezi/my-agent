from __future__ import annotations

"""LLM: implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

给人看的解释：
这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。
"""

import time

from ..memory_archive import archive_run_turn, write_recovery_snapshot
from ..memory_routing import build_routed_memory_context
from ..tools import ToolExecutionResult
from .models import AgentRunResult
from .parameters import _one_shot_tool_call_key


class SimpleAgentRuntimeMixin:
    """LLM: mixin for the primary model/tool execution loop.

    给人看的解释：
    `run()` 就在这里。普通聊天、gateway ask、runner 调用最后都会经过这条链路。
    """

    def run(
        self,
        user_prompt: str,
        *,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        save: bool | None = None,
        allowed_tools: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
        request_id: str = "",
        run_id: str = "",
        task_id: str = "",
        source: str = "run",
        recovery_snapshot: bool | None = None,
        recovery_task_refs: list[str] | None = None,
        recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
    ) -> AgentRunResult:
        """执行一轮智能体请求。"""

        memories = self.memory.search(user_prompt, self.config.memory_top_k)
        route_mode = str(getattr(self.config, "memory_rule_routing_mode", "soft") or "soft")
        route_enabled = bool(getattr(self.config, "memory_rule_routing_enabled", True)) and route_mode != "off"
        route_auto_read_limit = int(getattr(self.config, "memory_rule_auto_read_limit", 3))
        routed_context = build_routed_memory_context(
            self.root,
            user_prompt,
            enabled=route_enabled,
            mode=route_mode if route_mode != "off" else "soft",
            auto_read_limit=route_auto_read_limit,
            limit=max(route_auto_read_limit, 5),
        )
        runtime_injections = [
            *(inject or []),
            *routed_context.injected_sections,
        ]
        tool_catalog_section = (
            self.tools.render_catalog_section(allowed_tools=allowed_tools)
            if self.config.enable_tools
            else ""
        )
        tool_recommendations_section = (
            self.tools.render_recommended_tools_section(user_prompt, allowed_tools=allowed_tools)
            if self.config.enable_tools
            else ""
        )
        tool_context: list[str] = []
        tool_rounds = 0
        final_prompt = ""
        final_response = None
        one_shot_tool_calls: set[str] = set()
        executed_tools: list[str] = []
        archive_tool_calls: list[dict[str, object]] = []

        while True:
            final_prompt = self.prompts.build(
                user_prompt,
                memories,
                inject=runtime_injections,
                prompt_files=prompt_files,
                tool_catalog_section=tool_catalog_section,
                tool_recommendations_section=tool_recommendations_section,
                tool_context=tool_context,
            )
            response = self.backend.generate(final_prompt)
            final_response = response

            if not self.config.enable_tools:
                break

            calls = self.tools.parse_tool_calls(response.text)
            if not calls:
                break

            if tool_rounds >= self.config.max_tool_rounds:
                tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
                final_prompt = self.prompts.build(
                    user_prompt,
                    memories,
                    inject=runtime_injections,
                    prompt_files=prompt_files,
                    tool_catalog_section=tool_catalog_section,
                    tool_recommendations_section=tool_recommendations_section,
                    tool_context=tool_context,
                )
                final_response = self.backend.generate(final_prompt)
                break

            tool_rounds += 1
            tool_context.append(f"[assistant-tool-round-{tool_rounds}]\n{response.text}")
            for idx, payload in enumerate(calls, start=1):
                one_shot_key = _one_shot_tool_call_key(payload)
                if one_shot_key and one_shot_key in one_shot_tool_calls:
                    tool_name = str(payload.get("tool") or "unknown")
                    result = ToolExecutionResult(
                        tool_name,
                        False,
                        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
                        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
                    )
                else:
                    result = self.tools.execute_call(
                        payload,
                        allowed_tools=allowed_tools,
                        write_boundary=write_boundary,
                    )
                    if one_shot_key and result.ok:
                        one_shot_tool_calls.add(one_shot_key)
                if result.ok and result.tool not in {"__parse_error__", "unknown"}:
                    executed_tools.append(result.tool)
                archive_tool_calls.append(
                    {
                        "tool": result.tool,
                        "id": f"{tool_rounds}-{idx}",
                        "ok": result.ok,
                        "parameters": payload,
                    }
                )
                tool_context.append(
                    f"[tool-call-{tool_rounds}-{idx}]\n{payload}\n"
                    f"[tool-result-{tool_rounds}-{idx}]\n{result.render_for_prompt()}"
                )

        assert final_response is not None
        do_save = self.config.auto_save_memory if save is None else save
        run_request_id = request_id or f"run-{time.time_ns()}"
        if do_save:
            self.memory.add("user", user_prompt)
            self.memory.add("agent", final_response.text, tags=[final_response.backend])
            archive_result = archive_run_turn(
                self.root,
                session_id=getattr(self, "session_id", self.config.agent_name),
                request_id=run_request_id,
                run_id=run_id,
                task_id=task_id,
                user_prompt=user_prompt,
                response_text=final_response.text,
                backend=final_response.backend,
                tool_calls=archive_tool_calls,
                source=source,
                archive_level=int(getattr(self.config, "memory_archive_level", 3)),
            )
        else:
            archive_result = None
        should_write_snapshot = (
            bool(getattr(self.config, "memory_hook_enabled", True))
            and (do_save if recovery_snapshot is None else bool(recovery_snapshot))
        )
        snapshot_result = (
            write_recovery_snapshot(
                self.root,
                session_id=getattr(self, "session_id", self.config.agent_name),
                request_id=run_request_id,
                run_id=run_id,
                task_id=task_id,
                user_prompt=user_prompt,
                response_text=final_response.text,
                backend=final_response.backend,
                source=source,
                status="ok",
                tool_calls=archive_tool_calls,
                task_refs=recovery_task_refs or [],
                content_paths=[
                    *(recovery_content_paths or []),
                    *(routed_context.required_read_paths or []),
                    *(routed_context.candidate_paths or []),
                ],
                next_actions=recovery_next_actions or [],
                archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
            )
            if should_write_snapshot
            else None
        )

        return AgentRunResult(
            prompt=final_prompt,
            response=final_response.text,
            backend=final_response.backend,
            used_memories=len(memories),
            tool_rounds=tool_rounds,
            executed_tools=executed_tools,
            memory_route_matches=len(routed_context.matches),
            memory_route_paths=[
                *routed_context.required_read_paths,
                *routed_context.candidate_paths,
            ],
            archive_events=archive_result.event_count if archive_result else 0,
            archive_token_estimate=archive_result.token_estimate if archive_result else 0,
            recovery_snapshot_id=snapshot_result.snapshot_id if snapshot_result else "",
            recovery_snapshot_path=snapshot_result.path if snapshot_result else "",
            recovery_snapshot_error=snapshot_result.error if snapshot_result else "",
            recovery_snapshot_token_estimate=snapshot_result.token_estimate if snapshot_result else 0,
        )

    def remember(self, content: str, *, kind: str = "note"):
        """手动写入一条记忆。"""

        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        """召回相关记忆。"""

        return self.memory.search(query, top_k or self.config.memory_top_k)
