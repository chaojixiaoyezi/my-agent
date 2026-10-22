# LLM: 工作流运行记录的唯一内存与 JSON 字段契约；不保存脚本秘密或执行权限。
# 模块用途: 定义脚本结果、步骤快照和工作流断点记录。
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal

from ..assets.workflow_store import WorkflowStepSpec as AssetWorkflowStepSpec
from ..common.log_redaction import redact_sensitive_value

_BEARER_VALUE = re.compile(r'''(?i)(\bbearer\s+)(?!<redacted\b|\[redacted\])[^\s,;"'<>]+''')
WORKFLOW_ON_ERROR_POLICIES = ('escalate_to_agent', 'fail', 'skip')
StepStatus = Literal['pending', 'running', 'succeeded', 'skipped', 'failed',
                     'escalated_recovered', 'paused_for_resume']


# LLM: 继承资产步骤唯一字段合同；引擎与工具在执行前补充策略校验，不修改资产存储实现。
# 类用途: 提供带错误策略枚举检查的工作流步骤值对象。
@dataclass(frozen=True)
class WorkflowStepSpec(AssetWorkflowStepSpec):
    # LLM: 父类继续验证模式、编号和超时；非法错误策略不能进入执行账本。
    # 函数用途: 构造或反序列化时拒绝未知on_error值。
    def __post_init__(self):
        super().__post_init__()
        if self.on_error not in WORKFLOW_ON_ERROR_POLICIES:
            raise ValueError(f'Unsupported on_error policy: {self.on_error}')


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

    # LLM: 旧账本没有needs_repair时使用False，技能容器恢复为tuple。
    # 函数用途: 从JSON对象恢复步骤记录。
    @classmethod
    def from_dict(cls, value):
        return cls(**{**payload(value), 'resolved_skill_ids': tuple(value.get('resolved_skill_ids', ()))})


# LLM: 运行身份不由模型正文推导；该结构仅是账本事实，不能授予 Owner 权限。
# 类用途: 保存整个工作流运行的进度及断点位置。
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

    # LLM: 嵌套步骤与修复标记一起序列化，不隐式清除历史失败事实。
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


# LLM: 输入映射和条件只从明确步骤编号取值，不解析表达式或模型自然语言。
# 函数用途: 从此前步骤快照提取一个字段或完整输出；不存在时明确报错。
def output_value(reference, outputs):
    if not isinstance(reference, dict) or set(reference) - {'step_id', 'key', 'equals'}:
        raise ValueError('输出引用必须是结构化 step_id/key')
    value = outputs[reference['step_id']]
    if 'key' in reference:
        value = value[reference['key']]
    return payload(value)


# LLM: 技能序列必须是显式列表或元组，不把字符串、映射或空编号转换成权限候选。
# 函数用途: 校验静态技能、标签和解析器结果，防止裸字符串被拆成字符。
def skill_sequence(value):
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item for item in value):
        raise ValueError('技能编号或标签必须为非空字符串列表')
    return tuple(value)
