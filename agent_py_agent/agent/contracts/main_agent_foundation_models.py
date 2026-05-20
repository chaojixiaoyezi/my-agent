# LLM: Main-agent foundation models are shared by the runner and case modules.
# 模块用途: 定义主代理基础验收报告的请求、单项结果和总报告数据结构。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# LLM: MainAgentFoundationRequest bundles test runner options without adding end-user config knobs.
# 类用途: 描述主代理基础测试的工作区和是否纳入真实模型结果；默认只跑本地确定性用例。
@dataclass(frozen=True)
class MainAgentFoundationRequest:
    workspace: Path
    include_real_model: bool = False


# LLM: MainAgentFoundationCaseResult is one test category outcome with evidence refs.
# 类用途: 保存单个主代理基础测试类别的状态、中文说明、证据路径和问题列表。
@dataclass(frozen=True)
class MainAgentFoundationCaseResult:
    case_id: str
    title: str
    status: str
    summary: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict keeps reports stable for CLI/frontend/doc display.
    # 函数用途: 转成普通 dict，避免调用方依赖 dataclass 内部结构。
    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


# LLM: MainAgentFoundationReport summarizes all categories without hiding skipped real-model tests.
# 类用途: 保存主代理基础测试总报告；ok 只要求已执行用例没有失败。
@dataclass(frozen=True)
class MainAgentFoundationReport:
    ok: bool
    summary: dict[str, int]
    results: list[MainAgentFoundationCaseResult]

    # LLM: to_dict returns a refs-first payload safe for prompt injection and JSON reports.
    # 函数用途: 输出摘要、状态和证据引用，不携带大文件正文。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "results": [item.to_dict() for item in self.results],
        }


__all__ = [
    "MainAgentFoundationCaseResult",
    "MainAgentFoundationReport",
    "MainAgentFoundationRequest",
]
