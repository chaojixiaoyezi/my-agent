# LLM: 常数目录（参数中心 P10）的运行时只读入口：随包 JSON 是 scripts/build_constants_catalog.py 生成的投影，
#   权威位置是读取常数的那行源码。本模块只做查找与展示（/settings internal 与 user_config search 共用），
#   不参与任何机器判定，不提供修改；JSON 缺失或损坏时返回空目录，展示入口如实显示不可用。
#   目录不存行号（行号漂移不应让目录过期）；要显示“文件:行”时用 locate_line 只读打开那一个文件按名字定位，
#   定位不到就只显示文件。改动须同步 tooling/user_config_tool.py、gateway_parts/settings_control_service.py 与
#   agent_py_agent/tests/test_constants_catalog.py。
# 模块用途: 让用户和 my-agent 按名字/说明/文件搜到“这个常数在哪个文件哪一行、值多少、单位是什么、干什么用”。
from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path

_CATALOG = Path(__file__).resolve().parents[2] / "config" / "constants_catalog.json"
_PACKAGE = Path(__file__).resolve().parents[2]


# LLM: 只读随包 JSON；文件缺失或内容损坏返回空列表（查看入口不因目录问题报错），进程内缓存一次。
#   生成与校验由 scripts/build_constants_catalog.py 和守卫测试负责，运行时不做二次比对。
# 函数用途: 读出常数目录条目列表；读不到时返回空列表。
@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    try:
        payload = json.loads(_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = payload.get("constants")
    return entries if isinstance(entries, list) else []


# LLM: 关键词同时匹配名字、说明与文件路径（不区分大小写）；名字命中的排前（完全相同、再以关键词开头、再短名字优先），
#   其余按文件路径+名字稳定排序；limit 为 0 不限。纯展示，不改动目录。嵌套控制在两层内。
# 函数用途: 按关键词查找代码常数，给用户和模型“这个常数在哪”的答案。
def search_constants(query: str, *, limit: int = 10) -> list[dict]:
    needle = str(query or "").strip().lower()
    entries = load_catalog()
    if not needle:
        return entries[:limit] if limit else entries
    by_name: list[dict] = []
    by_other: list[dict] = []
    for entry in entries:
        name = str(entry.get("name") or "").lower()
        if needle in name:
            by_name.append(entry)
        elif needle in str(entry.get("description") or "").lower() or needle in str(entry.get("file") or "").lower():
            by_other.append(entry)
    by_name.sort(key=lambda entry: (str(entry.get("name")) != needle,
                                    not str(entry.get("name")).startswith(needle), len(str(entry.get("name")))))
    found = by_name + sorted(by_other, key=lambda entry: (entry.get("file"), entry.get("name")))
    return found[:limit] if limit else found


# LLM: 只读打开目录里记录的那一个源码文件，用 ast 找模块级 name 的赋值行；目录里的 file 是仓库相对
#   路径（以 agent_py_agent/ 开头），剥掉前缀拼到包根。找不到名字、文件读不了或语法错误都返回 None，
#   展示层遇到 None 就只显示文件不显示行号。绝不扫描整个包。
# 函数用途: 按名字在单个文件里定位常数定义行号。
def locate_line(file_rel: str, name: str) -> int | None:
    relative = str(file_rel or "")
    if not relative.startswith("agent_py_agent/"):
        return None
    path = _PACKAGE / relative[len("agent_py_agent/"):]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target = node.target.id
        if target == name:
            return node.lineno
    return None


# LLM: 查看入口显示“文件:行”用：只读定位该条目的行号并附到副本；定位不到就只带文件不带行号。
#   绝不把行号写回目录 JSON，也不参与一致性比较。纯展示。
# 函数用途: 给一条目录条目附上运行时定位的行号（定位不到则省略）。
def entry_with_line(entry: dict) -> dict:
    line = locate_line(str(entry.get("file") or ""), str(entry.get("name") or ""))
    if line is None:
        return dict(entry)
    return {**entry, "line": line}


__all__ = ["entry_with_line", "load_catalog", "locate_line", "search_constants"]