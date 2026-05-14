from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration_direct_write_guard import (
    DelegateOnlyDirectWriteGuardRequest,
    maybe_block_delegate_only_direct_write,
)


# LLM: _agent keeps direct-write guard tests independent from real runtime services.
# 函数用途: 构造只带 root 字段的 fake agent，足够测试路径归一。
def _agent():
    return SimpleNamespace(root="/tmp/workspace")


def _runner_agent(role: str):
    task = SimpleNamespace(role=role, agent_name=f"小小傻妞-{role}")
    return SimpleNamespace(
        root="/tmp/workspace",
        _current_subagent_run_id="run-1",
        subagents=SimpleNamespace(load=lambda _run_id: task),
    )


def test_delegate_only_prompt_blocks_root_write_file_to_deliverables():
    """用户要求通过子代理完成时，root 不能直接 write_file 写业务产物。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_agent(),
            user_prompt="只能通过子代理完成，root 不能自己直接写购物网站代码。",
            payload={"tool": "write_file", "path": "/tmp/workspace/deliverables/index.html", "content": "..."},
        )
    )

    assert result is not None
    assert result.ok is False
    assert "delegated_direct_write_blocked" in result.output
    assert "dispatch_subagents" in result.output


def test_delegate_only_prompt_blocks_shell_redirection_write():
    """shell 里直接 cat/echo 重定向写产物也会被拦住。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_agent(),
            user_prompt="必须通过 builder 子代理实现，root must not write deliverables.",
            payload={"tool": "run_command", "command": "cat > deliverables/index.html <<'EOF'\n...\nEOF"},
        )
    )

    assert result is not None
    assert result.ok is False
    assert "delegated_direct_write_blocked" in result.output


def test_delegate_only_prompt_allows_leaf_worker_product_write():
    """被派去交付的 leaf_worker 应能写自己的产物，guard 只拦 root/父级偷写。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_runner_agent("leaf_worker"),
            user_prompt="请派小傻妞来做，不要你自己亲自写页面。",
            payload={"tool": "write_file", "path": "/tmp/workspace/artifacts/index.html", "content": "..."},
        )
    )

    assert result is None


def test_delegate_only_prompt_still_blocks_coordinator_product_write():
    """coordinator 可以写协调报告，但不能代替 worker 直接写最终页面。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_runner_agent("coordinator"),
            user_prompt="请派小傻妞来做，不要你自己亲自写页面。",
            payload={"tool": "write_file", "path": "/tmp/workspace/artifacts/index.html", "content": "..."},
        )
    )

    assert result is not None
    assert result.ok is False


def test_delegate_only_prompt_allows_read_only_shell_commands():
    """观察目录状态不算直接写产物，root 仍可用于 E2E 观察。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_agent(),
            user_prompt="只能通过子代理完成。",
            payload={"tool": "run_command", "command": "ls -la deliverables"},
        )
    )

    assert result is None


def test_normal_prompt_does_not_block_direct_write():
    """普通用户任务不触发 delegate-only 写入保护。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_agent(),
            user_prompt="请创建一个简单页面。",
            payload={"tool": "write_file", "path": "/tmp/workspace/deliverables/index.html", "content": "..."},
        )
    )

    assert result is None


def test_delegate_only_prompt_allows_runtime_report_write():
    """协调报告和 task-local 元数据仍可写，避免 root/父级无法记录恢复事实。"""
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
            agent=_agent(),
            user_prompt="只能通过子代理完成。",
            payload={
                "tool": "write_file",
                "path": "/tmp/workspace/_runtime/subagents/subagent-1/WORK_LOG.md",
                "content": "- recovery note\n",
            },
        )
    )

    assert result is None
