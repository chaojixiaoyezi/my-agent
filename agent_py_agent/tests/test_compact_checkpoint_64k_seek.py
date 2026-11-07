# LLM: 仅修改pytest样本，暖缓存后改坏旧头再合法追加，验证真实检查点读口fail-closed。
# 模块用途: 回归固定头尾指纹和增量扫描的旧前缀完整性。
from __future__ import annotations

import json
import logging

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint_scan as scan
from agent_py_agent.agent.conversation.compact_checkpoint_scan import checkpoint_row_lookup


# LLM: 100条记录超过64KiB，D-1允许垃圾头行告警跳过；head-digest核验仍强制从头重扫。
# 函数用途: 验证坏头行不返回陈旧缓存地址，幸存旧行与合法追加行仍可读取。
def test_warm_checkpoint_head_corruption_plus_append_skips_row_and_rescans(tmp_path, caplog):
    path = tmp_path / 'ledger.jsonl'
    path.write_text(''.join(json.dumps({'checkpoint_id': str(i), 'padding': 'x' * 2048}) + '\n'
                            for i in range(100)))
    with checkpoint_row_lookup(path) as lookup:
        assert lookup('99')['checkpoint_id'] == '99'
    with path.open('r+b') as handle:
        handle.write(b'!')
        handle.seek(0, 2)
        handle.write(b'{"checkpoint_id":"100"}\n')
    with caplog.at_level(logging.WARNING, logger=scan.__name__):
        with checkpoint_row_lookup(path) as lookup:
            assert lookup('0') is None
            assert lookup('100')['checkpoint_id'] == '100'
            assert lookup('99')['checkpoint_id'] == '99'
    assert sum('COMPACT_LEDGER_ROW_SKIPPED' in r.getMessage() for r in caplog.records) == 1
