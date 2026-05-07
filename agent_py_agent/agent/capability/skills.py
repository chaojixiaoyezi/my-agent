# LLM: Capability module; keep skill/tool routing contracts stable for planner and dispatch callers.
# 模块用途: 描述和路由 agent 能力、技能、工具和执行条件。

from __future__ import annotations

"""Skill 扫描和读取模块。

这个模块只负责一件事：把磁盘上的 skill 变成轻量 Skill Card。
真正给子代理授权、和 tool 统一路由，是 `capabilities.py` 的职责。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# LLM: SkillCard is a 能力路由 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 一个 skill 的轻量索引卡。 Card 只放路由需要的短信息，不直接装进完整 `SKILL.md`。 这样即使未来有一万个 skill，也可以先检索 card，再按需加载正文。
@dataclass
class SkillCard:
    """一个 skill 的轻量索引卡。

    Card 只放路由需要的短信息，不直接装进完整 `SKILL.md`。
    这样即使未来有一万个 skill，也可以先检索 card，再按需加载正文。"""

    name: str
    description: str
    path: Path
    when_to_use: str = ""
    scope: str = "workspace"
    tags: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    risk_level: str = "low"
    source: str = "workspace"

    # LLM: SkillCard.render_compact belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 渲染给模型看的短卡片。。
    def render_compact(self) -> str:
        """渲染给模型看的短卡片。"""

        parts = [
            f"- skill:{self.name} [{self.scope}/{self.risk_level}]",
            f"  说明：{self.description}",
        ]
        if self.when_to_use:
            parts.append(f"  何时使用：{self.when_to_use}")
        if self.capabilities:
            parts.append(f"  能力：{', '.join(self.capabilities)}")
        if self.tools_required:
            parts.append(f"  需要工具：{', '.join(self.tools_required)}")
        return "\n".join(parts)


# LLM: SkillRegistry is a 能力路由 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: Skill 注册表。 它按目录扫描 `SKILL.md`，只解析索引信息。后续真正需要某个 skill 时， 再用 `load_body()` 读取正文。
class SkillRegistry:
    """Skill 注册表。

    它按目录扫描 `SKILL.md`，只解析索引信息。后续真正需要某个 skill 时，
    再用 `load_body()` 读取正文。"""

    # LLM: SkillRegistry.__init__ belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 SkillRegistry 的方法，通常依赖实例字段。
    def __init__(self, skill_dirs: list[str | Path] | None = None):
        self.skill_dirs = [Path(item).expanduser() for item in (skill_dirs or [])]
        self._cards: dict[str, SkillCard] = {}

    # LLM: SkillRegistry.scan belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 扫描所有 skill 目录，并按后出现覆盖先出现的规则合并同名 skill。。
    def scan(self) -> list[SkillCard]:
        """扫描所有 skill 目录，并按后出现覆盖先出现的规则合并同名 skill。"""

        cards: dict[str, SkillCard] = {}
        for skill_dir in self.skill_dirs:
            if not skill_dir.exists():
                continue
            for skill_file in sorted(skill_dir.glob("*/SKILL.md")):
                card = parse_skill_file(skill_file, source=str(skill_dir))
                cards[card.name] = card
        self._cards = cards
        return self.cards()

    # LLM: SkillRegistry.cards belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 返回当前已扫描到的 skill card。。
    def cards(self) -> list[SkillCard]:
        """返回当前已扫描到的 skill card。"""

        return list(self._cards.values())

    # LLM: SkillRegistry.get belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 按名称取一个 skill card。。
    def get(self, name: str) -> SkillCard | None:
        """按名称取一个 skill card。"""

        return self._cards.get(name)

    # LLM: SkillRegistry.load_body belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 读取某个 skill 的正文。 `max_chars=0` 表示不限制长度。这里先用字符数兜底，后续接 tokenizer 时可以替换成真正的 token 截断。。
    def load_body(self, name: str, *, max_chars: int = 0) -> str:
        """读取某个 skill 的正文。

        `max_chars=0` 表示不限制长度。这里先用字符数兜底，后续接 tokenizer
        时可以替换成真正的 token 截断。"""

        card = self.get(name)
        if card is None:
            raise KeyError(f"未知 skill: {name}")
        body = card.path.read_text(encoding="utf-8")
        if max_chars and len(body) > max_chars:
            return body[:max_chars] + "\n... 已截断"
        return body


# LLM: parse_skill_file belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 从 `SKILL.md` 解析轻量 Skill Card。。
def parse_skill_file(path: str | Path, *, source: str = "workspace") -> SkillCard:
    """从 `SKILL.md` 解析轻量 Skill Card。"""

    skill_path = Path(path)
    text = skill_path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or skill_path.parent.name).strip()
    description = str(meta.get("description") or _first_paragraph(body) or name).strip()
    return SkillCard(
        name=name,
        description=description,
        path=skill_path,
        when_to_use=str(meta.get("when_to_use", "")).strip(),
        scope=str(meta.get("scope", "workspace")).strip() or "workspace",
        tags=_as_list(meta.get("tags")),
        capabilities=_as_list(meta.get("capabilities")),
        tools_required=_as_list(meta.get("tools_required")),
        risk_level=str(meta.get("risk_level", "low")).strip() or "low",
        source=source,
    )


# LLM: _split_frontmatter belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 拆出 Markdown frontmatter。 这里只支持项目需要的极简 YAML 子集，避免为了 skill 索引引入完整解析器。。
def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 Markdown frontmatter。

    这里只支持项目需要的极简 YAML 子集，避免为了 skill 索引引入完整解析器。"""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta_text = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return _parse_meta(meta_text), body
    return {}, text


