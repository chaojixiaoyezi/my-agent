# LLM: Conveyor M05 / R3-W1 Cure-A（HTTP poll 路径，S50-02/S50-03）：/status、/client/* 与控制轮询每次都构造冻结 DTO。
#   3.14t 关闭 GIL 时，dataclasses 生成的冻结 __init__ 逐字段 object.__setattr__ 做类型查找；请求事件元数据清洗里的
#   ABC isinstance（每次给 dict 类型取弱引用查缓存）与模块级 re.fullmatch（按元组键查 re 的共享缓存）在 128 线程下护航。
#   本模块钉住三件事：(1) 本切片转换的 18 个热类都是 @fast_frozen + slots，并与“同一类源码去掉转换后的基线 dataclass”
#   差分一致：签名与默认值、构造成败与报错文本、eq/hash/repr、字段赋值与删除的 FrozenInstanceError、copy/pickle、
#   fast_replace / dataclasses.replace / copy.replace 的结果与错误；(2) 请求事件元数据清洗与原实现逐项同值，且精确
#   dict/mappingproxy 不走 ABC 检查、校验字符串不经 re 模块缓存；(3) 给 G1 锁等待闸门提供每个转换类的用例工厂。
# 模块用途: 固定 M05 的 Cure-A 转换合同（INTERFACE_CONTRACTS C1.3、C2、C3 A1），不读 git 历史。
from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import itertools
import math
import pickle
import re
import sys
import textwrap
import types
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.common.fast_dataclass import check_slots_compat, fast_replace
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    ConversationTaskStatus,
    NamedConversationWorkStatus,
)
from agent_py_agent.agent.gateway_parts import request_event_hub
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.client_service import (
    GatewayClientHistoryResult,
    GatewayClientMemoryResult,
)
from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
from agent_py_agent.agent.gateway_parts.http_input_receipt import CoordinatorInputRequest
from agent_py_agent.agent.gateway_parts.request_event_hub import Gap, RequestEvent
from agent_py_agent.agent.gateway_parts.request_event_routes import DurableCursor
from agent_py_agent.agent.gateway_parts.status_rendering import (
    GatewayHeartbeatFacts,
    GatewayReadiness,
    GatewayReadinessWait,
    GatewayRunningReport,
    GatewayStatusRenderContext,
    _GatewayRecordFacts,
)

_RUNNING_REPORT = GatewayRunningReport(4321, True)
_READINESS = GatewayReadiness("ready", "GATEWAY_READY", 4321, True, True, "running", "state", 10.0, 12.5)
_WORK = NamedConversationWorkStatus("goal", "g-1", "running", 2.5)
_TASK_STATUS = ConversationTaskStatus(state="running", task="/Users/someone/project/a.txt 整理", queued_count=1)

