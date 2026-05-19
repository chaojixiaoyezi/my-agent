# LLM: Live Lab shop-flow case helpers are split out so the generic cases dispatcher stays small.
# 模块用途: 承载购物站自然语言 E2E、网页业务流检查和静态网页检查；不改 Live Lab 主调度流程。

from __future__ import annotations

import json
import re
import textwrap
import time
from pathlib import Path

from .state_assertions import (
    assert_no_subagent_state_blockers,
    assert_persisted_subagent_state_clean,
)


# LLM: case_natural_shop_subagent is the stronger business-flow canary for delegated web work.
# 函数用途: 用普通用户说法要求主代理派小傻妞完成购物站首页，并检查注册、登录、购物车、结算和下单入口。
def case_natural_shop_subagent(lab) -> None:
    """Run a natural-language shopping-site task against the real gateway path."""

    lab.section("CASE natural_shop_subagent")
    prompt = _natural_shop_prompt()
    lab.record_prompt("natural_shop_subagent", prompt)
    lab.run_command(lab.agent_command("gateway", "start", "--force"), timeout=90)
    try:
        response = lab.run_command(
            lab.agent_command(
                "gateway",
                "ask",
                prompt,
                "--no-wait",
                "--json",
            ),
            timeout=60,
        )
        request_id, response_json_path = _parse_gateway_no_wait(response.stdout)
        lab.log(f"async_request_id={request_id}")
        lab.log(f"async_response={response_json_path}")
        response_payload = _wait_for_async_gateway_response(
            lab,
            response_json_path,
            timeout_seconds=_async_gateway_wait_budget(lab),
        )
        response_path = lab.responses_dir / "natural_shop_subagent.stdout.json"
        response_path.write_text(json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lab.log(f"response_file={response_path}")
        assert_no_subagent_state_blockers(json.dumps(response_payload, ensure_ascii=False))
    finally:
        lab.run_command(
            lab.agent_command("gateway", "stop", "--timeout", "15", "--kill", "--reason", "live lab done"),
            timeout=45,
            allow_fail=True,
        )
    output_path = lab.fixture_root / "lab_outputs" / "shop-demo" / "index.html"
    _assert_shop_html_output(output_path)
    _assert_static_site_check_clean(lab.fixture_root, output_path.parent)
    assert_persisted_subagent_state_clean(lab.fixture_root)
    lab.log(f"natural_shop_output={output_path}")


# LLM: _parse_gateway_no_wait keeps async long-task tests on structured refs from gateway ask.
# 函数用途: 从 gateway ask --no-wait 输出解析 request_id 和响应文件路径，避免长任务占住前台进程。
def _parse_gateway_no_wait(stdout: str) -> tuple[str, Path]:
    request_id = ""
    response_path = Path("")
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("queued request_id="):
            request_id = line.split("=", 1)[1].strip()
        elif line.startswith("response: "):
            response_path = Path(line.split("response: ", 1)[1].strip())
    if not request_id or not response_path:
        raise RuntimeError(f"无法解析 gateway async 输出: {stdout!r}")
    return request_id, response_path


# LLM: _wait_for_async_gateway_response polls the response artifact instead of blocking the foreground CLI.
# 函数用途: 长任务用 gateway 后台执行，Live Lab 定期读取 response 文件，避免宿主杀掉同步等待进程。
def _wait_for_async_gateway_response(lab, response_path: Path, *, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last_note = 0.0
    last_probe = 0.0
    while time.monotonic() < deadline:
        if response_path.exists():
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(1.0)
                continue
            if payload:
                return payload
        now = time.monotonic()
        if now - last_note >= 30:
            lab.log(f"等待 async gateway response: {response_path}")
            last_note = now
        if now - last_probe >= 60:
            _probe_foreground_main_agent(lab)
            last_probe = now
        time.sleep(2.0)
    raise RuntimeError(f"gateway async response 未在 {timeout_seconds:g}s 内生成: {response_path}")


# LLM: _async_gateway_wait_budget allows no-wait requests to survive multiple backend/tool rounds.
# 函数用途: 异步长任务不占前台 subprocess；等待预算按 max_cycles 放大，覆盖分块写入和恢复轮次。
def _async_gateway_wait_budget(lab) -> float:
    cycles = max(1, int(getattr(lab.args, "max_cycles", 1) or 1))
    return max(float(lab.args.timeout) + 180.0, float(lab.args.timeout) * cycles + 180.0)


# LLM: _probe_foreground_main_agent verifies the chat lane stays responsive while background work runs.
# 函数用途: 长任务异步执行期间，定时让主代理简短汇报状态；失败写日志，暴露 worker 池/会话解耦问题。
def _probe_foreground_main_agent(lab) -> None:
    prompt = "后台购物站任务现在什么状态？请只用一句话回复，不要创建新任务，不要改文件。"
    result = lab.run_command(
        lab.agent_command("gateway", "ask", prompt, "--timeout", "45", "--context-scope", "control_plane", "--json"),
        timeout=60,
        allow_fail=True,
    )
    if result.returncode == 0:
        lab.log("foreground_probe=pass")
        return
    lab.log(f"foreground_probe=fail exit_code={result.returncode}")


# LLM: _natural_shop_prompt keeps the stronger E2E phrased like a non-technical user request.
# 函数用途: 生成购物站真实 E2E 的自然语言提示词；要求小傻妞团队做事，但不写内部调度术语。
def _natural_shop_prompt() -> str:
    return textwrap.dedent(
        """
        我想做一个真实可用的小型购物网站演示。请你安排小傻妞来完成，不要你自己直接写页面正文。

        任务是：做一个高端生活方式购物网站的静态前端，能演示从注册、登录、浏览商品、加入购物车、结算到下单成功的完整流程。
        页面可以做成单文件 html，CSS 和 JavaScript 可以内嵌；不要依赖外部图片、外部字体、外部脚本或外部 CSS。

        请把最终页面保存到 lab_outputs/shop-demo/index.html。
        页面里必须有真实的商品卡片、购物车区域、注册表单、登录表单、结算表单和下单成功区域。
        这些业务区域请使用稳定 id：register、login、catalog、cart、checkout、order-confirmation。
        所有按钮都要能对应到真实动作，不要空链接、不要 disabled 按钮、不要只写一个摆设按钮。
        完成后请安排检查，确认文件存在、能作为网页打开、没有失效控件、没有外部资源依赖。
        最后告诉我保存路径和检查结果。
        """
    ).strip()


# LLM: _assert_shop_html_output validates user-visible shopping flow anchors before Live Lab can pass.
# 函数用途: 检查购物站真实 HTML 产物必须包含注册、登录、商品、购物车、结算和下单成功这些业务入口。
def _assert_shop_html_output(output_path) -> None:
    if not output_path.exists():
        raise RuntimeError(f"购物站 HTML 产物不存在: {output_path}")
    content = output_path.read_text(encoding="utf-8", errors="replace")
    lower = content.lower()
    required_terms = ["<html", "</html>", "<body", "</body>", "<script", "</script>"]
    missing_terms = [term for term in required_terms if term not in lower]
    if missing_terms:
        raise RuntimeError(f"购物站 HTML 产物缺少基本结构: {missing_terms}")
    missing_sections = _missing_shop_sections(lower)
    if missing_sections:
        raise RuntimeError(f"购物站 HTML 产物缺少业务区域: {missing_sections}")
    missing_actions = _missing_shop_actions(lower)
    if missing_actions:
        raise RuntimeError(f"购物站 HTML 产物缺少按钮动作: {missing_actions}")
    if "href=\"#\"" in lower or _has_disabled_control(lower):
        raise RuntimeError("购物站 HTML 产物包含空链接或 disabled 控件。")
    external_assets = _external_asset_refs(lower)
    if external_assets:
        raise RuntimeError(f"购物站 HTML 产物依赖外部资源: {external_assets[:5]}")


# LLM: _missing_shop_sections keeps the shop canary tied to concrete product flow DOM anchors.
# 函数用途: 返回缺失的购物流程区域 id；用轻量字符串检查避免 Live Lab 额外引入 HTML 解析依赖。
def _missing_shop_sections(lower_content: str) -> list[str]:
    required = {
        "register": ('id="register', "id='register", "registermodal", "regname", "注册"),
        "login": ('id="login', "id='login", "loginmodal", "登录"),
        "catalog": ('id="catalog', "id='catalog", 'id="products', "id='products", "productsgrid", "商品", "精品"),
        "cart": ('id="cart', "id='cart", "cartsidebar", "cartitems", "购物车"),
        "checkout": ('id="checkout', "id='checkout", "checkoutmodal", "checkoutitems", "结算"),
        "order-confirmation": ("order-confirmation", "successpage", "ordernumber", "下单成功", "订单号"),
    }
    return [label for label, markers in required.items() if not _contains_any(lower_content, markers)]


# LLM: _missing_shop_actions checks that visible buttons expose stable action names.
# 函数用途: 返回缺失的购物流程动作标记，方便子代理修复具体哪个按钮没有接上。
def _missing_shop_actions(lower_content: str) -> list[str]:
    required = {
        "register": ("data-action=\"register", "data-action='register", "handleregister", "registeruser", "注册"),
        "login": ("data-action=\"login", "data-action='login", "handlelogin", "loginuser", "登录"),
        "add-to-cart": ("data-action=\"add-to-cart", "data-action='add-to-cart", "addtocart", "加入购物车"),
        "checkout": (
            "data-action=\"checkout",
            "data-action='checkout",
            "proceedtocheckout",
            "handlecheckout",
            "getelementbyid('checkout-btn')",
            'getelementbyid("checkout-btn")',
            "去结算",
        ),
        "place-order": ("data-action=\"place-order", "data-action='place-order", "placeorder", "handlecheckout", "下单成功"),
    }
    return [label for label, markers in required.items() if not _contains_any(lower_content, markers)]


# LLM: _contains_any keeps semantic shop gates flexible without weakening them to pure prose.
# 函数用途: 检查 HTML 是否出现任一结构或动作候选词，兼容真实模型常见命名差异。
def _contains_any(lower_content: str, markers: tuple[str, ...]) -> bool:
    return any(marker in lower_content for marker in markers)


# LLM: _has_disabled_control rejects actual disabled HTML controls without flagging JS state toggles.
# 函数用途: 只拦截 `<button disabled>` 这类用户一打开就失效的控件，不误伤 CSS `:disabled` 或 JS `.disabled = true/false`。
def _has_disabled_control(lower_content: str) -> bool:
    return bool(re.search(r"<(button|input|select|textarea)\b[^>]*\sdisabled(\s|>|=)", lower_content))


# LLM: _assert_static_site_check_clean reuses the product static validator for stronger HTML behavior checks.
# 函数用途: 用已有 static_site_check 检查购物站坏链接、占位符、失效控件和表单绑定，不重复写验收逻辑。
def _assert_static_site_check_clean(fixture_root, site_root) -> None:
    from agent_py_agent.agent.subagents.static_site_validator import run_static_site_check

    record = run_static_site_check(
        {
            "name": "live-lab-shop-static-site",
            "site_root": str(site_root),
            "required_files": ["index.html"],
            "require_complete_html": True,
            "require_script": True,
            "required_dom_ids": ["register", "login", "catalog", "cart", "checkout", "order-confirmation"],
        },
        fixture_root,
    )
    if not record.passed:
        raise RuntimeError(f"购物站 static_site_check 未通过: {record.error} {record.validation_result}")


# LLM: _external_asset_refs keeps canaries offline-deterministic without banning normal outbound links.
# 函数用途: 找出会让单文件页面离线失效的外部图片、字体、脚本或 CSS 背景资源。
def _external_asset_refs(lower_content: str) -> list[str]:
    refs: list[str] = []
    for marker in ('src="http://', "src='http://", 'src="https://', "src='https://"):
        if marker in lower_content:
            refs.append(marker.rstrip("'\""))
    for marker in ('<link href="http://', "<link href='http://", '<link href="https://', "<link href='https://"):
        if marker in lower_content:
            refs.append(marker.rstrip("'\""))
    for marker in ("url(http://", "url(https://", "url('http://", "url('https://", 'url("http://', 'url("https://'):
        if marker in lower_content:
            refs.append(marker.rstrip("'\""))
    return refs
