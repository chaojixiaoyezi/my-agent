# LLM: 线程快照只有唯一文件；内容损坏在原锁内修复且只记原因码，安全路径仍拒绝；截断字段不从文案推断。
# 模块用途: 冻结人格前缀并展示期间行变化；坏缓存不阻断回合，截断窗口移出不当作撤销。
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
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
#   persona_epoch 记录渲染时读到的人格失效代次（清理迁移会推进它），冻结写入在锁内核对。
# 类用途: 携带当前人格展示、不完整target与渲染时的失效代次，供线程冻结和变化计算同源消费。
@dataclass(frozen=True)
class RenderedPersonaSections:
    sections: dict[str, str]
    truncated_targets: frozenset[str]
    # None 表示调用方未提供代次（旧路径/测试构造），冻结时不核对；提供时与持久代次不一致即拒绝写。
    persona_epoch: int | None = None
    agents_source_metadata: dict | None = None


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
#   来源在首帧/换代写回前也核当前权限，防止没有旧快照时把已撤权的在途render首次激活。
#   同epoch差分消费独立复核当前来源的完整metadata，与旧active共享一个网络期限；拒绝不撤销合法旧前缀。
#   渲染之后发生过受控失效（清理迁移推进了持久代次）时，禁止把旧渲染写回快照：返回空固定段，
#   让调用方下一轮按当前人格重新渲染，旧约定不再从固定前缀注入（2905）。
# 函数用途: 原子维护唯一固定人格，按同次截断事实计算增量；安全路径/写入错误仍暴露。
def freeze_thread_persona(home: Path, scope: ThreadPersonaScope, current: RenderedPersonaSections,
                          *, source_repository=None) -> tuple[list[str], str]:
    from ..capability.agents_source_network import source_request_budget
    path = _snapshot_path(home, scope.thread_id)
    epoch = {"compact_generation": scope.compact_generation, "model_profile_id": scope.model_profile_id,
             "protocol": list(scope.protocol)}
    # LLM: 外层线程前缀锁把「核对代次→写快照」整体圈进临界区：expire 的「推进代次→失效」
    #   也在同一把锁内，任何交错都被互斥挡住（mc4-3102）。
    with source_request_budget(), locked_json_path(_thread_prefix_lock_path(home)):
        if current.persona_epoch is not None and current.persona_epoch != _read_persona_epoch(home):
            return [], ""
        saved = _read_snapshot(path) if source_repository is not None else None
        if saved is not None and _revoke_source_locked(home, path, saved, source_repository):
            return [value for value in saved["sections"].values() if value], ""
        candidate = _snapshot_record(epoch, current)
        revoke_candidate = (source_repository is not None and current.agents_source_metadata is not None
                            and (saved is None or saved["epoch"] != epoch))
        if revoke_candidate and _revoke_source_locked(home, path, candidate, source_repository):
            return [value for value in candidate["sections"].values() if value], ""
        sections = _read_or_freeze_snapshot(path, epoch, current)
        saved = _read_snapshot(path) if source_repository is not None else None
        if saved is not None and (_source_is_pending(saved, current)
                                  or _source_diff_denied(current, source_repository)):
            current = replace(current, sections=current.sections | {"agents": sections["agents"], "diagnostics": sections["diagnostics"]})
    return [value for value in sections.values() if value], persona_updates(sections, current)


# LLM: 快照「读或首次写」收成单层小函数（mc4-3102 拆平）：调用方已持线程前缀锁，
#   这里再拿快照文件锁并用原子替换写入；返回本次生效的 sections。
# 函数用途: 读快照；缺失或代次不一致时按当前渲染写一份并返回 sections。
def _read_or_freeze_snapshot(
    path: Path, epoch: dict[str, object], current: RenderedPersonaSections
) -> dict:
    with locked_json_path(path):
        saved = _read_snapshot(path)
        if saved is None or saved["epoch"] != epoch:
            saved = _snapshot_record(epoch, current)
            write_private_text_file_atomic_unlocked(path, json.dumps(saved, ensure_ascii=False))
        return saved["sections"]


# LLM: 无I/O；来源元数据属于同次渲染，首帧及正常换代共用同一记录形状。
# 函数用途: 构造待冻结记录，元数据缺失时保持旧版本格式。
def _snapshot_record(epoch: dict, current: RenderedPersonaSections) -> dict:
    saved = {"schema_version": 1, "epoch": epoch, "sections": current.sections}
    if current.agents_source_metadata is not None:
        saved["agents_source"] = current.agents_source_metadata
    return saved


# LLM: 已持原thread_prefix锁；撤权只推进一次持久代次并清空agents，不能把在途旧render写回；保留其它人格。
# 函数用途: 让失去授权或原路径身份的active来源立即失效。
def _revoke_source_locked(home, path, saved, repository) -> bool:
    if saved.get("agents_source_revoked"):
        return False
    try:
        repository.require_active_permission(saved.get("agents_source"))
        return False
    except (ValueError, OSError):
        _bump_persona_epoch(home)
        saved["sections"] = saved["sections"] | {"agents": "", "diagnostics": ""}
        saved["agents_source_revoked"] = True
        with locked_json_path(path):
            write_private_text_file_atomic_unlocked(path, json.dumps(saved, ensure_ascii=False))
        return True


