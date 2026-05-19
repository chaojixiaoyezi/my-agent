from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.live_lab.cases import (
    _assert_natural_html_output,
    _natural_html_prompt,
)
from scripts.live_lab.constants import REAL_CASES, SUITES
from scripts.live_lab.file_repair_wave_case import (
    _assert_order_csv_output,
    _natural_file_repair_wave_prompt,
    assert_file_repair_wave_created,
    seed_failed_file_child,
)
from scripts.live_lab.markdown_repair_wave_case import (
    _assert_markdown_report_output,
    _complete_markdown_report,
    _natural_markdown_repair_wave_prompt,
    assert_markdown_repair_wave_created,
    seed_failed_markdown_child,
)
from scripts.live_lab.multi_complex_case import (
    _assert_contract_coverage,
    _bundle_contracts,
    _bundle_prompt,
    _complex_contracts,
)
from scripts.live_lab.session import LabSessionManager, _live_lab_max_subagents
from scripts.live_lab.shop_case import (
    _assert_shop_html_output,
    _assert_static_site_check_clean,
    _async_gateway_wait_budget,
    _external_asset_refs,
    _has_disabled_control,
    _missing_shop_actions,
    _missing_shop_sections,
    _natural_shop_prompt,
    _parse_gateway_no_wait,
    _probe_foreground_main_agent,
    _wait_for_async_gateway_response,
)
from scripts.live_lab.shop_repair_wave_case import (
    _natural_shop_repair_wave_prompt,
    assert_shop_repair_wave_created,
    seed_failed_shop_child,
)
from scripts.live_lab.state_assertions import (
    assert_no_subagent_state_blockers as _assert_no_subagent_state_blockers,
)
from scripts.live_lab.state_assertions import (
    assert_persisted_subagent_state_clean as _assert_persisted_subagent_state_clean,
)


# LLM: The natural Live Lab case should exercise ordinary user wording, not internal orchestration terms.
# 函数用途: 确认家具页面 E2E 的提示词接近用户真实说法，并要求主代理派小傻妞而不是自己写。
def test_natural_html_prompt_uses_user_language():
    prompt = _natural_html_prompt()

    assert "小傻妞" in prompt
    assert "高端现代家具品牌" in prompt
    assert "lab_outputs/furniture-home/index.html" in prompt
    assert "不要依赖外部图片" in prompt
    assert "dispatch" not in prompt.lower()
    assert "runner" not in prompt.lower()
    assert "contract" not in prompt.lower()


# LLM: The natural suite must stay opt-in but real-LLM gated.
# 函数用途: 确认新增自然语言 E2E 入口不会偷偷跑真实模型，同时可被用户显式运行。
def test_natural_html_case_is_registered_as_real_opt_in_suite():
    assert SUITES["natural"] == ["health", "natural_html_subagent"]
    assert "natural_html_subagent" in REAL_CASES


# LLM: The shop canary should also stay user-language and real-LLM opt-in.
# 函数用途: 确认购物站 E2E 不依赖内部术语，并且只有显式 suite 才会调用真实模型。
def test_natural_shop_case_is_registered_as_real_opt_in_suite():
    prompt = _natural_shop_prompt()

    assert SUITES["shop"] == ["health", "natural_shop_subagent"]
    assert "natural_shop_subagent" in REAL_CASES
    assert "小傻妞" in prompt
    assert "注册、登录、浏览商品、加入购物车、结算到下单成功" in prompt
    assert "lab_outputs/shop-demo/index.html" in prompt
    assert "dispatch" not in prompt.lower()
    assert "runner" not in prompt.lower()
    assert "contract" not in prompt.lower()