# (类, 基线 d9caa95af 是否已是 slots, 样例一（可只给必填字段）, 样例二（必须给出全部 init 字段且取值与样例一不同）)。
CONVERTED: tuple[tuple[type, bool, dict, dict], ...] = (
    (ConversationControlCommand, False, {"kind": "status"}, {
        "kind": "recover", "value": "v", "operation": "dismiss", "name": "n", "duration_seconds": 60,
        "valid": False, "usage": "用法：/recover", "target_id": "t-1", "confirmation_code": "c-1",
        "message_id_explicit": True, "fork_at": 3}),
    (ConversationTaskStatus, False, {}, {
        "state": "running", "task": "/Users/someone/a.txt 任务", "elapsed_seconds": 1.5, "queued_count": 2,
        "recent_progress": "读取中", "subagent_total": 3, "subagent_running": 1, "subagent_done": 1,
        "subagent_failed": 1, "subagent_other_labels": (("额度不足", 1),), "model_name": "m",
        "context_usage": {"tokens": 5}, "user_role": "管理员", "permission_mode": "ask",
        "im_task_status": {"pending": 1}, "compact_generation": 4, "verbose_level": "on",
        "durable_work": (_WORK,), "quarantined_wakes": 1, "monitors": {"m": 1},
        "model_timing": {"model_latency_ms": 5}, "model_usage": {"input_tokens": 3}}),
    (NamedConversationWorkStatus, False, {"kind": "audit", "name": "a", "status": "running"}, {
        "kind": "goal", "name": "g", "status": "paused", "elapsed_seconds": 9.5}),
    (ConversationControlResult, False, {"kind": "status", "ok": True, "message": "ok"}, {
        "kind": "stop", "ok": False, "message": "已停止", "request_id": "r-1", "status": _TASK_STATUS,
        "delivery_status": "accepted", "guidance_dedupe_key": "g-1", "operation_id": "op-1",
        "control_state": "stopping", "error_code": "INTERRUPTED", "details": {"a": [1]},
        "host_path_display": "full"}),
    (GatewayClientCredentials, False, {"data_root": Path("/tmp/m05-a")}, {
        "data_root": Path("/tmp/m05-b"), "configured_token": "token-b"}),
    (GatewayClientMemoryResult, False, {"operation": "recent", "ok": True}, {
        "operation": "search", "ok": False, "records": ({"id": "m1"},), "error_code": "E1",
        "status": {"state": "idle"}}),
    (GatewayClientHistoryResult, False, {"ok": True}, {
        "ok": False, "thread_id": "t-1", "turns": ({"role": "user", "text": "hi"},),
        "load_errors": ({"code": "E"},), "display_events": ({"kind": "notice"},), "message_cursor": 7,
        "before_message_cursor": 3, "has_older": True, "active_request_id": "r-9", "in_flight_status": "running",
        "turn_phase": "open", "stream_cursor": 11, "progress_path": "p", "queued_request_ids": ("q-1", "q-2")}),
    (CoordinatorInputRequest, False, {
        "prepared_request": {"id": "r-1"}, "scope": "scope-a", "request_id": "r-1", "client_input_digest": "d-1",
        "client_message_id": "m-1", "guidance_key": "", "allow_active_turn": False}, {
        "prepared_request": {"id": "r-2", "text": "x"}, "scope": "scope-b", "request_id": "r-2",
        "client_input_digest": "d-2", "client_message_id": "m-2", "guidance_key": "g-2", "allow_active_turn": True}),
    (Gap, True, {}, {"seq": 7, "reset_required": False, "reason": "overflow"}),
    (RequestEvent, True, {"seq": 1, "kind": "chunk", "request_id": "r-1", "ref": {"offset": 3, "attempt_id": "a1"}}, {
        "seq": 2, "kind": "progress", "request_id": "r-2",
        "ref": {"cursor": "v1:r-2:a2:3:10", "version": 2, "kind": "x", "secret": "dropped", "offset": math.inf}}),
    (DurableCursor, False, {"request_id": "r", "attempt_id": "a"}, {
        "request_id": "r2", "attempt_id": "a2", "line": 5, "offset": None}),
    (GatewayRunningReport, False, {"pid": 123, "alive": True}, {
        "pid": None, "alive": False, "load_error": {"code": "E"}}),
    (GatewayHeartbeatFacts, False, {"updated_at": 1.0, "age_seconds": 2.0}, {
        "updated_at": 3.5, "age_seconds": 0.25}),
    (GatewayStatusRenderContext, False, {
        "paths": "paths-a", "running_report": _RUNNING_REPORT, "status": "running", "heartbeat_at": 1.0,
        "heartbeat_age_seconds": 0.5}, {
        "paths": "paths-b", "running_report": GatewayRunningReport(None, False), "status": "stopped",
        "heartbeat_at": 0.0, "heartbeat_age_seconds": 9.0}),
    (GatewayReadiness, False, {
        "state": "starting", "reason": "GATEWAY_STARTING", "pid": 1, "process_alive": True, "ready": False,
        "status": "starting", "source": "state", "started_at": 1.0, "observed_at": 2.0}, {
        "state": "ready", "reason": "GATEWAY_READY", "pid": 2, "process_alive": False, "ready": True,
        "status": "running", "source": "heartbeat", "started_at": 3.0, "observed_at": 4.0,
        "load_errors": {"state": {"code": "E"}}, "ready_observation": {"pid": 2}, "cleanup_result": {"ok": True}}),
    (GatewayReadinessWait, False, {
        "state": "ready", "reason": "GATEWAY_READY", "ready": True, "pid": 1, "elapsed_seconds": 0.2, "polls": 1,
        "observed_alive": True, "last": _READINESS}, {
        "state": "timeout", "reason": "GATEWAY_START_TIMEOUT", "ready": False, "pid": None, "elapsed_seconds": 5.0,
        "polls": 25, "observed_alive": False, "last": GatewayReadiness(
            "stopped", "GATEWAY_NOT_READY", None, False, False, "", "", 0.0, 5.0)}),
    (_GatewayRecordFacts, False, {}, {
        "status": "running", "source": "heartbeat", "started_at": 5.0, "ready": True,
        "load_errors": {"heartbeat": {"code": "E"}}, "generation": ("g", 1), "restart_request_id": "rr-1",
        "cleanup_result": {"removed": 1}}),
    (GatewayExecutionTarget, False, {"thread_id": "t", "kind": "request", "turn_id": "r", "claim_id": "c"}, {
        "thread_id": "t2", "kind": "task", "turn_id": "run-2", "claim_id": "c2", "execution_attempt_id": "a2",
        "runtime_authority": {"run_id": "run-2"}, "task_id": "task-2"}),
)
_IDS = [cls.__qualname__ for cls, *_rest in CONVERTED]


