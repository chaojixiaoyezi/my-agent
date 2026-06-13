
from __future__ import annotations

"""Skill 扫描和读取模块。

这个模块只负责一件事：把磁盘上的 skill 变成轻量 Skill Card。
真正给子代理授权、和 tool 统一路由，是 `capabilities.py` 的职责。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.gates.skill_guard import evaluate_skill_guard_gate


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


class SkillRegistry:
    """Skill 注册表。

    它按目录扫描 `SKILL.md`，只解析索引信息。后续真正需要某个 skill 时，
    再用 `load_body()` 读取正文。"""

    def __init__(
        self,
        skill_dirs: list[str | Path] | None = None,
        *,
        guard_source: str = "manual",
        enforce_guard: bool = True,
        guard_force: bool = False,
    ):
        self.skill_dirs = [Path(item).expanduser() for item in (skill_dirs or [])]
        self.guard_source = guard_source
        self.enforce_guard = enforce_guard
        self.guard_force = guard_force
        self._cards: dict[str, SkillCard] = {}
        self._gate_decisions: dict[str, dict[str, Any]] = {}

    def scan(self) -> list[SkillCard]:
        """扫描所有 skill 目录，并按后出现覆盖先出现的规则合并同名 skill。"""

        cards: dict[str, SkillCard] = {}
        for skill_dir in self.skill_dirs:
            self._scan_skill_dir(skill_dir, cards)
        self._cards = cards
        return self.cards()

    # LLM: skill 树扫描(千级地基):递归发现任意深度的 <...>/<skill名>/SKILL.md,
    #   category=skill 目录相对扫描根的父链(长期助手 "目录即分类"形态,零 frontmatter
    #   负担);平铺旧布局(根下直接 <名>/SKILL.md)自动归 "general",完全兼容。
    # 函数用途: 把一个 skill 根目录下的所有技能(含子类目)扫成索引卡。
    def _scan_skill_dir(self, skill_dir: Path, cards: dict[str, SkillCard]) -> None:
        if not skill_dir.exists():
            return
        for skill_file in sorted(skill_dir.glob("**/SKILL.md")):
            self._register_skill_file(skill_dir, skill_file, cards)

    def _register_skill_file(self, skill_dir: Path, skill_file: Path, cards: dict[str, SkillCard]) -> None:
        decision = self._evaluate_skill_gate(skill_file)
        self._gate_decisions[skill_file.parent.name] = decision.to_dict()
        if self.enforce_guard and not decision.allowed:
            return
        card = parse_skill_file(skill_file, source=str(skill_dir))
        if card.category == "general":
            derived = _derived_category(skill_dir, skill_file)
            if derived:
                card.category = derived
        cards[card.name] = card
        self._gate_decisions[card.name] = decision.to_dict()

    def cards(self) -> list[SkillCard]:
        """返回当前已扫描到的 skill card。"""

        return list(self._cards.values())

    def get(self, name: str) -> SkillCard | None:
        """按名称取一个 skill card。"""

        return self._cards.get(name)

    def gate_decisions(self) -> dict[str, dict[str, Any]]:
        return {name: dict(decision) for name, decision in self._gate_decisions.items()}

    def load_body(self, name: str, *, max_chars: int = 0) -> str:
        """读取某个 skill 的正文。

        `max_chars=0` 表示不限制长度。这里先用字符数估算，后续接 tokenizer
        时可以替换成真正的 token 截断。"""

        card = self.get(name)
        if card is None:
            raise KeyError(f"未知 skill: {name}")
        decision = self._evaluate_skill_gate(card.path)
        self._gate_decisions[card.name] = decision.to_dict()
        if self.enforce_guard and not decision.allowed:
            raise PermissionError(_skill_guard_error(card.name, decision.to_dict()))
        body = card.path.read_text(encoding="utf-8")
        if max_chars and len(body) > max_chars:
            return body[:max_chars] + "\n... 已截断"
        return body

    def _evaluate_skill_gate(self, skill_file: Path) -> Any:
        from ..contracts.gates.skill_guard import SkillGuardRequest

        return evaluate_skill_guard_gate(
            skill_file.parent,
            SkillGuardRequest(
                source=self.guard_source,
                skill_name=skill_file.parent.name,
                force=self.guard_force,
            ),
        )


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
        category=str(meta.get("category", "")).strip() or "general",
        platforms=_as_list(meta.get("platforms")),
        scope=str(meta.get("scope", "workspace")).strip() or "workspace",
        tags=_as_list(meta.get("tags")),
        capabilities=_as_list(meta.get("capabilities")),
        tools_required=_as_list(meta.get("tools_required")),
        risk_level=str(meta.get("risk_level", "low")).strip() or "low",
        source=source,
    )


# 函数用途: 从相对路径推导类目链("research/code" 形态);平铺(无中间层)返回空。
def _derived_category(skill_dir: Path, skill_file: Path) -> str:
    try:
        parts = skill_file.parent.relative_to(skill_dir).parts[:-1]
    except ValueError:
        return ""
    return "/".join(parts)


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


def _skill_guard_error(name: str, decision: dict[str, Any]) -> str:
    codes = [
        str(item.get("code"))
        for item in decision.get("findings", [])
        if isinstance(item, dict) and item.get("code")
    ]
    return f"skill_guard_denied skill={name} codes={','.join(codes)}"
