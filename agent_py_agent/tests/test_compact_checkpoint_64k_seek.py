# LLM: 仅修改pytest样本，暖缓存后改坏旧头再合法追加，验证真实检查点读口fail-closed。
# 模块用途: 回归固定头尾指纹和增量扫描的旧前缀完整性。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation.compact_checkpoint_scan import checkpoint_row_lookup


# LLM: 100条记录超过64KiB，首字节不在上次尾锚中；追加行完全合法。
# 函数用途: 保证增量缓存不会遗漏旧账本头损坏。
def test_warm_checkpoint_head_corruption_plus_append_is_rejected(tmp_path):
    path = tmp_path / 'ledger.jsonl'
    path.write_text(''.join(json.dumps({'checkpoint_id': str(i), 'padding': 'x' * 2048}) + '\n'
                            for i in range(100)))
    with checkpoint_row_lookup(path) as lookup:
        assert lookup('99')['checkpoint_id'] == '99'
    with path.open('r+b') as handle:
        handle.write(b'!')
        handle.seek(0, 2)
        handle.write(b'{"checkpoint_id":"100"}\n')
    with pytest.raises(OSError, match='unreadable'):
        with checkpoint_row_lookup(path) as lookup:
            lookup('100')
