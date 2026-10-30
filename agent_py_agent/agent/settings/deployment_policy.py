# LLM: 唯一部署策略只认宿主 Home；JSON 只留显式覆盖；提交后必须核完整目标，已替换与目录耐久性未确认不得混淆。
# 模块用途: 严格解析部署预算并提供稀疏持久快照、脱敏错误与可区分的提交确认状态。
from __future__ import annotations

import json
import math
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from functools import cache
from pathlib import Path
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from ..common.nofollow_fs import read_bytes_beneath
from ..user_space.home_layout import MyAgentHomePaths

_SCHEMA = "deployment-policy.v1"
# 部署策略源文件允许安全读取的最大字节数。
_MAX_DEPLOYMENT_POLICY_SOURCE_BYTES = 1024 * 1024


# LLM: 错误只使用固定文案及固定原因码，不能带入原策略内容、凭据或任意路径。
# 类用途: 表示部署配置无效、来源不可信、版本冲突或当前身份无权写入。
class DeploymentPolicyError(ValueError):
    # LLM: reason 由内部固定分支给出，不从正文或异常消息反推。
    # 函数用途: 生成可以公开展示的配置错误。
    def __init__(self, reason: str = "deployment_policy_invalid") -> None:
        super().__init__("部署策略无效或不可用，未采用其它配置来源。")
        self.reason = reason


# LLM: committed=True 只说明安全回读确认完整目标；None 表示未知而非未写入；异常均不承诺耐久性，不携带路径或正文。
# 类用途: 区分目标已确认但刷盘未确认与目标无法确认的部署策略提交异常。
class DeploymentPolicyCommitError(DeploymentPolicyError):
    # LLM: expected_revision 是本次目标新版本，不是调用者的旧 CAS 版本；未知阶段不能按异常文案猜测替换是否发生。
    # 函数用途: 保存安全的提交状态属性及固定原因码，提醒调用方核实后再更新。
    def __init__(self, *, expected_revision: int, committed: bool | None) -> None:
        reason = "deployment_policy_durability_unconfirmed" if committed is True else "deployment_policy_commit_unconfirmed"
        super().__init__(reason)
        self.committed = committed
        self.unconfirmed = True
        self.durability_confirmed = False
        self.expected_revision = expected_revision
        self.args = (("部署策略目标内容已确认，但耐久性未确认，请勿盲目重试。" if committed is True
                      else "部署策略提交状态未确认，请核实目标内容后再更新。"),)


# LLM: 所有嵌套策略在直接构造和 JSON 解析时都走同一严格校验；冻结对象不携带第二份可变字典。
# 类用途: 让策略字段具有真正的类型、范围与不可变序列。
class _PolicyNode:
    # LLM: 只验证内存标量，既不读文件也不纠正非法值；bool 不得冒充数字。
    # 函数用途: 拒绝非法直接构造并将合法列表冻结成元组。
    def __post_init__(self) -> None:
        hints = _policy_hints(type(self))
        for item in fields(self):
            if not item.name.startswith("_"):
                value = _typed_value(hints[item.name], getattr(self, item.name),
                                     positive=item.metadata.get("positive", False), allow_empty=item.metadata.get("allow_empty", False))
                object.__setattr__(self, item.name, value)