# LLM: Multi-complex canary must stay real-LLM gated and verify structured task-card contracts.
# 函数用途: 确认多复杂任务测试覆盖多个主请求和单主请求多 worker 的结构化合同，不靠任务描述过关。
def test_multi_complex_case_is_registered_as_real_opt_in_suite():
    contracts = _complex_contracts()
    bundle_contracts = _bundle_contracts(contracts)

    assert SUITES["multi-complex"] == ["health", "multi_complex_subagents"]
    assert "multi_complex_subagents" in REAL_CASES
    assert [contract.case_id for contract in contracts] == ["deepseek_papers", "github_stars", "shop", "articles"]
    assert [contract.case_id for contract in bundle_contracts] == [
        "bundle_deepseek_papers",
        "bundle_github_stars",
        "bundle_shop",
        "bundle_articles",
    ]
    assert contracts[2].required_dom_ids == ["register", "login", "catalog", "cart", "checkout", "order-confirmation"]
    assert contracts[2].require_script is True
    assert _output_files(contracts).isdisjoint(_output_files(bundle_contracts))
    assert all(path.startswith("lab_outputs/bundle/") for path in _output_files(bundle_contracts))
    assert "items/tasks 批量参数一次性创建 4 个 item" in _bundle_prompt(bundle_contracts)


def _output_files(contracts):
    return {path for contract in contracts for path in contract.output_files}


# LLM: Multi-complex contract checks must read task attributes, not prose in goal.
# 函数用途: 防止测试误把 goal 正文里的路径/id 当作系统已经具备机器合同。
def test_multi_complex_contract_coverage_requires_attributes():
    contracts = _complex_contracts()
    cards = [
        {
            "goal": contract.prompt,
            "attributes": {
                "output_files": contract.output_files,
                "required_files": contract.required_files,
                "required_dom_ids": contract.required_dom_ids,
                **({"require_script": contract.require_script} if contract.require_script is not None else {}),
            },
        }
        for contract in contracts
    ]

    _assert_contract_coverage(cards, contracts)
    broken_cards = [dict(card) for card in cards]
    broken_cards[2] = {"goal": contracts[2].prompt, "attributes": {"output_files": contracts[2].output_files}}
    with pytest.raises(RuntimeError, match="required_dom_ids"):
        _assert_contract_coverage(broken_cards, contracts)


# LLM: Shop Live Lab must queue long work asynchronously instead of blocking the foreground ask process.
# 函数用途: 确认 gateway ask --no-wait 的稳定输出能解析成 request_id 和 response 路径。
def test_parse_gateway_no_wait_output():
    request_id, response_path = _parse_gateway_no_wait(
        "queued request_id=gw-123\n"
        "request: /tmp/run/.my_agent/gateway/requests/pending/gw-123.json\n"
        "response: /tmp/run/.my_agent/gateway/responses/gw-123.json\n"
    )

    assert request_id == "gw-123"
    assert str(response_path).endswith("/responses/gw-123.json")


# LLM: Async wait budget should scale with max_cycles because long tasks use multiple tool/model rounds.
# 函数用途: 防止真实购物站测试只等一轮 request_timeout，误杀后台分块写入任务。
def test_async_gateway_wait_budget_scales_with_max_cycles():
    lab = SimpleNamespace(args=SimpleNamespace(timeout=240, max_cycles=4))

    assert _async_gateway_wait_budget(lab) == 1140.0


# LLM: Live Lab real tasks need a runner wrapper timeout so stuck child workers become recoverable facts.
# 函数用途: 确认真实测试隔离配置会写入 runner_timeout_seconds，不让子代理无限 RUNNING 拖死 E2E。
def test_live_lab_config_sets_runner_timeout(tmp_path):
    source_config = tmp_path / "agent_config.yaml"
    source_config.write_text("model_backend: echo\n", encoding="utf-8")
    source_capability = tmp_path / "capability.yaml"
    source_capability.write_text("enable_capability_routing: true\n", encoding="utf-8")
    args = SimpleNamespace(
        config=source_config,
        capability_config=source_capability,
        runs_dir=tmp_path / "runs",
        run_id="run-timeout",
        real_llm=False,
        count=1,
        max_runners=1,
        max_cycles=2,
        timeout=45,
    )
    lab = LabSessionManager(args)
    lab.setup()

    text = lab.config_path.read_text(encoding="utf-8")
    assert "gateway_request_workers: 3" in text
    assert "max_subagents: 8" in text
    assert "gateway_foreground_reserved_workers: 1" in text
    assert "daemon_apply: true" in text
    assert "daemon_execute_runners: true" in text
    assert "daemon_max_runners: 1" in text
    assert "runner_timeout_seconds: 60" in text
    assert "request_timeout: 45" in text


