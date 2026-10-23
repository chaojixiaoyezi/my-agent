# LLM: 工作流只描述不执行，普通写入固定于Owner/workflows/id；全局兜底只读，管理员显式工厂绑定全局写根。
# 模块用途: 分层读取并管理工作流定义、说明、Skill绑定、自动引用及安全运行摘要。
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import (
    _AssetStore,
    canonical_skill_name,
    next_run_summary,
    positive_timeout,
    safe_asset_id,
    valid_skill_reference,
)
from .descriptor import AssetDescriptor, _Record

WORKFLOW_STEP_MODES = ("deterministic_script", "llm_skill_analysis", "hybrid")
WORKFLOW_ON_ERROR_POLICIES = ("escalate_to_agent", "fail", "skip")


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

    # LLM: 资产层唯一校验步骤；Skill引用允许来源前缀，脚本目录ID仍严格，模式/策略/超时不静默修正。
    # 函数用途: 在构造或JSON恢复时拒绝非法工作流步骤。
    def __post_init__(self):
        super().__post_init__()
        if self.mode not in WORKFLOW_STEP_MODES:
            raise ValueError("未知工作流步骤mode")
        if self.on_error not in WORKFLOW_ON_ERROR_POLICIES:
            raise ValueError(f"Unsupported on_error policy: {self.on_error}")
        positive_timeout(self.timeout_seconds)
        for skill_id in self.skill_ids:
            valid_skill_reference(skill_id)
        if self.script_id:
            safe_asset_id(self.script_id)


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

    # LLM: 所有执行/工具入口共用严格from_dict，不另猜字段或模式；值对象也复制，阻止嵌套可变值污染。
    # 函数用途: 从原生JSON或已验证定义恢复同一工作流合同。
    @classmethod
    def from_any(cls, value):
        return cls.from_dict(value.to_dict() if isinstance(value, WorkflowDefinition) else value)


# LLM: steps保留来源精确引用，AssetRefs.skills只放规范裸名称；陈旧派生值不沿用且不授予权限。
# 函数用途: 保存前去重汇总脚本和Skill引用。
def with_workflow_step_refs(definition):
    scripts = tuple(dict.fromkeys(step.script_id for step in definition.steps if step.script_id))
    skills = tuple(dict.fromkeys(canonical_skill_name(skill) for step in definition.steps for skill in step.skill_ids))
    refs = replace(definition.descriptor.refs, scripts=scripts, skills=skills)
    return replace(definition, descriptor=replace(definition.descriptor, refs=refs))


