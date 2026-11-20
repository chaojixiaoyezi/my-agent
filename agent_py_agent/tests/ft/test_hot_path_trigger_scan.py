# LLM: Conveyor M00 / R3-G2（C2.2）：热路径静态触发器棘轮。3.14t 关闭 GIL 后，热路径上的零参 super()、冻结类
#   dataclasses.replace、默认形 json.dumps、动态 getattr/hasattr 名、文本模式 open/read_text/write_text、带时区
#   datetime.now、type(name, bases, ns)、函数内 class、函数内 Thread(...)、object.__setattr__ 都会在每次调用时
#   打到类型缓存/类型锁。本门禁对 R3 §3 的 HTTP poll / model chunk / tool turn / scheduler tick 路径前缀做 AST
#   扫描，与 hot_path_allowlist.json 逐键比较：计数超出（含新键）即失败；计数低于记录（陈旧条目）也失败，迫使
#   转换模块在同一提交里收紧棘轮。键不含行号，无关改动不会搅动白名单。
# 模块用途: 提供 G2 扫描器、HOT_PREFIXES/NESTED_CLASS_ONLY_PATHS 两个前缀元组与门禁测试，并核验 W0 原语的导入边界。
from __future__ import annotations

import ast
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = Path(__file__).with_name("hot_path_allowlist.json")

# R3 §3（bench/scan_paths.py）的四条热路径前缀，相对 agent_py_agent/；agent/tooling/base.py 在基线不存在，已省略。
HOT_PREFIXES: tuple[str, ...] = (
    # HTTP poll
    "agent/gateway_parts/http_",
    "agent/gateway_parts/bounded_http_server.py",
    "agent/gateway_parts/client_",
    "agent/gateway_parts/status_",
    "agent/gateway_parts/request_event_",
    "agent/gateway_parts/control_service.py",
    "agent/gateway_parts/execution_control.py",
    "agent/gateway_parts/execution_target.py",
    "agent/gateway_parts/response_renderer.py",
    "agent/gateway_parts/request_entry_admission.py",
    "agent/gateway_parts/http_input_receipt.py",
    # model chunk
    "agent/backends/",
    "agent/agent_core/tool_model_generation.py",
    "agent/conversation/decision_model_call.py",
    "agent/contracts/model_call_ledger.py",
    # tool turn
    "agent/agent_core/tool_loop/",
    "agent/tooling/registry.py",
    "agent/agent_core/tool_",
    "agent/tooling/tool_",
    "agent/tooling/models.py",
    "agent/tooling/runtime_contracts.py",
    "agent/contracts/tool_approval.py",
    # scheduler tick
    "cli/gateway_loops.py",
    "agent/conversation/wake_",
    "agent/conversation/scheduler",
    "agent/conversation/thread_scheduler",
    "agent/agent_core/orchestration/background/",
    "agent/gateway_parts/request_worker.py",
    "agent/gateway_parts/request_execution.py",
)
# 只检查 nested_class 的精确文件路径（M13 追加 W16 的 13 个文件）；其余触发器不适用于这些文件。
NESTED_CLASS_ONLY_PATHS: tuple[str, ...] = ()
# R3 §3 原样列出、但基线 d9caa95af 下没有任何文件的调度前缀；前缀保留（日后出现文件即自动纳入扫描），只在前缀自检里豁免。
_R3_PREFIXES_WITHOUT_BASELINE_FILES = frozenset(("agent/conversation/scheduler", "agent/conversation/thread_scheduler"))

