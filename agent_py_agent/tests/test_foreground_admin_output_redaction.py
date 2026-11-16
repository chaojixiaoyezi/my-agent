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



def test_ordinary_tier_source_code_policy_survives_private_snapshot(tmp_path):
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
    from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool
    source = tmp_path / 'ordinary.py'
    source.write_text('password = "illustrative literal"\nmarker = "opaque-known"\n')
    policy = HostOutputRedaction(snapshot=HostCredentialSnapshot(secret_values=('opaque-known',)))
    reader = ReadFileTool(tmp_path, max_chars=1000)
    reader._host_output_redaction = policy
    result = reader.execute({'path': str(source)})
    assert 'illustrative literal' in result.output
    assert 'opaque-known' not in result.output
    assert result.result_envelope['tool_output_policy']['redaction'] == 'source_code'
    search = SearchTextTool(tmp_path, max_matches=10)
    search._host_output_redaction = policy
    result = search.execute({'path': str(source), 'query': 'password', 'literal': True})
    assert 'illustrative literal' in result.output
    assert result.result_envelope['tool_output_policy']['redaction'] == 'source_code'
    reader._host_output_redaction = HostOutputRedaction(snapshot=policy.snapshot, force_default=True)
    assert 'illustrative literal' not in reader.execute({'path': str(source)}).output



def test_finish_masks_every_unresolved_known_prefix_but_keeps_ordinary_tail():
    secret = 'SYNTHETIC_credential_123456'
    for length in range(1, len(secret)):
        stream = HostOutputStreamRedactor(secret_values=(secret,))
        output = stream.feed(b'ordinary diagnostic ' + secret[:length].encode()) + stream.finish()
        assert output == b'ordinary diagnostic ' + b'*' * length
        assert stream.finish() == b''
    stream = HostOutputStreamRedactor(secret_values=(secret,))
    assert stream.feed(b'ordinary unfinished tail') + stream.finish() == b'ordinary unfinished tail'


def test_capture_cancel_flush_masks_unresolved_known_prefix():
    import time
    secret = 'SYNTHETIC_credential_123456'
    prefix = secret[:14]
    proc = subprocess.Popen([sys.executable, '-c',
        "import os,time; os.write(1,b'ordinary diagnostic SYNTHETIC_cred'); time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    capture = ProcessOutputCapture(proc, host_output_redaction=HostOutputRedaction(
        snapshot=HostCredentialSnapshot(secret_values=(secret,))))
    try:
        deadline = time.monotonic() + 5
        while capture.result()[2]['bytes_seen']['stdout'] < len('ordinary diagnostic ' + prefix):
            assert time.monotonic() < deadline
            capture.wait_changed(0.01, generation=capture.generation)
        proc.terminate()
        proc.wait(timeout=5)
        assert capture.finish(5)
        output, _, facts = capture.result()
        assert prefix not in output
        assert output == 'ordinary diagnostic ' + '*' * len(prefix)
        assert facts['bytes_seen']['stdout'] == len('ordinary diagnostic ' + prefix)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        capture.finish(1)


def test_native_pty_eof_flush_masks_unresolved_known_prefix():
    import errno
    import os

    import pytest
    if os.name != 'posix':
        pytest.skip('需要原生 POSIX PTY')
    import pty
    secret = 'SYNTHETIC_credential_123456'
    prefix = secret[:14]
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, '-c',
        "import os; os.write(1,b'ordinary diagnostic SYNTHETIC_cred')"],
        stdout=slave, stderr=slave, close_fds=True)
    stream = HostOutputStreamRedactor(secret_values=(secret,))
    output = bytearray()
    source_seen = 0
    try:
        while True:
            try:
                chunk = os.read(master, 7)
            except OSError as exc:
                assert exc.errno == errno.EIO
                break
            if not chunk:
                break
            source_seen += len(chunk)
            output.extend(stream.feed(chunk))
            if source_seen == len('ordinary diagnostic ' + prefix):
                proc.wait(timeout=5)
                os.close(slave)
                slave = -1
        output.extend(stream.finish())
        assert prefix.encode() not in output
        assert output == b'ordinary diagnostic ' + b'*' * len(prefix)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