# 函数用途: 返回类的 init 字段名（按定义顺序）；这些类都没有 InitVar 与 kw_only 字段。
def _init_names(cls: type) -> list[str]:
    return [field.name for field in dataclasses.fields(cls) if field.init]


# 函数用途: 把实例投影成可比较的状态（类限定名、逐字段值、repr）；非数据类实例原样返回。
def _state(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__qualname__,
                tuple((field.name, getattr(value, field.name)) for field in dataclasses.fields(value)), repr(value))
    return value


# 函数用途: 调用并返回可比较的结果：成功给出状态投影，失败给出异常类型与文本。
def _outcome(fn, *args, **kwargs):
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 差分比较需要捕获任意异常类型
        return ("raise", type(exc), str(exc))
    return ("ok", _state(value))


# 函数用途: 基线 __post_init__ 的 object.__setattr__ 写法，替换孪生类命名空间里属于转换类的模块级槽位写入函数。
def _object_setattr_writer(name: str):
    def write(obj, value):
        object.__setattr__(obj, name, value)
    return write


# LLM: 孪生类取自转换类所在模块的同一段源码，只去掉 @fast_frozen，并把 dataclass 参数还原为基线形状（基线已是 slots 的
#   类保留 slots=True，去掉 weakref_slot）；模块级槽位写入函数换回 object.__setattr__ 写法，其余全局名照用模块的。
#   这样差分比较的是“同一类体在转换前后”的可观察行为，类体以后改动时孪生类自动跟随，不复制源码。
# 函数用途: 构造转换类在基线形状下的普通 dataclass 孪生类。
def _baseline_twin(cls: type, *, baseline_slots: bool) -> type:
    module = sys.modules[cls.__module__]
    node = ast.parse(textwrap.dedent(inspect.getsource(cls))).body[0]
    assert isinstance(node, ast.ClassDef) and node.name == cls.__name__
    decorators = []
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "fast_frozen":
            continue
        if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "dataclass":
            keywords = [item for item in decorator.keywords if item.arg not in ("slots", "weakref_slot")]
            if baseline_slots:
                keywords.append(ast.keyword(arg="slots", value=ast.Constant(True)))
            decorator = ast.Call(func=decorator.func, args=decorator.args, keywords=keywords)
        decorators.append(decorator)
    node.decorator_list = decorators
    namespace = dict(vars(module))
    for name, value in vars(module).items():
        descriptor = getattr(value, "__self__", None)
        if isinstance(descriptor, types.MemberDescriptorType) and descriptor.__objclass__ is cls:
            namespace[name] = _object_setattr_writer(descriptor.__name__)
    body: list[ast.stmt] = [node]
    if vars(module).get("annotations") is sys.modules["__future__"].annotations:
        body.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
    tree = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(tree, module.__file__, "exec", dont_inherit=True), namespace)
    twin = namespace[cls.__name__]
    assert twin is not cls and twin.__qualname__ == cls.__qualname__
    return twin


