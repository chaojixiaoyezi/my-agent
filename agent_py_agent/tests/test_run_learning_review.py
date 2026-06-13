"""主代理自学习复盘钩子钉子(稳而不管 2-3,长期助手 background_review 蓝本)。

钉死契约:
1. 默认关闭零成本:enable_self_learning=false 时零模型调用零落盘。
2. 开启时:一次无工具复盘调用 → LESSON: 行进 learning drafts(待用户审核,
   不直接改正式 skill/记忆);NO_LESSON/空回答=零候选(不为产出而编)。
3. 防御:backend/learning 缺失、调用异常一律静默返回 0,绝不影响主链路。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.run_learning_review import maybe_run_learning_review
from agent_py_agent.agent.subagents.manager import SubAgentManager

pytestmark = pytest.mark.integration


class _FakeBackend:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def generate(self, prompt, on_chunk=None):
        self.calls += 1
        self.prompt = prompt
        return SimpleNamespace(text=self.text)


def _agent(tmp_path: Path, *, enabled: bool, backend_text: str = "NO_LESSON"):
    manager = SubAgentManager(tmp_path / "subagents", enable_self_learning=enabled)
    return SimpleNamespace(
        config=SimpleNamespace(enable_self_learning=enabled),
        subagents=manager,
        backend=_FakeBackend(backend_text),
    )


def _params():
    return SimpleNamespace(user_prompt="分析这些项目并写报告", run_id="run-learn")


def test_disabled_means_zero_calls(tmp_path: Path) -> None:
    agent = _agent(tmp_path, enabled=False, backend_text="LESSON: 不该出现")
    count = maybe_run_learning_review(agent, _params(), {"ok": True, "artifacts": []})
    assert count == 0
    assert agent.backend.calls == 0, "默认关闭必须零模型调用"


def test_lessons_become_draft_candidates(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        enabled=True,
        backend_text="复盘:\nLESSON: 先用全字段+时间倒序检索再下结论\nLESSON: 翻译 PDF 用 pdf2zh 工具链\n闲话忽略",
    )
    report = {"ok": False, "artifacts": [{"path": "a.md"}], "quality_advisories": [{"gate": "expected_outputs_reconciliation"}]}

    count = maybe_run_learning_review(agent, _params(), report)

    assert count == 2
    assert agent.backend.calls == 1
    assert "expected_outputs_reconciliation" in agent.backend.prompt, "复盘输入带收尾事实"
    drafts = agent.subagents.learning.list_learning_candidates()
    lessons = {item.lesson for item in drafts}
    assert "先用全字段+时间倒序检索再下结论" in lessons
    assert all(item.status == "draft" for item in drafts), "只进待审草稿,不直接转正"


def test_no_lesson_and_failures_are_silent(tmp_path: Path) -> None:
    agent = _agent(tmp_path, enabled=True, backend_text="NO_LESSON")
    assert maybe_run_learning_review(agent, _params(), {"ok": True, "artifacts": []}) == 0

    class _Boom:
        def generate(self, prompt, on_chunk=None):
            raise RuntimeError("provider down")

    agent.backend = _Boom()
    assert maybe_run_learning_review(agent, _params(), {"ok": True, "artifacts": []}) == 0, "复盘失败绝不影响主链路"
