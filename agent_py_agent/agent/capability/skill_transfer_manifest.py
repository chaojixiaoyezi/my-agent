# LLM: 迁移清单只固定当前owner明确选定的私有Skill/资源字节；公共包、配置、聊天和任意路径都不是导出入口。
# 模块用途: 为双端人工确认生成不可变清单、逐文件摘要与安全快照，不执行脚本或授予工具权限。
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import read_bytes_beneath
from ..user_space.owner_resolver import OwnerIdentity
from .skill_learning_store import SkillLearningStore
from .skill_snapshot import skill_content_sha256

# 单次技能投递全部资源的累计字节预算，不授予接收方额外权限。
MAX_TRANSFER_BYTES = 8 * 1024 * 1024
# 单次技能投递最多包含的资源文件数，包含声明文件。
MAX_TRANSFER_FILES = 128
# 单次技能投递最多包含的技能数，仍由原安装确认与配额决定接收。
MAX_TRANSFER_SKILLS = 100


# LLM: 固定码作为机器事实；不保留正文、路径或底层异常，调用方不能解析中文授予权限。
# 类用途: 表示准备、授权或接收被安全合同拒绝。
class SkillTransferError(ValueError):
    # LLM: code必须由宿主定义，异常只携该码，不复制秘密。
    # 函数用途: 构造迁移的安全失败事实。
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# LLM: JSON摘要使用固定序列化且拒绝NaN，所有grant和去重都按同一算法。
# 函数用途: 计算不受键顺序影响的清单hash。
def manifest_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


# LLM: owner三元组由宿主规范路径投影生成，不能由模型声明自己是管理员或其它owner。
# 函数用途: 获取当前host的稳定接收/发送身份。
def host_owner(host):
    home = host.home_paths
    owner_id = home.owner_id.split('/')[-1]
    return asdict(OwnerIdentity(home.owner_provider, home.owner_kind, owner_id))


# LLM: 资源路径只允许Skill目录内显式相对普通文件，读取始终锚定原owner/skills，不把嵌套父路径晋升可信根；禁止链接、隐藏配置和秘密形态，不按未知扩展名自动拒绝。
# 函数用途: 校验一条清单资源名，保持开放文件格式与准确目录边界。
def resource_parts(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise SkillTransferError('SKILL_TRANSFER_RESOURCE_INVALID')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {'', '.', '..'} or part.startswith('.') for part in path.parts):
        raise SkillTransferError('SKILL_TRANSFER_RESOURCE_INVALID')
    return path.parts


