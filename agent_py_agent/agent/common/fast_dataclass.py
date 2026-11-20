"""冻结槽位数据类的快速构造：@fast_frozen 放在 @dataclass(frozen=True, slots=True) 之上。

dataclasses 为冻结类生成的 __init__ 逐字段调用 object.__setattr__(self, name, value)，每次都要在
类型上查找属性；python3.14t 关闭 GIL、上千个冻结类在 128 线程里同时构造时，类型缓存的哈希带冲突让
这些查找落到全局类型锁上（R3 §2.1、§4.1）。fast_frozen 按 dataclasses 的同一规则重新生成 __init__，
只把每个字段的赋值换成类创建时一次性取到的成员描述符 __set__；签名、默认值、default_factory、kw_only、
InitVar、__post_init__ 调用以及 eq/hash/repr/FrozenInstanceError 全部不变（差分测试钉住）；pickle/copy 状态
按转换前的布局输出，并能读回转换前后各版本写出的状态（见 _restore_state）。
同时安装 __replace__（copy.replace 协议）与 __fast_replace__；fast_replace 对未转换的类回落到
dataclasses.replace。check_slots_compat 列出把一个类改成 slots=True 前必须处理的阻碍（含包内调用方对实例
__dict__/弱引用的依赖），空列表才可转换；它只在转换时调用，fast_frozen 与请求路径从不调用。
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib.util
import inspect
import os
import pickle
import re
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
_SCAN_LOCK = threading.Lock()
# 包根目录 -> (调用点列表, 模块名 -> (返回注解名表, import 表) 或 None)；转换前检查对同一包根只扫描一次源码。
_CALLER_INDEX: dict[str, tuple[list, dict]] = {}
# 可能含调用点的源文件字样（vars(、__dict__、weakref）；一样都不含的文件不解析。
_CALLER_MARKERS = (b"vars(", b"__dict__", b"weakref")
# 字符串形式的返回注解里的标识符。
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 对实例取弱引用的 weakref 函数。
_WEAKREF_FUNCTIONS = frozenset(("ref", "proxy", "finalize"))
# 调用点种类 -> 报告文案。
_CALLER_MESSAGES = {
    "vars": "caller {site} applies vars() to a {cls} returned by {source}() (slots instances have no __dict__)",
    "dict": "caller {site} reads __dict__ of a {cls} returned by {source}() (slots instances have no __dict__)",
    "weakref": "caller {site} takes a weak reference to a {cls} returned by {source}() (convert with weakref_slot=True)",
}
# 标准库给冻结槽位数据类装的状态函数（按 fields() 顺序的值列表）；类体自己定义了其中任一个时 fast_frozen 两个都不换。
_STDLIB_STATE_FUNCTIONS = (dataclasses._dataclass_getstate, dataclasses._dataclass_setstate)
# 转换前就已是 @dataclass(frozen=True, slots=True) 的类（模块.限定名）。它们的历史读取方是标准库按位置配对的
# _dataclass_setstate，所以继续输出位置列表；新增这类转换时把类加进来。其余转换类转换前由实例 __dict__ 保存字段。
_POSITIONAL_STATE_CLASSES = frozenset((
    "agent_py_agent.agent.gateway_parts.request_event_hub.Gap",
    "agent_py_agent.agent.gateway_parts.request_event_hub.RequestEvent",
))


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
#   这两项回落条件在生成时按字段名展开成对本次调用 changes 的字面量成员判断（与 dataclasses.replace 自己的
#   f.name in changes 同一语义），没有这类字段就不生成。不要改回对类级共享集合调用 isdisjoint(changes) 或
#   <= changes.keys()：3.14t 关闭 GIL 时两者都会进入那个共享集合的临界区，所有线程替换同一个类时排在这把锁上，
#   128 线程吞吐只有 16 线程的约 0.2。其余情况按 dataclasses.replace 的规则用 self.<字段> 补齐缺省的 init 字段与
#   带默认值的 InitVar，再以 object.__new__ + 快速 __init__ 构造（类未改 __new__、元类未改 __call__ 时与
#   cls(**changes) 等价）。
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
    }
    fallback = ["__fast_type__(self) is not __fast_cls__"]
    fallback += [f"{f.name!r} in changes" for f in fields if not f.init]
    fallback += [f"{f.name!r} not in changes" for f in fields
                 if f._field_type is _FIELD_INITVAR and f.default is _MISSING]
    lines = [
        "def __replace__(self, /, **changes):",
        f" if {' or '.join(fallback)}:",
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


# LLM: 与转换前（无 slots）实例 __dict__ 同形：旧版本没有 __setstate__，读取方按 __dict__.update 原样恢复；本模块的
#   _restore_state 按名恢复。只含 dataclasses.fields() 的字段（与标准库一致），字段未赋值时同样抛 AttributeError。
# 函数用途: 转换类的 __getstate__（pickle 与 copy 共用）：返回 {字段名: 值}。
def _named_state(self) -> dict[str, object]:
    return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


# LLM: 标准库的 _dataclass_setstate 把状态当序列与 fields() 逐个配对：读到转换前的实例字典时，迭代出的是键，
#   槽位里会写进字段名字符串。这里按类型区分三种布局，互不歧义：dict 为转换前的实例字典，按字段名；list 为
#   标准库 _dataclass_getstate 输出的值列表，按 fields() 位置；(实例字典或 None, 槽位字典) 二元组为
#   object.__getstate__ 的槽位布局，合并后按字段名。缺少的字段用声明的 default/default_factory 补齐；未知字段名、
#   无默认值的缺失字段、值比字段多的列表和其它类型都抛 TypeError，且在写入任何槽位之前，不留下部分恢复的对象。
#   冻结类不经 __setattr__，与标准库一样用 object.__setattr__ 写槽位。
# 函数用途: 转换类的 __setstate__（pickle 与 copy 共用）：从命名、位置或槽位布局的状态恢复字段。
def _restore_state(self, state: object) -> None:
    fields = dataclasses.fields(self)
    owner = type(self).__qualname__
    if isinstance(state, dict):
        named = state
    elif type(state) is list:
        if len(state) > len(fields):
            raise TypeError(f"{owner}: pickle state has {len(state)} values for {len(fields)} fields")
        named = dict(zip([f.name for f in fields], state))
    elif type(state) is tuple and len(state) == 2 and all(part is None or isinstance(part, dict) for part in state):
        named = {**(state[0] or {}), **(state[1] or {})}
    else:
        raise TypeError(f"{owner}: unsupported pickle state {type(state).__name__} "
                        "(expected a field dict, a field list or an (instance dict, slot dict) pair)")
    unknown = named.keys() - {f.name for f in fields}
    if unknown:
        raise TypeError(f"{owner}: pickle state has unknown fields {sorted(unknown, key=repr)}")
    values = []
    for f in fields:
        if f.name in named:
            values.append(named[f.name])
        elif f.default is not _MISSING:
            values.append(f.default)
        elif f.default_factory is not _MISSING:
            values.append(f.default_factory())
        else:
            raise TypeError(f"{owner}: pickle state has no value for field {f.name!r}")
    for f, value in zip(fields, values):
        object.__setattr__(self, f.name, value)


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
#   状态函数仍是标准库那一对时换成 _restore_state（读任一布局）与按转换前布局的输出：_POSITIONAL_STATE_CLASSES
#   保留标准库的位置列表，其余为 _named_state。类体自定义的状态函数（含子类自己的）不动。
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
        if (cls.__dict__.get("__getstate__"), cls.__dict__.get("__setstate__")) == _STDLIB_STATE_FUNCTIONS:
            if f"{cls.__module__}.{cls.__qualname__}" not in _POSITIONAL_STATE_CLASSES:
                cls.__getstate__ = _named_state
            cls.__setstate__ = _restore_state
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


# 函数用途: 类所在顶层包的 (包名, 真实目录)；不是单目录的常规包（顶层模块、命名空间包等）时返回 None。
def _package_root(cls: type) -> tuple[str, str] | None:
    top = cls.__module__.partition(".")[0]
    paths = list(getattr(sys.modules.get(top), "__path__", None) or ())
    return (top, os.path.realpath(paths[0])) if len(paths) == 1 else None


# 函数用途: 读源文件字节；读不到返回 None。
def _read_source(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


# 函数用途: 解析源码字节；没有源码或语法错误时返回 None。
def _parse_bytes(source: bytes | None) -> ast.AST | None:
    if source is None:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


# 函数用途: 包内点分模块名对应的源文件（module.py 或 module/__init__.py）；不在包内或不存在时返回 None。
def _module_file(top: str, root: str, module: str) -> str | None:
    if module != top and not module.startswith(top + "."):
        return None
    base = os.path.join(root, *module.split(".")[1:])
    for path in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(path):
            return path
    return None


# 函数用途: 表达式里出现的名字（Name、属性名与字符串注解里的标识符），用来匹配返回注解。
def _annotation_names(node: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    for part in ast.walk(node):
        if isinstance(part, ast.Name):
            names.add(part.id)
        elif isinstance(part, ast.Attribute):
            names.add(part.attr)
        elif isinstance(part, ast.Constant) and isinstance(part.value, str):
            names.update(_IDENTIFIER.findall(part.value))
    return frozenset(names)


# 函数用途: ImportFrom 的绝对模块名；相对导入越出顶层包时返回 None。
def _import_base(package: str, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module or ""
    try:
        return importlib.util.resolve_name("." * node.level + (node.module or ""), package)
    except (ImportError, ValueError):
        return None


# 函数用途: 把 Name/Attribute 链还原成名字元组（f -> ("f",)，a.b.f -> ("a", "b", "f")）；其它表达式返回 None。
def _name_chain(node: ast.AST) -> tuple[str, ...] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


# 函数用途: 节点若对某个对象用 vars()、读 __dict__ 或经 weakref 取弱引用，返回 (被用的表达式, 种类)，否则 None。
def _instance_dict_use(node: ast.AST, imports: dict) -> tuple[ast.AST, str] | None:
    if isinstance(node, ast.Attribute):
        return (node.value, "dict") if node.attr == "__dict__" and isinstance(node.ctx, ast.Load) else None
    chain = _name_chain(node.func)
    if chain == ("vars",) and len(node.args) == 1:
        return node.args[0], "vars"
    if chain == ("getattr",) and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
            and node.args[1].value == "__dict__":
        return node.args[0], "dict"
    if chain is not None and imports.get(chain[0], ("", ""))[0] == "weakref":
        attr = imports[chain[0]][1]
        function = attr if len(chain) == 1 else chain[1] if len(chain) == 2 and attr is None else None
        if function in _WEAKREF_FUNCTIONS:
            return node.args[0], "weakref"
    return None


# LLM: 一次遍历同时收集三样：模块顶层 def/async def 的返回注解名；整个模块（含函数内）的 import 表，名字 ->
#   (绝对模块, 属性名)，属性名为 None 表示绑定的是模块本身；以及调用点。调用点按过程内名字绑定匹配：同一作用域
#   （模块、函数或类体）里 name = <调用>（含带注解赋值与海象赋值）记下被调用的名字链，对该 name 或对 <调用> 本身
#   用 vars()/__dict__/弱引用即一个调用点。同名多次绑定全部保留（保守）；不做跨作用域与数据流分析，参数、属性、
#   容器元素上的使用不追踪。遍历跳过 ctx 子节点（Load/Store 不含调用）。
# 函数用途: 分析一个模块，返回 (返回注解名表, import 表, 调用点 [(行号, 种类, 名字链)])。
def _analyze_module(tree: ast.AST, module: str, is_package: bool):
    package = module if is_package else module.rpartition(".")[0]
    returns = {node.name: _annotation_names(node.returns) for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None}
    imports: dict[str, tuple[str, str | None]] = {}
    scopes: list[tuple[dict[str, set[tuple[str, ...]]], list[ast.AST]]] = [({}, [])]
    stack = [(tree, scopes[0])]
    while stack:
        node, scope = stack.pop()
        kind = type(node)
        if kind is ast.Import:
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = (alias.name, None)
                else:
                    head = alias.name.partition(".")[0]
                    imports[head] = (head, None)
            continue
        if kind is ast.ImportFrom:
            base = _import_base(package, node)
            for alias in node.names if base is not None else ():
                if alias.name != "*":
                    imports[alias.asname or alias.name] = (base, alias.name)
            continue
        if kind in (ast.Assign, ast.AnnAssign, ast.NamedExpr):
            chain = _name_chain(node.value.func) if type(node.value) is ast.Call else None
            for target in (node.targets if kind is ast.Assign else (node.target,)) if chain is not None else ():
                if type(target) is ast.Name:
                    scope[0].setdefault(target.id, set()).add(chain)
        elif kind is ast.Attribute:
            if node.attr == "__dict__":
                scope[1].append(node)
        elif kind is ast.Call:
            func = type(node.func)
            if node.args and (func is ast.Name or (func is ast.Attribute and node.func.attr in _WEAKREF_FUNCTIONS)):
                scope[1].append(node)
        elif kind in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef):
            scope = ({}, [])
            scopes.append(scope)
        for name in node._fields:
            if name == "ctx":
                continue
            value = getattr(node, name, None)
            if type(value) is list:
                stack.extend((item, scope) for item in value if isinstance(item, ast.AST))
            elif isinstance(value, ast.AST):
                stack.append((value, scope))
    sites = []
    for bindings, candidates in scopes:
        for node in candidates:
            use = _instance_dict_use(node, imports)
            if use is None:
                continue
            subject, kind = use
            if isinstance(subject, ast.Call):
                chains = {_name_chain(subject.func)} - {None}
            elif isinstance(subject, ast.Name):
                chains = bindings.get(subject.id, set())
            else:
                continue
            sites.extend((node.lineno, kind, chain) for chain in sorted(chains))
    return returns, imports, sites


# 函数用途: 取模块的 (返回注解名表, import 表)，首次用到时解析并缓存；包外或解析失败为 None。
def _tables_for(top: str, root: str, tables: dict, module: str):
    if module not in tables:
        path = _module_file(top, root, module)
        tree = None if path is None else _parse_bytes(_read_source(path))
        tables[module] = None if tree is None else _analyze_module(
            tree, module, os.path.basename(path) == "__init__.py")[:2]
    return tables[module]


# 函数用途: 扫描包根下全部源文件，返回 (调用点列表, 模块表缓存)；调用点为 (路径:行号, 种类, 模块, import 表, 名字链)。
def _scan_root(top: str, root: str) -> tuple[list, dict]:
    sites: list = []
    tables: dict = {}
    parent = os.path.dirname(root)
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__" and not name.startswith("."))
        for filename in sorted(files):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            source = _read_source(path)
            if source is None or not any(marker in source for marker in _CALLER_MARKERS):
                continue
            tree = _parse_bytes(source)
            if tree is None:
                continue
            parts = os.path.relpath(path, root)[:-3].split(os.sep)
            is_package = parts[-1] == "__init__"
            module = ".".join([top, *(parts[:-1] if is_package else parts)])
            returns, imports, found = _analyze_module(tree, module, is_package)
            tables[module] = returns, imports
            display = os.path.relpath(path, parent).replace(os.sep, "/")
            sites.extend((f"{display}:{line}", kind, module, imports, chain) for line, kind, chain in found)
    return sites, tables


# LLM: 名字链的首名先按调用方模块的 import 表解析（没有则视为本模块里定义的名字），中间名依次落到存在源文件的
#   子模块上，最后一名是被调用的对象名；链里还剩属性或方法（无法静态确定）时返回 None。
# 函数用途: 把调用方的名字链解析成 (定义它的模块, 名字)。
def _resolve_callee(top: str, root: str, module: str, imports: dict, chain: tuple[str, ...]):
    base, attr = imports.get(chain[0], (module, chain[0]))
    parts = list(chain[1:]) if attr is None else [attr, *chain[1:]]
    while len(parts) > 1 and _module_file(top, root, f"{base}.{parts[0]}") is not None:
        base = f"{base}.{parts.pop(0)}"
    return (base, parts[0]) if len(parts) == 1 else None


# LLM: (模块, 名字) 就是该类（直接构造），或是返回注解写着类名的顶层函数，即产出该类实例；名字是从别处导入的
#   （再导出）时沿 import 表追到定义处，visited 防止循环导入绕圈。
# 函数用途: 判断调用 module.name 是否产出 cls 的实例。
def _produces(cls: type, top: str, root: str, tables: dict, module: str, name: str) -> bool:
    visited: set[tuple[str, str]] = set()
    while name is not None and (module, name) not in visited:
        visited.add((module, name))
        if module == cls.__module__ and name == cls.__qualname__:
            return True
        table = _tables_for(top, root, tables, module)
        if table is None:
            return False
        returns, imports = table
        if name in returns:
            return cls.__name__ in returns[name]
        module, name = imports.get(name, (module, None))
    return False


# LLM: 转换时检查专用（迁移提交与测试调用；fast_frozen 与请求路径从不调用）：在类所在顶层包里找对“产出该类实例
#   的调用”的结果用 vars()、读 __dict__ 或取弱引用的调用方；类已有 __weakref__ 槽位时弱引用不算阻碍。同一包根
#   第一次检查时扫描全部源码并缓存（源文件在进程内视为不变）；函数内定义的类与非常规包跳过。
# 函数用途: 返回调用方阻碍列表。
def _caller_blockers(cls: type) -> list[str]:
    located = None if "<locals>" in cls.__qualname__ else _package_root(cls)
    if located is None:
        return []
    top, root = located
    weak_ready = "__slots__" in cls.__dict__ and "__weakref__" in _slot_names(cls)
    blockers = []
    with _SCAN_LOCK:
        if root not in _CALLER_INDEX:
            _CALLER_INDEX[root] = _scan_root(top, root)
        sites, tables = _CALLER_INDEX[root]
        for site, kind, module, imports, chain in sites:
            if kind == "weakref" and weak_ready:
                continue
            target = _resolve_callee(top, root, module, imports, chain)
            if target is not None and _produces(cls, top, root, tables, *target):
                blockers.append(_CALLER_MESSAGES[kind].format(site=site, cls=cls.__name__, source=".".join(chain)))
    return blockers


# LLM: 只做静态与只读运行时检查，不修改类：报告冻结/init 前提、类体内手写 __init__、无 __slots__ 或变长的基类、
#   cached_property、__dict__/vars() 使用、零参 super()、对非字段常量名的 object.__setattr__/setattr、
#   早于 @dataclass 执行的类装饰器、模块用到 weakref 而转换后不保留 __weakref__、包内调用方对该类实例用
#   vars()/__dict__/弱引用（_caller_blockers），以及 pickle 按引用解析或探针实例往返失败。取不到源码时如实报告
#   “无法核验”，由调用方决定是否保留原类。它会扫描整个包的源码，只在转换时调用，不放在任何请求路径上。
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
    report.extend(_caller_blockers(cls))
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
