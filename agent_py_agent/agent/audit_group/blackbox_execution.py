# LLM: 执行仅消费宿主冻结的同源定义；不构造原WorkflowRun，不运行Scheduler或主头像。
# 模块用途: 在隔离单元中串联脚本、结构化技能、工作流及监控单次复核。
from __future__ import annotations

import json
import time

from ..workflows.script_runner import parse_script_output
from .access_policy import AuditGroupError
from .source_snapshot import local_skill_id, monitor_action, skill_script


# LLM: outputs为明确字段白名单，未声明字段即使执行成功也不得返回；不传stdout/stderr/上下文。
# 函数用途: 校验结构化结果并裁掉合同之外的字段。
def contract_output(raw, result):
    contract = raw.get("io_contract", {})
    outputs = contract.get("outputs", contract.get("expected_output_keys", []))
    if not isinstance(outputs, (list, tuple)) or not outputs or any(type(key) is not str for key in outputs):
        raise AuditGroupError("AUDIT_GROUP_STRUCTURED_CONTRACT_REQUIRED", status_code=409)
    value, error = parse_script_output(json.dumps(result, allow_nan=False), contract)
    if error:
        raise AuditGroupError("AUDIT_GROUP_INVALID_OUTPUT", status_code=409)
    return {key: value[key] for key in outputs}


# LLM: 剩余期限穿过所有依赖步骤，不能每步重置总超时；skill正文从不交给LLM或caller。
# 类用途: 消费一个只驻留内存的快照，执行受控依赖。
class SnapshotExecutor:
    # LLM: 固定caller写根和宿主隔离单元，定义不能覆盖它们。
    # 函数用途: 准备一次有总期限的执行上下文。
    def __init__(self, snapshot, unit, workspace, staging, timeout_s, *, authorize=None):
        self.snapshot, self.unit = snapshot, unit
        self.workspace, self.staging = workspace, staging
        self.deadline = time.monotonic() + timeout_s
        self.authorize = authorize

    # LLM: 执行分派只依赖结构化kind，不解释Prompt；所有脚本限于强制平台沙箱。
    # 函数用途: 执行冻结节点并返回合同白名单结果。
    def run(self, kind, identifier, inputs, *, rule_id=""):
        raw = self.snapshot.record(kind, identifier)
        if self.authorize is not None:
            self.authorize(kind)
        if time.monotonic() >= self.deadline:
            raise AuditGroupError("AUDIT_GROUP_TIMEOUT", status_code=408)
        if kind == "script":
            if raw.get("interpreter", "python3") != "python3":
                raise AuditGroupError("AUDIT_GROUP_INTERPRETER_UNSUPPORTED", status_code=409)
            if raw.get("data_sources") or raw.get("credential_refs"):
                raise AuditGroupError("AUDIT_GROUP_DATASOURCE_BLACKBOX_UNSUPPORTED", status_code=409)
            result = self.unit.execute(raw["code"], inputs, workspace=self.workspace, staging=self.staging,
                                       timeout_s=min(self.deadline - time.monotonic(), float(raw.get("timeout_seconds", 30))))
            return contract_output(raw, result)
        if kind == "skill":
            return contract_output(raw, self.run("script", skill_script(raw), inputs))
        if kind == "workflow":
            return self.workflow(raw, inputs)
        if kind == "monitor":
            return self.monitor(raw, inputs, rule_id)
        raise AuditGroupError("AUDIT_GROUP_ASSET_NOT_INVOCABLE", status_code=400)

    # LLM: 只跑明确脚本和结构化技能步骤，input_map引用只读本次结果；未知模式/条件失败关闭，不调模型。
    # 函数用途: 顺序执行冻结工作流，不保存源码或内部步骤快照。
    def workflow(self, raw, inputs):
        results, last = {}, {}
        steps = raw.get("steps", [])
        if not steps or len(steps) > 128:
            raise AuditGroupError("AUDIT_GROUP_WORKFLOW_UNSUPPORTED", status_code=409)
        for step in steps:
            identifier = step["step_id"]
            if identifier in results:
                raise AuditGroupError("AUDIT_GROUP_WORKFLOW_UNSUPPORTED", status_code=409)
            if not step_condition(step.get("condition"), inputs, results):
                results[identifier] = {}
                continue
            values = step_inputs(step.get("inputs_map", {}), inputs, results)
            mode = step.get("mode")
            if mode not in {"deterministic_script", "llm_skill_analysis", "hybrid"}:
                raise AuditGroupError("AUDIT_GROUP_WORKFLOW_UNSUPPORTED", status_code=409)
            last = self.run("script", step["script_id"], values) if step.get("script_id") else values
            for skill in step.get("skill_ids", []):
                last = self.run("skill", local_skill_id(skill), last)
            if not step.get("script_id") and not step.get("skill_ids"):
                raise AuditGroupError("AUDIT_GROUP_WORKFLOW_UNSUPPORTED", status_code=409)
            results[identifier] = last
        return contract_output(raw, last)

    # LLM: 不创建job/run/通知，不写checkpoint；复合规则需准确选中且依赖条件沿冻结值判断。
    # 函数用途: 脱离调度账本单次验证一条启用规则。
    def monitor(self, raw, inputs, rule_id):
        from ..scheduler.monitor_state import condition_matches
        job = raw["job"]
        candidates = [rule for rule in job.get("rules") or [job] if not rule_id or rule.get("id", "") == rule_id]
        if len(candidates) != 1:
            raise AuditGroupError("AUDIT_GROUP_RULE_REQUIRED", status_code=400)
        rule = {"depends_on": [], "condition": None, **candidates[0]}
        if rule.get("enabled", True) is not True:
            raise AuditGroupError("ASSET_DISABLED_BY_OWNER", status_code=409)
        if job.get("rules") and not condition_matches(job, rule, time.time()):
            raise AuditGroupError("AUDIT_GROUP_RULE_CONDITION_UNMET", status_code=409)
        kind, identifier, fixed_inputs = monitor_action(rule)
        return self.run(kind, identifier, {**fixed_inputs, **inputs})


# LLM: 字符串表达式不可求值；仅支持原结构化step_id/key/equals条件。
# 函数用途: 确定性检查工作流步骤条件。
def step_condition(condition, inputs, results):
    if condition in (None, "", {}):
        return True
    if type(condition) is not dict or set(condition) != {"step_id", "key", "equals"}:
        raise AuditGroupError("AUDIT_GROUP_WORKFLOW_UNSUPPORTED", status_code=409)
    source = inputs if condition["step_id"] == "$inputs" else results[condition["step_id"]]
    return source.get(condition["key"]) == condition["equals"]


# LLM: 只从本次输入和已完成步骤取字段，不能读来源路径或caller同名资产。
# 函数用途: 解析原工作流结构化input_map。
def step_inputs(mapping, inputs, results):
    values = {}
    for key, reference in mapping.items():
        if isinstance(reference, dict) and set(reference) == {"step_id", "key"}:
            source = inputs if reference["step_id"] == "$inputs" else results[reference["step_id"]]
            values[key] = source[reference["key"]]
        else:
            values[key] = reference
    return values or dict(inputs)
