from __future__ import annotations

import json
from types import SimpleNamespace


# LLM: 临时宿主目录先经产品初始化；直接写合成策略仅为准备测试守卫额度，不是正式写入口。
# 函数用途: 在测试家目录写入主代理自动恢复上限并返回该宿主冻结的守卫快照。
def _host_policy(root, limit):
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(root)
    (paths.root / "config" / "deployment_policy.json").write_text(json.dumps({
        "schema_version": "deployment-policy.v1", "revision": 1,
        "runtime_guards": {"main_agent_auto_resume_attempt_limit": limit},
    }), encoding="utf-8")
    return runtime_guard_policy(trusted_host_paths=paths)


def test_auto_resume_limit_reads_runtime_guard_config(tmp_path):
    from agent_py_agent.agent.contracts import main_agent_auto_resume

    policy = _host_policy(tmp_path / "host", 7)

    assert main_agent_auto_resume.auto_resume_limit(SimpleNamespace(), policy=policy) == 7


def test_auto_resume_limit_request_override_wins(tmp_path):
    from agent_py_agent.agent.contracts import main_agent_auto_resume

    policy = _host_policy(tmp_path / "host", 7)

    assert main_agent_auto_resume.auto_resume_limit(SimpleNamespace(max_auto_recovery_attempts=2), policy=policy) == 2


# LLM: 请求零不能放宽宿主正上限；只有宿主显式零保留不限次数语义。
# 函数用途: 验证宿主零上限下大量历史尝试仍可自动恢复。
def test_auto_resume_zero_limit_is_unlimited(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_auto_resume import auto_resume_decision
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    (tmp_path / "auto_recovery_ledger.json").write_text(
        json.dumps({"attempts": 99, "packet_refs": ["old.json"]}),
        encoding="utf-8",
    )
    request = SimpleNamespace(
        execute=True,
        max_auto_recovery_attempts=0,
        recovery_packet_path=None,
        auto_recovery_active=False,
        task_timeout_seconds=600,
    )
    runtime = SimpleNamespace(paths={"root": tmp_path}, request=request, case=SimpleNamespace(case_id="case-1"),
                              runtime_guard_policy=RuntimeGuardPolicy(main_agent_auto_resume_attempt_limit=0))
    bundle = SimpleNamespace(
        runtime=runtime,
        status="FAILED",
        recovery_packet_ref="recovery_packet.json",
        duration_seconds=5,
    )

    decision = auto_resume_decision(bundle)

    assert decision.allowed is True
    assert decision.max_attempts == 0