# 函数用途: 生成一组构造实参（有效与故意无效：缺必填、未知关键字、超额位置参数、重复绑定、空调用）。
def _argument_variants(cls: type, first: dict, second: dict) -> list[tuple[tuple, dict]]:
    names = _init_names(cls)
    required = [field.name for field in dataclasses.fields(cls) if field.init
                and field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING]
    positional = tuple(second[name] for name in names)
    variants = [((), dict(first)), ((), dict(second)), (positional, {}), ((), {**first, "zz_unknown": 1}),
                ((*positional, 0), {}), ((), {})]
    if required:
        variants.append(((), {key: value for key, value in second.items() if key != required[0]}))
        variants.append((positional[:1], {names[0]: positional[0]}))
    return variants


# 函数用途: 生成一组 replace 改动（空改动、逐字段改动、全部字段、未知键）。
def _replace_variants(cls: type, second: dict) -> list[dict]:
    return [{}, *({name: second[name]} for name in _init_names(cls)), dict(second), {"zz_unknown": 1}]


@pytest.fixture(scope="module")
def twins() -> dict[type, type]:
    return {cls: _baseline_twin(cls, baseline_slots=baseline_slots) for cls, baseline_slots, _a, _b in CONVERTED}


@pytest.mark.parametrize(("cls", "baseline_slots", "first", "second"), CONVERTED, ids=_IDS)
def test_hot_http_poll_class_is_fast_frozen_with_slots(cls, baseline_slots, first, second):
    assert cls in fast_dataclass.CONVERTED_CLASSES
    assert "__slots__" in cls.__dict__
    assert not hasattr(cls(**first), "__dict__")
    # dataclasses 生成的冻结 __init__ 逐字段调用 object.__setattr__；转换后的 __init__ 只经捕获的成员描述符写槽位。
    assert "__setattr__" not in cls.__init__.__code__.co_names
    post_init = cls.__dict__.get("__post_init__")
    if post_init is not None:
        assert "__setattr__" not in post_init.__code__.co_names
    # C3 A1 的转换前提：check_slots_compat 为空（含包内调用方扫描）。
    assert check_slots_compat(cls) == []


def test_samples_cover_every_init_field():
    for cls, _baseline_slots, first, second in CONVERTED:
        assert set(second) == set(_init_names(cls)), cls
        assert set(first) <= set(_init_names(cls)), cls
        assert all(first.get(name, None) != second[name] for name in second if name in first), cls


