# LLM: 只沿可信root的nofollow文件链读，拒绝未完成资产事务；不调用会mkdir/恢复/建锁的资产Store。
# 模块用途: 为观察和冻结执行提供单一无写入文件读取器。
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from ..capability.skills import split_skill_frontmatter
from ..common.nofollow_fs import list_names_beneath, open_directory_beneath, read_bytes_beneath
from ..plugin_host.owner_context import reject_constant, unique_object
from ..user_space.owner_resolver import resolve_owner_home
from .access_policy import AuditGroupError, owner_identity

KINDS = ("script", "skill", "workflow", "monitor", "datasource")


# LLM: 每份JSON最多4MiB，重复字段与NaN拒绝；不存在与损坏分开。
# 函数用途: 读取规范相对路径上的一个JSON对象，完全不物化目录。
def read_object(root, parts):
    raw = read_bytes_beneath(root, parts, max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    if raw is None:
        return None
    result = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if type(result) is not dict:
        raise ValueError("invalid object")
    return result


# LLM: 仅缺目录是空，权限/链接错误不当空；列表超界拒绝而非假complete。
# 函数用途: 有界枚举一个固定来源目录的直接条目。
def names(root, parts):
    try:
        values = sorted(list_names_beneath(root, parts))
    except FileNotFoundError:
        return []
    if len(values) > 2000:
        raise ValueError("source listing limit")
    return [value for value in values if not value.startswith(".")]


# LLM: 技能树里README等普通文件不是子技能；符号链接不能被当成可扫描目录或悄悄声称完整。
# 函数用途: 只枚举准确来源下的普通子目录，不跟随链接或物化目录。
def child_directories(root, parts):
    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        with os.scandir(descriptor) as entries:
            rows = [entry for entry in entries if not entry.name.startswith(".")]
        if len(rows) > 2000 or any(entry.is_symlink() for entry in rows):
            raise ValueError("invalid skill directory")
        return sorted(entry.name for entry in rows if entry.is_dir(follow_symlinks=False))
    finally:
        os.close(descriptor)


# LLM: 资产ID为安全单叶，任何路径/作用域前缀都不隐式降级为caller或全局来源。
# 函数用途: 检查读取目标不能穿越来源目录。
def asset_identifier(value):
    import re
    if type(value) is not str or re.fullmatch(r"[\w][\w.-]{0,127}", value) is None or ".." in value:
        raise AuditGroupError("INVALID_ASSET_ID", status_code=400)
    return value


# LLM: home只由owner_resolver产生，metadata不授路径；不创建source_home或锁。
# 类用途: 对一个准确owner读取资产定义、运行状态和依赖。
class ReadonlyAssetStore:
    # LLM: 冷构造无IO；root规范化仅针对宿主输入。
    # 函数用途: 固定一个来源用户的规范路径。
    def __init__(self, root, owner):
        self.root = Path(root).resolve()
        self.owner = resolve_owner_home(self.root, owner_identity(owner))
        self.home = self.owner.home_dir
        self.parts = self.home.relative_to(self.root).parts

    # LLM: 从宿主root打开整条owner链，禁止先resolve来源符号链接。
    # 函数用途: 读取此owner里的一个固定JSON叶。
    def object(self, *parts):
        return read_object(self.root, (*self.parts, *parts))

    # LLM: 原批量及单资产事务未提交即拒读，避免观察或执行不一致的半版本。
    # 函数用途: 在无写入前提下检查资产发布是否完成。
    def check_transaction(self, *parts):
        for leaf in ((".asset-batch.json",), (*parts, "asset-transaction.json")):
            if self.object(*leaf) is not None:
                raise ValueError("uncommitted asset transaction")

    # LLM: Scheduler store为唯一调度状态，坏jobs/runs不能按空处理。
    # 函数用途: 取得冻结监控和告警使用的原调度账本。
    def scheduler(self):
        value = self.object("data", "scheduler", "store.json") or {"jobs": {}, "runs": {}}
        if type(value.get("jobs")) is not dict or type(value.get("runs", {})) is not dict:
            raise ValueError("invalid scheduler")
        return value

    # LLM: 只枚举本owner，技能最多六级且只进入普通目录，README不误作技能；不发现其它作用域同名资产。
    # 函数用途: 列出一种资产的准确本地ID及技能相对目录。
    def entries(self, kind):
        if kind == "monitor":
            jobs = self.scheduler()["jobs"]
            bindings = names(self.root, (*self.parts, "data", "monitors"))
            return [(asset_identifier(identifier), ()) for identifier in sorted(set(jobs) | {
                name[:-5] for name in bindings if name.endswith(".json")})]
        collection = {"script": "scripts", "workflow": "workflows", "skill": "skills", "datasource": "datasources"}[kind]
        if kind != "skill":
            return [(asset_identifier(name), (collection, name)) for name in names(self.root, (*self.parts, collection))]
        pending, result = [("skills",)], []
        while pending:
            parts = pending.pop()
            children = names(self.root, (*self.parts, *parts))
            if "SKILL.md" in children:
                result.append((asset_identifier(parts[-1]), parts))
            elif children and len(parts) < 7:
                pending.extend((*parts, name) for name in child_directories(self.root, (*self.parts, *parts)))
            if len(pending) + len(result) > 2000:
                raise ValueError("source listing limit")
        return sorted(result)

    # LLM: 读取同owner元数据，monitor修订合并binding和job；绝不从job.prompt生成summary。
    # 函数用途: 冻结资产定义及内部正文，调用方只能公开白名单投影。
    def get(self, kind, identifier, *, skill_parts=None):
        identifier = asset_identifier(identifier)
        if kind == "monitor":
            self.check_transaction()
            binding = self.object("data", "monitors", identifier + ".json") or {}
            job = self.scheduler()["jobs"].get(identifier, {})
            if not binding and not job:
                raise FileNotFoundError()
            return {**binding, "id": identifier, "kind": kind, "job": job,
                    "summary": binding.get("summary", ""), "name": binding.get("name") or job.get("name", identifier),
                    "enabled": binding.get("enabled", True) is True and job.get("status") == "active"
                        and binding.get("schedule_spec", {}).get("enabled", True) is True,
                    "revision": f'{binding.get("revision", 0)}:{job.get("version", 0)}',
                    "runtime_summary": {"run_count": job.get("run_count", 0), "last_run_at": job.get("last_run_at"),
                                        "last_status": job.get("last_run_status", "")}}
        if kind == "skill":
            matches = [parts for name, parts in self.entries(kind) if name == identifier] if skill_parts is None else [skill_parts]
            if len(matches) != 1:
                raise FileNotFoundError()
            parts = matches[0]
            self.check_transaction(*parts)
            body = self.text(*parts, "SKILL.md")
            meta = self.object(*parts, "skill_meta.json") or {}
            data = {**split_skill_frontmatter(body)[0], **meta, "body_markdown": body}
            data.update(id=identifier, kind="skill", revision=sha256((body + json.dumps(meta, sort_keys=True)).encode()).hexdigest())
            return data
        collection = {"script": "scripts", "workflow": "workflows", "datasource": "datasources"}[kind]
        self.check_transaction(collection, identifier)
        data = self.object(collection, identifier, kind + ".json")
        if data is None:
            raise FileNotFoundError()
        if data.get("id") != identifier:
            raise ValueError("asset identity mismatch")
        return data

    # LLM: 源码只在宿主内存里，文本长度有界且nofollow，不产生source锁或输出文件。
    # 函数用途: 为冻结执行读取准确的脚本或技能正文。
    def text(self, *parts):
        raw = read_bytes_beneath(self.root, (*self.parts, *parts), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
        if raw is None:
            raise FileNotFoundError()
        return raw.decode("utf-8")
