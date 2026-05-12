from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration_body_read_guard import (
    DelegatingBodyReadGuardRequest,
    maybe_block_delegating_body_read,
)


# LLM: _Task keeps guard tests independent from manager persistence details.
# 函数用途: 创建最小 fake task，只覆盖委托读正文保护需要的字段。
def _task(run_id, *, identity="worker", children=None, done=False):
    role, agent_name = identity if isinstance(identity, tuple) else (identity, identity)
    status = "DONE" if done else "PLANNING"
    verification = "VERIFIED" if done else "UNVERIFIED"
    return SimpleNamespace(
        id=run_id,
        role=role,
        agent_name=agent_name,
        child_ids=list(children or []),
        status=status,
        verification_status=verification,
        task_dir=f"/tmp/runtime/subagents/{run_id}",
    )


# LLM: _Agent provides current runner context and a tiny subagent manager for guard tests.
# 函数用途: 构造带 root/current_subagent_run_id/subagents.load 的 fake agent。
def _agent(tasks):
    return SimpleNamespace(
        root="/tmp/workspace",
        _current_subagent_run_id="root",
        subagents=SimpleNamespace(load=lambda run_id: tasks[run_id]),
    )


# LLM: read_file body guard test captures the user's refs-only parent rule.
# 函数用途: 父级已有下级且验收代理未完成时，读取业务产物正文会被阻断。
def test_delegating_parent_cannot_read_product_body_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/app.js"},
        )
    )

    assert result is not None
    assert result.ok is False
    assert "delegating_body_read_blocked" in result.output
    assert "subagent_board" in result.output
    assert "acceptor" in result.output


# LLM: metadata reads stay available so parents can recover from blocked children without reading products.
# 函数用途: 委托期允许读取 task/status/验收类元数据文件。
def test_delegating_parent_can_read_runtime_metadata_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": "/tmp/workspace/.my_agent_runtime/subagents/worker/task.json"},
        )
    )

    assert result is None


# LLM: read_artifact guard prevents parents from expanding large child/dispatch artifacts too early.
# 函数用途: 验收完成前 read_artifact 正文读取也被阻断，避免 root 上下文反向膨胀。
def test_delegating_parent_cannot_read_artifact_body_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_artifact", "artifact_ref": "dispatch_subagents-1-abc"},
        )
    )

    assert result is not None
    assert "read_artifact" in result.output


# LLM: completed acceptor unlocks final parent inspection.
# 函数用途: 子树中已有 VERIFIED acceptor 后，父级可以进入最后验收读正文阶段。
def test_completed_acceptor_allows_final_parent_body_read():
    tasks = {
        "root": _task("root", identity="coordinator", children=["worker", "acceptor"]),
        "worker": _task("worker"),
        "acceptor": _task(
            "acceptor",
            identity=("acceptor", "小傻妞-验收"),
            done=True,
        ),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": str(Path("/tmp/workspace/deliverables/app.js"))},
        )
    )

    assert result is None


# LLM: explicit user override lets the parent do final inspection when the user asks for it.
# 函数用途: 用户明确要求“你自己验收/你看一下”时，父级读正文保护临时放行当前 run。
def test_user_can_explicitly_authorize_parent_body_read_for_acceptance():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="子代理做完之后，你自己做一下验收，你亲自看一下页面。",
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/app.js"},
        )
    )

    assert result is None


# LLM: generic acceptance criteria must not accidentally unlock parent body reads.
# 函数用途: 避免“主代理观察入口 + 验收标准”这类普通任务说明被误判成用户授权父级亲自读正文。
def test_generic_acceptance_criteria_does_not_authorize_parent_body_read():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="你是主代理观察入口。验收标准：必须有 tester 和 acceptor。",
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/app.js"},
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output
