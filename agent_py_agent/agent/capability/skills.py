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

# 单个 Skill 文件允许读取的最大字节数上限。
MAX_SKILL_BYTES = 4 * 1024 * 1024
# 技能元数据头部允许的最大UTF-8字节数。
MAX_FRONTMATTER_BYTES = 256 * 1024
# 技能元数据映射和列表允许的最大嵌套层数。
MAX_FRONTMATTER_DEPTH_COUNT = 32
# 兼容现有调用方的旧导入名，数值及生产读取点统一使用带单位的新常量。
MAX_FRONTMATTER_DEPTH = MAX_FRONTMATTER_DEPTH_COUNT
_LIST_FIELDS = ("tags", "platforms", "capabilities", "tools_required")


# LLM: Card仅携带发现元数据及Owner运行闸，不把发现视为授权；disabled卡不能进入active snapshot。
# 类用途: 表示按需读取正文前的轻量技能索引。
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
    enabled: bool = True

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


# LLM: Card共用codec与canonical skill_meta.json运行闸；侧文件显式enabled优先，缺省沿frontmatter/default True。
# 函数用途: 从明确路径有界读取Skill Card，不执行正文。
def parse_skill_file(
    path: str | Path,
    *,
    source: str = "workspace",
    require_frontmatter: bool = False,
    default_category: str = "general",
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
    category = meta.get("category")
    return SkillCard(
        name=name,
        description=description,
        path=skill_path,
        when_to_use=str(meta.get("when_to_use", "")).strip(),
        category=(str(category).strip() if category is not None else "") or default_category,
        platforms=_as_list(meta.get("platforms")),
        scope=str(meta.get("scope", "workspace")).strip() or "workspace",
        tags=_as_list(meta.get("tags")),
        capabilities=_as_list(meta.get("capabilities")),
        tools_required=_as_list(meta.get("tools_required")),
        risk_level=str(meta.get("risk_level", "low")).strip() or "low",
        source=source,
        enabled=skill_enabled(skill_path, meta),
    )


# LLM: 原skill_meta.json是唯一侧元数据权威；不创建目录/锁，严格bool，缺失保留头部值；可只复核当前侧开关。
# 函数用途: 为Card、快照和运行门读取技能启停状态，避免另建.delivery_meta影子状态。
def skill_enabled(path: str | Path, meta: dict | None = None) -> bool:
    from ..common.nofollow_fs import read_bytes_beneath

    raw = read_bytes_beneath(Path(path).parent, ('skill_meta.json',), max_bytes=MAX_SKILL_BYTES)
    side = json.loads(raw) if raw is not None else {}
    if not isinstance(side, dict):
        raise ValueError('skill_meta.json必须是对象')
    enabled = side.get('enabled', (meta or {}).get('enabled', True))
    if type(enabled) is not bool:
        raise ValueError('Skill enabled必须是布尔值')
    return enabled


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


# LLM: 只有标量起点引号进入quote模式；普通标量内部引号是文本，井号注释仍要求前置空白。
# 函数用途: 保留标量中的C#与引号文本。
def _yaml_uncomment(line, *, plain_scalar=False):
    if plain_scalar:
        return re.split(r"(?:^|\s)#", line, maxsplit=1)[0].rstrip()
    starts = _yaml_quote_positions(line)
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
        elif char in "\"'" and index in starts:
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
        index += 1
    if quote:
        raise ValueError("Skill YAML引号未闭合")
    return line.rstrip()


# LLM: 真映射冒号后须空白或行末；quoted键须先找到结束引号，值中冒号不改变标量起点。
# 函数用途: 找到key冒号或列表短横线后的标量起点。
def _yaml_value_offset(line):
    offset = len(line) - len(line.lstrip())
    if line[offset:].startswith("- "):
        offset += 2
    value = line[offset:]
    if value.startswith(("'", '"')):
        end = _yaml_quote_end(value, value[0], 1)
        mapping = re.match(r":(?:[ \t]+|$)", value[end + 1:]) if end is not None else None
        return offset + end + 1 + mapping.end() if mapping else offset
    mapping = re.match(r"[^:\n]+:(?:[ \t]+|$)", value)
    return offset + mapping.end() if mapping else offset


# LLM: 一次预计算可能的标量起点，不为普通标量内的每个引号重复扫描整行前缀。
# 函数用途: 为注释扫描和流容器拆分共用线性引号起点集合。
def _yaml_quote_positions(value, *, flow=False):
    offset = 0 if flow else _yaml_value_offset(value)
    starts = set() if flow else {len(value) - len(value.lstrip()), offset}
    if flow or value[offset:].lstrip().startswith(("[", "{")):
        starts.update(offset + match.end() - 1 for match in re.finditer(r'''(?:^|[:,\[{])\s*["']''', value[offset:]))
    return starts


# LLM: 起始引号由调用方确认；处理反斜线和单引号双写，未闭合返回None以读取下一物理行。
# 函数用途: 在一行内找到实际结束引号。
def _yaml_quote_end(value, quote, start):
    index = start
    while index < len(value):
        if (quote == '"' and value[index] == "\\") or (quote == "'" and value[index:index + 2] == "''"):
            index += 2
            continue
        if value[index] == quote:
            return index
        index += 1
    return None


# LLM: 只拼接真正以引号开头的标量；保留物理空行交统一fold处理，每个续行仍校验缩进安全边界。
# 函数用途: 将多行引号值恢复为可交给标量解析器的逻辑行。
def _yaml_quoted_line(lines, index):
    raw = lines[index]
    offset = _yaml_value_offset(raw)
    value = raw[offset:]
    index += 1
    if not value.startswith(("'", '"')):
        return raw, index
    quote, values, start = value[0], [], 1
    while True:
        end = _yaml_quote_end(value, quote, start)
        values.append(value if end is not None else value.rstrip())
        if end is not None:
            return raw[:offset] + _yaml_folded_text(values, quote=quote), index
        if index >= len(lines):
            raise ValueError("Skill YAML引号未闭合")
        _yaml_validate_indent(lines[index])
        value, start = lines[index].lstrip(), 0
        index += 1


# LLM: 普通标量只续接更深缩进的非映射/非列表文本；注释不算空行，续行仍拒绝tab缩进。
# 函数用途: 折叠合法普通标量续行并返回未消费位置。
def _yaml_plain_line(lines, index, raw):
    offset = _yaml_value_offset(raw)
    scalar = raw[offset:].strip()
    if not scalar or scalar.startswith(("'", '"', "[", "{", "|", ">", "!", "&", "*")):
        return raw, index
    indent = len(raw) - len(raw.lstrip(" "))
    values = [scalar]
    while index < len(lines):
        next_line = lines[index]
        _yaml_validate_indent(next_line)
        if next_line.lstrip().startswith("#"):
            index += 1
            continue
        if next_line.strip() and len(next_line) - len(next_line.lstrip(" ")) <= indent:
            break
        value = _yaml_uncomment(next_line.strip(), plain_scalar=True)
        if re.match(r"(?:-\s|[^:]+:\s|[^:]+:$)", value):
            break
        values.append(value)
        index += 1
    return raw[:offset] + _yaml_folded_text(values), index


# LLM: 物理行边界统一拒绝tab缩进，多行折叠不得绕过原结构token的安全检查。
# 函数用途: 在消耗任何物理行前核验缩进。
def _yaml_validate_indent(raw):
    if "\t" in raw[:len(raw) - len(raw.lstrip())]:
        raise ValueError("Skill YAML不能使用tab缩进")


# LLM: 块内内容不去注释，编码成普通字符串；结构token禁止tab缩进。
# 函数用途: 生成保留缩进的数据token。
def _yaml_tokens(text):
    lines, tokens, index = text.splitlines(), [], 0
    while index < len(lines):
        raw = lines[index]
        _yaml_validate_indent(raw)
        raw, index = _yaml_quoted_line(lines, index)
        line = _yaml_uncomment(raw)
        line, index = _yaml_plain_line(lines, index, line)
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


# LLM: 块文本保持原chomping；quoted空行折为换行，双引号编码为JSON转义且显式转义换行不插空格。
# 函数用途: 共用普通行与quoted flow折叠规则，保留空段而非逐物理行加空格。
def _yaml_folded_text(values, *, quote=""):
    pieces = []
    for position, line in enumerate(values):
        last = position + 1 >= len(values)
        escaped = quote == '"' and not last and (len(line) - len(line.rstrip("\\"))) % 2 == 1
        pieces.append(line[:-1] if escaped else line)
        if last or escaped:
            continue
        following = values[position + 1]
        if line and following and not line[:1].isspace() and not following[:1].isspace():
            pieces.append(" ")
        elif line or not following:
            pieces.append("\\n" if quote == '"' else "\n")
    return "".join(pieces)


# LLM: 容器递归与深度受限，键先规范再检查重复，不构造YAML对象。
# 函数用途: 解析一个缩进层的映射或列表。
def _yaml_block(tokens, index, indent, depth=0):
    if depth > MAX_FRONTMATTER_DEPTH_COUNT:
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
    if depth > MAX_FRONTMATTER_DEPTH_COUNT:
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
    starts = _yaml_quote_positions(value, flow=True)
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
        if char in "\"'" and index in starts:
            quote = char
            index += 1
            continue
        if char in "[{":
            stack.append(char)
            if depth + len(stack) > MAX_FRONTMATTER_DEPTH_COUNT:
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


# LLM: 标量按逗号分隔，有限数字与布尔可转文本；列表成员不二次拆分，拒绝null和嵌套容器。
# 函数用途: 统一schema字符串列表。
def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (int, float, bool)):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError("Skill列表字段必须是字符串或字符串列表")
    if any(not isinstance(item, (str, int, float, bool)) for item in values):
        raise ValueError("Skill列表字段不能包含null或嵌套容器")
    return [str(item).strip() for item in values if str(item).strip()]


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
