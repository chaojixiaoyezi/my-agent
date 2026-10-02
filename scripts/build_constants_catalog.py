#!/usr/bin/env python3
# LLM: 模块级数值常数的“只读目录”生成器（参数中心 P10）。只做静态扫描：用 ast 解析源码文件，
#   不 import 任何产品代码，不读运行时配置。产物是随包 JSON（agent_py_agent/config/constants_catalog.json），
#   供 /settings internal 与 user_config search 做查找展示；常数的权威位置仍是读取它的那行源码，
#   目录只是投影，绝不参与任何机器判定。协议类常数（状态码、事件名、版本号等）按规则排除。
#   改动扫描规则须同步 agent_py_agent/tests/test_constants_catalog.py（一致性、白名单、协议排除、单位/类别样例）。
# 模块用途: 让人和 my-agent 一眼看到“这个常数在哪个文件哪一行、值多少、单位是什么、干什么用”，
#   要改就去那一行改；--check 模式供守卫测试验证目录与源码一致。
from __future__ import annotations

import argparse
import ast
import json
import operator
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "agent_py_agent"
_DEFAULT_OUTPUT = _PACKAGE / "config" / "constants_catalog.json"
# 扫描范围：产品代码主体（与 test_constant_names_unique 同一口径），不含测试、vendor 与数据目录。
_SCANNED = ("agent", "cli")
# 模块级、全大写（允许前导下划线）的常数才算目录对象。
_NAME = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
# 说明要求是中文：紧挨着定义上方的注释行必须含中文字符。
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 单位推导：常数名结尾后缀 → 中文单位；长后缀优先，推不出记空（表示没有单位或不适用）。
_UNIT_SUFFIXES = (
    ("_SECONDS", "秒"),
    ("_SECOND", "秒"),
    ("_MILLISECONDS", "毫秒"),
    ("_MS", "毫秒"),
    ("_MINUTES", "分钟"),
    ("_HOURS", "小时"),
    ("_DAYS", "天"),
    ("_CHARS", "字符"),
    ("_BYTES", "字节"),
    ("_TOKENS", "tokens"),
    ("_TOKEN", "tokens"),
    ("_COUNT", "个"),
    ("_PERCENT", "%"),
    ("_TURNS", "轮"),
    ("_FILES", "个文件"),
    ("_REQUESTS", "次"),
    ("_KB", "KB"),
    ("_MB", "MB"),
    ("_GB", "GB"),
)

# 类别判定：按顺序命中第一条关键字（只读常数名，纯展示用，不参与守卫）。重试在超时前（_MAX_RETRIES 算重试），
# 超时在上限前（名字带 TIMEOUT 的按超时归类更贴切），比例阈值在上限前（_MAX_PERCENT 算比例）。
_CATEGORY_RULES = (
    ("重试", ("RETRY", "RETRIES", "ATTEMPT", "REDISPATCH")),
    ("超时", ("TIMEOUT",)),
    ("比例阈值", ("RATIO", "PERCENT", "THRESHOLD", "FACTOR")),
    ("上限预算", ("MAX", "LIMIT", "BUDGET", "RESERVE")),
)

# 协议类常数不收：状态码、事件名、schema/格式版本号、协议标记、类型名。这些是协议身份不是可调数值。
_PROTOCOL_MARKERS = ("VERSION", "STATUS", "EVENT", "SCHEMA", "PROTOCOL", "CODE")

# 数值字面量的算术求值表（只处理“简单算术”，与 _is_numeric 的判定范围一致）。
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


