"""飞书富媒体测试 — 入站解析、文件路由、multipart 构造、安全文件名(纯函数)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.adapter.feishu_media import (
    build_multipart,
    extract_feishu_content,
    file_message_type,
    file_upload_type,
    safe_media_filename,
)


class TestExtractContent:
    def test_text(self) -> None:
        assert extract_feishu_content("text", '{"text":"你好"}') == ("你好", {})

    def test_text_malformed_fallback(self) -> None:
        # 非 JSON content 容错当纯文本,不丢消息
        assert extract_feishu_content("text", "not-json") == ("not-json", {})

    def test_text_empty(self) -> None:
        # 解析成功但 text 空 → 空(交由上层判空丢弃)
        assert extract_feishu_content("text", '{"text":""}') == ("", {})

    def test_image(self) -> None:
        assert extract_feishu_content("image", '{"image_key":"img1"}') == ("[图片]", {"image_key": "img1"})

    def test_file_with_name(self) -> None:
        text, media = extract_feishu_content("file", '{"file_key":"f1","file_name":"a.pdf"}')
        assert text == "[文件:a.pdf]"
        assert media == {"file_key": "f1", "file_name": "a.pdf"}

    def test_unknown_type_placeholder(self) -> None:
        # 未知类型给占位,绝不丢消息
        assert extract_feishu_content("sticker", "{}") == ("[sticker 消息]", {})

    def test_post_flatten(self) -> None:
        content = '{"zh_cn":{"title":"标题","content":[[{"tag":"text","text":"正文"}]]}}'
        text, _ = extract_feishu_content("post", content)
        assert "标题" in text and "正文" in text

    @pytest.mark.parametrize('shape', ['direct', 'localized', 'wrapped', 'other-locale'])
    def test_post_code_markdown_links_and_unknown_text_are_preserved(self, shape):
        code = 'def main():\n    print("```合成测试")\n\nmain()'
        post = {'title': '合成富文本', 'content': [
            [{'tag': 'text', 'text': '检查以下代码：'}],
            [{'tag': 'code_block', 'language': 'python', 'text': code}],
            [{'tag': 'md', 'text': '**完整 Markdown**'}],
            [{'tag': 'a', 'text': '接口', 'href': 'https://example.invalid/api'}],
            [{'tag': 'future_text', 'text': '未来节点的正文'}],
        ]}
        content = post if shape == 'direct' else {'ja_jp' if shape == 'other-locale' else 'zh_cn': post}
        if shape == 'wrapped':
            content = {'post': content}
        text, _ = extract_feishu_content('post', content)
        assert code in text and '````python' in text
        assert '**完整 Markdown**' in text and 'https://example.invalid/api' in text
        assert '未来节点的正文' in text

    def test_post_keeps_each_embedded_media_ref_and_handles_bad_rows(self):
        text, media = extract_feishu_content('post', {'content': [None, 7,
            [{'tag': 'img', 'image_key': 'image-one'}, {'tag': 'img', 'image_key': 'image-two'}],
            [{'tag': 'file', 'file_key': 'file-one', 'file_name': 'sample.txt'}]]})
        assert '[图片]' in text and 'sample.txt' in text
        assert [row.get('image_key') or row.get('file_key') for row in media['items']] == ['image-one', 'image-two', 'file-one']

    def test_wrapped_post_mentions_and_command_use_same_block_as_content(self):
        from agent_py_agent.agent.adapter.feishu_mentions import directly_mentions_bot
        from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
        message = {'message_type':'post','content':{'post':{'ja_jp':{'content':[[
            {'tag':'at','user_id':'ou_bot'}, {'tag':'text','text':' /verbose stream'}]]}}}}
        assert directly_mentions_bot(message, 'ou_bot')
        incoming = feishu_to_incoming({'event':{'message':message,'sender':{'sender_id':{'open_id':'ou_one'}}}},bot_open_id='ou_bot')
        assert incoming.content == '/verbose stream'


class TestFileRouting:
    def test_upload_type(self) -> None:
        assert file_upload_type(Path("a.pdf")) == "pdf"
        assert file_upload_type(Path("a.mp4")) == "mp4"
        assert file_upload_type(Path("a.xyz")) == "stream"

    def test_message_type(self) -> None:
        assert file_message_type(Path("a.opus")) == "audio"
        assert file_message_type(Path("a.mp4")) == "media"
        assert file_message_type(Path("a.txt")) == "file"


class TestMultipart:
    def test_build(self) -> None:
        body, ct = build_multipart({"k": "v"}, "image", "a.png", b"\x89PNG")
        assert ct.startswith("multipart/form-data; boundary=")
        assert b'name="k"' in body and b"a.png" in body and b"\x89PNG" in body


class TestSafeFilename:
    def test_with_original_name(self) -> None:
        assert safe_media_filename("om_1", {"file_name": "report.pdf"}, "fk", ".bin") == "om_1-report.pdf"

    def test_path_traversal_stripped(self) -> None:
        # 路径分隔去掉防穿越
        name = safe_media_filename("om_1", {"file_name": "../../etc/passwd"}, "fk", ".bin")
        assert "/" not in name

    def test_no_name_uses_key(self) -> None:
        assert safe_media_filename("om_1", {}, "imgkey", ".png") == "om_1-imgkey.png"
