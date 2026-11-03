# LLM: 正式运行和StepDebugResult共用JSON/脱敏合同；调试别名从原字段派生，persisted仅声明不写正式运行账。
# 模块用途: 定义执行记录及新旧字段并存、含渲染输入和条件分支的单节点调试结果。
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal, get_args

from ..assets.workflow_store import WORKFLOW_ON_ERROR_POLICIES as WORKFLOW_ON_ERROR_POLICIES
from ..assets.workflow_store import WorkflowStepSpec as WorkflowStepSpec
from ..common.log_redaction import redact_sensitive_value

_BEARER_VALUE = re.compile(r'''(?i)(\bbearer\s+)(?!<redacted\b|\[redacted\])[^\s,;"'<>]+''')
StepStatus = Literal['pending', 'running', 'succeeded', 'skipped', 'failed',
                     'escalated_recovered', 'paused_for_resume', 'waiting_for_agent']
SCRIPT_WORK_CONTEXT_SCHEMA_VERSION = 'script-work-context.v1'
SCRIPT_OUTPUT_PROTOCOL_VERSION = 'script-output.v1'


class ScriptWorkContextError(ValueError):
    """SDK 的结构化拒绝；不在错误消息中回显宿主环境或上下文正文。"""

    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class ScriptWorkContext:
    """宿主冻结的只读工作视图，不包含凭证、来源环境名或投递目标。"""

    workspace_root: Path
    scratch_dir: Path
    run_id: str = ''
    task_id: str = ''
    effective_path_scope: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            workspace, scratch = Path(self.workspace_root), Path(self.scratch_dir)
            if not workspace.is_absolute() or not scratch.is_absolute():
                raise ValueError('absolute paths required')
            scope = self.effective_path_scope
            if not isinstance(scope, Mapping) or set(scope) != {'read_roots', 'write_roots'}:
                raise ValueError('explicit scope required')
            normalized = {}
            for key in ('read_roots', 'write_roots'):
                roots = scope[key]
                if not isinstance(roots, (tuple, list)) or any(not isinstance(p, (str, Path)) or not Path(p).is_absolute() for p in roots):
                    raise ValueError('absolute roots required')
                normalized[key] = tuple(Path(p).resolve(strict=False) for p in roots)
            if not isinstance(self.run_id, str) or not isinstance(self.task_id, str):
                raise ValueError('invalid run/task binding')
            object.__setattr__(self, 'workspace_root', workspace.resolve(strict=False))
            object.__setattr__(self, 'scratch_dir', scratch.resolve(strict=False))
            object.__setattr__(self, 'effective_path_scope', MappingProxyType(normalized))
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ScriptWorkContextError('SCRIPT_WORK_CONTEXT_INVALID') from None

    def to_dict(self) -> dict[str, Any]:
        """独立 JSON 投影给宿主启动器冻结；不序列化 MappingProxy 或进程对象。"""
        return {'schema_version': SCRIPT_WORK_CONTEXT_SCHEMA_VERSION,
                'workspace_root': str(self.workspace_root), 'scratch_dir': str(self.scratch_dir),
                'run_id': self.run_id, 'task_id': self.task_id,
                'effective_path_scope': {key: [str(path) for path in roots]
                                         for key, roots in self.effective_path_scope.items()}}


@dataclass(frozen=True)
class DeliveryIntent:
    """仅本地声明 requested；宿主退出校验和原投递账提交后才可能 accepted。"""

    request_key: str
    message: str = ''
    output_refs: tuple[str, ...] = ()
    status: str = field(default='requested', kw_only=True)

    def __post_init__(self) -> None:
        if (not isinstance(self.request_key, str) or not self.request_key.strip()
                or self.request_key != self.request_key.strip() or len(self.request_key) > 256
                or any(ord(c) < 32 for c in self.request_key) or not isinstance(self.message, str)
                or not isinstance(self.output_refs, tuple) or self.status != 'requested'):
            raise ScriptWorkContextError('SCRIPT_DELIVERY_REQUEST_INVALID')
        for ref in self.output_refs:
            if (not isinstance(ref, str) or not ref or ref != ref.strip() or any(ord(c) < 32 for c in ref)
                    or Path(ref).is_absolute() or PureWindowsPath(ref).drive or ref.startswith(('~', '\\'))
                    or '..' in ref.replace('\\', '/').split('/') or '://' in ref):
                raise ScriptWorkContextError('SCRIPT_DELIVERY_REQUEST_INVALID')


# LLM: 属性是完整输入合同，dict仅保留前步与非碰撞输入，各视图独立深拷贝。
# 类用途: 向宿主分析器提供兼容旧outputs的完整隔离上下文。
class AnalysisContext(dict[str, Any]):
    # LLM: dict中不插入skill_details等保留元字段，保持旧outputs相等合同。
    # 函数用途: 复制前步、原始和映射输入、Skill正文及数据源。
    def __init__(self, prior_outputs=None, run_inputs=None, step_inputs=None, skill_details=None, data_sources=None):
        self.prior_outputs = payload({} if prior_outputs is None else prior_outputs)
        self.run_inputs = payload({} if run_inputs is None else run_inputs)
        self.step_inputs = payload({} if step_inputs is None else step_inputs)
        self.skill_details = payload([] if skill_details is None else skill_details)
        self.data_sources = payload({} if data_sources is None else data_sources)
        super().__init__(payload(self.prior_outputs))
        for values in (self.step_inputs, self.run_inputs):
            for key, value in values.items():
                self.setdefault(key, payload(value))


