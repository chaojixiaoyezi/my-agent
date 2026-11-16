"""阻止已核出生身份的运行服务被直接停止；仅覆盖明确支持的命令语法。"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..gateway_parts.daemon_metadata import process_identity_is_live


def active_service_stop_denial(command: str, *, data_root: Path,
        foreground_admin_full_access: bool = False, service_root: Path | None = None) -> dict[str, object] | None:
    if foreground_admin_full_access is not True:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    executable = Path(words[0]).name
    targets = set()
    service = ''
    if executable == 'kill':
        targets = {int(word) for word in words[1:] if word.isdecimal()}
    elif executable in {'my-agent', 'my_agent'} and len(words) >= 3:
        if words[1] in {'gateway', 'adapter', 'supervisor'} and words[2] in {'stop', 'restart'}:
            service = words[1]
    if not targets and not service:
        return None
    root = service_root if service_root is not None else Path(data_root) / 'owners' / 'local' / 'main' / 'workspace' / 'runtime' / 'services' / 'gateway'
    for role in ('gateway', 'adapter', 'supervisor'):
        if service and service != role:
            continue
        try:
            raw = read_bytes_beneath(root, (f'{role}.pid',), max_bytes=65536, require_dir_fd=True)
            record = json.loads(raw)
        except (OSError, ValueError, TypeError):
            continue
        if (not isinstance(record, dict) or record.get('kind') not in ({'my-agent-adapter', 'my-agent-gateway'} if role == 'adapter' else {f'my-agent-{role}'})
                or process_identity_is_live(record) is not True):
            continue
        if service or record.get('pid') in targets:
            return {'error_code': 'ACTIVE_SERVICE_SELF_STOP_BLOCKED', 'effect_outcome': 'not_started',
                'role': role, 'next_step': '未发送信号；请用 restart_gateway 安排安全重启，或 runtime_governance 核对后经原管理入口操作。'}
    return None
