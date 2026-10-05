"""参数中心 P10：模块级数值常数的“只读目录 + 守卫”（2026-10-01）。

背景：用户指出代码里参数/常数混乱，“最好有个专门的地方统一修改，而不是每个代码里改不同的参数还要猜参数是干啥的”。
设计（已定）：常数留在读取它的地方（唯一权威），目录是投影——scripts/build_constants_catalog.py 用 ast 静态扫描
agent_py_agent，生成随包 JSON（config/constants_catalog.json），供 /settings internal 与 user_config search 查找展示，
改动仍需改源码那一行。

锁定：
- 目录必须与源码一致（新增/改名/改值常数后不重新生成目录就失败）；
- 协议类常数不收（状态码、事件名、schema/格式版本号等），规则见 _is_protocol_constant；
- 不合规常数（无单位后缀或无上方中文说明）只允许出现在 fixtures/constants_catalog_pending_fixes.json 的待整改白名单里，
  名单外的常数必须合规；白名单里的常数已合规或已删除时必须从白名单删掉（名单只会变短）。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.build_constants_catalog import (
    _is_protocol_constant,
    _module_entries,
    build_catalog,
    category_for_name,
    check_catalog,
    unit_for_name,
)

_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = _ROOT / "agent_py_agent" / "config" / "constants_catalog.json"
_FIXTURES = _ROOT / "agent_py_agent" / "tests" / "fixtures" / "constants_catalog_pending_fixes.json"


# 函数用途: 读待整改白名单里的常数名集合。
def _pending_names() -> set[str]:
    payload = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    return {key for group in payload["groups"] for key in group["keys"]}


# 函数用途: 把目录条目按名字分组（同名不同文件的常数归一组）。
def _definitions_by_name(entries: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["name"], []).append(entry)
    return grouped


def test_catalog_matches_source():
    differences = check_catalog(_ROOT, _CATALOG)
    assert not differences, "\n".join(differences)


def test_pending_fixes_whitelist_covers_all_noncompliant_constants():
    entries = build_catalog(_ROOT)
    pending = _pending_names()
    noncompliant = sorted({entry["name"] for entry in entries if not entry["unit"] or not entry["description"]} - pending)
    assert not noncompliant, (
        "这些常数不合规（无单位后缀或无上方中文说明）但不在待整改白名单里；"
        f"新增常数必须有单位后缀和上方中文说明：{noncompliant[:20]}…（共 {len(noncompliant)} 个）"
    )


def test_pending_fixes_whitelist_only_shrinks():
    entries = build_catalog(_ROOT)
    by_name = _definitions_by_name(entries)
    pending = _pending_names()
    stale = sorted(name for name in pending
                   if name in by_name and all(entry["unit"] and entry["description"] for entry in by_name[name]))
    assert not stale, f"这些白名单条目已经合规（有单位且有说明），请从白名单删掉：{stale[:20]}…（共 {len(stale)} 个）"


def test_pending_fixes_whitelist_has_no_stale_entries():
    entries = build_catalog(_ROOT)
    names = {entry["name"] for entry in entries}
    stale = sorted(_pending_names() - names)
    assert not stale, f"这些白名单条目对应的常数已不存在，请从白名单删掉：{stale[:20]}…（共 {len(stale)} 个）"


def test_protocol_constants_are_excluded():
    for name in ("HTTP_STATUS_OK", "SCHEMA_VERSION", "EVENT_READY", "_PROTOCOL_VERSION", "EXIT_CODE", "MESSAGE_TYPE"):
        assert _is_protocol_constant(name), f"{name} 应被当作协议类排除"
    for name in ("REQUEST_TIMEOUT_SECONDS", "MAX_CONNECTIONS", "DEFAULT_PORT"):
        assert not _is_protocol_constant(name), f"{name} 不应被当作协议类排除"


def test_unit_inference():
    assert unit_for_name("REQUEST_TIMEOUT_SECONDS") == "秒"
    assert unit_for_name("_MAX_BODY_BYTES") == "字节"
    assert unit_for_name("RESPONSE_PREVIEW_CHARS") == "字符"
    assert unit_for_name("SCREENSHOT_MAX_WIDTH_PX") == "像素"
    assert unit_for_name("TOKEN_BUDGET") == ""
    assert unit_for_name("DEFAULT_PORT") == ""


def test_category_inference():
    assert category_for_name("MAX_RETRIES") == "重试"
    assert category_for_name("REQUEST_TIMEOUT_SECONDS") == "超时"
    assert category_for_name("COMPACT_TRIGGER_PERCENT") == "比例阈值"
    assert category_for_name("MAX_BODY_BYTES") == "上限预算"
    assert category_for_name("DEFAULT_PORT") == "其它"


def test_scanner_collects_only_module_level_numeric(tmp_path):
    package = tmp_path / "agent_py_agent" / "agent"
    package.mkdir(parents=True)
    source = package / "sample.py"
    source.write_text(
        "from x import LIMIT\n"
        "# 上方中文说明：请求超时\n"
        "_TIMEOUT_SECONDS = 15\n"
        "MAX_BYTES = 8 * 1024 * 1024\n"
        "FLAG = True\n"
        "NAME = 'a'\n"
        "def f():\n"
        "    INNER = 3\n",
        encoding="utf-8",
    )
    entries = _module_entries(tmp_path, source)
    assert [(entry["name"], entry["value"], entry["unit"], entry["description"]) for entry in entries] == [
        ("_TIMEOUT_SECONDS", 15, "秒", "上方中文说明：请求超时"),
        ("MAX_BYTES", 8388608, "字节", ""),
    ]


def test_runtime_catalog_search_finds_by_name_file_and_description():
    from agent_py_agent.agent.settings.constants_catalog import load_catalog, search_constants

    entries = load_catalog()
    assert entries, "随包常数目录应为空之外的真实数据"
    first = entries[0]
    assert search_constants(first["name"], limit=20)
    assert search_constants(first["file"].split("/")[-1], limit=50)
    if first.get("description"):
        assert search_constants(first["description"][:4], limit=50)


def test_runtime_locate_line_finds_definition_only_in_that_file():
    from agent_py_agent.agent.settings.constants_catalog import (
        entry_with_line,
        load_catalog,
        locate_line,
    )

    first = load_catalog()[0]
    line = locate_line(first["file"], first["name"])
    assert isinstance(line, int) and line > 0, "目录里存在的常数应在它的源码文件里定位到行号"
    view = entry_with_line(first)
    assert view["line"] == line
    assert locate_line(first["file"], "NO_SUCH_CONSTANT_XYZ") is None
    assert locate_line("agent_py_agent/agent/not_a_real_file.py", "MAX_BYTES") is None
    assert locate_line("not_under_package.py", "MAX_BYTES") is None


def test_line_shift_does_not_invalidate_catalog(tmp_path):
    package = tmp_path / "agent_py_agent" / "agent"
    package.mkdir(parents=True)
    source = package / "sample.py"
    source.write_text(
        "# 上方中文说明：请求超时\n"
        "_TIMEOUT_SECONDS = 15\n"
        "MAX_BYTES = 8 * 1024 * 1024\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "constants_catalog.json"
    entries = build_catalog(tmp_path)
    catalog.write_text(json.dumps({"schema_version": 1, "count": len(entries), "constants": entries},
                                  ensure_ascii=False), encoding="utf-8")
    assert check_catalog(tmp_path, catalog) == []
    # 在常数上方插入空行：行号变了、常数本身没变，--check 必须仍然通过（目录不存行号）。
    source.write_text(
        "# 上方中文说明：请求超时\n"
        "\n"
        "\n"
        "_TIMEOUT_SECONDS = 15\n"
        "\n"
        "MAX_BYTES = 8 * 1024 * 1024\n",
        encoding="utf-8",
    )
    assert check_catalog(tmp_path, catalog) == []