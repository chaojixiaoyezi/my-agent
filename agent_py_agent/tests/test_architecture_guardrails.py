from __future__ import annotations

"""LLM: freezes architecture debt baselines and blocks new drift.

给人看的解释：
这些测试不是业务功能测试，而是工程治理护栏。它们允许历史债务存在，
但要求后续改动不能继续增加运行产物、星号导入、大入口文件和垃圾命名。
"""

import ast
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

STAR_IMPORT_BASELINE: dict[str, int] = {}

JUNK_NAME_BASELINE = {
    "agent_py_agent/agent/subagents/utils.py",
    "agent_py_agent/cli/common.py",
}

FORBIDDEN_CLASS_BASELINE: set[str] = set()

BUNDLE_VARARG_FUNCTION_EXEMPTIONS = {
    "agent_py_agent/agent/retrieval/embedding_usage.py:wrapper": (
        "Transparent purpose-labelling decorator (counted_as) must preserve arbitrary method signatures; "
        "嵌入用量计数的用途标注，不是产品服务接口。"
    ),
    "agent_py_agent/agent/adapter/feishu.py:log_message": (
        "HTTPRequestHandler-style logging override accepts formatter arguments; "
        "not a product service parameter entry point."
    ),
    "agent_py_agent/scripts/b_acceptance/timeout_budget_evidence.py:log_message": (
        "HTTPRequestHandler-style logging override accepts formatter arguments; "
        "门槛预算取证脚本(超时预算证据采集), not a product service parameter entry point."
    ),
    "scripts/fake_provider_outage.py:log_message": (
        "HTTPRequestHandler-style logging override accepts formatter arguments; "
        "drill harness script, not a product service parameter entry point."
    ),
    "scripts/run_r1_02_proxy.py:log_message": (
        "HTTPRequestHandler-style logging override accepts formatter arguments; "
        "R1-02 resilience-drill proxy script, not a product service parameter entry point."
    ),
    "scripts/watch_harness/content_source_simulator.py:log_message": (
        "HTTPRequestHandler-style logging override accepts formatter arguments; "
        "watch self-test harness script, not a product service parameter entry point."
    ),
    "agent_py_agent/agent/concurrency/retry.py:wrapper": (
        "Transparent retry decorator forwarding must preserve arbitrary callable signatures; "
        "this is infrastructure, not a product service interface."
    ),
    "agent_py_agent/agent/concurrency/retry.py:run": (
        "Transparent retry runner forwards arbitrary callable arguments; "
        "callers must not use this as a product interface pattern."
    ),
    "agent_py_agent/agent/gateway_parts/http_service.py:log_message": (
        "HTTP server logging override accepts formatter arguments; "
        "not a product service parameter entry point."
    ),
    "agent_py_agent/agent/web/dashboard.py:log_message": (
        "Web dashboard HTTP handler logging override accepts formatter arguments; "
        "not a product service parameter entry point (same as http_service/feishu)."
    ),
    "agent_py_agent/cli/chat_parts/tui_browser_login.py:log_message": (
        "Loopback login-callback HTTP handler logging override accepts formatter arguments; "
        "silenced because the request line carries the one-time authorization code, "
        "not a product service parameter entry point (same as http_service/feishu)."
    ),
    "scripts/watch_harness/multi_source_simulator.py:log_message": (
        "Watch-harness HTTP simulator logging override accepts formatter arguments; "
        "not a product service parameter entry point (same as http_service/feishu)."
    ),
    "scripts/watch_harness/messy_source_simulator.py:log_message": (
        "Watch-harness HTTP simulator logging override accepts formatter arguments; "
        "not a product service parameter entry point (same as multi_source_simulator)."
    ),
    "scripts/gateway_pressure.py:log_message": (
        "Pressure-harness stub LLM logging override accepts formatter arguments; "
        "not a product service parameter entry point (same as http_service/feishu)."
    ),
    "agent_py_agent/agent/memory_archive/runtime/_event_utils.py:_first_bool": (
        "Local event-field helper accepts candidate keys; no service boundary."
    ),
    "agent_py_agent/agent/memory_archive/runtime/_event_utils.py:_first_text": (
        "Local event-field helper accepts candidate keys; no service boundary."
    ),
    "agent_py_agent/cli/chat_parts/renderer.py:style_text": (
        "Small rendering helper accepts style codes; no product service boundary."
    ),
    "agent_py_agent/cli/scenario_utils.py:scenario_command": (
        "CLI command-list builder accepts command fragments; no service boundary."
    ),
    "agent_py_agent/cli/thinking_spinner.py:__exit__": (
        "Context-manager protocol method accepts arbitrary exception details."
    ),
    "scripts/live_lab/runner.py:agent_command": (
        "Script command-list builder accepts command fragments; no service boundary."
    ),
}

