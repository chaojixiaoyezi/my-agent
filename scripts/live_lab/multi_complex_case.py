# LLM: Multi-complex Live Lab catches concurrent task-card and foreground-lane regressions.
# 模块用途: 用多个真实复杂任务压测 gateway 主会话、任务卡合同、worker 派发和前台状态查询。

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .shop_case import _parse_gateway_no_wait, _probe_foreground_main_agent


# LLM: ComplexContract belongs to this module's structured contract; keep callers and tests aligned before changing it.
# 类用途: 保存本模块的结构化状态或公开边界。
@dataclass(frozen=True)
class ComplexContract:
    """Structured expectations for one Live Lab prompt."""

    case_id: str
    prompt: str
    output_files: list[str]
    required_files: list[str] = field(default_factory=list)
    required_dom_ids: list[str] = field(default_factory=list)
    require_script: bool | None = None


# LLM: case_multi_complex_subagents uses fixed structured test contracts, not production prose parsing.
# 函数用途: 同时覆盖多个主请求和单主请求多任务两种场景，检查任务合同、前台通道和任务隔离。
def case_multi_complex_subagents(lab) -> None:
    lab.section("CASE multi_complex_subagents")
    contracts = _complex_contracts()
    bundle_contracts = _bundle_contracts(contracts)
    all_contracts = [*contracts, *bundle_contracts]
    bundle_prompt = _bundle_prompt(bundle_contracts)
    lab.record_prompt("multi_complex_bundle", bundle_prompt)
    for contract in contracts:
        lab.record_prompt(f"multi_complex_{contract.case_id}", contract.prompt)

    lab.run_command(lab.agent_command("gateway", "start", "--force"), timeout=90)
    try:
        separate_requests = _submit_separate_requests(lab, contracts)
        bundle_request = _submit_no_wait(lab, "multi_complex_bundle", bundle_prompt)
        _wait_for_contract_cards(
            lab,
            contracts=all_contracts,
            min_total=len(all_contracts),
            timeout_seconds=_contract_wait_budget(lab),
        )
        _probe_foreground_main_agent(lab)
        _write_request_manifest(lab, [*separate_requests, bundle_request])
    finally:
        lab.run_command(
            lab.agent_command("gateway", "stop", "--timeout", "15", "--kill", "--reason", "multi complex live lab done"),
            timeout=45,
            allow_fail=True,
        )


# LLM: _submit_separate_requests is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _submit_separate_requests(lab, contracts: list[ComplexContract]) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    for contract in contracts:
        requests.append(_submit_no_wait(lab, f"multi_complex_{contract.case_id}", contract.prompt))
    return requests


# LLM: _submit_no_wait is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _submit_no_wait(lab, label: str, prompt: str) -> dict[str, str]:
    result = lab.run_command(
        lab.agent_command("gateway", "ask", prompt, "--no-wait", "--json"),
        timeout=60,
    )
    request_id, response_path = _parse_gateway_no_wait(result.stdout)
    lab.log(f"{label}_request_id={request_id}")
    lab.log(f"{label}_response={response_path}")
    return {"label": label, "request_id": request_id, "response_path": str(response_path)}


