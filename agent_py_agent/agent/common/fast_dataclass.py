"""冻结槽位数据类的快速构造：@fast_frozen 放在 @dataclass(frozen=True, slots=True) 之上。

dataclasses 为冻结类生成的 __init__ 逐字段调用 object.__setattr__(self, name, value)，每次都要在
类型上查找属性；python3.14t 关闭 GIL、上千个冻结类在 128 线程里同时构造时，类型缓存的哈希带冲突让
这些查找落到全局类型锁上（R3 §2.1、§4.1）。fast_frozen 按 dataclasses 的同一规则重新生成 __init__，
只把每个字段的赋值换成类创建时一次性取到的成员描述符 __set__；签名、默认值、default_factory、kw_only、
InitVar、__post_init__ 调用以及 eq/hash/repr/pickle/FrozenInstanceError 全部不变（差分测试钉住）。
同时安装 __replace__（copy.replace 协议）与 __fast_replace__；fast_replace 对未转换的类回落到
dataclasses.replace。check_slots_compat 列出把一个类改成 slots=True 前必须处理的阻碍，空列表才可转换。
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import inspect
import pickle
import sys
import textwrap
import threading
import types
from collections.abc import Callable

_FIELD = dataclasses._FIELD
_FIELD_INITVAR = dataclasses._FIELD_INITVAR
_HAS_DEFAULT_FACTORY = dataclasses._HAS_DEFAULT_FACTORY
_MISSING = dataclasses.MISSING

_LOCK = threading.Lock()
# 本进程内已转换的类（只读视图；每次转换整体替换元组）。
CONVERTED_CLASSES: tuple[type, ...] = ()
# 已转换类 -> 其 __fast_replace__；fast_replace 按 type(obj) 精确命中，子类不继承（回落 dataclasses.replace）。
_REPLACERS: dict[type, Callable[..., object]] = {}


# LLM: 沿 MRO 找第一个定义该名字的类；必须是成员描述符（槽位），否则说明它不是槽位字段。
#   返回的是描述符的 __set__（method-wrapper），调用 setter(obj, value) 直接写槽位，不经 __setattr__。
# 函数用途: 取得某个槽位字段的写入函数，替代 __post_init__ 里的 object.__setattr__(self, "x", v)。
def slot_setter(cls: type, name: str) -> Callable[[object, object], None]:
    for klass in getattr(cls, "__mro__", ()):
        descriptor = klass.__dict__.get(name)
        if descriptor is None:
            continue
        if isinstance(descriptor, types.MemberDescriptorType):
            return descriptor.__set__
        break
    raise TypeError(f"{getattr(cls, '__qualname__', cls)!s}.{name} is not a slot field")


# LLM: dataclasses 生成的 __init__ 由 exec 在 "<string>" 里以 __create_fn__ 闭包产出，且 __qualname__ 被改成
#   “类名.__init__”；类体里手写的 __init__ 文件名与限定名都不同。fast_frozen 生成的 __init__ 也满足同一形状。
# 函数用途: 判断一个 __init__ 是否为 dataclasses（或本模块）生成的。
def _is_generated_init(cls: type, init: object) -> bool:
    code = getattr(init, "__code__", None)
    return (code is not None and code.co_filename == "<string>" and code.co_name == "__init__"
            and getattr(code, "co_qualname", "__create_fn__.").startswith("__create_fn__.")
            and getattr(init, "__qualname__", "") == f"{cls.__qualname__}.__init__")


# 函数用途: 返回参与 __init__ 的字段（普通字段与 InitVar，按定义顺序，基类字段在前），与 dataclasses 一致。
def _init_fields(cls: type) -> list[dataclasses.Field]:
    return [f for f in cls.__dataclass_fields__.values() if f._field_type in (_FIELD, _FIELD_INITVAR)]


# LLM: 与 dataclasses.__create_fn__ 同一做法：把要引用的对象作为外层函数参数传入（闭包单元），在类所在模块的
#   全局字典里 exec，得到的函数 __globals__/__module__ 与 dataclasses 生成的一致。
# 函数用途: 编译一段只定义一个内层函数的源码并返回该函数。
def _create_fn(cls: type, name: str, source_lines: list[str], namespace: dict[str, object]) -> Callable:
    body = "\n".join(f" {line}" for line in source_lines)
    text = f"def __create_fn__({', '.join(namespace)}):\n{body}\n return {name}"
    module = sys.modules.get(cls.__module__)
    scope: dict[str, object] = {}
    exec(text, module.__dict__ if module is not None else {}, scope)
    fn = scope["__create_fn__"](**namespace)
    fn.__qualname__ = f"{cls.__qualname__}.{name}"
    return fn


# LLM: 参数表、默认值对象与赋值顺序逐条对齐 dataclasses._init_fn/_field_init（slots=True、frozen=True 分支）：
#   有 default_factory 且 init 的字段用 _HAS_DEFAULT_FACTORY 哨兵占位、为哨兵时调用工厂；init=False 的字段
#   用工厂或默认值初始化；InitVar 只作参数并原样传给 __post_init__；字段名为 self 时自身参数改名。
# 函数用途: 生成以成员描述符 __set__ 直接写槽位的 __init__。
def _build_init(cls: type) -> Callable:
    fields = _init_fields(cls)
    self_name = "__dataclass_self__" if "self" in cls.__dataclass_fields__ else "self"
    namespace: dict[str, object] = {"__fast_HAS_DEFAULT_FACTORY__": _HAS_DEFAULT_FACTORY}

    def parameter(f: dataclasses.Field) -> str:
        if f.default is not _MISSING:
            namespace[f"__fast_dflt_{f.name}__"] = f.default
            return f"{f.name}=__fast_dflt_{f.name}__"
        if f.default_factory is not _MISSING:
            return f"{f.name}=__fast_HAS_DEFAULT_FACTORY__"
        return f.name

    parameters = [self_name, *(parameter(f) for f in fields if f.init and not f.kw_only)]
    keyword_only = [parameter(f) for f in fields if f.init and f.kw_only]
    if keyword_only:
        parameters += ["*", *keyword_only]
    body: list[str] = []
    for f in fields:
        if f._field_type is _FIELD_INITVAR:
            continue
        if f.default_factory is not _MISSING:
            namespace[f"__fast_factory_{f.name}__"] = f.default_factory
            value = (f"__fast_factory_{f.name}__() if {f.name} is __fast_HAS_DEFAULT_FACTORY__ else {f.name}"
                     if f.init else f"__fast_factory_{f.name}__()")
        elif f.init:
            value = f.name
        elif f.default is not _MISSING:
            namespace[f"__fast_dflt_{f.name}__"] = f.default
            value = f"__fast_dflt_{f.name}__"
        else:
            continue
        namespace[f"__fast_set_{f.name}__"] = slot_setter(cls, f.name)
        body.append(f" __fast_set_{f.name}__({self_name}, {value})")
    if hasattr(cls, "__post_init__"):
        initvars = ", ".join(f.name for f in fields if f._field_type is _FIELD_INITVAR)
        body.append(f" {self_name}.__post_init__({initvars})")
    fn = _create_fn(cls, "__init__", [f"def __init__({', '.join(parameters)}):", *(body or [" pass"])], namespace)
    original = cls.__dict__["__init__"]
    annotate = getattr(original, "__annotate__", None)
    if annotate is not None:
        fn.__annotate__ = annotate
    else:
        fn.__annotations__ = dict(original.__annotations__)
    return fn


# LLM: 只对本类实例走快路径；init=False 字段出现在改动里、或缺少无默认值的 InitVar 时，直接交给
#   dataclasses.replace，让它抛出与当前解释器完全相同的异常（3.12 为 ValueError，3.13+ 为 TypeError）。
#   其余情况按 dataclasses.replace 的规则用 self.<字段> 补齐缺省的 init 字段与带默认值的 InitVar，
#   再以 object.__new__ + 快速 __init__ 构造（类未改 __new__、元类未改 __call__ 时与 cls(**changes) 等价）。
# 函数用途: 生成 __replace__/__fast_replace__。
def _build_replace(cls: type, init_fn: Callable) -> Callable:
    fields = _init_fields(cls)
    direct = cls.__new__ is object.__new__ and type(cls).__call__ is type.__call__
    namespace: dict[str, object] = {
        "__fast_cls__": cls,
        "__fast_type__": type,
        "__fast_init__": init_fn,
        "__fast_new__": object.__new__,
        "__fast_dc_replace__": dataclasses.replace,
        "__fast_not_init__": frozenset(f.name for f in fields if not f.init),
        "__fast_required__": frozenset(f.name for f in fields
                                       if f._field_type is _FIELD_INITVAR and f.default is _MISSING),
    }
    lines = [
        "def __replace__(self, /, **changes):",
        " if (__fast_type__(self) is not __fast_cls__ or not __fast_not_init__.isdisjoint(changes)",
        "         or not __fast_required__ <= changes.keys()):",
        "  return __fast_dc_replace__(self, **changes)",
    ]
    for f in fields:
        if f.init:
            lines.append(f" if {f.name!r} not in changes:")
            lines.append(f"  changes[{f.name!r}] = self.{f.name}")
    if direct:
        lines += [" __fast_obj__ = __fast_new__(__fast_cls__)", " __fast_init__(__fast_obj__, **changes)",
                  " return __fast_obj__"]
    else:
        lines.append(" return __fast_cls__(**changes)")
    return _create_fn(cls, "__replace__", lines, namespace)


# 函数用途: 校验 fast_frozen 的前提：冻结、slots=True、init=True 且 __init__ 由 dataclasses 生成。
def _require_convertible(cls: object) -> None:
    params = getattr(cls, "__dataclass_params__", None) if isinstance(cls, type) else None
    if params is None or "__dataclass_fields__" not in getattr(cls, "__dict__", {}):
        raise TypeError(f"fast_frozen expects a dataclass type, got {cls!r}")
    if not params.frozen or not params.init:
        raise TypeError(f"fast_frozen requires @dataclass(frozen=True, init=True) on {cls.__qualname__}")
    if "__slots__" not in cls.__dict__:
        raise TypeError(f"fast_frozen requires @dataclass(slots=True) on {cls.__qualname__}")
    if not _is_generated_init(cls, cls.__dict__.get("__init__")):
        raise TypeError(f"fast_frozen cannot replace a custom __init__ on {cls.__qualname__}")


# LLM: 类在导入时转换一次：就地替换 __init__ 并装上 __replace__/__fast_replace__，类对象身份不变；重复装饰直接返回。
# 函数用途: 类装饰器，放在 @dataclass(frozen=True, slots=True) 之上。
def fast_frozen(cls: type) -> type:
    global CONVERTED_CLASSES
    with _LOCK:
        if cls in _REPLACERS:
            return cls
        _require_convertible(cls)
        init_fn = _build_init(cls)
        replace_fn = _build_replace(cls, init_fn)
        cls.__init__ = init_fn
        cls.__replace__ = replace_fn
        cls.__fast_replace__ = replace_fn
        _REPLACERS[cls] = replace_fn
        CONVERTED_CLASSES = (*CONVERTED_CLASSES, cls)
    return cls


# 函数用途: 已转换类走 __fast_replace__，其余对象（含已转换类的未转换子类）回落到 dataclasses.replace。
def fast_replace(obj: object, /, **changes: object) -> object:
    replacer = _REPLACERS.get(type(obj))
    if replacer is None:
        return dataclasses.replace(obj, **changes)
    return replacer(obj, **changes)


# 函数用途: 取一个类在 MRO 上声明过的全部槽位名。
def _slot_names(cls: type) -> set[str]:
    names: set[str] = set()
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        names.update((slots,) if isinstance(slots, str) else slots)
    return names


# 函数用途: 按 “模块.限定名” 解析回类对象（pickle 按引用保存类时走同一路径）。
def _resolve_qualname(cls: type) -> object:
    target: object = sys.modules.get(cls.__module__)
    for part in cls.__qualname__.split("."):
        target = getattr(target, part, None)
    return target


# 函数用途: 当所有 init 参数都有默认值时构造一个探针实例；构造失败或无法无参构造时返回 None。
def _probe_instance(cls: type) -> object | None:
    for f in _init_fields(cls):
        if f.init and f.default is _MISSING and f.default_factory is _MISSING:
            return None
    try:
        return cls()
    except Exception:  # noqa: BLE001 - 探针失败只表示无法做往返检查
        return None


# 函数用途: 解析类与其模块的源码；取不到时返回 None。
def _parse_source(target: object) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(target)))
    except (OSError, TypeError, SyntaxError):
        return None


# 函数用途: 判断一个装饰器表达式是否为 dataclass / dataclasses.dataclass（可带参数）。
def _is_dataclass_decorator(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id == "dataclass") or (
        isinstance(target, ast.Attribute) and target.attr == "dataclass")


# LLM: 只做静态与只读运行时检查，不修改类：报告冻结/init 前提、类体内手写 __init__、无 __slots__ 或变长的基类、
#   cached_property、__dict__/vars() 使用、零参 super()、对非字段常量名的 object.__setattr__/setattr、
#   早于 @dataclass 执行的类装饰器、模块用到 weakref 而转换后不保留 __weakref__、以及 pickle 按引用解析
#   或探针实例往返失败。取不到源码时如实报告“无法核验”，由调用方决定是否保留原类。
# 函数用途: 返回把类改成 slots=True + fast_frozen 前必须处理的问题列表；空列表表示可安全转换。
def check_slots_compat(cls: type) -> list[str]:
    params = getattr(cls, "__dataclass_params__", None) if isinstance(cls, type) else None
    if params is None or not params.frozen or "__dataclass_fields__" not in cls.__dict__:
        return [f"{getattr(cls, '__qualname__', cls)!s}: not a frozen dataclass"]
    report: list[str] = []
    if not params.init:
        report.append("init=False: there is no generated __init__ to replace")
    init = cls.__dict__.get("__init__")
    if init is not None and not _is_generated_init(cls, init):
        report.append("custom __init__ defined in the class body")
    for base in cls.__mro__[1:]:
        if base is object:
            continue
        if base.__itemsize__:
            report.append(f"variable-size base {base.__qualname__} cannot carry field __slots__")
        elif "__dataclass_fields__" in base.__dict__ and "__slots__" not in base.__dict__:
            report.append(f"dataclass base {base.__qualname__} has no __slots__ (its fields live in __dict__)")
    for name, value in cls.__dict__.items():
        if isinstance(value, functools.cached_property):
            report.append(f"cached_property {name} needs an instance __dict__")
    field_names = set(cls.__dataclass_fields__)
    class_tree = _parse_source(cls)
    if class_tree is None:
        report.append("source unavailable: cannot verify __dict__/vars()/super()/attribute assignments")
    else:
        class_node = next((node for node in ast.walk(class_tree) if isinstance(node, ast.ClassDef)), None)
        if class_node is not None:
            decorators = class_node.decorator_list
            position = next((index for index, node in enumerate(decorators) if _is_dataclass_decorator(node)), None)
            for node in decorators[position + 1:] if position is not None else ():
                report.append(f"decorator {ast.unparse(node)} runs before @dataclass and captures the pre-slots class")
        for node in ast.walk(class_tree):
            if isinstance(node, ast.Attribute) and node.attr == "__dict__":
                report.append("uses __dict__")
            elif isinstance(node, ast.Call):
                func = node.func
                callee = func.id if isinstance(func, ast.Name) else (
                    f"{func.value.id}.{func.attr}" if isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name) else "")
                if callee == "vars":
                    report.append("uses vars()")
                elif callee == "super" and not node.args:
                    report.append("zero-argument super() (convert per C3 A2 or keep the class)")
                elif callee in ("object.__setattr__", "setattr") and len(node.args) >= 2:
                    name = node.args[1]
                    if isinstance(name, ast.Constant) and isinstance(name.value, str) \
                            and name.value not in field_names:
                        report.append(f"assigns non-field attribute {name.value!r}")
    module = sys.modules.get(cls.__module__)
    module_tree = _parse_source(module) if module is not None else None
    if module_tree is not None and any(
            (isinstance(node, ast.Name) and node.id == "weakref")
            or (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "weakref")
            or (isinstance(node, ast.alias) and node.name.split(".")[0] == "weakref")
            or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "weakref")
            for node in ast.walk(module_tree)):
        if "__slots__" in cls.__dict__ and "__weakref__" not in _slot_names(cls):
            report.append("module uses weakref but the class has no __weakref__ slot (use weakref_slot=True)")
        elif "__slots__" not in cls.__dict__:
            report.append("module uses weakref: convert with weakref_slot=True to keep weak references working")
    if "<locals>" in cls.__qualname__ or _resolve_qualname(cls) is not cls:
        report.append("pickle: class is not importable by module and qualname")
    else:
        probe = _probe_instance(cls)
        if probe is not None:
            try:
                restored = pickle.loads(pickle.dumps(probe))
            except Exception as exc:  # noqa: BLE001 - 报告任何往返失败
                report.append(f"pickle round trip failed: {type(exc).__name__}: {exc}")
            else:
                names = [f.name for f in dataclasses.fields(probe)]
                if [getattr(restored, name) for name in names] != [getattr(probe, name) for name in names]:
                    report.append("pickle round trip changed field values")
    return list(dict.fromkeys(report))


__all__ = ["CONVERTED_CLASSES", "check_slots_compat", "fast_frozen", "fast_replace", "slot_setter"]