# LLM: 所有读改写共用Owner集合锁，不执行步骤、Skill或正文；WORKFLOW.md只是JSON正文的同步展示副本。
# 类用途: 为工具、执行引擎与Broker提供工作流唯一存储入口。
class WorkflowStore(_AssetStore):
    # LLM: 普通实例只写宿主给定Owner；extra仅可读，is_admin不自动开放兜底写入，也不执行工作流。
    # 函数用途: 绑定工作流写根及有序全局只读候选。
    def __init__(self, owner_home, *, extra_roots=(), is_admin: bool = False):
        super().__init__(owner_home, "workflows", "workflow.json", extra_roots=extra_roots, is_admin=is_admin)

    # LLM: 描述符经冻结值对象复制；全局作用域只加在返回投影，不持久化或改变步骤引用。
    # 函数用途: 标记只读全局工作流的来源。
    def _mark_global(self, definition):
        descriptor = definition.descriptor
        return replace(definition, descriptor=replace(descriptor, metadata={**descriptor.metadata, 'asset_scope': 'global'}))

    # LLM: 平铺资产Key经唯一from_any恢复；不吞掉损坏JSON或未知字段。
    # 函数用途: 锁内读取完整定义，缺失返回None。
    def _get(self, workflow_id):
        payload = self._read(workflow_id)
        if payload is None:
            return None
        values = dict(payload)
        steps, body = values.pop("steps"), values.pop("body_markdown", "")
        return WorkflowDefinition.from_any({"descriptor": values, "steps": steps, "body_markdown": body})

    # LLM: 更新不隐式创建记录；缺失由调用方处理，不回退到其它Owner。
    # 函数用途: 为挂载和摘要更新取得现有定义。
    def _required(self, workflow_id):
        definition = self._get(workflow_id)
        if definition is None:
            raise KeyError(workflow_id)
        return definition

    # LLM: 同锁提交refs/steps/正文后读回权威记录；提交后读失败按独立阶段抛出，不能报告未开始。
    # 函数用途: 写入已经校验的工作流，供多个写入口共用。
    def _save(self, definition):
        definition = with_workflow_step_refs(definition)
        payload = definition.descriptor.to_dict() | {"steps": [step.to_dict() for step in definition.steps],
                                                    "body_markdown": definition.body_markdown}
        self._write(payload, {"WORKFLOW.md": definition.body_markdown or None})
        return self._readback_after_commit(definition.descriptor.id)

    # LLM: 输入共用严格from_any以保护可变metadata；不执行正文，写路径共享_save提交与读回阶段。
    # 函数用途: 创建或替换工作流并自动汇总步骤引用。
    def save_workflow(self, definition):
        definition = WorkflowDefinition.from_any(definition)
        with self._locked():
            self._get(definition.descriptor.id)
            return self._save(definition)

    # LLM: Owner坏记录不得触发回退；真实缺失才释放Owner锁后读取全局，不能把兜底对象交给本地更新入口。
    # 函数用途: 优先读取Owner工作流，缺失时只读全局定义。
    def get_workflow(self, workflow_id):
        safe_asset_id(workflow_id)
        with self._locked():
            definition = self._get(workflow_id)
        return definition if definition is not None else self._global_get(workflow_id)

    # LLM: Owner先认领ID，释放其锁后逐个全局根取锁，不读取已被遮蔽的低优先级定义。
    # 函数用途: 合并Owner及全局工作流，按ID稳定返回去重结果。
    def list_workflows(self):
        with self._locked():
            records = self._local_records()
        return self._merge_globals(records)

    # LLM: 先验证权威定义再受控删除；不跟随目录或文件链接。
    # 函数用途: 删除单个工作流目录，缺失返回False。
    def delete_workflow(self, workflow_id):
        safe_asset_id(workflow_id)
        with self._locked():
            if self._get(workflow_id) is None:
                return False
            return self._delete(workflow_id)

    # LLM: attach/detach同用锁内命令，模式检查不可只留在工具wrapper；精确引用不去前缀。
    # 函数用途: 动态给现有工作流步骤挂载一个Skill。
    def attach_skill_to_step(self, workflow_id, step_id, skill_id):
        return self._change_skill_binding(workflow_id, step_id, skill_id, remove=False)

    # LLM: 与attach共享原子读改写，不在工具层重建定义或覆盖其它步骤。
    # 函数用途: 从指定分析或混合步骤解绑一个精确Skill引用。
    def detach_skill_from_step(self, workflow_id, step_id, skill_id):
        return self._change_skill_binding(workflow_id, step_id, skill_id, remove=True)

    # LLM: 在原Owner集合锁内查模式、只替目标步并重算refs；失败前不写入，不接受裸路径。
    # 函数用途: 统一执行Skill挂载或解绑，避免两个存储权威和丢更新。
    def _change_skill_binding(self, workflow_id, step_id, skill_id, *, remove):
        valid_skill_reference(skill_id)
        safe_asset_id(step_id)
        with self._locked():
            definition = self._required(workflow_id)
            target = next((step for step in definition.steps if step.step_id == step_id), None)
            if target is None:
                raise KeyError(step_id)
            if target.mode not in ("llm_skill_analysis", "hybrid"):
                raise ValueError("目标必须是现有分析或混合步骤")
            skills = (tuple(value for value in target.skill_ids if value != skill_id) if remove
                      else tuple(dict.fromkeys((*target.skill_ids, skill_id))))
            steps = tuple(replace(step, skill_ids=skills)
                          if step.step_id == step_id else step for step in definition.steps)
            return self._save(replace(definition, steps=steps))

    # LLM: 本Owner摘要仍原锁读改写；仅全局存在时释放Owner锁后返回只读定义，不更新共享摘要。
    # 函数用途: 保存本地工作流运行摘要，安全跳过全局摘要写入。
    def record_run_summary(self, workflow_id, status, duration_ms, run_at=None, *, last_error=""):
        with self._locked():
            definition = self._get(workflow_id)
            if definition is not None:
                summary = next_run_summary(definition.descriptor.runtime_summary, status, duration_ms, run_at, last_error=last_error)
                return self._save(replace(definition, descriptor=replace(definition.descriptor, runtime_summary=summary)))
        definition = self._global_get(workflow_id)
        if definition is None:
            raise KeyError(workflow_id)
        return definition
