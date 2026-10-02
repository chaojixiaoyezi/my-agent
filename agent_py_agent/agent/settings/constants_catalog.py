# LLM: 常数目录（参数中心 P10）的运行时只读入口：随包 JSON 是 scripts/build_constants_catalog.py 生成的投影，
#   权威位置是读取常数的那行源码。本模块只做查找与展示（/settings internal 与 user_config search 共用），
#   不参与任何机器判定，不提供修改；JSON 缺失或损坏时返回空目录，展示入口如实显示不可用。
#   改动须同步 tooling/user_config_tool.py、gateway_parts/settings_control_service.py 与
#   agent_py_agent/tests/test_constants_catalog.py。
# 模块用途: 让用户和 my-agent 按名字/说明/文件搜到“这个常数在哪个文件哪一行、值多少、单位是什么、干什么用”。
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CATALOG = Path(__file__).resolve().parents[2] / "config" / "constants_catalog.json"


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
#   其余按文件路径+行号稳定排序；limit 为 0 不限。纯展示，不改动目录。嵌套控制在两层内。
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
    found = by_name + sorted(by_other, key=lambda entry: (entry.get("file"), entry.get("line")))
    return found[:limit] if limit else found


__all__ = ["load_catalog", "search_constants"]