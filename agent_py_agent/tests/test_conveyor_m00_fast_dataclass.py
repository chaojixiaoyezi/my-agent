# LLM: Conveyor M00 / R3-G0：@fast_frozen 只换掉冻结槽位数据类 __init__ 的赋值方式（捕获成员描述符 __set__，
#   不再走 object.__setattr__ 的类型查找），可观察语义必须与普通 @dataclass(frozen=True, slots=True) 完全一致。
#   这里对随机生成的同源类做差分：签名/默认值/default_factory/kw_only/InitVar/__post_init__/FrozenInstanceError/
#   eq/hash/repr/pickle/copy.replace，以及 fast_replace 与 dataclasses.replace 的结果和异常（类型+文本）一致；
#   另钉住 slot_setter、check_slots_compat 的报告项，并给 G1 锁等待门禁提供样例 DTO 用例工厂。Layer-2 M00-fix
#   （F1）：check_slots_compat 必须指出包内对工厂结果用 vars()/__dict__/弱引用的调用方，真实的读取方到消费方路径
#   在内存转换后仍用 vars(report) 时不得放行；它只在转换时调用，生产代码里不得出现。M10 前置修复：生成的
#   __replace__ 的回落判断只读本次调用的 changes，不查询类级共享集合（3.14t 关闭 GIL 时那是一把共享临界区锁），
#   单个类被所有线程同时替换的 G1 用例由本模块提供。S2-P1-01：转换类写出 {字段名: 值} 状态，并能读回命名、
#   位置与槽位二元组三种历史布局（真实 DTO 的跨版本样本在 test_conveyor_s2_legacy_dto_state.py）。
# 模块用途: 固定 agent/common/fast_dataclass.py 的转换合同（C1.3），不读 git 历史。
from __future__ import annotations

import copy
import copyreg
import dataclasses
import functools
import importlib
import importlib.util
import inspect
import io
import itertools
import json
import pickle
import random
import sys
import threading
import types
from dataclasses import FrozenInstanceError, InitVar, dataclass, field
from pathlib import Path

import pytest

from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.common.fast_dataclass import (
    check_slots_compat,
    fast_frozen,
    fast_replace,
    slot_setter,
)

# 随机生成的差分类数量（每个类同源生成普通版与 fast 版）。
GENERATED_CLASS_COUNT = 160
# 每个生成类的随机实参组数（含有效与故意无效的调用）。
ARGUMENT_SETS_PER_CLASS = 24
# 每个生成类的随机 replace 改动组数。
REPLACE_SETS_PER_CLASS = 12
# 生成器固定种子，失败可复现。
GENERATOR_SEED = 4242
# 每个生成类的最大字段数。
MAX_FIELDS_PER_CLASS = 8
# G1 样例 DTO 的复制类数：R3 §2.1 用 2000 个冻结类复现类型缓存哈希带冲突，单个类测不出护航。
G1_DTO_CLASS_COUNT = 2000

_HEADER = (
    "import dataclasses\n"
    "from dataclasses import InitVar, dataclass, field\n"
    "from typing import ClassVar\n"
    "from agent_py_agent.agent.common.fast_dataclass import fast_frozen, fast_replace, slot_setter\n"
)
_FUTURE = "from __future__ import annotations\n"
_STD_REQUIRED = ("req", "ivreq")
_STD_DEFAULTED = ("dflt", "fact", "ivdflt", "norepr", "nocmp")
_KW_KINDS = ("kwreq", "kwdflt", "kwfact")
_OTHER_KINDS = ("noinit_dflt", "noinit_fact", "noinit_post", "classvar")
_DEFAULTED = frozenset(("dflt", "fact", "ivdflt", "norepr", "nocmp", "kwdflt", "kwfact"))
_INIT_KINDS = frozenset(_STD_REQUIRED + _STD_DEFAULTED + _KW_KINDS)
_INITVAR_KINDS = frozenset(("ivreq", "ivdflt"))
_NOINIT_KINDS = frozenset(("noinit_dflt", "noinit_fact", "noinit_post"))


# 函数用途: 生成一个字段的源码行（同一行同时用于普通版与 fast 版）。
def _field_line(name: str, kind: str, rng: random.Random) -> str:
    return {
        "req": f"{name}: int",
        "dflt": f"{name}: int = {rng.randrange(-2, 9)}",
        "fact": f"{name}: list = field(default_factory=list)",
        "kwreq": f"{name}: str = field(kw_only=True)",
        "kwdflt": f"{name}: str = field(default={rng.choice(['a', 'bb', ''])!r}, kw_only=True)",
        "kwfact": f"{name}: dict = field(default_factory=dict, kw_only=True)",
        "ivreq": f"{name}: InitVar[int]",
        "ivdflt": f"{name}: InitVar[int] = {rng.randrange(0, 5)}",
        "noinit_dflt": f"{name}: tuple = field(default=(1, 'x'), init=False)",
        "noinit_fact": f"{name}: list = field(default_factory=lambda: [0], init=False)",
        "noinit_post": f"{name}: int = field(init=False)",
        "norepr": f"{name}: int = field(default=0, repr=False)",
        "nocmp": f"{name}: int = field(default=1, compare=False, hash=False)",
        "classvar": f"{name}: ClassVar[int] = 3",
    }[kind]