TRIGGERS: tuple[str, ...] = (
    "super0", "replace_on_fast_frozen", "json_dumps_default", "dyn_getattr", "dyn_hasattr", "text_open",
    "read_write_text", "aware_now", "type3", "nested_class", "thread_ctor", "object_setattr",
)
_REASON = re.compile(
    r"^(baseline-d9caa95af pending M\d{2}|baseline-d9caa95af outside conveyor scope"
    r"|cold-path: .+|cooperative-mro: .+|slots-incompatible: .+)$"
)
_TEXT_OPEN_CALLEES = frozenset(("open", "io.open", "builtins.open", "os.fdopen", "codecs.open"))
_MODE_CHARACTERS = frozenset("rwxabt+")
_DUMPS_SEPARATORS = ((",", ":"), (", ", ": "))
_W0_PRIMITIVES = (
    "agent/common/hot_json.py",
    "agent/common/fast_time.py",
    "agent/common/fast_dataclass.py",
    "agent/common/direct_socket_io.py",
    "agent/common/process_spawn.py",
    "agent/concurrency/lifo_work_queue.py",
    "agent/concurrency/work_item_pool.py",
)
_W0_FORBIDDEN_IMPORTS = ("agent_py_agent.agent.gateway_parts", "agent_py_agent.agent.agent_core", "agent_py_agent.cli")


# 函数用途: 收集模块内全部 import 别名（含函数内 import），映射到规范点分名。
def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


# 函数用途: 把 Name/Attribute 链还原成点分名；其它表达式返回 None。
def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


# 函数用途: 判断表达式是否为字符串常量。
def _is_str_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


# 函数用途: 默认形 json.dumps：只有一个位置参数，关键字只含 ensure_ascii/sort_keys/标准两种 separators。
def _default_shaped_dumps(node: ast.Call) -> bool:
    if len(node.args) != 1 or isinstance(node.args[0], ast.Starred):
        return False
    for keyword in node.keywords:
        if keyword.arg in ("ensure_ascii", "sort_keys"):
            continue
        if keyword.arg == "separators":
            try:
                value = ast.literal_eval(keyword.value)
            except Exception:  # noqa: BLE001 - 非字面量分隔符不是默认形
                return False
            if isinstance(value, (tuple, list)) and tuple(value) in _DUMPS_SEPARATORS:
                continue
        return False
    return True


# 函数用途: 取 open 类调用的 mode 实参（位置下标或 mode= 关键字）；缺省返回 None。
def _mode_argument(node: ast.Call, position: int) -> ast.AST | None:
    if len(node.args) > position:
        return node.args[position]
    return next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), None)


# 类用途: 单文件 AST 访问器，按 Python 风格限定名（Cls.method、outer.<locals>.inner、<module>）累计触发器次数。
class _TriggerScanner(ast.NodeVisitor):
    def __init__(self, tree: ast.AST, triggers: frozenset[str]) -> None:
        self.counts: Counter[tuple[str, str]] = Counter()
        self._aliases = _import_aliases(tree)
        self._triggers = triggers
        self._scopes: list[tuple[str, str]] = []
        self._lambda_depth = 0

    def _qualname(self) -> str:
        parts: list[str] = []
        for name, kind in self._scopes:
            parts.append(name)
            if kind == "function":
                parts.append("<locals>")
        if parts and parts[-1] == "<locals>":
            parts.pop()
        return ".".join(parts) or "<module>"

    def _in_function(self) -> bool:
        return self._lambda_depth > 0 or any(kind == "function" for _name, kind in self._scopes)

    def _add(self, trigger: str) -> None:
        if trigger in self._triggers:
            self.counts[(self._qualname(), trigger)] += 1

    def _canonical(self, node: ast.AST) -> str | None:
        dotted = _dotted(node)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        target = self._aliases.get(head, head)
        return f"{target}.{rest}" if rest else target

    def _visit_signature(self, args: ast.arguments) -> None:
        for default in [*args.defaults, *(item for item in args.kw_defaults if item is not None)]:
            self.visit(default)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast 回调名
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_signature(node.args)
        self._scopes.append((node.name, "function"))
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815 - ast 回调名

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast 回调名
        self._visit_signature(node.args)
        self._lambda_depth += 1
        self.visit(node.body)
        self._lambda_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast 回调名
        for expression in [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]:
            self.visit(expression)
        if self._in_function():
            self._add("nested_class")
        self._scopes.append((node.name, "class"))
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast 回调名
        func = node.func
        name = self._canonical(func)
        starred = any(isinstance(argument, ast.Starred) for argument in node.args)
        if name == "super" and not node.args and not node.keywords:
            self._add("super0")
        elif name == "dataclasses.replace":
            self._add("replace_on_fast_frozen")
        elif name == "json.dumps":
            if _default_shaped_dumps(node):
                self._add("json_dumps_default")
        elif name in ("getattr", "hasattr"):
            if len(node.args) >= 2 and not starred and not _is_str_constant(node.args[1]):
                self._add(f"dyn_{name}")
        elif name in _TEXT_OPEN_CALLEES:
            mode = _mode_argument(node, 1)
            if not (_is_str_constant(mode) and "b" in mode.value):
                self._add("text_open")
        elif name == "type":
            if len(node.args) == 3 and not starred:
                self._add("type3")
        elif name == "threading.Thread":
            if self._in_function():
                self._add("thread_ctor")
        elif name == "object.__setattr__":
            self._add("object_setattr")
        elif isinstance(func, ast.Attribute):
            if func.attr in ("read_text", "write_text"):
                self._add("read_write_text")
            elif func.attr == "open" and name not in ("os.open",):
                # Path.open 等方法：只认显式文本模式串，避免把 opener.open(request)、zip.open(name) 误计。
                mode = _mode_argument(node, 0)
                if _is_str_constant(mode) and mode.value and set(mode.value) <= _MODE_CHARACTERS \
                        and "b" not in mode.value:
                    self._add("text_open")
            elif func.attr == "now" and self._canonical(func.value) == "datetime.datetime":
                if node.args or any(keyword.arg == "tz" for keyword in node.keywords):
                    self._add("aware_now")
        self.generic_visit(node)


