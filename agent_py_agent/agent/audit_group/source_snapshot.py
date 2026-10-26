# LLM: 只冻结同一source_owner的完整依赖；不存在就拒绝，绝不回退caller/shared/global。
# 模块用途: 构造仅在宿主内存存活、不可公开序列化的资产执行快照。
from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256

from .access_policy import AuditGroupError, require_source_enabled
from .readonly_store import ReadonlyAssetStore, asset_identifier
from .snapshot_guard import source_snapshot_guard


# LLM: 技能只有明确结构化IO和脚本执行合同可跨owner运行；正文从不变成caller提示词。
# 函数用途: 返回宿主隔离技能单元的准确脚本依赖，无合同即拒绝。
def skill_script(raw):
    contract = raw.get("metadata", {}).get("blackbox", {})
    outputs = raw.get("io_contract", {}).get("outputs", [])
    if (type(contract) is not dict or set(contract) != {"executor", "script_id"}
            or contract["executor"] != "script" or not isinstance(outputs, (tuple, list)) or not outputs
            or any(type(key) is not str for key in outputs)):
        raise AuditGroupError("AUDIT_GROUP_SKILL_BLACKBOX_UNSUPPORTED", status_code=409)
    return asset_identifier(contract["script_id"])


# LLM: 仅run动作的脚本/工作流是监控黑盒调用目标；不执行任意工具、计划或通知动作。
# 函数用途: 将冻结监控工具动作解析为同owner资产引用和输入。
def monitor_action(rule):
    action = rule.get("tool") or rule.get("precheck") or {}
    name, arguments = action.get("name"), action.get("arguments", {})
    if rule.get("use_llm", False) or name not in {"scripts", "workflows"} or arguments.get("action") != "run":
        raise AuditGroupError("AUDIT_GROUP_MONITOR_BLACKBOX_UNSUPPORTED", status_code=409)
    kind = "script" if name == "scripts" else "workflow"
    return kind, asset_identifier(arguments.get(kind + "_id")), arguments.get("inputs", {})


# LLM: 明确规则先按ID唯一定位并核启停，不触碰其它规则的LLM或资产依赖；不明确时保持原启用规则集合。
# 函数用途: 为冻结与执行共用准确的监控规则选择及缺失/禁用错误。
def monitor_rules(raw, rule_id=None):
    job = raw["job"]
    rules = job.get("rules") or [job]
    if not rule_id:
        return [rule for rule in rules if rule.get("enabled", True)]
    selected = [rule for rule in rules if rule.get("id", "") == rule_id]
    if not selected:
        raise AuditGroupError("RULE_NOT_FOUND", status_code=404)
    if len(selected) != 1:
        raise AuditGroupError("ASSET_DEPENDENCY_INVALID", status_code=409)
    if selected[0].get("enabled", True) is not True:
        raise AuditGroupError("ASSET_DISABLED", status_code=409)
    return selected


# LLM: 只允许准确owner:技能引用；其它来源无法在source闭包内证明，不能删前缀猜同名。
# 函数用途: 解析工作流里的同来源技能引用。
def local_skill_id(value):
    if type(value) is not str or ":" in value and not value.startswith("owner:"):
        raise AuditGroupError("ASSET_DEPENDENCY_MISSING", status_code=409)
    return asset_identifier(value.removeprefix("owner:"))


# LLM: 内部正文和定义只保存在repr=False的私有映射里；本类型没有to_dict等公开出口。
# 类用途: 固定一次调用的源码、依赖、修订与内容指纹。
@dataclass(frozen=True)
class SourceAssetSnapshot:
    owner_id: str
    kind: str
    identifier: str
    revision: str | int
    _records: dict = field(repr=False)
    fingerprint: str = ""
    rule_id: str = ""

    # LLM: 先核来源状态，再读取选定规则的依赖闭包；规则选择绑定指纹，后续复核不得退回全部规则。
    # 函数用途: 构造一个同源冻结执行单元。
    @classmethod
    def freeze(cls, root, source_owner, kind, identifier, *, rule_id=None):
        home = require_source_enabled(root, source_owner)
        reader = ReadonlyAssetStore(root, home.identity)
        records, checked = {}, {}
        with source_snapshot_guard(reader):
            _freeze_node(reader, kind, identifier, records, set(), rule_id=rule_id)
            _freeze_node(reader, kind, identifier, checked, set(), rule_id=rule_id)
            if records != checked:
                raise AuditGroupError("REVISION_CONFLICT", status_code=409)
        primary = records[(kind, identifier)]
        signature = sha256(json.dumps([rule_id or "", sorted((key, value) for key, value in records.items())],
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return cls(home.owner_id, kind, identifier, primary.get("revision", 0), records, signature, rule_id or "")

    # LLM: 只返回宿主内部冻结值，调用方必须继续做输出白名单投影。
    # 函数用途: 查找已冻结的一个准确依赖。
    def record(self, kind, identifier):
        return self._records[(kind, identifier)]


# LLM: 循环和缺失依赖均拒绝；仅首层监控使用明确规则选择，递归依赖仍完整冻结，脚本code只在内存。
# 函数用途: 递归冻结一个节点和它的同源启用依赖。
def _freeze_node(reader, kind, identifier, records, pending, *, rule_id=None):
    key = (kind, asset_identifier(identifier))
    if key in pending or len(pending) > 24 or len(records) >= 128:
        raise AuditGroupError("ASSET_DEPENDENCY_INVALID", status_code=409)
    if key in records:
        return
    pending.add(key)
    try:
        raw = reader.get(kind, identifier)
    except FileNotFoundError:
        raise AuditGroupError("ASSET_DEPENDENCY_MISSING", status_code=409) from None
    if raw.get("enabled", True) is not True:
        raise AuditGroupError("ASSET_DISABLED_BY_OWNER", status_code=409)
    if kind == "script":
        entrypoint = asset_identifier(raw.get("entrypoint", "main.py"))
        raw = {**raw, "code": reader.text("scripts", identifier, entrypoint)}
    for child_kind, child_id in _dependencies(kind, raw, rule_id=rule_id):
        _freeze_node(reader, child_kind, child_id, records, pending)
    records[key] = raw
    pending.remove(key)


# LLM: 闭包来自明确refs、步骤及已选监控动作；规则先选中才校验LLM/依赖，不解析Prompt找执行权。
# 函数用途: 列出需要一起冻结和校验enabled的同源资产。
def _dependencies(kind, raw, *, rule_id=None):
    result = []
    if kind == "skill":
        result.append(("script", skill_script(raw)))
    if kind == "workflow":
        for step in raw.get("steps", []):
            if step.get("script_id"):
                result.append(("script", asset_identifier(step["script_id"])))
            result.extend(("skill", local_skill_id(value)) for value in step.get("skill_ids", []))
    if kind == "monitor":
        for rule in monitor_rules(raw, rule_id):
            child_kind, child_id, _inputs = monitor_action(rule)
            result.append((child_kind, child_id))
    for collection, ids in raw.get("refs", {}).items():
        child_kind = {"scripts": "script", "workflows": "workflow", "skills": "skill", "datasources": "datasource"}.get(collection)
        if child_kind is not None:
            result.extend((child_kind, asset_identifier(value)) for value in ids)
    return list(dict.fromkeys(result))
