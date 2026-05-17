# LLM: Shop repair-wave Live Lab case forces a failed child before asking root to recover it.
# 模块用途: 预置一个真实失败的小傻妞购物站 run，然后用自然语言测试 root 是否会派修复小傻妞闭环。

from __future__ import annotations

import json
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_dispatch_closeout_resolution import (
    blocking_task_ids,
    task_resolved_for_closeout,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_base import SubAgentManagerInitParams

from .shop_case import _assert_shop_html_output, _assert_static_site_check_clean
from .state_assertions import (
    assert_no_subagent_state_blockers,
    assert_persisted_subagent_state_clean,
)


# LLM: ShopRepairSeed is the small handoff record returned by deterministic Live Lab setup.
# 类用途: 保存失败 seed 的 run_id 和 HTML 产物路径，方便测试和真实 case 后续断言。
@dataclass(frozen=True)
class ShopRepairSeed:
    run_id: str
    output_path: Path


# LLM: case_natural_shop_repair_wave validates the failure-to-repair loop with a real root model call.
# 函数用途: 先制造一个购物站验收失败 run，再让主代理用普通用户话术安排小傻妞修复并完成验收。
def case_natural_shop_repair_wave(lab) -> None:
    """Run a real-LLM repair wave starting from a rejected shopping child."""

    lab.section("CASE natural_shop_repair_wave")
    seed = seed_failed_shop_child(lab.fixture_root)
    prompt = _natural_shop_repair_wave_prompt()
    lab.record_prompt("natural_shop_repair_wave", prompt)
    lab.log(f"seed_failed_run_id={seed.run_id}")
    lab.run_command(lab.agent_command("gateway", "start", "--force"), timeout=90)
    try:
        response = lab.run_command(
            lab.agent_command(
                "gateway",
                "ask",
                prompt,
                "--timeout",
                str(lab.args.timeout),
                "--json",
            ),
            timeout=lab.args.timeout + 240,
        )
        response_path = lab.responses_dir / "natural_shop_repair_wave.stdout.json"
        response_path.write_text(response.stdout, encoding="utf-8")
        lab.log(f"response_file={response_path}")
        assert_no_subagent_state_blockers(response.stdout)
    finally:
        lab.run_command(
            lab.agent_command("gateway", "stop", "--timeout", "15", "--kill", "--reason", "live lab done"),
            timeout=45,
            allow_fail=True,
        )
    output_path = lab.fixture_root / "lab_outputs" / "shop-demo" / "index.html"
    _assert_shop_html_output(output_path)
    _assert_static_site_check_clean(lab.fixture_root, output_path.parent)
    assert_shop_repair_wave_created(lab.fixture_root, seed.run_id)
    assert_persisted_subagent_state_clean(lab.fixture_root)
    lab.log(f"natural_shop_repair_wave_output={output_path}")


# LLM: _natural_shop_repair_wave_prompt is intentionally user-like so the canary catches jargon dependence.
# 函数用途: 生成失败后修复测试的自然语言提示词；不出现 dispatch、runner、contract 等内部术语。
def _natural_shop_repair_wave_prompt() -> str:
    return textwrap.dedent(
        """
        刚刚那个购物网站没通过检查。请你看一下当前小傻妞留下的状态和检查结果，
        再安排小傻妞把它修好，不要你自己直接写页面正文。

        目标还是 lab_outputs/shop-demo/index.html。
        这个页面要能演示注册、登录、浏览商品、加入购物车、结算、下单成功的完整流程。
        不要空链接、不要 disabled 按钮、不要外部图片、外部字体、外部脚本或外部 CSS。
        修好后请安排检查，确认文件存在、能作为网页打开、按钮都有真实动作。
        最后告诉我保存路径和检查结果。
        """
    ).strip()


# LLM: seed_failed_shop_child creates a real manager task so repair logic reads normal task.json reports.
# 函数用途: 给 Live Lab 预置一个 AWAITING_ACCEPTANCE 的失败购物站 run，包括坏 HTML、output.json 和验收失败报告。
def seed_failed_shop_child(fixture_root: Path) -> ShopRepairSeed:
    root = Path(fixture_root)
    output = root / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_broken_shop_html(), encoding="utf-8")
    manager = _seed_manager(root)
    task = manager.create_run(
        goal=f"初版购物站未通过检查，需要修复 {output}",
        thought="Live Lab seed: first child produced a broken shopping page.",
        plan=["保留失败产物", "等待父级安排修复"],
        agent_name="小傻妞-初版失败",
        role="worker",
        extra_write_roots=[str(output.parent)],
        acceptance_checks=[
            "购物站必须有完整注册、登录、购物车、结算和下单成功流程",
            "required_dom_ids: register, login, catalog, cart, checkout, order-confirmation",
        ],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "UNVERIFIED"
    task.artifact_refs = [str(output)]
    task.result = _seed_result(output)
    _write_seed_output(task, output)
    _write_failed_reports(task, output)
    manager.save(task)
    return ShopRepairSeed(run_id=task.id, output_path=output)


# LLM: assert_shop_repair_wave_created verifies repair by task facts and target coverage, not final prose.
# 函数用途: 检查失败 seed 后确实出现 DONE/VERIFIED 的修复小傻妞，并且覆盖同一个购物站产物。
def assert_shop_repair_wave_created(fixture_root: Path, seed_run_id: str) -> None:
    tasks = _task_snapshots(Path(fixture_root))
    seed = _task_by_id(tasks, seed_run_id)
    repairs = [task for task in tasks if _is_verified_repair(task, seed_run_id)]
    if not repairs:
        raise RuntimeError("没有发现已验证的修复小傻妞。")
    if seed and not task_resolved_for_closeout(seed, tasks):
        raise RuntimeError(f"失败 seed 没有被修复 run 覆盖: {seed_run_id}")
    blockers = blocking_task_ids(tasks)
    if blockers:
        raise RuntimeError(f"修复后仍有未解决的小傻妞状态: {', '.join(blockers)}")


# LLM: _seed_manager keeps fixture setup aligned with runtime SubAgentManager paths.
# 函数用途: 使用真实 manager 创建 seed run，避免测试台手写不完整 task.json。
def _seed_manager(root: Path) -> SubAgentManager:
    return SubAgentManager(
        root / ".my_agent" / "subagents",
        params=SubAgentManagerInitParams(workspace_root=root, workspace_roots=[root]),
    )


# LLM: _write_seed_output mirrors runner output refs without pretending the bad run passed.
# 函数用途: 写入 output.json，让路径/产物合同和 sibling 覆盖逻辑能读取同一目标文件。
def _write_seed_output(task, output: Path) -> None:
    payload = {"artifacts": [{"path": str(output)}], "files_modified": [str(output)], "status": "failed_acceptance"}
    Path(task.output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# LLM: _write_failed_reports seeds parent-acceptance refs consumed by structured repair advice.
# 函数用途: 写 acceptance_review、test_execution 和 followup，小报告点明 disabled 控件与缺失流程。
def _write_failed_reports(task, output: Path) -> None:
    reports = Path(task.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (reports / "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT", "run_id": task.id, "generated_at": now}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports / "test_execution.json").write_text(
        json.dumps(_test_execution_payload(task.id, output), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports / "parent_acceptance_auto_followup.json").write_text(
        json.dumps(_followup_payload(task.id, output, now), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# LLM: _test_execution_payload is generic static-site failure evidence, not shopping-only prose.
# 函数用途: 生成父级测试失败记录，包含 validation_result.path 和可行动问题列表。
def _test_execution_payload(run_id: str, output: Path) -> dict[str, object]:
    return {
        "run_id": run_id,
        "total_tests": 1,
        "failed": 1,
        "records": [
            {
                "test_name": "static site behavior check",
                "validation_method": "static_site_check",
                "executed": True,
                "exit_code": 1,
                "error": "inert controls and missing purchase flow",
                "validation_result": {
                    "ok": False,
                    "path": str(output),
                    "inert_control_hits": ["button[data-action='checkout'] disabled"],
                    "missing_dom_id_hits": ["register", "login", "cart", "checkout", "order-confirmation"],
                    "repair_hints": ["修复同一个 index.html，不要新建无关文件。"],
                },
            }
        ],
    }


# LLM: _followup_payload mirrors the parent follow-up file shape enough for refs-first repair advice.
# 函数用途: 给 repair worker 提供 failed_tests 摘要；不自动执行任何后续动作。
def _followup_payload(run_id: str, output: Path, generated_at: float) -> dict[str, object]:
    return {
        "schema": "parent_acceptance_auto_followup.v1",
        "generated_at": generated_at,
        "run_id": run_id,
        "followup": {
            "status": "needs_manual_rescue",
            "action": "plan_rescue",
            "reason": "父级静态站点检查失败，需要修复产物后重新验收。",
            "test_failed": 1,
            "failed_tests": [{"name": "static site behavior check", "error": str(output)}],
        },
    }


# LLM: _task_snapshots loads small task records only; artifact bodies stay out of the test harness context.
# 函数用途: 从隔离 workspace 读取 task.json，转换成属性对象供状态收口 helpers 判断。
def _task_snapshots(root: Path) -> list[SimpleNamespace]:
    tasks: list[SimpleNamespace] = []
    for path in sorted((root / ".my_agent" / "subagents").glob("subagent-*/task.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            tasks.append(SimpleNamespace(**payload))
    return tasks


# LLM: _is_verified_repair requires terminal status plus repair intent and target overlap.
# 函数用途: 判断某个 run 是否真的是修复 sibling，而不是另一个无关完成任务。
def _is_verified_repair(task: SimpleNamespace, seed_run_id: str) -> bool:
    if str(getattr(task, "id", "") or "") == seed_run_id:
        return False
    if str(getattr(task, "status", "") or "").upper() != "DONE":
        return False
    if str(getattr(task, "verification_status", "") or "").upper() != "VERIFIED":
        return False
    text = " ".join([str(getattr(task, "agent_name", "") or ""), str(getattr(task, "goal", "") or "")]).lower()
    return any(token in text for token in ("修复", "补齐", "fix", "repair", "patch"))


# LLM: _task_by_id performs exact lookup so user-provided ids never become glob patterns.
# 函数用途: 按 run_id 找到 seed task；找不到时返回 None 让调用方给出明确失败。
def _task_by_id(tasks: list[SimpleNamespace], run_id: str) -> SimpleNamespace | None:
    for task in tasks:
        if str(getattr(task, "id", "") or "") == run_id:
            return task
    return None


# LLM: _seed_result keeps target refs available in legacy result parsing.
# 函数用途: 兼容通过 task.result 读取 artifact_path 的旧状态判断路径。
def _seed_result(output: Path) -> str:
    return "[SUBAGENT_RESULT] " + json.dumps({"artifacts": [{"path": str(output)}]}, ensure_ascii=False) + " [/SUBAGENT_RESULT]"


# LLM: _broken_shop_html is intentionally incomplete so parent acceptance advice has real facts to repair.
# 函数用途: 生成一个可打开但明显不合格的购物站文件，包含 disabled 按钮和缺失流程。
def _broken_shop_html() -> str:
    return """<!doctype html><html><body><section id="catalog"><button disabled>购买</button></section><script></script></body></html>"""