# LLM: Multi-complex Live Lab needs enough headroom for separate requests plus a bundle batch.
# 函数用途: 锁定真实复杂测试的任务卡上限，避免 count=1 把批量 items 截成单任务。
def test_live_lab_max_subagents_scales_for_bundle_cases():
    args = SimpleNamespace(count=1, max_runners=2, max_cycles=4)

    assert _live_lab_max_subagents(args) == 32


# LLM: Async shop waiting should actively probe the foreground lane while background work runs.
# 函数用途: 验证长任务等待循环会定时检查前台 main agent 是否还能响应。
def test_async_gateway_wait_probes_foreground_lane(tmp_path, monkeypatch):
    response_path = tmp_path / "response.json"
    probes: list[str] = []

    class Lab:
        def log(self, message):
            pass

    def fake_probe(lab):
        probes.append("called")

    def fake_sleep(seconds):
        response_path.write_text('{"ok": true}', encoding="utf-8")

    monkeypatch.setattr("scripts.live_lab.shop_case._probe_foreground_main_agent", fake_probe)
    monkeypatch.setattr("scripts.live_lab.shop_case.time.sleep", fake_sleep)

    payload = _wait_for_async_gateway_response(Lab(), response_path, timeout_seconds=3)

    assert payload == {"ok": True}
    assert probes == ["called"]


# LLM: Foreground probe failures should be visible in lab logs instead of silently passing.
# 函数用途: 验证前台对话被后台长任务堵住时，Live Lab 会留下明确失败日志。
def test_probe_foreground_main_agent_logs_failure():
    logs: list[str] = []

    class Lab:
        def agent_command(self, *parts):
            return ("my-agent", *parts)

        def run_command(self, *args, **kwargs):
            return SimpleNamespace(returncode=7)

        def log(self, message):
            logs.append(message)

    _probe_foreground_main_agent(Lab())

    assert logs == ["foreground_probe=fail exit_code=7"]


# LLM: The repair-wave canary should start from a failed child and still use ordinary user wording.
# 函数用途: 确认购物站失败修复 E2E 是独立 opt-in suite，提示词不靠内部调度术语通过。
def test_natural_shop_repair_wave_case_is_registered_as_real_opt_in_suite():
    prompt = _natural_shop_repair_wave_prompt()

    assert SUITES["shop-repair"] == ["health", "natural_shop_repair_wave"]
    assert "natural_shop_repair_wave" in REAL_CASES
    assert "小傻妞" in prompt
    assert "没通过检查" in prompt
    assert "lab_outputs/shop-demo/index.html" in prompt
    assert "dispatch" not in prompt.lower()
    assert "runner" not in prompt.lower()
    assert "contract" not in prompt.lower()


# LLM: The file repair canary keeps repair-wave coverage outside web/static-site tasks.
# 函数用途: 确认非网页文件修复 E2E 也走普通用户话术和真实模型 opt-in，不依赖内部调度词。
def test_natural_file_repair_wave_case_is_registered_as_real_opt_in_suite():
    prompt = _natural_file_repair_wave_prompt()

    assert SUITES["file-repair"] == ["health", "natural_file_repair_wave"]
    assert "natural_file_repair_wave" in REAL_CASES
    assert "小傻妞" in prompt
    assert "订单报表" in prompt
    assert "lab_outputs/order-report/orders.csv" in prompt
    assert "dispatch" not in prompt.lower()
    assert "runner" not in prompt.lower()
    assert "contract" not in prompt.lower()


# LLM: Markdown repair expands non-web E2E beyond CSV and static-site outputs.
# 函数用途: 确认 Markdown 文档修复 case 也是真实模型 opt-in，并保持普通用户话术。
def test_natural_markdown_repair_wave_case_is_registered_as_real_opt_in_suite():
    prompt = _natural_markdown_repair_wave_prompt()

    assert SUITES["markdown-repair"] == ["health", "natural_markdown_repair_wave"]
    assert "natural_markdown_repair_wave" in REAL_CASES
    assert "小傻妞" in prompt
    assert "Markdown" in prompt
    assert "lab_outputs/report/weekly.md" in prompt
    assert "dispatch" not in prompt.lower()
    assert "runner" not in prompt.lower()
    assert "contract" not in prompt.lower()