# 函数用途: 扫描一段源码，返回 {(限定名, 触发器): 次数}。
def scan_source(source: bytes | str, triggers: frozenset[str] = frozenset(TRIGGERS)) -> Counter:
    tree = ast.parse(source)
    scanner = _TriggerScanner(tree, triggers)
    scanner.visit(tree)
    return scanner.counts


# 函数用途: 列出 agent/ 与 cli/ 下的全部 .py 相对路径（posix 形式、排序稳定）。
def _package_files(package_root: Path) -> list[str]:
    files = []
    for top in ("agent", "cli"):
        for path in sorted((package_root / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                files.append(path.relative_to(package_root).as_posix())
    return files


# 函数用途: 按 HOT_PREFIXES（全部触发器）与 NESTED_CLASS_ONLY_PATHS（仅 nested_class）扫描，返回 {键: 次数}。
def scan_hot_paths(package_root: Path = PACKAGE_ROOT) -> dict[str, int]:
    observed: dict[str, int] = {}
    for relative in _package_files(package_root):
        if relative.startswith(HOT_PREFIXES):
            triggers = frozenset(TRIGGERS)
        elif relative in NESTED_CLASS_ONLY_PATHS:
            triggers = frozenset(("nested_class",))
        else:
            continue
        for (qualname, trigger), count in scan_source((package_root / relative).read_bytes(), triggers).items():
            observed[f"{relative}::{qualname}::{trigger}"] = count
    return observed


# 函数用途: 读取白名单 JSON（version 1）。
def _load_allowlist() -> dict:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def test_hot_prefixes_each_match_files():
    files = _package_files(PACKAGE_ROOT)
    missing = {prefix for prefix in HOT_PREFIXES if not any(path.startswith(prefix) for path in files)}
    assert missing <= _R3_PREFIXES_WITHOUT_BASELINE_FILES, sorted(missing - _R3_PREFIXES_WITHOUT_BASELINE_FILES)
    assert all(path in files for path in NESTED_CLASS_ONLY_PATHS)


def test_allowlist_is_canonical_with_well_formed_reasons():
    allowlist = _load_allowlist()
    assert allowlist.get("version") == 1 and set(allowlist) == {"version", "entries"}
    text = ALLOWLIST_PATH.read_text(encoding="utf-8")
    assert text == json.dumps(allowlist, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    for key, entry in allowlist["entries"].items():
        path, qualname, trigger = key.split("::")
        assert path and qualname and trigger in TRIGGERS, key
        assert set(entry) == {"count", "reason"}, key
        assert isinstance(entry["count"], int) and entry["count"] > 0, key
        assert _REASON.match(entry["reason"]), (key, entry["reason"])


def test_no_new_or_increased_hot_path_triggers():
    allowed = {key: entry["count"] for key, entry in _load_allowlist()["entries"].items()}
    observed = scan_hot_paths()
    exceeded = {key: (count, allowed.get(key, 0)) for key, count in sorted(observed.items())
                if count > allowed.get(key, 0)}
    assert not exceeded, "热路径新增触发器（键: (实测, 白名单)）——按 C3 转换或带理由登记：\n" + "\n".join(
        f"  {key}: {pair}" for key, pair in exceeded.items())


def test_no_stale_allowlist_entries():
    allowed = {key: entry["count"] for key, entry in _load_allowlist()["entries"].items()}
    observed = scan_hot_paths()
    stale = {key: (observed.get(key, 0), count) for key, count in sorted(allowed.items())
             if observed.get(key, 0) < count}
    assert not stale, "白名单陈旧（键: (实测, 白名单)）——同一提交里收紧计数或删除条目：\n" + "\n".join(
        f"  {key}: {pair}" for key, pair in stale.items())


def test_scanner_counts_each_trigger_and_ignores_safe_forms():
    source = '''
import json, threading, dataclasses as dc, datetime as dt
from dataclasses import replace
from datetime import datetime, timezone
from json import dumps as jd
from threading import Thread

class Model(Base):
    def method(self, name, path):
        super().method()
        super(Model, self).method()
        replace(self, a=1)
        dc.replace(self, a=1)
        json.dumps(self)
        jd(self, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        json.dumps(self, indent=2)
        json.dumps(self, default=str)
        json.dumps(self, separators=(",", ": "))
        getattr(self, name)
        getattr(self, "literal")
        hasattr(self, name)
        open(path)
        open(path, "rb")
        open(path, mode="w")
        path.open("a", encoding="utf-8")
        path.open("rb")
        opener.open(request)
        path.read_text()
        path.write_text("x")
        datetime.now(timezone.utc)
        dt.datetime.now(tz=timezone.utc)
        datetime.now()
        type("X", (), {})
        type(self)
        Thread(target=print)
        object.__setattr__(self, "a", 1)

        class Inner:
            pass

        return lambda: threading.Thread(target=print)

T = threading.Thread(target=print)
'''
    counts = scan_source(source)
    assert dict(counts) == {
        ("Model.method", "super0"): 1,
        ("Model.method", "replace_on_fast_frozen"): 2,
        ("Model.method", "json_dumps_default"): 2,
        ("Model.method", "dyn_getattr"): 1,
        ("Model.method", "dyn_hasattr"): 1,
        ("Model.method", "text_open"): 3,
        ("Model.method", "read_write_text"): 2,
        ("Model.method", "aware_now"): 2,
        ("Model.method", "type3"): 1,
        ("Model.method", "thread_ctor"): 2,
        ("Model.method", "object_setattr"): 1,
        ("Model.method", "nested_class"): 1,
    }
    nested = scan_source("def outer():\n    def inner():\n        class C:\n            pass\n",
                         frozenset(("nested_class",)))
    assert dict(nested) == {("outer.<locals>.inner", "nested_class"): 1}


def test_w0_primitives_import_no_runtime_layers():
    for relative in _W0_PRIMITIVES:
        module = "agent_py_agent." + relative[:-3].replace("/", ".")
        package = module.rpartition(".")[0]
        tree = ast.parse((PACKAGE_ROOT / relative).read_bytes())
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                        if node.level else (node.module or ""))
                targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            for target in targets:
                if target.split(".")[0] in ("agent", "cli"):
                    target = "agent_py_agent." + target
                assert not any(target == prefix or target.startswith(prefix + ".")
                               for prefix in _W0_FORBIDDEN_IMPORTS), (relative, target)
