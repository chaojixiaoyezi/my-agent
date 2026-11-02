# LLM: 仅供 runner-retry 离线夹具注入；使用真实原生工具块，不注册为用户模型或直接写验收状态。
# 模块用途: 首次任务调用真实抛错，重试通过 write_file 工具产生证据，再返回普通 final。
from __future__ import annotations

import json
from pathlib import Path

from ...agent.backends import BaseBackend, ModelResponse
from ...agent.backends.base import ProviderToolCapability, _utc_now_iso


class ScenarioRetryBackend(BaseBackend):
    name = "scenario_retry_backend"
    report_content = "# runner retry\n\n第二次 runner 尝试通过原生 write_file 生成了这份隔离证据。\n"

    def __init__(self) -> None:
        self.calls = 0
        self.report_path: Path | None = None

    def probe_tool_capability(self) -> ProviderToolCapability:
        return ProviderToolCapability(
            provider=self.name, endpoint="local://scenario-runner-retry", model="", stream=False,
            native_supported=True, evidence="offline_fixture_emits_native_tool_use_blocks",
            observed_at=_utc_now_iso(),
        )

    def generate(
        self, prompt: str, on_chunk=None, *, tools=None, tool_choice=None, messages=None,
        on_thinking_delta=None, on_tool_input_progress=None, request_options=None,
    ) -> ModelResponse:
        # 故障诊断不是 runner 任务调用，不消耗这三个固定任务响应。
        if "输出严格 JSON 格式" in prompt:
            return ModelResponse(text=json.dumps({
                "analysis_reason": "离线场景模拟临时 runner 失败，允许重试。",
                "root_cause": "transient_runner_failure", "suggested_params": {},
                "should_retry": True, "should_split": False, "confidence": 0.9,
            }, ensure_ascii=False), backend=self.name)
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scenario transient runner failure")
        if self.report_path is None:
            raise RuntimeError("scenario report path is not configured")
        if self.calls == 2:
            if not any(tool.get("name") == "write_file" for tool in tools or []):
                raise RuntimeError("scenario requires an authorized native write_file tool")
            block = {"id": "scenario-retry-write", "name": "write_file", "input": {
                "path": str(self.report_path), "content": self.report_content,
            }}
            return ModelResponse(
                text="", backend=self.name, tool_use_blocks=[block],
                assistant_content_blocks=[{"type": "tool_use", **block}], stop_reason="tool_use",
            )
        if self.calls == 3:
            if not self.report_path.is_file() or self.report_path.read_text(encoding="utf-8") != self.report_content:
                raise RuntimeError("scenario native tool did not produce the expected report")
            return ModelResponse(text="runner retry 的证据报告已经通过 write_file 落盘。", backend=self.name)
        raise RuntimeError("scenario received an unexpected extra runner model call")