# LLM: The repair-wave seed must look like a real failed child run, not a prose-only fixture.
# 函数用途: 确认测试台能预置一个待修复购物站 run，并保留机器可读产物、验收失败和写入边界。
def test_seed_failed_shop_child_creates_rejected_run_with_artifact_refs(tmp_path):
    seed = seed_failed_shop_child(tmp_path)

    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    task_json = tmp_path / ".my_agent" / "subagents" / seed.run_id / "task.json"
    acceptance = tmp_path / ".my_agent" / "subagents" / seed.run_id / "reports" / "acceptance_review.json"
    assert seed.run_id.startswith("subagent-")
    assert output.exists()
    assert "disabled" in output.read_text(encoding="utf-8").lower()
    assert task_json.exists()
    assert acceptance.exists()
    text = task_json.read_text(encoding="utf-8")
    assert '"status": "AWAITING_ACCEPTANCE"' in text
    assert '"verification_status": "UNVERIFIED"' in text
    assert str(output) in text


# LLM: The file repair seed must use ordinary file/content checks instead of static-site validation.
# 函数用途: 确认测试台能预置一个 CSV 文件验收失败 run，并保留 content_check 失败证据和产物写入边界。
def test_seed_failed_file_child_creates_rejected_run_with_content_refs(tmp_path):
    seed = seed_failed_file_child(tmp_path)

    output = tmp_path / "lab_outputs" / "order-report" / "orders.csv"
    task_json = tmp_path / ".my_agent" / "subagents" / seed.run_id / "task.json"
    report = tmp_path / ".my_agent" / "subagents" / seed.run_id / "reports" / "test_execution.json"
    assert output.exists()
    assert "order_id,customer,total" not in output.read_text(encoding="utf-8")
    assert task_json.exists()
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert '"validation_method": "content_check"' in text
    assert str(output) in task_json.read_text(encoding="utf-8")


# LLM: Markdown repair seed should look like a normal failed document child with content checks.
# 函数用途: 确认 Markdown 文档失败 seed 有真实 task、坏产物和 content_check 父级失败证据。
def test_seed_failed_markdown_child_creates_rejected_run_with_content_refs(tmp_path):
    seed = seed_failed_markdown_child(tmp_path)

    output = tmp_path / "lab_outputs" / "report" / "weekly.md"
    task_json = tmp_path / ".my_agent" / "subagents" / seed.run_id / "task.json"
    report = tmp_path / ".my_agent" / "subagents" / seed.run_id / "reports" / "test_execution.json"
    assert output.exists()
    assert "# 本周进展" not in output.read_text(encoding="utf-8")
    assert task_json.exists()
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert '"validation_method": "content_check"' in text
    assert str(output) in task_json.read_text(encoding="utf-8")


# LLM: The repair-wave assertion should require a verified repair sibling that covers the same product file.
# 函数用途: 确认失败 seed 不能单独通过；只有同目标修复 run DONE/VERIFIED 后才算修复闭环成立。
def test_assert_shop_repair_wave_created_requires_verified_repair_sibling(tmp_path):
    seed = seed_failed_shop_child(tmp_path)

    with pytest.raises(RuntimeError, match="没有发现已验证的修复小傻妞"):
        assert_shop_repair_wave_created(tmp_path, seed.run_id)

    repair_dir = tmp_path / ".my_agent" / "subagents" / "subagent-repair"
    repair_dir.mkdir(parents=True)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.write_text(_complete_shop_html(), encoding="utf-8")
    (repair_dir / "output.json").write_text(
        '{"artifacts":[{"path":"' + str(output) + '"}]}',
        encoding="utf-8",
    )
    (repair_dir / "task.json").write_text(
        "\n".join(
            [
                "{",
                '  "id": "subagent-repair",',
                '  "status": "DONE",',
                '  "verification_status": "VERIFIED",',
                '  "role": "worker",',
                '  "agent_name": "小傻妞-验收修复",',
                '  "goal": "修复 lab_outputs/shop-demo/index.html",',
                '  "output_json": "' + str(repair_dir / "output.json") + '"',
                "}",
            ]
        ),
        encoding="utf-8",
    )

    assert_shop_repair_wave_created(tmp_path, seed.run_id)


