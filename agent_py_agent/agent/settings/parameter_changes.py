# LLM: 参数中心唯一的写入口：写用户配置文件（当前进程实际加载的配置，见 user_config_capability.user_config_path），
#   以及白名单里的模型档案字段（PROFILE_FIELDS，经 model_profiles 唯一的锁内保存入口，记在该用户档案旁的 .changes.jsonl，
#   与配置账本同一格式、同一脱敏与回滚规则）。配置写入按登记表类型渲染值（布尔、数字不加引号），写后用正式 load_config
#   回读核对，不一致就恢复原文件并报错；每次写入追加一条
#   账本（用户配置旁的 settings-changes.jsonl，保留最近 500 条）。模型始终不能写边界；仅已认证用户 /settings 可在限定作用域
#   修改 USER_SETTINGS_BOUNDARY_KEYS，set/reset/revert 共用校验。列表/映射与随包默认 YAML 拒绝。副作用：改写用户配置与账本。
#   gateway_parts/settings_control_service.py、user_config_capability.set_tunable_value 与 settings/reasoning_probe.py。
# 模块用途: 让用户与 my-agent 修改参数、恢复默认、查看修改记录并按编号回滚。
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..common.json_io import append_jsonl_capped, locked_json_path, read_jsonl_objects_report
from .config_io import set_simple_yaml_raw, unset_simple_yaml_value
from .parameter_registry import (
    SOURCE_AGENT,
    SOURCE_CAPABILITY,
    SOURCE_RUNTIME_GUARD,
    ParameterSpec,
    applied_value_with,
    parameter_registry,
)
from .user_config_capability import (
    BOUNDARY_KEYS,
    TUNABLE_KEYS,
    USER_SETTINGS_BOUNDARY_KEYS,
    effect_text,
    mask_value,
    packaged_config_path,
)