# LLM: 只认数字字面量与由它们组成的简单算术（四则、整除、取模、乘方、正负号）；布尔、字符串、列表不是数值。
#   与 test_constant_names_unique 的同一判定口径，保证目录与“同名常数守卫”看到的集合一致。
# 函数用途: 判断赋值右边是不是纯数值表达式。
def _is_numeric(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _is_numeric(node.operand)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _is_numeric(node.left) and _is_numeric(node.right)
    return False


# LLM: 对 _is_numeric 判定通过的表达式求值；不支持的节点返回 None（防御，正常扫描不会走到）。纯函数。
# 函数用途: 算出常数定义右边的实际数值，写进目录的 value 字段。
def _eval_numeric(node: ast.AST) -> int | float | None:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        return _UNARY[type(node.op)](_eval_numeric(node.operand))
    if isinstance(node, ast.BinOp):
        left, right = _eval_numeric(node.left), _eval_numeric(node.right)
        if left is None or right is None:
            return None
        return _BINARY[type(node.op)](left, right)
    return None


# LLM: 协议类排除规则集中在这里，守卫测试用样例钉住（HTTP_STATUS_OK 不收、SCHEMA_VERSION 不收、EVENT_READY 不收）。
#   名字含版本/状态/事件/schema/协议/码字样的，或以 _TYPE 结尾的，都算协议身份不是可调数值。
# 函数用途: 判断一个常数名是否属于协议类（不收进目录）。
def _is_protocol_constant(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _PROTOCOL_MARKERS) or upper.endswith("_TYPE")


# LLM: 只按名字最后一个完整记号匹配；长后缀优先（_SECONDS 在 _SECOND 前、_MILLISECONDS 在 _MS 前），
#   推不出返回空串，不猜别名（与参数登记表的 unit_for_key 同一思路）。
# 函数用途: 从常数名后缀推导单位（秒/毫秒/字符/字节/tokens/百分比等）。
def unit_for_name(name: str) -> str:
    stripped = str(name or "").lstrip("_")
    for suffix, unit in _UNIT_SUFFIXES:
        if stripped.endswith(suffix):
            return unit
    return ""


# LLM: 按 _CATEGORY_RULES 顺序命中第一条；没命中是“其它”。只读名字，纯展示分类，不影响任何机器判定。
# 函数用途: 给一个常数分展示类别（上限预算/超时/比例阈值/重试/其它）。
def category_for_name(name: str) -> str:
    upper = str(name or "").upper()
    for category, markers in _CATEGORY_RULES:
        if any(marker in upper for marker in markers):
            return category
    return "其它"


# LLM: 只取紧挨着定义上方的最近一行非空注释；必须含中文字符才算说明（英文 noqa 一类不算）。
#   多行注释块只取最近一行，够“一眼看懂干什么用”之用。纯函数，不解析注释内容。
# 函数用途: 从源码行里取常数上方的中文说明；没有则返回空串。
def _description(lines: list[str], lineno: int) -> str:
    index = lineno - 2
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0:
        return ""
    text = lines[index].strip()
    if text.startswith("#") and _CJK.search(text):
        return text.lstrip("#").strip()
    return ""


# LLM: 只认模块级单目标赋值（NAME = …）和带值的注解赋值（NAME: T = …）；其它语句返回 None。纯函数。
# 函数用途: 取出一条模块级语句赋值的名字与右值，供常数定位/收集共用同一判定。
def _assigned_name(node: ast.stmt) -> tuple[str, ast.expr] | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return node.target.id, node.value
    return None


# LLM: 目录只收全大写、非协议类、右值可静态求出数值的模块级常数；与 _module_entries 同一判定。纯函数。
# 函数用途: 判断一个模块级赋值是否属于常数目录收录范围。
def _is_catalog_constant(target: str, value: ast.expr) -> bool:
    return bool(_NAME.match(target)) and not _is_protocol_constant(target) and _is_numeric(value)


# LLM: 收集一个源码文件里所有合格的模块级数值常数（全大写、非协议类、数字字面量或简单算术），
#   每个条目带名字、相对路径、计算值、单位、类别、上方中文说明。不存行号：目录唯一失效时机是常数
#   增删、改名、改值、改说明或改单位/类别，源码里插入空行/换行这类行号漂移不应当让目录过期（行号由
#   查看入口在运行时按名字定位）。注释行取同一份源文件的行数组。单个文件读不出来或语法错误时跳过。
# 函数用途: 把一个 .py 文件变成常数目录条目列表。
def _module_entries(root: Path, path: Path) -> list[dict]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    relative = str(path.relative_to(root).as_posix())
    entries: list[dict] = []
    for node in tree.body:
        assigned = _assigned_name(node)
        if assigned is None or not _is_catalog_constant(*assigned):
            continue
        target, value = assigned
        entries.append({
            "name": target,
            "file": relative,
            "value": _eval_numeric(value),
            "unit": unit_for_name(target),
            "category": category_for_name(target),
            "description": _description(lines, node.lineno),
        })
    return entries


# LLM: 全量扫描产品代码（agent 与 cli），按文件路径+名字排序输出稳定列表；不 import 任何产品模块。
#   每次调用都是全新扫描，不缓存，保证 --check 与写文件用同一份结果。不用行号排序：行号不属于目录内容。
# 函数用途: 生成完整的常数目录条目列表。
def build_catalog(root: Path | None = None) -> list[dict]:
    base = Path(root) if root is not None else _ROOT
    entries: list[dict] = []
    for package in _SCANNED:
        for path in sorted((base / "agent_py_agent" / package).rglob("*.py")):
            entries.extend(_module_entries(base, path))
    entries.sort(key=lambda entry: (entry["file"], entry["name"]))
    return entries


# LLM: --check 的核心：重扫源码与随包 JSON 的 constants 逐项比较（忽略元数据字段），返回差异描述列表；
#   空列表表示一致。目录文件缺失、JSON 损坏都算差异。守卫测试直接调它，不依赖子进程。
# 函数用途: 检查随包常数目录是否与当前源码一致，不一致时给出可读差异。
def check_catalog(root: Path | None = None, catalog_path: Path | None = None) -> list[str]:
    entries = build_catalog(root)
    output = Path(catalog_path) if catalog_path is not None else _DEFAULT_OUTPUT
    if not output.is_file():
        return [f"常数目录文件不存在：{output}"]
    try:
        current = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"常数目录文件读不了：{output}（{exc}）"]
    stored = current.get("constants")
    if not isinstance(stored, list):
        return [f"常数目录缺少 constants 列表：{output}"]
    if stored != entries:
        return [f"常数目录与源码不一致：目录 {len(stored)} 项，源码扫描 {len(entries)} 项（目录过期，请重新生成）"]
    return []


# LLM: CLI 入口：默认写文件，--check 只比较不写；--output 可换输出路径（测试用）。退出码 0 表示一致/写成功。
# 函数用途: 命令行生成或校验常数目录。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或校验模块级数值常数目录（参数中心 P10）")
    parser.add_argument("--check", action="store_true", help="只校验目录与源码是否一致，不写文件")
    parser.add_argument("--output", default=None, help="输出 JSON 路径（默认随包 config/constants_catalog.json）")
    args = parser.parse_args(argv)
    output = Path(args.output) if args.output else _DEFAULT_OUTPUT
    if args.check:
        differences = check_catalog(_ROOT, output)
        if not differences:
            print(f"常数目录与源码一致（{len(build_catalog(_ROOT))} 项）。")
            return 0
        for line in differences:
            print(line)
        return 1
    entries = build_catalog(_ROOT)
    payload = {
        "schema_version": 1,
        "generated_by": "scripts/build_constants_catalog.py",
        "count": len(entries),
        "constants": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成常数目录：{output}（{len(entries)} 项）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())