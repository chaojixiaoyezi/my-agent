# LLM: 工作流运行记录的唯一内存与JSON合同；ephemeral_debug仅控制调试副作用，不授予执行权限。
# 模块用途: 定义脚本结果、步骤快照、正式运行和内存调试记录。
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from ..assets.workflow_store import WORKFLOW_ON_ERROR_POLICIES as WORKFLOW_ON_ERROR_POLICIES
from ..assets.workflow_store import WorkflowStepSpec as WorkflowStepSpec
from ..common.log_redaction import redact_sensitive_value

_BEARER_VALUE = re.compile(r'''(?i)(\bbearer\s+)(?!<redacted\b|\[redacted\])[^\s,;"'<>]+''')
StepStatus = Literal['pending', 'running', 'succeeded', 'skipped', 'failed',
                     'escalated_recovered', 'paused_for_resume']


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
        return cls(**{**payload(value), 'resolved_skill_ids': tuple(value.get('resolved_skill_ids', ()))})


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

    # LLM: 嵌套步骤、进度和断点一起序列化；旧记录缺字段由默认值兼容，不清除历史失败。
    # 函数用途: 输出完整运行JSON快照。
    def to_dict(self):
        return payload(self)

    # LLM: 步骤委托同一模型恢复；旧账本修复标记缺省为False。
    # 函数用途: 从JSON恢复整个工作流运行记录。
    @classmethod
    def from_dict(cls, value):
        return cls(**{**payload(value), 'steps': tuple(StepExecutionRecord.from_dict(item) for item in value.get('steps', ()))})


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
