# LLM: 所有步骤mode只由WORKFLOW_STEP_MODES校验；存储支持调试别名/控制节点，不授正式DAG新执行权。
# 模块用途: 分层保存工作流、节点配置、Skill绑定和摘要，保留原scope/CAS及执行权限边界。
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace

from . import (
    _AssetStore,
    canonical_skill_name,
    next_asset_revision,
    next_run_summary,
    positive_timeout,
    safe_asset_id,
    valid_skill_reference,
)
from .descriptor import AssetDescriptor, _Record, execution_definition_ref

WORKFLOW_STEP_MODES = ("deterministic_script", "llm_skill_analysis", "hybrid",
                       "http_request", "script_transform", "condition", "tool_call", "llm_prompt", "subworkflow",
                       "condition_branch", "approval_wait", "notification", "script", "agent_task")
WORKFLOW_ON_ERROR_POLICIES = ("escalate_to_agent", "fail", "skip")
BUSINESS_WORKFLOW_METADATA = 'business_workflow'


# LLM: 旧模式及调试别名/控制节点共用原值对象；config仅有限JSON，不承载回调、审批或执行授权。
# 类用途: 定义一步的模式、脚本、配置、结构化条件及预算，不在保存时执行节点。
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
    config: dict[str, object] = field(default_factory=dict)

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


# LLM: 定义是纯值对象，steps保持唯一；enabled与原descriptor同步，任一False均拒绝隐式复活，旧记录默认True。
# 类用途: 组合工作流描述符、步骤序列与可选Markdown说明。
@dataclass(frozen=True)
class WorkflowDefinition(_Record):
    descriptor: AssetDescriptor
    steps: tuple[WorkflowStepSpec, ...]
    body_markdown: str = ""
    enabled: bool = True

    # LLM: 严格类型后同步定义/描述符运行闸；显式set_enabled须同时替换两字段，不允许缺省True覆盖已禁用描述符。
    # 函数用途: 校验工作流定义，不读写Owner状态。
    def __post_init__(self):
        super().__post_init__()
        enabled = self.enabled and self.descriptor.enabled
        object.__setattr__(self, 'enabled', enabled)
        object.__setattr__(self, 'descriptor', replace(self.descriptor, enabled=enabled))
        if self.descriptor.kind != "workflow" or len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("工作流kind错误或step_id重复")

    # LLM: 嵌套descriptor保留自身schema_version；外层enabled与描述符值一致，不创建第二份运行状态。
    # 函数用途: 提供完整工作流的JSON传输结构。
    def to_dict(self):
        return {"descriptor": self.descriptor.to_dict(), "steps": [step.to_dict() for step in self.steps],
                "body_markdown": self.body_markdown, "enabled": self.enabled}

    # LLM: 所有执行/工具入口共用严格from_dict，不另猜字段或模式；值对象也复制，阻止嵌套可变值污染。
    # 函数用途: 从原生JSON或已验证定义恢复同一工作流合同。
    @classmethod
    def from_any(cls, value):
        return cls.from_dict(value.to_dict() if isinstance(value, WorkflowDefinition) else value)

    # LLM: 每次从权威定义重新计算，统计及显示字段不能污染内容身份；不保存第二份可写版本。
    # 函数用途: 提供工作流的稳定执行引用。
    @property
    def execution_definition_ref(self):
        return execution_definition_ref(self.to_dict())


# LLM: steps保留来源精确引用，AssetRefs.skills只放规范裸名称；陈旧派生值不沿用且不授予权限。
# 函数用途: 保存前去重汇总脚本和Skill引用。
def with_workflow_step_refs(definition):
    scripts = tuple(dict.fromkeys(step.script_id for step in definition.steps if step.script_id))
    skills = tuple(dict.fromkeys(canonical_skill_name(skill) for step in definition.steps for skill in step.skill_ids))
    refs = replace(definition.descriptor.refs, scripts=scripts, skills=skills)
    return replace(definition, descriptor=replace(definition.descriptor, refs=refs))