# LLM: File repair completion should require a verified sibling and a repaired target file.
# 函数用途: 复现非网页 repair-wave 的状态门；失败 seed 不能单独通过，修复 sibling 必须覆盖同一个 CSV。
def test_assert_file_repair_wave_created_requires_verified_repair_sibling(tmp_path):
    seed = seed_failed_file_child(tmp_path)

    with pytest.raises(RuntimeError, match="没有发现已验证的文件修复小傻妞"):
        assert_file_repair_wave_created(tmp_path, seed.run_id)

    repair_dir = tmp_path / ".my_agent" / "subagents" / "subagent-file-repair"
    repair_dir.mkdir(parents=True)
    output = tmp_path / "lab_outputs" / "order-report" / "orders.csv"
    output.write_text(_complete_order_csv(), encoding="utf-8")
    (repair_dir / "output.json").write_text(
        '{"artifacts":[{"path":"' + str(output) + '"}]}',
        encoding="utf-8",
    )
    (repair_dir / "task.json").write_text(
        "\n".join(
            [
                "{",
                '  "id": "subagent-file-repair",',
                '  "status": "DONE",',
                '  "verification_status": "VERIFIED",',
                '  "role": "worker",',
                '  "agent_name": "小傻妞-文件修复",',
                '  "goal": "修复 lab_outputs/order-report/orders.csv",',
                '  "output_json": "' + str(repair_dir / "output.json") + '"',
                "}",
            ]
        ),
        encoding="utf-8",
    )

    assert_file_repair_wave_created(tmp_path, seed.run_id)


# LLM: Markdown repair completion should require a verified repair sibling and exact document content.
# 函数用途: 复现 Markdown repair-wave 状态门；只有同目标 DONE/VERIFIED 修复 run 才能通过。
def test_assert_markdown_repair_wave_created_requires_verified_repair_sibling(tmp_path):
    seed = seed_failed_markdown_child(tmp_path)

    with pytest.raises(RuntimeError, match="没有发现已验证的 Markdown 修复小傻妞"):
        assert_markdown_repair_wave_created(tmp_path, seed.run_id)

    repair_dir = tmp_path / ".my_agent" / "subagents" / "subagent-markdown-repair"
    repair_dir.mkdir(parents=True)
    output = tmp_path / "lab_outputs" / "report" / "weekly.md"
    output.write_text(_complete_markdown_report(), encoding="utf-8")
    (repair_dir / "output.json").write_text(
        '{"artifacts":[{"path":"' + str(output) + '"}]}',
        encoding="utf-8",
    )
    (repair_dir / "task.json").write_text(
        "\n".join([
            "{",
            '  "id": "subagent-markdown-repair",',
            '  "status": "DONE",',
            '  "verification_status": "VERIFIED",',
            '  "role": "worker",',
            '  "agent_name": "小傻妞-Markdown修复",',
            '  "goal": "修复 lab_outputs/report/weekly.md",',
            '  "output_json": "' + str(repair_dir / "output.json") + '"',
            "}",
        ]),
        encoding="utf-8",
    )

    assert_markdown_repair_wave_created(tmp_path, seed.run_id)


# LLM: Natural Live Lab should not fail long page tasks because of an artificial test harness tool cap.
# 函数用途: 确认隔离配置默认不限制工具轮数，避免真实页面生成被测试台自己截断。
def test_live_lab_config_keeps_tool_rounds_unlimited(tmp_path):
    source = tmp_path / "agent_config.yaml"
    source.write_text("model_backend: echo\n", encoding="utf-8")
    args = SimpleNamespace(
        config=str(source),
        capability_config=str(tmp_path / "missing_capability_config.yaml"),
        runs_dir=str(tmp_path / "runs"),
        run_id="natural-config",
        real_llm=False,
        count=2,
        timeout=180,
    )

    session = LabSessionManager(args)
    session.setup()

    text = session.config_path.read_text(encoding="utf-8")
    assert "max_tool_rounds: 0" in text
    assert "gateway_port: 0" in text
    capability_text = session.capability_config_path.read_text(encoding="utf-8")
    assert "subagent_heartbeat_timeout: 60" in capability_text
    assert "subagent_run_timeout: 540" in capability_text


