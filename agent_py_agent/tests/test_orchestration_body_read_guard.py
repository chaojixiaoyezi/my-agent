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


# LLM: _top_level_agent models the CLI root that has delegated children but is not itself a subagent run.
# 函数用途: 构造无 current_subagent_run_id 的 fake agent，覆盖真实 E2E root 只能读 refs/报告的场景。
def _top_level_agent(tasks):
    return SimpleNamespace(
        root="/tmp/workspace",
        _current_subagent_run_id="",
        subagents=SimpleNamespace(
            load=lambda run_id: tasks[run_id],
            list_runs=lambda: list(tasks.values()),
        ),
    )


# LLM: _predelegation_agent models a fresh root turn before any child run has been created.
# 函数用途: 覆盖 root 尚未派出小傻妞时，派工任务应先传路径而不是吞 data 正文。
def _predelegation_agent():
    return _top_level_agent({})


# LLM: predelegation roots may read brief task files to understand enough to delegate well.
# 函数用途: 用户要求派小傻妞时，root 派工前仍可读 README/目标/rubric 等短说明。
def test_predelegation_root_can_read_brief_before_subagents_created():
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_predelegation_agent(),
            user_prompt="subagent_delegation=true\n请安排小傻妞协作完成这个任务。",
            task_attributes={"subagent_delegation": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/README.md"},
        )
    )

    assert result is None


# LLM: predelegation roots should hand data refs to children instead of reading source bodies first.
# 函数用途: 真实 E2E 暴露 root 派工前吞 data 正文；这里固定为先 create_subagents 并传 required_read_paths。
def test_predelegation_root_blocks_data_body_before_subagents_created():
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_predelegation_agent(),
            user_prompt="subagent_delegation=true\n请安排小傻妞协作完成这个任务。",
            task_attributes={"subagent_delegation": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/data/company_profile.md"},
        )
    )

    assert result is not None
    assert result.ok is False
    assert "predelegation_source_read_blocked" in result.output
    assert "create_subagents" in result.output
    assert "required_read_paths" in result.output


# LLM: bundle-shaped filesystem calls must hit the same refs-first guard as flat path calls.
# 函数用途: 真实模型会写 {"filesystem": {"path": ...}}；派工前 source 正文仍要阻断且提示带路径。
def test_predelegation_root_blocks_bundled_data_body_before_subagents_created():
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_predelegation_agent(),
            user_prompt="subagent_delegation=true\n请安排小傻妞协作完成这个任务。",
            task_attributes={"subagent_delegation": True},
            payload={
                "tool": "read_file",
                "filesystem": {"path": "/tmp/workspace/data/company_profile.md"},
            },
        )
    )

    assert result is not None
    assert "predelegation_source_read_blocked" in result.output
    assert "/tmp/workspace/data/company_profile.md" in result.output


# LLM: artifact bodies also stay out of root context before initial delegation.
# 函数用途: 派工前 root 不应先读取 read_file 大输出 artifact，应把来源路径或 artifact ref 交给下级。
def test_predelegation_root_blocks_plain_artifact_before_subagents_created():
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_predelegation_agent(),
            user_prompt="subagent_delegation=true\n请安排小傻妞协作完成这个任务。",
            task_attributes={"subagent_delegation": True},
            payload={"tool": "read_artifact", "artifact_ref": "read_file-12-1-abc.json"},
        )
    )

    assert result is not None
    assert result.ok is False
    assert "predelegation_source_read_blocked" in result.output


# LLM: predelegation roots still need shell directory discovery before assigning refs.
# 函数用途: 派工前允许 root 用 find/ls 看目录结构，否则无法把正确资料路径交给下级。
def test_predelegation_root_can_shell_list_directories_before_subagents_created():
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_predelegation_agent(),
            user_prompt="subagent_delegation=true\n请安排小傻妞协作完成这个任务。",
            task_attributes={"subagent_delegation": True},
            payload={"tool": "run_command", "command": "find /tmp/workspace -maxdepth 2 -type f"},
        )
    )

    assert result is None


# LLM: old workspace runs must not disable the fresh root-turn refs-first handoff.
# 函数用途: 真实复测复用目录时有历史 run；当前轮未派工前仍应阻断 root 读取 data 正文。
def test_predelegation_root_ignores_historical_runs_when_blocking_data_body():
    agent = _top_level_agent({"old-worker": _task("old-worker", identity="worker", done=True)})
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=agent,
            user_prompt="subagent_delegation=true\n当前目录里是一套材料，请组织多层小傻妞协作完成。",
            task_attributes={"subagent_delegation": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/data/company_profile.md"},
        )
    )

    assert result is not None
    assert "predelegation_source_read_blocked" in result.output