@pytest.mark.parametrize(("cls", "baseline_slots", "first", "second"), CONVERTED, ids=_IDS)
def test_converted_class_matches_its_baseline_dataclass(twins, cls, baseline_slots, first, second):
    twin = twins[cls]
    assert ("__slots__" in twin.__dict__) is baseline_slots
    assert inspect.signature(cls) == inspect.signature(twin)
    assert cls.__init__.__defaults__ == twin.__init__.__defaults__
    assert cls.__init__.__kwdefaults__ == twin.__init__.__kwdefaults__
    assert cls.__match_args__ == twin.__match_args__
    assert [(field.name, field.default, field.default_factory, field.init, field.repr, field.compare, field.hash,
             field.kw_only) for field in dataclasses.fields(cls)] == [
        (field.name, field.default, field.default_factory, field.init, field.repr, field.compare, field.hash,
         field.kw_only) for field in dataclasses.fields(twin)]
    for args, kwargs in _argument_variants(cls, first, second):
        expected = _outcome(twin, *args, **kwargs)
        assert _outcome(cls, *args, **kwargs) == expected, (args, kwargs)
    for sample in (first, second):
        ours, theirs = cls(**sample), twin(**sample)
        assert (ours == cls(**sample)) is (theirs == twin(**sample)) is True
        assert _outcome(hash, ours) == _outcome(hash, theirs)
        assert _outcome(dataclasses.asdict, ours) == _outcome(dataclasses.asdict, theirs)
        assert _outcome(copy.copy, ours) == _outcome(copy.copy, theirs)
        assert _outcome(copy.deepcopy, ours) == _outcome(copy.deepcopy, theirs)
        for method in ("to_dict", "encode"):
            if hasattr(twin, method):
                assert _outcome(getattr(ours, method)) == _outcome(getattr(theirs, method)), method
        for name in _init_names(cls):
            assert _outcome(setattr, ours, name, 1) == _outcome(setattr, theirs, name, 1)
            assert _outcome(delattr, ours, name) == _outcome(delattr, theirs, name)
            with pytest.raises(FrozenInstanceError):
                setattr(ours, name, 1)
        # 非字段名：基线非 slots 类抛 FrozenInstanceError；slots 冻结类在 3.12 上抛 TypeError（CPython 既有行为），都不可写。
        with pytest.raises((FrozenInstanceError, TypeError, AttributeError)):
            ours.zz_extra = 1
        assert not hasattr(ours, "zz_extra")
    assert (cls(**first) == cls(**second)) is (twin(**first) == twin(**second)) is False


@pytest.mark.parametrize(("cls", "baseline_slots", "first", "second"), CONVERTED, ids=_IDS)
def test_fast_replace_matches_dataclasses_replace_on_the_baseline(twins, cls, baseline_slots, first, second):
    twin = twins[cls]
    ours, theirs = cls(**first), twin(**first)
    for changes in _replace_variants(cls, second):
        expected = _outcome(dataclasses.replace, theirs, **changes)
        assert _outcome(fast_replace, ours, **changes) == expected, changes
        # 本切片之外的调用方继续用 dataclasses.replace；转换类经快速 __init__ 构造，结果与报错不变。
        assert _outcome(dataclasses.replace, ours, **changes) == expected, changes
        assert _outcome(ours.__replace__, **changes) == expected, changes
        if hasattr(copy, "replace"):
            assert _outcome(copy.replace, ours, **changes) == _outcome(copy.replace, theirs, **changes)
    replaced = fast_replace(ours, **second)
    assert type(replaced) is cls and replaced == cls(**second)


@pytest.mark.parametrize(("cls", "baseline_slots", "first", "second"), CONVERTED, ids=_IDS)
def test_pickle_round_trip_keeps_class_and_state(cls, baseline_slots, first, second):
    for sample in (first, second):
        value = cls(**sample)
        for protocol in (2, pickle.HIGHEST_PROTOCOL):
            if cls is RequestEvent:
                # ref 是 mappingproxy，基线同样不可 pickle；转换不改变这一点。
                with pytest.raises(TypeError, match="mappingproxy"):
                    pickle.dumps(value, protocol)
                continue
            restored = pickle.loads(pickle.dumps(value, protocol))
            assert type(restored) is cls and _state(restored) == _state(value)


