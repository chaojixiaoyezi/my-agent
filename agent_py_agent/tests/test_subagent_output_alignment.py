"""R4 子项①钉子测试：声明产物落点对齐到任务交付区（家目录方案）。

复刻 R4 GoAttack 根因（docs/audits/R4-goattack-20260611.md）：主代理派工的
output_files 子代理写不进去、任务死锁。修法（用户确认的家目录方案）：默认落点是
任务交付区 tasks/<日期>/<任务>/output/（delivery_root），子代理直接写、用户拿走即可；
交付区显式授权进写边界；只有声明落在任务目录之外才记 delivery_map 走兜底搬运。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.subagents.context_bundle_contracts import (
    allowed_write_roots,
    output_contract,
    task_packet,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.services.output_alignment import (
    OUTPUT_TARGET_LOCKED_CODE,
    anchored_output_refs,
    delivery_root,
    output_write_grant_roots,
)


def _create_task(tmp_path: Path, output_files: list[str], **kwargs):
    manager = SubAgentManager(workspace=tmp_path / "workspace")
    task = manager.create_run(
        params=CreateRunParams(
            goal="复刻平台后端",
            thought="先理解边界",
            plan=["写代码"],
            role="worker",
            attributes={"output_files": list(output_files)},
            **kwargs,
        ),
    )
    return manager, task


def _inside(path_text: str, roots: list[str]) -> bool:
    resolved = Path(path_text).resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def test_delivery_root_defaults_to_task_output_dir(tmp_path: Path) -> None:
    # 默认交付区 = 任务工作区/output（用户主目录下 tasks/<日期>/<任务>/output 的形态）
    _, task = _create_task(tmp_path, ["app/main.py"])
    assert delivery_root(task) == str(Path(task.task_workspace_dir) / "output")


def test_explicit_output_dir_overrides_default(tmp_path: Path) -> None:
    # 主代理/用户显式指定交付目录时用指定的
    manager, task = _create_task(tmp_path, ["app/main.py"])
    custom = str(tmp_path / "user-chosen-deliverables")
    task.attributes = {**task.attributes, "run_workspace": {"output_dir": custom}}
    assert delivery_root(task) == custom


def test_relative_declarations_anchor_into_delivery_root_no_move(tmp_path: Path) -> None:
    # R4 主形态：相对声明锚到交付区，可写、用户拿走即可，无需搬运（delivery_map 空）
    _, task = _create_task(tmp_path, ["goattack-python/app/__init__.py", "goattack-python/requirements.txt"])
    delivery = delivery_root(task)
    anchoring = anchored_output_refs(task)
    assert all(ref.startswith(delivery) for ref in anchoring.anchored_refs)
    assert anchoring.anchored_refs[0].endswith("goattack-python/app/__init__.py")
    assert anchoring.delivery_map == []
    # attributes 声明保持原样不被改写
    assert task.attributes["output_files"] == ["goattack-python/app/__init__.py", "goattack-python/requirements.txt"]


def test_declarations_already_in_delivery_root_stay_untouched(tmp_path: Path) -> None:
    # 绝对路径已指向交付区 → 原样保留、不搬运（R4 真实数据形态）
    _, task = _create_task(tmp_path, ["placeholder.md"])
    declared = str(Path(delivery_root(task)) / "goattack-python" / "config.py")
    task.attributes = {**task.attributes, "output_files": [declared]}
    anchoring = anchored_output_refs(task)
    assert anchoring.anchored_refs == [declared]
    assert anchoring.delivery_map == []


def test_declaration_outside_task_gets_relocated_and_recorded(tmp_path: Path) -> None:
    # 声明落在任务目录之外 → 重定位到交付区 + 记 delivery_map 供兜底搬运
    manager, task = _create_task(tmp_path, ["placeholder.md"])
    outside = str(tmp_path / "somewhere-else" / "report.md")
    task.attributes = {**task.attributes, "output_files": [outside]}
    anchoring = anchored_output_refs(task)
    assert anchoring.anchored_refs[0].startswith(delivery_root(task))
    assert any(item["to"] == outside and item["reason"].startswith("relocated") for item in anchoring.delivery_map)


def test_delivery_root_is_granted_into_write_boundary(tmp_path: Path) -> None:
    # 交付区必须进真实写边界，子代理才写得进去（R4 死因正是写不进交付区）
    manager, task = _create_task(tmp_path, ["goattack-python/requirements.txt"])
    boundary = manager.runner_context._build_write_boundary(task)
    assert _inside(output_write_grant_roots(task)[0], boundary["allowed_write_roots"])
    # 模型可见的 allowed_write_roots 也含交付区
    assert _inside(delivery_root(task), allowed_write_roots(task))


def test_anchored_refs_pass_write_boundary(tmp_path: Path) -> None:
    # 端到端：执行合同给出的落点必须能通过真实写入门禁
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

    manager, task = _create_task(tmp_path, ["goattack-python/app/models/scan.py"])
    target = task_packet(task)["write_contract"]["required_file_refs"][0]
    boundary = manager.runner_context._build_write_boundary(task)
    error = validate_write_boundary(
        "write_file",
        {"path": target, "content": "code"},
        workspace_root=Path(task.task_workspace_dir),
        write_boundary={
            "allowed_write_roots": list(boundary["allowed_write_roots"]),
            "locked_files": list(task.locked_files or []),
        },
    )
    assert error == ""


def test_non_path_declarations_stay_untouched(tmp_path: Path) -> None:
    _, task = _create_task(tmp_path, ["https://example.com/spec"])
    anchoring = anchored_output_refs(task)
    assert anchoring.delivery_map == []


def test_anchoring_is_stable_and_pure(tmp_path: Path) -> None:
    manager, task = _create_task(tmp_path, ["app/core/scanner.py"])
    first = anchored_output_refs(task)
    manager.save(task)
    second = anchored_output_refs(manager.load(task.id))
    assert first.anchored_refs == second.anchored_refs
    assert first.delivery_map == second.delivery_map
    # 纯投影：attributes 不被改写
    assert manager.load(task.id).attributes["output_files"] == ["app/core/scanner.py"]


def test_relative_escape_falls_back_to_basename(tmp_path: Path) -> None:
    _, task = _create_task(tmp_path, ["../../outside/evil.py"])
    anchoring = anchored_output_refs(task)
    assert anchoring.anchored_refs == [str(Path(delivery_root(task)) / "evil.py")]
    assert anchoring.anchored_refs[0].startswith(delivery_root(task))


def test_locked_delivery_target_records_structured_warning(tmp_path: Path) -> None:
    # R4 死锁形态：交付落点被 locked_files 盖住 → 结构化 warning，不静默
    _, task = _create_task(tmp_path, ["app/main.py"])
    task.locked_files = [delivery_root(task)]
    contract = output_contract(task)
    warnings = contract["write_contract_warnings"]
    assert any(w["code"] == OUTPUT_TARGET_LOCKED_CODE for w in warnings)


def test_delivery_map_rendered_into_runner_packet_lines(tmp_path: Path) -> None:
    # 重定位场景下 runner prompt 渲染必须带 delivery map
    from agent_py_agent.agent.subagents.context_bundle_contracts import render_task_packet_lines

    manager, task = _create_task(tmp_path, ["placeholder.md"])
    outside = str(tmp_path / "elsewhere" / "out.md")
    task.attributes = {**task.attributes, "output_files": [outside]}
    lines = render_task_packet_lines(task_packet(task))
    delivery_line = next(line for line in lines if line.startswith("- output_delivery_map:"))
    assert "=>" in delivery_line


def test_runner_result_delivers_relocated_output_to_declared_location(tmp_path: Path) -> None:
    # R4 子项④端到端：声明落在任务内非交付区时，runner result 写回按 delivery_map
    # 把交付区落点的真实产物搬到声明意图位置。
    from agent_py_agent.agent.subagents.manager_runner_result_payload import (
        RecordRunnerResultParams,
    )
    from agent_py_agent.agent.subagents.models import SubAgentParsedOutput

    manager, task = _create_task(tmp_path, ["placeholder.md"])
    declared = str(Path(task.task_workspace_dir) / "work" / "handoff" / "config.py")
    task.attributes = {**task.attributes, "output_files": [declared]}
    manager.save(task)
    task = manager.load(task.id)
    anchoring = anchored_output_refs(task)
    anchored_target = Path(anchoring.anchored_refs[0])
    anchored_target.parent.mkdir(parents=True, exist_ok=True)
    anchored_target.write_text("DATABASE_URL = 'postgres://...'\n", encoding="utf-8")

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="done",
            structured_output=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="DONE",
                summary="后端配置完成",
                findings=[{"claim": "配置已写", "evidence_refs": [str(anchored_target)]}],
                next_actions=[],
            ),
        )
    )
    assert result.ok is True
    assert Path(declared).exists()
    assert "DATABASE_URL" in Path(declared).read_text(encoding="utf-8")
    loaded = manager.load(task.id)
    assert any(r["status"] == "delivered" for r in loaded.attributes["output_delivery_results"])


# ---------------------------------------------------------------------------
# A2 钉子:runner 执行期写边界与 canonical 一致 + R4b narrowing 形态固化
# ---------------------------------------------------------------------------


def test_ancestor_forbidden_root_does_not_block_granted_delivery_root(tmp_path: Path) -> None:
    """R4b 实测复盘固化:forbidden 含交付区【祖先】(如 /Users/<user>)时不拦交付区。

    _forbidden_root_blocks_target 的 narrowing 语义:forbidden root 只有落在某个
    allowed root【之内】才收窄;allowed root 的祖先级 forbidden(默认家目录保护)
    不得吞掉显式授权的交付区。R4b 子代理的"授权又禁止"形态,机制判定必须是放行;
    若此钉子失败,说明有人改了 narrowing 方向,会让所有默认派工直接死锁(R4 形态回归)。
    """
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

    manager, task = _create_task(tmp_path, ["backend/app/core/__init__.py"])
    boundary = manager.runner_context._build_write_boundary(task)
    target = str(Path(delivery_root(task)) / "backend" / "app" / "core" / "__init__.py")

    error = validate_write_boundary(
        "write_file",
        {"path": target, "content": "x"},
        workspace_root=Path(task.task_workspace_dir),
        write_boundary={
            "allowed_write_roots": list(boundary["allowed_write_roots"]),
            # tmp_path 是交付区的祖先,模拟 R4b 的 /Users/example 形态
            "forbidden_write_roots": [str(tmp_path)],
            "locked_files": [],
        },
    )
    assert error == "", f"祖先级 forbidden 不得拦已授权交付区,实际被拦: {error}"

    # 对照:forbidden 真落在 allowed 之内(narrowing 成立)时必须拦——双向钉死语义
    inner_forbidden = str(Path(delivery_root(task)) / "backend" / "app" / "core")
    error_inner = validate_write_boundary(
        "write_file",
        {"path": target, "content": "x"},
        workspace_root=Path(task.task_workspace_dir),
        write_boundary={
            "allowed_write_roots": list(boundary["allowed_write_roots"]),
            "forbidden_write_roots": [inner_forbidden],
            "locked_files": [],
        },
    )
    assert "forbidden_write_roots" in error_inner, "allowed 内的 forbidden 子树必须收窄拦截"


def test_runner_boundary_fields_mirror_canonical_task_fields(tmp_path: Path) -> None:
    """boundary 的 forbidden/locked 必须恒等于 task 字段(同源,无中间改写)。

    R4b 审计的"未完全排除项"正是运行时 boundary 与 canonical 可能不一致;
    本钉子钉死同源关系,排除中间层漂移。
    """
    manager, task = _create_task(tmp_path, ["app/main.py"])
    task.forbidden_write_roots = [str(tmp_path / "secret")]
    task.locked_files = [str(tmp_path / "locked.txt")]
    manager.save(task)

    reloaded = manager.load(task.id)
    boundary = manager.runner_context._build_write_boundary(reloaded)
    assert boundary["forbidden_write_roots"] == reloaded.forbidden_write_roots
    assert boundary["locked_files"] == reloaded.locked_files


def test_first_attempt_execution_context_already_grants_delivery_root(tmp_path: Path) -> None:
    """首轮 attempt(零 grant、零重试)的执行上下文就必须含交付区,且重复构造稳定。

    run_subagent_flow 会构造两次执行上下文(probe 前后);两次的写边界必须一致,
    且无需任何 capability grant 介入,首轮即可写交付区——排除"早期轮次没授权、
    后期才补"的时序窗口(R4b 时成时败疑点)。
    """
    manager, task = _create_task(tmp_path, ["backend/app/main.py"])

    ctx_first = manager.runner_context.build_execution_context(task.id)
    ctx_second = manager.runner_context.build_execution_context(task.id)

    for ctx in (ctx_first, ctx_second):
        assert _inside(delivery_root(task), list(ctx.write_boundary["allowed_write_roots"]))
    assert ctx_first.write_boundary["allowed_write_roots"] == ctx_second.write_boundary["allowed_write_roots"]
    assert ctx_first.write_boundary["forbidden_write_roots"] == ctx_second.write_boundary["forbidden_write_roots"]
    assert ctx_first.write_boundary["locked_files"] == ctx_second.write_boundary["locked_files"]


def test_placeholder_artifacts_are_marked_and_projected(tmp_path: Path) -> None:
    """P2-2 钉子:兜底渲染的占位产物带 placeholder 标记+独立账本,closeout 投影
    单列计数——占位符不得冒充真交付(R5a 交付区占位文件形态的明示)。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
        _compact_children,
    )
    from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
    from agent_py_agent.agent.subagents.result_artifact_evidence import (
        materialize_missing_declared_output_artifacts,
    )

    manager, task = _create_task(tmp_path, ["analysis.md"])
    parsed = SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="结构化摘要")

    materialized = materialize_missing_declared_output_artifacts(task, parsed, [])

    assert materialized and materialized[0]["placeholder"] is True
    assert task.attributes["placeholder_artifacts"], "占位事实必须进独立账本"
    manager.save(task)

    reloaded = manager.load(task.id)
    rows = _compact_children([{"run_id": reloaded.id, "attributes": dict(reloaded.attributes)}])
    assert rows[0]["placeholder_artifact_count"] == 1


