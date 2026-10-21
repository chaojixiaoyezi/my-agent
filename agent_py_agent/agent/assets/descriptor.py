# LLM: 四类资产共用纯值契约；统一脱敏保留数字预算，访问目标和凭据引用先硬拒绝原始秘密，无IO。
# 模块用途: 定义可冻结、可JSON往返的标准提取Key，供存储、适配器和拓扑共用。
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from ..common.log_redaction import redact_sensitive_value
from . import safe_asset_id

ASSET_KINDS = ("monitor", "workflow", "script", "skill")
SCHEMA_VERSION = "asset_descriptor.v1"
_TOKEN_METRICS = frozenset(('tokens', 'max_tokens', 'input_tokens', 'output_tokens', 'total_tokens',
                          'token_count', 'prompt_tokens', 'completion_tokens'))
# 短sk-前缀也可能是locale或引用名；真实密钥形状由公共redactor统一识别。
_RAW_SECRET = re.compile(r'(?i)(?:\bbearer\s+|(?:api_?key|token)\s*=|<redacted[^>]*>|\[redacted\])')
_URL_CREDENTIAL = re.compile(r'(?i)://[^/?#\s]*:[^/?#\s]*@|[?&](?:api_?key|access_token|password|secret|token)=')
_BEARER_VALUE = re.compile(r'''(?i)(\bbearer\s+)[^\s,;"'<>]+''')


# LLM: 字段秘密判定沿统一脱敏口；camelCase只规范用于判定，不改公开键名，安全计数不丢失。
# 函数用途: 判断JSON字段能否保留，避免token子串误删普通指标。
def _keep_field(key, value):
    normalized = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', key).strip().lower().replace('-', '_')
    if normalized in _TOKEN_METRICS and type(value) in (int, float):
        return True
    return normalized != 'bearer' and redact_sensitive_value(None, field_name=normalized) is None


