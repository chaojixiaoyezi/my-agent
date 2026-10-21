# LLM: 工作流定义只描述步骤与引用，不调度或执行；结构化condition仅透传原执行器，权威存储固定于Owner/workflows/id。
# 模块用途: 管理工作流定义、说明、步骤Skill绑定、自动引用及运行摘要。
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import _AssetStore, next_run_summary, positive_timeout, safe_asset_id
from .descriptor import AssetDescriptor, _Record

WORKFLOW_STEP_MODES = ("deterministic_script", "llm_skill_analysis", "hybrid")


# LLM: mode只允许三种结构化执行声明；condition保留空字符串或JSON比较结构，inputs_map同为数据，均不在存储层执行。
# 类用途: 定义工作流一步的脚本、Skill、条件、错误策略及超时。
@dataclass(frozen=True)
class WorkflowStepSpec(_Record):
    step_id: str
    name: str
    mode: str
    script_id: str = ""
    skill_ids: tuple[str, ...] = ()
    skill_tags: tuple[str, ...] = ()
    condition: str | dict[str, object] = ""
    on_error: str = "escalate_to_agent"
    inputs_map: dict[str, object] = field(default_factory=dict)
    timeout_seconds: float = 120

    # LLM: 标识符和超时先验证；不能把script_id当路径或把错误mode静默改成脚本模式。
    # 函数用途: 在构造或JSON恢复时校验工作流步骤。
    def __post_init__(self):
        super().__post_init__()
        if self.mode not in WORKFLOW_STEP_MODES:
            raise ValueError("未知工作流步骤mode")
        positive_timeout(self.timeout_seconds)
        for asset_id in (*self.skill_ids, *((self.script_id,) if self.script_id else ())):
            safe_asset_id(asset_id)


# LLM: 定义是纯值对象，steps只允许唯一step_id；refs的脚本/Skill权威来自保存时的实际steps。
# 类用途: 组合工作流描述符、步骤序列与可选Markdown说明。
@dataclass(frozen=True)
class WorkflowDefinition(_Record):
    descriptor: AssetDescriptor
    steps: tuple[WorkflowStepSpec, ...]
    body_markdown: str = ""

    # LLM: 不允许非workflow描述符或重复步骤，避免后续挂载与断点定位歧义。
    # 函数用途: 校验工作流定义，不读写Owner状态。
    def __post_init__(self):
        super().__post_init__()
        if self.descriptor.kind != "workflow" or len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("工作流kind错误或step_id重复")

    # LLM: 嵌套descriptor保留自身schema_version，不把定义层当成另一份资产协议。
    # 函数用途: 提供完整工作流的JSON传输结构。
    def to_dict(self):
        return {"descriptor": self.descriptor.to_dict(), "steps": [step.to_dict() for step in self.steps],
                "body_markdown": self.body_markdown}


# LLM: 由steps推导两个引用集合，旧派生值不沿用；其它kind引用保持原值与顺序。
# 函数用途: 保存前去重汇总脚本和Skill引用。
def _with_step_refs(definition):
    scripts = tuple(dict.fromkeys(step.script_id for step in definition.steps if step.script_id))
    skills = tuple(dict.fromkeys(skill for step in definition.steps for skill in step.skill_ids))
    refs = replace(definition.descriptor.refs, scripts=scripts, skills=skills)
    return replace(definition, descriptor=replace(definition.descriptor, refs=refs))


# LLM: 所有读改写共用Owner集合锁，不执行步骤、Skill或正文；WORKFLOW.md只是JSON正文的同步展示副本。
# 类用途: 为工具、执行引擎与Broker提供工作流唯一存储入口。
class WorkflowStore(_AssetStore):
    # LLM: 构造固定workflows目录，不创建或调度工作流运行。
    # 函数用途: 绑定当前Owner工作流存储。
    def __init__(self, owner_home):
        super().__init__(owner_home, "workflows", "workflow.json")

    # LLM: 平铺资产Key先严格恢复，再恢复步骤；不吞掉损坏JSON或未知字段。
    # 函数用途: 锁内读取完整定义，缺失返回None。
    def _get(self, workflow_id):
        payload = self._read(workflow_id)
        if payload is None:
            return None
        values = dict(payload)
        steps, body = values.pop("steps"), values.pop("body_markdown", "")
        return WorkflowDefinition.from_dict({"descriptor": values, "steps": steps, "body_markdown": body})

    # LLM: 更新不隐式创建记录；缺失由调用方处理，不回退到其它Owner。
    # 函数用途: 为挂载和摘要更新取得现有定义。
    def _required(self, workflow_id):
        definition = self._get(workflow_id)
        if definition is None:
            raise KeyError(workflow_id)
        return definition

    # LLM: 保存后refs与steps同源，正文与元数据锁内提交；文件写失败由基座回滚。
    # 函数用途: 写入已经校验的工作流，供多个写入口共用。
    def _save(self, definition):
        definition = _with_step_refs(definition)
        payload = definition.descriptor.to_dict() | {"steps": [step.to_dict() for step in definition.steps],
                                                    "body_markdown": definition.body_markdown}
        self._write(payload, {"WORKFLOW.md": definition.body_markdown or None})
        return definition

    # LLM: 输入重新走严格恢复，防止可变metadata被事后污染；不执行正文。
    # 函数用途: 创建或替换工作流并自动汇总步骤引用。
    def save_workflow(self, definition):
        definition = WorkflowDefinition.from_dict(definition.to_dict())
        with self._locked():
            self._get(definition.descriptor.id)
            return self._save(definition)

    # LLM: 只读指定Owner当前定义，未知ID为None；路径和叶链接错误上抛。
    # 函数用途: 取得工作流定义。
    def get_workflow(self, workflow_id):
        safe_asset_id(workflow_id)
        with self._locked():
            return self._get(workflow_id)

    # LLM: 按ID稳定枚举，损坏记录不静默过滤为缺失。
    # 函数用途: 列出本Owner已保存工作流。
    def list_workflows(self):
        with self._locked():
            return [definition for asset_id in self._ids() if (definition := self._get(asset_id)) is not None]

    # LLM: 先验证权威定义再受控删除；不跟随目录或文件链接。
    # 函数用途: 删除单个工作流目录，缺失返回False。
    def delete_workflow(self, workflow_id):
        safe_asset_id(workflow_id)
        with self._locked():
            if self._get(workflow_id) is None:
                return False
            return self._delete(workflow_id)

    # LLM: 只修改目标step_id并按skill_id去重，同锁内重新推导refs；未知步骤不保存。
    # 函数用途: 动态给现有工作流步骤挂载一个Skill。
    def attach_skill_to_step(self, workflow_id, step_id, skill_id):
        safe_asset_id(skill_id)
        with self._locked():
            definition = self._required(workflow_id)
            if step_id not in {step.step_id for step in definition.steps}:
                raise KeyError(step_id)
            steps = tuple(replace(step, skill_ids=tuple(dict.fromkeys((*step.skill_ids, skill_id))))
                          if step.step_id == step_id else step for step in definition.steps)
            return self._save(replace(definition, steps=steps))

    # LLM: 摘要更新只消费本次结构化运行结果，同一集合锁覆盖读取和发布，不丢并发次数。
    # 函数用途: 保存工作流最近运行与连续失败摘要。
    def record_run_summary(self, workflow_id, status, duration_ms, run_at):
        with self._locked():
            definition = self._required(workflow_id)
            summary = next_run_summary(definition.descriptor.runtime_summary, status, duration_ms, run_at)
            return self._save(replace(definition, descriptor=replace(definition.descriptor, runtime_summary=summary)))
