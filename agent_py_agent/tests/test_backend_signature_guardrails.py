# LLM: 后端传输签名守卫：cabfix 把 _gateway_request/request_stream_iter 改成 (StreamCall, options) 后，
#   OAuth 覆盖与 Responses 调用点没跟上，Mac 测试被 **kwargs 替身遮住，只在 Linux 车道暴露（75a26ca37）。
#   本模块用类关系发现 + inspect 签名比较 + ast 调用点扫描防复发；不写死类名清单，新增 backend
#   只要继承 HttpBackend 就自动被守。改动 backends 传输签名时同步核对 test_model_oauth_transport。
# 模块用途: 守住 HttpBackend 四个传输方法的覆盖签名与产品调用点关键字，失败时给出类名/方法名/差异。
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

# 四个必须与基类保持同一签名的传输方法（基类定义在 backends/http.py）。
GUARDED_TRANSPORT_METHODS = (
    "_gateway_request",
    "request_json",
    "request_stream",
    "request_stream_iter",
)
# 调用点只扫描agent/backends模块；包根用于范围限定和诊断路径，CLI同名方法不属于该合同。
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


# LLM: 类发现必须走真实导入后的类关系，不能写死类名：新增 backend 只要继承 HttpBackend 就自动被守；
#   导入失败会让守卫直接报错而不是静默漏检，因此不做 try/except 吞异常。
# 函数用途: 导入 backends 包下所有模块，返回全部 HttpBackend 子类（不含基类自身），按限定名排序。
def _iter_http_backend_subclasses() -> list[type]:
    package = importlib.import_module("agent_py_agent.agent.backends")
    from agent_py_agent.agent.backends.http import HttpBackend

    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        _collect_backend_subclasses(module, HttpBackend, found)
    return [found[key] for key in sorted(found)]


# LLM: 单个模块的类收集独立成函数，把 for/if 两层留在原地，避免类发现函数超嵌套预算。
# 函数用途: 把模块里定义的所有 HttpBackend 子类（不含基类）按限定名收进 found。
def _collect_backend_subclasses(module: object, base: type, found: dict[str, type]) -> None:
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, base) and value is not base:
            found[f"{value.__module__}.{value.__qualname__}"] = value


# LLM: 参数指纹只比较"名字、种类、默认值有无"三件事：它们决定调用方兼容性；注解和文档不参与
#   （注解变化不影响 TypeError，不该误伤）。种类用 Parameter.kind.name 保留位置/关键字专用差异。
# 函数用途: 把一个可调用对象的签名投影成可比较的元组列表。
def _signature_fingerprint(func: object) -> list[tuple[str, str, bool]]:
    parameters = inspect.signature(func).parameters
    return [
        (parameter.name, parameter.kind.name, parameter.default is inspect.Parameter.empty)
        for parameter in parameters.values()
    ]


# LLM: 失败信息要能直接指出第几个参数、哪一侧不同；不做智能归并，逐位对比足够定位。
# 函数用途: 逐位对比基类与覆盖的参数指纹，拼出人类可读的差异描述。
def _describe_signature_diff(
    base: list[tuple[str, str, bool]],
    actual: list[tuple[str, str, bool]],
) -> str:
    parts: list[str] = []
    for index in range(max(len(base), len(actual))):
        base_item = base[index] if index < len(base) else None
        actual_item = actual[index] if index < len(actual) else None
        if base_item != actual_item:
            parts.append(f"第 {index + 1} 个参数: 基类={base_item} 覆盖={actual_item}")
    return "; ".join(parts) or "无差异"