# LLM: 数据容器无执行副作用，输出与兜底上下文供引擎和工具读取。
# 类用途: 保存一次真实脚本调用的结果与错误上下文。
@dataclass
class ScriptExecutionResult:
    script_id: str
    ok: bool
    exit_code: int | None
    duration_ms: float
    started_at: float
    ended_at: float
    output_data: dict[str, Any]
    stdout_preview: str
    stderr_preview: str
    error_code: str = ''
    needs_repair: bool = False
    escalation_context: dict[str, Any] = field(default_factory=dict)
    source_check: dict[str, Any] = field(default_factory=dict)
    execution_definition_ref: str = field(default='', kw_only=True)
    workflow_run_id: str = field(default='', kw_only=True)
    phase: str = field(default='', kw_only=True)
    effect_outcome: str = field(default='', kw_only=True)
    breakpoint: dict[str, Any] | None = field(default=None, kw_only=True)
    confirmed_step_refs: tuple[str, ...] = field(default=(), kw_only=True)


# LLM: 仅内存返回，不可进入WorkflowRunStore；step_type保留保存的mode，所有状态新旧别名统一脱敏。
# 类用途: 给Python与HTTP提供同源单步结果，persisted=False不否认显式许可的业务副作用。
@dataclass
class StepDebugResult:
    workflow_id: str
    step_id: str
    status: str
    duration_ms: float
    output_data: dict[str, Any]
    rendered_input: dict[str, Any]
    condition_branch: bool | str | None = None
    logs: list[str] = field(default_factory=list)
    error_detail: dict[str, Any] | None = None
    dry_run: bool = False
    side_effects_blocked: bool = False
    ok: bool = True
    step_type: str = ''

    # LLM: 先脱敏再从旧字段复制别名，不存第二份状态；调用方修改一份输出不影响另一份或DTO。
    # 函数用途: 返回新旧字段一致的独立调试字典，明确没有持久化正式运行。
    def to_dict(self):
        result = redact_bearer_values(payload(self))
        result.update(output=payload(result['output_data']), error=payload(result['error_detail']),
            side_effects_suppressed=bool(result['side_effects_blocked']), persisted=False)
        return result


# LLM: 状态仅接受 StepStatus；所有可变字段独立创建，供账本序列化。
# 类用途: 记录一步的输入、输出、模型参与情况和恢复草案。
@dataclass
class StepExecutionRecord:
    step_id: str
    name: str
    mode: str
    status: StepStatus
    started_at: float
    ended_at: float
    duration_ms: float
    llm_involved: bool
    script_id: str = ''
    resolved_skill_ids: tuple[str, ...] = ()
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    output_snapshot: dict[str, Any] = field(default_factory=dict)
    error_code: str = ''
    error_message: str = ''
    escalation_result: dict[str, Any] = field(default_factory=dict)
    needs_repair: bool = False
    description: str = field(default='', kw_only=True)
    next_step_ids: tuple[str, ...] = field(default=(), kw_only=True)

    # LLM: JSON往返保留修复标记，返回独立快照而不是内部可变字典。
    # 函数用途: 序列化一个步骤的完整运行事实。
    def to_dict(self):
        return payload(self)

    # LLM: 状态校验集中于模型；旧账本缺needs_repair用False，技能容器恢复tuple。
    # 函数用途: 严格恢复独立步骤记录。
    @classmethod
    def from_dict(cls, value):
        if value['status'] not in get_args(StepStatus):
            raise ValueError('未知步骤状态')
        return cls(**{**payload(value), 'resolved_skill_ids': tuple(value.get('resolved_skill_ids', ())),
                      'next_step_ids': tuple(value.get('next_step_ids', ()))})