# 基线（d9caa95af）_safe_string 原文：每次调用拼出模式串，交给模块级 re.fullmatch。作为差分参照。
def _baseline_safe_string(value: object, *, cursor: bool = False) -> bool:
    if type(value) is not str:
        return False
    pattern = r"[A-Za-z0-9_-]+"
    if cursor:
        pattern += r"|v1(?:[.:][A-Za-z0-9_-]+)+"
    return re.fullmatch(pattern, value) is not None


# 基线（d9caa95af）_safe_ref 原文：任何输入先做 ABC isinstance(ref, Mapping)。作为差分参照。
def _baseline_safe_ref(ref: object) -> dict[str, object]:
    result: dict[str, object] = {}
    if not isinstance(ref, Mapping):
        return result
    for key in ("cursor", "offset", "version", "attempt_id", "kind"):
        try:
            value = ref.get(key)
        except Exception:  # noqa: BLE001 - 与基线一致
            continue
        if (
            type(value) in (bool, int)
            or (type(value) is float and math.isfinite(value))
            or _baseline_safe_string(value, cursor=key == "cursor")
        ):
            result[key] = value
    return result


# 类用途: 普通 Mapping 子类（走 ABC 检查的非 dict 映射）。
class _PlainMapping(Mapping):
    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


# 类用途: get 对部分键抛错的映射（清洗必须跳过该键，不中断）。
class _ExplodingMapping(_PlainMapping):
    def get(self, key, default=None):
        if key == "offset":
            raise RuntimeError("boom")
        return super().get(key, default)


# 类用途: 只经 Mapping.register 登记的虚拟子类。
class _RegisteredMapping:
    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=None):
        return self._data.get(key, default)


Mapping.register(_RegisteredMapping)


# 类用途: dict 子类（精确类型判断不命中，仍走原 ABC 路径与子类自己的 get）。
class _UpperDict(dict):
    def get(self, key, default=None):
        value = super().get(key, default)
        return value.upper() if isinstance(value, str) else value


# 类用途: str 子类（基线按 type(value) is str 拒绝，转换后也必须拒绝）。
class _Text(str):
    pass


_STRINGS = ("", "a", "A-z_09", "a b", "a/b", "a.b", "v1", "v1:a", "v1.a.b", "v1:a:b:1:20", "v1:", "v1::a", "V1:a",
            "x\n", "中文", "a\x00", "-", "_", "😀", "v1:a b", " v1:a")
_VALUES = (None, True, False, 0, -3, 2**70, 1.5, -0.0, math.inf, -math.inf, math.nan, b"a", ["a"], ("a",),
           _Text("ok"), *_STRINGS)


def test_safe_string_matches_the_baseline_regex_semantics():
    for value in _VALUES:
        for cursor in (False, True):
            assert request_event_hub._safe_string(value, cursor=cursor) is _baseline_safe_string(value, cursor=cursor), (
                value, cursor)


def test_safe_ref_matches_the_baseline_for_every_mapping_shape():
    payloads = [{key: value, "body": "x"} for key in ("cursor", "offset", "version", "attempt_id", "kind")
                for value in _VALUES]
    payloads += [{"cursor": "v1:r:a:1", "offset": 5, "version": 2, "attempt_id": "a-1", "kind": "chunk", "path": "/x"},
                 {"offset": math.nan, "version": math.inf, "kind": "a b"}, {}]
    shapes = [lambda data: data, dict, OrderedDict, MappingProxyType, _PlainMapping, _ExplodingMapping,
              _RegisteredMapping, _UpperDict]
    for payload in payloads:
        for shape in shapes:
            ref = shape(payload)
            ours = request_event_hub._safe_ref(ref)
            assert type(ours) is dict and ours == _baseline_safe_ref(ref), (shape, payload)
    for not_mapping in (None, [("offset", 1)], "offset", 5, ("offset",), {"offset"}):
        assert request_event_hub._safe_ref(not_mapping) == _baseline_safe_ref(not_mapping) == {}


