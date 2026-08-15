"""飞书出站渲染测试 — markdown 选型、post 富文本构建、长消息分片(纯函数)。"""
from __future__ import annotations

import json

from agent_py_agent.agent.adapter.feishu_render import (
    build_markdown_post_rows,
    build_outbound_payload,
    has_markdown_table,
    looks_like_markdown,
    split_message,
    strip_markdown_to_plain_text,
)


class TestMarkdownDetection:
    def test_looks_like_markdown(self) -> None:
        assert looks_like_markdown("**bold**")
        assert looks_like_markdown("- item")
        assert looks_like_markdown("# title")
        assert not looks_like_markdown("普通文本没有任何标记")

    def test_has_markdown_table(self) -> None:
        assert has_markdown_table("| a | b |\n|---|---|\n| 1 | 2 |")
        assert not has_markdown_table("**bold** 但没有表格")


class TestOutboundPayload:
    def test_markdown_to_post(self) -> None:
        msg_type, content = build_outbound_payload("- a\n**b**")
        assert msg_type == "post"
        assert "zh_cn" in content  # post 富文本结构

    def test_plain_to_text(self) -> None:
        msg_type, content = build_outbound_payload("你好世界")
        assert msg_type == "text"
        assert json.loads(content) == {"text": "你好世界"}

    def test_table_downgrades_to_text(self) -> None:
        # 飞书 post 不渲染 markdown 表格 → 降级纯文本(否则显示空白)
        msg_type, _ = build_outbound_payload("| a | b |\n|---|---|\n| 1 | 2 |")
        assert msg_type == "text"


class TestPostRows:
    def test_code_fence_is_own_row(self) -> None:
        # code block 必须单独成 post 行,散文段各自聚合
        rows = build_markdown_post_rows("前言段\n```py\ncode here\n```\n后语段")
        assert len(rows) == 3  # 前言 / code块 / 后语

    def test_plain_single_row(self) -> None:
        assert len(build_markdown_post_rows("就一段散文")) == 1


class TestStripMarkdown:
    def test_strip_to_plain(self) -> None:
        out = strip_markdown_to_plain_text("# 标题\n**粗** `码` [链](http://x)")
        assert "**" not in out and "`" not in out and not out.startswith("#")
        assert "链" in out and "http://x" in out


class TestSplitMessage:
    def test_short_stays_one_piece(self) -> None:
        assert split_message("hi") == ["hi"]

    def test_long_single_line_hard_split(self) -> None:
        pieces = split_message("x" * 9000)
        assert len(pieces) == 2
        assert all(len(p) <= 8000 for p in pieces)

    def test_multiline_split_on_boundary(self) -> None:
        pieces = split_message("\n".join(["一行内容"] * 3000))
        assert len(pieces) >= 2
        assert all(len(p) <= 8000 for p in pieces)