# 函数用途: 随机生成一个类的规格：可选基类字段、本类字段（满足位置参数先无默认后有默认的约束）与 __post_init__。
def _class_spec(rng: random.Random, index: int) -> dict:
    names = (f"f{position}" for position in itertools.count())
    base = []
    if rng.random() < 0.25:
        base = [(next(names), "req") for _ in range(rng.randrange(1, 3))]
        if rng.random() < 0.5:
            base.append((next(names), "kwdflt"))
    kinds = [rng.choice(_STD_REQUIRED + _STD_DEFAULTED + _KW_KINDS + _OTHER_KINDS)
             for _ in range(rng.randrange(1, MAX_FIELDS_PER_CLASS))]
    required = [kind for kind in kinds if kind in _STD_REQUIRED]
    defaulted = [kind for kind in kinds if kind in _STD_DEFAULTED]
    others = [kind for kind in kinds if kind not in _STD_REQUIRED and kind not in _STD_DEFAULTED]
    ordered = required + defaulted
    for kind in others:
        ordered.insert(rng.randrange(len(ordered) + 1), kind)
    fields = []
    for kind in ordered:
        name = next(names)
        # 偶尔用 self/object/type 作字段名：生成的 __init__ 不得被参数名遮蔽内部引用。
        if kind in ("req", "dflt") and rng.random() < 0.08:
            special = rng.choice(("self", "object", "type"))
            if special not in {field_name for field_name, _ in base + fields}:
                name = special
        fields.append((name, kind))
    all_fields = base + fields
    initvars = [name for name, kind in all_fields if kind in _INITVAR_KINDS]
    posts = [name for name, kind in fields if kind == "noinit_post"]
    first_int = next((name for name, kind in all_fields if kind == "req"), None)
    return {
        "name": f"Gen{index}", "base": base, "fields": fields, "all_fields": all_fields,
        "initvars": initvars, "posts": posts, "first_int": first_int,
        "post_init": bool(initvars or posts) or rng.random() < 0.3, "future": index % 2 == 1,
    }


# 函数用途: 把规格渲染成类定义源码；fast 版只多一行 @fast_frozen。
def _class_source(spec: dict, rng: random.Random, *, fast: bool) -> str:
    lines = []
    decorator = "@fast_frozen\n" if fast else ""
    base_name = ""
    if spec["base"]:
        base_name = spec["name"] + "Base"
        lines.append(f"{decorator}@dataclass(frozen=True, slots=True)\nclass {base_name}:")
        lines.extend(f"    {_field_line(name, kind, rng)}" for name, kind in spec["base"])
        lines.append("")
    lines.append(f"{decorator}@dataclass(frozen=True, slots=True)\nclass {spec['name']}"
                 f"{'(' + base_name + ')' if base_name else ''}:")
    lines.extend(f"    {_field_line(name, kind, rng)}" for name, kind in spec["fields"])
    if spec["post_init"]:
        lines.append(f"    def __post_init__(self, {', '.join(spec['initvars'])}):")
        if spec["first_int"]:
            lines.append(f"        if self.{spec['first_int']} < 0:")
            lines.append(f"            raise ValueError('negative ' + str(self.{spec['first_int']}))")
        total = " + ".join(spec["initvars"]) or "7"
        for post in spec["posts"]:
            lines.append(f"        _set_{spec['name']}_{post}(self, {total})")
        lines.append("        return None")
    lines.append("")
    for post in spec["posts"]:
        lines.append(f"_set_{spec['name']}_{post} = slot_setter({spec['name']}, {post!r})")
    lines.append("")
    return "\n".join(lines)


