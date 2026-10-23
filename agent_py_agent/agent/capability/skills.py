# LLM: Card与资产共用有界安全frontmatter和根声明，不执行或授权技能。
# 模块用途: 提供技能卡片、唯一头部codec及确定性发现根。
"""Skill 扫描和读取模块。

这个模块只负责一件事：把磁盘上的 skill 变成轻量 Skill Card。
真正的扫描、策略、逐轮快照由 `skill_service.py` 负责；给子代理授权和 tool 统一路由
分别由编排层与 `router.py` 负责。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SKILL_BYTES = 4 * 1024 * 1024
MAX_FRONTMATTER_BYTES = 256 * 1024
MAX_FRONTMATTER_DEPTH = 32
_LIST_FIELDS = ("tags", "platforms", "capabilities", "tools_required")


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


# LLM: 卡片和资产使用同一codec，require_frontmatter仍仅校验卡片必填字段。
# 函数用途: 从明确路径有界读取Skill Card，不执行正文。
def parse_skill_file(
    path: str | Path,
    *,
    source: str = "workspace",
    require_frontmatter: bool = False,
) -> SkillCard:
    """从 `SKILL.md` 解析轻量 Skill Card。"""

    skill_path = Path(path)
    text = read_skill_text(skill_path)
    meta, body = split_skill_frontmatter(text)
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


# LLM: 发现和卡片读取共用字节预算，不创建或执行路径。
# 函数用途: 有界读取一个明确技能文件。
def read_skill_text(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_SKILL_BYTES + 1)
    if len(raw) > MAX_SKILL_BYTES:
        raise ValueError("Skill正文超过安全字节预算")
    return raw.decode("utf-8")


# LLM: 有起始分隔符却无结束符必须失败；头部与全文分别限长，仅产生普通数据。
# 函数用途: 统一拆出技能元数据和正文。
def split_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        raise ValueError("Skill文本无效或超过安全字节预算")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Skill YAML头缺少结束分隔符")
    header = "\n".join(lines[1:end])
    if len(header.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise ValueError("Skill YAML头超过安全字节预算")
    return _parse_meta(header), "\n".join(lines[end + 1:])


# LLM: 旧名称只委托唯一codec，不维护第二份实现。
# 函数用途: 兼容原拆分入口。
def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    return split_skill_frontmatter(text)


# LLM: 顶层必须为完整映射，重复键与未消费缩进拒绝；列表字段由schema归一。
# 函数用途: 从头部生成普通JSON兼容元数据。
def _parse_meta(text: str) -> dict[str, Any]:
    tokens = _yaml_tokens(text)
    if not tokens:
        return {}
    data, index = _yaml_block(tokens, 0, 0)
    if not isinstance(data, dict) or index != len(tokens):
        raise ValueError("Skill YAML头必须是完整对象")
    for key in _LIST_FIELDS:
        if key in data:
            data[key] = _as_list(data[key])
    return data


# LLM: 仅引号外且前有空白的井号开始注释，处理双引号转义与单引号双写。
# 函数用途: 保留标量中的C#与引号文本。
def _yaml_uncomment(line):
    quote, index = "", 0
    while index < len(line):
        char = line[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and line[index:index + 2] == "''":
                    index += 2
                    continue
                quote = ""
        elif char in "\"'" and (index == 0 or line[index - 1] in " \t:[{,"):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
        index += 1
    if quote:
        raise ValueError("Skill YAML引号未闭合")
    return line.rstrip()


# LLM: 块内内容不去注释，编码成普通字符串；结构token禁止tab缩进。
# 函数用途: 生成保留缩进的数据token。
def _yaml_tokens(text):
    lines, tokens, index = text.splitlines(), [], 0
    while index < len(lines):
        raw = lines[index]
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ValueError("Skill YAML不能使用tab缩进")
        line = _yaml_uncomment(raw)
        index += 1
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        value = line.strip()
        block = re.search(r"(?:^-\s+|:\s*)([>|][+-]?)$", value)
        if block:
            parent = indent + (2 if value.startswith("- ") and ":" in value else 0)
            content, index = _yaml_block_text(lines, index, parent, block.group(1))
            value = value[:block.start(1)] + json.dumps(content, ensure_ascii=False)
        tokens.append((indent, value))
    return tokens


# LLM: 只消费更深缩进的原始行，literal保留换行，folded按段折叠，chomping仅影响末尾。
# 函数用途: 读取竖线与折叠块文本。
def _yaml_block_text(lines, index, indent, style):
    block = []
    while index < len(lines):
        raw = lines[index]
        if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= indent:
            break
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ValueError("Skill YAML不能使用tab缩进")
        block.append(raw)
        index += 1
    if not block:
        return "", index
    margin = min((len(line) - len(line.lstrip(" ")) for line in block if line.strip()), default=0)
    values = [line[margin:] if line.strip() else "" for line in block]
    content = "\n".join(values) if style.startswith("|") else _yaml_folded_text(values)
    if style.endswith("-"):
        content = content.rstrip("\n")
    elif style.endswith("+"):
        content += "\n"
    else:
        content = content.rstrip("\n") + "\n"
    return content, index


# LLM: 仅处理已去除公共缩进的块文本；保留空段和额外缩进，不改变chomping策略。
# 函数用途: 折叠相邻普通行，避免块读取函数堆叠控制分支。
def _yaml_folded_text(values):
    pieces = []
    for position, line in enumerate(values):
        pieces.append(line)
        if position + 1 >= len(values):
            continue
        following = values[position + 1]
        if line and following and not line[:1].isspace() and not following[:1].isspace():
            pieces.append(" ")
        elif line or not following:
            pieces.append("\n")
    return "".join(pieces)


# LLM: 容器递归与深度受限，键先规范再检查重复，不构造YAML对象。
# 函数用途: 解析一个缩进层的映射或列表。
def _yaml_block(tokens, index, indent, depth=0):
    if depth > MAX_FRONTMATTER_DEPTH:
        raise ValueError("Skill YAML嵌套过深")
    sequence = tokens[index][1] == "-" or tokens[index][1].startswith("- ")
    result = [] if sequence else {}
    while index < len(tokens) and tokens[index][0] == indent:
        raw = tokens[index][1]
        if sequence:
            if raw != "-" and not raw.startswith("- "):
                break
            value = raw[1:].strip()
            if re.match(r"[^:]+:(?:\s|$)", value) and not value.startswith(('"', "'", "[", "{")):
                tokens[index] = (indent + 2, value)
                item, index = _yaml_block(tokens, index, indent + 2, depth + 1)
            else:
                item, index = _yaml_item(tokens, index, indent, value, depth)
            result.append(item)
            continue
        key, separator, value = raw.partition(":")
        key = _yaml_scalar(key.strip(), depth + 1)
        if not separator or not isinstance(key, str) or not key or key == "<<" or key in result:
            raise ValueError("Skill YAML键无效或重复")
        result[key], index = _yaml_item(tokens, index, indent, value.strip(), depth)
    if index < len(tokens) and tokens[index][0] > indent:
        raise ValueError("Skill YAML缩进无效")
    return result, index


# LLM: 空值只能接更深容器或同层列表，有值条目只走安全标量读取。
# 函数用途: 解析当前条目并返回未消费位置。
def _yaml_item(tokens, index, indent, value, depth):
    following = index + 1
    if value:
        return _yaml_scalar(value, depth + 1), following
    if following < len(tokens):
        child_indent, child = tokens[following]
        if child_indent > indent or child_indent == indent and (child == "-" or child.startswith("- ")):
            return _yaml_block(tokens, following, child_indent, depth + 1)
    return None, following


# LLM: 引号始终是文本，流容器有界递归；明确拒绝tag、anchor、alias及合并键。
# 函数用途: 解析安全字符串、有限数字、布尔、空值和流容器。
def _yaml_scalar(value, depth=0):
    if depth > MAX_FRONTMATTER_DEPTH:
        raise ValueError("Skill YAML嵌套过深")
    if value.startswith("'"):
        if not re.fullmatch(r"'(?:[^']|'')*'", value, flags=re.DOTALL):
            raise ValueError("Skill YAML单引号值无效")
        return value[1:-1].replace("''", "'")
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Skill YAML双引号值无效") from exc
    if value.startswith(("!", "&", "*")):
        raise ValueError("Skill YAML不允许tag/anchor/alias")
    if value.startswith(("[", "{")):
        if not value.endswith("]" if value[0] == "[" else "}"):
            raise ValueError("Skill YAML流容器未闭合")
        parts = _yaml_flow_parts(value[1:-1], depth)
        if value[0] == "[":
            return [_yaml_scalar(part.strip(), depth + 1) for part in parts]
        result = {}
        for part in parts:
            key, separator, item = part.partition(":")
            key = _yaml_scalar(key.strip(), depth + 1)
            if not separator or not isinstance(key, str) or not key or key == "<<" or key in result:
                raise ValueError("Skill YAML流对象键无效或重复")
            result[key] = _yaml_scalar(item.strip(), depth + 1)
        return result
    if value.lower() in ("null", "~"):
        return None
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
        number = json.loads(value)
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError("Skill YAML数字必须有限")
        return number
    return value


# LLM: 只在引号与嵌套容器外按逗号拆分，扫描时已限制深度。
# 函数用途: 拆分流列表或映射的直接成员。
def _yaml_flow_parts(value, depth):
    if not value.strip():
        return []
    parts, stack, quote, index, start = [], [], "", 0, 0
    while index < len(value):
        char = value[index]
        if quote:
            if (quote == '"' and char == "\\") or (quote == "'" and value[index:index + 2] == "''"):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'" and (index == 0 or value[index - 1] in " \t:[{,"):
            quote = char
            index += 1
            continue
        if char in "[{":
            stack.append(char)
            if depth + len(stack) > MAX_FRONTMATTER_DEPTH:
                raise ValueError("Skill YAML嵌套过深")
            index += 1
            continue
        if char in "]}":
            if not stack or stack.pop() != ({"}": "{", "]": "["}[char]):
                raise ValueError("Skill YAML流容器不匹配")
            index += 1
            continue
        if char == "," and not stack:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    if quote or stack:
        raise ValueError("Skill YAML流容器未闭合")
    parts.append(value[start:])
    if any(not part.strip() for part in parts):
        raise ValueError("Skill YAML流容器包含空项")
    return parts


# LLM: 标量按逗号分隔；列表中已引用成员不二次拆分，不接受非文本或嵌套列表。
# 函数用途: 统一schema字符串列表。
def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise ValueError("Skill列表字段必须是字符串或字符串列表")
    return [item.strip() for item in values if item.strip()]


# LLM: 只声明宿主给定根，不扫描或创建目录；learned保留原owner来源。
# 函数用途: 为目录服务和适配器提供唯一确定性根顺序。
def skill_catalog_roots(owner_home, workspace_root=None, extra_roots=()):
    home = Path(owner_home).absolute()
    workspace = Path(workspace_root).absolute() if workspace_root is not None else home / "workspace"
    roots = [(workspace / ".agents/skills", "workspace"), (workspace / ".agent/skills", "workspace"),
             (home / "workspace/.agents/skills", "workspace"), (home / "workspace/.agent/skills", "workspace"),
             (home / "skills", "owner"), (home / "skills/learned", "owner"), (home / "learned", "owner")]
    if isinstance(extra_roots, (str, bytes, Path)):
        raise ValueError("extra_roots必须是根目录序列")
    for value in extra_roots:
        path, source = value if isinstance(value, (tuple, list)) and len(value) == 2 else (value, "workspace")
        if not isinstance(source, str) or not re.fullmatch(
            r"(?:workspace|owner|shared|builtin|learned|plugin:[A-Za-z0-9][A-Za-z0-9_.-]{0,127})", source
        ):
            raise ValueError("Skill根来源无效")
        roots.append((Path(path).absolute(), source))
    return tuple(dict.fromkeys(roots))


def _first_paragraph(body: str) -> str:
    """从 Markdown 正文里取第一段非标题文本作为默认描述。"""

    for block in body.split("\n\n"):
        text = block.strip()
        if text and not text.startswith("#"):
            return " ".join(text.split())
    return ""
