"""使用隔离合成凭据验证临时来源快照、流与原坐标分页。"""
import subprocess
import sys
from dataclasses import dataclass, field

from agent_py_agent.agent.settings.secret_ref import (
    HostCredentialSnapshot,
    configured_credential_snapshot,
)
from agent_py_agent.agent.tooling.host_output_redaction import (
    HostOutputRedaction,
    HostOutputStreamRedactor,
    redact_host_tool_outcome,
    redact_source_window,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.process_output_capture import ProcessOutputCapture


@dataclass
class Config:
    credential: str = field(default='', metadata={'secret_ref': True})
    ordinary: str = 'not-a-credential'
    tool_read_max_chars: int = 64


def test_snapshot_sources_are_declared_bounded_private(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('SYNTHETIC_SOURCE', 'opaque arbitrary value')
    value = configured_credential_snapshot(Config('env:SYNTHETIC_SOURCE'))
    assert value.secret_values == ('opaque arbitrary value',)
    assert 'opaque arbitrary value' not in repr(value)
    assert 'not-a-credential' not in value.secret_values
    missing = configured_credential_snapshot(Config('file:' + str(tmp_path / 'absent')))
    assert missing.coverage == 'partial'
    assert missing.unavailable_sources[0].reason_code == 'CREDENTIAL_SOURCE_MISSING'
    source = tmp_path / 'secret'
    source.write_text('x' * 65)
    large = configured_credential_snapshot(Config('file:' + str(source)))
    assert large.unavailable_sources[0].reason_code == 'CREDENTIAL_SOURCE_TOO_LARGE'


def test_every_stream_split_and_original_coordinate_window():
    secret = '私密-opaque-value'
    raw = ('head\n' + secret + '\ntail').encode()
    for split in range(len(raw) + 1):
        stream = HostOutputStreamRedactor(secret_values=(secret,))
        safe = stream.feed(raw[:split]) + stream.feed(raw[split:]) + stream.finish()
        assert secret.encode() not in safe
        assert safe.count(b'\n') == 2
        assert len(safe) == len(raw)
    text = raw.decode()
    pages = [redact_source_window(lambda start, size: text[start:start + size],
        offset=i, limit=1, total_chars=len(text), secret_values=(secret,)) for i in range(len(text))]
    assert ''.join(pages) == 'head\n' + '*' * len(secret) + '\ntail'


def test_capture_scrubs_before_retention_and_preserves_source_facts():
    secret = 'opaque-private-credential'
    proc = subprocess.Popen([sys.executable, '-c',
        "import os; os.write(1,b'headopaque-private-'); os.write(1,b'credentialtail'); os.write(2,b'opaque-private-credential')"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    capture = ProcessOutputCapture(proc, max_bytes=12,
        host_output_redaction=HostOutputRedaction(snapshot=HostCredentialSnapshot(secret_values=(secret,))))
    proc.wait(timeout=5)
    assert capture.finish(5)
    out, err, facts = capture.result()
    assert out == 'head********'
    assert err == '*' * 12
    assert facts['bytes_seen'] == {'stdout': len('head' + secret + 'tail'), 'stderr': len(secret)}
    assert facts['bytes_discarded']['stdout'] == len('head' + secret + 'tail') - 12


def test_outcome_errors_and_nested_projection_keep_effect_facts():
    secret = 'plain opaque credential'
    original = ToolHandlerOutcome('shell', False, secret, error_code='EXACT_FAILURE',
        effect_outcome='unknown', result_envelope={'_display': secret, 'nested': [secret]})
    safe = redact_host_tool_outcome(original,
        snapshot=HostCredentialSnapshot(secret_values=(secret,), snapshot_error_code='CREDENTIAL_SNAPSHOT_UNAVAILABLE'),
        force_default=True)
    assert secret not in repr(safe)
    assert safe.error_code == original.error_code and safe.effect_outcome == 'unknown'
    assert safe.result_envelope['credential_redaction']['coverage'] == 'partial'
    assert original.output == secret


def test_generic_patterns_split_prompt_and_bounded_long_line():
    raw = b'normal diagnostic\nAuthorization: Bearer unusual opaque credential\npassword=plain-secret\n'
    for split in range(len(raw) + 1):
        stream = HostOutputStreamRedactor()
        result = stream.feed(raw[:split]) + stream.feed(raw[split:]) + stream.finish()
        assert b'plain-secret' not in result
        assert b'unusual opaque credential' not in result
        assert b'normal diagnostic' in result
    stream = HostOutputStreamRedactor(secret_values=('x' * 10000,))
    assert stream.feed(b'Enter input: ') == b'Enter input: '
    assert len(stream.feed(b'x' * 200000)) < 100
    assert len(stream._line) <= 65536


def test_real_file_pages_keep_offsets_and_withhold_missing_source(tmp_path):
    from agent_py_agent.agent.settings.secret_ref import CredentialSourceStatus
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
    source = tmp_path / 'config.txt'
    source.write_text('prefix SECRET suffix\n')
    tool = ReadFileTool(tmp_path, max_chars=100)
    tool._host_output_redaction = HostOutputRedaction(snapshot=HostCredentialSnapshot(secret_values=('SECRET',)))
    first = tool.execute({'path': str(source), 'offset': 7, 'max_chars': 3})
    second = tool.execute({'path': str(source), 'offset': 10, 'max_chars': 3})
    assert 'SEC' not in first.output and 'RET' not in second.output
    assert first.result_envelope['read_window']['next_offset'] == 10
    tool._host_output_redaction = HostOutputRedaction(snapshot=HostCredentialSnapshot(
        unavailable_sources=(CredentialSourceStatus(field_name='credential',
            source_path=source, reason_code='CREDENTIAL_SOURCE_TOO_LARGE'),)))
    withheld = tool.execute({'path': str(source)})
    assert withheld.ok and 'SECRET' not in withheld.output
    assert withheld.result_envelope['credential_redaction']['content_status'] == 'withheld'