# LLM: Live Lab must pass its isolated capability config into commands that accept it.
# 函数用途: 确认真实测试的 gateway/scenario 调度使用隔离超时策略，而不是仓库默认无限超时。
def test_live_lab_agent_command_injects_capability_config_for_supported_commands(tmp_path):
    from scripts.live_lab.reporter import LabReporter
    from scripts.live_lab.runner import LabRunner

    source = tmp_path / "agent_config.yaml"
    source.write_text("model_backend: echo\n", encoding="utf-8")
    args = SimpleNamespace(
        config=str(source),
        capability_config=str(tmp_path / "missing_capability_config.yaml"),
        runs_dir=str(tmp_path / "runs"),
        run_id="natural-config",
        real_llm=False,
        count=1,
        timeout=120,
    )
    session = LabSessionManager(args)
    session.setup()
    runner = LabRunner(session, LabReporter(session, args), args)

    gateway_start = runner.agent_command("gateway", "start", "--force")
    scenario_test = runner.agent_command("scenario-test", "--case", "happy")
    gateway_ask = runner.agent_command("gateway", "ask", "hi")

    assert "--capability-config" in gateway_start
    assert str(session.capability_config_path) in gateway_start
    assert "--capability-config" in scenario_test
    assert str(session.capability_config_path) in scenario_test
    assert "--capability-config" not in gateway_ask


# LLM: Natural E2E validation checks concrete artifact facts instead of trusting the final prose.
# 函数用途: 确认 HTML 验收读取真实文件，并能发现空链接这类用户可见问题。
def test_assert_natural_html_output_rejects_empty_links(tmp_path):
    output = tmp_path / "lab_outputs" / "furniture-home" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(
        '<html><body><a href="#">Furniture</a></body></html>',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="空链接"):
        _assert_natural_html_output(output)


# LLM: Root-relative links are broken in a static single-file deliverable and should fail fast.
# 函数用途: 确认自然语言 HTML 验收能拦住 /shop 这类需要真实路由支持的坏链接。
def test_assert_natural_html_output_rejects_root_route_links(tmp_path):
    output = tmp_path / "lab_outputs" / "furniture-home" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(
        '<html><body><main>高端家具</main><a href="/shop">Shop</a></body></html>',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="空链接"):
        _assert_natural_html_output(output)


# LLM: The shop gate should fail when a generated page lacks key purchase-flow sections.
# 函数用途: 防止购物站只有漂亮首屏但没有注册、登录、购物车、结算和下单成功这些真实入口。
def test_assert_shop_html_output_rejects_missing_flow_sections(tmp_path):
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><script></script><section id='catalog'>商品</section></body></html>", encoding="utf-8")

    with pytest.raises(RuntimeError, match="缺少业务区域"):
        _assert_shop_html_output(output)


# LLM: The shop gate should accept a compact but complete offline purchase-flow demo.
# 函数用途: 确认单文件购物站只要包含必要区域、按钮动作和完整 HTML，就能通过轻量 E2E 产物门。
def test_assert_shop_html_output_accepts_complete_offline_shop(tmp_path):
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(_complete_shop_html(), encoding="utf-8")

    _assert_shop_html_output(output)


# LLM: Shop Live Lab should reuse static_site_check for behavior-level HTML failures.
# 函数用途: 确认购物站 case 的静态站点验收能接受完整示例，避免真实 E2E 跑到最后才发现接口拼错。
def test_assert_static_site_check_clean_accepts_complete_offline_shop(tmp_path):
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(_complete_shop_html(), encoding="utf-8")

    _assert_static_site_check_clean(tmp_path, output.parent)