# LLM: ordinary non-delegation reads keep behaving normally.
# 函数用途: 用户只是让 root 自己读取资料时，不启用派工前 source refs 保护。
def test_predelegation_guard_does_not_block_plain_root_read():
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_predelegation_agent(),
            user_prompt="帮我看看这个资料文件。",
            payload={"tool": "read_file", "path": "/tmp/workspace/data/company_profile.md"},
        )
    )

    assert result is None


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


# LLM: delegated parent body guard must not rely on parser-flattened filesystem parameters.
# 函数用途: 委托期父级用 bundle 形态读业务正文时也阻断，避免真实模型参数换形态绕过。
def test_delegating_parent_cannot_read_bundled_product_body_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={
                "tool": "read_file",
                "filesystem": {"path": "/tmp/workspace/deliverables/app.js"},
            },
        )
    )

    assert result is not None
    assert result.ok is False
    assert "delegating_body_read_blocked" in result.output


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


# LLM: task-local runner metadata must remain readable even when product bodies stay blocked.
# 函数用途: 父级 runner 派出子代理后，仍能读取自己 task_dir 下的 output/runner 元数据用于收口。
def test_delegating_parent_can_read_own_task_output_metadata_before_acceptor_done():
    tasks = {"root": _task("root", identity="worker", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": "/tmp/runtime/subagents/root/output.json"},
        )
    )

    assert result is None


# LLM: task-local compact packets are recovery control-plane refs, not product body reads.
# 函数用途: 父级恢复自己时必须能读 latest_continue_packet/checkpoint/summary，否则 packet-first 接续会被 guard 卡住。
def test_delegating_parent_can_read_own_task_local_continue_packet_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请从 latest_continue_packet 接着跑。",
            payload={
                "tool": "read_file",
                "path": (
                    "/tmp/runtime/subagents/tasks/root/agents/root/"
                    "compactions/latest_continue_packet.json"
                ),
            },
        )
    )

    assert result is None


# LLM: Child task metadata must be visible to a parent that is trying to recover or close a delegated run.
# 函数用途: 防止父级 refs-only 状态下连子代理 output/status 也读不到，从而卡在恢复/验收循环里。
def test_delegating_parent_can_read_child_task_output_metadata_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": "/tmp/runtime/subagents/worker/output.json"},
        )
    )

    assert result is None


# LLM: A delegating worker still owns its task-local deliverables and must be able to inspect them.
# 函数用途: worker 读回自己刚写的产物不应被父级 refs-only 保护误挡；保护的是父级偷看下级正文。
def test_delegating_worker_can_read_own_task_local_product_body_before_acceptor_done():
    tasks = {"root": _task("root", identity="worker", children=["tester"]), "tester": _task("tester")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": "/tmp/runtime/subagents/root/算法测试方案.md"},
        )
    )

    assert result is None


# LLM: Dispatch report JSON is a refs-only orchestration summary, not product body text.
# 函数用途: 父级恢复时可以读取 subagent_dispatch_report，避免因看不到调度摘要而重复创建 repair。
def test_delegating_parent_can_read_subagent_dispatch_report_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_file", "path": "/tmp/workspace/_runtime/subagents/subagent_dispatch_report.json"},
        )
    )

    assert result is None


# LLM: orchestration artifacts are refs-only status packets, so parent recovery may read them.
# 函数用途: 委托期允许读取 dispatch/subagent_board 小摘要 artifact，避免父级被迫盲目恢复。
def test_delegating_parent_can_read_orchestration_artifact_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={
                "tool": "read_artifact",
                "artifact_ref": "/tmp/workspace/memory_archive/artifacts/tool_outputs/dispatch_subagents-1-abc.json",
            },
        )
    )

    assert result is None


# LLM: non-orchestration artifacts still stay blocked until acceptor completes.
# 函数用途: 验收完成前不允许父级读取普通大 artifact 正文，避免 root 上下文反向膨胀。
def test_delegating_parent_cannot_read_artifact_body_before_acceptor_done():
    tasks = {"root": _task("root", identity="coordinator", children=["worker"]), "worker": _task("worker")}
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_agent(tasks),
            user_prompt="请让子代理先做，最后按验收标准收口。",
            payload={"tool": "read_artifact", "artifact_ref": "child-output-full-body-1-abc.json"},
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
            user_prompt="parent_body_read=allow\n子代理做完之后，你自己做一下验收，你亲自看一下页面。",
            task_attributes={"parent_body_read": "allow"},
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


