# LLM: 线程快照只有唯一文件；内容损坏在原锁内修复且只记原因码，安全路径仍拒绝；截断字段不从文案推断。
# 模块用途: 冻结人格前缀并展示期间行变化；坏缓存不阻断回合，截断窗口移出不当作撤销。
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from ..common.nofollow_fs import ensure_private_dir
from ..common.opaque_id import validate_path_segment

# 动态人格行变化总字符预算；超出仅给数量，避免尾巴吞掉窗口。
PERSONA_UPDATES_MAX_CHARS = 4000


# LLM: 只接受已提交线程的代次/所选档案与实际协议；候选模型或候选压缩不能产生新 epoch。
# 类用途: 保存本次准备的线程断点，不持有 Store、凭据或正文，允许容量投影安全复制。
@dataclass(frozen=True)
class ThreadPersonaScope:
    thread_id: str
    compact_generation: int
    model_profile_id: str
    protocol: tuple[str, str]


# LLM: 安全渲染段与截断集合来自同一次Repository snapshot；只有结构字段决定删除资格，不解析正文。
# 类用途: 携带当前人格展示与不完整target，供线程冻结和变化计算同源消费。
@dataclass(frozen=True)
class RenderedPersonaSections:
    sections: dict[str, str]
    truncated_targets: frozenset[str]


# LLM: 主/子/后台渲染安全点共用，只加载线程元数据，不读消息；缺线程保持一次性准备语义。
# 函数用途: 从运行参数绑定真实线程事实，候选投影复用该值，不读取候选配置来刷新快照。
def thread_persona_scope(agent: object, params: object) -> ThreadPersonaScope | None:
    if not getattr(getattr(agent, "config", None), "thread_prompt_prefix_freeze_enabled", True):
        return None
    attrs = getattr(params, "task_attributes", None)
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        return None
    thread = store.threads.load(thread_id)
    if thread is None:
        return None
    protocol = getattr(params, "tool_protocol_snapshot", None)
    return ThreadPersonaScope(thread_id, thread.compact_generation, thread.model_profile_id,
                              (str(getattr(agent.config, "model_backend", "") or ""), str(getattr(protocol, "source_protocol", ""))))


# LLM: 路径只由已校验线程 ID 派生，拒绝符号链接；目录/文件私有创建，不向线程 JSON 塞正文。
# 函数用途: 在 owner 的 persona/thread_prefix 下确定唯一快照地址，防止跨 owner 或跨目录读写。
def _snapshot_path(home: Path, thread_id: str) -> Path:
    name = validate_path_segment(thread_id, kind="thread_id")
    directory = home / "persona" / "thread_prefix"
    if any(path.is_symlink() for path in (home, home / "persona", directory)):
        raise ValueError("PERSONA_PREFIX_SYMLINK")
    ensure_private_dir(home / "persona")
    ensure_private_dir(directory)
    path = directory / f"{name}.json"
    if path.is_symlink():
        raise ValueError("PERSONA_PREFIX_SYMLINK")
    return path


# LLM: 原锁内首建/内容修复/换代只存安全渲染段；截断集合仅控制当轮删除资格，不借此刷新快照。
# 函数用途: 原子维护唯一固定人格，按同次截断事实计算增量；安全路径/写入错误仍暴露。
def freeze_thread_persona(home: Path, scope: ThreadPersonaScope, current: RenderedPersonaSections) -> tuple[list[str], str]:
    path = _snapshot_path(home, scope.thread_id)
    epoch = {"compact_generation": scope.compact_generation, "model_profile_id": scope.model_profile_id,
             "protocol": list(scope.protocol)}
    with locked_json_path(path):
        saved = _read_snapshot(path)
        if saved is None or saved["epoch"] != epoch:
            saved = {"schema_version": 1, "epoch": epoch, "sections": current.sections}
            write_private_text_file_atomic_unlocked(path, json.dumps(saved, ensure_ascii=False))
        sections = saved["sections"]
    return [value for value in sections.values() if value], persona_updates(sections, current)


