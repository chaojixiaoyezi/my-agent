"""第4/6层根修:后台上报轮(无契约)跳过交付收尾门(输出即报告,别被 uncontracted 打回成
[MAIN_AGENT_DELIVERY_REWORK_REQUIRED] 内部信号、被通道层过滤发不到用户飞书)。

⚠️ 关键回归:旧实现按 params.reason 判定,但收尾门这层的 ToolLoopExecuteParams【没有 reason
字段】→ 恒 False → 豁免从未生效。旧测试用带 reason 属性的 SimpleNamespace mock 掩盖了它。
本测试改用真实存在的结构字段 source + delivery_contract,并覆盖"有契约不豁免"的安全边界。"""
from __future__ import annotations

from types import SimpleNamespace

from agent.agent_core.delivery_closeout.closeout import (
    _is_report_only_wake,
    main_agent_delivery_closeout_response,
)


def test_background_report_run_without_contract_is_exempt():
    # 后台调度触发 + 无待验收契约 = 给用户的状态/发现报告轮 → 豁免收尾门
    assert _is_report_only_wake(SimpleNamespace(source="background_main_agent", delivery_contract=None)) is True
    assert _is_report_only_wake(SimpleNamespace(source="background_main_agent")) is True


def test_contracted_delivery_run_not_exempt_even_if_background():
    # 有待验收契约的交付轮(即便后台整合子代理产物)照常全 gate、不豁免——假 done 门不被绕过
    assert _is_report_only_wake(
        SimpleNamespace(source="background_main_agent", delivery_contract={"required_artifacts": ["out.md"]})
    ) is False


def test_foreground_run_not_exempt():
    # 前台请求轮(source=run/gateway)不是上报轮 → 走 uncontracted 假 done 门(保护)
    assert _is_report_only_wake(SimpleNamespace(source="run", delivery_contract=None)) is False
    assert _is_report_only_wake(SimpleNamespace(source="gateway_request", delivery_contract=None)) is False


def test_reason_attribute_no_longer_relied_on():
    # 回归钉子:纵使给了 reason 属性,判定也不再依赖它(真实 params 没有 reason;只认 source)。
    # 有 reason 但 source 非后台 → 不豁免;有 reason 且 source 是后台无契约 → 靠 source 豁免。
    assert _is_report_only_wake(SimpleNamespace(reason="observation_requires_main_agent", source="run")) is False
    assert _is_report_only_wake(
        SimpleNamespace(reason="whatever", source="background_main_agent", delivery_contract=None)
    ) is True


def test_closeout_short_circuits_for_background_report_wake():
    # 上报轮:收尾门第一行就返回 None(不碰契约/workspace),报告走 run 正常投递路由(已修=飞书)
    req = SimpleNamespace(
        params=SimpleNamespace(source="background_main_agent", delivery_contract=None), agent=None, backend=None
    )
    assert main_agent_delivery_closeout_response(req) is None