# 类用途: 测试替身：任何属性访问都失败，证明请求事件清洗不再调用模块级 re 函数。
class _ModuleRegexForbidden:
    def __getattr__(self, name):
        raise AssertionError(f"re.{name} reached from the request-event hot path")


# 类用途: 测试替身的元类：isinstance 一律失败，证明精确 dict/mappingproxy 不再走 ABC 检查。
class _AbcCheckForbiddenMeta(type):
    def __instancecheck__(cls, instance):
        raise AssertionError("ABC isinstance reached from the request-event hot path")


# 类用途: 替换模块里的 Mapping 名；只允许非 dict/mappingproxy 的输入触发它。
class _AbcCheckForbidden(metaclass=_AbcCheckForbiddenMeta):
    pass


def test_request_event_sanitizer_never_consults_the_shared_regex_cache(monkeypatch):
    monkeypatch.setattr(request_event_hub, "re", _ModuleRegexForbidden())
    assert request_event_hub._safe_string("chunk") is True
    assert request_event_hub._safe_string("v1:r-1:a1:3:10", cursor=True) is True
    assert request_event_hub._safe_string("a b") is False
    event = RequestEvent(1, "chunk", "r-1", {"attempt_id": "a1", "cursor": "v1:r-1:a1:3:10", "kind": "a b"})
    assert dict(event.ref) == {"attempt_id": "a1", "cursor": "v1:r-1:a1:3:10"}


def test_request_event_sanitizer_skips_the_abc_check_for_plain_mappings(monkeypatch):
    monkeypatch.setattr(request_event_hub, "Mapping", _AbcCheckForbidden)
    assert request_event_hub._safe_ref({"offset": 1, "body": "x"}) == {"offset": 1}
    assert request_event_hub._safe_ref(MappingProxyType({"offset": 1})) == {"offset": 1}
    event = RequestEvent(1, "chunk", "r-1", {"offset": 1})
    assert dict(fast_replace(event, seq=2).ref) == {"offset": 1}
    with pytest.raises(AssertionError, match="ABC isinstance"):
        request_event_hub._safe_ref(_PlainMapping({"offset": 1}))


def test_request_event_hub_publish_keeps_sanitized_metadata():
    hub = request_event_hub.RequestEventHub(ring_events=4, topic_capacity=2, subscribers_per_topic=2, idle_seconds=1)
    subscription = hub.subscribe(("request", "r-1"))
    assert hub.publish(("request", "r-1"), "chunk", "r-1", {"offset": 7, "attempt_id": "a1", "body": "secret"}) == 1
    assert hub.publish(("request", "r-1"), "bad kind", "r-1", {}) == 0
    assert hub.publish(("request", "r-1"), "chunk", "r/1", {}) == 0
    (event,) = subscription.read()
    assert type(event) is RequestEvent and (event.seq, event.kind, event.request_id) == (1, "chunk", "r-1")
    assert type(event.ref) is MappingProxyType and dict(event.ref) == {"offset": 7, "attempt_id": "a1"}
    hub.close()


# LLM: G1 用例量的是构造：这 18 个类在 HTTP poll 热路径上每次请求做的就是构造（只有 GatewayClientHistoryResult
#   另在历史页上 fast_replace 一次）。单类 fast_replace 不登记进 G1：M00 _build_replace 生成的 __replace__ 每次调用都对
#   全线程共享的 frozenset 做 isdisjoint 与 <= changes.keys()，3.14t 关闭 GIL 时两步都要取该 frozenset 的对象临界区，
#   单类 128/16 线程吞吐比约 0.13-0.21。修复归 fast_dataclass 的所有者（M05 交接 open_followups 记录了位置与改法）。
# 函数用途: G1 工厂的公共形状：每次调用构造一次转换类（一次调用 = 一个操作）。
def _g1_operation(build):
    counter = itertools.count()

    def operation():
        return build(next(counter))

    return operation