LEDGER_NAME = "settings-changes.jsonl"
# 设置修改台账最多保留 500 条：超限轮转，防 JSONL 无限增长。
_MAX_LEDGER_RECORD_COUNT = 500
# 设置修改台账单条文本最多 500 字符：防超长记录撑爆台账。
_MAX_TEXT_CHARS = 500
_TRUE_WORDS = frozenset({"true", "1", "yes", "on", "开", "开启", "是"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off", "关", "关闭", "否"})
_BOUNDARY_REASON = "属于安全边界（凭据、权限、身份、路径、外部地址、会运行代码的服务或插件等），不能由模型或聊天命令修改"
# 可经参数中心修改并记账的模型档案字段（其余字段只能在 /model 里编辑）；取值由档案 schema（validate_model）校验。
PROFILE_FIELDS = frozenset({"reasoning_control"})
_USER_SETTINGS_WRITE: ContextVar[bool] = ContextVar("user_settings_write", default=False)


# LLM: 只能由 Gateway 已认证管理员控制入口进入；授权随调用退出还原，不接收客户端字段，不把 origin.actor 当授权。
#   唯一例外（用户 10-02 拍板）：settings.embedding_selection 在结构化规则全部满足后进入——本机管理员、目标是本人目录里的
#   嵌入档案、端点主机与默认对话模型相同——只写 embedding_model_profile 与 memory_semantic_recall 两项，不扩到别的边界键。
# 函数用途: 给用户 /settings 一个短暂的边界写作用域，模型工具与回滚默认仍拒绝。
@contextmanager
def user_settings_write_scope():
    token = _USER_SETTINGS_WRITE.set(True)
    try:
        yield
    finally:
        _USER_SETTINGS_WRITE.reset(token)


# LLM: 谁发起、为什么；actor 用固定短标签（model / chat / cli / test），reason 只给人看、截断后记账，不参与任何判断。
# 类用途: 一次参数修改的来源。
@dataclass(frozen=True)
class ChangeOrigin:
    actor: str
    reason: str = ""


# LLM: 写入口要写哪个文件由参数来源决定：agent 主配置写在用户配置（user_path），capability 写在运行时实际读取的
#   那份文件（capability_path，由调用方按 capability_config_for_agent 同一路径解析，不是用户配置同目录）。
#   runtime_guard 运行时没有用户覆盖层，写入口整体拒绝，不在这里给路径。
# 类用途: 一次参数修改可能涉及的各来源文件路径。
@dataclass(frozen=True)
class WritePaths:
    user_path: Path | None
    capability_path: Path | None = None


# LLM: 账本里的一行在写入前的内容；previous/value 是配置文件里的原样标量文本（None 表示没有覆盖），凭据类由 _record 脱敏。
#   target 只在修改对象不是用户配置文件时给出（模型档案：{kind, profile_id, model_name}），回滚据此写回原处。
# 类用途: 一条待记账的参数修改。
@dataclass(frozen=True)
class _ChangeRow:
    action: str
    previous: str | None
    value: str | None
    origin: ChangeOrigin
    reverts: str = ""
    target: dict | None = None


# LLM: 一次模型档案字段修改的目标；value 为 None 表示删掉这个字段、回到档案默认（reasoning_control 删掉即 auto）。
# 类用途: 要修改哪个模型档案的哪个字段、改成什么。
@dataclass(frozen=True)
class ProfileFieldChange:
    profile_id: str
    field: str
    value: str | None


# LLM: 只携带稳定错误码与给人看的中文说明；调用方把它转成 ok=False 的回执，不带路径或堆栈之外的秘密。
# 类用途: 参数修改被拒绝或没能生效。
class ParameterChangeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 随包默认 YAML 不是用户配置；没有路径或文件不存在都拒绝写，不自动创建。只用于 agent 主配置。
# 函数用途: 确认写入目标是存在的用户配置文件。
def _target(user_path: Path | None) -> Path:
    if user_path is None:
        raise ParameterChangeError("USER_CONFIG_MISSING", (
            "当前进程没有用户配置文件位置（没有加载用户配置，环境变量 MY_AGENT_CONFIG 也未设置），"
            f"不能安全写入；请人工编辑随包默认配置：{packaged_config_path()}"))
    path = Path(user_path).expanduser()
    if path.resolve() == packaged_config_path().resolve():
        raise ParameterChangeError("USER_CONFIG_IS_PACKAGED", "当前加载的是随包默认配置，升级会被覆盖，不能写入；请用独立的用户配置文件。")
    if not path.is_file():
        raise ParameterChangeError("USER_CONFIG_MISSING", f"用户配置文件不存在：{path}")
    return path


# LLM: 按参数来源给写入目标。agent 主配置必须已存在；capability 写运行时实际读取的那份文件
#   （agent.capability_config_path 或 default_capability_config_path(agent.root)，由调用方解析传入），
#   文件不存在时允许新建（运行时读不到时按默认实例走，所以建好文件后重启就能读到）。runtime_guard
#   运行时没有用户覆盖层，写什么都不会生效，这里直接拒绝，不给目标路径。
# 函数用途: 按参数来源给出实际写入目标（主配置=用户配置文件，capability=运行时读取的文件）。
def _write_target(spec: ParameterSpec, paths: WritePaths) -> Path:
    if spec.source == SOURCE_AGENT:
        return _target(paths.user_path)
    if spec.source == SOURCE_CAPABILITY:
        path = paths.capability_path
        if path is None:
            raise ParameterChangeError("USER_CONFIG_MISSING",
                                       "当前进程没有 capability 配置的运行时路径，不能安全写入。")
        target = Path(path).expanduser()
        from ..capability.runtime_config_reload import bundled_capability_config_path

        # 随包默认文件只读、永不被写（P18 验收：开发模式下 root 是仓库目录时旧第一候选就是随包默认）。
        if target.resolve() == bundled_capability_config_path().resolve():
            raise ParameterChangeError(
                "CAPABILITY_IS_PACKAGED",
                "capability 随包默认文件只读，不能写入；用户配置请放在 <owner home>/config/capability_config.yaml。")
        return target
    raise ParameterChangeError(
        "PARAMETER_SOURCE_READ_ONLY",
        f"'{spec.source}' 配置运行时只读随包文件、没有用户覆盖层，改了也不会生效；只能查看和搜索，不能修改。")


# LLM: 先查登记表，再查边界：BOUNDARY_KEYS 给出原因原样回显；其余边界项给统一原因。只有可信用户命令作用域且在
#   用户白名单（USER_SETTINGS_BOUNDARY_KEYS）里的边界项可由用户写；模型的 set/reset/revert 一律按边界拒绝。
#   运行时没有覆盖层的来源（runtime_guard）先于安全边界拒绝，原因要结构化（PARAMETER_SOURCE_READ_ONLY）。
# 函数用途: 取出一个可写参数的登记信息，不可写时拒绝。
def _writable_spec(key: str) -> ParameterSpec:
    spec = parameter_registry().get(str(key or "").strip())
    if spec is None:
        raise ParameterChangeError("PARAMETER_UNKNOWN", f"没有名为 '{key}' 的参数；可以先搜索参数名。")
    if spec.source == SOURCE_RUNTIME_GUARD:
        raise ParameterChangeError(
            "PARAMETER_SOURCE_READ_ONLY",
            f"'{spec.key}' 属于 {spec.source} 配置：运行时只读随包文件、没有用户覆盖层，改了也不会生效；只能查看和搜索。")
    user_allowed = _USER_SETTINGS_WRITE.get() and spec.key in USER_SETTINGS_BOUNDARY_KEYS
    if not spec.writable and not user_allowed:
        reason = BOUNDARY_KEYS.get(spec.key, _BOUNDARY_REASON)
        raise ParameterChangeError("PARAMETER_BOUNDARY", f"'{spec.key}' 属于安全边界，不能由模型自行修改：{reason}")
    return spec


# LLM: 显式白名单项先用原校验（如百分比区间）；其余按字段类型解析：布尔认常见中英文词，整数/小数默认非负时拒绝负数，
#   文本拒绝换行和引号且限 500 字；列表/映射不在聊天里改。返回（期望生效值，写入文本）。
# 函数用途: 把用户给的值解析并渲染成可写入的 YAML 标量。
def _render(spec: ParameterSpec, value: object) -> tuple[object, str]:
    text = str(value if value is not None else "").strip()
    if spec.key in TUNABLE_KEYS:
        ok, error, normalized = TUNABLE_KEYS[spec.key].validate(value)
        if not ok:
            raise ParameterChangeError("PARAMETER_INVALID", f"'{spec.key}' 取值不合法：{error}")
        text = str(normalized)
    if spec.value_type == "bool":
        word = text.lower()
        if word not in _TRUE_WORDS | _FALSE_WORDS:
            raise ParameterChangeError("PARAMETER_INVALID", f"'{spec.key}' 取值不合法：必须是 true 或 false")
        return word in _TRUE_WORDS, "true" if word in _TRUE_WORDS else "false"
    if spec.value_type in {"int", "float"}:
        return _render_number(spec, text)
    if spec.value_type == "str":
        if len(text) > _MAX_TEXT_CHARS or any(mark in text for mark in ('"', "'", "\n", "\r")):
            raise ParameterChangeError("PARAMETER_INVALID", f"'{spec.key}' 取值不合法：文本不能含引号或换行，且不超过 {_MAX_TEXT_CHARS} 字")
        return text, f'"{text}"'
    raise ParameterChangeError("PARAMETER_STRUCTURED", f"'{spec.key}' 是 {spec.value_type} 结构，请直接编辑用户配置文件。")


# 函数用途: 解析整数或小数参数；默认值非负时拒绝负数，小数拒绝 NaN/无穷。
def _render_number(spec: ParameterSpec, text: str) -> tuple[object, str]:
    try:
        number = int(text) if spec.value_type == "int" else float(text)
    except ValueError:
        raise ParameterChangeError("PARAMETER_INVALID", f"'{spec.key}' 取值不合法：必须是{'整数' if spec.value_type == 'int' else '数字'}") from None
    if isinstance(number, float) and not math.isfinite(number):
        raise ParameterChangeError("PARAMETER_INVALID", f"'{spec.key}' 取值不合法：必须是有限数字")
    if isinstance(spec.default, (int, float)) and spec.default >= 0 and number < 0:
        raise ParameterChangeError("PARAMETER_INVALID", f"'{spec.key}' 取值不合法：不能是负数")
    return number, str(number)


# LLM: 用正式加载器读出进程重启后会拿到的值；这是“改了是否真的生效”的唯一判据。主配置用 load_config，
#   capability 用 load_capability_config（capability 目标路径是运行时实际读取的那份，见 _write_target），
#   runtime_guard 用 runtime_guard_policy（values 字典）。
# 函数用途: 读取目标文件经完整加载后的某个参数值（按来源选加载器）。
def _effective(path: Path, key: str) -> object:
    source = getattr(parameter_registry().get(key), "source", SOURCE_AGENT)
    if source == SOURCE_CAPABILITY:
        from ..capability.config import load_capability_config

        return getattr(load_capability_config(path), key)
    if source == SOURCE_RUNTIME_GUARD:
        from .runtime_guard_config import runtime_guard_policy

        return runtime_guard_policy(path=path).values.get(key)
    from .config import load_config

    return getattr(load_config(path), key)


# LLM: 类型也必须一致：小数目标读回来必须真是数字（不能是 "0.9" 这样的字符串，否则供应商会收到字符串参数）。
# 函数用途: 判断加载后的值是否就是想写入的值。
def _same(expected: object, actual: object) -> bool:
    if isinstance(expected, float):
        numeric = isinstance(actual, (int, float)) and not isinstance(actual, bool)
        return numeric and math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-12)
    return type(expected) is type(actual) and expected == actual


# 函数用途: 账本路径：用户配置文件旁的 settings-changes.jsonl。
def ledger_path(user_config: Path) -> Path:
    return Path(user_config).with_name(LEDGER_NAME)


# 函数用途: 追加一条用户配置参数的修改记录（账本在用户配置旁）并返回它。副作用：追加账本。
def _record(path: Path, spec: ParameterSpec, row: _ChangeRow) -> dict[str, object]:
    return _append_record(ledger_path(path), spec.key, row, spec.masked)


# LLM: 配置参数与模型档案字段共用的记账：所有值都经 mask_value 结构脱敏后才写（凭据、网址密码、请求头/环境变量等）；
#   脱敏改动了值的记录标 masked，回滚据此拒绝。reason 截断到 200 字，target 与 reverts 有值才写。副作用：追加 ledger。
# 函数用途: 往指定账本追加一条修改记录并返回它。
def _append_record(ledger: Path, key: str, row: _ChangeRow, masked: bool) -> dict[str, object]:
    previous, value = _ledger_text(key, row.previous), _ledger_text(key, row.value)
    entry: dict[str, object] = {
        "id": uuid4().hex[:12], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key": key, "action": row.action, "actor": str(row.origin.actor or "unknown")[:40],
        "reason": str(row.origin.reason or "")[:200],
        # 脱敏改动了任何一个值，这条记录就不能回滚（回滚会把 *** 写回配置）
        "masked": masked or previous != row.previous or value != row.value,
        "previous": previous, "value": value,
    }
    entry.update({name: item for name, item in (("target", row.target), ("reverts", row.reverts)) if item})
    _ensure_private_file(ledger)
    append_jsonl_capped(ledger, entry, max_records=_MAX_LEDGER_RECORD_COUNT)
    return entry


def _receipt(path: Path, spec: ParameterSpec, entry: dict[str, object], actual: object) -> dict[str, object]:
    saved = entry.get("value")
    return {
        "ok": True, "key": spec.key, "action": entry["action"], "change_id": entry["id"], "saved_to": str(path),
        "previous": entry.get("previous"), "saved": saved if saved is None else str(saved).strip('"'),
        "written_value_matches": True, "effective": mask_value(spec.key, actual), "effect_when": spec.effect,
        "effect_text": effect_text(spec.effect), "note": "当前进程仍在使用启动时加载的值，请按 effect_text 的时机生效。",
    }


def _failure(error: ParameterChangeError) -> dict[str, object]:
    report: dict[str, object] = {"ok": False, "code": error.code, "error": str(error)}
    if error.code == "PARAMETER_BOUNDARY":
        report["boundary_reason"] = str(error).split("：", 1)[-1]
    return report


# LLM: 写前保留原文（capability 文件原本不存在时记空串），回读不一致即恢复原文件（新建的删掉）并报 PARAMETER_NOT_EFFECTIVE。
#   capability 目标文件不存在时新建：运行时读不到配置时按默认实例走，建好文件后重启 Gateway 就能读到，
#   所以允许从空文件开始，只写被改的键。账本统一记在用户配置（agent_config）旁，不随 capability 文件位置走。
# 函数用途: 修改一个参数并返回结构化回执。
def set_parameter(key: object, value: object, *, paths: WritePaths, origin: ChangeOrigin) -> dict[str, object]:
    try:
        spec = _writable_spec(str(key or ""))
        path = _write_target(spec, paths)
        expected, rendered = _render(spec, value)
        existed = path.is_file()
        original = path.read_text(encoding="utf-8") if existed else ""
        if not existed:
            _create_config_file(path)
        previous, _line = set_simple_yaml_raw(path, spec.key, rendered)
        actual = _effective(path, spec.key)
        if not _same(expected, actual):
            _restore(path, original, existed)
            raise ParameterChangeError("PARAMETER_NOT_EFFECTIVE",
                                       f"'{spec.key}' 写入后加载得到的值与目标不一致，已恢复原文件，没有生效。")
        ledger = paths.user_path if paths.user_path is not None else path
        return _receipt(path, spec, _record(ledger, spec, _ChangeRow("set", previous, rendered, origin)), actual)
    except ParameterChangeError as error:
        return _failure(error)
    except (OSError, ValueError) as error:
        return _failure(ParameterChangeError("USER_CONFIG_WRITE_FAILED", f"写入失败：{error}"))


# LLM: 参数中心新建的配置文件可能放凭据类键（capability 来源也会走到这里），一律以 0600 新建；已存在就报错交给调用方，
#   调用方只在确认不存在时才调用。之后的写回由 config_io 保留这个权限。
# 函数用途: 以仅本人可读写的权限新建一份只含说明行的配置文件。副作用：新建文件。
def _create_config_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# 由参数中心创建（只含被改的键，其余按随包默认）\n")


# LLM: 修改账本和配置放在同一目录，记录里有脱敏前后的键名与原因；首次创建时以 0600 建空文件，
#   之后 append_jsonl_capped 的原子重写会保留这个权限（json_io._keep_target_mode）。已存在时不改。
# 函数用途: 确保账本文件存在且新建时只有本人可读写。副作用：可能新建空文件。
def _ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return


# LLM: 文件原本不存在时恢复 = 删除新建文件，避免留下半截配置；原本存在则原样写回。
# 函数用途: 把写入失败/不一致的目标文件恢复成写入前的样子。
def _restore(path: Path, original: str, existed: bool) -> None:
    if existed:
        path.write_text(original, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


# LLM: 删除用户配置里的覆盖行，让参数回到随包默认；本来就没有覆盖时拒绝。capability 文件不存在 = 没有覆盖。
#   副作用：写用户配置（或 capability 运行时文件）与账本。
# 函数用途: 把一个参数恢复成默认值并返回结构化回执。
def reset_parameter(key: object, *, paths: WritePaths, origin: ChangeOrigin) -> dict[str, object]:
    try:
        spec = _writable_spec(str(key or ""))
        path = _write_target(spec, paths)
        if not path.is_file():
            raise ParameterChangeError("PARAMETER_NOT_OVERRIDDEN", f"'{spec.key}' 没有用户覆盖，已经是默认值。")
        previous = unset_simple_yaml_value(path, spec.key)
        if previous is None:
            raise ParameterChangeError("PARAMETER_NOT_OVERRIDDEN", f"'{spec.key}' 没有用户覆盖，已经是默认值。")
        actual = _effective(path, spec.key)
        ledger = paths.user_path if paths.user_path is not None else path
        return _receipt(path, spec, _record(ledger, spec, _ChangeRow("reset", previous, None, origin)), actual)
    except ParameterChangeError as error:
        return _failure(error)
    except (OSError, ValueError) as error:
        return _failure(ParameterChangeError("USER_CONFIG_WRITE_FAILED", f"写入失败：{error}"))


# LLM: 记账用的文本：None 表示“默认值/没有覆盖”，原样保留；其余一律结构脱敏。
# 函数用途: 把要写进修改记录的一个值脱敏。
def _ledger_text(key: str, value: str | None) -> str | None:
    return None if value is None else mask_value(key, value)


# LLM: 历史记录的回显出口：previous/value 再过一遍 mask_value（旧记录可能写于脱敏收紧之前，脱敏对已遮住的值不再改变）。
#   revert_change 内部读 parameter_history 的原记录，不经过这里。
# 函数用途: 返回一条修改记录供展示的脱敏副本。
def displayed_change(item: dict[str, object]) -> dict[str, object]:
    key = str(item.get("key") or "")
    return {**item, **{field: mask_value(key, item[field]) for field in ("previous", "value") if item.get(field) is not None}}


# LLM: 只在回执 ok 且该参数登记了派生规则时给结果；新值取回执的 saved（恢复默认或回滚到默认时为 None，改用登记表默认值），
#   交给 parameter_registry.applied_value_with 按同一派生规则算。config 决定按哪个模型算（调用方传）。只读。
# 函数用途: 给修改、恢复默认、回滚的回执算出“新值在这个模型上的实际效果”。
def applied_after_change(report: dict[str, object], config: object) -> tuple[object, str] | None:
    spec = parameter_registry().get(str(report.get("key") or "")) if report.get("ok") else None
    if spec is None:
        return None
    saved = report.get("saved")
    return applied_value_with(spec.key, spec.default if saved is None else saved, config)


# LLM: 坏行由 read_jsonl_objects_report 跳过；按时间倒序，key 为空时返回全部参数，limit 为 0 表示不限。只读。
# 函数用途: 读取参数修改记录（最新在前）。
def parameter_history(*, user_path: Path | None, key: str = "", limit: int = 20) -> list[dict[str, object]]:
    if user_path is None or not ledger_path(Path(user_path)).is_file():
        return []
    records = read_jsonl_objects_report(ledger_path(Path(user_path)), context="settings.changes").records
    selected = [item for item in reversed(records) if not key or item.get("key") == key]
    return selected[:limit] if limit else selected


# LLM: 编号可用至少 6 位前缀，不唯一或找不到都拒绝；凭据类记录只有脱敏值，不能回滚。回滚本身也记账并可再回滚。
#   写入沿用 set 的原文恢复规则：原值为空（原来没有覆盖）时删除覆盖行；capability 文件原本不存在且目标也是默认
#   时按没有覆盖拒绝。副作用：写用户配置（或 capability 运行时文件）与账本。
# 函数用途: 把某次修改撤销，恢复到那次修改之前的值。
def revert_change(change_id: str, *, paths: WritePaths, origin: ChangeOrigin) -> dict[str, object]:
    try:
        user_path = paths.user_path if paths.user_path is not None else Path()
        entry = _find_change(parameter_history(user_path=user_path, limit=0), change_id)
        return _revert_entry(paths, entry, origin)
    except ParameterChangeError as error:
        return _failure(error)
    except (OSError, ValueError) as error:
        return _failure(ParameterChangeError("USER_CONFIG_WRITE_FAILED", f"写入失败：{error}"))


# LLM: 回滚单条记录的核心写入：先把当前配置值写回 previous（或删除覆盖行），再用正式加载器回读核对。
#   凭据类记录只有脱敏值、不能回滚；capability 文件原本不存在且目标就是默认时按没有覆盖拒绝。
#   嵌套用早返回压平：masked、无覆盖、回读没变化各自提前抛错，不把 if 叠起来。副作用：写配置与账本。
# 函数用途: 执行一条修改记录的回滚写入与回读核对，返回结构化回执。
def _revert_entry(paths: WritePaths, entry: dict[str, object], origin: ChangeOrigin) -> dict[str, object]:
    spec = _writable_spec(str(entry.get("key") or ""))
    path = _write_target(spec, paths)
    if entry.get("masked"):
        raise ParameterChangeError("CHANGE_MASKED", f"'{spec.key}' 是凭据类参数，记录里只有脱敏值，不能回滚；请重新设置。")
    target = entry.get("previous")
    existed = path.is_file()
    original = path.read_text(encoding="utf-8") if existed else ""
    if not existed and target is None:
        raise ParameterChangeError("PARAMETER_NOT_OVERRIDDEN", f"'{spec.key}' 当前已经是默认值，无需回滚。")
    if not existed:
        _create_config_file(path)
    current = unset_simple_yaml_value(path, spec.key) if target is None else set_simple_yaml_raw(path, spec.key, str(target))[0]
    actual = _effective(path, spec.key)
    if target is None and current is None:
        _restore(path, original, existed)
        raise ParameterChangeError("PARAMETER_NOT_OVERRIDDEN", f"'{spec.key}' 当前已经是默认值，无需回滚。")
    row = _ChangeRow("revert", current, None if target is None else str(target), origin, reverts=str(entry["id"]))
    entry_out = _record(paths.user_path if paths.user_path is not None else path, spec, row)
    return _receipt(path, spec, entry_out, actual)


# LLM: 编号至少 6 位前缀，在给定记录里找唯一一条；找不到或不唯一都拒绝。只读。
# 函数用途: 按修改编号找到一条修改记录。
def _find_change(records: list[dict[str, object]], change_id: str) -> dict[str, object]:
    ref = str(change_id or "").strip().lower()
    matches = [item for item in records if str(item.get("id", "")).startswith(ref)]
    if len(ref) < 6 or not matches:
        raise ParameterChangeError("CHANGE_NOT_FOUND", f"没有编号以 {ref} 开头的修改记录（至少输入 6 位）。")
    if len(matches) > 1:
        raise ParameterChangeError("CHANGE_AMBIGUOUS", f"编号 {ref} 对应多条修改记录，请多输入几位。")
    return matches[0]


# LLM: 每个用户一份，位置由可信 home 身份决定（与 model_profiles_path 同一摘要），不接受调用方指定路径。
# 函数用途: 模型档案字段修改记录的位置：该用户模型档案文件旁的 .changes.jsonl。
def profile_ledger_path(home_paths: object) -> Path:
    from .model_profiles import model_profiles_path

    return model_profiles_path(home_paths).with_suffix(".changes.jsonl")


# LLM: 决策设置（模型档案同目录）的修改也进同一份档案账本，供 /settings history 一类的入口展示；reason 只记录与展示，
#   不参与任何机器判断；超长在 _append_record 截断到 200 字。key 固定为 decision_settings，action 区分 patch/reset。
# 函数用途: 追加一条决策设置修改记录并返回它。副作用：追加档案账本。
def record_decision_change(home_paths: object, *, operation: str, fields: list[str], reason: str) -> dict[str, object]:
    return _append_record(
        profile_ledger_path(home_paths),
        "decision_settings",
        _ChangeRow(
            f"decision_{operation}",
            None,
            ",".join(fields) or "(no fields)",
            ChangeOrigin("model", str(reason or "")),
        ),
        False,
    )


# LLM: 坏行由 read_jsonl_objects_report 跳过；按时间倒序。只读。
# 函数用途: 读取当前用户的模型档案字段修改记录（最新在前）。
def profile_change_history(home_paths: object) -> list[dict[str, object]]:
    ledger = profile_ledger_path(home_paths)
    if not ledger.is_file():
        return []
    return list(reversed(read_jsonl_objects_report(ledger, context="settings.profile_changes").records))


# LLM: 只改 PROFILE_FIELDS 里的字段，只改当前用户自己的档案（部署默认与共享模型不在这里，按 PROFILE_NOT_FOUND 拒绝）；
#   锁内读改写整行并经 validate_model 校验，再往该用户的档案账本追加一条带 target 的记录。副作用：改写模型档案文件与档案账本。
# 函数用途: 修改一个模型档案字段并返回结构化回执（含修改编号，可用 revert_profile_change 撤销）。
def set_profile_field(agent: object, change: ProfileFieldChange, *, origin: ChangeOrigin) -> dict[str, object]:
    return _profile_change(agent, change, _ChangeRow("set", None, change.value, origin))


# LLM: 按编号在当前用户的档案账本里找记录，把字段写回那次修改之前的值（None 即删掉字段）；回滚本身也记账、可再回滚。
#   只能找到本人账本里的记录，碰不到其他用户的档案。副作用：改写模型档案文件与档案账本。
# 函数用途: 撤销一次模型档案字段修改。
def revert_profile_change(agent: object, change_id: str, *, origin: ChangeOrigin) -> dict[str, object]:
    try:
        entry = _find_change(profile_change_history(agent.home_paths), change_id)
        if entry.get("masked"):
            raise ParameterChangeError("CHANGE_MASKED", "这条记录里只有脱敏值，不能回滚；请重新设置。")
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        change = ProfileFieldChange(str(target.get("profile_id") or ""), str(entry.get("key") or ""), entry.get("previous"))
        return _profile_change(agent, change, _ChangeRow("revert", None, change.value, origin, reverts=str(entry["id"])))
    except ParameterChangeError as error:
        return _failure(error)
    except (OSError, ValueError) as error:
        return _failure(ParameterChangeError("PROFILE_WRITE_FAILED", f"读取修改记录失败：{error}"))


# LLM: 写档案与记账的共同部分；previous 以锁内读到的原值为准（不信调用方），写失败不记账。
# 函数用途: 执行一次档案字段修改并记账，返回回执。
def _profile_change(agent: object, change: ProfileFieldChange, row: _ChangeRow) -> dict[str, object]:
    try:
        previous, model_name = _write_profile_field(agent, change)
        target = {"kind": "model_profile", "profile_id": change.profile_id, "model_name": model_name}
        entry = _append_record(profile_ledger_path(agent.home_paths), change.field,
                               replace(row, previous=previous, target=target), False)
    except ParameterChangeError as error:
        return _failure(error)
    except (OSError, ValueError) as error:
        return _failure(ParameterChangeError("PROFILE_WRITE_FAILED", f"写入失败：{error}"))
    return {"ok": True, "key": change.field, "action": entry["action"], "change_id": entry["id"],
            "profile_id": change.profile_id, "model_name": model_name, "previous": entry["previous"],
            "saved": entry["value"], "effect_text": "下一轮对话起生效（每轮都会重新读取模型档案）。"}


# LLM: 必须持模型档案文件锁完成读改写，保存走 model_profiles._save_profiles（原子替换并轮换代次，与 /model 编辑同一入口）；
#   字段值由 validate_model 校验，档案不存在时报 PROFILE_NOT_FOUND。返回（原值, 模型名）。副作用：改写模型档案文件。
# 函数用途: 在锁内修改当前用户某个模型档案的一个字段。
def _write_profile_field(agent: object, change: ProfileFieldChange) -> tuple[str | None, str]:
    from .model_profiles import _save_profiles, model_profiles_path, read_model_profiles
    from .model_provider_schema import validate_model

    if change.field not in PROFILE_FIELDS:
        raise ParameterChangeError("PROFILE_FIELD_UNKNOWN", f"模型档案字段 '{change.field}' 不能经参数中心修改，请在 /model 里编辑。")
    path = model_profiles_path(agent.home_paths)
    missing = ParameterChangeError("PROFILE_NOT_FOUND", "当前用户没有这个模型档案（部署默认模型与共享模型不在个人档案里）。")
    if not path.is_file():
        raise missing
    with locked_json_path(path):
        data = read_model_profiles(path)
        row = data["profiles"].get(change.profile_id)
        if row is None:
            raise missing
        kept = {name: value for name, value in row.items() if name != change.field}
        data["profiles"][change.profile_id] = validate_model({**kept, **({} if change.value is None else {change.field: change.value})})
        _save_profiles(path, data)
    return row.get(change.field), str(row.get("model_name") or "")


__all__ = [
    "LEDGER_NAME",
    "PROFILE_FIELDS",
    "ChangeOrigin",
    "ParameterChangeError",
    "ProfileFieldChange",
    "WritePaths",
    "displayed_change",
    "ledger_path",
    "parameter_history",
    "profile_change_history",
    "profile_ledger_path",
    "record_decision_change",
    "reset_parameter",
    "revert_change",
    "revert_profile_change",
    "set_parameter",
    "set_profile_field",
]
