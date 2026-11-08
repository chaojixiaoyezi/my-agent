"""继承验证：两条追加路径都须保留单次 EIO，并在重试时正确封住原尾。"""
import errno
import os

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.tests import test_r5db_jsonl_append_tail_seal as tail_seal

append_records = tail_seal.append_records


@pytest.mark.parametrize("tail", [b'{"half": ', b'{"complete": 2}'])
def test_inherited_tail_and_eio(tmp_path, monkeypatch, append_records, tail):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b'{"keep": 1}\n' + tail)
    failure = OSError(errno.EIO, "injected source-open failure")
    original_open, failed = os.open, []

    def fail_once(open_path, flags, *args, **kwargs):
        if (os.fspath(open_path) == os.fspath(path)
                and flags & os.O_ACCMODE == os.O_RDWR and not failed):
            failed.append(True)
            raise failure
        return original_open(open_path, flags, *args, **kwargs)

    monkeypatch.setattr(json_io.os, "open", fail_once)
    with pytest.raises(OSError) as caught:
        append_records(path, [{"new": 3}])
    assert caught.value is failure and failed == [True]
    assert path.read_bytes() == b'{"keep": 1}\n' + tail and not tail_seal._torn(path)
    append_records(path, [{"new": 3}])
    report = json_io.read_jsonl_objects_report(path)
    expected = [{"keep": 1}, *([{"complete": 2}] if tail == b'{"complete": 2}' else []), {"new": 3}]
    assert report.load_errors == [] and report.records == expected