# LLM: 覆盖可能来自 mixin（如 _OAuthMixin），所以按每个子类的 MRO 遍历所有祖先类的 __dict__，
#   而不是只看子类自己；同一祖先类只需检查一次（其 __dict__ 与访问路径无关）。
# 函数用途: 检查所有 HttpBackend 子类 MRO 上的四个传输方法覆盖是否与基类同签名。
def _iter_signature_violations() -> tuple[list[str], int]:
    from agent_py_agent.agent.backends.http import HttpBackend

    base = {
        name: _signature_fingerprint(getattr(HttpBackend, name))
        for name in GUARDED_TRANSPORT_METHODS
    }
    problems: list[str] = []
    checked_overrides = 0
    visited: set[type] = set()
    for subclass in _iter_http_backend_subclasses():
        checked_overrides += _check_subclass_mro(subclass, base, problems, visited)
    return problems, checked_overrides


# LLM: 子类 MRO 的遍历独立成函数：去重、跳过基类/object，再交给单类检查；主入口保持一层循环。
# 函数用途: 检查一个子类 MRO 上所有祖先类的覆盖，返回检查到的覆盖数量。
def _check_subclass_mro(
    subclass: type,
    base: dict[str, list[tuple[str, str, bool]]],
    problems: list[str],
    visited: set[type],
) -> int:
    from agent_py_agent.agent.backends.http import HttpBackend

    checked = 0
    for klass in subclass.__mro__:
        if klass in visited or klass is object or klass is HttpBackend:
            continue
        visited.add(klass)
        checked += _check_class_overrides(klass, base, problems)
    return checked


# LLM: 单个祖先类的四个覆盖检查独立成函数：签名读取失败按违规记账（不静默跳过），
#   逐方法比较与问题描述留在这一处，失败信息统一带类名与方法名。
# 函数用途: 检查一个类的四个传输方法覆盖，返回检查到的覆盖数量并把问题写进 problems。
def _check_class_overrides(
    klass: type,
    base: dict[str, list[tuple[str, str, bool]]],
    problems: list[str],
) -> int:
    checked = 0
    for name in GUARDED_TRANSPORT_METHODS:
        override = klass.__dict__.get(name)
        if override is None:
            continue
        checked += 1
        try:
            actual = _signature_fingerprint(override)
        except (TypeError, ValueError) as exc:
            problems.append(
                f"{klass.__module__}.{klass.__qualname__}.{name}: 无法读取签名（{exc}）"
            )
            continue
        if actual != base[name]:
            problems.append(
                f"{klass.__module__}.{klass.__qualname__}.{name}: "
                f"{_describe_signature_diff(base[name], actual)}"
            )
    return checked


def test_backend_transport_overrides_keep_base_signature() -> None:
    """覆盖四个传输方法的后端（含 mixin）必须与 HttpBackend 基类同参数名/同种类/同默认值。"""
    problems, checked_overrides = _iter_signature_violations()
    assert checked_overrides >= 2, (
        f"守卫必须至少检查到 OAuth 的两个覆盖，实际 {checked_overrides} 个（防类发现失效）"
    )
    assert not problems, (
        "后端传输方法覆盖签名与 HttpBackend 基类不一致（cabfix 类回归："
        "覆盖必须与基类同参数名/同种类/同默认值）:\n" + "\n".join(problems)
    )


# LLM: 调用点只认 self./super() 的直接调用：request_stream_lines 按被调方签名分派（(path, payload,
#   headers, first_event_timeout, deadline) 位置参数），不是关键字调用，不在此守卫范围。
# 函数用途: 从调用表达式取出目标描述（self / super()）；不是这四个方法或不是直接调用时返回 None。
def _transport_call_target(func: ast.expr) -> str | None:
    if not isinstance(func, ast.Attribute) or func.attr not in GUARDED_TRANSPORT_METHODS:
        return None
    value = func.value
    if isinstance(value, ast.Name) and value.id == "self":
        return "self"
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "super"
    ):
        return "super()"
    return None


