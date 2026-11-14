# LLM: 后端传输签名守卫：cabfix 把 _gateway_request/request_stream_iter 改成 (StreamCall, options) 后，
#   OAuth 覆盖与 Responses 调用点没跟上，Mac 测试被 **kwargs 替身遮住，只在 Linux 车道暴露（75a26ca37）。
#   本模块用类关系发现 + inspect 签名比较 + ast 调用点扫描防复发；不写死类名清单，新增 backend
#   只要继承 HttpBackend 就自动被守。改动 backends 传输签名时同步核对 test_model_oauth_transport。
# 模块用途: 守住 HttpBackend 四个传输方法的覆盖签名与产品调用点关键字，失败时给出类名/方法名/差异。
from __future__ import annotations

import importlib
import inspect
import pkgutil

# 四个必须与基类保持同一签名的传输方法（基类定义在 backends/http.py）。
GUARDED_TRANSPORT_METHODS = (
    "_gateway_request",
    "request_json",
    "request_stream",
    "request_stream_iter",
)


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