# 函数用途: 以真实模块身份执行生成源码（pickle 与字符串注解解析都需要 sys.modules 里的模块）。
def _build_module(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    return module


@pytest.fixture(scope="module")
def generated():
    rng = random.Random(GENERATOR_SEED)
    specs = [_class_spec(rng, index) for index in range(GENERATED_CLASS_COUNT)]
    modules = {}
    for variant in ("a", "b"):
        chosen = [spec for spec in specs if spec["future"] == (variant == "b")]
        header = (_FUTURE if variant == "b" else "") + _HEADER
        for kind in ("plain", "fast"):
            text_rng = random.Random(GENERATOR_SEED + (variant == "b"))
            body = "".join(_class_source(spec, text_rng, fast=kind == "fast") for spec in chosen)
            modules[(kind, variant)] = _build_module(f"m00_gen_{kind}_{variant}", header + body)
    pairs = []
    for spec in specs:
        variant = "b" if spec["future"] else "a"
        pairs.append((spec, getattr(modules[("plain", variant)], spec["name"]),
                      getattr(modules[("fast", variant)], spec["name"])))
    yield pairs
    for kind, variant in modules:
        sys.modules.pop(f"m00_gen_{kind}_{variant}", None)


# 函数用途: 调用并返回可比较的结果：成功时给出状态投影，失败时给出异常类型与文本。
def _outcome(fn, *args, **kwargs):
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 差分比较需要捕获任意异常类型
        return ("raise", type(exc), str(exc))
    return ("ok", _state(value))


# 函数用途: 把实例投影成可比较的状态（字段值 + repr + 类名）；非数据类实例原样返回。
def _state(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__qualname__, tuple((f.name, getattr(value, f.name)) for f in dataclasses.fields(value)),
                repr(value))
    return value


# 函数用途: 生成某种字段的随机实参值。
def _value(kind: str, rng: random.Random):
    if kind in ("kwreq", "kwdflt"):
        return rng.choice(["x", "", "中文", "😀"])
    if kind == "fact":
        return [rng.randrange(5)]
    if kind == "kwfact":
        return {"k": rng.randrange(5)}
    return rng.randrange(-1, 40)


# 函数用途: 生成一次构造调用的位置/关键字实参，按概率注入缺参、未知参数、超额位置参数、重复绑定等错误。
def _call_arguments(spec: dict, rng: random.Random) -> tuple[list, dict]:
    std = [(name, kind) for name, kind in spec["all_fields"] if kind in _STD_REQUIRED + _STD_DEFAULTED]
    kw = [(name, kind) for name, kind in spec["all_fields"] if kind in _KW_KINDS]
    args: list = []
    kwargs: dict = {}
    positional_open = True
    for name, kind in std:
        roll = rng.random()
        if kind in _DEFAULTED and roll < 0.3:
            positional_open = False
            continue
        if positional_open and roll < 0.75:
            args.append(_value(kind, rng))
        else:
            positional_open = False
            kwargs[name] = _value(kind, rng)
    for name, kind in kw:
        if kind in _DEFAULTED and rng.random() < 0.4:
            continue
        kwargs[name] = _value(kind, rng)
    mutation = rng.random()
    if mutation < 0.08 and kwargs:
        kwargs.pop(next(iter(kwargs)))
    elif mutation < 0.16 and args:
        args.pop()
    elif mutation < 0.22:
        kwargs["zz_unknown"] = 1
    elif mutation < 0.28:
        args = args + [0] * (len(std) - len(args) + 1)
    elif mutation < 0.34 and args and std:
        kwargs[std[0][0]] = _value(std[0][1], rng)
    return args, kwargs


# 函数用途: 生成一次 replace 的改动集合，按概率混入 init=False 字段、未知键与缺失的必填 InitVar。
def _replace_changes(spec: dict, rng: random.Random) -> dict:
    changes = {}
    for name, kind in spec["all_fields"]:
        if kind in _INIT_KINDS and rng.random() < 0.4:
            changes[name] = _value(kind, rng)
    roll = rng.random()
    noinit = [name for name, kind in spec["all_fields"] if kind in _NOINIT_KINDS]
    if roll < 0.15 and noinit:
        changes[rng.choice(noinit)] = 5
    elif roll < 0.25:
        changes["zz_unknown"] = 1
    elif roll < 0.6:
        # 必填 InitVar 一律补齐，否则 replace 必然失败，覆盖不到成功路径。
        for name, kind in spec["all_fields"]:
            if kind == "ivreq":
                changes[name] = rng.randrange(0, 9)
    return changes


def test_generated_classes_are_differentially_identical(generated):
    rng = random.Random(GENERATOR_SEED + 7)
    constructed = 0
    for spec, plain_cls, fast_cls in generated:
        assert plain_cls is not fast_cls and plain_cls.__qualname__ == fast_cls.__qualname__
        assert fast_cls in fast_dataclass.CONVERTED_CLASSES and plain_cls not in fast_dataclass.CONVERTED_CLASSES
        assert inspect.signature(fast_cls) == inspect.signature(plain_cls), spec
        assert fast_cls.__init__.__qualname__ == plain_cls.__init__.__qualname__
        assert fast_cls.__init__.__defaults__ == plain_cls.__init__.__defaults__
        assert fast_cls.__init__.__kwdefaults__ == plain_cls.__init__.__kwdefaults__
        assert fast_cls.__match_args__ == plain_cls.__match_args__
        for _ in range(ARGUMENT_SETS_PER_CLASS):
            args, kwargs = _call_arguments(spec, rng)
            plain_outcome = _outcome(plain_cls, *args, **kwargs)
            assert _outcome(fast_cls, *args, **kwargs) == plain_outcome, (spec, args, kwargs)
            if plain_outcome[0] != "ok":
                continue
            constructed += 1
            plain, fast = plain_cls(*args, **kwargs), fast_cls(*args, **kwargs)
            assert (fast == fast_cls(*args, **kwargs)) is (plain == plain_cls(*args, **kwargs)) is True
            assert _outcome(hash, fast) == _outcome(hash, plain)
            for name in [f.name for f in dataclasses.fields(plain)] + ["zz_extra"]:
                assert _outcome(setattr, fast, name, 1) == _outcome(setattr, plain, name, 1)
                assert _outcome(delattr, fast, name) == _outcome(delattr, plain, name)
            for protocol in (2, pickle.HIGHEST_PROTOCOL):
                assert _state(pickle.loads(pickle.dumps(fast, protocol))) == _state(plain)
                assert type(pickle.loads(pickle.dumps(fast, protocol))) is fast_cls
            for _ in range(REPLACE_SETS_PER_CLASS):
                changes = _replace_changes(spec, rng)
                expected = _outcome(dataclasses.replace, plain, **changes)
                assert _outcome(fast_replace, fast, **changes) == expected, (spec, changes)
                assert _outcome(fast.__replace__, **changes) == expected, (spec, changes)
                assert _outcome(fast.__fast_replace__, **changes) == expected, (spec, changes)
                if hasattr(copy, "replace"):
                    assert _outcome(copy.replace, fast, **changes) == _outcome(copy.replace, plain, **changes)
    assert constructed > GENERATED_CLASS_COUNT * 4


def test_generated_init_assigns_through_captured_descriptors_not_object_setattr(generated):
    controls = 0
    for _spec, plain_cls, fast_cls in generated:
        # 对照：dataclasses 生成的 __init__ 有字段要赋值时走 object.__setattr__；fast 版不得再出现该名字。
        controls += "__setattr__" in plain_cls.__init__.__code__.co_names
        assert "__setattr__" not in fast_cls.__init__.__code__.co_names
        assert "__setattr__" not in fast_cls.__replace__.__code__.co_names
    assert controls > GENERATED_CLASS_COUNT // 2


# LLM: 按旧写入方的流形状（类按引用，再 BUILD(state)）写出任意历史状态：转换前字典实例的 {字段名: 值}、Wave-1
#   合并版标准库的值列表，以及 object.__getstate__ 的 (实例字典或 None, 槽位字典)。协议 0/1 经
#   copyreg._reconstructor，协议 2 起经 NEWOBJ；pickler 只接受该类实例的 NEWOBJ，所以目标是未初始化的实例。
# 类用途: 只改写目标实例的 reduce 结果，其余对象照常序列化。
class _HistoricalWriter(pickle.Pickler):
    def __init__(self, file, protocol: int, target: object, state: object) -> None:
        super().__init__(file, protocol)
        self._protocol = protocol
        self._target = target
        self._state = state

    def reducer_override(self, obj):
        if obj is not self._target:
            return NotImplemented
        if self._protocol < 2:
            return copyreg._reconstructor, (type(obj), object, None), self._state
        return copyreg.__newobj__, (type(obj),), self._state


# 函数用途: 经真实 pickle 流，用一份历史状态恢复某个类的实例。
def _restore_historical(cls: type, state: object, protocol: int) -> object:
    target = object.__new__(cls)
    buffer = io.BytesIO()
    _HistoricalWriter(buffer, protocol, target, state).dump(target)
    return pickle.loads(buffer.getvalue())


def test_generated_classes_restore_every_historical_state_layout(generated):
    rng = random.Random(GENERATOR_SEED + 11)
    restored_classes = 0
    for spec, plain_cls, fast_cls in generated:
        for _ in range(ARGUMENT_SETS_PER_CLASS):
            args, kwargs = _call_arguments(spec, rng)
            if _outcome(plain_cls, *args, **kwargs)[0] == "ok":
                break
        else:
            continue
        plain = plain_cls(*args, **kwargs)
        names = [f.name for f in dataclasses.fields(plain)]
        named = {name: getattr(plain, name) for name in names}
        state = fast_cls(*args, **kwargs).__getstate__()
        assert state == named and list(state) == names, spec
        for layout in (named, [named[name] for name in names], (None, named)):
            for protocol in (0, 2, pickle.HIGHEST_PROTOCOL):
                restored = _restore_historical(fast_cls, layout, protocol)
                assert type(restored) is fast_cls and _state(restored) == _state(plain), (spec, layout)
        restored_classes += 1
    assert restored_classes > GENERATED_CLASS_COUNT // 2


def test_frozen_instance_error_type_is_unchanged(generated):
    _spec, plain_cls, fast_cls = next(item for item in generated if not item[0]["initvars"]
                                      and all(kind in _DEFAULTED or kind in _NOINIT_KINDS or kind == "classvar"
                                              for _name, kind in item[0]["all_fields"]))
    name = dataclasses.fields(plain_cls)[0].name
    for cls in (fast_cls, plain_cls):
        with pytest.raises(FrozenInstanceError):
            setattr(cls(), name, 1)
        with pytest.raises(FrozenInstanceError):
            delattr(cls(), name)


@fast_frozen
@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int = 0


# 类用途: 未加 fast_frozen 的子类：fast_replace 与继承来的 __replace__ 都必须回落到 dataclasses.replace。
@dataclass(frozen=True, slots=True)
class LabelledPoint(Point):
    label: str = "p"


def test_unconverted_subclass_and_plain_objects_fall_back_to_dataclasses_replace():
    sub = LabelledPoint(1, 2, "a")
    assert fast_replace(sub, label="b") == LabelledPoint(1, 2, "b")
    assert sub.__replace__(x=5) == LabelledPoint(5, 2, "a")
    assert type(sub.__replace__(x=5)) is LabelledPoint
    with pytest.raises(TypeError) as ours:
        fast_replace(object())
    with pytest.raises(TypeError) as theirs:
        dataclasses.replace(object())
    assert str(ours.value) == str(theirs.value)


# 类用途: 没有 init=False 字段、也没有必填 InitVar 的转换类：__replace__ 只剩类型检查一条回落条件。
@fast_frozen
@dataclass(frozen=True, slots=True)
class ReplaceOpenDto:
    request_id: str
    attempt: int = 0
    status: str = "queued"


# 类用途: 同时带 init=False 字段、必填 InitVar 与带默认值 InitVar 的转换类：回落条件逐个字段生成。
@fast_frozen
@dataclass(frozen=True, slots=True)
class ReplaceGuardedDto:
    request_id: str
    token: InitVar[int]
    attempt: int = 0
    salt: InitVar[int] = 1
    digest: int = field(init=False, default=0)

    def __post_init__(self, token: int, salt: int) -> None:
        _set_replace_guarded_digest(self, self.attempt * 31 + token + salt)


_set_replace_guarded_digest = slot_setter(ReplaceGuardedDto, "digest")


# LLM: 3.14t 关闭 GIL 时，frozenset.isdisjoint(changes) 会进入该集合与 changes 的临界区；frozenset <= changes.keys()
#   反射到 dict_keys 的比较后要迭代该集合，同样进入它的临界区。所有线程替换同一个类时就排在这一把锁上（G1 约 0.2）。
#   这里在任何解释器上静态钉住：生成的 __replace__ 不调用集合方法，闭包里没有任何容器（随机生成的 160 个类也一样）。
def test_replace_fallback_checks_read_only_the_per_call_changes(generated):
    converted = [ReplaceOpenDto, ReplaceGuardedDto, *(fast_cls for _spec, _plain_cls, fast_cls in generated)]
    for cls in converted:
        replace_fn = cls.__dict__["__replace__"]
        assert not {"isdisjoint", "issubset", "issuperset", "keys"} & set(replace_fn.__code__.co_names), cls
        containers = [cell.cell_contents for cell in replace_fn.__closure__ or ()
                      if isinstance(cell.cell_contents, (set, frozenset, dict, list, tuple, types.MappingProxyType))]
        assert containers == [], (cls.__qualname__, containers)
    # init=False 字段只出现在回落条件里，它以字面量名字出现在常量表中。
    assert "digest" in ReplaceGuardedDto.__replace__.__code__.co_consts


@pytest.mark.parametrize(("cls", "args", "changes"), [
    (ReplaceOpenDto, ("r", 1), {}),
    (ReplaceOpenDto, ("r", 1), {"status": "done"}),
    (ReplaceOpenDto, ("r", 1), {"attempt": 3, "zz_unknown": 1}),
    (ReplaceGuardedDto, ("r", 2, 1), {"token": 3}),
    (ReplaceGuardedDto, ("r", 2, 1), {"token": 3, "attempt": 5, "salt": 7}),
    (ReplaceGuardedDto, ("r", 2, 1), {}),
    (ReplaceGuardedDto, ("r", 2, 1), {"attempt": 5}),
    (ReplaceGuardedDto, ("r", 2, 1), {"token": 3, "digest": 9}),
    (ReplaceGuardedDto, ("r", 2, 1), {"digest": 9}),
    (ReplaceGuardedDto, ("r", 2, 1), {"token": 3, "zz_unknown": 1}),
], ids=["open-noop", "open-change", "open-unknown-key", "guarded-initvar", "guarded-all-fields",
        "guarded-missing-initvar", "guarded-missing-initvar-with-change", "guarded-noinit-field",
        "guarded-noinit-and-missing-initvar", "guarded-unknown-key"])
def test_replace_fallback_matches_dataclasses_replace(cls, args, changes):
    instance = cls(*args)
    expected = _outcome(dataclasses.replace, instance, **changes)
    assert _outcome(fast_replace, instance, **changes) == expected
    assert _outcome(instance.__replace__, **changes) == expected
    if hasattr(copy, "replace"):
        assert _outcome(copy.replace, instance, **changes) == expected


def test_fast_frozen_is_idempotent_and_registers_the_class():
    before = fast_dataclass.CONVERTED_CLASSES
    assert fast_frozen(Point) is Point
    assert before == fast_dataclass.CONVERTED_CLASSES
    assert Point in fast_dataclass.CONVERTED_CLASSES
    assert isinstance(fast_dataclass.CONVERTED_CLASSES, tuple)
    assert Point.__replace__ is Point.__fast_replace__


@pytest.mark.parametrize("decorate", [
    lambda: dataclass(frozen=True)(type("NoSlots", (), {"__annotations__": {"a": int}})),
    lambda: dataclass(slots=True)(type("NotFrozen", (), {"__annotations__": {"a": int}})),
    lambda: dataclass(frozen=True, slots=True, init=False)(type("NoInit", (), {"__annotations__": {"a": int}})),
    lambda: type("NotDataclass", (), {}),
])
def test_fast_frozen_rejects_classes_it_cannot_convert(decorate):
    with pytest.raises(TypeError):
        fast_frozen(decorate())


def test_fast_frozen_rejects_custom_init():
    @dataclass(frozen=True, slots=True)
    class Custom:
        a: int = 0

        def __init__(self, a: int = 0) -> None:
            object.__setattr__(self, "a", a)

    with pytest.raises(TypeError):
        fast_frozen(Custom)


def test_slot_setter_writes_only_real_slots():
    point = Point(1, 2)
    set_x = slot_setter(Point, "x")
    set_x(point, 9)
    assert point.x == 9 and point == Point(9, 2)
    with pytest.raises(TypeError):
        slot_setter(Point, "missing")
    with pytest.raises(TypeError):
        slot_setter(type("Plain", (), {}), "x")
    inherited = slot_setter(LabelledPoint, "y")
    labelled = LabelledPoint(1, 2)
    inherited(labelled, 7)
    assert labelled.y == 7


# 下列类专供 check_slots_compat：源码就在本模块，inspect 可取到。
@dataclass(frozen=True)
class CompatOk:
    a: int = 0
    b: str = "x"
    c: tuple = ()

    def total(self) -> int:
        return self.a + len(self.b)


@dataclass(frozen=True, slots=True)
class CompatOkSlotted:
    a: int = 0


@dataclass(frozen=True)
class UsesDict:
    a: int = 0

    def snapshot(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class UsesVars:
    a: int = 0

    def snapshot(self) -> dict:
        return vars(self)


@dataclass(frozen=True)
class CachedProperty:
    a: int = 0

    @functools.cached_property
    def double(self) -> int:
        return self.a * 2


@dataclass(frozen=True)
class ZeroArgSuper:
    a: int = 0

    def describe(self) -> str:
        return super().__repr__()


@dataclass(frozen=True)
class ExtraAttribute:
    a: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "_cache", self.a)


@dataclass(frozen=True)
class CustomInitClass:
    a: int = 0

    def __init__(self, a: int = 0) -> None:
        object.__setattr__(self, "a", a)


_EARLY_REGISTRY: list[type] = []


# 函数用途: 早于 @dataclass 执行的类装饰器（捕获的是转换前的类对象）。
def _register_early(cls: type) -> type:
    _EARLY_REGISTRY.append(cls)
    return cls


@dataclass(frozen=True)
@_register_early
class EarlyDecorator:
    a: int = 0


@dataclass
class NotFrozen:
    a: int = 0


class NotDataclass:
    pass


@dataclass(frozen=True)
class PlainBase:
    a: int = 0


@dataclass(frozen=True, slots=True)
class SlottedOnPlainBase(PlainBase):
    b: int = 0


@pytest.mark.parametrize(("cls", "needle"), [
    (UsesDict, "__dict__"),
    (UsesVars, "vars()"),
    (CachedProperty, "cached_property"),
    (ZeroArgSuper, "super()"),
    (ExtraAttribute, "_cache"),
    (CustomInitClass, "__init__"),
    (EarlyDecorator, "decorator"),
    (NotFrozen, "frozen"),
    (NotDataclass, "frozen"),
    (SlottedOnPlainBase, "__slots__"),
])
def test_check_slots_compat_reports_each_blocker(cls, needle):
    report = check_slots_compat(cls)
    assert report and all(isinstance(line, str) for line in report)
    assert any(needle in line for line in report), report


def test_check_slots_compat_accepts_clean_classes():
    assert check_slots_compat(CompatOk) == []
    assert check_slots_compat(CompatOkSlotted) == []
    assert check_slots_compat(Point) == []


def test_check_slots_compat_reports_local_class_pickle_and_missing_source(generated):
    @dataclass(frozen=True)
    class Local:
        a: int = 0

    assert any("pickle" in line for line in check_slots_compat(Local))
    _spec, plain_cls, _fast_cls = generated[0]
    assert any("source" in line for line in check_slots_compat(plain_cls))


def test_check_slots_compat_reports_weakref_use(tmp_path, monkeypatch):
    path = tmp_path / "m00_weak_module.py"
    path.write_text(
        "import weakref\n"
        "from dataclasses import dataclass\n\n\n"
        "@dataclass(frozen=True)\n"
        "class Weak:\n"
        "    a: int = 0\n\n\n"
        "@dataclass(frozen=True, slots=True, weakref_slot=True)\n"
        "class WeakReady:\n"
        "    a: int = 0\n\n\n"
        "_REFS = weakref.WeakSet()\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("m00_weak_module", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "m00_weak_module", module)
    spec.loader.exec_module(module)
    assert any("weakref" in line for line in check_slots_compat(module.Weak))
    assert check_slots_compat(module.WeakReady) == []


# 调用方检查用的临时包：工厂按返回注解产出 Report；三个调用方对结果用 vars()/__dict__/弱引用，clean.py 只读字段。
_CALLER_PACKAGE_FILES = {
    "__init__.py": "",
    "reports.py": (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n\n\n"
        "@dataclass(frozen=True)\n"
        "class Report:\n"
        "    payload: dict\n"
        "    load_error: object = None\n\n\n"
        "@dataclass(frozen=True)\n"
        "class Quiet:\n"
        "    payload: dict\n\n\n"
        "def read_report(path) -> Report:\n"
        "    return Report({'path': path})\n\n\n"
        "def read_quiet(path) -> 'Quiet | None':\n"
        "    return Quiet({'path': path})\n"
    ),
    "uses_vars.py": (
        "from .reports import read_report\n\n\n"
        "def first_field(path):\n"
        "    report = read_report(path)\n"
        "    return next(iter(vars(report).values()))\n"
    ),
    "uses_dict.py": (
        "from . import reports\n\n\n"
        "def raw(path):\n"
        "    return reports.read_report(path).__dict__\n"
    ),
    "uses_weakref.py": (
        "import weakref\n"
        "from .reports import Report\n\n\n"
        "def track(payload):\n"
        "    return weakref.ref(Report(payload))\n"
    ),
    "clean.py": (
        "from .reports import read_quiet, read_report\n\n\n"
        "def payload(path):\n"
        "    return read_report(path).payload\n\n\n"
        "def unrelated(obj):\n"
        "    return vars(obj)\n\n\n"
        "def quiet(path):\n"
        "    return read_quiet(path).payload\n"
    ),
}


def test_check_slots_compat_reports_callers_that_need_an_instance_dict(tmp_path, monkeypatch):
    package = f"m00fix_callers_{tmp_path.name.replace('-', '_')}"
    (tmp_path / package).mkdir()
    for name, text in _CALLER_PACKAGE_FILES.items():
        (tmp_path / package / name).write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [key for key in sys.modules if key == package or key.startswith(package + ".")]:
        monkeypatch.delitem(sys.modules, name)
    reports = importlib.import_module(f"{package}.reports")
    try:
        findings = check_slots_compat(reports.Report)
        quiet = check_slots_compat(reports.Quiet)
    finally:
        for name in [key for key in sys.modules if key == package or key.startswith(package + ".")]:
            del sys.modules[name]
    for needle, kind in ((f"{package}/uses_vars.py:6", "vars()"), (f"{package}/uses_dict.py:5", "__dict__"),
                         (f"{package}/uses_weakref.py:6", "weak reference")):
        assert any(needle in line and kind in line for line in findings), findings
    assert not any("clean.py" in line for line in findings), findings
    assert quiet == []


# 函数用途: 按原字段（含默认值）重建一份 slots=True 的冻结副本，模拟 Cure-A 转换后的类。
def _slotted_copy(cls: type) -> type:
    namespace: dict[str, object] = {"__module__": cls.__module__, "__qualname__": cls.__qualname__,
                                    "__annotations__": {f.name: f.type for f in dataclasses.fields(cls)}}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            namespace[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            namespace[f.name] = dataclasses.field(default_factory=f.default_factory)
    return dataclass(frozen=True, slots=True)(type(cls.__name__, (), namespace))


def test_check_slots_compat_flags_real_report_readers_while_they_use_vars(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.conversation import runtime_reconciliation
    from agent_py_agent.agent.gateway_parts import compact_operations
    from agent_py_agent.agent.gateway_parts import io as gateway_io

    terminal = tmp_path / "terminal"
    terminal.mkdir()
    (terminal / "root-1.json").write_text(json.dumps(
        {"schema_version": "gateway_terminal_request.v1", "id": "root-1", "status": "done"}), encoding="utf-8")
    approval = tmp_path / "approvals" / "a.json"
    approval.parent.mkdir()
    approval.write_text(json.dumps({"status": "pending", "thread_id": "t-1"}), encoding="utf-8")
    cases = (
        (gateway_io, "GatewayJsonReadReport", "agent_py_agent/agent/conversation/runtime_reconciliation.py",
         lambda: runtime_reconciliation.canonical_gateway_terminal_reason(types.SimpleNamespace(terminal=terminal),
                                                                          "root-1"), "parent_turn_ended"),
        (json_io, "JsonObjectReadReport", "agent_py_agent/agent/gateway_parts/compact_operations.py",
         lambda: compact_operations._inspect_approval_record(approval, "t-1"), "pending_approval"),
    )
    for module, name, caller, consume, expected in cases:
        cls = getattr(module, name)
        assert consume() == expected
        # 已转换的类直接用；未转换的按计划在内存里转成 slots=True，跑真实的读取方到消费方路径。
        slotted = cls if "__slots__" in cls.__dict__ else _slotted_copy(cls)
        monkeypatch.setattr(module, name, slotted)
        try:
            outcome = consume()
        except TypeError:
            outcome = TypeError
        finally:
            monkeypatch.setattr(module, name, cls)
        if outcome is TypeError:
            # 消费方仍对报告用 vars()：转换前检查必须指出它，不能放行。
            assert any(caller in line and "vars()" in line for line in check_slots_compat(cls)), name
        else:
            assert outcome == expected


def test_check_slots_compat_is_never_called_from_production_code():
    package_root = Path(fast_dataclass.__file__).resolve().parents[2]
    offenders = sorted(
        str(path.relative_to(package_root))
        for base in ("agent", "cli")
        for path in (package_root / base).rglob("*.py")
        if path.resolve() != Path(fast_dataclass.__file__).resolve() and b"check_slots_compat" in path.read_bytes()
    )
    assert offenders == []


_G1_LOCK = threading.Lock()
_G1_MAKERS: list = []


# 函数用途: 惰性生成 G1 样例 DTO：同一个 DTO 形状复制 G1_DTO_CLASS_COUNT 份，每份配一个单态构造函数（含 fast_replace）。
def _g1_makers() -> list:
    with _G1_LOCK:
        if not _G1_MAKERS:
            chunks = [_HEADER]
            for index in range(G1_DTO_CLASS_COUNT):
                chunks.append(
                    f"@fast_frozen\n@dataclass(frozen=True, slots=True)\nclass Dto{index}:\n"
                    "    request_id: str\n    attempt: int\n    status: str = 'queued'\n    detail: object = None\n\n"
                    f"def mk{index}(n):\n    return fast_replace(Dto{index}('r', n), status='done')\n\n"
                )
            module = _build_module("m00_g1_dto", "".join(chunks))
            _G1_MAKERS.extend(getattr(module, f"mk{index}") for index in range(G1_DTO_CLASS_COUNT))
        return _G1_MAKERS


# 函数用途: G1 锁等待门禁用例工厂：每个线程轮转构造 2000 个 fast_frozen 类并 fast_replace，一次调用即一个操作。
def g1_case_fast_frozen_dto():
    makers = _g1_makers()
    count = len(makers)
    counter = itertools.count()

    def operation():
        n = next(counter)
        return makers[n % count](n)

    return operation


# 函数用途: G1 用例工厂：所有线程反复替换同一个转换类（回落条件只有类型检查），一次调用即一个操作。
def g1_case_fast_replace_open_class():
    instance = ReplaceOpenDto("r", 1)
    return lambda: fast_replace(instance, status="done")


# 函数用途: G1 用例工厂：所有线程反复替换同一个带 init=False 字段与必填 InitVar 的转换类，一次调用即一个操作。
def g1_case_fast_replace_guarded_class():
    instance = ReplaceGuardedDto("r", 2, 1)
    return lambda: fast_replace(instance, token=3)