# LLM: 所有写共用根锁及集合锁，来源投影不授写权；WORKFLOW.md只是JSON正文的同步展示副本，不执行步骤。
# 类用途: 为工具、执行引擎与Broker提供工作流唯一存储入口。
class WorkflowStore(_AssetStore):
    # LLM: workspace只登记读取来源，构造时不创建.agent；extra仍只读且不执行任何工作流。
    # 函数用途: 绑定工作流Owner根、项目读取覆盖及全局只读候选。
    def __init__(self, owner_home, *, extra_roots=(), is_admin: bool = False, workspace_root=None):
        super().__init__(owner_home, "workflows", "workflow.json", extra_roots=extra_roots, is_admin=is_admin,
                         workspace_root=workspace_root)

    # LLM: 描述符经冻结值对象复制；全局作用域只加在返回投影，不持久化或改变步骤引用。
    # 函数用途: 标记只读全局工作流的来源。
    def _mark_global(self, definition):
        descriptor = definition.descriptor
        return replace(definition, descriptor=replace(descriptor, scope_layer='global', source_scope='builtin',
            workspace_root='', metadata={**descriptor.metadata, 'asset_scope': 'global'}))

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
        business = definition.descriptor.metadata.get(BUSINESS_WORKFLOW_METADATA)
        if business is not None:
            from ..workflows.orphan_maintenance import update_due_index
            previous = self._get(definition.descriptor.id)
            if previous is not None:
                old_state = previous.descriptor.metadata.get(BUSINESS_WORKFLOW_METADATA, {})
                if (business.get('last_content_edit_at') == old_state.get('last_content_edit_at')
                        and (definition.execution_definition_ref != previous.execution_definition_ref
                             or definition.body_markdown != previous.body_markdown
                             or definition.descriptor.name != previous.descriptor.name)):
                    business = json.loads(json.dumps(business))
                    business['last_content_edit_at'] = time.time()
                    if business['scope']['scope_mode'] == 'unassigned':
                        business['orphan_since'] = business['last_content_edit_at']
                    definition = replace(definition, descriptor=replace(definition.descriptor,
                        metadata={**definition.descriptor.metadata, BUSINESS_WORKFLOW_METADATA: business}))
            update_due_index(self, definition)
        payload = definition.descriptor.to_dict() | {"steps": [step.to_dict() for step in definition.steps],
                                                    "body_markdown": definition.body_markdown}
        self._write(payload, {"WORKFLOW.md": definition.body_markdown or None})
        saved = self._readback_after_commit(definition.descriptor.id)
        if business is not None:
            update_due_index(self, saved, committed=True)
        return saved

    # LLM: 输入共用严格from_any，显式scope选写根，CAS在锁内读取实际目标，投影路径不产生权限。
    # 函数用途: 创建或替换Owner/项目工作流，汇总引用并返回递增revision。
    def save_workflow(self, definition, *, target_scope='owner', expected_revision=None):
        definition = WorkflowDefinition.from_any(definition)
        target = self._target_store(target_scope)
        with target._locked():
            old = target._get(definition.descriptor.id)
            next_asset_revision(old.descriptor.to_dict() if old else None, expected_revision)
            # 普通正文编辑不能借 metadata 改宿主范围、治理或操作回执。
            metadata = dict(definition.descriptor.metadata)
            metadata.pop(BUSINESS_WORKFLOW_METADATA, None)
            if old and BUSINESS_WORKFLOW_METADATA in old.descriptor.metadata:
                state = json.loads(json.dumps(old.descriptor.metadata[BUSINESS_WORKFLOW_METADATA]))
                if (definition.execution_definition_ref != old.execution_definition_ref
                        or definition.body_markdown != old.body_markdown
                        or definition.descriptor.name != old.descriptor.name):
                    state['last_content_edit_at'] = time.time()
                    if state['scope']['scope_mode'] == 'unassigned':
                        state['orphan_since'] = state['last_content_edit_at']
                metadata[BUSINESS_WORKFLOW_METADATA] = state
            definition = replace(definition, descriptor=replace(definition.descriptor, metadata=metadata))
            return target._save(definition)

    def apply_business_operation(self, workflow_id, *, action, intent, idempotency_key,
                                 expected_revision, revision_target, transform):
        """宿主领域命令复用原资产事务；范围、正文和重放回执在同一 canonical JSON。"""
        safe_asset_id(workflow_id)
        if not isinstance(idempotency_key, str) or not 0 < len(idempotency_key.encode('utf-8')) <= 256:
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        identity = hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()
        raw = json.dumps({'action': action, 'intent': intent, 'expected_revision': expected_revision},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        fingerprint = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        with self._locked():
            previous = self._get(workflow_id)
            state = (previous.descriptor.metadata.get(BUSINESS_WORKFLOW_METADATA, {}) if previous else {})
            operations = dict(state.get('operations', {}))
            if receipt := operations.get(identity):
                if receipt['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return json.loads(json.dumps(receipt['result']))
            if revision_target is not None:
                if previous is None:
                    raise KeyError(workflow_id)
                actual = (state.get('scope', {}).get('scope_revision', 0) if revision_target == 'scope'
                          else previous.descriptor.revision)
                if type(expected_revision) is not int or expected_revision != actual:
                    raise ValueError(f'REVISION_CONFLICT: expected={expected_revision}, actual={actual}')
            updated, result = transform(previous)
            next_revision = next_asset_revision(previous.descriptor.to_dict() if previous else None)
            # transform 用下个正式 revision 形成回执；读回 ACK 失败仍沿同键重放，不重复正文写。
            result = json.loads(json.dumps(result, allow_nan=False))
            state = json.loads(json.dumps(updated.descriptor.metadata[BUSINESS_WORKFLOW_METADATA]))
            operations[identity] = {'fingerprint': fingerprint, 'result': result, 'revision': next_revision}
            state['operations'] = operations
            updated = replace(updated, descriptor=replace(updated.descriptor,
                metadata={**updated.descriptor.metadata, BUSINESS_WORKFLOW_METADATA: state}))
            self._save(updated)
            return result

    # LLM: 项目冷读只用readonly视图，坏记录不回退；返回值不能决定后续更新写根。
    # 函数用途: 依项目、Owner、全局优先级读取工作流定义。
    def get_workflow(self, workflow_id):
        safe_asset_id(workflow_id)
        if self.workspace_root is not None:
            from .workspace_assets import WorkspaceAssetOverlay, _workflow_definition

            record = WorkspaceAssetOverlay(self.workspace_root, owner_home=self.owner_home,
                builtin_roots=self._global_roots).get_asset('workflow', workflow_id)
            return _workflow_definition(record) if record is not None else None
        with self._locked():
            definition = self._get(workflow_id)
        return definition if definition is not None else self._global_get(workflow_id)

    # LLM: 有workspace复用只读覆盖入口，不创建.agent；同ID只读取首个实际来源。
    # 函数用途: 合并项目、Owner及全局工作流，按ID稳定返回定义。
    def list_workflows(self):
        if self.workspace_root is not None:
            from .workspace_assets import WorkspaceAssetOverlay, _workflow_definition

            return [_workflow_definition(row) for row in WorkspaceAssetOverlay(self.workspace_root,
                owner_home=self.owner_home, builtin_roots=self._global_roots).list_assets('workflow')]
        with self._locked():
            records = self._local_records()
        return self._merge_globals(records)

    # LLM: 先验证权威定义再受控删除；不跟随目录或文件链接。
    # 函数用途: 删除单个工作流目录，缺失返回False。
    def delete_workflow(self, workflow_id):
        safe_asset_id(workflow_id)
        with self._locked():
            definition = self._get(workflow_id)
            if definition is None:
                return False
            if BUSINESS_WORKFLOW_METADATA in definition.descriptor.metadata:
                raise PermissionError('WORKFLOW_BUSINESS_RECYCLE_REQUIRED')
            return self._delete(workflow_id)

    # LLM: 仅本Owner原根锁/集合锁内改变运行闸；两层enabled同步，不修改步骤、凭据或运行快照。
    # 函数用途: 显式开启或停用已存在工作流，返回正式存储读回值。
    def set_enabled(self, workflow_id: str, enabled: bool) -> WorkflowDefinition:
        if type(enabled) is not bool:
            raise ValueError('enabled必须是布尔值')
        with self._locked():
            definition = self._required(workflow_id)
            return self._save(replace(definition, enabled=enabled,
                descriptor=replace(definition.descriptor, enabled=enabled)))

    # LLM: attach/detach同用锁内命令，模式检查不可只留在工具wrapper；精确引用不去前缀。
    # 函数用途: 动态给现有工作流步骤挂载一个Skill。
    def attach_skill_to_step(self, workflow_id, step_id, skill_id):
        return self._change_skill_binding(workflow_id, step_id, skill_id, remove=False)

    # LLM: 与attach共享原子读改写，不在工具层重建定义或覆盖其它步骤。
    # 函数用途: 从指定分析或混合步骤解绑一个精确Skill引用。
    def detach_skill_from_step(self, workflow_id, step_id, skill_id):
        return self._change_skill_binding(workflow_id, step_id, skill_id, remove=True)

    # LLM: 在原根/集合锁内查模式、只替目标步重算refs；完全相同的绑定不写盘或推进revision，不接受裸路径。
    # 函数用途: 幂等执行Skill挂载或解绑，避免两个存储权威和丢更新。
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
            if skills == target.skill_ids:
                return definition
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
                if BUSINESS_WORKFLOW_METADATA in definition.descriptor.metadata:
                    state = json.loads(json.dumps(definition.descriptor.metadata[BUSINESS_WORKFLOW_METADATA]))
                    state['last_run_at'] = time.time()
                    if state['scope']['scope_mode'] == 'unassigned':
                        state['orphan_since'] = state['last_run_at']
                    definition = replace(definition, descriptor=replace(definition.descriptor,
                        metadata={**definition.descriptor.metadata, BUSINESS_WORKFLOW_METADATA: state}))
                return self._save(replace(definition, descriptor=replace(definition.descriptor, runtime_summary=summary)))
        definition = self._global_get(workflow_id)
        if definition is None:
            raise KeyError(workflow_id)
        return definition