# LLM: 23 项守卫的默认与说明唯一归本类，保留各自 0 语义；暂态空序列关闭重试，熔断退避必须非空有限。
# 类用途: 保存运行守卫唯一的 typed 默认值与显式覆盖。
@dataclass(frozen=True)
class RuntimeGuardPolicy(_PolicyNode):
    repeat_fail_threshold: int = field(default=10, metadata={"description": "相同工具连续失败后提示换策略的次数阈值"})
    readonly_no_progress_threshold: int = field(default=3, metadata={"description": "只读工具重复无进展后的策略提示阈值"})
    repeated_success_hint_threshold: int = field(default=5, metadata={"description": "重复成功但未推进任务时的提示次数"})
    terminal_block_enabled: bool = field(default=False, metadata={"description": "是否对反复失败的终端调用实施阻断"})
    repeated_failure_halt_threshold: int = field(default=15, metadata={"description": "重复失败触发停机保护的次数阈值"})
    hard_failure_halt_enabled: bool = field(default=False, metadata={"description": "是否启用硬失败停机保护"})
    hard_failure_halt_threshold: int = field(default=15, metadata={"description": "硬失败触发停机保护的次数阈值"})
    tool_rate_window_seconds: int = field(default=60, metadata={"description": "单工具调用频率统计窗口秒数"})
    tool_rate_max_calls: int = field(default=60, metadata={"description": "单工具在统计窗口内允许的调用次数，0不限制"})
    tool_circuit_failure_threshold: int = field(default=3, metadata={"description": "工具失败触发熔断退避的次数"})
    tool_circuit_backoff_seconds: tuple[float, ...] = field(default=(1, 2, 4, 8, 16, 30), metadata={"description": "工具熔断后逐级等待的秒数，末档重复使用"})
    tool_rate_max_records: int = field(default=256, metadata={"positive": True, "description": "工具频率统计最多保留的记录条数，至少1条"})
    unknown_command_allowlist: tuple[str, ...] = field(default=(), metadata={"description": "额外允许的未知命令名称，空列表不额外放行"})
    unknown_command_window_seconds: int = field(default=600, metadata={"description": "未知命令额外频率保护的统计窗口秒数"})
    unknown_command_max_calls: int = field(default=200, metadata={"description": "未知命令窗口内允许的调用次数"})
    background_max_tool_rounds: int = field(default=5000, metadata={"description": "后台单任务工具轮数保护上限"})
    background_max_tool_calls_per_round: int = field(default=4, metadata={"description": "后台每轮允许的工具调用数量"})
    provider_transient_auto_resume_delays_seconds: tuple[float, ...] = field(default=(10, 25, 45, 100, 180), metadata={"allow_empty": True, "description": "服务商暂态失败的自动恢复等待序列，空列表关闭重试"})
    provider_transient_auto_resume_total_budget_seconds: int = field(default=1800, metadata={"description": "一次服务商暂态失败自动恢复的总等待预算"})
    provider_supply_backoff_base_seconds: int = field(default=30, metadata={"description": "服务商供给不足的初始退避秒数"})
    provider_supply_backoff_max_seconds: int = field(default=900, metadata={"description": "服务商供给不足的单次退避最长秒数"})
    provider_transient_redispatch_limit: int = field(default=8, metadata={"description": "服务商暂态失败后重新投递的次数上限"})
    main_agent_auto_resume_attempt_limit: int = field(default=3, metadata={"description": "主代理中断后自动恢复尝试次数上限"})
    _source_path: str = field(default="", init=False, repr=False, compare=False)
    _loaded_at: float = field(default=0.0, init=False, repr=False, compare=False)
    _configured_fields: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)

    # LLM: 仅维护原运行快照协议版本，不代表旧 YAML 仍是配置来源。
    # 函数用途: 返回守卫快照的稳定版本标签。
    @property
    def schema_version(self) -> str:
        return "runtime_guard_policy.v1"

    # LLM: 来源只由宿主 loader 填入，JSON 不可写该内部字段。
    # 函数用途: 给审计提供准确策略来源。
    @property
    def source_path(self) -> str:
        return self._source_path

    # LLM: 加载时间是本次快照事实，不参与配置默认或持久覆盖。
    # 函数用途: 返回守卫快照读取时间。
    @property
    def loaded_at(self) -> float:
        return self._loaded_at

    # LLM: 字典由不可变 typed 字段临时生成，修改它不会反写策略，不是第二份权威。
    # 函数用途: 为原审计及冻结调用方提供只读值投影。
    @property
    def values(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self) if item.init}

    # LLM: 区分显式宿主覆盖与 Python 默认，不能把默认伪称来自 JSON。
    # 函数用途: 返回每个字段的真实来源标签。
    @property
    def sources(self) -> dict:
        return {key: self._source_path if key in self._configured_fields else "python:RuntimeGuardPolicy" for key in self.values}

    # LLM: typed 字段已严格校验，未登记内部算法键只使用调用方原代码默认，不从文件接收新键。
    # 函数用途: 保留原整数读取 API，而不维护可变 values 配置。
    def int_value(self, key: str, default: int = 0) -> int:
        value = getattr(self, key, default)
        if type(value) is not int:
            raise DeploymentPolicyError()
        return max(0, value)

    # LLM: 不再把任意字符串猜成 bool；当前策略只接受真实布尔值。
    # 函数用途: 保留原布尔读取 API。
    def bool_value(self, key: str, default: bool = False) -> bool:
        value = getattr(self, key, default)
        if type(value) is not bool:
            raise DeploymentPolicyError()
        return value

    # LLM: 空重试序列是明确关闭，不能用 or default 偷换；非法元组在构造时已经拒绝。
    # 函数用途: 保留原序列读取 API 并返回冻结元组。
    def float_tuple_value(self, key: str, default: tuple[float, ...]) -> tuple[float, ...]:
        return tuple(getattr(self, key, default))

    # LLM: 运行快照仅投影本次值、来源和准确任务编号，不写配置或签发权限。
    # 函数用途: 为原日志和恢复链生成独立守卫快照。
    def snapshot(self, *, task_id: str = "", run_id: str = "") -> dict:
        return {"schema_version": self.schema_version, "task_id": task_id, "run_id": run_id,
                "source_path": self.source_path, "loaded_at": self.loaded_at, "values": self.values, "sources": self.sources}


