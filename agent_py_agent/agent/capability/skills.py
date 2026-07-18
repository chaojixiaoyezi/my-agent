"""Skill 扫描和读取模块。

这个模块只负责一件事：把磁盘上的 skill 变成轻量 Skill Card。
真正的扫描、策略、逐轮快照由 `skill_service.py` 负责；给子代理授权和 tool 统一路由
分别由编排层与 `router.py` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillCard:
    """一个 skill 的轻量索引卡。

    Card 只放路由需要的短信息，不直接装进完整 `SKILL.md`。
    这样即使未来有一万个 skill，也可以先检索 card，再按需加载正文。"""

    name: str
    description: str
    path: Path
    when_to_use: str = ""
    # skill 树第一期(千级地基):category 来自目录层级推导(skills/<类目>/<名>/
    # SKILL.md),frontmatter 显式声明可覆盖;platforms 供平台过滤(空=全平台)。
    category: str = "general"
    platforms: list[str] = field(default_factory=list)
    scope: str = "workspace"
    tags: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    risk_level: str = "low"
    source: str = "workspace"

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


def parse_skill_file(
    path: str | Path,
    *,
    source: str = "workspace",
    require_frontmatter: bool = False,
) -> SkillCard:
    """从 `SKILL.md` 解析轻量 Skill Card。"""

    skill_path = Path(path)
    text = skill_path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    if require_frontmatter:
        if not _has_frontmatter(text):
            raise ValueError("missing YAML frontmatter delimited by ---")
        if not str(meta.get("description") or "").strip():
            raise ValueError("missing frontmatter field: description")
    name = str(meta.get("name") or skill_path.parent.name).strip()
    description = str(meta.get("description") or _first_paragraph(body) or name).strip()
    return SkillCard(
        name=name,
        description=description,
        path=skill_path,
        when_to_use=str(meta.get("when_to_use", "")).strip(),
        category=str(meta.get("category", "")).strip() or "general",
        platforms=_as_list(meta.get("platforms")),
        scope=str(meta.get("scope", "workspace")).strip() or "workspace",
        tags=_as_list(meta.get("tags")),
        capabilities=_as_list(meta.get("capabilities")),
        tools_required=_as_list(meta.get("tools_required")),
        risk_level=str(meta.get("risk_level", "low")).strip() or "low",
        source=source,
    )


def _has_frontmatter(text: str) -> bool:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


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


def _append_meta_list_item(data: dict[str, Any], current_key: str | None, line: str) -> bool:
    if not (line.startswith("  - ") and current_key):
        return False
    data.setdefault(current_key, []).append(_parse_value(line[4:]))
    return True


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


def _parse_value(value: str) -> Any:
    """解析 frontmatter 标量或简单行内列表。"""

    value = value.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(item.strip()) for item in inner.split(",")]
    return value


def _as_list(value: Any) -> list[str]:
    """把 frontmatter 里的值统一转成字符串列表。"""

    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _first_paragraph(body: str) -> str:
    """从 Markdown 正文里取第一段非标题文本作为默认描述。"""

    for block in body.split("\n\n"):
        text = block.strip()
        if text and not text.startswith("#"):
            return " ".join(text.split())
    return ""