# LLM: top-level CLI roots also need refs-only protection after delegation starts.
# 函数用途: root 不是 subagent run 时，只要当前提示明确要求“只调度下级、只读 refs/报告”，验收前也不能读产物正文。
def test_top_level_refs_only_root_cannot_read_product_body_before_acceptor_done():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
        "tester": _task("tester", identity="tester", done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\nroot 只能创建和调度下级，并读 refs/报告；不要直接读取业务产物正文。",
            task_attributes={"refs_only": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/index.html"},
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output
    assert "create_subagents" in result.output
    assert "schedule_child_subagents" not in result.output


# LLM: natural Chinese "only use child report" should trigger top-level refs-only mode.
# 函数用途: 用户不用 refs-only 术语、只说“根据小傻妞报告收口”时，root 也不能验收前偷读产物正文。
def test_top_level_natural_report_only_prompt_blocks_product_body_before_acceptor_done():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\n请不要亲自写页面，安排小傻妞完成后你只根据小傻妞的报告做收口。",
            task_attributes={"refs_only": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/index.html"},
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output


# LLM: Real user wording may include "你自己直接写页面正文" instead of refs-only jargon.
# 函数用途: 购物站真实 E2E 暴露 root 读正文保护漏识别这类普通话术；这里固定为验收前阻断。
def test_top_level_delegate_only_prompt_with_you_directly_write_text_blocks_product_body():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\n请你安排小傻妞来完成，不要你自己直接写页面正文。",
            task_attributes={"refs_only": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/index.html"},
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output


# LLM: top-level refs-only shell reads should not bypass read_file body protection.
# 函数用途: root 用 run_command tail/cat 等读取产物正文时也要阻断，避免绕过子代理报告边界。
def test_top_level_natural_report_only_prompt_blocks_shell_tail_product_body():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\n请不要亲自写页面，安排小傻妞完成后你只根据小傻妞的报告做收口。",
            task_attributes={"refs_only": True},
            payload={"tool": "run_command", "command": "tail -20 /tmp/workspace/deliverables/index.html"},
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output
    assert "run_command" in result.output


# LLM: Shell body-read guard must understand common cd-and-read command chains.
# 函数用途: root 用 `cd 产物目录 && grep/cat 文件` 读取页面正文时也要阻断，不能绕过 read_file 保护。
def test_top_level_natural_report_only_prompt_blocks_shell_cd_grep_product_body():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\n请不要亲自写页面，安排小傻妞完成后你只根据小傻妞的报告做收口。",
            task_attributes={"refs_only": True},
            payload={
                "tool": "run_command",
                "command": "cd /tmp/workspace/deliverables && grep -c 'href=\"#\"' index.html",
            },
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output


# LLM: orchestration metadata may still be inspected through shell reads.
# 函数用途: 委托期允许 root 用 shell 查看 subagent_dispatch_report 这类控制面文件，不把父级恢复卡死。
def test_top_level_refs_only_root_can_shell_read_orchestration_metadata():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\n请不要亲自写页面，安排小傻妞完成后你只根据小傻妞的报告做收口。",
            task_attributes={"refs_only": True},
            payload={
                "tool": "run_command",
                "command": "cat /tmp/workspace/_runtime/subagents/subagent_dispatch_report.json",
            },
        )
    )

    assert result is None


# LLM: top-level refs-only protection unlocks after a real acceptor has completed.
# 函数用途: 最终验收子代理 DONE/VERIFIED 后，root 才能进入最后读正文核查阶段。
def test_top_level_refs_only_root_can_read_product_body_after_acceptor_done():
    tasks = {
        "worker": _task("worker", identity="worker", done=True),
        "tester": _task("tester", identity="tester", done=True),
        "acceptor": _task("acceptor", identity=("acceptor", "小傻妞-验收"), done=True),
    }
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=_top_level_agent(tasks),
            user_prompt="refs_only=true\nroot 只能创建和调度下级，并读 refs/报告；不要直接读取业务产物正文。",
            task_attributes={"refs_only": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/index.html"},
        )
    )

    assert result is None


# LLM: Current-turn scope prevents old acceptor runs from unlocking a fresh delegated root.
# 函数用途: 复用 subagent_workspace 时，旧任务里的 VERIFIED acceptor 不能让本轮 root 偷读新产物正文。
def test_top_level_scope_ignores_old_acceptor_when_blocking_product_body():
    tasks = {
        "current-worker": _task("current-worker", identity="worker", done=True),
        "old-acceptor": _task("old-acceptor", identity=("acceptor", "旧验收"), done=True),
    }
    agent = _top_level_agent(tasks)
    agent._orchestration_run_ids_seen = {"current-worker"}

    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=agent,
            user_prompt="refs_only=true\n请不要亲自写页面，安排小傻妞完成后你只根据小傻妞的报告做收口。",
            task_attributes={"refs_only": True},
            payload={"tool": "read_file", "path": "/tmp/workspace/deliverables/index.html"},
        )
    )

    assert result is not None
    assert "delegating_body_read_blocked" in result.output