# LLM: 关键字集合以基类签名为权威——覆盖签名已由上一个守卫保证与基类一致，无需按 MRO 逐个解析；
#   **kwargs 展开（arg 为 None）无法静态判定，跳过；基类方法带 VAR_KEYWORD 时整个方法放宽。
# 函数用途: ast扫描后端模块（tests/除外），返回四个传输方法调用里签名不认识的关键字位置。
def _scan_transport_call_sites() -> tuple[list[str], int]:
    from agent_py_agent.agent.backends.http import HttpBackend

    allowed = {
        name: _allowed_keywords(getattr(HttpBackend, name))
        for name in GUARDED_TRANSPORT_METHODS
    }
    violations: list[str] = []
    call_count = 0
    for path in sorted((_PACKAGE_ROOT / "agent" / "backends").rglob("*.py")):
        call_count += _scan_file_call_sites(path, allowed, violations)
    return violations, call_count


# LLM: 单个方法的可传关键字集合独立成函数：VAR_KEYWORD 出现时整方法放宽（返回 None），
#   位置或关键字参数名都允许作关键字传入；保持扫描函数只见数据、不见内层分支。
# 函数用途: 由方法签名算出允许的关键字集合；带 **kwargs 时返回 None 表示不设限。
def _allowed_keywords(method: object) -> set[str] | None:
    signature = inspect.signature(method)
    names: set[str] = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.add(parameter.name)
    return names


# LLM: 只接收agent/backends下的模块，排除其他客户端的同名方法；tests替身继续整文件跳过。
#   调用点关键字仍由真实基类签名检查，不因范围限定而放宽self/super的关键字约束。
# 函数用途: 扫描一个后端文件里的传输方法直接调用，返回发现的相关调用点数量。
def _scan_file_call_sites(
    path: Path,
    allowed: dict[str, set[str] | None],
    violations: list[str],
) -> int:
    relative_parts = path.relative_to(_PACKAGE_ROOT).parts
    if relative_parts[:2] != ("agent", "backends") or "tests" in relative_parts:
        return 0
    call_count = 0
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _transport_call_target(node.func)
        if target is None:
            continue
        call_count += 1
        permitted = allowed[node.func.attr]
        if permitted is None:
            continue
        violations.extend(_call_site_violations(node, target, permitted, path))
    return call_count


# LLM: 单个调用点的关键字检查独立成函数，让扫描函数保持"文件 → 调用"两层；违规信息带
#   相对仓库根的路径与行号，失败时能直接定位到调用方。
# 函数用途: 返回一个调用点里所有签名外关键字的位置描述（**kwargs 展开跳过）。
def _call_site_violations(
    node: ast.Call,
    target: str,
    permitted: set[str],
    path: Path,
) -> list[str]:
    found: list[str] = []
    for keyword in node.keywords:
        if keyword.arg is None:
            continue
        if keyword.arg not in permitted:
            found.append(
                f"{path.relative_to(_PACKAGE_ROOT.parent)}:{node.lineno} "
                f"{target}.{node.func.attr}({keyword.arg}=...) "
                f"不在签名参数 {sorted(permitted)} 里"
            )
    return found


def test_backend_transport_call_sites_use_known_keywords() -> None:
    """四个传输方法的产品调用点只允许传被调方签名里有的关键字（签名变更后调用方必须跟上）。"""
    violations, call_count = _scan_transport_call_sites()
    assert call_count >= 3, (
        f"调用点扫描至少应看到多个直接调用，实际 {call_count} 个（防扫描失效）"
    )
    assert not violations, (
        "后端传输方法调用点传了签名不认识的关键字（签名变更后调用方没跟上）:\n"
        + "\n".join(violations)
    )