def test_declared_output_roots_granted_without_environment_fields(tmp_path):
    """R8 接力实锤钉子:delivery_root 的环境字段(run_workspace 等)缺失时,
    子代理写 output_files 声明位置曾被自家写边界 WRITE_FORBIDDEN(R8a 实测
    17 次拒、需 capreq 救场)。声明产物目录过围栏后必须直接进写边界。"""
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(goal="分析X并写报告", thought="t", plan=["p"])
    target_dir = Path(task.task_workspace_dir) / "output"
    attrs = dict(task.attributes or {})
    attrs["output_files"] = [str(target_dir / "X-架构分析.md")]
    attrs.pop("run_workspace", None)  # 模拟创建链未透传环境字段
    task.attributes = attrs
    manager.save(task)

    boundary = manager.runner_context._build_write_boundary(manager.load(task.id))
    error = validate_write_boundary(
        "write_file",
        {"path": str(target_dir / "X-架构分析.md"), "content": "x"},
        workspace_root=tmp_path,
        write_boundary=boundary,
    )

    assert error == "", f"声明位置必须可写,实际被拒: {error}"
    assert any(str(target_dir) == root for root in boundary["allowed_write_roots"])


def test_declared_output_roots_outside_fence_not_granted(tmp_path):
    """围栏是客观事实硬门:声明指向任务链外的任意路径不得自动授权(防自我扩权),
    仍走 capability_request 流程。"""
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(goal="分析Y", thought="t", plan=["p"])
    import tempfile
    outside = Path(tempfile.mkdtemp(prefix="outside-fence-")) / "Y-报告.md"  # 真围栏外:tmp_path 在 manager.workspace_root(主代理 workspace 语义)围栏内
    attrs = dict(task.attributes or {})
    attrs["output_files"] = [str(outside)]
    attrs.pop("run_workspace", None)
    task.attributes = attrs
    manager.save(task)

    boundary = manager.runner_context._build_write_boundary(manager.load(task.id))

    assert not any(str(outside.parent) == root for root in boundary["allowed_write_roots"]), \
        "围栏外的声明目录不得自动进写边界"
