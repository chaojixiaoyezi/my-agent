"""W3 入站指标合同：仅解析给定值，不读附件、不做网络请求。"""
import pytest

from agent_py_agent.agent.capability.inbound_indicators import extract_indicators


# LLM: 测试只投影结果，不改变被测输入或来源。
# 函数用途: 取得某种指标的值供精确断言。
def values(rows, kind):
    return [row['value'] for row in rows if row['type'] == kind]


# LLM: 有效IP须完整校验，保留范围标记且不将版本解释为地址。
# 函数用途: 验证地址、保留范围和典型误报。
@pytest.mark.parametrize('text,expected', [
    ('8.8.8.8', ['8.8.8.8']), ('192.168.2.1', ['192.168.2.1']),
    ('999.1.1.1', []), ('v1.2.3.4', []), ('version 1.2.3.4', []),
    ('version: 1.2.3.4', []), ('1.2.3.4.5', []),
])
def test_ipv4_validation_and_versions(text, expected):
    rows = extract_indicators(text)
    assert values(rows, 'ipv4') == expected
    for row in rows:
        if row['type'] == 'ipv4':
            assert row['reserved'] is (text == '192.168.2.1')


# LLM: 文件扩展名是有限误报排除，不把普通域名及其子域一并排除。
# 函数用途: 验证域名识别及文件名否定对照。
@pytest.mark.parametrize('text,expected', [
    ('example.com', ['example.com']), ('api.example.org', ['api.example.org']),
    ('report.json main.py README.md data.csv build.js', []),
    ('image.png archive.tar.gz', []),
])
def test_domains_ignore_file_names(text, expected):
    assert values(extract_indicators(text), 'domain') == expected


# LLM: 来源和首次出现顺序来自给定文本，不按类型排序或去网络核验。
# 函数用途: 覆盖URL、email、摘要、CVE和IPv6的完整值。
def test_url_email_hashes_cve_and_ipv6():
    md5, sha1, sha256 = 'a' * 32, 'b' * 40, 'c' * 64
    text = f'https://example.com/a?q=1 user@example.org {md5} {sha1} {sha256} CVE-2024-12345 2001:db8::1'
    rows = extract_indicators(text)
    for kind, expected in [('url', 'https://example.com/a?q=1'), ('email', 'user@example.org'),
                           ('md5', md5), ('sha1', sha1), ('sha256', sha256),
                           ('cve_id', 'CVE-2024-12345'), ('ipv6', '2001:db8::1')]:
        assert values(rows, kind) == [expected]
    assert all(row['source'] == 'text' for row in rows)
    assert values(extract_indicators('a' * 31 + ' ' + 'b' * 41), 'md5') == []
    assert values(extract_indicators('a' * 31 + ' ' + 'b' * 41), 'sha1') == []


# LLM: 图片只消费元数据；OCR必须由调用方给出，不能隐式读取文件或运行识别。
# 函数用途: 验证纯图片、OCR和跨来源去重。
def test_image_only_and_adapter_ocr_are_scanned_without_file_access():
    attachment = {'media_type': 'image/png', 'name': 'alert.png', 'path': '/must/not/read', 'sha256': 'f' * 64}
    rows = extract_indicators('', attachments=[attachment], ocr_texts=['8.8.8.8 job48'])
    assert values(rows, 'image_attachment') == ['f' * 64]
    assert values(rows, 'ipv4') == ['8.8.8.8']
    assert next(row for row in rows if row['type'] == 'ipv4')['source'] == 'ocr:0'
    assert values(rows, 'monitor_alias') == ['job48']
    assert attachment['path'] == '/must/not/read'


# LLM: 扫描预算按UTF-8字节累计覆盖文本和OCR，不能靠多个字段绕过。
# 函数用途: 验证64KiB截断及多字节边界。
def test_scan_byte_cap_applies_to_all_text_sources():
    assert values(extract_indicators('x' * 65536 + ' 8.8.8.8'), 'ipv4') == []
    assert values(extract_indicators('汉' * 10 + ' 8.8.8.8', max_scan_bytes=30), 'ipv4') == []
    assert values(extract_indicators('x' * 32, ocr_texts=['8.8.8.8'], max_scan_bytes=32), 'ipv4') == []
    assert extract_indicators('8.8.8.8', max_scan_bytes=0) == []


# LLM: 同类型同值按首次观察去重，后来的来源不能改写首次来源。
# 函数用途: 验证确定顺序、去重及给定结构化标签。
def test_first_seen_order_and_structured_indicator_labels():
    rows = extract_indicators('8.8.8.8 1.1.1.1 8.8.8.8', ocr_texts=['1.1.1.1'])
    assert values(rows, 'ipv4') == ['8.8.8.8', '1.1.1.1']
    assert rows == extract_indicators('8.8.8.8 1.1.1.1 8.8.8.8', ocr_texts=['1.1.1.1'])
    labeled = extract_indicators('hostname=db01 port=443 process_name=worker file_path=/var/log/app alert_id=evt-9 monitor_alias=job48')
    for kind, value in [('hostname', 'db01'), ('port', '443'), ('process_name', 'worker'),
                        ('file_path', '/var/log/app'), ('alert_id', 'evt-9'), ('monitor_alias', 'job48')]:
        assert value in values(labeled, kind)