BUNDLE_KWARG_FUNCTION_EXEMPTIONS = {
    "agent_py_agent/agent/retrieval/embedding_usage.py:wrapper": (
        "Transparent purpose-labelling decorator (counted_as) must preserve arbitrary method signatures; "
        "嵌入用量计数的用途标注，不是产品服务接口。"
    ),
    "agent_py_agent/agent/concurrency/retry.py:wrapper": (
        "Transparent retry decorator forwarding must preserve arbitrary callable signatures; "
        "this is infrastructure, not a product service interface."
    ),
    "agent_py_agent/agent/concurrency/retry.py:run": (
        "Transparent retry runner forwards arbitrary callable arguments; "
        "callers must not use this as a product interface pattern."
    ),
}

RUNTIME_ARTIFACT_NAMES = {
    ".DS_Store",
    ".coverage",
    ".pytest_cache",
    "__pycache__",
    "MagicMock",
    "mutation_test_report.json",
}
RUNTIME_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}
JUNK_FILE_NAMES = {
    "common.py",
    "final.py",
    "final2.py",
    "helper.py",
    "helpers.py",
    "manager2.py",
    "manager_extra.py",
    "misc.py",
    "new.py",
    "old.py",
    "temp.py",
    "tmp.py",
    "utils.py",
}

NATURAL_LANGUAGE_FACT_SOURCE_FORBIDDEN_MARKERS = {
    "agent_py_agent/agent/agent_core/runner/ref_fields.py": [
        "_fallback_file_ref_roles",
        "_READ_REF_MARKERS",
        "_WRITE_REF_MARKERS",
        "goal_input_refs",
        "goal_output_refs",
        "_structured_refs",
    ],
    "agent_py_agent/agent/subagents/services/base.py": [
        "_extract_write_dirs",
        "_DIR_PATTERN",
        "_WINDOWS_DIR_PATTERN",
        "_HOME_DIR_PATTERN",
    ],
    "agent_py_agent/agent/agent_core/orchestration/create_constraints.py": [
        "goal_has_concrete_file_target",
        "goal_has_single_concrete_file_target",
        "_CONCRETE_FILE_TARGET_RE",
        "getattr(agent, \"_current_user_prompt\"",
        "_structured_parent_constraints(user_text)",
        "_normalized_goal",
        "goal_output_refs(params.goal)",
        "goal_output_refs(candidate.goal)",
    ],
    "agent_py_agent/agent/agent_core/orchestration/write_guard.py": [
        "_goal_has_write_intent",
        "_path_candidate_is_write_target",
        "_WRITE_INTENT_WORDS",
        "_NEGATED_WRITE_MARKERS",
        "_TARGET_LEFT_MARKERS",
    ],
    "agent_py_agent/agent/agent_core/orchestration/dispatch/scope.py": [
        "_CONCRETE_FILE_TARGET_RE",
        "_is_active_concrete_worker_task",
    ],
    "agent_py_agent/agent/agent_core/subagent/finalize_helpers.py": [
        "_structured_next_step_text",
        "_looks_like_tool_round_limit_text",
    ],
    "agent_py_agent/agent/subagents/services/hierarchy/write_policy.py": [
        "_extract_write_dirs",
    ],
    "agent_py_agent/agent/agent_core/spawn_role_seed.py": [
        "_extract_write_dirs",
    ],
    "agent_py_agent/agent/agent_core/orchestration_delegation_intent.py": [
        "_mentions_delegate_actor",
        "_mentions_delegation_action",
        "_mentions_parent_should_not_do_body",
        "prompt_requests_refs_only_delegation",
        "prompt_requests_subagent_delegation",
        "user_authorized_parent_body_read",
        "prompt: str",
        "str(prompt",
        "user_prompt",
    ],
    "agent_py_agent/agent/agent_core/orchestration_quality_intent.py": [
        "_natural_quality_roles",
        "_mentions_agent_role",
        "_mentions_agent_word",
        "required_quality_roles_from_prompt",
    ],
    "agent_py_agent/agent/subagents/services/hierarchy/context.py": [
        "_missing_relevant_file_terms",
        "_missing_forbidden_file_terms",
        "_missing_hierarchy_contract_terms",
        "_missing_capability_contract_terms",
        "_implicit_hierarchy_contract_segments",
        "_labeled_contract_field",
        "_scope_tokens",
        "_segment_score",
        "hierarchy_contract_present",
    ],
    "agent_py_agent/agent/agent_core/orchestration_root_contract.py": [
        "_missing_terms",
        "_missing_hierarchy_contracts",
    ],
    "agent_py_agent/agent/subagents/patch/patch_apply_helpers.py": [
        "extract_patch_test_command",
    ],
    "agent_py_agent/agent/subagents/services/patch_apply/test_commands.py": [
        "for check in task.acceptance_checks",
    ],
    "agent_py_agent/agent/agent_core/failure_analysis_service.py": [
        "_goal_based_split_suggestions",
    ],
    "agent_py_agent/agent/subagents/services/acceptance_controlled_exec_findings.py": [
        "getattr(task, \"goal\", \"\")",
        "for item in task.acceptance_checks",
    ],
    "agent_py_agent/agent/subagents/services/hierarchy/schedule_idempotency.py": [
        "_normalized_goal",
        "request.goal == task.goal",
        "goal_output_refs(request.goal)",
    ],
    "agent_py_agent/agent/subagents/execution/test_items.py": [
        "artifact_summaries",
        "_artifact_summaries_by_path",
    ],
}


