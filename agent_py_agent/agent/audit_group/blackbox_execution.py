# LLM: 执行仅消费宿主冻结的同源定义；不构造原WorkflowRun，不运行Scheduler或主头像。
# 模块用途: 在隔离单元中串联脚本、结构化技能、工作流及监控单次复核。
from __future__ import annotations

import json
import os
import stat
import time
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from ..workflows.script_runner import parse_script_output
from .access_policy import AuditGroupError
from .safe_projection import scrub_private_text
from .source_snapshot import local_skill_id, monitor_action, monitor_rules, skill_script


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

    # LLM: 不创建job/run/通知或checkpoint；明确目标与冻结共用选择，复合监控仍须rule_id，条件沿完整job判断。
    # 函数用途: 脱离调度账本单次验证一条启用规则。
    def monitor(self, raw, inputs, rule_id):
        from ..scheduler.monitor_state import condition_matches
        job = raw["job"]
        candidates = monitor_rules(raw, rule_id) if rule_id else job.get("rules") or [job]
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


# LLM: 仅扫描本次独占workspace元数据，最多4096项，跳过链接/特殊文件；不扫描普通项目或授沙箱写权。
# 函数用途: 在调用前后记录普通单链接文件身份，区分本次新增与修改的输出。
def workspace_inventory(workspace):
    result, count = {}, 0
    for directory, folders, files, fd in os.fwalk(workspace, follow_symlinks=False):
        count += len(folders) + len(files)
        if count > 4096:
            raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
        for name in files:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                path = os.path.relpath(os.path.join(directory, name), workspace)
                result[path] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return result


# LLM: 独占输出先全批有界校验/净化；原配额锁内暂存后同父FD目录原子发表，失败不留部分正式产物。
# 函数用途: 将本次安全文本产物一次发表到caller自己的执行目录，并返回实际字节元数据。
def generated_artifacts(home, parts, before, records, *, workspace):
    after, rows, payloads, total = workspace_inventory(workspace), [], [], 0
    for path, identity in sorted(after.items()):
        if before.get(path) == identity:
            continue
        if len(rows) >= 32:
            raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
        raw = read_bytes_beneath(workspace, tuple(path.split(os.sep)), max_bytes=1024 * 1024, require_dir_fd=True)
        if raw is None or workspace_inventory(workspace).get(path) != identity:
            raise AuditGroupError("AUDIT_GROUP_ARTIFACT_CHANGED", status_code=409)
        try:
            data = scrub_private_text(raw.decode("utf-8"), records).encode("utf-8")
        except UnicodeError:
            raise AuditGroupError("AUDIT_GROUP_ARTIFACT_FORMAT_UNSUPPORTED", status_code=409) from None
        total += len(data)
        if total > 4 * 1024 * 1024:
            raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
        target = (*parts, "artifacts", f"artifact-{len(rows) + 1:04d}.txt")
        payloads.append((target, data))
        rows.append({"name": scrub_private_text(os.path.basename(path), records)[:128],
                     "size_bytes": len(data), "sha256": sha256(data).hexdigest(), "relative_path": "/".join(target)})
    if payloads:
        quota = owner_quota_enforcer_from_policy(home.home_dir)
        changes = tuple(OwnerQuotaChange(home.home_dir.joinpath(*target), len(data)) for target, data in payloads)
        with quota.reserve(changes), TemporaryDirectory(prefix="publish-", dir=home.home_dir.joinpath(*parts)) as temporary:
            temporary = Path(temporary)
            for target, data in payloads:
                write_bytes_atomic_beneath(home.home_dir, (*temporary.relative_to(home.home_dir).parts, target[-1]), data)
            fd = open_directory_beneath(home.home_dir, parts)
            try:
                os.rename(temporary.name, "artifacts", src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                os.close(fd)
    return rows