# LLM: 内容损坏等价于缺失，调用方须在原锁内重建；链接/非普通文件和IO安全错误仍抛出，不含正文日志。
# 函数用途: 有界读回快照，内容不合法记录原因码后返回None，不让缓存优化卡死回合。
def _read_snapshot(path: Path) -> dict | None:
    if path.is_symlink():
        raise ValueError("PERSONA_PREFIX_SYMLINK")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("PERSONA_PREFIX_INVALID")
    if path.stat().st_size > 1024 * 1024:
        return _invalid_snapshot(path)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid_snapshot(path)
    if not isinstance(saved, dict) or saved.get("schema_version") != 1 or not isinstance(saved.get("epoch"), dict):
        return _invalid_snapshot(path)
    sections = saved.get("sections")
    if not isinstance(sections, dict) or list(sections) != ["agents", "soul", "user", "diagnostics"]:
        return _invalid_snapshot(path)
    if not all(isinstance(value, str) for value in sections.values()):
        return _invalid_snapshot(path)
    return saved


# LLM: 内容错误只输出线程ID/固定原因码，无正文、路径和异常堆栈；仅_read_snapshot的内容验证分支调用。
# 函数用途: 报告快照内容失效并返回缺失标记，让原锁内流程重建。
def _invalid_snapshot(path: Path) -> None:
    logging.getLogger(__name__).warning("thread_id=%s reason_code=PERSONA_PREFIX_INVALID", path.stem)
    return None


# LLM: 只比较安全行；结构截断target不完整，不能证明删除，只算当前有/快照无；超预算留相同口径数量。
# 函数用途: 按target展示新增及可证明的删除，截断文件只报新增，避免尾窗移动被误认成约定撤销。
def persona_updates(previous: dict[str, str], current: RenderedPersonaSections) -> str:
    changes = {target: _line_changes(previous[target], current.sections[target], target in current.truncated_targets)
               for target in previous}
    changes = {target: rows for target, rows in changes.items() if rows}
    if not changes:
        return ""
    heading = "# Persona Updates\n以下为冻结人格之后的行变化；本轮请结合这些变化，固定段在下次压缩或换模型后刷新。"
    sections = [f"## {target}\n" + "\n".join(rows) for target, rows in changes.items()]
    detail = heading + "\n" + "\n".join(sections)
    if len(detail) <= PERSONA_UPDATES_MAX_CHARS:
        return detail
    counts = [_change_counts(target, rows, target in current.truncated_targets) for target, rows in changes.items()]
    return heading + "\n" + "\n".join(counts) + "\n变化超过尾部预算，下次压缩后整体生效。"


# LLM: 截断材料只做成员差，避免重复/移动或尾窗移出造成伪增删；完整材料沿原difflib顺序，字符不改。
# 函数用途: 不完整target只给快照里不存在的当前行，完整target保留原增删。
def _line_changes(previous: str, current: str, additions_only: bool) -> list[str]:
    if previous == current:
        return []
    old, new = previous.splitlines(), current.splitlines()
    if additions_only:
        previous_lines = set(old)
        return ["+ " + line for line in new if line not in previous_lines]
    result = []
    for kind, i, j, a, b in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if kind != "equal":
            result.extend("- " + line for line in old[i:j])
            result.extend("+ " + line for line in new[a:b])
    return result


# LLM: 计数与明细用同一增量和截断资格，截断时不输出删除数，不推测被挤出行的语义。
# 函数用途: 给预算溢出的target生成简短条数，完整target仍显示增删数量。
def _change_counts(target: str, rows: list[str], additions_only: bool) -> str:
    added = sum(row.startswith("+ ") for row in rows)
    if additions_only:
        return f"- {target}: 新增 {added} 行"
    removed = sum(row.startswith("- ") for row in rows)
    return f"- {target}: 新增 {added} 行，删除 {removed} 行"