def _tracked_files() -> list[str]:
    """Get list of tracked files, with source-tree handling for non-git environments."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # Source tarball / non-git environment path.
    return [
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and not path.name.startswith("._")
        and path.name != ".DS_Store"
        and path.suffix != ".pyc"
    ]


def _python_source_files() -> list[Path]:
    files: list[Path] = []
    for package in ("agent_py_agent", "scripts"):
        root = REPO_ROOT / package
        if root.exists():
            files.extend(
                path for path in root.rglob("*.py")
                if ".git" not in path.parts
                and "__pycache__" not in path.parts
                and not path.name.startswith("._")
            )
    return files


def _star_import_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "*"
    )


def test_no_new_star_imports() -> None:
    """Existing star imports are debt; new ones must not appear."""

    counts: dict[str, int] = {}
    for path in _python_source_files():
        star_count = _star_import_count(path)
        if star_count:
            counts[path.relative_to(REPO_ROOT).as_posix()] = star_count

    assert counts == STAR_IMPORT_BASELINE


def test_runtime_artifacts_are_not_present_in_tracked_files() -> None:
    """Generated local state must stay out of the repository tree."""

    offenders: list[str] = []
    for relative_path in _tracked_files():
        path = REPO_ROOT / relative_path
        if not path.exists():
            continue
        if any(part in RUNTIME_ARTIFACT_NAMES for part in path.parts):
            offenders.append(relative_path)
            continue
        if path.suffix in RUNTIME_ARTIFACT_SUFFIXES:
            offenders.append(relative_path)

    assert offenders == []


def test_no_new_junk_filenames() -> None:
    """New modules need specific names that communicate ownership."""

    offenders = []
    for relative_path in _tracked_files():
        path = Path(relative_path)
        if path.name not in JUNK_FILE_NAMES:
            continue
        if relative_path in JUNK_NAME_BASELINE:
            continue
        offenders.append(relative_path)

    assert offenders == []


def test_code_does_not_use_plain_language_as_machine_facts() -> None:
    """Runtime facts must come from structured fields, not prose keyword guesses."""

    assert _plain_language_fact_source_offenders() == []


def _plain_language_fact_source_offenders() -> list[str]:
    """Return removed prose-fact helper markers that reappeared in source."""

    offenders: list[str] = []
    for relative_path, markers in NATURAL_LANGUAGE_FACT_SOURCE_FORBIDDEN_MARKERS.items():
        offenders.extend(_forbidden_marker_hits(relative_path, markers))
    return offenders


def _forbidden_marker_hits(relative_path: str, markers: list[str]) -> list[str]:
    """Check one source file for forbidden helper names or constants."""

    path = REPO_ROOT / relative_path
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [f"{relative_path}: {marker}" for marker in markers if marker in text]


def test_compileall_succeeds(tmp_path, monkeypatch) -> None:
    """All Python source must compile without syntax errors."""

    import compileall
    import sys

    # compileall 是显式编译，不理会 PYTHONDONTWRITEBYTECODE，会给每个产品目录写 __pycache__（全量一次 97 个目录）；
    # 用 sys.pycache_prefix 把字节码改写到临时目录，语法检查本身不变。
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "pycache"))
    result = compileall.compile_dir(
        str(REPO_ROOT / "agent_py_agent"),
        quiet=2,
        force=True,
    )
    assert result is True


def _check_forbidden_class(path: Path, forbidden: set[str]) -> list[str]:
    """Check one file for forbidden class definitions, excluding baseline entries."""
    offenders = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return offenders
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in forbidden:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        key = f"{rel}:{node.name}"
        if key not in FORBIDDEN_CLASS_BASELINE:
            offenders.append(key)
    return offenders


def test_no_new_forbidden_globals() -> None:
    """No new files may define forbidden global patterns like AgentManager, TaskManager."""

    FORBIDDEN_CLASS_NAMES = {
        "AgentManager",
        "TaskManager",
        "ServiceManager",
        "RuntimeEverything",
        "CommonHelper",
    }
    offenders = []
    for path in _python_source_files():
        offenders.extend(_check_forbidden_class(path, FORBIDDEN_CLASS_NAMES))

    assert offenders == []


def _var_parameter_offenders(
    *,
    parameter_kind: str,
    exemptions: dict[str, str],
) -> list[str]:
    offenders: list[str] = []
    for path in _python_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in f"/{rel}":
            continue
        offenders.extend(_var_parameter_offenders_in_path(path, rel, parameter_kind, exemptions))
    return offenders


def _var_parameter_offenders_in_path(
    path: Path,
    rel: str,
    parameter_kind: str,
    exemptions: dict[str, str],
) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameter = node.args.vararg if parameter_kind == "args" else node.args.kwarg
        if parameter is None:
            continue
        key = f"{rel}:{node.name}"
        if key in exemptions:
            continue
        sigil = "*" if parameter_kind == "args" else "**"
        offenders.append(f"{rel}:{node.lineno} {node.name}({sigil}{parameter.arg})")
    return offenders


def test_product_code_has_no_var_keyword_service_interfaces() -> None:
    """Service-facing product code must use typed bundles instead of **kwargs."""

    assert all(reason.strip() for reason in BUNDLE_KWARG_FUNCTION_EXEMPTIONS.values())
    assert _var_parameter_offenders(
        parameter_kind="kwargs",
        exemptions=BUNDLE_KWARG_FUNCTION_EXEMPTIONS,
    ) == []


def test_product_code_has_no_var_positional_service_interfaces() -> None:
    """Service-facing product code must use typed bundles instead of *args."""

    assert all(reason.strip() for reason in BUNDLE_VARARG_FUNCTION_EXEMPTIONS.values())
    assert _var_parameter_offenders(
        parameter_kind="args",
        exemptions=BUNDLE_VARARG_FUNCTION_EXEMPTIONS,
    ) == []


def test_no_macos_or_python_cache_artifacts() -> None:
    """Source tree must not contain tracked macOS metadata or Python cache files."""

    tracked = set(_tracked_files())
    bad: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        # Only check tracked files (untracked dirty files are in .gitignore)
        if rel not in tracked:
            continue
        if path.name.startswith("._") or path.name == ".DS_Store":
            bad.append(rel)
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            bad.append(rel)

    assert not bad, f"Found dirty tracked artifacts: {bad}"


def test_governance_docs_exist() -> None:
    """Key governance documents must be present in the repo."""

    required_docs = [
        "CODE_SIZE_POLICY.md",
        "CODE_SIZE_REPORT.md",
        "ARCHITECTURE_EXEMPTIONS.md",
        "REFACTORING_BACKLOG.md",
        "CLEAN_PACKAGE_POLICY.md",
        "TESTING_POLICY.md",
    ]
    missing = [name for name in required_docs if not (REPO_ROOT / name).exists()]
    assert not missing, f"Missing governance docs: {missing}"


# ---- scripts/ 本机 Gateway 凭据守卫（G2b 前置，flc）----
# LLM: scripts/ 下指向本机 Gateway 的 HTTP 调用（curl/urllib）必须带本机客户端凭据头
#   （X-Gateway-Token，或经 gateway_script_headers/gateway_client_credentials 取得的凭据变量），
#   或访问公开只读路由（/status、/metrics）；G2b 打开后回环不再自带信任，缺凭据的脚本会被降匿名。
#   按 URL 目标结构判定，不写死文件名；只覆盖能静态看出指向 Gateway 的调用（字面量地址，或
#   文件内被赋值为 Gateway 地址的变量），参数化 URL 的调用不在覆盖内。

GATEWAY_HOST_MARKERS = ("127.0.0.1", "localhost")
GATEWAY_PORT_TEXT = "8420"
GATEWAY_TARGET_RE = re.compile(r"(?:127\.0\.0\.1|localhost):(?:8420\b|\$\{?GATEWAY_PORT\}?)")
GATEWAY_PUBLIC_ROUTE_RE = re.compile(r"/(?:status|metrics)(?![\w-])")
GATEWAY_CREDENTIAL_MARKERS = ("x-gateway-token", "gateway_script_headers", "gateway_client_credentials")
# LLM: 凭据头的值必须来自变量引用（$VAR/${VAR}/"${ARR[@]}"）或产品入口调用；写死字面量会让脚本"S 看着带了头"、
#   实际每个部署发同一个编死的假串，G2b 一开就变成匿名请求。所以只认结构：冒号后必须以 $ 开头才算变量。
SHELL_CREDENTIAL_HEADER_RE = re.compile(
    r"x-gateway-token\s*:\s*(?P<value>[^'\"\s]*)", re.IGNORECASE)
PYTHON_CREDENTIAL_CALL_NAMES = frozenset({"gateway_script_headers", "gateway_client_credentials"})
SCRIPT_SUFFIXES = (".sh", ".py")
PYTHON_HTTP_CALL_NAMES = frozenset(
    {"urlopen", "urllib.request.urlopen", "urllib.request.Request", "Request"})


def _gateway_script_files() -> list[Path]:
    """scripts/ 下全部 .sh/.py 验收脚本（按目录结构扫描，不写死具体文件名）。"""

    root = REPO_ROOT / "scripts"
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in SCRIPT_SUFFIXES and "__pycache__" not in path.parts
    )


def _gateway_address_variables(lines: list[str]) -> set[str]:
    """收集被简单赋值为“回环地址 + Gateway 端口”的变量名（shell/python 通用形态）。"""

    names: set[str] = set()
    for line in lines:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", line)
        if match is None:
            continue
        value = match.group(2)
        if any(marker in value for marker in GATEWAY_HOST_MARKERS) and (
            GATEWAY_PORT_TEXT in value or "GATEWAY_PORT" in value or "gateway_port" in value
        ):
            names.add(match.group(1))
    return names


def _credential_variables(lines: list[str]) -> set[str]:
    """收集“出现在 X-Gateway-Token 取值位置”的 shell 变量名（$VAR 引用或 VAR=/VAR+=( 赋值）。

    只认头值本身引用的变量，不认同一行里出现的其它变量：否则 `curl -H "X-Gateway-Token: 固定串" "$GW/ask"`
    会因为行内有 `$GW` 被误判成“带了凭据变量”，写死的字面量也就混过去了。
    """

    names: set[str] = set()
    for line in lines:
        if "x-gateway-token" not in line.lower():
            continue
        for match in SHELL_CREDENTIAL_HEADER_RE.finditer(line):
            names.update(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", match.group("value")))
        # 数组/变量赋值形态：VAR=(-H "X-Gateway-Token: $T") 或 VAR+=(...) 也算凭据来源。
        names.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\+?=\(", line))
    return names


def _references_shell_variable(text: str, names: set[str]) -> bool:
    """shell 文本是否引用了集合里的任一变量（$VAR / ${VAR} 形态）。"""

    return any(re.search(r"\$\{?" + re.escape(name) + r"\}?", text) for name in names)


def _references_python_name(text: str, names: set[str]) -> bool:
    """Python 文本是否引用了集合里的任一名字（裸标识符形态）。"""

    return any(re.search(r"\b" + re.escape(name) + r"\b", text) for name in names)


def _text_carries_credential_marker(text: str) -> bool:
    """文本里是否出现凭据头名或产品凭据入口（大小写不敏感）。"""

    lowered = text.lower()
    return any(marker in lowered for marker in GATEWAY_CREDENTIAL_MARKERS)


def _shell_credential_header_is_variable(logical: str) -> bool:
    """shell 逻辑行里的 X-Gateway-Token 取值是否来自变量引用（否则是写死字面量）。"""

    found = False
    for match in SHELL_CREDENTIAL_HEADER_RE.finditer(logical):
        found = True
        value = match.group("value")
        # 取值必须是变量引用（$VAR / ${VAR} / "${ARR[@]}"）或数组展开；纯字面量一律算写死。
        if not value.startswith("$") and "@]" not in value:
            return False
    return found


def _python_calls_credential_entry(source: str) -> bool:
    """Python 源码里是否真的调用了产品凭据入口（按 AST 调用形态，不看字符串字面量）。"""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Call) and _dotted_call_name(node.func).split(".")[-1] in PYTHON_CREDENTIAL_CALL_NAMES
        for node in ast.walk(tree)
    )


def _shell_logical_lines(text: str) -> list[tuple[int, str]]:
    """把反斜杠续行合并成逻辑行，保留起始行号（报错要指到调用开始的那一行）。"""

    merged: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        merged.append((start, buffer))
        buffer = ""
    if buffer:
        merged.append((start, buffer))
    return merged


def _shell_text_offenders(text: str, rel: str) -> list[str]:
    """返回 shell 文本里缺凭据的本机 Gateway curl 调用（文件:行 + 说明）。"""

    lines = text.splitlines()
    gateway_vars = _gateway_address_variables(lines)
    credential_vars = _credential_variables(lines)
    offenders: list[str] = []
    for start, logical in _shell_logical_lines(text):
        stripped = logical.strip()
        if not stripped or stripped.startswith("#") or "curl" not in logical:
            continue
        targets = bool(GATEWAY_TARGET_RE.search(logical)) or _references_shell_variable(
            logical, gateway_vars)
        if not targets or GATEWAY_PUBLIC_ROUTE_RE.search(logical):
            continue
        if _references_shell_variable(logical, credential_vars) or _shell_credential_header_is_variable(logical):
            continue
        detail = ("凭据头的值写死成了字面量（必须来自 $VAR/${VAR}/数组展开或产品入口）"
                  if _text_carries_credential_marker(logical) else "未带凭据头（X-Gateway-Token/凭据变量）")
        offenders.append(f"{rel}:{start} curl 指向本机 Gateway 但{detail}，且非公开路由")
    return offenders


def _dotted_call_name(node: ast.expr) -> str:
    """拼出调用表达式的点分名字（如 urllib.request.urlopen）。"""

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _enclosing_scope_text(source: str, lineno: int, ranges: list[tuple[int, int]]) -> str:
    """返回调用点所属函数的源码段；模块级调用返回整个文件（脚本型代码凭据常与调用分离）。"""

    enclosing = [item for item in ranges if item[0] <= lineno <= item[1]]
    if not enclosing:
        return source
    start, end = min(enclosing, key=lambda item: item[1] - item[0])
    return "\n".join(source.splitlines()[start - 1:end])


def _python_text_offenders(source: str, rel: str) -> list[str]:
    """返回 Python 文本里缺凭据的本机 Gateway urllib 调用（文件:行 + 说明）。"""

    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []
    gateway_vars = _gateway_address_variables(source.splitlines())
    ranges = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _dotted_call_name(node.func)
        if call_name not in PYTHON_HTTP_CALL_NAMES:
            continue
        segment = ast.get_source_segment(source, node) or ""
        targets = bool(GATEWAY_TARGET_RE.search(segment)) or _references_python_name(
            segment, gateway_vars)
        if not targets or GATEWAY_PUBLIC_ROUTE_RE.search(segment):
            continue
        scope = _enclosing_scope_text(source, node.lineno, ranges)
        if _python_calls_credential_entry(scope):
            continue
        detail = ("凭据头的值写死成了字面量（必须来自 gateway_script_headers()/gateway_client_credentials() 或变量）"
                  if _text_carries_credential_marker(scope) else "未带凭据（gateway_script_headers/X-Gateway-Token）")
        offenders.append(f"{rel}:{node.lineno} {call_name} 指向本机 Gateway 但{detail}，且非公开路由")
    return offenders


def _gateway_script_credential_offenders() -> list[str]:
    """扫描 scripts/ 下全部脚本，返回未带凭据的本机 Gateway 调用。"""

    offenders: list[str] = []
    for path in _gateway_script_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".sh":
            offenders.extend(_shell_text_offenders(text, rel))
        else:
            offenders.extend(_python_text_offenders(text, rel))
    return offenders


def test_scripts_gateway_http_calls_carry_credentials() -> None:
    """scripts/ 下指向本机 Gateway 的 curl/urllib 调用必须带凭据头或访问公开路由。"""

    offenders = _gateway_script_credential_offenders()
    assert not offenders, "scripts/ 下存在未带本机凭据的 Gateway 调用：\n" + "\n".join(offenders)


# LLM: 自检样本按“期望是否违规”成对列出，新增形态只往表里加一行；表驱动让断言保持一层，也便于看清哪一侧变红。
#   约定：True = 必须被标红（违规），False = 必须放行。
GATEWAY_SCRIPT_SELF_CHECK_SAMPLES = (
    ('GW="http://127.0.0.1:8420"\ncurl -s "$GW/ask"\n', "sample.sh", True),
    ('GW="http://127.0.0.1:8420"\nGW_AUTH_HEADER=(-H "X-Gateway-Token: $T")\n'
     'curl -s "$GW/ask" ${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"}\n', "sample.sh", False),
    # 写死字面量必须红：守卫只看“值是不是来自变量/产品入口”，不看行里有没有出现过头名。
    ('GW="http://127.0.0.1:8420"\ncurl -s -H "X-Gateway-Token: abc123" "$GW/ask"\n', "sample.sh", True),
    ('GW="http://127.0.0.1:8420"\ncurl -s -H "X-Gateway-Token: <redacted>" "$GW/ask"\n', "sample.sh", True),
    # 变量形态都放行：$VAR、${VAR}。
    ('GW="http://127.0.0.1:8420"\nTOKEN=x\ncurl -s -H "X-Gateway-Token: $TOKEN" "$GW/ask"\n', "sample.sh", False),
    ('GW="http://127.0.0.1:8420"\nTOKEN=x\ncurl -s -H "X-Gateway-Token: ${TOKEN}" "$GW/ask"\n', "sample.sh", False),
    ('GW="http://127.0.0.1:8420"\ncurl -s "$GW/status"\n', "sample.sh", False),
    ('import urllib.request\ndef go():\n    urllib.request.urlopen("http://127.0.0.1:8420/ask")\n', "sample.py", True),
    ('import urllib.request\ndef go():\n    headers = {"X-Gateway-Token": "abc123"}\n'
     '    urllib.request.urlopen("http://127.0.0.1:8420/ask")\n', "sample.py", True),
    ('from agent_py_agent.cli.gateway_client_headers import gateway_script_headers\ndef go():\n'
     '    gateway_script_headers()\n    urllib.request.urlopen("http://127.0.0.1:8420/ask")\n', "sample.py", False),
    ('import urllib.request\n'
     'from agent_py_agent.agent.gateway_parts.client_credentials import gateway_client_credentials\n'
     'def go(agent):\n    gateway_client_credentials(agent).headers()\n'
     '    urllib.request.urlopen("http://127.0.0.1:8420/ask")\n', "sample.py", False),
)


def test_gateway_script_credential_rule_self_check() -> None:
    """守卫规则合成样本自检：违规形态必须被标记，合规与公开路由必须通过。"""

    assert _gateway_address_variables(['GW="http://127.0.0.1:8420"']) == {"GW"}
    wrong = [sample for sample, name, flagged in GATEWAY_SCRIPT_SELF_CHECK_SAMPLES
             if bool(_shell_text_offenders(sample, name) or _python_text_offenders(sample, name)) is not flagged]
    assert not wrong, "自检样本判定与期望不符：" + repr(wrong[0][:60])
