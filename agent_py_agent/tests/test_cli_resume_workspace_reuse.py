"""CLI 续跑复用首轮任务目录（EXEC-29c）focused 测试。

背景：EXEC-29/29b 真机失效——bind_cli_run_conversation 返回的 bound 只有
conversation_thread_id，run_workspace 是 agent.run 内部 attach 才生成的，
resume_loop 从 bound 读 workspace 恒为空，续跑轮仍新建"系统续跑-N"目录。
EXEC-29c 改为首轮 run 完成后从 agent._current_run_task_workspace 回写
base_params。本文件覆盖回写行为与失败路径。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.resume_loop import ResumeRunOnce
from agent_py_agent.tests.support.ci_cli_owner_fixtures import isolated_cli_owner_paths


# LLM: 只替换业务运行结果，身份、来源与线程由原临时 owner 和 ConversationStore 维护，不绕过学习政策。
# 类用途: 为工作区续跑回写测试提供真实会话绑定，同时观察原 run 参数。
class _FakeAgent:
    """只提供 bind 与 run 所需属性的最小 agent。"""

    def __init__(self, tmp_path, workspace_root=""):
        self.home_paths = isolated_cli_owner_paths(tmp_path)
        self.conversation_store = ConversationStore(self.home_paths.owner_home_dir / "conversations")
        self.subagents = SimpleNamespace(runtime_db=None)
        self._current_run_task_workspace = workspace_root
        self.run_calls = []
        self.last_result = None

    def run(self, prompt, *, params, save=False, source="", delivery_contract=None, on_chunk=None, resume_context=False):
        self.run_calls.append(
            dict(prompt=prompt, params=params, save=save, source=source,
                 resume_context=resume_context)
        )
        result = self.last_result
        return result


def _params(**overrides) -> RunParams:
    base = dict(
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        source="cli_run",
        task_attributes={},
    )
    base.update(overrides)
    return RunParams(**base)


def _ok_result():
    return SimpleNamespace(runtime_status="ok", response="ok")


def test_first_round_writes_workspace_back_into_base_params(tmp_path):
    """首轮 run 后 base_params 回写 run_workspace（EXEC-29c 主行为）。"""
    root = tmp_path / "tasks" / "我的任务"
    agent = _FakeAgent(tmp_path, workspace_root=str(root))
    agent.last_result = _ok_result()
    runner = ResumeRunOnce(agent, base_params=_params())
    runner(prompt="任务", continuation_seq=0)

    attrs = runner.base_params.task_attributes
    assert attrs["run_workspace"] == {
        "task_root": str(root),
        "output_dir": str(root / "output"),
        "work_dir": str(root / "work"),
    }
    # 不带 conversation_thread_id 之外的会话身份（EXEC-29b 约束保持）
    assert "conversation_thread_id" not in attrs


def test_resume_round_carries_workspace_into_bound_params(tmp_path):
    """续跑轮 apply 后的 params 含首轮 run_workspace（同目录复用）。"""
    root = tmp_path / "tasks" / "我的任务"
    agent = _FakeAgent(tmp_path, workspace_root=str(root))
    agent.last_result = _ok_result()
    runner = ResumeRunOnce(agent, base_params=_params())
    runner(prompt="任务", continuation_seq=0)
    runner(prompt="续跑提示", continuation_seq=1, attempt_id="", continuation_reason="operation_incomplete")

    second = agent.run_calls[1]["params"]
    ws = second.task_attributes.get("run_workspace")
    assert ws is not None
    assert ws["task_root"] == str(root)
    assert ws["work_dir"] == str(root / "work")


def test_no_workspace_when_agent_root_empty(tmp_path):
    """失败路径：agent 未建立 workspace 时不注入脏值。"""
    agent = _FakeAgent(tmp_path, workspace_root="")
    agent.last_result = _ok_result()
    runner = ResumeRunOnce(agent, base_params=_params())
    runner(prompt="任务", continuation_seq=0)

    assert runner.base_params.task_attributes == {}