# LLM: 守卫比较器自检：名字、参数种类、默认值有无任一变化都必须判为不同；防比较器本身写错导致
#   守卫长期绿灯。新增变化形态时只往这里加一对函数，不放宽真实守卫。
# 函数用途: 用成对样本函数验证参数指纹比较器能识别三类签名差异。
def test_signature_fingerprint_detects_name_kind_and_default_changes() -> None:
    def base(self, path, payload, *, options=None): ...

    def same(self, path, payload, *, options=None): ...

    def renamed(self, path, body, *, options=None): ...

    def keyword_only_changed(self, path, payload, options=None): ...

    def default_dropped(self, path, payload, *, options): ...

    fingerprint = _signature_fingerprint
    assert fingerprint(base) == fingerprint(same)
    assert fingerprint(base) != fingerprint(renamed)
    assert fingerprint(base) != fingerprint(keyword_only_changed)
    assert fingerprint(base) != fingerprint(default_dropped)


# LLM: 样本只写pytest临时树，关键字仍从真实HttpBackend签名提取，不修改产品文件。
# 函数用途: 为扫描器范围和错误识别准备一个独立源码文件。
def _call_site_fixture(tmp_path, monkeypatch, relative_path: str, source: str):
    from agent_py_agent.agent.backends.http import HttpBackend

    package_root = tmp_path / "agent_py_agent"
    monkeypatch.setitem(globals(), "_PACKAGE_ROOT", package_root)
    path = package_root / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    allowed = {
        name: _allowed_keywords(getattr(HttpBackend, name))
        for name in GUARDED_TRANSPORT_METHODS
    }
    return path, allowed


# LLM: 同名CLI/普通客户端不属于后端传输合同；tests替身也继续排除。
# 函数用途: 防止把非后端的conversation_id参数误报为HttpBackend签名错误。
@pytest.mark.parametrize("relative_path", [
    "cli/chat_parts/gateway_client.py",
    "agent/other_client.py",
    "tests/fake_backend.py",
    "agent/backends/tests/fake_backend.py",
])
def test_transport_call_scanner_ignores_non_backend_clients(tmp_path, monkeypatch, relative_path):
    source = (
        "class OtherClient:\n"
        "    def invoke(self):\n"
        "        self.request_json(conversation_id='thread')\n"
        "        super().request_json(conversation_id='thread')\n"
    )
    path, allowed = _call_site_fixture(tmp_path, monkeypatch, relative_path, source)
    violations = []
    assert _scan_file_call_sites(path, allowed, violations) == 0
    assert violations == []


# LLM: 四个方法和两种直接接收者都必须继续受守卫覆盖，目录限定不能变成全跳过。
# 函数用途: 验证后端模块的未知关键字仍被识别并附带准确调用位置。
@pytest.mark.parametrize("method", GUARDED_TRANSPORT_METHODS)
@pytest.mark.parametrize("target", ["self", "super()"])
def test_transport_call_scanner_keeps_backend_keyword_checks(tmp_path, monkeypatch, method, target):
    source = (
        "class ProbeBackend(HttpBackend):\n"
        "    def invoke(self):\n"
        f"        {target}.{method}(unknown_transport_keyword=True)\n"
    )
    path, allowed = _call_site_fixture(tmp_path, monkeypatch, "agent/backends/nested/probe.py", source)
    violations = []
    assert _scan_file_call_sites(path, allowed, violations) == 1
    assert len(violations) == 1
    assert "agent_py_agent/agent/backends/nested/probe.py:3" in violations[0]
    assert f"{target}.{method}(unknown_transport_keyword=...)" in violations[0]


# LLM: 合法参数不能被目录修复误伤，self与super调用仍分别计入覆盖数。
# 函数用途: 保留后端正常关键字调用的通过语义。
def test_transport_call_scanner_accepts_known_backend_keywords(tmp_path, monkeypatch):
    source = (
        "class ProbeBackend(HttpBackend):\n"
        "    def invoke(self):\n"
        "        self.request_json(path='/model', payload={}, headers=None)\n"
        "        super().request_json(path='/model', payload={}, total_deadline_seconds=1)\n"
    )
    path, allowed = _call_site_fixture(tmp_path, monkeypatch, "agent/backends/probe.py", source)
    violations = []
    assert _scan_file_call_sites(path, allowed, violations) == 2
    assert violations == []