# LLM: 仅比较来源CAS而非抓取时间/状态；旧无元数据快照视为原owner默认，新source不得提前进差分。
# 函数用途: 判断当前渲染是不是还没有到断点激活的新来源。
def _source_is_pending(saved, current) -> bool:
    if saved.get("agents_source_revoked"):
        return True
    before, after = saved.get("agents_source"), current.agents_source_metadata
    if before is None:
        return after is not None and after["snapshot"]["kind"] != "owner_default"
    if after is None:
        return True
    fields = ("owner", "kind", "locator", "config_version", "revision")
    return any(before["snapshot"][key] != after["snapshot"][key] for key in fields)


# LLM: 差分是一次新的正文消费；CAS或摘要相同也不能复用旧redirect权限，拒绝只屏蔽AGENTS差分。
# 函数用途: 复核本次渲染来源，避免撤权的新正文进入动态增量。
def _source_diff_denied(current, repository) -> bool:
    try:
        repository.require_active_permission(current.agents_source_metadata)
        return False
    except (ValueError, OSError):
        return True


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


# LLM: 清理迁移后的受控失效：先推进持久失效代次（锁内递增），再删除命中快照；在途渲染的
#   旧代次冻结会被 freeze_thread_persona 拒绝，不会把已清理的旧约定写回固定前缀（2905）。
#   锁外预筛减少锁竞争，锁内复核保证与冻结写入不竞态；失效=删除快照文件，下轮重建时用当前人格。
# 函数用途: 让包含被清理条目的线程快照受控失效，返回实际删除的快照数。
def expire_thread_personas_containing(home: Path, contents: list[str]) -> int:
    needles = _content_needles(contents)
    if not needles:
        return 0
    directory = home / "persona" / "thread_prefix"
    if any(part.is_symlink() for part in (home, home / "persona", directory)):
        raise ValueError("PERSONA_PREFIX_SYMLINK")
    # LLM: 「推进代次→失效快照」与 freeze 的「核对代次→写快照」共用同一把线程前缀锁：
    #   在途渲染的旧代次冻结要么发生在失效前（会被失效清掉）、要么发生在推进后（核对拒绝）。
    with locked_json_path(_thread_prefix_lock_path(home)):
        _bump_persona_epoch(home)
        return _expire_snapshots_locked(directory, needles)


# LLM: 目录遍历与失效收成单层小函数（mc4-3102 拆平）：调用方已持线程前缀锁，遍历中跳过
#   链接与非普通文件（IO 安全错误仍由底层抛出）；返回失效个数。
# 函数用途: 让目录里命中内容的快照失效，返回失效个数。
def _expire_snapshots_locked(directory: Path, needles: list[str]) -> int:
    if not directory.is_dir():
        return 0
    expired = 0
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        if _expire_snapshot_locked(path, needles):
            expired += 1
    return expired


# LLM: 线程前缀锁是 freeze 写入与 expire 失效的唯一互斥点（mc4-3102）：「核对代次→写快照」
#   与「推进代次→失效快照」在同一把锁内完成，两者之间不再有交错窗口；锁文件只作 flock
#   锚点、不写内容，放 persona 目录下与其它人格状态同域。
# 函数用途: 返回线程前缀锁文件路径。
def _thread_prefix_lock_path(home: Path) -> Path:
    return home / "persona" / "thread_prefix.lock"


# LLM: 失效代次放 persona/ 下的持久小文件；只在锁内递增，读取是单次原子读，不携带正文。
# 函数用途: 返回人格失效代次文件的路径。
def _persona_epoch_path(home: Path) -> Path:
    return home / "persona" / "thread_prefix_epoch.json"


# 函数用途: 读取当前人格失效代次；缺失、链接或坏内容按 0 处理（首次渲染不核对）。
def _read_persona_epoch(home: Path) -> int:
    path = _persona_epoch_path(home)
    if path.is_symlink():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(0, int(payload.get("epoch") or 0))
    except (TypeError, ValueError):
        return 0


# 函数用途: 在文件锁内把人格失效代次 +1，返回新代次。
def _bump_persona_epoch(home: Path) -> int:
    if any(part.is_symlink() for part in (home, home / "persona")):
        raise ValueError("PERSONA_PREFIX_SYMLINK")
    ensure_private_dir(home / "persona")
    path = _persona_epoch_path(home)
    if path.is_symlink():
        raise ValueError("PERSONA_PREFIX_SYMLINK")
    with locked_json_path(path):
        next_value = _read_persona_epoch(home) + 1
        write_private_text_file_atomic_unlocked(path, json.dumps({"epoch": next_value}))
    return next_value


# LLM: 复核与删除在同一把快照锁内完成，避免预筛后快照被冻结刷新导致误删新快照。
# 函数用途: 在锁内复核命中后删除一个快照文件，返回是否删除。
def _expire_snapshot_locked(path: Path, needles: list[str]) -> bool:
    with locked_json_path(path):
        if not _snapshot_contains(path, needles):
            return False
        path.unlink()
        return True


# LLM: 只做行级子串判断（被清理行是快照行的子串），不解析正文语义；缺失/损坏/超限视为不含，
#   坏缓存不阻断迁移，也不把链接目标读进来。
# 函数用途: 判断快照的 agents 段是否仍包含任一被清理条目。
def _snapshot_contains(path: Path, needles: list[str]) -> bool:
    try:
        saved = _read_snapshot(path)
    except ValueError:
        return False
    if saved is None:
        return False
    lines = str(saved["sections"].get("agents", "")).splitlines()
    return any(needle in line for line in lines for needle in needles)


# 函数用途: 把被清理条目正文拆成非空行，作为快照命中的最小比较单位。
def _content_needles(contents: list[str]) -> list[str]:
    needles: list[str] = []
    for text in contents:
        needles.extend(part.strip() for part in str(text or "").splitlines() if part.strip())
    return needles