# LLM: 字符串先走统一脱敏口，再补独立Bearer值遮蔽；JSON类型/有限数仍严格，返回独立容器且不执行正文。
# 函数用途: 过滤秘密字段及说明中的命令、URL、HTTP头和裸Bearer凭据，保留数字预算。
def clean_json(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON字段名必须是字符串")
        return {key: clean_json(item) for key, item in value.items() if _keep_field(key, item)}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    if type(value) is str:
        return _BEARER_VALUE.sub(r'\1<redacted>', redact_sensitive_value(value))
    if value is None or type(value) in (int, bool):
        return redact_sensitive_value(value)
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("资产字段必须是有限JSON值")


# LLM: dataclass注解是类型权威，不接受bool冒充计数或字符串冒充列表；嵌套对象复制后归一。
# 函数用途: 把JSON列表和嵌套对象恢复为严格契约值。
def _convert(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        for option in args:
            try:
                return _convert(value, option)
            except (ValueError, TypeError):
                continue
        raise ValueError("资产字段类型不匹配")
    if origin is tuple and isinstance(value, (list, tuple)):
        return tuple(_convert(item, args[0]) for item in value)
    if origin is dict and isinstance(value, dict) or annotation is object:
        return clean_json(value)
    if isinstance(annotation, type) and issubclass(annotation, _Record):
        return annotation.from_dict(value.to_dict() if isinstance(value, annotation) else value)
    if type(value) is annotation or annotation is float and type(value) in (int, float):
        return clean_json(value)
    raise ValueError("资产字段类型不匹配")


# LLM: 字段表、类型与枚举统一在构造和from_dict执行；实例只携带脱敏副本，不进行IO。
# 类用途: 给所有资产值对象提供严格序列化和恢复，避免各存储自行宽松解析。
class _Record:
    # LLM: dataclass生成构造器调用本钩子；冻结实例只在初始化时归一字段。
    # 函数用途: 校验并复制类型，拒绝负计数、空标识符和未知种类/评估决定。
    def __post_init__(self):
        hints = get_type_hints(type(self))
        for item in fields(self):
            value = _convert(getattr(self, item.name), hints[item.name])
            if item.name in {"id", "source_id", "target_id", "candidate_id", "step_id"}:
                safe_asset_id(value)
            if item.name in {"name", "credential_ref"} and not value.strip():
                raise ValueError("资产名称或凭据引用不能为空")
            if item.name in {"sample_count", "run_count", "consecutive_failures", "last_duration_ms"} and value is not None and value < 0:
                raise ValueError("运行计数和耗时不能为负")
            object.__setattr__(self, item.name, value)
        if isinstance(self, AssetDescriptor) and self.kind not in ASSET_KINDS:
            raise ValueError("未知资产kind")
        if isinstance(self, EvaluatedCandidate) and self.decision not in {"active", "ignored", "pending"}:
            raise ValueError("未知候选评估decision")
        if isinstance(self, AssetRefs):
            for item in fields(self):
                for value in (() if item.name == 'skill_dir' else getattr(self, item.name)):
                    safe_asset_id(value)

    # LLM: 每次投影再次脱敏，以免调用方修改可变metadata后把秘密字段带出。
    # 函数用途: 输出只含JSON值的独立字典，顶层描述符带协议版本。
    def to_dict(self):
        payload = clean_json(asdict(self))
        if isinstance(self, AssetRefs) and not self.skill_dir:
            payload.pop('skill_dir')
        if isinstance(self, AssetDescriptor):
            payload.update(refs=self.refs.to_dict(), used_by=self.used_by.to_dict())
        return {"schema_version": SCHEMA_VERSION, **payload} if isinstance(self, AssetDescriptor) else payload

    # LLM: 未知普通字段拒绝；secret键先过滤，schema_version只属于顶层描述符。
    # 函数用途: 从外部JSON安全重建本类型，不做隐式字段别名或版本迁移。
    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("资产记录必须是对象")
        values = clean_json(payload)
        if cls is AssetDescriptor and values.pop("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise ValueError("不支持的资产协议版本")
        if values.keys() - {item.name for item in fields(cls)}:
            raise ValueError("资产记录包含未知字段")
        return cls(**values)


# LLM: endpoint仅为不含认证秘密的访问说明；构造时先硬拒绝原值，避免脱敏后静默当有效配置。
# 类用途: 描述一个资产会访问的目标。
@dataclass(frozen=True)
class AccessTarget(_Record):
    target_id: str
    type: str
    endpoint: str
    method: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    # LLM: 原始URL凭据或统一脱敏命中的秘密一律拒绝；超类仍负责严格字段类型与安全ID。
    # 函数用途: 防止明文认证URL进入访问目标声明。
    def __post_init__(self):
        if isinstance(self.endpoint, str) and (_URL_CREDENTIAL.search(self.endpoint)
                or redact_sensitive_value(self.endpoint) != self.endpoint or _RAW_SECRET.search(self.endpoint)):
            raise ValueError('AccessTarget endpoint must not contain raw credentials')
        super().__post_init__()


# LLM: 评估决定仅为active/ignored/pending，供调用方避免重复试查，不直接执行源切换。
# 类用途: 保留新数据源的评估时间、原因和样本数。
@dataclass(frozen=True)
class EvaluatedCandidate(_Record):
    candidate_id: str
    decision: str
    evaluated_at: str
    reason: str = ""
    sample_count: int = 0


# LLM: query_scope是结构化JSON，候选评估独立于活跃查询范围；不调用目标系统。
# 类用途: 定义查询源、范围和已评估新源。
@dataclass(frozen=True)
class DataSourceSpec(_Record):
    source_id: str
    system: str
    query_scope: dict[str, object] = field(default_factory=dict)
    evaluated_candidates: tuple[EvaluatedCandidate, ...] = ()


# LLM: 仅持有不透明credential_ref及说明状态；所有文本字段先拒绝原始密钥，不加载真实凭据。
# 类用途: 为资产记录宿主凭据引用绑定。
@dataclass(frozen=True)
class CredentialBindingRef(_Record):
    credential_ref: str
    alias: str = ""
    authentication: str = ""
    status: str = "configured"

    # LLM: 适配现行credential_ref/alias/authentication/status，不发明credential_id或target别名；无IO。
    # 函数用途: 拒绝把密钥、授权串或含秘密URL冒充凭据引用和说明。
    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, str) and (_RAW_SECRET.search(value) or _URL_CREDENTIAL.search(value)
                    or redact_sensitive_value(value) != value):
                raise ValueError('CredentialBindingRef must not contain raw secret values')
        super().__post_init__()


# LLM: IO声明仅为JSON结构说明，不产生执行或完成权威。
# 类用途: 记录资产输入与输出契约。
@dataclass(frozen=True)
class IOContract(_Record):
    inputs: tuple[object, ...] = ()
    outputs: tuple[object, ...] = ()


# LLM: 依赖引用仅为各kind安全ID；可选skill_dir只保存原目录名作展示，不能授路径权限，空值不输出。
# 类用途: 按四类资产组织正向或反向引用。
@dataclass(frozen=True)
class AssetRefs(_Record):
    workflows: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    monitors: tuple[str, ...] = ()
    skill_dir: str = ''


# LLM: 摘要只表示累计观察，不替代运行账本；计数和耗时非负，错误自动脱敏，旧JSON缺省空错误。
# 类用途: 显示资产最近运行、连续失败和安全错误摘要。
@dataclass(frozen=True)
class RuntimeSummary(_Record):
    last_run_at: str | None = None
    last_status: str = ""
    last_duration_ms: float | None = None
    run_count: int = 0
    consecutive_failures: int = 0
    last_error: str = ""


# LLM: 标准提取Key是四类资产共同投影；不携带owner_home或授权，不写入任何状态。
# 类用途: 统一描述名称、访问源、凭据引用、IO、依赖与运行摘要。
@dataclass(frozen=True)
class AssetDescriptor(_Record):
    id: str
    kind: str
    name: str
    summary: str = ""
    version: str = "1.0.0"
    owner_id: str = ""
    access_targets: tuple[AccessTarget, ...] = ()
    data_sources: tuple[DataSourceSpec, ...] = ()
    credential_refs: tuple[CredentialBindingRef, ...] = ()
    io_contract: IOContract = field(default_factory=IOContract)
    refs: AssetRefs = field(default_factory=AssetRefs)
    used_by: AssetRefs = field(default_factory=AssetRefs)
    runtime_summary: RuntimeSummary = field(default_factory=RuntimeSummary)
    tags: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)