# LLM: _parse_meta belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 解析 card frontmatter 里的小型 key/value/list 结构。。
def _parse_meta(text: str) -> dict[str, Any]:
    """解析 card frontmatter 里的小型 key/value/list 结构。"""

    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if _append_meta_list_item(data, current_key, line):
            continue
        current_key = _parse_meta_mapping_line(data, current_key, line)
    return data


# LLM: _append_meta_list_item belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把结果、日志或状态写回磁盘/索引，改动时要确认审计记录和失败处理。
def _append_meta_list_item(data: dict[str, Any], current_key: str | None, line: str) -> bool:
    if not (line.startswith("  - ") and current_key):
        return False
    data.setdefault(current_key, []).append(_parse_value(line[4:]))
    return True


# LLM: _parse_meta_mapping_line belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 读取文件、配置或外部文本并转换成内部对象，格式变化要保留兼容路径。
def _parse_meta_mapping_line(data: dict[str, Any], current_key: str | None, line: str) -> str | None:
    if ":" not in line or line.startswith(" "):
        return current_key
    key, value = line.split(":", 1)
    key = key.strip()
    value = value.strip()
    if value == "":
        data[key] = []
        return key
    data[key] = _parse_value(value)
    return None


# LLM: _parse_value belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 解析 frontmatter 标量或简单行内列表。。
def _parse_value(value: str) -> Any:
    """解析 frontmatter 标量或简单行内列表。"""

    value = value.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(item.strip()) for item in inner.split(",")]
    return value


# LLM: _as_list belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把 frontmatter 里的值统一转成字符串列表。。
def _as_list(value: Any) -> list[str]:
    """把 frontmatter 里的值统一转成字符串列表。"""

    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


# LLM: _first_paragraph belongs to 能力路由; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 从 Markdown 正文里取第一段非标题文本作为兜底描述。。
def _first_paragraph(body: str) -> str:
    """从 Markdown 正文里取第一段非标题文本作为兜底描述。"""

    for block in body.split("\n\n"):
        text = block.strip()
        if text and not text.startswith("#"):
            return " ".join(text.split())
    return ""