# LLM: 身份不由正文推导；debug只由引擎内存创建，禁止调试记录进入正式运行Store。
# 类用途: 保存工作流运行或内存调试的进度及断点，不授予Owner权限。
@dataclass
class WorkflowRunRecord:
    workflow_run_id: str
    workflow_id: str
    owner_id: str
    trigger_kind: str
    status: str
    started_at: float
    ended_at: float
    duration_ms: float
    current_step_id: str = ''
    failed_step_id: str = ''
    steps: tuple[StepExecutionRecord, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    final_output: dict[str, Any] = field(default_factory=dict)
    resumed_from_step_id: str = ''
    needs_repair: bool = False

    workflow_name: str = ''
    total_steps: int = 0
    completed_steps: int = 0
    breakpoint: dict[str, Any] | None = None
    ephemeral_debug: bool = False
    # 执行身份不使用统计 CAS；空值明确表示旧记录没有冻结此事实。
    execution_definition_ref: str = field(default='', kw_only=True)
    phase: str = field(default='', kw_only=True)
    effect_outcome: str = field(default='', kw_only=True)
    confirmed_step_refs: tuple[str, ...] = field(default=(), kw_only=True)
    # 三种版本各守职责：业务空间选择、Workflow scope、原运行 CAS 与执行定义指纹互不替代。
    business_context: dict[str, Any] = field(default_factory=dict, kw_only=True)
    scope_snapshot: dict[str, Any] = field(default_factory=dict, kw_only=True)
    scope_revision: int = field(default=0, kw_only=True)
    revision: int = field(default=0, kw_only=True)
    progress_seq: int = field(default=0, kw_only=True)

    def __post_init__(self) -> None:
        # 这里只定义快照/版本合同；锁内比较与递增由 W3 的原 run_store 实现，不在 DTO 猜 CAS。
        for name in ('scope_revision', 'revision'):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f'{name} 必须是整数')
            if value < 0:
                raise ValueError(f'{name} 不能为负数')
        for name in ('business_context', 'scope_snapshot'):
            value = getattr(self, name)
            if type(value) is not dict:
                raise TypeError(f'{name} 必须是 JSON 对象')
            setattr(self, name, payload(value))

    # LLM: 嵌套步骤、进度和断点一起序列化；旧记录缺字段由默认值兼容，不清除历史失败。
    # 函数用途: 输出完整运行JSON快照。
    def to_dict(self):
        return payload(self)

    # LLM: 步骤委托同一模型恢复；旧账本修复标记缺省为False。
    # 函数用途: 从JSON恢复整个工作流运行记录。
    @classmethod
    def from_dict(cls, value):
        return cls(**{**payload(value), 'steps': tuple(StepExecutionRecord.from_dict(item) for item in value.get('steps', ())),
                      'confirmed_step_refs': tuple(value.get('confirmed_step_refs', ()))})


# LLM: 所有跨磁盘和回调边界的 JSON 值均复制，禁止共享可变快照和非 JSON 对象。
# 函数用途: 将 dataclass 或 JSON 配置转成独立的普通数据。
def payload(value):
    return json.loads(json.dumps(asdict(value) if is_dataclass(value) else value, allow_nan=False))


# LLM: 在通用脱敏后补齐裸Bearer值遮蔽，保持字典键和数值类型不变。
# 函数用途: 递归遮蔽对象或字符串中的独立Bearer凭据片段。
def redact_bearer_values(value):
    value = redact_sensitive_value(value)
    if isinstance(value, str):
        return _BEARER_VALUE.sub(r'\1<redacted>', value)
    if isinstance(value, dict):
        return {k: redact_bearer_values(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_bearer_values(v) for v in value)
    if isinstance(value, list):
        return [redact_bearer_values(v) for v in value]
    return value


# LLM: 资产和账本路径只接受最多三段稳定标识，不跟随 Owner 根以下的符号链接。
# 函数用途: 校验编号并生成受限路径；不创建目录。
def safe_path(root, part, part2=None, part3=None):
    path = Path(root).absolute()
    if path.is_symlink():
        raise ValueError('Owner 根不能是符号链接')
    parts = (part, *(item for item in (part2, part3) if item is not None))
    for seg in parts:
        if not isinstance(seg, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', seg):
            raise ValueError('无效的资产或步骤编号')
        path /= seg
        if path.is_symlink():
            raise ValueError('资产路径不能包含符号链接')
    return path


# LLM: 仅显式fallback处理缺步、缺键或None；无fallback保留原缺失错误，不猜默认值。
# 函数用途: 从前步或冻结初始输入取值，递归解析结构化兜底引用。
def output_value(reference, outputs, inputs=None):
    if not isinstance(reference, dict) or 'step_id' not in reference or set(reference) - {'step_id', 'key', 'equals', 'fallback'}:
        raise ValueError('输出引用必须是结构化 step_id/key')
    step_id = reference['step_id']
    try:
        if step_id == '$inputs' and isinstance(inputs, dict):
            default = '' if reference.get('equals') == '' else None
            value = inputs.get(reference['key'], default) if 'key' in reference else inputs
        else:
            value = outputs[step_id]
            if 'key' in reference and value is not None:
                value = value[reference['key']]
    except KeyError:
        if 'fallback' not in reference:
            raise
        return output_value(reference['fallback'], outputs, inputs)
    if value is None and 'fallback' in reference:
        return output_value(reference['fallback'], outputs, inputs)
    return payload(value)


# LLM: 技能序列必须是显式列表或元组，不把字符串、映射或空编号转换成权限候选。
# 函数用途: 校验静态技能、标签和解析器结果，防止裸字符串被拆成字符。
def skill_sequence(value):
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item for item in value):
        raise ValueError('技能编号或标签必须为非空字符串列表')
    return tuple(value)