# LLM: 对所有字节做既有敏感形态检查；不静默脱敏改hash，命中直接拒绝，异常不携命中内容。
# 函数用途: 拒绝清单内可识别密钥/口令，返回原字节以供准确摘要和复制。
def safe_payload(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_TRANSFER_BYTES:
        raise SkillTransferError('SKILL_TRANSFER_TOO_LARGE')
    text = raw.decode('utf-8', errors='replace')
    if redact_sensitive_text(text, code_file=True) != text:
        raise SkillTransferError('SKILL_TRANSFER_SECRET_BLOCKED')
    return raw


# LLM: stable_id/hash必须匹配当前合法快照，路径还须在当前owner/skills；source=owner不等于允许导出包或其他owner。
# 函数用途: 冻结显式Skill与资源清单，公共/受管来源只可沿原授权引用使用，不能复制出私有副本。
def build_skill_bundles(agent, selections):
    if not isinstance(selections, list) or not 0 <= len(selections) <= MAX_TRANSFER_SKILLS:
        raise SkillTransferError('SKILL_TRANSFER_MANIFEST_INVALID')
    if not selections:
        return []
    snapshot = agent.current_skill_snapshot()
    root = Path(agent.home_paths.owner_home_dir) / 'skills'
    registry = SkillLearningStore.for_home(agent.home_paths).load_registry()
    result, names = [], set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) - {'stable_id', 'content_sha256', 'resources', 'target_name'}:
            raise SkillTransferError('SKILL_TRANSFER_MANIFEST_INVALID')
        entry = snapshot.resolve(str(selection.get('stable_id') or ''))
        if entry is None or entry.stable_id != selection.get('stable_id') or entry.source != 'owner' or entry.content_sha256 != selection.get('content_sha256'):
            raise SkillTransferError('SKILL_TRANSFER_SOURCE_NOT_EXPORTABLE')
        path = Path(entry.path)
        if path.is_symlink() or not path.is_relative_to(root) or path.name != 'SKILL.md':
            raise SkillTransferError('SKILL_TRANSFER_SOURCE_NOT_EXPORTABLE')
        body = _read_export_file(root, path.relative_to(root).parts)
        if body is None or skill_content_sha256(body.decode()) != entry.content_sha256:
            raise SkillTransferError('SKILL_TRANSFER_SOURCE_CHANGED')
        origin = {'owner': host_owner(agent), 'stable_id': entry.stable_id, 'content_sha256': entry.content_sha256}
        name = selection.get('target_name') or 'shared-' + manifest_hash(origin)[:16]
        if not isinstance(name, str) or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name) is None or name in names:
            raise SkillTransferError('SKILL_TRANSFER_NAME_CONFLICT')
        names.add(name)
        _check_export_budget(result, {}, body)
        files = {'SKILL.md': _file_record(body)}
        resources = selection.get('resources', [])
        if not isinstance(resources, list) or len(resources) >= MAX_TRANSFER_FILES:
            raise SkillTransferError('SKILL_TRANSFER_MANIFEST_INVALID')
        for resource in resources:
            parts = resource_parts(resource)
            if resource == 'SKILL.md' or resource in files:
                raise SkillTransferError('SKILL_TRANSFER_RESOURCE_INVALID')
            raw = _read_export_file(root, (*path.parent.relative_to(root).parts, *parts))
            if raw is None:
                raise SkillTransferError('SKILL_TRANSFER_RESOURCE_INVALID')
            _check_export_budget(result, files, raw)
            files[resource] = _file_record(raw)
        registered = registry.skills.get(entry.name)
        result.append({'name': name, 'source_name': entry.name, 'origin': origin,
            'source_revision': getattr(registered, 'revision', None), 'files': files})
    if sum(file['size'] for item in result for file in item['files'].values()) > MAX_TRANSFER_BYTES or sum(len(item['files']) for item in result) > MAX_TRANSFER_FILES:
        raise SkillTransferError('SKILL_TRANSFER_TOO_LARGE')
    return result


# LLM: 每读取一个显式文件后立即核对整批上限，避免先在内存堆满所有资源才拒绝；不按扩展名放宽预算。
# 函数用途: 在生成base64快照前限制准确字节和文件总数。
def _check_export_budget(items, current, raw):
    records = [record for item in items for record in item['files'].values()] + list(current.values())
    if len(records) + 1 > MAX_TRANSFER_FILES or sum(record['size'] for record in records) + len(raw) > MAX_TRANSFER_BYTES:
        raise SkillTransferError('SKILL_TRANSFER_TOO_LARGE')


# LLM: 清单内bytes采用base64存于原owner暂存，公开投影必须删除data，不向模型返回原文或资源。
# 函数用途: 为一份安全字节快照附加准确长度和摘要。
def _file_record(raw):
    raw = safe_payload(raw)
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'data': base64.b64encode(raw).decode()}


# LLM: 接收端重新检查每一字节与清单长度/hash和秘密；签名不能替代资源校验。
# 函数用途: 解码已授权清单文件，坏或被篡改的快照拒绝整批提交。
def file_bytes(record):
    try:
        raw = base64.b64decode(record['data'], validate=True)
        if len(raw) != record['size'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('changed')
        return safe_payload(raw)
    except (KeyError, TypeError, ValueError):
        raise SkillTransferError('SKILL_TRANSFER_SNAPSHOT_INVALID') from None


# LLM: 模型与目标确认前只见其授权owner的元数据；不把bundle字节、绝对路径或其它owner目录交给模型。
# 函数用途: 生成可供真人核对的准确资源清单摘要。
def public_manifest(manifest):
    return {**{key: value for key, value in manifest.items() if key != 'skills'}, 'skills': [
        {**{key: value for key, value in item.items() if key != 'files'}, 'files': {
            path: {key: value for key, value in record.items() if key != 'data'} for path, record in item['files'].items()}}
        for item in manifest['skills']]}


# LLM: nofollow失败属于客观路径拒绝，不暴露底层路径或转成可重试权限放宽。
# 函数用途: 有界读取精确导出普通文件，链接/目录/越界统一拒绝。
def _read_export_file(root, parts):
    try:
        return read_bytes_beneath(root, parts, max_bytes=MAX_TRANSFER_BYTES, require_dir_fd=True)
    except (OSError, RuntimeError, ValueError):
        raise SkillTransferError('SKILL_TRANSFER_RESOURCE_INVALID') from None