def g1_case_conversation_control_command():
    return _g1_operation(lambda n: ConversationControlCommand("stop", operation="interrupt", fork_at=n))


def g1_case_conversation_task_status():
    return _g1_operation(lambda n: ConversationTaskStatus("running", "task", 1.0, n, durable_work=(_WORK,)))


def g1_case_named_conversation_work_status():
    return _g1_operation(lambda n: NamedConversationWorkStatus("goal", "g", "running", float(n)))


def g1_case_conversation_control_result():
    return _g1_operation(lambda n: ConversationControlResult("status", True, "ok", request_id="r", status=_TASK_STATUS))


def g1_case_gateway_client_credentials():
    root = Path("/tmp/m05-g1")
    return _g1_operation(lambda n: GatewayClientCredentials(root, "token"))


def g1_case_gateway_client_memory_result():
    records = ({"id": "m1"},)
    return _g1_operation(lambda n: GatewayClientMemoryResult("recent", True, records=records))


def g1_case_gateway_client_history_result():
    return _g1_operation(lambda n: GatewayClientHistoryResult(True, thread_id="t", message_cursor=n))


def g1_case_coordinator_input_request():
    prepared = {"id": "r-1"}
    return _g1_operation(lambda n: CoordinatorInputRequest(prepared, None, "r-1", "d", "m", "", False))


def g1_case_request_event_gap():
    return _g1_operation(lambda n: Gap(seq=n, reason="overflow"))


def g1_case_request_event():
    ref = {"offset": 3, "attempt_id": "a1", "cursor": "v1:r-1:a1:3:10"}
    return _g1_operation(lambda n: RequestEvent(n, "chunk", "r-1", ref))


def g1_case_durable_cursor():
    return _g1_operation(lambda n: DurableCursor("r-1", "a1", n, n))


def g1_case_gateway_running_report():
    return _g1_operation(lambda n: GatewayRunningReport(n, True))


def g1_case_gateway_heartbeat_facts():
    return _g1_operation(lambda n: GatewayHeartbeatFacts(float(n), 0.5))


def g1_case_gateway_status_render_context():
    return _g1_operation(lambda n: GatewayStatusRenderContext("paths", _RUNNING_REPORT, "running", float(n), 0.5))


def g1_case_gateway_readiness():
    return _g1_operation(
        lambda n: GatewayReadiness("ready", "GATEWAY_READY", n, True, True, "running", "state", 1.0, 2.0))


def g1_case_gateway_readiness_wait():
    return _g1_operation(lambda n: GatewayReadinessWait("ready", "GATEWAY_READY", True, n, 0.2, 1, True, _READINESS))


def g1_case_gateway_record_facts():
    return _g1_operation(lambda n: _GatewayRecordFacts("running", "state", float(n), True))


def g1_case_gateway_execution_target():
    authority = {"run_id": "run-1"}
    return _g1_operation(lambda n: GatewayExecutionTarget("t", "task", "run-1", "c", "a", authority, "task-1"))


# 本模块登记到 G1 CASES 的工厂（每个转换类一个）；与 test_ft_lock_wait_gate.CASES 的 m05_ 前缀条目一一对应。
G1_FACTORIES: tuple[str, ...] = tuple(name for name in tuple(globals()) if name.startswith("g1_case_"))


def test_every_converted_class_has_a_registered_g1_case():
    from agent_py_agent.tests.ft.test_ft_lock_wait_gate import CASES

    prefix = "agent_py_agent.tests.test_conveyor_m05_cure_a_http."
    registered = {dotted.rpartition(".")[2] for dotted, _label in CASES if dotted.startswith(prefix)}
    assert registered == set(G1_FACTORIES)
    built = set()
    for name in G1_FACTORIES:
        operation = globals()[name]()
        built.add(type(operation()))
    assert built == {cls for cls, *_rest in CONVERTED}