# LLM: 源文件预算不是 RSS；0 只禁缓存，不删除 canonical 数据。
# 类用途: 定义候选材料缓存的准入与驱逐上界。
@dataclass(frozen=True)
class CandidateCachePolicy(_PolicyNode):
    max_paths: int = 8
    source_budget_bytes: int = 128 * 1024 * 1024


# LLM: 超大条目仍可读，只不缓存；三个容量不能互相冒充同一种限制。
# 类用途: 定义任务缓存条数、条目源字节与负缓存条数。
@dataclass(frozen=True)
class TaskCachePolicy(_PolicyNode):
    max_entries: int = 4096
    item_source_max_bytes: int = 256 * 1024
    negative_max_entries: int = 4096


# LLM: 驻留预算为估值而不是 OS 硬限制；null 不新增帽，不臆造 low preset。
# 类用途: 汇总各缓存分区与部署者显式驻留估值预算。
@dataclass(frozen=True)
class CachePolicy(_PolicyNode):
    resident_budget_bytes: int | None = None
    candidates: CandidateCachePolicy = field(default_factory=CandidateCachePolicy)
    tasks: TaskCachePolicy = field(default_factory=TaskCachePolicy)


# LLM: 普通查询只能收紧此保护；0 不能取消后备扫描的文件与墙钟上限。
# 类用途: 定义 Python 文件系统后备扫描的部署硬上界。
@dataclass(frozen=True)
class FilesystemFallbackBudget(_PolicyNode):
    max_files: int = field(default=20000, metadata={"positive": True})
    max_wall_seconds: float = field(default=10.0, metadata={"positive": True})


# LLM: null 保留原调用数值合同；显式预算必须为正，仍须与调用 deadline/取消取交集。
# 类用途: 定义查询额外部署预算，不声称这些数值已覆盖外部查询消费者。
@dataclass(frozen=True)
class QueryPolicy(_PolicyNode):
    max_scan_items: int | None = field(default=None, metadata={"positive": True})
    max_scan_bytes: int | None = field(default=None, metadata={"positive": True})
    max_wall_seconds: float | None = field(default=None, metadata={"positive": True})
    filesystem_fallback: FilesystemFallbackBudget = field(default_factory=FilesystemFallbackBudget)


# LLM: 这是一次排空总期限而非每个线程各等一遍；更早宿主 deadline 仍取 min。
# 类用途: 定义 standalone 总宽限，部署生成方可显式写入 k8s 投影值。
@dataclass(frozen=True)
class ShutdownPolicy(_PolicyNode):
    total_grace_seconds: float = 5.0


# LLM: 默认在字段定义处唯一持有；私有字符串只记录显式输入键，不成为第二份有效策略。
# 类用途: 汇总部署策略与 revision，提供保持显式默认值的稀疏序列化。
@dataclass(frozen=True)
class DeploymentPolicy(_PolicyNode):
    schema_version: str = field(default=_SCHEMA, init=False)
    revision: int = 0
    runtime_guards: RuntimeGuardPolicy = field(default_factory=RuntimeGuardPolicy)
    cache: CachePolicy = field(default_factory=CachePolicy)
    query: QueryPolicy = field(default_factory=QueryPolicy)
    shutdown: ShutdownPolicy = field(default_factory=ShutdownPolicy)
    _explicit_json: str = field(default="", init=False, repr=False, compare=False)

    # LLM: 返回独立字典；显式 0/null/默认值均保留，未配置的默认不落盘。
    # 函数用途: 生成可以保存或脱敏展示的稀疏策略快照。
    def to_dict(self) -> dict:
        body = json.loads(self._explicit_json) if self._explicit_json else _sparse_node(self)
        return {**body, "schema_version": _SCHEMA, "revision": self.revision}


