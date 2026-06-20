"""审计 #24(部分)修复真测:自建 YAML 解析引号感知去注释——引号内的 '#' 不再被当注释截断。

原问题:load_simple_yaml 用 raw.split('#',1)[0] 在解析引号前就把 '#' 后当注释砍掉,color: "#ff0000" → ''、
含 # 的 URL/口令被静默清空(配置项静默失效)。修复后引号内 '#' 保留,引号外的尾注释/整行注释照常去掉。
"""

from __future__ import annotations

from agent_py_agent.agent.settings.config_io import _strip_yaml_comment, load_simple_yaml


def test_strip_comment_respects_quotes() -> None:
    assert _strip_yaml_comment('color: "#ff0000"') == 'color: "#ff0000"'  # 双引号内 # 保留
    assert _strip_yaml_comment("k: 'a # b'") == "k: 'a # b'"  # 单引号内 # 保留
    assert _strip_yaml_comment("plain: hello  # trailing") == "plain: hello"  # 引号外尾注释去掉
    assert _strip_yaml_comment("# whole line") == ""  # 整行注释
    assert _strip_yaml_comment('url: "http://x#frag"') == 'url: "http://x#frag"'  # URL fragment 不截


def test_load_yaml_hash_in_quotes_not_truncated(tmp_path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        'color: "#ff0000"\n'
        'note: "a # b"\n'
        'plain: hello  # trailing comment\n'
        "# full comment line\n"
        'url: "http://example.com/x#frag"\n',
        encoding="utf-8",
    )
    d = load_simple_yaml(p)
    assert d["color"] == "#ff0000"  # 引号内 # 不被截(原会变空字符串)
    assert d["note"] == "a # b"
    assert d["plain"] == "hello"  # 引号外尾注释正常去掉
    assert d["url"] == "http://example.com/x#frag"
    assert "full comment line" not in d  # 整行注释不入数据