# LLM: The file repair artifact gate checks concrete CSV content, not final prose.
# 函数用途: 确认订单报表缺关键表头/行时会失败，修复完整后才能通过。
def test_assert_order_csv_output_checks_required_content(tmp_path):
    output = tmp_path / "lab_outputs" / "order-report" / "orders.csv"
    output.parent.mkdir(parents=True)
    output.write_text("order_id,total\nA-1001,299.00\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="订单报表缺少内容"):
        _assert_order_csv_output(output)

    output.write_text(_complete_order_csv(), encoding="utf-8")
    _assert_order_csv_output(output)


# LLM: Markdown repair artifact gate checks concrete document content.
# 函数用途: 确认周报 Markdown 缺标题/列表/风险段时失败，完整内容才通过。
def test_assert_markdown_report_output_checks_required_content(tmp_path):
    output = tmp_path / "lab_outputs" / "report" / "weekly.md"
    output.parent.mkdir(parents=True)
    output.write_text("# 草稿\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Markdown 报告缺少内容"):
        _assert_markdown_report_output(output)

    output.write_text(_complete_markdown_report(), encoding="utf-8")
    _assert_markdown_report_output(output)


# LLM: Shop helper checks should report exact missing ids/actions for repair workers.
# 函数用途: 确认购物站验收失败时能指出缺哪个区域或动作，方便后续小傻妞修复。
def test_shop_missing_helpers_report_exact_contract_parts():
    lower = "<section id='catalog'></section><button data-action='add-to-cart'>买</button>"

    assert _missing_shop_sections(lower) == ["register", "login", "cart", "checkout", "order-confirmation"]
    assert _missing_shop_actions(lower) == ["register", "login", "checkout", "place-order"]


# LLM: The shop canary should accept real DOM event bindings, not only data-action attributes.
# 函数用途: 覆盖真实购物站产物中 checkout-btn 通过 addEventListener 绑定点击事件的合法写法。
def test_shop_missing_actions_accepts_checkout_button_event_binding():
    lower = """
    <button data-action="register">注册</button>
    <button data-action="login">登录</button>
    <button data-action="add-to-cart">加入购物车</button>
    <button id="checkout-btn" type="button">结算</button>
    <button data-action="place-order">下单</button>
    <script>
    document.getElementById('checkout-btn').addEventListener('click', () => {});
    </script>
    """.lower()

    assert _missing_shop_actions(lower) == []


# LLM: The natural canary must fail when the gateway response says the subagent chain is still blocked.
# 函数用途: 防止真实产物存在但主代理明确报告 blocking_run_ids 时，Live Lab 误报 PASS。
def test_assert_no_subagent_state_blockers_rejects_gateway_notice():
    stdout = (
        '{"ok": true, "response": "---\\n\\n## Subagent State Notice\\n\\n'
        '子代理链路尚未完整通过\\n- done_verified: 0\\n'
        '- blocking_run_ids: subagent-123"}'
    )

    with pytest.raises(RuntimeError, match="子代理链路仍阻塞"):
        _assert_no_subagent_state_blockers(stdout)


# LLM: Ordinary successful prose should pass the natural canary control-plane gate.
# 函数用途: 确认没有 blocker 信号的 gateway JSON 不会被误拦截。
def test_assert_no_subagent_state_blockers_accepts_clean_response():
    stdout = '{"ok": true, "response": "保存路径：lab_outputs/furniture-home/index.html\\n检查通过。"}'

    _assert_no_subagent_state_blockers(stdout)


# LLM: Live Lab should compare final prose with persisted task.json state before reporting pass.
# 函数用途: 复现自然语言 E2E 假绿：一个旧小傻妞仍是 PLANNING，但主代理文字说 done_verified。
def test_assert_persisted_subagent_state_clean_rejects_planning_run(tmp_path):
    subagents = tmp_path / ".my_agent" / "subagents"
    done = subagents / "subagent-done"
    planned = subagents / "subagent-planned"
    done.mkdir(parents=True)
    planned.mkdir()
    (done / "task.json").write_text(
        '{"id":"subagent-done","status":"DONE","verification_status":"VERIFIED","goal":"写 index.html"}',
        encoding="utf-8",
    )
    (planned / "task.json").write_text(
        '{"id":"subagent-planned","status":"PLANNING","verification_status":"UNVERIFIED","goal":"写 index.html"}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="持久化子代理状态仍未完成"):
        _assert_persisted_subagent_state_clean(tmp_path)


# LLM: A single verified subagent should satisfy the natural Live Lab persisted-state gate.
# 函数用途: 确认正常 DONE/VERIFIED 的小傻妞不会被状态门误拦截。
def test_assert_persisted_subagent_state_clean_accepts_verified_run(tmp_path):
    subagents = tmp_path / ".my_agent" / "subagents" / "subagent-done"
    subagents.mkdir(parents=True)
    (subagents / "task.json").write_text(
        '{"id":"subagent-done","status":"DONE","verification_status":"VERIFIED"}',
        encoding="utf-8",
    )

    _assert_persisted_subagent_state_clean(tmp_path)


# LLM: The natural canary should catch remote assets before visual E2E claims success.
# 函数用途: 确认外部图片、字体和 CSS 背景会被识别，避免单文件页面离线打开时失效。
def test_external_asset_refs_detects_remote_page_assets():
    html = """
    <link href="https://fonts.example/css" rel="stylesheet">
    <img src="https://cdn.example/hero.jpg">
    <div style="background-image:url('https://cdn.example/bg.jpg')"></div>
    """

    refs = _external_asset_refs(html.lower())

    assert '<link href="https://' in refs
    assert 'src="https://' in refs
    assert "url('https://" in refs


# LLM: Normal outbound content links are allowed; only page-rendering assets are blocked.
# 函数用途: 确认页面里的普通外部链接不会被误当成图片/字体/脚本依赖。
def test_external_asset_refs_allows_normal_links():
    html = '<a href="https://example.com/story">Furniture story</a>'

    assert _external_asset_refs(html.lower()) == []


# LLM: Disabled-control detection should reject real disabled attributes, not JS state management.
# 函数用途: 购物站允许脚本里动态切换 button.disabled，但不能交付一开始就 disabled 的按钮。
def test_has_disabled_control_ignores_css_and_javascript_state():
    assert _has_disabled_control(".checkout-btn:disabled{color:#aaa} checkoutBtn.disabled = true;") is False
    assert _has_disabled_control('<button class="buy" disabled>Buy</button>') is True


# LLM: A complete single-file furniture page should satisfy the lightweight artifact gate.
# 函数用途: 确认可打开的家具 HTML 文件可以通过自然语言 E2E 的基础验收。
def test_assert_natural_html_output_accepts_complete_page(tmp_path):
    output = tmp_path / "lab_outputs" / "furniture-home" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(
        '<!doctype html><html><body><main>高端家具 Furniture</main><section id="shop">Shop</section><a href="#shop">Shop</a></body></html>',
        encoding="utf-8",
    )

    _assert_natural_html_output(output)


# LLM: _complete_shop_html is a tiny valid shop fixture used by Live Lab contract tests.
# 函数用途: 提供完整购物流程 HTML，避免测试样例自身缺区域或动作。
def _complete_shop_html() -> str:
    return """<!doctype html>
<html>
<head><title>Shop</title></head>
<body>
<section id="register"><form id="register-form"><input name="email"><button data-action="register" type="button" onclick="registerUser()">注册</button></form></section>
<section id="login"><form id="login-form"><input name="email"><button data-action="login" type="button" onclick="loginUser()">登录</button></form></section>
<section id="catalog"><article>商品</article><button data-action="add-to-cart" type="button" onclick="addToCart()">加入购物车</button></section>
<section id="cart"><button data-action="checkout" type="button" onclick="showCheckout()">结算</button></section>
<section id="checkout"><form id="checkout-form"><input name="address"><button data-action="place-order" type="button" onclick="placeOrder()">下单</button></form></section>
<section id="order-confirmation">下单成功</section>
<script>
function registerUser(){return true}
function loginUser(){return true}
function addToCart(){return true}
function showCheckout(){return true}
function placeOrder(){return true}
</script>
</body>
</html>"""


# LLM: _complete_order_csv is a compact valid non-web artifact for file repair tests.
# 函数用途: 提供满足订单报表验收的 CSV 内容，避免测试夹具本身缺字段。
def _complete_order_csv() -> str:
    return "\n".join(
        [
            "order_id,customer,total,status,notes",
            "A-1001,Lin Studio,299.00,PAID,first order",
            "A-1002,North Home,188.50,SHIPPED,priority delivery",
            "SUMMARY,total_orders=2,total_amount=487.50,status=OK,notes=ready",
            "",
        ]
    )