# LLM: 缓存只含代码定义的注解，不含策略文件、身份或权限；同一类的类型解析只做一次。
# 函数用途: 获取 typed 字段的实际类型。
@cache
def _policy_hints(cls: type) -> dict:
    return get_type_hints(cls)


# LLM: JSON 与 Python 构造共用递归严格判据；不做字符串数值或真假值猜测，不夹取非法值。
# 函数用途: 校验并冻结一个策略字段。
def _typed_value(kind: type, value: object, *, positive: bool = False, allow_empty: bool = False):
    origin, arguments = get_origin(kind), get_args(kind)
    if origin is UnionType:
        if value is None and type(None) in arguments:
            return None
        return _typed_value(next(item for item in arguments if item is not type(None)), value, positive=positive, allow_empty=allow_empty)
    if isinstance(kind, type) and is_dataclass(kind):
        if isinstance(value, kind):
            return value
        if type(value) is not dict or set(value) - {item.name for item in fields(kind) if item.init}:
            raise DeploymentPolicyError()
        return kind(**value)
    if origin is tuple:
        if type(value) not in (tuple, list) or arguments[0] is float and not value and not allow_empty:
            raise DeploymentPolicyError()
        return tuple(_typed_value(arguments[0], item) for item in value)
    if kind is bool:
        valid = type(value) is bool
    elif kind is int:
        valid = type(value) is int and value >= (1 if positive else 0)
    elif kind is float:
        valid = type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)
    elif kind is str:
        valid = isinstance(value, str) and len(value) <= 4096 and not any(ord(char) < 32 for char in value)
    else:
        valid = False
    if not valid:
        raise DeploymentPolicyError()
    return float(value) if kind is float else value


# LLM: 仅用于没有原显式键集合的宿主直接构造；比较 typed 默认值不读取另一份配置。
# 函数用途: 从策略对象中提取非默认覆盖，序列转成 JSON 列表。
def _sparse_node(node: _PolicyNode) -> dict:
    result = {}
    for item in fields(node):
        if not item.init:
            continue
        value = getattr(node, item.name)
        default = item.default_factory() if item.default_factory is not MISSING else item.default
        if value != default:
            result[item.name] = _sparse_node(value) if isinstance(value, _PolicyNode) else list(value) if isinstance(value, tuple) else value
    return result


# LLM: 版本、未知键、嵌套类型先全部验证；保留的是原显式键，不是完整默认配置副本。
# 函数用途: 从稀疏输入构建不可变的部署策略。
def deployment_policy_from_dict(data: object) -> DeploymentPolicy:
    allowed = {item.name for item in fields(DeploymentPolicy) if item.init} | {"schema_version"}
    if type(data) is not dict or data.get("schema_version") != _SCHEMA or set(data) - allowed:
        raise DeploymentPolicyError()
    policy = DeploymentPolicy(**{key: value for key, value in data.items() if key != "schema_version"})
    explicit = {key: value for key, value in data.items() if key not in {"schema_version", "revision"}}
    object.__setattr__(policy, "_explicit_json", json.dumps(explicit, allow_nan=False))
    return policy


# LLM: 路径只从宿主标准解析对象获取，owner config_dir、cwd 与正文文件路径不接受。
# 函数用途: 返回部署策略唯一受信位置。
def deployment_policy_path(trusted_host_paths: MyAgentHomePaths) -> Path:
    if not isinstance(trusted_host_paths, MyAgentHomePaths) or trusted_host_paths.config_dir != trusted_host_paths.root / "config":
        raise DeploymentPolicyError("deployment_policy_source_invalid")
    return trusted_host_paths.config_dir / "deployment_policy.json"


# LLM: 重复 JSON 键不能被最后一个值偷偷覆盖，错误不包含键名或原值。
# 函数用途: 严格组装 JSON 对象。
def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentPolicyError()
        result[key] = value
    return result