# LLM: _wait_for_contract_cards is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _wait_for_contract_cards(
    lab,
    *,
    contracts: list[ComplexContract],
    min_total: int,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    last_probe = 0.0
    last_snapshot: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        cards = _subagent_task_cards(lab.fixture_root)
        last_snapshot = cards
        if len(cards) >= min_total:
            _assert_contract_coverage(cards, contracts)
            lab.log(f"multi_complex_contract_cards=pass total={len(cards)}")
            return
        now = time.monotonic()
        if now - last_probe >= 30:
            _probe_foreground_main_agent(lab)
            lab.log(f"multi_complex_wait cards={len(cards)} expected_min={min_total}")
            last_probe = now
        time.sleep(2.0)
    summary_path = lab.responses_dir / "multi_complex_contract_cards.last.json"
    summary_path.write_text(json.dumps(last_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    raise RuntimeError(f"多复杂任务未在预算内创建足够任务卡: got={len(last_snapshot)} expected_min={min_total} snapshot={summary_path}")


# LLM: _assert_contract_coverage is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _assert_contract_coverage(cards: list[dict[str, object]], contracts: list[ComplexContract]) -> None:
    missing: dict[str, list[str]] = {}
    for contract in contracts:
        task = _find_task_for_contract(cards, contract)
        if not task:
            missing[contract.case_id] = ["task_card"]
            continue
        errors = _contract_errors(task, contract)
        if errors:
            missing[contract.case_id] = errors
    if missing:
        raise RuntimeError(f"多复杂任务卡缺失机器合同: {missing}")


# LLM: _find_task_for_contract is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _find_task_for_contract(cards: list[dict[str, object]], contract: ComplexContract) -> dict[str, object] | None:
    for card in cards:
        attrs = _attrs(card)
        outputs = set(_string_list(attrs.get("output_files")))
        if any(output in outputs for output in contract.output_files):
            return card
    return None


# LLM: _contract_errors is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _contract_errors(card: dict[str, object], contract: ComplexContract) -> list[str]:
    attrs = _attrs(card)
    errors: list[str] = []
    outputs = set(_string_list(attrs.get("output_files")))
    required_files = set(_string_list(attrs.get("required_files")))
    required_dom_ids = set(_string_list(attrs.get("required_dom_ids")))
    if not set(contract.output_files).issubset(outputs):
        errors.append("output_files")
    if contract.required_files and not set(contract.required_files).issubset(required_files):
        errors.append("required_files")
    if contract.required_dom_ids and not set(contract.required_dom_ids).issubset(required_dom_ids):
        errors.append("required_dom_ids")
    if contract.require_script is not None and bool(attrs.get("require_script")) is not contract.require_script:
        errors.append("require_script")
    return errors


# LLM: _subagent_task_cards is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _subagent_task_cards(fixture_root: Path) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for task_path in sorted((fixture_root / ".my_agent" / "subagents").glob("subagent-*/task.json")):
        try:
            payload = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            cards.append(payload)
    return cards


# LLM: _attrs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _attrs(card: dict[str, object]) -> dict[str, object]:
    value = card.get("attributes")
    return value if isinstance(value, dict) else {}


# LLM: _string_list is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: _contract_wait_budget is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _contract_wait_budget(lab) -> float:
    return max(float(lab.args.timeout), 240.0)


# LLM: _write_request_manifest is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _write_request_manifest(lab, requests: list[dict[str, str]]) -> None:
    path = lab.responses_dir / "multi_complex_requests.json"
    path.write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8")
    lab.log(f"multi_complex_requests={path}")


# LLM: _complex_contracts is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _complex_contracts() -> list[ComplexContract]:
    return [
        ComplexContract(
            case_id="deepseek_papers",
            prompt=(
                "请安排小傻妞做长期资料任务：收集 2025 年之后 DeepSeek 公开论文，"
                "逐篇翻译成中文，并尽量保持原 PDF 的目录、图片位置、表格和版式。"
                "最终每篇论文输出一个 PDF，保存到 lab_outputs/deepseek-papers/translated-pdfs/。"
                "请把子任务的 output_files 机器合同设置为 lab_outputs/deepseek-papers/translated-pdfs/README.md，"
                "required_files 设置为 README.md；README 里列出后续 PDF 清单、来源、状态和未完成项。"
            ),
            output_files=["lab_outputs/deepseek-papers/translated-pdfs/README.md"],
            required_files=["README.md"],
        ),
        ComplexContract(
            case_id="github_stars",
            prompt=(
                "请安排小傻妞做数据分析任务：统计 2026 年后每周 GitHub 升星最快的前 20 个项目，"
                "输出 XLSX，字段包含中文介绍、推荐理由、升星数量、项目地址、技术栈、后期预测。"
                "最终文件保存到 lab_outputs/github-stars/weekly_top20.xlsx。"
                "请把 output_files 机器合同设置为 lab_outputs/github-stars/weekly_top20.xlsx，"
                "required_files 设置为 weekly_top20.xlsx。"
            ),
            output_files=["lab_outputs/github-stars/weekly_top20.xlsx"],
            required_files=["weekly_top20.xlsx"],
        ),
        ComplexContract(
            case_id="shop",
            prompt=(
                "请安排小傻妞做购物站任务：开发静态购物网站，完整覆盖注册、登录、浏览商品、加入购物车、"
                "结算到付款前流程，不使用数据库，用文件或浏览器本地状态模拟存储。"
                "最终单文件保存到 lab_outputs/shop-demo/index.html。"
                "页面必须包含 DOM id：register、login、catalog、cart、checkout、order-confirmation。"
                "请把 output_files 设置为 lab_outputs/shop-demo/index.html，required_files 设置为 index.html，"
                "required_dom_ids 设置为上述 6 个 id，require_script 设置为 true。"
            ),
            output_files=["lab_outputs/shop-demo/index.html"],
            required_files=["index.html"],
            required_dom_ids=["register", "login", "catalog", "cart", "checkout", "order-confirmation"],
            require_script=True,
        ),
        ComplexContract(
            case_id="articles",
            prompt=(
                "请安排小傻妞做文章下载任务：每周最热文章下载，并按文章类型自动分类，"
                "输出索引、分类说明和下载状态。最终索引保存到 lab_outputs/hot-articles/index.xlsx，"
                "下载文件目录为 lab_outputs/hot-articles/files/。"
                "请把 output_files 机器合同设置为 lab_outputs/hot-articles/index.xlsx，"
                "required_files 设置为 index.xlsx。"
            ),
            output_files=["lab_outputs/hot-articles/index.xlsx"],
            required_files=["index.xlsx"],
        ),
    ]


# LLM: _bundle_contracts is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _bundle_contracts(contracts: list[ComplexContract]) -> list[ComplexContract]:
    return [_bundle_contract(contract) for contract in contracts]


# LLM: _bundle_contract is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _bundle_contract(contract: ComplexContract) -> ComplexContract:
    output_files = [_bundle_output_path(path) for path in contract.output_files]
    return replace(
        contract,
        case_id=f"bundle_{contract.case_id}",
        prompt=_bundle_prompt_paths(contract.prompt),
        output_files=output_files,
    )


# LLM: _bundle_prompt_paths is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _bundle_prompt_paths(prompt: str) -> str:
    replacements = {
        "lab_outputs/deepseek-papers/translated-pdfs/": "lab_outputs/bundle/deepseek-papers/translated-pdfs/",
        "lab_outputs/github-stars/weekly_top20.xlsx": "lab_outputs/bundle/github-stars/weekly_top20.xlsx",
        "lab_outputs/shop-demo/index.html": "lab_outputs/bundle/shop-demo/index.html",
        "lab_outputs/hot-articles/index.xlsx": "lab_outputs/bundle/hot-articles/index.xlsx",
        "lab_outputs/hot-articles/files/": "lab_outputs/bundle/hot-articles/files/",
    }
    result = prompt
    for source, target in replacements.items():
        result = result.replace(source, target)
    return result


# LLM: _bundle_output_path is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _bundle_output_path(path: str) -> str:
    return _bundle_prompt_paths(path)


# LLM: _bundle_prompt is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _bundle_prompt(contracts: list[ComplexContract]) -> str:
    parts = [
        "请你作为一个主代理，一次性安排多个小傻妞并行处理下面 4 个复杂任务。",
        "必须使用 create_subagents 的 items/tasks 批量参数一次性创建 4 个 item；不要拆成多轮逐个 create_subagents。",
        "每个任务都必须创建独立子任务，不要合并成一个大任务；每个子任务都要保留我写明的 output_files、required_files、required_dom_ids、require_script 机器合同字段。",
    ]
    parts.extend(f"{index}. {contract.prompt}" for index, contract in enumerate(contracts, start=1))
    parts.append("创建任务后请立即调度第一轮，并保持前台可以随时查询进度。")
    return "\n\n".join(parts)