# LLM: no-follow 有界读；唯有不存在可用 typed 默认，损坏/权限/未知版本及解码递归异常都明确失败，不写文件。
# 函数用途: 加载指定受信宿主的唯一部署策略。
def load_deployment_policy(*, trusted_host_paths: MyAgentHomePaths) -> DeploymentPolicy:
    deployment_policy_path(trusted_host_paths)
    try:
        raw = read_bytes_beneath(trusted_host_paths.root, ("config", "deployment_policy.json"),
                                 max_bytes=_MAX_DEPLOYMENT_POLICY_SOURCE_BYTES, require_dir_fd=True)
        if raw is None:
            return DeploymentPolicy()
        return deployment_policy_from_dict(json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object))
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise DeploymentPolicyError() from None


# LLM: 合并的是已验证的显式覆盖，不补满默认；原对象和调用方字典都不就地修改。
# 函数用途: 递归合并一份稀疏策略变更。
def _merge_changes(current: dict, changes: dict) -> dict:
    result = dict(current)
    for key, value in changes.items():
        result[key] = _merge_changes(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


# LLM: 复用有界 no-follow 快照读及唯一严格解码器；读前后核完整目录链和叶身份，缺失绝不采用安装默认值。
# 函数用途: 在原锁内确认完整目标及新版本，读中移除、原位篡改或任何不一致均给出未知提交回执。
def _confirm_deployment_policy_commit(*, trusted_host_paths: MyAgentHomePaths, payload: dict) -> DeploymentPolicy:
    try:
        raw = read_bytes_beneath(trusted_host_paths.root, ("config", "deployment_policy.json"),
                                 max_bytes=_MAX_DEPLOYMENT_POLICY_SOURCE_BYTES, require_dir_fd=True, snapshot_retries=1)
        if raw is None:
            raise DeploymentPolicyError()
        confirmed = deployment_policy_from_dict(json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object))
        if confirmed.revision != payload["revision"] or confirmed.to_dict() != payload:
            raise DeploymentPolicyError()
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise DeploymentPolicyCommitError(expected_revision=payload["revision"], committed=None) from None
    return confirmed


# LLM: 身份拒门先于 I/O；锁内核旧版本及完整回读目标；写入错误后先核实内容，已提交也不能冒称目录耐久性成功。
# 函数用途: 经同一部署策略写入器应用稀疏变更，拒绝并发覆盖和普通 owner 写入。
def update_deployment_policy(*, trusted_host_paths: MyAgentHomePaths, expected_revision: int, changes: dict) -> DeploymentPolicy:
    from ..common.global_write_guard import require_global_writer
    from ..common.json_io import (
        locked_json_path,
        serialize_private_json_no_newline,
        write_private_json_file_atomic_unlocked,
    )
    from ..user_space.owner_access import is_complete_local_admin_owner

    path = deployment_policy_path(trusted_host_paths)
    if not is_complete_local_admin_owner(trusted_host_paths):
        raise DeploymentPolicyError("deployment_policy_forbidden")
    require_global_writer("deployment_policy_write")
    if type(expected_revision) is not int or expected_revision < 0:
        raise DeploymentPolicyError("deployment_policy_conflict")
    if type(changes) is not dict or set(changes) & {"schema_version", "revision"}:
        raise DeploymentPolicyError()
    checked = deployment_policy_from_dict({"schema_version": _SCHEMA, **changes}).to_dict()
    checked.pop("schema_version")
    checked.pop("revision")
    with locked_json_path(path):
        current = load_deployment_policy(trusted_host_paths=trusted_host_paths)
        if current.revision != expected_revision:
            raise DeploymentPolicyError("deployment_policy_conflict")
        merged = _merge_changes(current.to_dict(), checked)
        merged["revision"] = current.revision + 1
        policy = deployment_policy_from_dict(merged)
        payload = policy.to_dict()
        encoded = (serialize_private_json_no_newline(payload) + "\n").encode("utf-8")
        if len(encoded) > _MAX_DEPLOYMENT_POLICY_SOURCE_BYTES:
            raise DeploymentPolicyError("deployment_policy_size_exceeded")
        try:
            write_private_json_file_atomic_unlocked(path, payload)
        except OSError:
            _confirm_deployment_policy_commit(trusted_host_paths=trusted_host_paths, payload=payload)
            raise DeploymentPolicyCommitError(expected_revision=policy.revision, committed=True) from None
        return _confirm_deployment_policy_commit(trusted_host_paths=trusted_host_paths, payload=payload)
